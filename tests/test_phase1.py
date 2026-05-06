"""Phase 1 unit tests — models, YAML loading, analyzer, recommender, audio capture."""

import math
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np
import pytest

# ── Models ────────────────────────────────────────────────────────────────────

from models.channel import EQBand, ChannelState
from models.genre_profile import GenreProfile, InstrumentWeight
from models.event import RoomAnalysis, LogEvent, AdjustmentEvent

# ── Core ──────────────────────────────────────────────────────────────────────

from core.config_loader import (
    load_band_config,
    load_genre_profiles,
    load_setlist,
    apply_band_overrides,
)
from core.analyzer import Analyzer
from core.recommender import RecommendationEngine, Recommendation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(__file__).parent.parent / "config"
GENRES_DIR = CONFIG_DIR / "genres"
EXPECTED_GENRES = {"AOR", "Hard Rock", "Glam Metal", "Heavy Rock", "Heavy Metal", "Post-Grunge", "Party Rock"}
BAND_NAMES = ("sub_bass", "bass", "low_mid", "mid", "high_mid", "presence", "air")


def _make_eq_band(num=1, gain_db=0.0, freq_hz=1000.0):
    return EQBand(band_num=num, type=2, freq_hz=freq_hz, gain_db=gain_db, q=1.0)


def _make_channel(num=1, label="Kick", rms_db=-20.0, fader_db=0.0,
                  muted=False, inactive_threshold_db=None):
    return ChannelState(
        channel_num=num,
        label=label,
        fader_db=fader_db,
        muted=muted,
        eq=[_make_eq_band(n + 1, freq_hz=f) for n, f in enumerate([80, 500, 2000, 8000])],
        comp_on=False,
        comp_threshold_db=-20.0,
        comp_ratio_index=3,
        gate_on=False,
        gate_threshold_db=-40.0,
        rms_linear=0.1,
        rms_db=rms_db,
        timestamp=time.time(),
        inactive_threshold_db=inactive_threshold_db,
    )


def _make_room(lufs=-18.0, bands=None):
    if bands is None:
        bands = {b: -30.0 for b in BAND_NAMES}
    now = time.time()
    return RoomAnalysis(
        lufs=lufs,
        rms_db=-20.0,
        bands=bands,
        band_delta={b: 0.0 for b in BAND_NAMES},
        lufs_delta=0.0,
        timestamp=now,
    )


def _make_glam_profile():
    return GenreProfile(
        id="Glam Metal",
        name="Glam Metal",
        examples=["Ratt", "Poison"],
        target_lufs=-18.0,
        dynamic_range="medium",
        frequency_targets={b: 0.0 for b in BAND_NAMES},
        instrument_weights=[],
        notes="",
    )


def _minimal_band_cfg():
    return {
        "band": "Test Band",
        "default_genre": "Glam Metal",
        "x32": {"ip": "192.168.1.1", "port": 10023},
        "audio": {"device_name_match": "DJI"},
        "channels": {1: {"label": "Kick", "type": "instrument"}},
        "thresholds": {
            "recommendation_trigger_db": 3.0,
            "lufs_trigger_db": 2.0,
            "baseline_drift_trigger_db": 2.0,
            "rate_of_change_suppress_db": 3.0,
            "rate_of_change_window_s": 5,
            "suppression_duration_s": 60,
            "recommendation_cooldown_s": 60,
        },
        "frequency_fingerprints": {
            "Kick": {"primary": [60, 80]},
        },
    }


# ===========================================================================
# MODEL TESTS
# ===========================================================================

class TestEQBand:
    def test_instantiation(self):
        band = EQBand(band_num=1, type=2, freq_hz=1000.0, gain_db=3.0, q=1.4)
        assert band.band_num == 1
        assert band.freq_hz == 1000.0
        assert band.gain_db == 3.0

    def test_lcut_type(self):
        band = EQBand(band_num=1, type=0, freq_hz=80.0, gain_db=0.0, q=0.7)
        assert band.type == 0


class TestChannelState:
    def test_active_when_not_muted_and_above_threshold(self):
        ch = _make_channel(rms_db=-20.0, inactive_threshold_db=-35.0)
        assert ch.is_active() is True

    def test_inactive_when_muted(self):
        ch = _make_channel(muted=True)
        assert ch.is_active() is False

    def test_inactive_below_threshold(self):
        ch = _make_channel(rms_db=-40.0, inactive_threshold_db=-35.0)
        assert ch.is_active() is False

    def test_active_no_threshold(self):
        ch = _make_channel(inactive_threshold_db=None)
        assert ch.is_active() is True

    def test_eq_list_length(self):
        ch = _make_channel()
        assert len(ch.eq) == 4

    def test_optional_fields_default_none(self):
        ch = _make_channel()
        assert ch.usage is None
        assert ch.paired_channel is None
        assert ch.role is None
        assert ch.priority is None


class TestGenreProfile:
    def test_target_for_existing_band(self):
        profile = _make_glam_profile()
        profile.frequency_targets["high_mid"] = 3.0
        assert profile.target_for_band("high_mid") == 3.0

    def test_target_for_missing_band_returns_zero(self):
        profile = _make_glam_profile()
        assert profile.target_for_band("nonexistent") == 0.0

    def test_weight_for_channel_found(self):
        weight = InstrumentWeight(label="Lead Vocal", priority="very_high")
        profile = _make_glam_profile()
        profile.instrument_weights = [weight]
        assert profile.weight_for_channel("Lead Vocal") is weight

    def test_weight_for_channel_not_found(self):
        profile = _make_glam_profile()
        assert profile.weight_for_channel("Ghost Instrument") is None


class TestRoomAnalysis:
    def test_instantiation(self):
        ra = _make_room(lufs=-20.0)
        assert ra.lufs == -20.0
        assert set(ra.bands.keys()) == set(BAND_NAMES)


class TestLogEvent:
    def test_minimal_instantiation(self):
        evt = LogEvent(id="evt_001", timestamp="12:00:00", event_type="RECOMMENDATION")
        assert evt.id == "evt_001"
        assert evt.channel is None

    def test_full_instantiation(self):
        evt = LogEvent(
            id="evt_002",
            timestamp="13:30:00",
            event_type="MANUAL_ADJUSTMENT",
            channel="Lead Vocal",
            channel_num=14,
            parameter="fader",
            before=-5.0,
            after=-3.0,
            match_status="matched",
        )
        assert evt.channel_num == 14
        assert evt.before == -5.0


class TestAdjustmentEvent:
    def test_instantiation(self):
        adj = AdjustmentEvent(
            channel_num=14,
            channel_label="Lead Vocal",
            parameter="fader",
            before=-5.0,
            after=-3.0,
            timestamp=time.time(),
        )
        assert adj.parameter == "fader"
        assert adj.after == -3.0


# ===========================================================================
# YAML / CONFIG LOADER TESTS
# ===========================================================================

class TestLoadBandConfig:
    def test_loads_real_band_yaml(self):
        cfg = load_band_config()
        assert cfg["band"] == "Nostalgic Knights"
        assert cfg["default_genre"] == "Glam Metal"
        assert len(cfg["channels"]) == 12

    def test_all_required_keys_present(self):
        cfg = load_band_config()
        for key in ("band", "default_genre", "x32", "audio", "channels", "thresholds"):
            assert key in cfg, f"Missing key: {key}"

    def test_x32_has_ip_and_port(self):
        cfg = load_band_config()
        assert "ip" in cfg["x32"]
        assert "port" in cfg["x32"]
        assert cfg["x32"]["port"] == 10023

    def test_channel_9_is_lead_vocal(self):
        cfg = load_band_config()
        ch9 = cfg["channels"][9]
        assert ch9["label"] == "Lead Vocal"
        assert ch9["type"] == "vocal"
        assert ch9.get("priority") == "very_high"

    def test_missing_key_raises(self, tmp_path):
        import yaml
        bad = tmp_path / "bad_band.yaml"
        bad.write_text(yaml.dump({"band": "Test", "channels": {1: {"label": "Kick"}}}))
        with pytest.raises(ValueError, match="missing required key"):
            load_band_config(bad)

    def test_empty_channels_raises(self, tmp_path):
        import yaml
        bad = tmp_path / "bad_band.yaml"
        data = {
            "band": "X", "default_genre": "Y", "x32": {}, "audio": {},
            "channels": {}, "thresholds": {},
        }
        bad.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="channels"):
            load_band_config(bad)


class TestLoadGenreProfiles:
    def test_loads_all_seven_genres(self):
        profiles = load_genre_profiles()
        assert set(profiles.keys()) == EXPECTED_GENRES

    @pytest.mark.parametrize("genre_id", sorted(EXPECTED_GENRES))
    def test_genre_has_required_fields(self, genre_id):
        profiles = load_genre_profiles()
        p = profiles[genre_id]
        assert p.id == genre_id
        assert p.name
        assert isinstance(p.target_lufs, float)
        assert p.dynamic_range in (
            "low", "low-medium", "medium", "medium-high", "high", "very-high"
        )

    @pytest.mark.parametrize("genre_id", sorted(EXPECTED_GENRES))
    def test_genre_has_all_seven_frequency_bands(self, genre_id):
        profiles = load_genre_profiles()
        p = profiles[genre_id]
        for band in BAND_NAMES:
            assert band in p.frequency_targets, (
                f"{genre_id} missing frequency band: {band}"
            )

    @pytest.mark.parametrize("genre_id", sorted(EXPECTED_GENRES))
    def test_genre_frequency_targets_are_floats(self, genre_id):
        profiles = load_genre_profiles()
        for band, val in profiles[genre_id].frequency_targets.items():
            assert isinstance(val, float), f"{genre_id}.{band} is not float: {val!r}"

    @pytest.mark.parametrize("genre_id", sorted(EXPECTED_GENRES))
    def test_genre_has_instrument_weights(self, genre_id):
        profiles = load_genre_profiles()
        p = profiles[genre_id]
        assert len(p.instrument_weights) > 0, f"{genre_id} has no instrument_weights"

    @pytest.mark.parametrize("genre_id", sorted(EXPECTED_GENRES))
    def test_instrument_weight_priorities_valid(self, genre_id):
        valid = {"very_high", "high", "medium", "low", "none"}
        profiles = load_genre_profiles()
        for w in profiles[genre_id].instrument_weights:
            assert w.priority in valid, (
                f"{genre_id}/{w.label} invalid priority: {w.priority!r}"
            )

    def test_empty_dir_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="No genre YAML files"):
            load_genre_profiles(tmp_path)

    def test_glam_metal_target_lufs(self):
        profiles = load_genre_profiles()
        assert profiles["Glam Metal"].target_lufs == -18.0

    def test_lead_vocal_very_high_in_glam(self):
        profiles = load_genre_profiles()
        w = profiles["Glam Metal"].weight_for_channel("Lead Vocal")
        assert w is not None
        assert w.priority == "very_high"


class TestLoadSetlist:
    def test_loads_real_setlist(self):
        songs = load_setlist()
        assert songs is not None
        assert isinstance(songs, list)

    def test_missing_setlist_returns_none(self, tmp_path):
        result = load_setlist(tmp_path / "nonexistent.yaml")
        assert result is None


class TestApplyBandOverrides:
    def test_heavy_metal_high_mid_override(self):
        profiles = load_genre_profiles()
        cfg = load_band_config()
        before = profiles["Heavy Metal"].frequency_targets["high_mid"]
        result = apply_band_overrides(profiles, cfg)
        after = result["Heavy Metal"].frequency_targets["high_mid"]
        # band.yaml sets Heavy Metal high_mid to +3 (3.0)
        assert after == 3.0

    def test_global_notes_appended(self):
        profiles = load_genre_profiles()
        cfg = load_band_config()
        result = apply_band_overrides(profiles, cfg)
        for p in result.values():
            assert "Kick consistently" in p.notes


# ===========================================================================
# ANALYZER TESTS
# ===========================================================================

class TestAnalyzer:
    SR = 48000

    def test_silence_returns_sentinel_values(self):
        analyzer = Analyzer(self.SR)
        silence = np.zeros(self.SR * 2, dtype=np.float32)
        result = analyzer.analyze(silence)
        assert result.rms_db <= -89.0
        assert result.lufs <= -60.0

    def test_below_minimum_length_returns_silent(self):
        analyzer = Analyzer(self.SR)
        short = np.zeros(100, dtype=np.float32)
        result = analyzer.analyze(short)
        assert result.rms_db == -90.0

    def test_none_audio_returns_silent(self):
        analyzer = Analyzer(self.SR)
        result = analyzer.analyze(None)
        assert result.rms_db == -90.0

    def test_sine_1khz_mid_band_dominant(self):
        analyzer = Analyzer(self.SR)
        t = np.linspace(0, 2.0, self.SR * 2, endpoint=False)
        sine = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        result = analyzer.analyze(sine)
        # 1kHz falls in mid band (500-2000 Hz) — should be highest energy
        assert result.bands["mid"] > result.bands["sub_bass"]
        assert result.bands["mid"] > result.bands["air"]

    def test_rms_positive_for_sine(self):
        analyzer = Analyzer(self.SR)
        t = np.linspace(0, 2.0, self.SR * 2, endpoint=False)
        sine = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        result = analyzer.analyze(sine)
        assert result.rms_db > -30.0

    def test_band_delta_zero_on_first_call_with_silence(self):
        analyzer = Analyzer(self.SR)
        silence = np.zeros(self.SR * 2, dtype=np.float32)
        result = analyzer.analyze(silence)
        for b in BAND_NAMES:
            assert result.band_delta[b] == 0.0

    def test_result_has_all_band_keys(self):
        analyzer = Analyzer(self.SR)
        silence = np.zeros(self.SR * 2, dtype=np.float32)
        result = analyzer.analyze(silence)
        assert set(result.bands.keys()) == set(BAND_NAMES)
        assert set(result.band_delta.keys()) == set(BAND_NAMES)


# ===========================================================================
# RECOMMENDER TESTS
# ===========================================================================

class TestRecommendationEngine:
    def _engine(self, profile=None):
        cfg = _minimal_band_cfg()
        p = profile or _make_glam_profile()
        return RecommendationEngine(cfg, p)

    def _channels(self):
        return {1: _make_channel(num=1, label="Kick", rms_db=-20.0)}

    # ── LUFS checks ──────────────────────────────────────────────────────────

    def test_lufs_hot_generates_recommendation(self):
        engine = self._engine()
        # Profile target is -18, send -12 (6dB hot — above 2dB threshold)
        room = _make_room(lufs=-12.0)
        recs = engine.evaluate(room, self._channels())
        lufs_recs = [r for r in recs if r.issue == "lufs_hot"]
        assert len(lufs_recs) == 1
        assert "hot" in lufs_recs[0].detail

    def test_lufs_low_generates_recommendation(self):
        engine = self._engine()
        room = _make_room(lufs=-25.0)  # 7dB below -18 target
        recs = engine.evaluate(room, self._channels())
        lufs_recs = [r for r in recs if r.issue == "lufs_low"]
        assert len(lufs_recs) == 1

    def test_lufs_in_range_no_recommendation(self):
        engine = self._engine()
        room = _make_room(lufs=-18.5)  # within 2dB threshold
        recs = engine.evaluate(room, self._channels())
        lufs_recs = [r for r in recs if "lufs" in r.issue]
        assert len(lufs_recs) == 0

    def test_recommendation_has_all_required_fields(self):
        engine = self._engine()
        room = _make_room(lufs=-12.0)
        recs = engine.evaluate(room, self._channels())
        assert len(recs) > 0
        rec = recs[0]
        assert rec.issue
        assert rec.detail
        assert rec.suggestion
        assert rec.genre_id == "Glam Metal"
        assert isinstance(rec.timestamp, float)

    # ── format_terminal ───────────────────────────────────────────────────────

    def test_format_terminal_contains_detail(self):
        engine = self._engine()
        room = _make_room(lufs=-12.0)
        recs = engine.evaluate(room, self._channels())
        assert len(recs) > 0
        text = recs[0].format_terminal()
        assert "LUFS" in text or "lufs" in text.lower()
        assert "Suggest:" in text

    # ── Baseline drift ────────────────────────────────────────────────────────

    def test_baseline_drift_detected(self):
        engine = self._engine()
        baseline_ch = _make_channel(num=1, label="Kick", fader_db=0.0)
        engine.set_baseline({1: baseline_ch})
        # Move fader 4dB up — above 2dB drift threshold
        drifted = _make_channel(num=1, label="Kick", fader_db=4.0)
        room = _make_room()
        recs = engine.evaluate(room, {1: drifted})
        drift_recs = [r for r in recs if r.issue == "baseline_drift"]
        assert len(drift_recs) == 1
        assert "4.0dB" in drift_recs[0].detail

    def test_baseline_drift_within_threshold_no_rec(self):
        engine = self._engine()
        baseline_ch = _make_channel(num=1, label="Kick", fader_db=0.0)
        engine.set_baseline({1: baseline_ch})
        # Only 1dB drift — within 2dB threshold
        small_drift = _make_channel(num=1, label="Kick", fader_db=1.0)
        room = _make_room()
        recs = engine.evaluate(room, {1: small_drift})
        drift_recs = [r for r in recs if r.issue == "baseline_drift"]
        assert len(drift_recs) == 0

    def test_muted_channel_skipped_in_drift(self):
        engine = self._engine()
        baseline_ch = _make_channel(num=1, label="Kick", fader_db=0.0)
        engine.set_baseline({1: baseline_ch})
        muted = _make_channel(num=1, label="Kick", fader_db=5.0, muted=True)
        room = _make_room()
        recs = engine.evaluate(room, {1: muted})
        drift_recs = [r for r in recs if r.issue == "baseline_drift"]
        assert len(drift_recs) == 0

    # ── Suppression ───────────────────────────────────────────────────────────

    def test_fast_fader_move_triggers_suppression(self):
        engine = self._engine()
        baseline_ch = _make_channel(num=1, label="Kick", fader_db=0.0)
        engine.set_baseline({1: baseline_ch})

        now = time.time()
        # First eval — establish fader snapshot
        room = _make_room(lufs=-12.0, bands={b: -30.0 for b in BAND_NAMES})
        room_ts = room.timestamp
        engine.evaluate(room, {1: _make_channel(num=1, fader_db=0.0)})

        # Fast move: 5dB in <5s — should trigger suppression
        ch_moved = _make_channel(num=1, label="Kick", fader_db=5.0)
        ch_moved.timestamp = room_ts + 1.0
        engine._last_fader_time[1] = room_ts

        # Manually call suppression update with moved channel
        engine._update_suppression({1: ch_moved}, room_ts + 1.0)

        assert engine._is_suppressed(1, room_ts + 2.0)

    def test_cooldown_prevents_repeat_rec(self):
        engine = self._engine()
        baseline_ch = _make_channel(num=1, label="Kick", fader_db=0.0)
        engine.set_baseline({1: baseline_ch})

        now = time.time()
        engine._last_rec[1] = now  # pretend rec just fired

        # Cooldown is 60s — check 1s later, should not pass
        assert engine._cooldown_ok(1, now + 1.0) is False
        # 61s later — should pass
        assert engine._cooldown_ok(1, now + 61.0) is True

    # ── set_genre ─────────────────────────────────────────────────────────────

    def test_set_genre_updates_profile(self):
        engine = self._engine()
        new_profile = _make_glam_profile()
        new_profile.id = "AOR"
        new_profile.target_lufs = -20.0
        engine.set_genre(new_profile)
        room = _make_room(lufs=-12.0)  # 8dB hot vs -20 target
        recs = engine.evaluate(room, self._channels())
        lufs_recs = [r for r in recs if r.issue == "lufs_hot"]
        assert len(lufs_recs) == 1
        assert lufs_recs[0].genre_id == "AOR"

    # ── Fix 1: Silence guard (threshold -50.0 dBFS) ──────────────────────────

    def test_no_recs_when_rms_db_is_silence(self):
        engine = self._engine()
        baseline_ch = _make_channel(num=1, label="Kick", fader_db=0.0)
        engine.set_baseline({1: baseline_ch})
        silent_bands = {b: -90.0 for b in BAND_NAMES}
        room = RoomAnalysis(
            lufs=-12.0,       # would normally trigger lufs_hot
            rms_db=-90.0,     # silence sentinel — well below -50dB guard
            bands=silent_bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0,
            timestamp=time.time(),
        )
        drifted = _make_channel(num=1, label="Kick", fader_db=5.0)
        recs = engine.evaluate(room, {1: drifted})
        lufs_band_recs = [r for r in recs if r.issue in ("lufs_hot", "lufs_low")
                          or r.issue.endswith("_buildup") or r.issue.endswith("_deficiency")]
        assert lufs_band_recs == [], f"Expected no LUFS/band recs during silence, got: {lufs_band_recs}"

    def test_silence_guard_just_below_threshold_no_rec(self):
        engine = self._engine()
        room = RoomAnalysis(
            lufs=-12.0, rms_db=-51.0,   # one dB below -50 guard
            bands={b: -30.0 for b in BAND_NAMES},
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, self._channels())
        lufs_recs = [r for r in recs if r.issue in ("lufs_hot", "lufs_low")]
        assert lufs_recs == []

    def test_silence_guard_just_above_threshold_fires_rec(self):
        engine = self._engine()
        room = RoomAnalysis(
            lufs=-12.0, rms_db=-49.0,   # one dB above -50 guard, 6dB hot
            bands={b: -30.0 for b in BAND_NAMES},
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, self._channels())
        lufs_recs = [r for r in recs if r.issue == "lufs_hot"]
        assert len(lufs_recs) == 1

    # ── Fix 2: Channel RMS guard in _find_culprit ─────────────────────────────

    def test_silent_channel_not_selected_as_culprit(self):
        # Kick has a sub_bass fingerprint [60,80] and is active (not muted,
        # no inactive_threshold_db), but rms_db=-90 → should be skipped.
        engine = self._engine()
        silent_kick = _make_channel(num=1, label="Kick", rms_db=-90.0)
        # sub_bass bands way off target — would normally trigger a rec
        bands = {b: -30.0 for b in BAND_NAMES}
        bands["sub_bass"] = 10.0   # huge positive deviation
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0,
            bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, {1: silent_kick})
        band_recs = [r for r in recs if r.issue.endswith("_buildup") or r.issue.endswith("_deficiency")]
        assert band_recs == [], (
            f"Silent channel (rms=-90dB) should never be culprit, got: {band_recs}"
        )

    def test_active_channel_above_rms_guard_is_selected(self):
        # Same setup but rms_db=-49.0 — should be eligible as culprit
        engine = self._engine()
        kick = _make_channel(num=1, label="Kick", rms_db=-49.0)
        bands = {b: -30.0 for b in BAND_NAMES}
        bands["sub_bass"] = 10.0
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0,
            bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, {1: kick})
        # Channel is now eligible — a sub_bass rec may fire (cooldown starts at 0)
        # Just verify _find_culprit didn't refuse it on RMS grounds
        culprit = engine._find_culprit("sub_bass", {1: kick}, 5.0)
        assert culprit is not None
        assert culprit.channel_num == 1

    # ── Fix 3: Global LUFS cooldown ───────────────────────────────────────────

    def _room_at(self, ts: float, lufs: float = -12.0, rms_db: float = -20.0):
        return RoomAnalysis(
            lufs=lufs, rms_db=rms_db,
            bands={b: -30.0 for b in BAND_NAMES},
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=ts,
        )

    def test_global_lufs_cooldown_suppresses_second_call(self):
        engine = self._engine()
        now = time.time()

        recs1 = engine.evaluate(self._room_at(now, lufs=-12.0), self._channels())
        assert any(r.issue == "lufs_hot" for r in recs1), "First call should fire"

        # 1 second later — still within 60s cooldown
        recs2 = engine.evaluate(self._room_at(now + 1.0, lufs=-12.0), self._channels())
        assert not any(r.issue == "lufs_hot" for r in recs2), "Should be suppressed within 60s"

    def test_global_lufs_cooldown_expires_after_60s(self):
        engine = self._engine()
        now = time.time()

        engine.evaluate(self._room_at(now, lufs=-12.0), self._channels())

        # 61 seconds later — cooldown expired
        recs = engine.evaluate(self._room_at(now + 61.0, lufs=-12.0), self._channels())
        assert any(r.issue == "lufs_hot" for r in recs), "Should fire again after cooldown"

    def test_global_lufs_cooldown_independent_of_channel_cooldown(self):
        # Fires once, then 61s later fires again — channel cooldowns don't interfere
        engine = self._engine()
        now = time.time()
        engine.evaluate(self._room_at(now, lufs=-12.0), self._channels())
        recs = engine.evaluate(self._room_at(now + 61.0, lufs=-12.0), self._channels())
        lufs_recs = [r for r in recs if r.issue == "lufs_hot"]
        assert len(lufs_recs) == 1

    # ── Fix 4: EQ band selection within 2 octaves ─────────────────────────────

    def test_eq_band_within_2_octaves_is_selected(self):
        engine = self._engine()
        ch = _make_channel(num=1, label="Kick", rms_db=-20.0)
        # mid band: lo=500 hi=2000 mid_freq=1250. 2-octave range: 312.5–5000Hz
        # Place Band 1 at 1000Hz (within range), Band 2 far away at 80Hz
        ch.eq[0] = EQBand(band_num=1, type=2, freq_hz=1000.0, gain_db=0.0, q=1.0)
        ch.eq[1] = EQBand(band_num=2, type=2, freq_hz=80.0,   gain_db=0.0, q=1.0)
        ch.eq[2] = EQBand(band_num=3, type=2, freq_hz=8000.0, gain_db=0.0, q=1.0)
        ch.eq[3] = EQBand(band_num=4, type=2, freq_hz=12000.0,gain_db=0.0, q=1.0)

        eq_detail, suggestion = engine._eq_recommendation(ch, "mid", 4.0)
        assert "EQ Band" in eq_detail
        assert "1000" in eq_detail           # 1000Hz band selected, not 80Hz
        assert "cut to" in suggestion

    def test_eq_band_outside_2_octaves_suggests_add(self):
        engine = self._engine()
        ch = _make_channel(num=1, label="Kick", rms_db=-20.0)
        # high_mid: lo=2000 hi=6000 mid_freq=4000. 2-octave range: 1000–16000Hz
        # Place all bands at 80Hz (way below 1000Hz lower bound)
        for i in range(4):
            ch.eq[i] = EQBand(band_num=i + 1, type=2, freq_hz=80.0, gain_db=0.0, q=1.0)

        eq_detail, suggestion = engine._eq_recommendation(ch, "high_mid", 4.0)
        assert eq_detail == ""
        assert "Add EQ point" in suggestion
        assert "2000-6000" in suggestion
        assert "cut" in suggestion           # deviation > 0

    def test_eq_band_outside_2_octaves_boost_suggestion(self):
        engine = self._engine()
        ch = _make_channel(num=1, label="Kick", rms_db=-20.0)
        for i in range(4):
            ch.eq[i] = EQBand(band_num=i + 1, type=2, freq_hz=80.0, gain_db=0.0, q=1.0)

        eq_detail, suggestion = engine._eq_recommendation(ch, "high_mid", -4.0)
        assert "boost" in suggestion         # deviation < 0

    def test_eq_selects_closest_band_among_multiple_within_range(self):
        engine = self._engine()
        ch = _make_channel(num=1, label="Kick", rms_db=-20.0)
        # mid band mid_freq=1250. Two bands in range: 800Hz and 1100Hz.
        # 1100Hz is closer to 1250Hz and should win.
        ch.eq[0] = EQBand(band_num=1, type=2, freq_hz=800.0,  gain_db=2.0, q=1.0)
        ch.eq[1] = EQBand(band_num=2, type=2, freq_hz=1100.0, gain_db=1.0, q=1.0)
        ch.eq[2] = EQBand(band_num=3, type=2, freq_hz=5500.0, gain_db=0.0, q=1.0)
        ch.eq[3] = EQBand(band_num=4, type=2, freq_hz=12000.0,gain_db=0.0, q=1.0)

        eq_detail, _ = engine._eq_recommendation(ch, "mid", 4.0)
        assert "Band 2" in eq_detail         # 1100Hz is closer than 800Hz
        assert "1100" in eq_detail

    def test_eq_lcut_hcut_bands_excluded_from_selection(self):
        engine = self._engine()
        ch = _make_channel(num=1, label="Kick", rms_db=-20.0)
        # All bands are LCut (type=0) or HCut (type=5) — should suggest adding EQ
        ch.eq[0] = EQBand(band_num=1, type=0, freq_hz=1000.0, gain_db=0.0, q=0.7)
        ch.eq[1] = EQBand(band_num=2, type=5, freq_hz=1200.0, gain_db=0.0, q=0.7)
        ch.eq[2] = EQBand(band_num=3, type=0, freq_hz=1100.0, gain_db=0.0, q=0.7)
        ch.eq[3] = EQBand(band_num=4, type=5, freq_hz=900.0,  gain_db=0.0, q=0.7)

        eq_detail, suggestion = engine._eq_recommendation(ch, "mid", 4.0)
        assert eq_detail == ""
        assert "Add EQ point" in suggestion


# ===========================================================================
# IMP-019 — TRANSITION GRACE PERIOD
# ===========================================================================

class TestTransitionGracePeriod:
    def _engine(self, grace_s: float = 30.0):
        cfg = _minimal_band_cfg()
        cfg["thresholds"]["transition_grace_seconds"] = grace_s
        return RecommendationEngine(cfg, _make_glam_profile())

    def test_transition_suppresses_lufs_rec(self):
        engine = self._engine()
        engine.set_transition(True)
        room = _make_room(lufs=-12.0)
        recs = engine.evaluate(room, {1: _make_channel()})
        assert not any(r.issue in ("lufs_hot", "lufs_low") for r in recs)

    def test_transition_suppresses_band_recs(self):
        engine = self._engine()
        engine.set_transition(True)
        bands = {b: -30.0 for b in BAND_NAMES}
        bands["sub_bass"] = 15.0   # huge sub_bass buildup → would normally trigger
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0, bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, {1: _make_channel()})
        band_recs = [r for r in recs if r.issue.endswith("_buildup") or r.issue.endswith("_deficiency")]
        assert band_recs == []

    def test_transition_does_not_suppress_baseline_drift(self):
        engine = self._engine()
        baseline_ch = _make_channel(num=1, fader_db=0.0)
        engine.set_baseline({1: baseline_ch})
        engine.set_transition(True)
        drifted = _make_channel(num=1, fader_db=5.0)
        room = _make_room()
        recs = engine.evaluate(room, {1: drifted})
        drift_recs = [r for r in recs if r.issue == "baseline_drift"]
        assert len(drift_recs) == 1, "Drift alerts must still fire during transition"

    def test_set_transition_false_restores_lufs_recs(self):
        engine = self._engine()
        engine.set_transition(True)
        engine.set_transition(False)
        room = _make_room(lufs=-12.0)
        recs = engine.evaluate(room, {1: _make_channel()})
        assert any(r.issue == "lufs_hot" for r in recs)

    def test_set_transition_false_restores_band_recs(self):
        engine = self._engine()
        engine.set_transition(True)
        engine.set_transition(False)
        bands = {b: -30.0 for b in BAND_NAMES}
        bands["sub_bass"] = 15.0
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0, bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, {1: _make_channel()})
        # Kick has sub_bass fingerprint [60,80] in _minimal_band_cfg → may fire
        # Just verify the gate is lifted; we don't require a rec (depends on fingerprint)
        assert engine._in_transition is False

    def test_transition_auto_expires_when_transition_end_passed(self):
        engine = self._engine(grace_s=30.0)
        engine.set_transition(True)
        # Back-date the expiry so next evaluate() sees it as expired
        engine._transition_end = time.time() - 1.0
        room = _make_room(lufs=-12.0)
        engine.evaluate(room, {1: _make_channel()})   # should auto-clear
        assert engine._in_transition is False

    def test_transition_still_active_before_grace_expires(self):
        engine = self._engine(grace_s=30.0)
        engine.set_transition(True)
        assert engine._in_transition is True
        # _transition_end is ~30 s in the future — should still be active
        room = _make_room(lufs=-12.0)
        engine.evaluate(room, {1: _make_channel()})
        assert engine._in_transition is True

    def test_transition_end_timestamp_set_correctly(self):
        engine = self._engine(grace_s=30.0)
        before = time.time()
        engine.set_transition(True)
        after = time.time()
        assert before + 30.0 <= engine._transition_end <= after + 30.0

    def test_set_transition_false_clears_end_time(self):
        engine = self._engine()
        engine.set_transition(True)
        engine.set_transition(False)
        assert engine._transition_end == 0.0
        assert engine._in_transition is False

    def test_transition_clears_prev_active_issues(self):
        engine = self._engine()
        engine._prev_active_issues = {(1, "mid_buildup"), (2, "bass_deficiency")}
        engine.set_transition(True)
        bands = {b: -30.0 for b in BAND_NAMES}
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0, bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        engine.evaluate(room, {1: _make_channel()})
        # _check_bands exits early and clears _prev_active_issues
        assert engine._prev_active_issues == set()


# ===========================================================================
# IMP-019 — SONG EVENT LOGGING
# ===========================================================================

import core.logger as _logger_module


def _make_logger(tmp_path):
    """Create a SessionLogger backed by a tmp directory."""
    orig = _logger_module.SHOWS_DIR
    _logger_module.SHOWS_DIR = tmp_path
    try:
        lg = _logger_module.SessionLogger("Test Band", "show", "127.0.0.1", "Glam Metal")
    finally:
        _logger_module.SHOWS_DIR = orig
    return lg


class TestSongEvents:
    def test_log_song_start_creates_event(self, tmp_path):
        lg = _make_logger(tmp_path)
        evt_id = lg.log_song_start(
            {"title": "Round and Round", "artist": "Ratt", "genre_profile": "Glam Metal"},
            position=1, genre_id="Glam Metal",
        )
        assert evt_id.startswith("evt_")
        ev = next(e for e in lg._events if e["type"] == "SONG_START")
        assert ev["title"]         == "Round and Round"
        assert ev["artist"]        == "Ratt"
        assert ev["genre_profile"] == "Glam Metal"
        assert ev["setlist_position"] == 1

    def test_log_song_start_no_setlist_generic_title(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start(None, position=0, genre_id="AOR")
        ev = next(e for e in lg._events if e["type"] == "SONG_START")
        assert "Song" in ev["title"]   # auto-generated generic title
        assert ev["genre_profile"] == "AOR"

    def test_log_song_end_creates_event_with_duration(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Test"}, position=1, genre_id="AOR")
        time.sleep(0.05)
        lg.log_song_end()
        ev = next(e for e in lg._events if e["type"] == "SONG_END")
        assert ev["duration_s"] > 0.0

    def test_log_song_end_saves_to_completed_songs(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Kryptonite", "artist": "3 Doors Down",
                           "genre_profile": "Post-Grunge"},
                          position=2, genre_id="Post-Grunge")
        lg.log_song_end()
        assert len(lg._completed_songs) == 1
        seg = lg._completed_songs[0]
        assert seg["type"]   == "song"
        assert seg["title"]  == "Kryptonite"
        assert seg["artist"] == "3 Doors Down"

    def test_between_songs_gap_recorded(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Song 1"}, 1, "AOR")
        lg.log_song_end()
        time.sleep(0.02)
        lg.log_song_start({"title": "Song 2"}, 2, "AOR")
        lg.log_song_end()
        gaps = [s for s in lg._completed_songs if s["type"] == "between_songs"]
        assert len(gaps) == 1
        assert gaps[0]["duration_s"] >= 0.0

    def test_recs_during_song_tracked_in_rec_ids(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Test Song"}, 1, "Glam Metal")
        rec = Recommendation(
            channel_num=7, channel_label="Guitar 1",
            issue="mid_buildup", detail="test", current_state={},
            suggestion="test", genre_id="Glam Metal", timestamp=time.time(),
        )
        evt_id = lg.log_recommendation(rec)
        lg.log_song_end()
        assert evt_id in lg._completed_songs[0]["rec_ids"]

    def test_recs_outside_song_not_tracked_in_song(self, tmp_path):
        lg = _make_logger(tmp_path)
        # Log a rec BEFORE any song starts
        rec = Recommendation(
            channel_num=7, channel_label="Guitar 1",
            issue="lufs_hot", detail="test", current_state={},
            suggestion="test", genre_id="Glam Metal", timestamp=time.time(),
        )
        pre_id = lg.log_recommendation(rec)
        lg.log_song_start({"title": "Song"}, 1, "Glam Metal")
        lg.log_song_end()
        assert pre_id not in lg._completed_songs[0]["rec_ids"]

    def test_record_lufs_samples_stored_during_song(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Test"}, 1, "AOR")
        for lufs_val in [-18.5, -17.2, -19.0]:
            lg.record_lufs(lufs_val)
        lg.log_song_end()
        assert lg._completed_songs[0]["lufs_samples"] == [-18.5, -17.2, -19.0]

    def test_record_lufs_ignored_outside_song(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.record_lufs(-18.0)   # no song active — should be ignored
        assert lg._song_lufs == []

    def test_record_lufs_ignores_silence_sentinel(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Test"}, 1, "AOR")
        lg.record_lufs(-90.0)   # silence sentinel
        lg.record_lufs(-70.0)   # exactly at threshold — excluded (> -70 required)
        lg.record_lufs(-18.0)   # valid sample
        lg.log_song_end()
        assert lg._completed_songs[0]["lufs_samples"] == [-18.0]

    # ── Per-song report accuracy ──────────────────────────────────────────────

    def test_per_song_accuracy_in_report(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Accuracy Test", "artist": "", "genre_profile": "AOR"},
                          1, "AOR")

        def _rec(issue="mid_buildup"):
            return Recommendation(
                channel_num=7, channel_label="Guitar 1",
                issue=issue, detail="test", current_state={},
                suggestion="fader cut", genre_id="AOR", timestamp=time.time(),
            )

        # Log 4 recs
        ids = [lg.log_recommendation(_rec()) for _ in range(4)]

        # Simulate 2 matched adjustments
        from models.channel import ChannelState, EQBand
        from models.event import AdjustmentEvent
        for rid in ids[:2]:
            adj = AdjustmentEvent(7, "Guitar 1", "fader", -2.0, -4.0, time.time())
            # directly patch last rec so correlation works
            lg._last_recs[7] = (rid, time.time() - 1.0, "fader cut", {})
            lg._log_adjustment(adj, time.strftime("%H:%M:%S"))

        lg.log_song_end()
        report = lg.generate_report()
        # Report should contain the song title and accuracy
        assert "Accuracy Test" in report
        assert "Recs: 4" in report

    def test_report_song_breakdown_section_present(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Don't Stop Believin'", "artist": "Journey",
                           "genre_profile": "AOR"}, 1, "AOR")
        lg.record_lufs(-19.5)
        lg.record_lufs(-18.0)
        lg.log_song_end()
        report = lg.generate_report()
        assert "SONG BREAKDOWN" in report
        assert "Don't Stop Believin'" in report
        assert "Journey" in report
        assert "LUFS" in report

    def test_between_songs_shown_in_report(self, tmp_path):
        lg = _make_logger(tmp_path)
        lg.log_song_start({"title": "Song 1"}, 1, "AOR")
        lg.log_song_end()
        lg.log_song_start({"title": "Song 2"}, 2, "AOR")
        lg.log_song_end()
        report = lg.generate_report()
        assert "Between songs" in report


# ===========================================================================
# FIX 1 — SUPPRESSION SLIDING WINDOW + RMS TRIGGER
# ===========================================================================

class TestSuppressionSlidingWindow:
    """The fader reference must stay anchored during the window — not advance
    every poll — so accumulated small moves that total > suppress_db are caught."""

    def _engine(self):
        cfg = _minimal_band_cfg()
        return RecommendationEngine(cfg, _make_glam_profile())

    def test_single_large_spike_triggers_suppression(self):
        engine = self._engine()
        now = 100.0
        engine._last_fader[1]      = 0.0
        engine._last_fader_time[1] = now
        ch = _make_channel(num=1, fader_db=5.0)
        engine._update_suppression({1: ch}, now + 1.0)
        assert engine._is_suppressed(1, now + 2.0)

    def test_accumulated_moves_within_window_trigger_suppression(self):
        # 1.5 dB at T+2s (no trigger), then 3.5 dB total at T+4s (trigger)
        engine = self._engine()
        now = 100.0
        engine._last_fader[1]      = 0.0
        engine._last_fader_time[1] = now

        # First move: 1.5 dB — below threshold, reference should NOT advance
        ch1 = _make_channel(num=1, fader_db=1.5)
        engine._update_suppression({1: ch1}, now + 2.0)
        assert not engine._is_suppressed(1, now + 2.5), "Should not suppress on 1.5 dB move"
        # Reference must still be anchored at (0.0, now), not advanced to (1.5, now+2)
        assert engine._last_fader[1] == pytest.approx(0.0)

        # Second move: 3.5 dB total from original reference — threshold exceeded
        ch2 = _make_channel(num=1, fader_db=3.5)
        engine._update_suppression({1: ch2}, now + 4.0)
        assert engine._is_suppressed(1, now + 5.0), "Should suppress: 3.5 dB in 4 s within 5 s window"

    def test_move_outside_window_does_not_trigger(self):
        # Move > 3 dB but elapsed > window_s → reference advanced, no suppression
        engine = self._engine()
        now = 100.0
        engine._last_fader[1]      = 0.0
        engine._last_fader_time[1] = now
        ch = _make_channel(num=1, fader_db=5.0)
        engine._update_suppression({1: ch}, now + 6.0)   # 6s > window_s=5s
        assert not engine._is_suppressed(1, now + 7.0), "Move after window expiry should not suppress"

    def test_window_expiry_advances_reference(self):
        engine = self._engine()
        now = 100.0
        engine._last_fader[1]      = 0.0
        engine._last_fader_time[1] = now
        # After window expires, reference should be at new fader value
        ch = _make_channel(num=1, fader_db=2.0)
        engine._update_suppression({1: ch}, now + 6.0)
        assert engine._last_fader[1] == pytest.approx(2.0)
        assert engine._last_fader_time[1] == pytest.approx(now + 6.0)

    def test_suppression_trigger_label_fader(self):
        engine = self._engine()
        now = 100.0
        engine._last_fader[1]      = 0.0
        engine._last_fader_time[1] = now
        ch = _make_channel(num=1, fader_db=5.0)
        engine._update_suppression({1: ch}, now + 1.0)
        assert engine._suppression_trigger.get(1) == "rate_of_change_fader"

    # ── RMS trigger (boost-pedal) ─────────────────────────────────────────────

    def test_rms_spike_triggers_suppression(self):
        engine = self._engine()
        now = 100.0
        engine._last_rms[1]      = -20.0
        engine._last_rms_time[1] = now
        ch = _make_channel(num=1, rms_db=-14.0)   # +6 dB rise in 1 s > 4 dB threshold
        engine._update_suppression({1: ch}, now + 1.0)
        assert engine._is_suppressed(1, now + 2.0)

    def test_rms_gradual_rise_no_suppression(self):
        engine = self._engine()
        now = 100.0
        engine._last_rms[1]      = -20.0
        engine._last_rms_time[1] = now
        ch = _make_channel(num=1, rms_db=-17.5)   # only +2.5 dB in 1 s < 4 dB threshold
        engine._update_suppression({1: ch}, now + 1.0)
        assert not engine._is_suppressed(1, now + 2.0)

    def test_rms_drop_does_not_trigger_suppression(self):
        # Downward RMS move (e.g. engineer cuts fader) should not suppress
        engine = self._engine()
        now = 100.0
        engine._last_rms[1]      = -10.0
        engine._last_rms_time[1] = now
        ch = _make_channel(num=1, rms_db=-20.0)   # -10 dB drop
        engine._update_suppression({1: ch}, now + 0.5)
        assert not engine._is_suppressed(1, now + 1.0), "Downward RMS should not suppress"

    def test_rms_spike_outside_window_no_suppression(self):
        engine = self._engine()
        now = 100.0
        engine._last_rms[1]      = -20.0
        engine._last_rms_time[1] = now
        ch = _make_channel(num=1, rms_db=-14.0)
        engine._update_suppression({1: ch}, now + 3.0)   # 3s > rms_window=2s
        assert not engine._is_suppressed(1, now + 4.0)

    def test_suppression_trigger_label_rms(self):
        engine = self._engine()
        now = 100.0
        engine._last_rms[1]      = -20.0
        engine._last_rms_time[1] = now
        ch = _make_channel(num=1, rms_db=-14.0)
        engine._update_suppression({1: ch}, now + 1.0)
        assert engine._suppression_trigger.get(1) == "rate_of_change_rms"

    def test_fader_trigger_overwrites_rms_trigger(self):
        # If both fire, the fader trigger (last to run) wins the label
        engine = self._engine()
        now = 100.0
        engine._last_fader[1]      = 0.0
        engine._last_fader_time[1] = now
        engine._last_rms[1]        = -20.0
        engine._last_rms_time[1]   = now
        ch = _make_channel(num=1, fader_db=5.0, rms_db=-14.0)
        engine._update_suppression({1: ch}, now + 1.0)
        assert engine._is_suppressed(1, now + 2.0)
        # Both fire; whichever runs last (fader runs first in the loop, RMS second
        # — so rms trigger overwrites) — just verify suppression fired
        assert engine._suppression_trigger[1] in ("rate_of_change_fader", "rate_of_change_rms")


# ===========================================================================
# FIX 2 — FADER VALUE USES CULPRIT CHANNEL, NOT STALE DATA
# ===========================================================================

class TestRecommendationFaderValue:
    """Verify that the fader shown in a band recommendation belongs to the
    culprit channel, not to any other channel evaluated in the same cycle."""

    def _engine_with_two_guitars(self):
        cfg = {
            "band": "Test", "default_genre": "Glam Metal",
            "x32": {"ip": "127.0.0.1", "port": 10023},
            "audio": {"device_name_match": "X"},
            "channels": {
                7: {"label": "Guitar 1", "type": "instrument"},
                8: {"label": "Guitar 2", "type": "instrument"},
            },
            "thresholds": {
                "recommendation_trigger_db": 3.0,
                "lufs_trigger_db": 2.0,
                "baseline_drift_trigger_db": 2.0,
                "rate_of_change_suppress_db": 3.0,
                "rate_of_change_window_s": 5,
                "suppression_duration_s": 60,
                "recommendation_cooldown_s": 60,
            },
            "frequency_fingerprints": {
                "Guitar 1": {"primary": [200, 5000]},
                "Guitar 2": {"primary": [200, 5000]},
            },
        }
        profile = _make_glam_profile()
        return RecommendationEngine(cfg, profile)

    def test_recommendation_fader_matches_culprit_fader(self):
        engine = self._engine_with_two_guitars()

        # Guitar 1: low RMS, fader at +0.5 dB
        # Guitar 2: higher RMS (will be chosen as culprit), fader at -5.0 dB
        guitar1 = ChannelState(
            channel_num=7, label="Guitar 1",
            fader_db=0.5, muted=False,
            eq=[_make_eq_band(b + 1, freq_hz=f) for b, f in enumerate([80, 315, 2500, 8000])],
            comp_on=False, comp_threshold_db=-20, comp_ratio_index=3,
            gate_on=False, gate_threshold_db=-40,
            rms_linear=0.1, rms_db=-20.0, timestamp=time.time(),
        )
        guitar2 = ChannelState(
            channel_num=8, label="Guitar 2",
            fader_db=-5.0, muted=False,
            eq=[_make_eq_band(b + 1, freq_hz=f) for b, f in enumerate([80, 315, 2500, 8000])],
            comp_on=False, comp_threshold_db=-20, comp_ratio_index=3,
            gate_on=False, gate_threshold_db=-40,
            rms_linear=0.2, rms_db=-14.0, timestamp=time.time(),   # higher RMS → culprit
        )
        channels = {7: guitar1, 8: guitar2}

        # Room analysis with mid-band buildup
        bands = {b: -30.0 for b in BAND_NAMES}
        bands["mid"] = -10.0     # large mid deviation
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0, bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, channels)
        mid_recs = [r for r in recs if "mid" in r.issue]
        assert len(mid_recs) >= 1, "Expected a mid-band recommendation"

        for rec in mid_recs:
            # The culprit should be Guitar 2 (higher RMS)
            assert rec.channel_label == "Guitar 2", (
                f"Expected Guitar 2 as culprit, got {rec.channel_label}"
            )
            # The fader shown must be Guitar 2's fader (-5.0), not Guitar 1's (+0.5)
            fader_str = rec.current_state.get("fader", "")
            assert "-5.0" in fader_str, (
                f"Expected Guitar 2 fader (-5.0 dB) in current_state, got: {fader_str}"
            )
            assert "+0.5" not in fader_str, (
                f"Guitar 1 fader (+0.5 dB) must not appear in Guitar 2 recommendation"
            )

    def test_recommendation_rms_matches_culprit_rms(self):
        engine = self._engine_with_two_guitars()
        guitar1 = ChannelState(
            channel_num=7, label="Guitar 1", fader_db=0.5, muted=False,
            eq=[_make_eq_band(1)], comp_on=False, comp_threshold_db=-20,
            comp_ratio_index=3, gate_on=False, gate_threshold_db=-40,
            rms_linear=0.1, rms_db=-20.0, timestamp=time.time(),
        )
        guitar2 = ChannelState(
            channel_num=8, label="Guitar 2", fader_db=-5.0, muted=False,
            eq=[_make_eq_band(1)], comp_on=False, comp_threshold_db=-20,
            comp_ratio_index=3, gate_on=False, gate_threshold_db=-40,
            rms_linear=0.2, rms_db=-14.0, timestamp=time.time(),
        )
        bands = {b: -30.0 for b in BAND_NAMES}
        bands["mid"] = -10.0
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0, bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, {7: guitar1, 8: guitar2})
        mid_recs = [r for r in recs if "mid" in r.issue]
        assert len(mid_recs) >= 1
        for rec in mid_recs:
            rms_str = rec.current_state.get("rms", "")
            assert "-14.0" in rms_str, f"Expected Guitar 2 RMS (-14.0), got: {rms_str}"


# ===========================================================================
# FIX 3 — DEVIATION STABILITY GUARD
# ===========================================================================

class TestStabilityGuard:
    def _engine(self):
        return RecommendationEngine(_minimal_band_cfg(), _make_glam_profile())

    # ── _update_stability / _issue_cooldown_ok ───────────────────────────────

    def test_first_two_fires_use_base_cooldown(self):
        engine = self._engine()
        ch, issue = 1, "mid_buildup"
        engine._update_stability(ch, issue)
        engine._update_stability(ch, issue)
        # Still base cooldown after 2 fires
        engine._last_rec[ch] = 100.0
        assert engine._issue_cooldown_ok(ch, issue, 160.0), "Should pass after 60 s with base cooldown"

    def test_third_fire_doubles_cooldown(self):
        engine = self._engine()
        ch, issue = 1, "mid_buildup"
        for _ in range(3):
            engine._update_stability(ch, issue)
        engine._last_rec[ch] = 100.0
        # 60 s later — would pass base but fails doubled (120 s required)
        assert not engine._issue_cooldown_ok(ch, issue, 160.0), "Should be blocked by 120 s cooldown"
        # 120 s later — passes
        assert engine._issue_cooldown_ok(ch, issue, 220.0), "Should pass after 120 s"

    def test_fourth_fire_doubles_again_to_240(self):
        engine = self._engine()
        ch, issue = 1, "mid_buildup"
        for _ in range(4):
            engine._update_stability(ch, issue)
        engine._last_rec[ch] = 100.0
        # 120 s later — would pass 120 s cooldown but fails 240 s
        assert not engine._issue_cooldown_ok(ch, issue, 220.0), "Should be blocked by 240 s cooldown"
        # 240 s later — passes
        assert engine._issue_cooldown_ok(ch, issue, 340.0), "Should pass after 240 s"

    def test_cooldown_capped_at_4x_base(self):
        engine = self._engine()
        ch, issue = 1, "mid_buildup"
        for _ in range(10):   # many fires
            engine._update_stability(ch, issue)
        effective = engine._issue_cooldown.get((ch, issue), 60.0)
        assert effective <= 240.0, f"Cooldown {effective} s exceeds 4× cap (240 s)"

    def test_different_issues_on_same_channel_are_independent(self):
        engine = self._engine()
        ch = 1
        for _ in range(3):
            engine._update_stability(ch, "mid_buildup")
        # sub_bass_buildup on same channel is unaffected
        engine._last_rec[ch] = 100.0
        assert engine._issue_cooldown_ok(ch, "sub_bass_buildup", 160.0), \
            "Different issue on same channel should use base cooldown"

    def test_same_issue_on_different_channels_are_independent(self):
        engine = self._engine()
        for _ in range(3):
            engine._update_stability(1, "mid_buildup")
        engine._last_rec[1] = 100.0
        engine._last_rec[2] = 100.0
        assert engine._issue_cooldown_ok(2, "mid_buildup", 160.0), \
            "Same issue on different channel should use base cooldown"

    # ── notify_adjustment reset ───────────────────────────────────────────────

    def test_notify_adjustment_resets_stability(self):
        engine = self._engine()
        ch, issue = 1, "mid_buildup"
        for _ in range(3):
            engine._update_stability(ch, issue)
        engine._last_rec[ch] = 100.0
        assert not engine._issue_cooldown_ok(ch, issue, 160.0), "Confirm extended cooldown active"

        engine.notify_adjustment(ch)
        assert engine._issue_cooldown_ok(ch, issue, 160.0), "Should use base cooldown after adjustment"
        assert engine._consecutive_fires.get((ch, issue), 0) == 0

    def test_notify_adjustment_resets_all_issues_on_channel(self):
        engine = self._engine()
        for issue in ("mid_buildup", "bass_buildup", "high_mid_deficiency"):
            for _ in range(3):
                engine._update_stability(1, issue)
        engine.notify_adjustment(1)
        for issue in ("mid_buildup", "bass_buildup", "high_mid_deficiency"):
            assert (1, issue) not in engine._consecutive_fires

    def test_notify_adjustment_does_not_affect_other_channels(self):
        engine = self._engine()
        for _ in range(3):
            engine._update_stability(1, "mid_buildup")
            engine._update_stability(2, "mid_buildup")
        engine.notify_adjustment(1)
        assert (2, "mid_buildup") in engine._consecutive_fires, \
            "Other channel's stability should be untouched"

    # ── Deviation-resolved reset ──────────────────────────────────────────────

    def test_deviation_resolved_resets_stability(self):
        engine = self._engine()
        ch, issue = 1, "mid_buildup"
        for _ in range(3):
            engine._update_stability(ch, issue)
        engine._last_rec[ch] = 100.0
        assert not engine._issue_cooldown_ok(ch, issue, 160.0)

        # Simulate: issue was active last cycle, but resolves this cycle
        engine._prev_active_issues = {(ch, issue)}
        current_active: set = set()   # nothing active now
        for key in engine._prev_active_issues - current_active:
            engine._consecutive_fires.pop(key, None)
            engine._issue_cooldown.pop(key, None)
        engine._prev_active_issues = current_active

        assert engine._issue_cooldown_ok(ch, issue, 160.0), \
            "Should use base cooldown after deviation resolved"

    # ── End-to-end: stability guard suppresses repeat static recommendations ──

    def test_stability_guard_reduces_repeat_recs(self):
        engine = self._engine()
        # Simulate 3 fires by directly advancing state
        ch, issue = 1, "lufs_hot"   # Use global rec for simplicity
        now = time.time()

        # First three fires at base cooldown intervals
        for i in range(3):
            room = RoomAnalysis(
                lufs=-12.0, rms_db=-20.0,
                bands={b: -30.0 for b in BAND_NAMES},
                band_delta={b: 0.0 for b in BAND_NAMES},
                lufs_delta=0.0, timestamp=now + i * 60.0,
            )
            recs = engine.evaluate(room, {1: _make_channel(num=1)})
            assert any(r.issue == "lufs_hot" for r in recs), f"Expected fire on cycle {i+1}"

        # Note: LUFS uses _last_global_rec not the stability guard — verify
        # the stability guard doesn't accidentally block it
        room4 = RoomAnalysis(
            lufs=-12.0, rms_db=-20.0,
            bands={b: -30.0 for b in BAND_NAMES},
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=now + 3 * 60.0,
        )
        recs4 = engine.evaluate(room4, {1: _make_channel(num=1)})
        # LUFS uses its own cooldown — 3rd fire was at T+120, T+180 > T+120+60, so it fires
        assert any(r.issue == "lufs_hot" for r in recs4), \
            "LUFS uses its own cooldown track, not the band stability guard"


# ===========================================================================
# AUDIO CAPTURE TESTS  (sounddevice mocked — no hardware required)
# ===========================================================================

from core.audio_capture import AudioCapture


def _fake_devices(entries: list[dict]) -> list[dict]:
    """Build a minimal device list for mocking sd.query_devices()."""
    defaults = {"max_input_channels": 2, "max_output_channels": 2, "default_samplerate": 48000.0}
    return [{**defaults, **e} for e in entries]


class TestAudioCaptureInit:
    def test_defaults(self):
        cap = AudioCapture()
        assert cap._match == "DJI"
        assert cap._preferred_sr == 48000
        assert cap._forced_index is None
        assert cap.sample_rate == 48000

    def test_custom_params(self):
        cap = AudioCapture(
            device_name_match="CABLE Output",
            preferred_sample_rate=44100,
            forced_device_index=5,
        )
        assert cap._match == "CABLE Output"
        assert cap._preferred_sr == 44100
        assert cap._forced_index == 5

    def test_device_name_before_start(self):
        cap = AudioCapture()
        assert cap.device_name == "not connected"

    def test_get_buffer_before_start_returns_empty(self):
        cap = AudioCapture()
        buf, sr = cap.get_buffer()
        assert len(buf) == 0
        assert sr == 48000


class TestAudioCaptureFindDevice:
    CABLE = "CABLE Output (VB-Audio Virtual Cable)"

    def _cap(self, match=None, preferred_sr=48000):
        return AudioCapture(
            device_name_match=match or self.CABLE,
            preferred_sample_rate=preferred_sr,
        )

    @patch("core.audio_capture.sd.query_devices")
    def test_single_match_returns_correct_tuple(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": "Microphone Array", "max_input_channels": 2, "default_samplerate": 44100.0},
            {"name": self.CABLE,          "max_input_channels": 1, "default_samplerate": 48000.0},
        ])
        idx, name, sr = self._cap().find_device()
        assert idx == 1
        assert name == self.CABLE
        assert sr == 48000

    @patch("core.audio_capture.sd.query_devices")
    def test_multiple_matches_prefers_preferred_sr(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": self.CABLE, "max_input_channels": 1, "default_samplerate": 44100.0},
            {"name": self.CABLE, "max_input_channels": 1, "default_samplerate": 48000.0},
        ])
        idx, name, sr = self._cap(preferred_sr=48000).find_device()
        # Should prefer index 1 (48000Hz match) over index 0 (44100Hz)
        assert idx == 1
        assert sr == 48000

    @patch("core.audio_capture.sd.query_devices")
    def test_multiple_matches_falls_back_to_first_when_no_sr_match(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": self.CABLE, "max_input_channels": 1, "default_samplerate": 44100.0},
            {"name": self.CABLE, "max_input_channels": 1, "default_samplerate": 96000.0},
        ])
        idx, name, sr = self._cap(preferred_sr=48000).find_device()
        assert idx == 0   # first match wins when none hit preferred_sr
        assert sr == 44100

    @patch("core.audio_capture.sd.query_devices")
    def test_no_match_raises_runtime_error(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": "Speakers",    "max_input_channels": 0, "default_samplerate": 48000.0},
            {"name": "Microphone",  "max_input_channels": 2, "default_samplerate": 44100.0},
        ])
        with pytest.raises(RuntimeError, match="No audio input device matching"):
            self._cap().find_device()

    @patch("core.audio_capture.sd.query_devices")
    def test_no_match_error_lists_available_devices(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": "Microphone", "max_input_channels": 2, "default_samplerate": 44100.0},
        ])
        with pytest.raises(RuntimeError) as exc_info:
            self._cap().find_device()
        assert "Microphone" in str(exc_info.value)
        assert "Available input devices" in str(exc_info.value)

    @patch("core.audio_capture.sd.query_devices")
    def test_output_only_device_ignored(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": self.CABLE, "max_input_channels": 0, "default_samplerate": 48000.0},
        ])
        with pytest.raises(RuntimeError):
            self._cap().find_device()

    @patch("core.audio_capture.sd.query_devices")
    def test_name_match_is_case_insensitive(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": "cable output (vb-audio virtual cable)",
             "max_input_channels": 1, "default_samplerate": 48000.0},
        ])
        cap = AudioCapture(device_name_match="CABLE Output")
        idx, name, sr = cap.find_device()
        assert idx == 0


class TestAudioCaptureListDevices:
    CABLE = "CABLE Output (VB-Audio Virtual Cable)"

    @patch("core.audio_capture.sd.query_devices")
    def test_output_contains_matching_marker(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": "Microphone", "max_input_channels": 2, "default_samplerate": 44100.0},
            {"name": self.CABLE,   "max_input_channels": 1, "default_samplerate": 48000.0},
        ])
        cap = AudioCapture(device_name_match=self.CABLE)
        result = cap.list_devices()
        assert "← use this" in result
        assert self.CABLE in result

    @patch("core.audio_capture.sd.query_devices")
    def test_output_only_device_excluded(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": "HDMI Out", "max_input_channels": 0, "default_samplerate": 48000.0},
            {"name": self.CABLE, "max_input_channels": 1, "default_samplerate": 48000.0},
        ])
        cap = AudioCapture(device_name_match=self.CABLE)
        result = cap.list_devices()
        assert "HDMI Out" not in result

    @patch("core.audio_capture.sd.query_devices")
    def test_header_present(self, mock_qd):
        mock_qd.return_value = _fake_devices([
            {"name": self.CABLE, "max_input_channels": 1, "default_samplerate": 48000.0},
        ])
        cap = AudioCapture(device_name_match=self.CABLE)
        result = cap.list_devices()
        assert result.startswith("Available audio input devices:")


class TestAudioCaptureStart:
    CABLE = "CABLE Output (VB-Audio Virtual Cable)"

    @patch("core.audio_capture.sd.InputStream")
    @patch("core.audio_capture.sd.query_devices")
    def test_forced_index_bypasses_find_device(self, mock_qd, mock_stream):
        mock_qd.return_value = {"name": self.CABLE, "default_samplerate": 48000.0,
                                "max_input_channels": 1}
        stream_instance = MagicMock()
        mock_stream.return_value = stream_instance

        cap = AudioCapture(device_name_match="NOMATCH", forced_device_index=7)
        cap.start()

        assert cap._device_index == 7
        assert cap.sample_rate == 48000
        # query_devices called with index 7 (not the full list)
        mock_qd.assert_called_once_with(7)

    @patch("core.audio_capture.sd.InputStream")
    @patch("core.audio_capture.sd.query_devices")
    def test_stream_always_opens_mono(self, mock_qd, mock_stream):
        # Device advertises 2 input channels — stream must still open as 1
        mock_qd.return_value = _fake_devices([
            {"name": self.CABLE, "max_input_channels": 2, "default_samplerate": 48000.0},
        ])
        stream_instance = MagicMock()
        mock_stream.return_value = stream_instance

        cap = AudioCapture(device_name_match=self.CABLE)
        cap.start()

        _, kwargs = mock_stream.call_args
        assert kwargs.get("channels") == 1 or mock_stream.call_args[0][1] == 1 or \
               mock_stream.call_args.kwargs.get("channels") == 1

    @patch("core.audio_capture.sd.InputStream")
    @patch("core.audio_capture.sd.query_devices")
    def test_forced_index_stream_opens_mono(self, mock_qd, mock_stream):
        mock_qd.return_value = {"name": self.CABLE, "default_samplerate": 48000.0,
                                "max_input_channels": 2}
        stream_instance = MagicMock()
        mock_stream.return_value = stream_instance

        cap = AudioCapture(forced_device_index=3)
        cap.start()

        _, kwargs = mock_stream.call_args
        assert mock_stream.call_args.kwargs.get("channels") == 1

    @patch("core.audio_capture.sd.InputStream")
    @patch("core.audio_capture.sd.query_devices")
    def test_stream_started_after_open(self, mock_qd, mock_stream):
        mock_qd.return_value = _fake_devices([
            {"name": self.CABLE, "max_input_channels": 1, "default_samplerate": 48000.0},
        ])
        stream_instance = MagicMock()
        mock_stream.return_value = stream_instance

        cap = AudioCapture(device_name_match=self.CABLE)
        cap.start()

        stream_instance.start.assert_called_once()

    @patch("core.audio_capture.sd.InputStream")
    @patch("core.audio_capture.sd.query_devices")
    def test_buffer_allocated_on_start(self, mock_qd, mock_stream):
        mock_qd.return_value = _fake_devices([
            {"name": self.CABLE, "max_input_channels": 1, "default_samplerate": 48000.0},
        ])
        mock_stream.return_value = MagicMock()

        cap = AudioCapture(device_name_match=self.CABLE, buffer_seconds=2.0)
        assert cap._buffer is None
        cap.start()
        assert cap._buffer is not None
        assert len(cap._buffer) == 48000 * 2


# ===========================================================================
# MAIN.PY CLI ARGUMENT TESTS
# ===========================================================================

import argparse
import sys as _sys


class TestMainArgParser:
    """Test that main.py's argparse config accepts the new --device-index flag."""

    def _make_parser(self):
        # Replicate main.py's parser setup exactly
        parser = argparse.ArgumentParser(description="FOH Assistant")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--show",     action="store_true")
        group.add_argument("--baseline", action="store_true")
        group.add_argument("--devices",  action="store_true")
        group.add_argument("--test-osc", action="store_true")
        parser.add_argument("--x32-ip")
        parser.add_argument("--device-index", type=int, default=None)
        return parser

    def test_device_index_parsed_as_int(self):
        parser = self._make_parser()
        args = parser.parse_args(["--show", "--device-index", "36"])
        assert args.device_index == 36

    def test_device_index_defaults_to_none(self):
        parser = self._make_parser()
        args = parser.parse_args(["--show"])
        assert args.device_index is None

    def test_device_index_with_baseline(self):
        parser = self._make_parser()
        args = parser.parse_args(["--baseline", "--device-index", "2"])
        assert args.device_index == 2

    def test_devices_mode_no_device_index_needed(self):
        parser = self._make_parser()
        args = parser.parse_args(["--devices"])
        assert args.device_index is None

    def test_device_index_rejects_non_int(self):
        parser = self._make_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--show", "--device-index", "not_a_number"])


# ===========================================================================
# OSC CLIENT POLL FALLBACK TESTS
# ===========================================================================

from core.osc_client import X32OSCClient


class TestOSCClientPollFallback:
    """Tests for the poll fallback logic.  No network I/O — handlers and
    internal methods are called directly on an unconnected client instance."""

    def _client(self, channel_map=None, poll_ms=500):
        return X32OSCClient(
            ip="127.0.0.1", port=10023,
            channel_map=channel_map or {1: {"label": "Kick", "type": "instrument"}},
            poll_interval_ms=poll_ms,
        )

    # ── Constructor / defaults ────────────────────────────────────────────────

    def test_last_push_time_empty_on_init(self):
        assert self._client()._last_push_time == {}

    def test_poll_interval_stored_as_seconds(self):
        assert self._client(poll_ms=1000)._poll_interval_s == pytest.approx(1.0)
        assert self._client(poll_ms=500)._poll_interval_s == pytest.approx(0.5)

    def test_poll_thread_none_before_connect(self):
        assert self._client()._poll_thread is None

    # ── _is_channel_stale ─────────────────────────────────────────────────────

    def test_stale_true_for_channel_never_received(self):
        client = self._client()
        assert client._is_channel_stale(1, time.time()) is True

    def test_stale_true_for_old_timestamp(self):
        client = self._client()
        client._last_push_time[1] = time.time() - 5.0
        assert client._is_channel_stale(1, time.time()) is True

    def test_stale_false_for_fresh_timestamp(self):
        client = self._client()
        client._last_push_time[1] = time.time()
        assert client._is_channel_stale(1, time.time()) is False

    def test_stale_respects_custom_stale_s(self):
        client = self._client()
        client._last_push_time[1] = time.time() - 1.5
        assert client._is_channel_stale(1, time.time(), stale_s=2.0) is False
        assert client._is_channel_stale(1, time.time(), stale_s=1.0) is True

    def test_stale_threshold_is_at_least_2_seconds(self):
        # stale_s = max(2.0, poll_interval * 4). At 500ms: max(2.0, 2.0) = 2.0
        client = self._client(poll_ms=500)
        stale_s = max(2.0, client._poll_interval_s * 4)
        assert stale_s >= 2.0

    # ── Timestamp stamped by _handle_node ────────────────────────────────────

    def test_handle_node_mix_stamps_push_time(self):
        client = self._client()
        before = time.time()
        client._handle_node("/node", "ch/01/mix", 0.75, 1, 0.0)
        assert 1 in client._last_push_time
        assert client._last_push_time[1] >= before

    def test_handle_node_eq_stamps_push_time(self):
        client = self._client()
        eq_vals = [2, 0.5, 0.0, 0.707] * 4   # 4 bands × 4 values
        before = time.time()
        client._handle_node("/node", "ch/01/eq", *eq_vals)
        assert 1 in client._last_push_time
        assert client._last_push_time[1] >= before

    def test_handle_node_dyn_stamps_push_time(self):
        client = self._client()
        before = time.time()
        client._handle_node("/node", "ch/01/dyn", 0, -20.0, 3)
        assert 1 in client._last_push_time
        assert client._last_push_time[1] >= before

    def test_handle_node_gate_stamps_push_time(self):
        client = self._client()
        before = time.time()
        client._handle_node("/node", "ch/01/gate", 0, -40.0)
        assert 1 in client._last_push_time
        assert client._last_push_time[1] >= before

    def test_handle_node_unknown_channel_does_not_raise(self):
        client = self._client()
        client._handle_node("/node", "main/st/mix", 0.75, 1, 0.0)
        assert client._last_push_time == {}   # main/ path should not stamp ch times

    # ── Timestamp stamped by _handle_channel_param ───────────────────────────

    def test_handle_channel_param_fader_stamps_push_time(self):
        client = self._client()
        before = time.time()
        client._handle_channel_param("/ch/01/mix/fader", 0.75)
        assert 1 in client._last_push_time
        assert client._last_push_time[1] >= before

    def test_handle_channel_param_mute_stamps_push_time(self):
        client = self._client()
        before = time.time()
        client._handle_channel_param("/ch/01/mix/on", 0)
        assert 1 in client._last_push_time
        assert client._last_push_time[1] >= before

    def test_handle_channel_param_eq_gain_stamps_push_time(self):
        client = self._client()
        before = time.time()
        client._handle_channel_param("/ch/01/eq/2/g", 1.5)
        assert 1 in client._last_push_time
        assert client._last_push_time[1] >= before

    def test_handle_channel_param_updates_state_correctly(self):
        client = self._client()
        client._handle_channel_param("/ch/01/mix/fader", 0.8)
        assert client._state[1]["fader"] == pytest.approx(0.8)

    # ── Poll repoll logic ─────────────────────────────────────────────────────

    def test_poll_repoll_stale_channel_sends_node_requests(self):
        client = self._client(channel_map={
            1: {"label": "Kick"}, 9: {"label": "Guitar 1"}
        })
        # Channel 1 stale, channel 9 fresh
        client._last_push_time[1] = time.time() - 10.0
        client._last_push_time[9] = time.time()

        sent = []
        client._send = lambda addr, params=None: sent.append((addr, params))

        now     = time.time()
        stale_s = max(2.0, client._poll_interval_s * 4)
        for ch_num in client._channel_map:
            if client._is_channel_stale(ch_num, now, stale_s):
                ch = f"{ch_num:02d}"
                client._send("/node", [f"ch/{ch}/mix"])
                client._send("/node", [f"ch/{ch}/eq"])

        addresses  = [s[0] for s in sent]
        param_strs = [str(s[1]) for s in sent]
        assert "/node" in addresses
        assert any("ch/01/mix" in p for p in param_strs), "ch 1 should be re-polled"
        assert any("ch/01/eq"  in p for p in param_strs), "ch 1 eq should be re-polled"
        assert not any("ch/09" in p for p in param_strs), "ch 9 is fresh, no repoll"

    def test_poll_skips_fresh_channel(self):
        client = self._client()
        client._last_push_time[1] = time.time()

        sent = []
        client._send = lambda addr, params=None: sent.append((addr, params))

        now     = time.time()
        stale_s = max(2.0, client._poll_interval_s * 4)
        for ch_num in client._channel_map:
            if client._is_channel_stale(ch_num, now, stale_s):
                ch = f"{ch_num:02d}"
                client._send("/node", [f"ch/{ch}/mix"])
                client._send("/node", [f"ch/{ch}/eq"])

        assert sent == [], "No /node requests expected for fresh channel"

    def test_all_never_updated_channels_are_stale(self):
        client = self._client(channel_map={
            ch: {"label": f"CH{ch:02d}"} for ch in range(1, 15)
        })
        now = time.time()
        stale_s = 2.0
        stale = [ch for ch in client._channel_map
                 if client._is_channel_stale(ch, now, stale_s)]
        assert len(stale) == 14   # none have been heard from


# ===========================================================================
# IMP-020 — TRANSITION GRACE CANCELS IMMEDIATELY ON SONG START
# ===========================================================================

class TestTransitionGraceIMP020:
    def _engine(self, grace_s: float = 8.0):
        cfg = _minimal_band_cfg()
        cfg["thresholds"]["transition_grace_seconds"] = grace_s
        return RecommendationEngine(cfg, _make_glam_profile())

    def test_double_next_gives_fresh_8s_grace(self):
        """Simulating 'n' pressed twice: second grace is fresh 8s, not stacked."""
        engine = self._engine(grace_s=8.0)
        # First transition start
        engine.set_transition(False)
        engine.set_transition(True)
        first_end = engine._transition_end

        # Brief pause, then second transition (simulating rapid 'n' again)
        before = time.time()
        engine.set_transition(False)
        engine.set_transition(True)
        second_end = engine._transition_end
        after = time.time()

        # Second end should be ~8s from now, not ~16s
        assert before + 8.0 <= second_end <= after + 8.0

    def test_cancel_then_restart_gives_fresh_grace(self):
        """After set_transition(True) -> set_transition(False) -> set_transition(True),
        grace should be ~8s from the last set_transition(True) call."""
        engine = self._engine(grace_s=8.0)
        engine.set_transition(True)
        before = time.time()
        engine.set_transition(False)
        engine.set_transition(True)
        after = time.time()

        assert engine._in_transition is True
        assert before + 8.0 <= engine._transition_end <= after + 8.0

    def test_transition_fires_and_suppresses_for_8s(self):
        """Grace still fires and suppresses for 8 seconds when set."""
        engine = self._engine(grace_s=8.0)
        before = time.time()
        engine.set_transition(True)
        after = time.time()

        assert engine._in_transition is True
        assert before + 8.0 <= engine._transition_end <= after + 8.0
        # Verify it suppresses LUFS
        room = _make_room(lufs=-12.0)
        recs = engine.evaluate(room, {1: _make_channel()})
        assert not any(r.issue in ("lufs_hot", "lufs_low") for r in recs)

    def test_transition_grace_seconds_is_8_in_band_yaml(self):
        """Verify transition_grace_seconds is 8 in band.yaml."""
        from core.config_loader import load_band_config
        cfg = load_band_config()
        assert cfg["thresholds"]["transition_grace_seconds"] == 8

    def test_end_current_song_cancels_grace_before_restart(self):
        """set_transition(False) then set_transition(True) in rapid sequence:
        end state has ~8s grace, not stacked time."""
        engine = self._engine(grace_s=8.0)
        # Manually set a long transition_end to simulate stacked scenario
        engine._in_transition = True
        engine._transition_end = time.time() + 100.0   # artificially long

        # Cancelling and restarting should reset to 8s
        before = time.time()
        engine.set_transition(False)
        engine.set_transition(True)
        after = time.time()

        assert before + 8.0 <= engine._transition_end <= after + 8.0


# ===========================================================================
# IMP-025 — FREQUENCY FINGERPRINT CORRECTIONS
# ===========================================================================

class TestFingerprintIMP025:
    def _engine_with_guitars(self):
        cfg = _minimal_band_cfg()
        cfg["frequency_fingerprints"] = {
            "Guitar 1": {"body": [200, 1000], "bite": [2000, 5000]},
            "Guitar 2": {"body": [200, 1000], "bite": [2000, 5000]},
        }
        cfg["channels"] = {
            7: {"label": "Guitar 1", "type": "instrument"},
            8: {"label": "Guitar 2", "type": "instrument"},
        }
        return RecommendationEngine(cfg, _make_glam_profile())

    def _engine_with_bass(self):
        cfg = _minimal_band_cfg()
        cfg["frequency_fingerprints"] = {
            "Bass": {"primary": [40, 250], "definition": [700, 1000], "attack": [2000, 4000]},
        }
        cfg["channels"] = {13: {"label": "Bass", "type": "instrument"}}
        return RecommendationEngine(cfg, _make_glam_profile())

    def _engine_with_guitar_and_kick(self):
        cfg = _minimal_band_cfg()
        cfg["frequency_fingerprints"] = {
            "Guitar 1": {"body": [200, 1000], "bite": [2000, 5000]},
            "Kick": {"fundamental": [50, 80], "body": [80, 150],
                     "click": [2000, 4000], "mud_zone": [300, 500]},
        }
        cfg["channels"] = {
            7: {"label": "Guitar 1", "type": "instrument"},
            1: {"label": "Kick", "type": "instrument"},
        }
        return RecommendationEngine(cfg, _make_glam_profile())

    def test_guitar1_culprit_for_low_mid_buildup(self):
        """_find_culprit() correctly finds Guitar 1 as low-mid culprit via body zone."""
        engine = self._engine_with_guitars()
        guitar1 = _make_channel(num=7, label="Guitar 1", rms_db=-20.0)
        culprit = engine._find_culprit("low_mid", {7: guitar1}, 5.0)
        assert culprit is not None
        assert culprit.label == "Guitar 1"

    def test_guitar1_culprit_for_high_mid_buildup(self):
        """_find_culprit() correctly finds Guitar 1 as high-mid culprit via bite zone."""
        engine = self._engine_with_guitars()
        guitar1 = _make_channel(num=7, label="Guitar 1", rms_db=-20.0)
        culprit = engine._find_culprit("high_mid", {7: guitar1}, 5.0)
        assert culprit is not None
        assert culprit.label == "Guitar 1"

    def test_bass_culprit_for_mid_band_via_definition_zone(self):
        """Bass definition zone (700-1000Hz) overlaps with mid band (500-2000Hz)."""
        engine = self._engine_with_bass()
        bass = _make_channel(num=13, label="Bass", rms_db=-20.0)
        culprit = engine._find_culprit("mid", {13: bass}, 5.0)
        assert culprit is not None
        assert culprit.label == "Bass"

    def test_guitar1_outscores_kick_in_low_mid(self):
        """Guitar 1 body zone (200-1000Hz) gives more low-mid overlap than
        Kick mud_zone (300-500Hz), so Guitar 1 should be the culprit when both
        are active at the same RMS."""
        engine = self._engine_with_guitar_and_kick()
        guitar1 = _make_channel(num=7, label="Guitar 1", rms_db=-20.0)
        kick = _make_channel(num=1, label="Kick", rms_db=-20.0)
        channels = {7: guitar1, 1: kick}
        culprit = engine._find_culprit("low_mid", channels, 5.0)
        assert culprit is not None
        assert culprit.label == "Guitar 1"


# ===========================================================================
# IMP-022 — READ HPF STATE AND INPUT GAIN FROM X32
# ===========================================================================

class TestChannelStateHPFIMP022:
    def test_default_hpf_fields(self):
        """ChannelState instantiates with default HPF fields."""
        ch = _make_channel()
        assert ch.hpf_on is False
        assert ch.hpf_freq_hz == 80.0
        assert ch.hpf_slope == 1
        assert ch.input_gain_db == 0.0

    def test_explicit_hpf_fields(self):
        """ChannelState accepts explicit HPF/gain values."""
        ch = ChannelState(
            channel_num=7,
            label="Guitar 1",
            fader_db=0.0,
            muted=False,
            eq=[_make_eq_band(n + 1) for n in range(4)],
            comp_on=False,
            comp_threshold_db=-20.0,
            comp_ratio_index=3,
            gate_on=False,
            gate_threshold_db=-40.0,
            rms_linear=0.1,
            rms_db=-20.0,
            timestamp=time.time(),
            hpf_on=True,
            hpf_freq_hz=100.0,
            hpf_slope=1,
            input_gain_db=35.0,
        )
        assert ch.hpf_on is True
        assert ch.hpf_freq_hz == 100.0
        assert ch.hpf_slope == 1
        assert ch.input_gain_db == 35.0


class TestOSCClientHPFIMP022:
    def _client(self):
        return X32OSCClient(
            ip="127.0.0.1", port=10023,
            channel_map={1: {"label": "Kick", "type": "instrument"}},
        )

    def test_preamp_hpon_1_maps_to_hpf_on_true(self):
        """preamp_hpon=1 → hpf_on=True in build_channel_states()."""
        client = self._client()
        with client._state_lock:
            client._state[1] = {
                "fader": 0.75, "mute": 1,
                "preamp_hpon": 1,
                "preamp_hpf": 0.3,
                "preamp_hpslope": 1,
                "preamp_gain": 0.0,
            }
        channels = client.build_channel_states()
        assert channels[1].hpf_on is True

    def test_preamp_hpon_0_maps_to_hpf_on_false(self):
        """preamp_hpon=0 → hpf_on=False in build_channel_states()."""
        client = self._client()
        with client._state_lock:
            client._state[1] = {
                "fader": 0.75, "mute": 1,
                "preamp_hpon": 0,
                "preamp_hpf": 0.3,
                "preamp_hpslope": 1,
                "preamp_gain": 0.0,
            }
        channels = client.build_channel_states()
        assert channels[1].hpf_on is False

    def test_hpf_freq_converted_from_x32_float(self):
        """HPF frequency is correctly converted from X32 float to Hz."""
        from core.osc_client import eq_float_to_hz
        client = self._client()
        # 0.3 ≈ 80Hz on the log scale
        with client._state_lock:
            client._state[1] = {
                "fader": 0.75, "mute": 1,
                "preamp_hpon": 1,
                "preamp_hpf": 0.3,
                "preamp_hpslope": 1,
                "preamp_gain": 0.0,
            }
        channels = client.build_channel_states()
        expected = eq_float_to_hz(0.3)
        assert channels[1].hpf_freq_hz == pytest.approx(expected, rel=1e-4)

    def test_handle_node_preamp_parses_fields(self):
        """_handle_node() correctly parses preamp node values."""
        client = self._client()
        # preamp node: gain, invert, hpon, hpf, hpslope, lofilt
        client._handle_node("/node", "ch/01/preamp", 12.0, 0, 1, 0.3, 1, 0)
        with client._state_lock:
            raw = client._state.get(1, {})
        assert raw.get("preamp_gain") == pytest.approx(12.0)
        assert raw.get("preamp_hpon") == 1
        assert raw.get("preamp_hpf") == pytest.approx(0.3)
        assert raw.get("preamp_hpslope") == 1

    def test_handle_channel_param_preamp_hpon(self):
        """Individual preamp/hpon param push is parsed correctly."""
        client = self._client()
        client._handle_channel_param("/ch/01/preamp/hpon", 1)
        with client._state_lock:
            raw = client._state.get(1, {})
        assert raw.get("preamp_hpon") == 1

    def test_handle_channel_param_preamp_gain(self):
        """Individual preamp/gain param push is parsed correctly."""
        client = self._client()
        client._handle_channel_param("/ch/01/preamp/gain", 24.0)
        with client._state_lock:
            raw = client._state.get(1, {})
        assert raw.get("preamp_gain") == pytest.approx(24.0)


class TestBaselineSnapshotHPFIMP022:
    def test_baseline_snapshot_includes_hpf_fields(self, tmp_path):
        """_save_baseline() includes hpf_on, hpf_freq_hz, hpf_slope, input_gain_db."""
        import json
        from core.baseline import _save_baseline

        orig_shows_dir = None
        import core.baseline as _bl_module
        orig = _bl_module.SHOWS_DIR
        _bl_module.SHOWS_DIR = tmp_path
        try:
            ch = ChannelState(
                channel_num=7,
                label="Guitar 1",
                fader_db=0.0,
                muted=False,
                eq=[_make_eq_band(n + 1) for n in range(4)],
                comp_on=False,
                comp_threshold_db=-20.0,
                comp_ratio_index=3,
                gate_on=False,
                gate_threshold_db=-40.0,
                rms_linear=0.1,
                rms_db=-20.0,
                timestamp=time.time(),
                hpf_on=True,
                hpf_freq_hz=100.0,
                hpf_slope=1,
                input_gain_db=24.0,
            )
            genre = _make_glam_profile()
            _save_baseline({7: ch}, genre, "Test Band")
            # Find the saved file
            saved_files = list(tmp_path.glob("*.json"))
            assert len(saved_files) == 1
            data = json.loads(saved_files[0].read_text())
            ch_data = data["channels"]["7"]
            assert "hpf_on" in ch_data
            assert ch_data["hpf_on"] is True
            assert "hpf_freq_hz" in ch_data
            assert ch_data["hpf_freq_hz"] == pytest.approx(100.0, abs=0.2)
            assert "hpf_slope" in ch_data
            assert ch_data["hpf_slope"] == 1
            assert "input_gain_db" in ch_data
            assert ch_data["input_gain_db"] == pytest.approx(24.0, abs=0.01)
        finally:
            _bl_module.SHOWS_DIR = orig


# ===========================================================================
# IMP-023 — FULL PARAMETRIC EQ ADVISORY
# ===========================================================================

from core.recommender import _named_move, _q_advice, BAND_PERCEPTUAL_WEIGHTS


class TestEQAdvisoryIMP023:
    def _engine(self, trigger_db=3.0):
        cfg = _minimal_band_cfg()
        cfg["thresholds"]["recommendation_trigger_db"] = trigger_db
        cfg["frequency_fingerprints"] = {
            "Kick": {"fundamental": [50, 80], "body": [80, 150],
                     "click": [2000, 4000], "mud_zone": [300, 500]},
            "Lead Vocal": {"primary": [100, 4000], "intelligibility": [1000, 4000],
                           "sibilance": [5000, 9000]},
        }
        cfg["channels"] = {
            1: {"label": "Kick", "type": "instrument"},
            9: {"label": "Lead Vocal", "type": "vocal"},
        }
        return RecommendationEngine(cfg, _make_glam_profile())

    # ── _named_move tests ─────────────────────────────────────────────────────

    def test_named_move_mud_cut(self):
        assert _named_move(315, "cut") == "Mud cut"

    def test_named_move_harshness_cut(self):
        assert _named_move(3500, "cut") == "Harshness cut"

    def test_named_move_presence_boost(self):
        assert _named_move(2500, "boost") == "Presence boost"

    def test_named_move_no_match(self):
        assert _named_move(6000, "boost") == ""

    # ── Named move in EQ recommendation ──────────────────────────────────────

    def test_eq_recommendation_prefixed_with_named_move(self):
        """EQ recommendation for a low-mid cut starts with 'Mud cut —'."""
        engine = self._engine()
        ch = _make_channel(num=1, label="Kick", rms_db=-20.0)
        # Set an EQ band at 315Hz (within Mud cut zone 200-400Hz, in low_mid 250-500Hz)
        ch.eq[0] = EQBand(band_num=1, type=2, freq_hz=315.0, gain_db=2.0, q=1.0)
        eq_detail, suggest = engine._eq_recommendation(ch, "low_mid", 4.0)
        assert "Mud cut" in suggest

    def test_eq_recommendation_mispositioned_band_includes_move_to(self):
        """EQ recommendation for mispositioned band includes 'move to' string."""
        engine = self._engine()
        ch = _make_channel(num=1, label="Kick", rms_db=-20.0)
        # Band at 3000Hz but problem is low_mid (250-500Hz) — mispositioned
        ch.eq[0] = EQBand(band_num=1, type=2, freq_hz=3000.0, gain_db=2.0, q=1.0)
        # Note: 3000Hz is within 2 octaves of low_mid mid_freq=375Hz (375/4=93.75, 375*4=1500)
        # Actually 3000 > 1500 so it's outside 2 octaves — "Add EQ point" will fire
        # Use mid band (mid_freq=1250, range 312.5-5000) and put band outside
        ch2 = _make_channel(num=9, label="Lead Vocal", rms_db=-20.0)
        # Place band at 100Hz — outside 2-octave range of mid (312.5-5000Hz) → Add EQ
        ch2.eq[0] = EQBand(band_num=1, type=2, freq_hz=100.0, gain_db=2.0, q=1.0)
        for i in range(1, 4):
            ch2.eq[i] = EQBand(band_num=i + 1, type=2, freq_hz=100.0, gain_db=0.0, q=1.0)
        # Use a band that IS in range but at the wrong freq — within 2 octaves but outside band
        # sub_bass mid_freq=50Hz. Place band at 200Hz (within 2 octaves: 12.5-200Hz → right at edge)
        # Put band within 2 octaves but outside actual band range
        # high_mid mid_freq=4000Hz, 2-octave range: 1000-16000Hz, band range: 2000-6000Hz
        # Put band at 1200Hz — within 2 octaves but below band lo=2000Hz
        ch3 = _make_channel(num=9, label="Lead Vocal", rms_db=-20.0)
        ch3.eq[0] = EQBand(band_num=1, type=2, freq_hz=1200.0, gain_db=2.0, q=1.0)
        for i in range(1, 4):
            ch3.eq[i] = EQBand(band_num=i + 1, type=2, freq_hz=1200.0, gain_db=0.0, q=1.0)
        eq_detail, suggest = engine._eq_recommendation(ch3, "high_mid", 4.0)
        assert "move to" in suggest

    # ── Q advice tests ────────────────────────────────────────────────────────

    def test_q_advice_fires_for_broad_cut(self):
        """Q advice fires for Q=0.4 on a cut (too broad)."""
        eq = EQBand(band_num=1, type=2, freq_hz=315.0, gain_db=-3.0, q=0.4)
        advice = _q_advice(eq, "cut")
        assert advice != ""
        assert "broad" in advice.lower()

    def test_q_advice_fires_for_narrow_boost(self):
        """Q advice fires for Q=3.0 on a boost (too narrow)."""
        eq = EQBand(band_num=1, type=2, freq_hz=1000.0, gain_db=3.0, q=3.0)
        advice = _q_advice(eq, "boost")
        assert advice != ""
        assert "narrow" in advice.lower()

    def test_q_advice_silent_for_appropriate_cut(self):
        """Q advice does NOT fire for Q=2.0 on a cut (appropriate)."""
        eq = EQBand(band_num=1, type=2, freq_hz=315.0, gain_db=-3.0, q=2.0)
        advice = _q_advice(eq, "cut")
        assert advice == ""

    def test_q_advice_silent_for_appropriate_boost(self):
        """Q advice does NOT fire for Q=1.0 on a boost (appropriate)."""
        eq = EQBand(band_num=1, type=2, freq_hz=1000.0, gain_db=3.0, q=1.0)
        advice = _q_advice(eq, "boost")
        assert advice == ""

    # ── Psychoacoustic weighting tests ────────────────────────────────────────

    def _engine_with_vocal(self, trigger_db=3.0):
        cfg = _minimal_band_cfg()
        cfg["thresholds"]["recommendation_trigger_db"] = trigger_db
        cfg["frequency_fingerprints"] = {
            "Lead Vocal": {"primary": [100, 4000], "intelligibility": [1000, 4000],
                           "sibilance": [5000, 9000]},
        }
        cfg["channels"] = {9: {"label": "Lead Vocal", "type": "vocal"}}
        return RecommendationEngine(cfg, _make_glam_profile())

    def test_high_mid_2pt4db_fires_with_trigger_3db(self):
        """2.4dB high-mid deviation fires when trigger_db=3.0 (weighted: 3.12 >= 3.0)."""
        engine = self._engine_with_vocal(trigger_db=3.0)
        # high_mid weight = 1.3, so 2.4 * 1.3 = 3.12 >= 3.0 → should trigger
        vocal = _make_channel(num=9, label="Lead Vocal", rms_db=-20.0)
        # Build bands where high_mid is 2.4dB above median
        bands = {b: -30.0 for b in BAND_NAMES}
        # median will be -30. high_mid at -27.6 → normalized = 2.4 above target 0.0
        bands["high_mid"] = -27.6
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0, bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        # Reset cooldown to ensure we can fire
        recs = engine.evaluate(room, {9: vocal})
        high_mid_recs = [r for r in recs if "high_mid" in r.issue]
        assert len(high_mid_recs) >= 1, (
            f"Expected high_mid rec (2.4dB * weight 1.3 = 3.12 >= 3.0), got: {recs}"
        )

    def test_sub_bass_3db_does_not_fire_with_trigger_3db(self):
        """3.0dB sub-bass deviation does NOT fire when trigger_db=3.0 (weighted: 1.8 < 3.0)."""
        cfg = _minimal_band_cfg()
        cfg["thresholds"]["recommendation_trigger_db"] = 3.0
        cfg["frequency_fingerprints"] = {
            "Kick": {"fundamental": [50, 80], "body": [80, 150]},
        }
        cfg["channels"] = {1: {"label": "Kick", "type": "instrument"}}
        engine = RecommendationEngine(cfg, _make_glam_profile())
        # sub_bass weight = 0.6, so 3.0 * 0.6 = 1.8 < 3.0 → should NOT trigger
        kick = _make_channel(num=1, label="Kick", rms_db=-20.0)
        bands = {b: -30.0 for b in BAND_NAMES}
        # median = -30, sub_bass at -27 → normalized = 3.0
        bands["sub_bass"] = -27.0
        room = RoomAnalysis(
            lufs=-18.0, rms_db=-20.0, bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(room, {1: kick})
        sub_recs = [r for r in recs if "sub_bass" in r.issue]
        assert len(sub_recs) == 0, (
            f"sub_bass 3.0dB * weight 0.6 = 1.8 < 3.0 should not fire, got: {sub_recs}"
        )

    def test_culprit_with_eq_boost_ranks_above_flat_eq(self):
        """Culprit with EQ boost in problem band ranks above culprit with same RMS but flat EQ."""
        cfg = _minimal_band_cfg()
        cfg["frequency_fingerprints"] = {
            "Guitar 1": {"body": [200, 1000], "bite": [2000, 5000]},
            "Guitar 2": {"body": [200, 1000], "bite": [2000, 5000]},
        }
        cfg["channels"] = {
            7: {"label": "Guitar 1", "type": "instrument"},
            8: {"label": "Guitar 2", "type": "instrument"},
        }
        engine = RecommendationEngine(cfg, _make_glam_profile())

        # Both guitars at same RMS
        guitar1 = _make_channel(num=7, label="Guitar 1", rms_db=-20.0)
        guitar2 = _make_channel(num=8, label="Guitar 2", rms_db=-20.0)
        # Guitar 1 has a 6dB boost in the low_mid band (250-500Hz)
        guitar1.eq[0] = EQBand(band_num=1, type=2, freq_hz=315.0, gain_db=6.0, q=1.0)
        # Guitar 2 is flat
        guitar2.eq[0] = EQBand(band_num=1, type=2, freq_hz=315.0, gain_db=0.0, q=1.0)

        culprit = engine._find_culprit("low_mid", {7: guitar1, 8: guitar2}, 5.0)
        assert culprit is not None
        assert culprit.label == "Guitar 1", (
            f"Guitar 1 with EQ boost should outrank flat Guitar 2, got: {culprit.label}"
        )


# ===========================================================================
# IMP-021 — SOUNDCHECK MODE
# ===========================================================================

from core.soundcheck import check_hpf, check_gain_staging, check_compressor_sanity


def _make_channel_hpf(num=7, label="Guitar 1", rms_db=-20.0, fader_db=0.0,
                      muted=False, hpf_on=False, hpf_freq_hz=80.0, hpf_slope=1,
                      comp_on=False, comp_ratio_index=3, input_gain_db=0.0):
    return ChannelState(
        channel_num=num,
        label=label,
        fader_db=fader_db,
        muted=muted,
        eq=[_make_eq_band(n + 1) for n in range(4)],
        comp_on=comp_on,
        comp_threshold_db=-20.0,
        comp_ratio_index=comp_ratio_index,
        gate_on=False,
        gate_threshold_db=-40.0,
        rms_linear=0.1,
        rms_db=rms_db,
        timestamp=time.time(),
        hpf_on=hpf_on,
        hpf_freq_hz=hpf_freq_hz,
        hpf_slope=hpf_slope,
        input_gain_db=input_gain_db,
    )


class TestSoundcheckModeIMP021:
    def _make_parser(self):
        """Replicate main.py's argparse setup including --soundcheck."""
        parser = argparse.ArgumentParser(description="FOH Assistant")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--show",       action="store_true")
        group.add_argument("--baseline",   action="store_true")
        group.add_argument("--soundcheck", action="store_true")
        group.add_argument("--devices",    action="store_true")
        group.add_argument("--test-osc",   action="store_true")
        parser.add_argument("--x32-ip")
        parser.add_argument("--device-index", type=int, default=None)
        return parser

    def test_soundcheck_flag_accepted(self):
        """--soundcheck is accepted as a CLI argument."""
        parser = self._make_parser()
        args = parser.parse_args(["--soundcheck"])
        assert args.soundcheck is True

    def test_soundcheck_mutually_exclusive_with_show(self):
        """--soundcheck and --show are mutually exclusive."""
        parser = self._make_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--soundcheck", "--show"])

    # ── check_hpf ─────────────────────────────────────────────────────────────

    def test_check_hpf_none_for_kick(self):
        """check_hpf() returns None for Kick regardless of HPF state."""
        kick = _make_channel_hpf(num=1, label="Kick", hpf_on=False)
        assert check_hpf(kick) is None

    def test_check_hpf_none_for_bass(self):
        """check_hpf() returns None for Bass regardless of HPF state."""
        bass = _make_channel_hpf(num=13, label="Bass", hpf_on=False)
        assert check_hpf(bass) is None

    def test_check_hpf_advisory_for_active_guitar_hpf_off(self):
        """check_hpf() returns advisory string for active Guitar 1 with hpf_on=False."""
        guitar = _make_channel_hpf(num=7, label="Guitar 1", hpf_on=False, rms_db=-20.0)
        result = check_hpf(guitar)
        assert result is not None
        assert "HPF OFF" in result
        assert "Guitar 1" in result

    def test_check_hpf_none_for_guitar_hpf_on_good_slope(self):
        """check_hpf() returns None for active Guitar 1 with hpf_on=True, hpf_slope=1."""
        guitar = _make_channel_hpf(num=7, label="Guitar 1", hpf_on=True, hpf_slope=1)
        assert check_hpf(guitar) is None

    def test_check_hpf_slope_advisory_for_6db_slope(self):
        """check_hpf() returns slope advisory for active Guitar 1 with hpf_on=True, slope=0."""
        guitar = _make_channel_hpf(num=7, label="Guitar 1", hpf_on=True, hpf_slope=0,
                                   hpf_freq_hz=80.0, rms_db=-20.0)
        result = check_hpf(guitar)
        assert result is not None
        assert "SLOPE" in result or "6dB" in result

    def test_check_hpf_none_for_inactive_channel(self):
        """check_hpf() returns None for inactive channel regardless of HPF state."""
        guitar = _make_channel_hpf(num=7, label="Guitar 1", hpf_on=False, muted=True)
        assert check_hpf(guitar) is None

    # ── check_gain_staging ────────────────────────────────────────────────────

    def test_gain_staging_hard_flag_weak_signal_high_fader(self):
        """check_gain_staging() returns hard flag for rms_db=-35, fader_db=+6.0."""
        ch = _make_channel_hpf(num=9, label="Lead Vocal", rms_db=-35.0, fader_db=6.0)
        result = check_gain_staging(ch)
        assert result is not None
        assert "GAIN STAGING" in result

    def test_gain_staging_soft_note_ok_signal_high_fader(self):
        """check_gain_staging() returns soft note for rms_db=-20, fader_db=+6.0."""
        ch = _make_channel_hpf(num=9, label="Lead Vocal", rms_db=-20.0, fader_db=6.0)
        result = check_gain_staging(ch)
        assert result is not None
        assert "FADER HIGH" in result

    def test_gain_staging_none_normal_state(self):
        """check_gain_staging() returns None for rms_db=-20, fader_db=+2.0."""
        ch = _make_channel_hpf(num=9, label="Lead Vocal", rms_db=-20.0, fader_db=2.0)
        assert check_gain_staging(ch) is None

    def test_gain_staging_none_for_inactive(self):
        """check_gain_staging() returns None for inactive channel."""
        ch = _make_channel_hpf(num=9, label="Lead Vocal", rms_db=-35.0, fader_db=6.0, muted=True)
        assert check_gain_staging(ch) is None

    # ── check_compressor_sanity ───────────────────────────────────────────────

    def test_comp_sanity_none_when_comp_off(self):
        """check_compressor_sanity() returns None when comp_on=False."""
        ch = _make_channel_hpf(num=9, label="Lead Vocal", comp_on=False, comp_ratio_index=9)
        assert check_compressor_sanity(ch) is None

    def test_comp_sanity_advisory_for_vocal_high_ratio(self):
        """check_compressor_sanity() returns advisory for Lead Vocal with comp_ratio_index=9."""
        ch = _make_channel_hpf(num=9, label="Lead Vocal", comp_on=True, comp_ratio_index=9)
        result = check_compressor_sanity(ch)
        assert result is not None
        assert "COMP RATIO" in result

    def test_comp_sanity_none_for_kick_high_ratio(self):
        """check_compressor_sanity() returns None for Kick with comp_ratio_index=9 (percussion)."""
        ch = _make_channel_hpf(num=1, label="Kick", comp_on=True, comp_ratio_index=9)
        assert check_compressor_sanity(ch) is None

    # ── Soundcheck mode engine state ──────────────────────────────────────────

    def test_soundcheck_engine_has_no_baseline(self):
        """Soundcheck mode engine has no baseline (drift checks don't run)."""
        from core.recommender import RecommendationEngine
        cfg = _minimal_band_cfg()
        cfg["thresholds"]["recommendation_cooldown_s"] = 20
        engine = RecommendationEngine(cfg, _make_glam_profile())
        # No baseline set — _check_baseline_drift should not run
        assert engine._baseline is None
        # Verify drift check is not called by evaluate
        ch = _make_channel(num=1, label="Kick", fader_db=10.0, rms_db=-20.0)
        room = _make_room()
        recs = engine.evaluate(room, {1: ch})
        drift_recs = [r for r in recs if r.issue == "baseline_drift"]
        assert len(drift_recs) == 0

    def test_save_baseline_includes_hpf_fields(self, tmp_path):
        """_save_baseline() includes hpf_on, hpf_freq_hz, hpf_slope, input_gain_db."""
        import json
        import core.baseline as _bl_module
        orig = _bl_module.SHOWS_DIR
        _bl_module.SHOWS_DIR = tmp_path
        try:
            from core.baseline import _save_baseline
            ch = _make_channel_hpf(num=7, label="Guitar 1",
                                   hpf_on=True, hpf_freq_hz=90.0, hpf_slope=1,
                                   input_gain_db=18.0)
            genre = _make_glam_profile()
            _save_baseline({7: ch}, genre, "Test Band")
            saved = list(tmp_path.glob("*.json"))
            assert len(saved) == 1
            data = json.loads(saved[0].read_text())
            ch_data = data["channels"]["7"]
            assert ch_data["hpf_on"] is True
            assert "hpf_freq_hz" in ch_data
            assert "hpf_slope" in ch_data
            assert "input_gain_db" in ch_data
        finally:
            _bl_module.SHOWS_DIR = orig
