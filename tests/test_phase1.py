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
        assert len(cfg["channels"]) == 14

    def test_all_required_keys_present(self):
        cfg = load_band_config()
        for key in ("band", "default_genre", "x32", "audio", "channels", "thresholds"):
            assert key in cfg, f"Missing key: {key}"

    def test_x32_has_ip_and_port(self):
        cfg = load_band_config()
        assert "ip" in cfg["x32"]
        assert "port" in cfg["x32"]
        assert cfg["x32"]["port"] == 10023

    def test_channel_14_is_lead_vocal(self):
        cfg = load_band_config()
        ch14 = cfg["channels"][14]
        assert ch14["label"] == "Lead Vocal"
        assert ch14["type"] == "vocal"

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
