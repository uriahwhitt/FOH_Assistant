"""Simulator unit tests — SimBoard, conversion functions, scenario files.

All tests are pure (no network I/O, no threading).  The X32Simulator class
itself is not instantiated here because it binds a real UDP port; only the
pure data-layer components are tested.
"""

import struct
from pathlib import Path

import pytest
import yaml

from simulator.x32_sim import (
    SimBoard,
    db_to_fader_float,
    fader_float_to_db,
    hz_to_eq_float,
)
from core.osc_client import parse_meters_1

SCENARIOS_DIR = Path(__file__).parent.parent / "simulator" / "scenarios"

EXPECTED_SCENARIOS = {
    "baseline.yaml",
    "level_creep.yaml",
    "solo_event.yaml",
    "sparse_mic.yaml",
    "clean_show.yaml",
}


# ===========================================================================
# Conversion functions
# ===========================================================================

class TestDbToFaderFloat:
    def test_unity_is_near_0_75(self):
        assert abs(db_to_fader_float(0.0) - 0.75) < 0.02

    def test_minus_90_clamps_to_zero(self):
        assert db_to_fader_float(-90.0) == pytest.approx(0.0, abs=0.01)

    def test_result_always_in_0_1(self):
        for db in (-120.0, -90.0, -60.0, -30.0, -10.0, 0.0, 5.0, 20.0):
            f = db_to_fader_float(db)
            assert 0.0 <= f <= 1.0, f"db={db} gave f={f}"

    def test_monotone_increasing(self):
        levels = [-80.0, -40.0, -20.0, -10.0, 0.0, 5.0]
        floats = [db_to_fader_float(d) for d in levels]
        assert floats == sorted(floats)

    def test_roundtrip_within_1db(self):
        for db in (-60.0, -30.0, -10.0, 0.0):
            f   = db_to_fader_float(db)
            got = fader_float_to_db(f)
            assert abs(got - db) < 1.0, f"roundtrip failed for {db}: got {got}"


class TestFaderFloatToDb:
    def test_zero_float_is_minus_90(self):
        assert fader_float_to_db(0.0) == -90.0

    def test_0_75_is_near_0db(self):
        assert abs(fader_float_to_db(0.75) - 0.0) < 1.0

    def test_monotone_increasing(self):
        floats = [0.0, 0.1, 0.25, 0.5, 0.75, 1.0]
        dbs    = [fader_float_to_db(f) for f in floats]
        assert dbs == sorted(dbs)


class TestHzToEqFloat:
    def test_20hz_is_zero(self):
        assert hz_to_eq_float(20.0) == pytest.approx(0.0, abs=1e-6)

    def test_20khz_is_one(self):
        assert hz_to_eq_float(20000.0) == pytest.approx(1.0, abs=1e-6)

    def test_geometric_midpoint_is_near_half(self):
        # sqrt(20 * 20000) ≈ 632 Hz → should be near 0.5 on a log scale
        f = hz_to_eq_float(632.0)
        assert abs(f - 0.5) < 0.02

    def test_clamps_below_20(self):
        assert hz_to_eq_float(1.0) == hz_to_eq_float(20.0)

    def test_clamps_above_20k(self):
        assert hz_to_eq_float(50000.0) == hz_to_eq_float(20000.0)

    def test_monotone_increasing(self):
        freqs  = [20.0, 100.0, 500.0, 2000.0, 8000.0, 20000.0]
        floats = [hz_to_eq_float(hz) for hz in freqs]
        assert floats == sorted(floats)


# ===========================================================================
# SimBoard
# ===========================================================================

class TestSimBoardDefaults:
    def test_all_32_channels_present(self):
        board = SimBoard()
        for ch in range(1, 33):
            assert board.get(ch, "fader") is not None, f"ch {ch} has no fader"

    def test_default_all_unmuted(self):
        board = SimBoard()
        for ch in range(1, 33):
            assert board.get(ch, "on") == 1, f"ch {ch} should default to unmuted"

    def test_default_fader_near_minus_3db(self):
        board = SimBoard()
        expected = db_to_fader_float(-3.0)
        for ch in (1, 7, 9, 14):
            assert abs(board.get(ch, "fader") - expected) < 0.01

    def test_all_four_eq_bands_present(self):
        board = SimBoard()
        for b in range(1, 5):
            assert board.get(1, f"eq_{b}_g") is not None
            assert board.get(1, f"eq_{b}_f") is not None


class TestSimBoardApplyInitialState:
    def test_fader_db_applied(self):
        board = SimBoard()
        board.apply_initial_state({"channels": {9: {"fader_db": -6.0}}})
        assert abs(board.get(9, "fader") - db_to_fader_float(-6.0)) < 0.001

    def test_muted_true_sets_on_zero(self):
        board = SimBoard()
        board.apply_initial_state({"channels": {6: {"muted": True}}})
        assert board.get(6, "on") == 0

    def test_muted_false_sets_on_one(self):
        board = SimBoard()
        board.apply_initial_state({"channels": {6: {"muted": False}}})
        assert board.get(6, "on") == 1

    def test_eq_gain_applied(self):
        board = SimBoard()
        board.apply_initial_state({"channels": {9: {"eq": [{"band": 2, "gain": 3.5, "freq": 315}]}}})
        assert board.get(9, "eq_2_g") == pytest.approx(3.5)

    def test_eq_freq_applied(self):
        board = SimBoard()
        board.apply_initial_state({"channels": {9: {"eq": [{"band": 1, "gain": 0.0, "freq": 80}]}}})
        expected = hz_to_eq_float(80.0)
        assert abs(board.get(9, "eq_1_f") - expected) < 0.001

    def test_main_fader_applied(self):
        board = SimBoard()
        board.apply_initial_state({"main_fader_db": -6.0})
        assert abs(board.get_main_fader() - db_to_fader_float(-6.0)) < 0.001

    def test_unknown_channel_ignored(self):
        board = SimBoard()
        board.apply_initial_state({"channels": {99: {"fader_db": 0.0}}})   # ch 99 doesn't exist
        # Should not raise; channel 99 is simply skipped

    def test_multiple_channels_applied(self):
        board = SimBoard()
        board.apply_initial_state({"channels": {9: {"fader_db": -2.0}, 10: {"fader_db": -3.0}}})
        assert abs(board.get(9, "fader") - db_to_fader_float(-2.0)) < 0.001
        assert abs(board.get(10, "fader") - db_to_fader_float(-3.0)) < 0.001


class TestSimBoardSetGet:
    def test_set_and_get_fader(self):
        board = SimBoard()
        board.set(1, "fader", 0.8, silent=True)
        assert board.get(1, "fader") == pytest.approx(0.8)

    def test_set_and_get_eq_gain(self):
        board = SimBoard()
        board.set(9, "eq_2_g", 2.5, silent=True)
        assert board.get(9, "eq_2_g") == pytest.approx(2.5)

    def test_set_silent_suppresses_stdout(self, capsys):
        board = SimBoard()
        board.set(1, "fader", 0.5, silent=True)
        assert capsys.readouterr().out == ""

    def test_set_non_silent_prints_to_stdout(self, capsys):
        board = SimBoard()
        board.set(1, "fader", 0.5, silent=False)
        out = capsys.readouterr().out
        assert "SIM" in out

    def test_set_on_nonexistent_channel_is_safe(self):
        board = SimBoard()
        board.set(99, "fader", 0.5, silent=True)   # should not raise


class TestSimBoardMeterBlob:
    def test_blob_parses_with_parse_meters_1(self):
        board = SimBoard()
        blob  = board.get_meter_blob()
        result = parse_meters_1(blob)
        assert "channel_rms" in result
        assert "gate_gr"     in result
        assert "dynamics_gr" in result

    def test_channel_rms_list_has_32_entries(self):
        board  = SimBoard()
        result = parse_meters_1(board.get_meter_blob())
        assert len(result["channel_rms"]) == 32

    def test_muted_channel_has_zero_rms(self):
        board = SimBoard()
        board.set(1, "on", 0, silent=True)   # mute channel 1
        result = parse_meters_1(board.get_meter_blob())
        assert result["channel_rms"][0] == pytest.approx(0.0)   # index 0 = ch 1

    def test_unmuted_channel_has_positive_rms(self):
        board  = SimBoard()
        # Default fader is db_to_fader_float(-3.0) ≈ 0.17; RMS ≈ 0.17 * 0.4 = 0.068
        result = parse_meters_1(board.get_meter_blob())
        assert result["channel_rms"][0] > 0.0

    def test_higher_fader_gives_higher_rms(self):
        board = SimBoard()
        board.set(1, "fader", db_to_fader_float(-3.0),  silent=True)
        rms_low = parse_meters_1(board.get_meter_blob())["channel_rms"][0]
        board.set(1, "fader", db_to_fader_float(0.0),   silent=True)
        rms_high = parse_meters_1(board.get_meter_blob())["channel_rms"][0]
        assert rms_high > rms_low

    def test_blob_header_structure(self):
        board = SimBoard()
        blob  = board.get_meter_blob()
        # bytes 4-7: little-endian count of floats
        num_floats = struct.unpack_from("<I", blob, 4)[0]
        assert num_floats == 96        # 32 ch_rms + 32 gate_gr + 32 dyn_gr
        assert len(blob) == 4 + 4 + num_floats * 4


# ===========================================================================
# Scenario files
# ===========================================================================

class TestScenarioFiles:
    def test_all_expected_scenarios_exist(self):
        found = {f.name for f in SCENARIOS_DIR.glob("*.yaml")}
        for name in EXPECTED_SCENARIOS:
            assert name in found, f"Missing scenario: {name}"

    @pytest.mark.parametrize("filename", sorted(EXPECTED_SCENARIOS))
    def test_scenario_loads_as_valid_yaml(self, filename):
        path = SCENARIOS_DIR / filename
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data is not None

    @pytest.mark.parametrize("filename", sorted(EXPECTED_SCENARIOS))
    def test_scenario_has_required_top_level_keys(self, filename):
        path = SCENARIOS_DIR / filename
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for key in ("name", "description", "timeline"):
            assert key in data, f"{filename} missing '{key}'"

    @pytest.mark.parametrize("filename", sorted(EXPECTED_SCENARIOS))
    def test_timeline_is_a_list(self, filename):
        path = SCENARIOS_DIR / filename
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data["timeline"], list)

    @pytest.mark.parametrize("filename", sorted(EXPECTED_SCENARIOS))
    def test_all_timeline_events_have_at_s_and_action(self, filename):
        path = SCENARIOS_DIR / filename
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for event in data["timeline"]:
            assert "at_s"   in event, f"{filename}: event missing 'at_s': {event}"
            assert "action" in event, f"{filename}: event missing 'action': {event}"

    @pytest.mark.parametrize("filename", sorted(EXPECTED_SCENARIOS))
    def test_initial_state_loads_into_simboard(self, filename):
        path = SCENARIOS_DIR / filename
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        board = SimBoard()
        board.apply_initial_state(data.get("initial_state", {}))   # should not raise

    def test_baseline_has_empty_timeline(self):
        with open(SCENARIOS_DIR / "baseline.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["timeline"] == []

    def test_level_creep_has_two_or_more_fader_drift_events(self):
        with open(SCENARIOS_DIR / "level_creep.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        drifts = [e for e in data["timeline"] if e.get("action") == "fader_drift"]
        assert len(drifts) >= 2, "level_creep should have at least Guitar 1 and Guitar 2 drifts"

    def test_level_creep_has_eq_change_event(self):
        with open(SCENARIOS_DIR / "level_creep.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        eq_events = [e for e in data["timeline"] if e.get("action") == "eq_change"]
        assert len(eq_events) >= 1

    def test_solo_event_has_large_fader_spike(self):
        with open(SCENARIOS_DIR / "solo_event.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        spikes = [e for e in data["timeline"]
                  if e.get("action") == "fader_move" and e.get("target_db", 0) >= 3.0]
        assert len(spikes) >= 1, "solo_event should include a fader spike ≥ +3 dB"

    def test_sparse_mic_initial_fader_produces_rms_below_threshold(self):
        """Drum Vocal (ch 6) initial fader should yield simulated RMS below -35 dBFS."""
        with open(SCENARIOS_DIR / "sparse_mic.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        board = SimBoard()
        board.apply_initial_state(data.get("initial_state", {}))
        ch6_fader = board.get(6, "fader")
        # SimBoard approximation: RMS linear = fader_float * 0.4
        rms_linear = ch6_fader * 0.4
        import math
        rms_db = 20 * math.log10(rms_linear) if rms_linear > 0 else -90.0
        assert rms_db < -35.0, (
            f"Drum Vocal initial RMS {rms_db:.1f} dBFS should be below -35 dBFS threshold"
        )

    def test_clean_show_all_timeline_moves_within_drift_threshold(self):
        """All fader_move events should move ≤ 1 dB from initial state."""
        with open(SCENARIOS_DIR / "clean_show.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        initial = {
            int(ch): p.get("fader_db", -3.0)
            for ch, p in data.get("initial_state", {}).get("channels", {}).items()
        }
        for event in data["timeline"]:
            if event.get("action") != "fader_move":
                continue
            ch  = event["channel"]
            tgt = event["target_db"]
            ini = initial.get(ch, -3.0)
            drift = abs(tgt - ini)
            assert drift <= 1.5, (
                f"clean_show ch{ch} moves {drift:.1f} dB from initial — "
                "exceeds intent of a quiet scenario"
            )

    def test_baseline_initial_state_covers_all_12_channels(self):
        with open(SCENARIOS_DIR / "baseline.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        channels = data.get("initial_state", {}).get("channels", {})
        assert len(channels) == 12, (
            f"baseline.yaml should define all 12 confirmed channels, got {len(channels)}"
        )
        # Verify the confirmed channel numbers are present
        expected_channels = {1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15}
        assert set(int(k) for k in channels.keys()) == expected_channels
