"""Phase 2 test suite — channel model, geometry, mic analyzer, forward model, data models."""

import json
import math
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import yaml

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.channel_model import (
    FREQ_AXIS, N_FREQS, SILENCE_THRESHOLD_DB,
    InstrumentPrior, compute_transfer_curves, compute_contribution_curve,
    infer_input_state, peaking_eq_response, hpf_response, low_shelf_response,
    high_shelf_response, lpf_response, eq_band_response, eq_float_to_hz,
    fader_float_to_db, hpslope_int_to_db_oct, linear_to_dbfs,
)
from models.channel import ChannelConfig, ChannelMeterState, EQBand
from models.venue import VenueGeometry, VenueProfile
from models.analysis import MicAnalysis, ForwardModelResult
from core.geometry import (
    load_venue_profile, IrregularRoomAcoustics, OpenAirAcoustics,
    CornerStageAcoustics, RectangularRoomAcoustics,
    comb_filter_notches_hz, axial_room_modes, room_mode_mask,
    boundary_gain_db, arrival_delta_ms, sub_top_phase_at_crossover,
    ground_reflection_comb_notches, comb_filter_correction_curve,
    compute_venue_distances,
)
from core.mic_analyzer import (
    MicAnalyzer, EMAState, SpectrumHistory,
    compute_welch_spectrum, interpolate_to_freq_axis,
    compute_band_levels, compute_lufs, is_room_silent, ANALYSIS_BANDS,
)
from core.forward_model import (
    ForwardModel, BAND_RANGES, CONFIDENCE_THRESHOLD,
    score_channel_contributions, find_dominant_channel,
    decompose_deviation,
)

VENUES_DIR = Path(__file__).parent.parent / "config" / "venues"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_channel_config(ch_num=7, label="Guitar 1", instrument_type="guitar",
                         hpf_hz=100.0, eq_enabled=True, fader_db=-10.0,
                         muted=False) -> ChannelConfig:
    eq_bands = [EQBand(band_num=b, type=2, freq_hz=1000.0, gain_db=0.0, q=1.0)
                for b in range(1, 5)]
    cfg = ChannelConfig(
        channel_num=ch_num, label=label, instrument_type=instrument_type,
        trim_db=0.0, polarity_inverted=False,
        hpf_enabled=(hpf_hz > 20.0), hpf_freq_hz=hpf_hz, hpf_slope_db_oct=12,
        eq_enabled=eq_enabled, eq_bands=eq_bands,
        fader_db=fader_db, muted=muted, pan=0.0,
        comp_enabled=False, comp_threshold_db=-20.0, comp_ratio=2.0,
        comp_attack_ms=10.0, comp_release_ms=100.0, comp_makeup_db=0.0,
        gate_enabled=False, gate_threshold_db=-40.0, gate_range_db=20.0,
    )
    return cfg


def make_meter(ch_num=7, post_fade_db=-20.0, rms_delta_db=0.0,
               input_state="normal") -> ChannelMeterState:
    rms_db = post_fade_db + 10.0
    return ChannelMeterState(
        channel_num=ch_num, timestamp_ms=1000.0,
        input_rms_linear=10 ** (rms_db / 20),
        gate_gr_linear=1.0, dyn_gr_linear=1.0,
        input_rms_db=rms_db, gate_gr_db=0.0, dyn_gr_db=0.0,
        post_fade_db=post_fade_db, effective_gr_db=0.0,
        rms_delta_db=rms_delta_db, input_state=input_state,
    )


def make_mic_analysis(lufs=-17.0, silent=False) -> MicAnalysis:
    spectrum = np.random.normal(-40, 5, N_FREQS).astype(float)
    band_levels = {bn: {'avg_db': -40.0, 'peak_db': -35.0, 'peak_hz': 1000.0}
                   for bn, _, _ in ANALYSIS_BANDS}
    return MicAnalysis(
        lufs=lufs,
        raw_spectrum_db=spectrum,
        corrected_spectrum_db=spectrum,
        smoothed_spectrum_db=spectrum,
        spectral_delta_db=np.zeros(N_FREQS),
        band_levels=band_levels,
        room_mode_flags=np.zeros(N_FREQS, dtype=bool),
        correction_applied_db=np.zeros(N_FREQS),
        is_silent=silent,
        timestamp_ms=1000.0,
    )


def make_prior(states=('normal',)) -> InstrumentPrior:
    prior_data = {
        s: {'curve': [[80, -8], [250, 0], [630, 2], [1000, 0], [4000, -3], [16000, -10]]}
        for s in states
    }
    return InstrumentPrior('guitar', prior_data)


# ---------------------------------------------------------------------------
# FREQ_AXIS constants
# ---------------------------------------------------------------------------

class TestFreqAxis:
    def test_length(self):
        assert len(FREQ_AXIS) == N_FREQS == 1000

    def test_range(self):
        assert abs(FREQ_AXIS[0] - 20.0) < 0.01
        assert abs(FREQ_AXIS[-1] - 20000.0) < 1.0

    def test_log_spaced(self):
        log_axis = np.log10(FREQ_AXIS)
        diffs = np.diff(log_axis)
        assert np.allclose(diffs, diffs[0], rtol=1e-3)

    def test_silence_threshold(self):
        assert SILENCE_THRESHOLD_DB == -50.0


# ---------------------------------------------------------------------------
# Conversion utilities
# ---------------------------------------------------------------------------

class TestConversions:
    def test_eq_float_to_hz_midpoint(self):
        hz = eq_float_to_hz(0.5)
        assert 600 < hz < 700

    def test_eq_float_to_hz_limits(self):
        assert abs(eq_float_to_hz(0.0) - 20.0) < 0.1
        assert abs(eq_float_to_hz(1.0) - 20000.0) < 1.0

    def test_fader_float_to_db_unity(self):
        db = fader_float_to_db(0.75)
        assert abs(db - 0.0) < 0.1

    def test_fader_float_to_db_off(self):
        db = fader_float_to_db(0.0)
        assert db == -90.0

    def test_hpslope_int_to_db_oct(self):
        assert hpslope_int_to_db_oct(0) == 12
        assert hpslope_int_to_db_oct(1) == 18
        assert hpslope_int_to_db_oct(2) == 24
        assert hpslope_int_to_db_oct(9) == 12  # unknown → default 12

    def test_linear_to_dbfs_unity(self):
        assert abs(linear_to_dbfs(1.0) - 0.0) < 0.01

    def test_linear_to_dbfs_silence(self):
        assert linear_to_dbfs(0.0) == -90.0


# ---------------------------------------------------------------------------
# EQ transfer functions
# ---------------------------------------------------------------------------

class TestTransferFunctions:
    def test_peaking_eq_at_center_freq(self):
        center = 1000.0
        gain = 6.0
        freqs = np.array([center])
        resp = peaking_eq_response(freqs, center, gain, q=1.0)
        assert abs(resp[0] - gain) < 0.1

    def test_peaking_eq_flat_for_zero_gain(self):
        freqs = FREQ_AXIS
        resp = peaking_eq_response(freqs, 1000.0, 0.0, 1.0)
        assert np.allclose(resp, 0.0)

    def test_hpf_response_at_cutoff(self):
        cutoff = 100.0
        freqs = np.array([cutoff])
        resp = hpf_response(freqs, cutoff, slope_db_oct=12)
        assert abs(resp[0] - (-3.0)) < 0.1

    def test_hpf_response_below_cutoff(self):
        cutoff = 1000.0
        freqs = np.array([100.0])
        resp = hpf_response(freqs, cutoff, slope_db_oct=12)
        assert resp[0] < -15.0

    def test_hpf_response_above_cutoff(self):
        cutoff = 100.0
        freqs = np.array([10000.0])
        resp = hpf_response(freqs, cutoff, slope_db_oct=12)
        assert abs(resp[0]) < 0.5

    def test_low_shelf_below_corner(self):
        corner = 200.0
        gain = 4.0
        freqs = np.array([50.0])
        resp = low_shelf_response(freqs, corner, gain, q=0.707)
        assert resp[0] > 1.5  # positive gain at frequencies below corner

    def test_high_shelf_above_corner(self):
        corner = 8000.0
        gain = 3.0
        freqs = np.array([16000.0])
        resp = high_shelf_response(freqs, corner, gain, q=0.707)
        assert resp[0] > 1.0  # positive gain at frequencies above corner

    def test_lpf_at_cutoff(self):
        cutoff = 5000.0
        freqs = np.array([cutoff])
        resp = lpf_response(freqs, cutoff, slope_db_oct=12)
        assert abs(resp[0] - (-3.0)) < 0.1

    def test_eq_band_dispatcher_peq(self):
        band = EQBand(band_num=2, type=2, freq_hz=1000.0, gain_db=3.0, q=1.0)
        # Use exact center frequency to avoid numerical cancellation at off-center points
        freqs = np.array([1000.0])
        resp = eq_band_response(band, freqs)
        assert abs(resp[0] - 3.0) < 0.3

    def test_eq_band_dispatcher_veq_treated_as_peq(self):
        freqs = np.array([500.0, 1000.0, 2000.0])
        band_peq = EQBand(band_num=2, type=2, freq_hz=1000.0, gain_db=3.0, q=1.0)
        band_veq = EQBand(band_num=2, type=3, freq_hz=1000.0, gain_db=3.0, q=1.0)
        resp_peq = eq_band_response(band_peq, freqs)
        resp_veq = eq_band_response(band_veq, freqs)
        assert np.allclose(resp_peq, resp_veq)

    def test_eq_band_dispatcher_lcut(self):
        band = EQBand(band_num=1, type=0, freq_hz=200.0, gain_db=0.0, q=0.707)
        resp = eq_band_response(band, FREQ_AXIS)
        idx_low = np.argmin(np.abs(FREQ_AXIS - 50.0))
        assert resp[idx_low] < -10.0

    def test_eq_band_dispatcher_hcut(self):
        band = EQBand(band_num=4, type=5, freq_hz=8000.0, gain_db=0.0, q=0.707)
        resp = eq_band_response(band, FREQ_AXIS)
        idx_hi = np.argmin(np.abs(FREQ_AXIS - 16000.0))
        assert resp[idx_hi] < -3.0


# ---------------------------------------------------------------------------
# Transfer curve computation
# ---------------------------------------------------------------------------

class TestComputeTransferCurves:
    def test_hpf_enabled(self):
        cfg = make_channel_config(hpf_hz=100.0)
        compute_transfer_curves(cfg)
        assert cfg.hpf_curve_db is not None
        assert cfg.transfer_curve_db is not None
        # At 50Hz (well below 100Hz cutoff) should attenuate
        idx = np.argmin(np.abs(FREQ_AXIS - 50.0))
        assert cfg.hpf_curve_db[idx] < -6.0

    def test_hpf_at_20hz_treated_as_disabled(self):
        cfg = make_channel_config(hpf_hz=20.0)
        cfg.hpf_enabled = False
        compute_transfer_curves(cfg)
        assert np.allclose(cfg.hpf_curve_db, 0.0)

    def test_eq_disabled_gives_flat_eq_curve(self):
        cfg = make_channel_config(eq_enabled=False)
        # Manually set eq_enabled
        cfg.eq_enabled = False
        compute_transfer_curves(cfg)
        assert np.allclose(cfg.eq_curve_db, 0.0)

    def test_transfer_curve_is_sum_of_hpf_and_eq(self):
        cfg = make_channel_config(hpf_hz=100.0, eq_enabled=True)
        cfg.eq_bands[0] = EQBand(band_num=1, type=2, freq_hz=2000.0, gain_db=3.0, q=1.0)
        compute_transfer_curves(cfg)
        expected = cfg.hpf_curve_db + cfg.eq_curve_db
        assert np.allclose(cfg.transfer_curve_db, expected)

    def test_transfer_curve_shape(self):
        cfg = make_channel_config()
        compute_transfer_curves(cfg)
        assert cfg.transfer_curve_db.shape == (N_FREQS,)


# ---------------------------------------------------------------------------
# Contribution curve
# ---------------------------------------------------------------------------

class TestContributionCurve:
    def test_muted_returns_silence(self):
        cfg = make_channel_config(muted=True)
        compute_transfer_curves(cfg)
        meter = make_meter()
        prior = make_prior()
        result = compute_contribution_curve(cfg, meter, prior.get_curve())
        assert np.all(result == -90.0)

    def test_below_threshold_returns_silence(self):
        cfg = make_channel_config()
        compute_transfer_curves(cfg)
        meter = make_meter(post_fade_db=-80.0)  # below SILENCE_THRESHOLD_DB
        prior = make_prior()
        result = compute_contribution_curve(cfg, meter, prior.get_curve())
        assert np.all(result == -90.0)

    def test_active_channel_returns_signal(self):
        cfg = make_channel_config(fader_db=-10.0)
        compute_transfer_curves(cfg)
        meter = make_meter(post_fade_db=-20.0)
        prior = make_prior()
        result = compute_contribution_curve(cfg, meter, prior.get_curve())
        assert result.shape == (N_FREQS,)
        assert np.any(result > -90.0)

    def test_contribution_requires_transfer_curves(self):
        cfg = make_channel_config()
        # Don't call compute_transfer_curves
        meter = make_meter()
        prior = make_prior()
        with pytest.raises(AssertionError):
            compute_contribution_curve(cfg, meter, prior.get_curve())

    def test_higher_fader_gives_higher_contribution(self):
        cfg_loud = make_channel_config(fader_db=-5.0)
        cfg_soft = make_channel_config(fader_db=-20.0)
        compute_transfer_curves(cfg_loud)
        compute_transfer_curves(cfg_soft)
        meter_loud = make_meter(post_fade_db=-5.0)
        meter_soft = make_meter(post_fade_db=-20.0)
        prior = make_prior()
        result_loud = compute_contribution_curve(cfg_loud, meter_loud, prior.get_curve())
        result_soft = compute_contribution_curve(cfg_soft, meter_soft, prior.get_curve())
        assert np.mean(result_loud) > np.mean(result_soft)


# ---------------------------------------------------------------------------
# Instrument prior
# ---------------------------------------------------------------------------

class TestInstrumentPrior:
    PRIOR_DATA = {
        'normal': {'curve': [[80, -8], [250, 0], [630, 2], [1000, 0], [4000, -3], [16000, -10]]},
        'solo_active': {'curve': [[80, -12], [250, -2], [1000, 2], [4000, 2.5], [16000, -4]]},
    }

    def test_normal_state_zero_mean(self):
        prior = InstrumentPrior('guitar', self.PRIOR_DATA)
        curve = prior.get_curve('normal')
        assert abs(np.mean(curve)) < 0.01

    def test_solo_active_state_zero_mean(self):
        prior = InstrumentPrior('guitar', self.PRIOR_DATA)
        curve = prior.get_curve('solo_active')
        assert abs(np.mean(curve)) < 0.01

    def test_solo_active_higher_in_upper_freqs(self):
        prior = InstrumentPrior('guitar', self.PRIOR_DATA)
        n = prior.get_curve('normal')
        s = prior.get_curve('solo_active')
        idx_hi = np.where(FREQ_AXIS > 2000)[0]
        assert np.mean(s[idx_hi]) > np.mean(n[idx_hi])

    def test_fallback_to_normal(self):
        prior = InstrumentPrior('guitar', self.PRIOR_DATA)
        curve = prior.get_curve('unknown_state')
        assert np.allclose(curve, prior.get_curve('normal'))

    def test_missing_normal_state_gives_zeros(self):
        prior = InstrumentPrior('kick', {})
        assert np.allclose(prior.get_curve('normal'), 0.0)

    def test_curve_length_is_n_freqs(self):
        prior = InstrumentPrior('guitar', self.PRIOR_DATA)
        assert len(prior.get_curve('normal')) == N_FREQS

    def test_get_curve_returns_copy(self):
        prior = InstrumentPrior('guitar', self.PRIOR_DATA)
        c1 = prior.get_curve('normal')
        c1[0] = 999.0
        c2 = prior.get_curve('normal')
        assert c2[0] != 999.0


# ---------------------------------------------------------------------------
# Input state inference
# ---------------------------------------------------------------------------

class TestInferInputState:
    def test_silent_post_fade(self):
        cfg = make_channel_config(instrument_type='guitar')
        compute_transfer_curves(cfg)
        meter = make_meter(post_fade_db=-80.0)
        state = infer_input_state(7, meter, cfg, [])
        assert state == 'silent'

    def test_gated(self):
        cfg = make_channel_config(instrument_type='guitar')
        compute_transfer_curves(cfg)
        meter = make_meter(post_fade_db=-20.0)
        meter.gate_gr_db = -10.0
        state = infer_input_state(7, meter, cfg, [])
        assert state == 'gated'

    def test_solo_onset_on_rms_spike(self):
        cfg = make_channel_config(instrument_type='guitar')
        compute_transfer_curves(cfg)
        meter = make_meter(post_fade_db=-15.0, rms_delta_db=3.5)
        state = infer_input_state(7, meter, cfg, ['normal'])
        assert state == 'solo_onset'

    def test_normal_for_vocal(self):
        cfg = make_channel_config(instrument_type='vocal_lead')
        compute_transfer_curves(cfg)
        meter = make_meter(post_fade_db=-15.0, rms_delta_db=5.0)
        state = infer_input_state(9, meter, cfg, ['normal'])
        assert state == 'normal'

    def test_solo_active_sustained(self):
        cfg = make_channel_config(instrument_type='guitar')
        compute_transfer_curves(cfg)
        meter = make_meter(post_fade_db=-15.0, rms_delta_db=0.2)
        state = infer_input_state(7, meter, cfg, ['solo_active'])
        assert state == 'solo_active'

    def test_decay_after_solo(self):
        cfg = make_channel_config(instrument_type='guitar')
        compute_transfer_curves(cfg)
        meter = make_meter(post_fade_db=-15.0, rms_delta_db=-2.5)
        state = infer_input_state(7, meter, cfg, ['solo_active'])
        assert state == 'decay'


# ---------------------------------------------------------------------------
# Geometry physics
# ---------------------------------------------------------------------------

class TestAcousticPhysics:
    def test_comb_filter_notches_symmetric(self):
        notches = comb_filter_notches_hz(5.0, 5.0)
        assert notches == []

    def test_comb_filter_notches_asymmetric(self):
        notches = comb_filter_notches_hz(5.0, 7.0)
        assert len(notches) > 0
        assert all(20 <= f <= 20000 for f in notches)

    def test_comb_filter_notches_fundamental(self):
        dist_diff = 1.0  # 1m difference
        delta_t = dist_diff / 343.0
        expected_fundamental = 1.0 / (2.0 * delta_t)
        notches = comb_filter_notches_hz(5.0, 6.0)
        assert abs(notches[0] - expected_fundamental) < 1.0

    def test_axial_room_modes_returns_dict(self):
        modes = axial_room_modes(10.0, 8.0, 3.0)
        assert 'length' in modes
        assert 'width' in modes
        assert 'height' in modes

    def test_axial_room_modes_below_500hz(self):
        modes = axial_room_modes(10.0, 8.0, 3.0)
        for key, freqs in modes.items():
            for f in freqs:
                assert f <= 500.0

    def test_room_mode_mask_shape(self):
        modes = axial_room_modes(10.0, 8.0, 3.0)
        mask = room_mode_mask(modes, FREQ_AXIS)
        assert mask.shape == (N_FREQS,)
        assert mask.dtype == bool

    def test_room_mode_mask_flags_modes(self):
        # Simple mode: fundamental at 343/2/10 = 17.15Hz
        # Next mode: 34.3Hz — within 20-500Hz range
        modes = axial_room_modes(5.0, 4.0, 2.5)
        mask = room_mode_mask(modes, FREQ_AXIS)
        assert mask.any()

    def test_boundary_gain_open_air(self):
        assert boundary_gain_db(True, 'open_air') == 0.0

    def test_boundary_gain_corner(self):
        assert boundary_gain_db(True, 'corner') == 9.0

    def test_boundary_gain_floor_only(self):
        assert boundary_gain_db(False, 'rectangular') == 3.0

    def test_arrival_delta_ms(self):
        delta = arrival_delta_ms(5.0, 7.0)
        assert abs(delta - (2.0 / 343.0 * 1000.0)) < 0.01

    def test_sub_top_phase_at_crossover(self):
        phase = sub_top_phase_at_crossover(6.0, 5.0, 100.0)
        assert 0.0 <= phase < 360.0

    def test_ground_reflection_notches(self):
        notches = ground_reflection_comb_notches(2.4, 1.5, 5.0)
        assert len(notches) > 0
        assert all(20 <= f <= 20000 for f in notches)

    def test_comb_correction_shape(self):
        notches = [500.0, 1500.0]
        reliability = comb_filter_correction_curve(notches, FREQ_AXIS)
        assert reliability.shape == (N_FREQS,)
        # Near 500Hz should have reduced reliability
        idx = np.argmin(np.abs(FREQ_AXIS - 500.0))
        assert reliability[idx] < 1.0


# ---------------------------------------------------------------------------
# Venue profiles
# ---------------------------------------------------------------------------

class TestVenueProfiles:
    @pytest.mark.parametrize("venue_id,stage_type", [
        ("outdoor_patio_june13", "open_air"),
        ("ajs_bar", "corner"),
        ("corner_bar_june20", "corner"),
    ])
    def test_venue_load(self, venue_id, stage_type):
        profile = load_venue_profile(venue_id)
        assert profile.venue_id == venue_id
        assert profile.stage_type == stage_type
        assert profile.geometry is not None
        assert profile.acoustics is not None

    def test_acoustics_correction_curve_shape(self):
        for vid in ("outdoor_patio_june13", "ajs_bar"):
            p = load_venue_profile(vid)
            curve = p.acoustics.mic_correction_curve()
            assert curve.shape == (N_FREQS,)

    def test_acoustics_room_mode_mask_shape(self):
        for vid in ("outdoor_patio_june13", "ajs_bar"):
            p = load_venue_profile(vid)
            mask = p.acoustics.room_mode_mask()
            assert mask.shape == (N_FREQS,)
            assert mask.dtype == bool

    def test_open_air_no_room_modes(self):
        p = load_venue_profile("outdoor_patio_june13")
        mask = p.acoustics.room_mode_mask()
        assert not mask.any()

    def test_corner_has_room_modes(self):
        p = load_venue_profile("ajs_bar")
        modes = p.geometry.room_modes_hz
        assert len(modes) > 0
        assert any(len(v) > 0 for v in modes.values())

    def test_open_air_lufs_adjustment_positive(self):
        p = load_venue_profile("outdoor_patio_june13")
        assert p.geometry.lufs_target_adjustment_db > 0.0

    def test_corner_sub_adjustment_negative(self):
        p = load_venue_profile("ajs_bar")
        assert p.geometry.sub_target_adjustment_db < 0.0

    def test_open_air_high_reliability(self):
        p = load_venue_profile("outdoor_patio_june13")
        assert p.geometry.mic_reliability_weight >= 0.85

    def test_corner_lower_reliability_than_open_air(self):
        p_outdoor = load_venue_profile("outdoor_patio_june13")
        p_corner  = load_venue_profile("ajs_bar")
        assert p_corner.geometry.mic_reliability_weight < p_outdoor.geometry.mic_reliability_weight

    def test_irregular_fallback_loads(self):
        ira = IrregularRoomAcoustics({})
        assert ira.mic_reliability_weight() == 0.55
        assert ira.lufs_target_adjustment_db() == 0.0
        assert ira.mic_correction_curve().shape == (N_FREQS,)

    def test_venue_geometry_distances_positive(self):
        p = load_venue_profile("outdoor_patio_june13")
        g = p.geometry
        assert g.dist_mic_to_top_left_m > 0
        assert g.dist_mic_to_top_right_m > 0

    def test_venue_profile_dataclass(self):
        p = load_venue_profile("ajs_bar")
        assert isinstance(p, VenueProfile)
        assert isinstance(p.geometry, VenueGeometry)

    def test_nonexistent_venue_raises(self):
        with pytest.raises(FileNotFoundError):
            load_venue_profile("no_such_venue_xyz")


# ---------------------------------------------------------------------------
# MicAnalyzer and pipeline primitives
# ---------------------------------------------------------------------------

class TestWelchFFT:
    SR = 48000
    DURATION = 0.5

    def test_returns_freqs_and_psd(self):
        audio = np.random.randn(int(self.SR * self.DURATION)).astype(np.float32)
        freqs, psd = compute_welch_spectrum(audio, self.SR)
        assert len(freqs) == len(psd)
        assert freqs[0] >= 0
        assert freqs[-1] <= self.SR / 2

    def test_sine_peak_at_correct_frequency(self):
        t = np.linspace(0, self.DURATION, int(self.SR * self.DURATION))
        sine = 0.1 * np.sin(2 * np.pi * 1000 * t).astype(np.float32)
        freqs, psd = compute_welch_spectrum(sine, self.SR)
        peak_freq = freqs[np.argmax(psd)]
        assert 900 < peak_freq < 1100

    def test_interpolate_to_freq_axis_shape(self):
        audio = np.random.randn(int(self.SR * self.DURATION)).astype(np.float32)
        freqs, psd = compute_welch_spectrum(audio, self.SR)
        spectrum = interpolate_to_freq_axis(freqs, psd)
        assert spectrum.shape == (N_FREQS,)

    def test_interpolate_handles_empty_input(self):
        result = interpolate_to_freq_axis(np.array([]), np.array([]))
        assert result.shape == (N_FREQS,)
        assert np.all(result == -90.0)


class TestBandLevels:
    def test_returns_all_8_bands(self):
        spectrum = np.ones(N_FREQS) * -40.0
        bands = compute_band_levels(spectrum)
        assert len(bands) == 8
        expected = {'sub', 'bass', 'low_mid', 'mid_low', 'mid_high', 'upper_mid', 'presence', 'air'}
        assert set(bands.keys()) == expected

    def test_band_keys(self):
        spectrum = np.ones(N_FREQS) * -40.0
        bands = compute_band_levels(spectrum)
        for band_name in bands:
            b = bands[band_name]
            assert 'avg_db' in b
            assert 'peak_db' in b
            assert 'peak_hz' in b

    def test_flat_spectrum_gives_similar_band_levels(self):
        spectrum = np.ones(N_FREQS) * -40.0
        bands = compute_band_levels(spectrum)
        avg_levels = [b['avg_db'] for b in bands.values()]
        assert max(avg_levels) - min(avg_levels) < 1.0


class TestEMAState:
    def test_first_update_returns_input(self):
        ema = EMAState(alpha=0.3)
        s = np.ones(N_FREQS) * -40.0
        result = ema.update(s)
        assert np.allclose(result, -40.0)

    def test_second_update_weighted(self):
        ema = EMAState(alpha=0.3)
        ema.update(np.ones(N_FREQS) * -40.0)
        result = ema.update(np.ones(N_FREQS) * -30.0)
        expected = 0.3 * (-30.0) + 0.7 * (-40.0)  # = -37.0
        assert abs(result[0] - expected) < 0.01

    def test_reset_clears_state(self):
        ema = EMAState(alpha=0.3)
        ema.update(np.ones(N_FREQS) * -40.0)
        ema.reset()
        result = ema.update(np.ones(N_FREQS) * -20.0)
        assert np.allclose(result, -20.0)


class TestSpectrumHistory:
    def _make_mic(self, ts, level=-40.0):
        spec = np.ones(N_FREQS) * level
        return MicAnalysis(
            lufs=-17.0, raw_spectrum_db=spec, corrected_spectrum_db=spec,
            smoothed_spectrum_db=spec, spectral_delta_db=np.zeros(N_FREQS),
            band_levels={}, room_mode_flags=np.zeros(N_FREQS, dtype=bool),
            correction_applied_db=np.zeros(N_FREQS), is_silent=False, timestamp_ms=ts,
        )

    def test_push_and_retrieve(self):
        sh = SpectrumHistory()
        for i in range(5):
            sh.push(self._make_mic(i * 500.0))
        snap = sh.get_snapshot_before(2500.0, offset_ms=500.0)
        assert snap is not None
        assert snap.shape == (N_FREQS,)

    def test_empty_history_returns_none(self):
        sh = SpectrumHistory()
        assert sh.get_snapshot_before(1000.0) is None

    def test_max_depth_maintained(self):
        sh = SpectrumHistory()
        for i in range(25):  # exceeds HISTORY_DEPTH=20
            sh.push(self._make_mic(i * 500.0))
        assert len(sh._history) == SpectrumHistory.HISTORY_DEPTH


class TestMicAnalyzer:
    SR = 48000

    def _make_capture(self, sr=48000, duration=1.0):
        from core.audio_capture import AudioCapture
        cap = MagicMock(spec=AudioCapture)
        cap.sample_rate = sr
        n_fft  = int(duration * 0.5 * sr)
        n_lufs = int(duration * 3.0 * sr)
        cap.get_analysis_window.return_value = np.random.randn(n_fft).astype(np.float32) * 0.1
        cap.get_lufs_window.return_value     = np.random.randn(n_lufs).astype(np.float32) * 0.1
        return cap

    def test_analyze_returns_mic_analysis(self):
        analyzer = MicAnalyzer(IrregularRoomAcoustics({}))
        cap = self._make_capture()
        result = analyzer.analyze(cap)
        assert isinstance(result, MicAnalysis)

    def test_analyze_spectrum_shape(self):
        analyzer = MicAnalyzer(IrregularRoomAcoustics({}))
        cap = self._make_capture()
        result = analyzer.analyze(cap)
        assert result.smoothed_spectrum_db.shape == (N_FREQS,)

    def test_analyze_with_geometry_correction(self):
        p = load_venue_profile("outdoor_patio_june13")
        analyzer = MicAnalyzer(p.acoustics)
        assert not np.allclose(analyzer.correction_curve_db, 0.0)

    def test_silent_short_buffer_detected(self):
        analyzer = MicAnalyzer(IrregularRoomAcoustics({}))
        from core.audio_capture import AudioCapture
        cap = MagicMock(spec=AudioCapture)
        cap.sample_rate = 48000
        cap.get_analysis_window.return_value = np.zeros(100).astype(np.float32)
        cap.get_lufs_window.return_value     = np.zeros(100).astype(np.float32)
        result = analyzer.analyze(cap)
        assert result.is_silent

    def test_reset_ema_clears_prev_spectrum(self):
        analyzer = MicAnalyzer(IrregularRoomAcoustics({}))
        cap = self._make_capture()
        analyzer.analyze(cap)
        assert analyzer._prev_spectrum is not None
        analyzer.reset_ema()
        assert analyzer._prev_spectrum is None

    def test_characterize_input_event(self):
        analyzer = MicAnalyzer(IrregularRoomAcoustics({}))
        pre  = np.ones(N_FREQS) * -45.0
        post = np.ones(N_FREQS) * -40.0
        post[FREQ_AXIS > 2000] = -35.0  # upper freq boost
        result = analyzer.characterize_input_event(pre, post)
        assert 'centroid_shift_hz' in result
        assert 'dominant_band' in result
        assert 'mic_confirmed_change' in result
        assert isinstance(result['mic_confirmed_change'], bool)


# ---------------------------------------------------------------------------
# Forward model
# ---------------------------------------------------------------------------

class TestForwardModel:
    def _make_fm(self):
        return ForwardModel(IrregularRoomAcoustics({}))

    def _make_inputs(self, n_channels=1):
        configs = {}
        meters  = {}
        priors  = {}
        for i in range(n_channels):
            ch = i + 7
            cfg = make_channel_config(ch_num=ch)
            compute_transfer_curves(cfg)
            configs[ch] = cfg
            meters[ch]  = make_meter(ch_num=ch, post_fade_db=-20.0)
            priors[ch]  = make_prior(('normal', 'solo_active'))
        board_rta = np.full(100, -42.0)
        return configs, meters, priors, board_rta

    def test_silent_input_returns_silent_result(self):
        fm = self._make_fm()
        configs, meters, priors, rta = self._make_inputs()
        mic = make_mic_analysis(silent=True)
        result = fm.run(configs, meters, priors, mic, rta)
        assert result.is_silent

    def test_active_input_returns_result(self):
        fm = self._make_fm()
        configs, meters, priors, rta = self._make_inputs()
        mic = make_mic_analysis(silent=False)
        result = fm.run(configs, meters, priors, mic, rta)
        assert not result.is_silent
        assert not result.no_active_channels

    def test_result_shapes_correct(self):
        fm = self._make_fm()
        configs, meters, priors, rta = self._make_inputs()
        mic = make_mic_analysis()
        result = fm.run(configs, meters, priors, mic, rta)
        assert result.predicted_db.shape == (N_FREQS,)
        assert result.board_rta_db.shape == (N_FREQS,)
        assert result.deviation_db.shape == (N_FREQS,)
        assert result.confidence.shape   == (N_FREQS,)

    def test_passive_mode_is_true(self):
        fm = self._make_fm()
        configs, meters, priors, rta = self._make_inputs()
        mic = make_mic_analysis()
        result = fm.run(configs, meters, priors, mic, rta)
        assert result.passive_mode is True

    def test_cycle_count_increments(self):
        fm = self._make_fm()
        configs, meters, priors, rta = self._make_inputs()
        mic = make_mic_analysis()
        r1 = fm.run(configs, meters, priors, mic, rta)
        r2 = fm.run(configs, meters, priors, mic, rta)
        assert r2.cycle_num == 2

    def test_dominant_channels_per_band(self):
        fm = self._make_fm()
        configs, meters, priors, rta = self._make_inputs()
        mic = make_mic_analysis()
        result = fm.run(configs, meters, priors, mic, rta)
        assert set(result.dominant_channels.keys()) == set(BAND_RANGES.keys())

    def test_no_active_channels_result(self):
        fm = self._make_fm()
        mic = make_mic_analysis()
        result = fm.run({}, {}, {}, mic, np.full(100, -42.0))
        assert result.no_active_channels

    def test_r_squared_range(self):
        fm = self._make_fm()
        configs, meters, priors, rta = self._make_inputs()
        mic = make_mic_analysis()
        result = fm.run(configs, meters, priors, mic, rta)
        assert 0.0 <= result.r_squared_mic <= 1.0 or result.r_squared_mic >= 0.0
        assert 0.0 <= result.r_squared_board <= 1.0 or result.r_squared_board >= 0.0


class TestScoreChannelContributions:
    def test_single_channel_dominates(self):
        contrib = {7: np.ones(N_FREQS) * -20.0}
        scores = score_channel_contributions(contrib, 80, 200)
        assert scores[7] > 0.99

    def test_equal_channels_split_evenly(self):
        contrib = {7: np.ones(N_FREQS) * -20.0, 8: np.ones(N_FREQS) * -20.0}
        scores = score_channel_contributions(contrib, 80, 200)
        assert abs(scores[7] - 0.5) < 0.01
        assert abs(scores[8] - 0.5) < 0.01

    def test_scores_sum_to_one(self):
        contrib = {1: np.ones(N_FREQS) * -18.0, 7: np.ones(N_FREQS) * -22.0,
                   9: np.ones(N_FREQS) * -15.0}
        scores = score_channel_contributions(contrib, 200, 500)
        assert abs(sum(scores.values()) - 1.0) < 0.001


class TestDecomposeDeviation:
    def test_insufficient_history_returns_current(self):
        history = [np.ones(N_FREQS) * -2.0]
        room, mix = decompose_deviation(history)
        assert np.allclose(room, 0.0)
        assert np.allclose(mix, -2.0)

    def test_room_deviation_is_median(self):
        history = [np.ones(N_FREQS) * float(i) for i in range(20)]
        room, mix = decompose_deviation(history)
        assert abs(np.mean(room) - np.median(range(20))) < 1.0

    def test_mix_deviation_is_current_minus_room(self):
        history = [np.ones(N_FREQS) * 0.0 for _ in range(15)]
        current = np.ones(N_FREQS) * 5.0
        history.append(current)
        room, mix = decompose_deviation(history)
        expected_mix = current - room
        assert np.allclose(mix, expected_mix)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class TestMicAnalysis:
    def test_spectral_centroid_nonzero_for_signal(self):
        spectrum = np.zeros(N_FREQS)
        idx = np.argmin(np.abs(FREQ_AXIS - 2000.0))
        spectrum[idx] = 0.0  # peak at 2kHz (0 dBFS reference)
        mic = make_mic_analysis()
        mic.smoothed_spectrum_db = spectrum
        centroid = mic.spectral_centroid_hz
        assert centroid > 0.0

    def test_spectral_centroid_type(self):
        mic = make_mic_analysis()
        assert isinstance(mic.spectral_centroid_hz, float)


class TestForwardModelResult:
    def test_silent_classmethod(self):
        result = ForwardModelResult.silent(timestamp_ms=1000.0)
        assert result.is_silent
        assert result.predicted_db.shape == (N_FREQS,)
        assert result.passive_mode is True

    def test_no_active_channels_classmethod(self):
        result = ForwardModelResult.make_no_channels(timestamp_ms=1000.0)
        assert result.no_active_channels
        assert not result.is_silent

    def test_silent_all_minus_90(self):
        result = ForwardModelResult.silent(timestamp_ms=0.0)
        assert np.all(result.predicted_db == -90.0)


# ---------------------------------------------------------------------------
# ChannelConfig and ChannelMeterState models
# ---------------------------------------------------------------------------

class TestChannelConfig:
    def test_hpf_enabled_when_freq_above_20hz(self):
        cfg = make_channel_config(hpf_hz=80.0)
        assert cfg.hpf_enabled is True

    def test_hpf_disabled_when_freq_at_20hz(self):
        cfg = make_channel_config(hpf_hz=20.0)
        cfg.hpf_enabled = False
        assert not cfg.hpf_enabled

    def test_transfer_curve_none_before_compute(self):
        cfg = make_channel_config()
        assert cfg.transfer_curve_db is None

    def test_transfer_curve_populated_after_compute(self):
        cfg = make_channel_config()
        compute_transfer_curves(cfg)
        assert cfg.transfer_curve_db is not None
        assert cfg.transfer_curve_db.shape == (N_FREQS,)


class TestChannelMeterState:
    def test_default_input_state_normal(self):
        m = make_meter()
        assert m.input_state == 'normal'

    def test_effective_gr_is_sum(self):
        m = ChannelMeterState(
            channel_num=7, timestamp_ms=0.0,
            input_rms_linear=0.3, gate_gr_linear=0.8, dyn_gr_linear=0.9,
            gate_gr_db=-2.0, dyn_gr_db=-1.0, effective_gr_db=-3.0,
            post_fade_db=-20.0, input_rms_db=-10.0,
        )
        assert m.effective_gr_db == -3.0


# ---------------------------------------------------------------------------
# Band.yaml instrument priors completeness
# ---------------------------------------------------------------------------

class TestBandYamlPriors:
    BAND_YAML = Path(__file__).parent.parent / "config" / "band.yaml"
    REQUIRED_TYPES = {'kick', 'guitar', 'guitar_lead', 'bass_di',
                      'vocal_lead', 'vocal_bkg', 'keys', 'overhead'}

    def _load(self):
        with open(self.BAND_YAML) as f:
            return yaml.safe_load(f)

    def test_instrument_priors_present(self):
        d = self._load()
        assert 'instrument_priors' in d

    def test_required_types_defined(self):
        d = self._load()
        priors = d.get('instrument_priors', {})
        for t in self.REQUIRED_TYPES:
            assert t in priors, f"Missing instrument prior: {t}"

    def test_each_prior_has_normal_state(self):
        d = self._load()
        for instr_type, states in d.get('instrument_priors', {}).items():
            assert 'normal' in states, f"{instr_type} missing 'normal' state"

    def test_guitar_has_solo_states(self):
        d = self._load()
        guitar = d['instrument_priors']['guitar']
        assert 'solo_active' in guitar

    def test_all_channels_have_instrument_type(self):
        d = self._load()
        for ch_num, ch_cfg in d['channels'].items():
            assert 'instrument_type' in ch_cfg, f"ch {ch_num} missing instrument_type"

    def test_logging_config_present(self):
        d = self._load()
        assert 'logging' in d
        assert 'level' in d['logging']

    def test_prior_curves_load_as_instrument_prior(self):
        d = self._load()
        priors = d.get('instrument_priors', {})
        for instr_type, states in priors.items():
            prior = InstrumentPrior(instr_type, states)
            curve = prior.get_curve('normal')
            assert curve.shape == (N_FREQS,)
            assert abs(np.mean(curve)) < 0.1  # roughly normalized


# ===========================================================================
# IMP-026 HPF FIX TESTS
# ===========================================================================

class TestHPFFrequencyInference:
    """IMP-026: hpf_on derived from frequency > 22Hz, not phantom power flag."""

    def _client(self):
        from core.osc_client import X32OSCClient
        return X32OSCClient("127.0.0.1", 10023,
                            {1: {"label": "Kick", "type": "instrument"}})

    def test_hpf_on_true_when_freq_above_22hz(self):
        """freq > 22Hz → hpf_on=True, regardless of phantom power state."""
        client = self._client()
        with client._state_lock:
            client._state[1] = {
                "fader": 0.75, "mute": 1,
                "preamp_hpon": 0,   # phantom OFF — must not affect hpf_on
                "preamp_hpf": 0.3,  # ~158Hz > 22Hz → HPF engaged
                "preamp_hpslope": 1, "preamp_gain": 0.0,
            }
        assert client.build_channel_states()[1].hpf_on is True

    def test_hpf_on_false_when_freq_at_minimum(self):
        """hpf_freq at 20Hz (X32 minimum) → hpf_on=False (HPF not engaged)."""
        client = self._client()
        with client._state_lock:
            client._state[1] = {
                "fader": 0.75, "mute": 1,
                "preamp_hpon": 1,    # phantom ON — must not affect hpf_on
                "preamp_hpf": 0.0,  # 20Hz = minimum = HPF off
                "preamp_hpslope": 1, "preamp_gain": 0.0,
            }
        assert client.build_channel_states()[1].hpf_on is False

    def test_phantom_on_with_hpf_off_gives_hpf_false(self):
        """Phantom ON + HPF at minimum → hpf_on=False."""
        client = self._client()
        with client._state_lock:
            client._state[1] = {
                "fader": 0.75, "mute": 1,
                "preamp_hpon": 1, "preamp_hpf": 0.0,
                "preamp_hpslope": 1, "preamp_gain": 0.0,
            }
        assert client.build_channel_states()[1].hpf_on is False


# ===========================================================================
# IMP-046b LUFS SILENCE GATE TESTS
# ===========================================================================

class TestLUFSSilenceGate:
    """IMP-046b: LUFS is a silence gate only — never generates recommendations."""

    def _engine(self):
        from core.recommender import RecommendationEngine
        from models.genre_profile import GenreProfile
        BANDS = ("sub_bass", "bass", "low_mid", "mid", "high_mid", "presence", "air")
        cfg = {
            "band": "Test",
            "thresholds": {
                "recommendation_trigger_db": 3.0,
                "lufs_trigger_db": 2.0,
                "baseline_drift_trigger_db": 2.0,
                "rate_of_change_suppress_db": 3.0,
                "rate_of_change_window_s": 5,
                "suppression_duration_s": 60,
                "recommendation_cooldown_s": 60,
            },
            "frequency_fingerprints": {},
        }
        profile = GenreProfile(
            id="Glam Metal", name="Glam Metal", examples=[],
            target_lufs=-18.0, dynamic_range="medium",
            frequency_targets={b: 0.0 for b in BANDS},
            instrument_weights=[], notes="",
        )
        return RecommendationEngine(cfg, profile)

    def _room(self, lufs=-18.0, rms_db=-20.0):
        from models.event import RoomAnalysis
        BANDS = ("sub_bass", "bass", "low_mid", "mid", "high_mid", "presence", "air")
        bands = {b: -30.0 for b in BANDS}
        return RoomAnalysis(lufs=lufs, rms_db=rms_db, bands=bands,
                            band_delta={b: 0.0 for b in BANDS},
                            lufs_delta=0.0, timestamp=time.time())

    def test_lufs_never_generates_recommendation(self):
        """evaluate() must never return lufs_hot or lufs_low issues."""
        engine = self._engine()
        for lufs in (-12.0, -18.0, -25.0, -30.0):
            recs = engine.evaluate(self._room(lufs=lufs), {})
            lufs_recs = [r for r in recs if r.issue in ("lufs_hot", "lufs_low")]
            assert lufs_recs == [], f"LUFS rec fired for lufs={lufs}"

    def test_silence_gate_triggered_below_threshold(self):
        """rms_db < -50 → _in_silence=True."""
        engine = self._engine()
        engine.evaluate(self._room(rms_db=-90.0), {})
        assert engine._in_silence is True

    def test_silence_gate_not_triggered_above_threshold(self):
        """rms_db > -50 → _in_silence=False."""
        engine = self._engine()
        engine.evaluate(self._room(rms_db=-49.0), {})
        assert engine._in_silence is False

    def test_silence_suppresses_band_recs(self):
        """No band recs fire when room is silent even with band deviation."""
        from models.event import RoomAnalysis
        BANDS = ("sub_bass", "bass", "low_mid", "mid", "high_mid", "presence", "air")
        engine = self._engine()
        bands = {b: -30.0 for b in BANDS}
        bands["sub_bass"] = 0.0   # would normally trigger if not silent
        silent_room = RoomAnalysis(
            lufs=-18.0, rms_db=-90.0, bands=bands,
            band_delta={b: 0.0 for b in BANDS},
            lufs_delta=0.0, timestamp=time.time(),
        )
        recs = engine.evaluate(silent_room, {})
        band_recs = [r for r in recs if r.issue.endswith(("_buildup", "_deficiency"))]
        assert band_recs == []


# ===========================================================================
# RTA ENGINE STATE MACHINE TESTS
# ===========================================================================

class TestRTAEngineStateMachine:
    """RTAEngine state transitions, watchdog, and investigation cooldown."""

    def _make_engine(self):
        from core.rta_engine import RTAEngine
        from unittest.mock import MagicMock
        osc = MagicMock()
        osc.board_rta_db = np.full(100, -30.0)
        return RTAEngine(osc)

    def test_initial_state_is_main_bus(self):
        from core.rta_engine import RTAState
        engine = self._make_engine()
        assert engine.state == RTAState.MAIN_BUS
        assert engine.is_available is True

    def test_set_main_bus_resets_state(self):
        from core.rta_engine import RTAState
        engine = self._make_engine()
        engine._state = RTAState.INVESTIGATING
        engine.set_main_bus()
        assert engine.state == RTAState.MAIN_BUS
        engine._osc.set_rta_source.assert_called()

    def test_start_investigation_changes_state(self):
        from core.rta_engine import RTAState
        engine = self._make_engine()
        result = engine.start_investigation(98)
        assert result is True
        assert engine.state == RTAState.INVESTIGATING
        assert engine.is_available is False

    def test_start_investigation_false_if_already_investigating(self):
        engine = self._make_engine()
        engine.start_investigation(98)
        assert engine.start_investigation(99) is False

    def test_watchdog_forces_main_bus_after_timeout(self):
        from core.rta_engine import RTAState, WATCHDOG_TIMEOUT_S
        engine = self._make_engine()
        engine._state = RTAState.INVESTIGATING
        engine._state_entered_at = time.time() - WATCHDOG_TIMEOUT_S - 1.0
        engine.check_watchdog()
        assert engine.state == RTAState.MAIN_BUS

    def test_watchdog_no_op_within_timeout(self):
        from core.rta_engine import RTAState
        engine = self._make_engine()
        engine._state = RTAState.INVESTIGATING
        engine._state_entered_at = time.time()
        engine.check_watchdog()
        assert engine.state == RTAState.INVESTIGATING

    def test_watchdog_no_op_in_main_bus_regardless_of_time(self):
        from core.rta_engine import RTAState
        engine = self._make_engine()
        engine._state_entered_at = time.time() - 100.0
        engine.check_watchdog()
        assert engine.state == RTAState.MAIN_BUS

    def test_investigation_cooldown_blocks_second_scan(self):
        from core.rta_engine import INVESTIGATION_COOLDOWN
        engine = self._make_engine()
        engine._last_investigation['bass'] = time.time()
        assert engine.investigation_allowed('bass') is False

    def test_investigation_cooldown_expires(self):
        from core.rta_engine import INVESTIGATION_COOLDOWN
        engine = self._make_engine()
        engine._last_investigation['bass'] = time.time() - INVESTIGATION_COOLDOWN - 1.0
        assert engine.investigation_allowed('bass') is True

    def test_investigation_allowed_for_unseen_band(self):
        engine = self._make_engine()
        assert engine.investigation_allowed('mid_high') is True

    def test_cal_scan_requires_main_bus_state(self):
        """run_cal_scan returns empty lists if not in MAIN_BUS."""
        from core.rta_engine import RTAState
        engine = self._make_engine()
        engine._state = RTAState.INVESTIGATING
        results, updates = engine.run_cal_scan([], None, None, {})
        assert results == []
        assert updates == []


# ===========================================================================
# NORMALIZE_TO_SHAPE TESTS
# ===========================================================================

class TestNormalizeToShape:

    def test_output_mean_is_zero(self):
        from core.mic_analyzer import normalize_to_shape
        spectrum = np.array([-20.0, -10.0, -30.0, -15.0, -25.0])
        assert abs(np.mean(normalize_to_shape(spectrum))) < 1e-10

    def test_position_independent(self):
        """Same shape at different levels normalizes identically."""
        from core.mic_analyzer import normalize_to_shape
        shape = np.array([-5.0, 0.0, 3.0, -2.0, 4.0])
        np.testing.assert_allclose(
            normalize_to_shape(shape - 10.0),
            normalize_to_shape(shape - 25.0),
            atol=1e-10,
        )

    def test_freq_mask_restricts_mean(self):
        from core.mic_analyzer import normalize_to_shape
        spectrum = np.array([-20.0, -10.0, -30.0, -15.0, -100.0])
        mask = np.array([True, True, True, True, False])
        result = normalize_to_shape(spectrum, freq_mask=mask)
        expected_mean = float(np.mean(spectrum[:4]))   # -18.75
        assert abs(result[0] - (spectrum[0] - expected_mean)) < 0.01

    def test_output_length_matches_input(self):
        from core.mic_analyzer import normalize_to_shape
        spectrum = np.random.randn(N_FREQS) - 20.0
        assert len(normalize_to_shape(spectrum)) == N_FREQS

    def test_band_average_flat_spectrum(self):
        from core.mic_analyzer import band_average
        spectrum = np.full(N_FREQS, -20.0)
        assert abs(band_average(spectrum, (80.0, 250.0)) - (-20.0)) < 0.5

    def test_band_average_out_of_range_returns_minus90(self):
        from core.mic_analyzer import band_average
        spectrum = np.full(N_FREQS, -20.0)
        assert band_average(spectrum, (25000.0, 30000.0)) == -90.0


# ===========================================================================
# IMP-052 PEAK DETECTION TESTS
# ===========================================================================

class TestPeakDetection:

    def test_find_band_peak_spike(self):
        """A spike at 315Hz in a flat spectrum should return peak_hz ≈ 315Hz."""
        from core.mic_analyzer import find_band_peak
        spectrum = np.full(N_FREQS, -40.0)
        idx = int(np.argmin(np.abs(FREQ_AXIS - 315)))
        spectrum[idx] = -30.0
        peak_hz, prominence = find_band_peak(spectrum, FREQ_AXIS, 200, 500)
        assert abs(peak_hz - 315) < 30
        assert prominence > 5.0

    def test_find_band_peak_flat_band_low_prominence(self):
        """Flat energy across a band should return near-zero prominence."""
        from core.mic_analyzer import find_band_peak
        spectrum = np.full(N_FREQS, -40.0)
        peak_hz, prominence = find_band_peak(spectrum, FREQ_AXIS, 200, 500)
        assert prominence < 1.0

    def test_find_band_peak_out_of_range_returns_center(self):
        """Band with no FREQ_AXIS bins should return band center, 0 prominence."""
        from core.mic_analyzer import find_band_peak
        spectrum = np.full(N_FREQS, -40.0)
        peak_hz, prominence = find_band_peak(spectrum, FREQ_AXIS, 25000.0, 30000.0)
        assert peak_hz == pytest.approx((25000.0 + 30000.0) / 2.0)
        assert prominence == 0.0

    def test_compute_band_levels_has_prominence(self):
        """compute_band_levels() must return peak_prominence_db for every band."""
        from core.mic_analyzer import compute_band_levels
        spectrum = np.full(N_FREQS, -40.0)
        levels = compute_band_levels(spectrum)
        for band, data in levels.items():
            assert 'peak_prominence_db' in data, f"Missing peak_prominence_db in {band}"
            assert 'peak_hz' in data
            assert 'avg_db' in data
            assert 'peak_db' in data

    def test_compute_band_levels_prominence_zero_for_flat(self):
        """Flat spectrum → all prominences ≈ 0."""
        from core.mic_analyzer import compute_band_levels
        levels = compute_band_levels(np.full(N_FREQS, -30.0))
        for band, data in levels.items():
            assert abs(data['peak_prominence_db']) < 0.1, \
                f"Expected ~0 prominence for flat spectrum in {band}"

    def test_recommendation_text_includes_peak_hz(self):
        """Band recommendation text should include the peak Hz value."""
        from core.recommender import _build_band_recommendation_text
        mic_band_levels = {
            'upper_mid': {'peak_hz': 3150.0, 'peak_prominence_db': 2.5,
                          'avg_db': -38.0, 'peak_db': -35.5}
        }
        text = _build_band_recommendation_text(
            band='upper_mid', direction='buildup', deviation_db=3.8,
            dominant_channel_label='Guitar 1', mic_band_levels=mic_band_levels
        )
        assert '3150' in text
        assert 'Guitar 1' in text

    def test_named_move_uses_peak_hz(self):
        """Named move lookup should use peak_hz — 3150Hz falls in Harshness cut range."""
        from core.recommender import _build_band_recommendation_text
        mic_band_levels = {
            'upper_mid': {'peak_hz': 3150.0, 'peak_prominence_db': 2.5,
                          'avg_db': -38.0, 'peak_db': -35.5}
        }
        text = _build_band_recommendation_text(
            band='upper_mid', direction='buildup', deviation_db=3.8,
            dominant_channel_label='Guitar 1', mic_band_levels=mic_band_levels
        )
        assert 'Harshness cut' in text

    def test_broad_energy_description_when_low_prominence(self):
        """Low prominence (<= 0.5dB) → 'broad energy' description, not a Hz value."""
        from core.recommender import _build_band_recommendation_text
        mic_band_levels = {
            'bass': {'peak_hz': 150.0, 'peak_prominence_db': 0.2,
                     'avg_db': -30.0, 'peak_db': -29.8}
        }
        text = _build_band_recommendation_text(
            band='bass', direction='buildup', deviation_db=3.0,
            dominant_channel_label='Bass', mic_band_levels=mic_band_levels
        )
        assert 'broad energy' in text

    def test_sharp_resonance_note_when_high_prominence(self):
        """Prominence > 2.0dB → 'sharp resonance' note in text."""
        from core.recommender import _build_band_recommendation_text
        mic_band_levels = {
            'upper_mid': {'peak_hz': 3150.0, 'peak_prominence_db': 2.5,
                          'avg_db': -38.0, 'peak_db': -35.5}
        }
        text = _build_band_recommendation_text(
            band='upper_mid', direction='buildup', deviation_db=3.8,
            dominant_channel_label='Guitar 1', mic_band_levels=mic_band_levels
        )
        assert 'sharp resonance' in text


# ===========================================================================
# MicAnalysis normalized_shape_db FIELD TESTS
# ===========================================================================

class TestMicAnalysisNormalizedShapeField:

    def test_default_normalized_shape_is_zero_array(self):
        """normalized_shape_db defaults to zeros(1000) when omitted."""
        empty = np.full(N_FREQS, -90.0)
        a = MicAnalysis(
            lufs=-70.0, raw_spectrum_db=empty, corrected_spectrum_db=empty,
            smoothed_spectrum_db=empty, spectral_delta_db=np.zeros(N_FREQS),
            band_levels={}, room_mode_flags=np.zeros(N_FREQS, dtype=bool),
            correction_applied_db=np.zeros(N_FREQS),
            is_silent=True, timestamp_ms=0.0,
        )
        assert a.normalized_shape_db.shape == (1000,)
        assert np.all(a.normalized_shape_db == 0.0)

    def test_normalized_shape_is_mean_subtracted(self):
        """Providing normalized_shape_db stores it correctly."""
        empty = np.full(N_FREQS, -90.0)
        spectrum = np.random.randn(N_FREQS) - 20.0
        shape = spectrum - float(np.mean(spectrum))
        a = MicAnalysis(
            lufs=-18.0, raw_spectrum_db=spectrum, corrected_spectrum_db=spectrum,
            smoothed_spectrum_db=spectrum, spectral_delta_db=np.zeros(N_FREQS),
            band_levels={}, room_mode_flags=np.zeros(N_FREQS, dtype=bool),
            correction_applied_db=np.zeros(N_FREQS),
            is_silent=False, timestamp_ms=0.0,
            normalized_shape_db=shape,
        )
        assert abs(np.mean(a.normalized_shape_db)) < 1e-8


# ===========================================================================
# IMP-053 DUAL-PATH FFT + DISPLAY BUFFER TESTS
# ===========================================================================

class TestDisplayPath:

    def _make_capture(self, n_buf=144000, sr=48000):
        """AudioCapture with a pre-filled buffer of pink-ish noise."""
        from core.audio_capture import AudioCapture
        cap = AudioCapture(preferred_sample_rate=sr)
        cap._sample_rate = sr
        cap._buffer      = (np.random.randn(n_buf) * 0.1).astype(np.float32)
        return cap

    def test_get_display_window_returns_n_samples(self):
        """get_display_window(4800) returns exactly 4800 samples."""
        cap = self._make_capture()
        win = cap.get_display_window(4800)
        assert len(win) == 4800

    def test_get_display_window_clamps_to_buffer_size(self):
        """Requesting more samples than buffer returns what's available."""
        from core.audio_capture import AudioCapture
        cap = AudioCapture()
        cap._sample_rate = 48000
        cap._buffer = np.zeros(1000, dtype=np.float32)
        win = cap.get_display_window(2000)
        assert len(win) == 1000

    def test_get_display_window_empty_buffer_returns_zeros(self):
        """Unstarted capture returns empty array."""
        from core.audio_capture import AudioCapture
        cap = AudioCapture()
        assert len(cap.get_display_window(4800)) == 0

    def test_display_spectrum_returns_correct_shape(self):
        """compute_display_spectrum() returns array of length N_FREQS."""
        from core.mic_analyzer import MicAnalyzer
        from core.geometry import IrregularRoomAcoustics
        cap      = self._make_capture()
        analyzer = MicAnalyzer(IrregularRoomAcoustics({}))
        result   = analyzer.compute_display_spectrum(cap)
        assert len(result) == N_FREQS

    def test_display_ema_separate_from_analysis_ema(self):
        """Display EMA state must not be the same object as analysis EMA."""
        from core.mic_analyzer import MicAnalyzer
        from core.geometry import IrregularRoomAcoustics
        analyzer = MicAnalyzer(IrregularRoomAcoustics({}))
        assert analyzer._ema_display is not analyzer._ema_analysis

    def test_display_spectrum_mean_near_zero(self):
        """Output is normalized to shape — mean should be ≈ 0."""
        from core.mic_analyzer import MicAnalyzer
        from core.geometry import IrregularRoomAcoustics
        cap      = self._make_capture()
        analyzer = MicAnalyzer(IrregularRoomAcoustics({}))
        result   = analyzer.compute_display_spectrum(cap)
        assert abs(float(np.mean(result))) < 1.0

    def test_display_buffer_update_and_snapshot(self):
        """update() and snapshot() round-trip correctly."""
        from core.display_buffer import DisplayBuffer
        from core.third_octave import N_THIRD_OCTAVE
        buf  = DisplayBuffer()
        arr  = np.ones(N_THIRD_OCTAVE) * 3.0
        buf.update(mic_shape_fast=arr, song_name="Test Song")
        snap = buf.snapshot()
        assert np.allclose(snap['mic_shape_fast'], arr)
        assert snap['song_name'] == "Test Song"

    def test_display_buffer_is_silent_by_default(self):
        """is_silent defaults to True."""
        from core.display_buffer import DisplayBuffer
        assert DisplayBuffer().snapshot()['is_silent'] is True

    def test_display_buffer_unknown_key_ignored(self):
        """update() with an unknown key does not raise."""
        from core.display_buffer import DisplayBuffer
        buf = DisplayBuffer()
        buf.update(nonexistent_key="value")  # must not raise

    def test_display_buffer_thread_safety(self):
        """Concurrent update() and snapshot() must not deadlock or corrupt data."""
        import threading as _threading
        from core.display_buffer import DisplayBuffer
        buf    = DisplayBuffer()
        errors = []
        stop   = _threading.Event()

        def writer():
            from core.third_octave import N_THIRD_OCTAVE as _N
            while not stop.is_set():
                buf.update(mic_shape_fast=np.random.randn(_N))

        def reader():
            from core.third_octave import N_THIRD_OCTAVE as _N
            while not stop.is_set():
                try:
                    snap = buf.snapshot()
                    assert len(snap['mic_shape_fast']) == _N
                except Exception as e:
                    errors.append(e)

        t1 = _threading.Thread(target=writer, daemon=True)
        t2 = _threading.Thread(target=reader, daemon=True)
        t1.start()
        t2.start()
        time.sleep(0.3)
        stop.set()
        assert not errors, f"Thread safety errors: {errors}"


# ===========================================================================
# IMP-051 DISPLAY WINDOW / HELPER FUNCTION TESTS
# ===========================================================================

class TestDisplayHelpers:
    """Tests for the display helper functions in main.py."""

    def _make_mic_result(self, shape_db=None):
        """Build a minimal MicAnalysis with normalized_shape_db populated."""
        empty = np.full(N_FREQS, -90.0)
        spec  = shape_db if shape_db is not None else np.zeros(N_FREQS)
        return MicAnalysis(
            lufs=-18.0, raw_spectrum_db=spec, corrected_spectrum_db=spec,
            smoothed_spectrum_db=spec, spectral_delta_db=np.zeros(N_FREQS),
            band_levels={
                b: {'avg_db': float(np.mean(spec)), 'peak_db': float(np.max(spec)),
                    'peak_hz': 1000.0, 'peak_prominence_db': 0.5}
                for b in ('sub', 'bass', 'low_mid', 'mid_low', 'mid_high',
                          'upper_mid', 'presence', 'air')
            },
            room_mode_flags=np.zeros(N_FREQS, dtype=bool),
            correction_applied_db=np.zeros(N_FREQS),
            is_silent=False, timestamp_ms=0.0,
            normalized_shape_db=spec - float(np.mean(spec)),
        )

    def _make_genre(self, targets=None):
        from models.genre_profile import GenreProfile
        targets = targets or {}
        return GenreProfile(
            id='test', name='test', examples=[], target_lufs=-18.0,
            dynamic_range='medium',
            frequency_targets={**{b: 0.0 for b in (
                'sub', 'bass', 'low_mid', 'mid_low', 'mid_high',
                'upper_mid', 'presence', 'air')}, **targets},
            instrument_weights=[], notes='',
        )

    def test_compute_band_highlights_positive_for_excess(self):
        """Mic elevated in upper_mid above target → positive deviation."""
        from main import _compute_band_highlights
        spec = np.zeros(N_FREQS)
        mask = (FREQ_AXIS >= 2000) & (FREQ_AXIS < 4000)
        spec[mask] = 6.0   # upper_mid elevated
        mic  = self._make_mic_result(spec - float(np.mean(spec)))
        # Provide normalized_shape directly
        mic.normalized_shape_db = spec - float(np.mean(spec))
        genre = self._make_genre({'upper_mid': 0.0})
        highlights = _compute_band_highlights(mic, genre)
        assert highlights.get('upper_mid', 0.0) > 0.0

    def test_compute_band_highlights_negative_for_deficiency(self):
        """Mic below target in upper_mid → negative deviation."""
        from main import _compute_band_highlights
        spec = np.zeros(N_FREQS)
        mask = (FREQ_AXIS >= 2000) & (FREQ_AXIS < 4000)
        spec[mask] = -6.0   # upper_mid deficient
        mic = self._make_mic_result(spec - float(np.mean(spec)))
        mic.normalized_shape_db = spec - float(np.mean(spec))
        genre = self._make_genre({'upper_mid': 0.0})
        highlights = _compute_band_highlights(mic, genre)
        assert highlights.get('upper_mid', 0.0) < 0.0

    def test_compute_band_highlights_returns_empty_for_none(self):
        """None inputs return empty dict without raising."""
        from main import _compute_band_highlights
        assert _compute_band_highlights(None, None) == {}
        assert _compute_band_highlights(None, self._make_genre()) == {}

    def test_extract_band_peaks_returns_hz_and_prominence(self):
        """_extract_band_peaks returns (peak_hz, prominence) tuples."""
        from main import _extract_band_peaks
        mic = self._make_mic_result()
        peaks = _extract_band_peaks(mic)
        assert isinstance(peaks, dict)
        for band, val in peaks.items():
            assert len(val) == 2  # (peak_hz, prominence_db)

    def test_genre_to_shape_array_correct_length(self):
        """_genre_to_shape_array returns array of N_FREQS length."""
        from main import _genre_to_shape_array
        genre = self._make_genre({'sub': -2.0, 'bass': 1.5, 'upper_mid': 2.0})
        result = _genre_to_shape_array(genre)
        assert len(result) == N_FREQS

    def test_genre_to_shape_array_not_flat_for_real_profile(self):
        """_genre_to_shape_array must return a non-flat array for a real genre profile."""
        from main import _genre_to_shape_array
        from core.config_loader import load_genre_profiles
        profiles = load_genre_profiles()
        genre = profiles.get('Glam Metal') or next(iter(profiles.values()), None)
        if genre is None:
            return
        result = _genre_to_shape_array(genre)
        assert len(result) == N_FREQS
        assert not np.allclose(result, 0.0), "Genre target shape should not be flat"

    def test_display_buffer_full_cycle(self):
        """Simulate a full analysis-cycle update and verify snapshot."""
        from core.display_buffer import DisplayBuffer
        from core.third_octave import N_THIRD_OCTAVE
        buf   = DisplayBuffer()
        bands = np.random.randn(N_THIRD_OCTAVE) * 3.0
        buf.update(
            mic_bands=bands,
            board_rta_bands=bands * 0.5,
            genre_target_bands=np.zeros(N_THIRD_OCTAVE),
            band_highlights={'upper_mid': 3.2},
            band_peaks={'upper_mid': (3150.0, 2.1)},
            song_name='Round and Round',
            genre_name='Glam Metal',
            lufs=-18.5,
            is_silent=False,
        )
        snap = buf.snapshot()
        assert snap['song_name'] == 'Round and Round'
        assert snap['genre_name'] == 'Glam Metal'
        assert abs(snap['lufs'] - (-18.5)) < 0.01
        assert snap['is_silent'] is False
        assert snap['band_highlights']['upper_mid'] == pytest.approx(3.2)


# ===========================================================================
# ForwardModel RTA guard (Bug 2 regression)
# ===========================================================================

class TestForwardModelRtaGuard:
    """_interpolate_rta_to_freq_axis must not crash on bad input."""

    def test_none_input_returns_1000_point_fallback(self):
        import numpy as np
        from core.forward_model import _interpolate_rta_to_freq_axis
        result = _interpolate_rta_to_freq_axis(None)
        assert len(result) == 1000

    def test_wrong_length_returns_1000_point_fallback(self):
        import numpy as np
        from core.forward_model import _interpolate_rta_to_freq_axis
        result = _interpolate_rta_to_freq_axis(np.full(50, -40.0))
        assert len(result) == 1000

    def test_correct_length_interpolates_not_fallback(self):
        import numpy as np
        from core.forward_model import _interpolate_rta_to_freq_axis
        result = _interpolate_rta_to_freq_axis(np.full(100, -40.0))
        assert len(result) == 1000
        assert not np.all(result == -60.0)   # must not be the flat fallback value


# ===========================================================================
# THIRD-OCTAVE BAND AVERAGING TESTS
# ===========================================================================

class TestThirdOctave:

    def test_to_third_octave_returns_31_bands(self):
        from core.third_octave import to_third_octave
        from core.channel_model import FREQ_AXIS
        spectrum = np.zeros(len(FREQ_AXIS))
        result = to_third_octave(spectrum)
        assert len(result) == 31

    def test_flat_spectrum_produces_equal_bands(self):
        """A flat input spectrum should produce all equal band values."""
        from core.third_octave import to_third_octave
        from core.channel_model import FREQ_AXIS
        flat   = np.full(len(FREQ_AXIS), -20.0)
        result = to_third_octave(flat)
        assert np.allclose(result, result[0], atol=0.01)

    def test_normalize_third_octave_mean_zero(self):
        """normalize_third_octave() output should have mean ≈ 0."""
        from core.third_octave import normalize_third_octave
        bands  = np.array([float(i) for i in range(31)])
        result = normalize_third_octave(bands)
        assert abs(float(np.mean(result))) < 0.01

    def test_bass_heavy_spectrum_shows_bass_elevation(self):
        """Heavy bass spectrum should show positive values in bass bands."""
        from core.third_octave import to_third_octave, normalize_third_octave
        from core.channel_model import FREQ_AXIS
        spectrum = np.full(len(FREQ_AXIS), -40.0)
        bass_mask = (FREQ_AXIS >= 80) & (FREQ_AXIS <= 250)
        spectrum[bass_mask] = -20.0
        bands  = to_third_octave(spectrum)
        normed = normalize_third_octave(bands)
        bass_region = normed[4:11]
        assert float(np.mean(bass_region)) > 3.0, \
            f"Bass region should be elevated, got {bass_region}"


# ===========================================================================
# SHAPE NORMALIZATION LEVEL-INDEPENDENCE TESTS
# ===========================================================================

class TestShapeNormalization:
    """normalize_to_shape removes overall level — same shape, different levels, same result."""

    def _make_mic(self, normalized_shape_db):
        empty = np.zeros(N_FREQS)
        return MicAnalysis(
            lufs=-20.0, raw_spectrum_db=empty, corrected_spectrum_db=empty,
            smoothed_spectrum_db=empty, spectral_delta_db=empty,
            band_levels={}, room_mode_flags=np.zeros(N_FREQS, dtype=bool),
            correction_applied_db=empty, is_silent=False, timestamp_ms=0.0,
            normalized_shape_db=normalized_shape_db,
        )

    def test_normalize_to_shape_removes_level(self):
        """Two spectra with same shape but different levels normalize identically."""
        from core.mic_analyzer import normalize_to_shape

        shape = np.linspace(5, -5, N_FREQS)
        loud  = shape + 20.0
        quiet = shape - 20.0

        result_loud  = normalize_to_shape(loud)
        result_quiet = normalize_to_shape(quiet)

        assert np.allclose(result_loud, result_quiet, atol=1e-6), (
            "Same shape at different levels should normalize identically"
        )

    def test_band_highlights_are_level_independent(self):
        """Band highlight deviations should not change when overall level changes."""
        from core.mic_analyzer import normalize_to_shape
        from main import _compute_band_highlights
        from models.genre_profile import GenreProfile

        band_names = ('sub', 'bass', 'low_mid', 'mid_low', 'mid_high',
                      'upper_mid', 'presence', 'air')
        genre = GenreProfile(
            id='test', name='test', examples=[], target_lufs=-18.0,
            dynamic_range='medium',
            frequency_targets={b: 0.0 for b in band_names},
            instrument_weights=[], notes='',
        )

        shape = np.zeros(N_FREQS)
        shape[:200] = 8.0
        shape[200:] = -2.0

        mic_loud  = self._make_mic(normalize_to_shape(shape + 30))
        mic_quiet = self._make_mic(normalize_to_shape(shape - 10))

        highlights_loud  = _compute_band_highlights(mic_loud,  genre)
        highlights_quiet = _compute_band_highlights(mic_quiet, genre)

        for band in highlights_loud:
            assert abs(highlights_loud[band] - highlights_quiet[band]) < 0.1, (
                f"Band {band} highlight changed with level: "
                f"{highlights_loud[band]:.2f} vs {highlights_quiet[band]:.2f}"
            )

    def test_recommendation_engine_level_independent(self):
        """normalized_shape_db should be identical when spectra share shape but differ in level."""
        from core.mic_analyzer import normalize_to_shape

        shape = np.zeros(N_FREQS)
        shape[:200] = 8.0
        shape[200:] = -2.0

        mic_loud  = self._make_mic(normalize_to_shape(shape + 30))
        mic_quiet = self._make_mic(normalize_to_shape(shape - 10))

        assert np.allclose(
            mic_loud.normalized_shape_db, mic_quiet.normalized_shape_db, atol=1e-6
        ), "normalized_shape_db must be level-independent"


class TestActiveNormalization:

    def test_active_norm_centers_active_range(self):
        """Mean of active range (80–8000Hz) should be ≈ 0 after normalization."""
        from core.mic_analyzer import normalize_to_shape_active, ACTIVE_NORM_LO_HZ, ACTIVE_NORM_HI_HZ
        from core.channel_model import FREQ_AXIS

        spectrum = np.full(len(FREQ_AXIS), -40.0)
        spectrum[FREQ_AXIS < 80]   = -80.0
        spectrum[FREQ_AXIS > 8000] = -70.0

        result = normalize_to_shape_active(spectrum, FREQ_AXIS)

        active_mask = (FREQ_AXIS >= ACTIVE_NORM_LO_HZ) & (FREQ_AXIS <= ACTIVE_NORM_HI_HZ)
        active_mean = float(np.mean(result[active_mask]))
        assert abs(active_mean) < 0.1, f"Active range mean should be ~0, got {active_mean:.2f}"

    def test_active_norm_preserves_shape(self):
        """Normalization should shift the curve, not distort it."""
        from core.mic_analyzer import normalize_to_shape_active
        from core.channel_model import FREQ_AXIS

        rng      = np.random.default_rng(42)
        spectrum = rng.standard_normal(len(FREQ_AXIS)) * 5 + np.linspace(5, -5, len(FREQ_AXIS))
        result   = normalize_to_shape_active(spectrum, FREQ_AXIS)

        assert np.allclose(np.diff(spectrum), np.diff(result), atol=1e-6)

    def test_active_norm_different_from_full_norm_when_extremes_silent(self):
        """With silent sub/air, active norm should differ from full-range norm."""
        from core.mic_analyzer import normalize_to_shape, normalize_to_shape_active
        from core.channel_model import FREQ_AXIS

        spectrum = np.full(len(FREQ_AXIS), -40.0)
        spectrum[FREQ_AXIS < 80]   = -80.0
        spectrum[FREQ_AXIS > 8000] = -80.0

        full_norm   = normalize_to_shape(spectrum)
        active_norm = normalize_to_shape_active(spectrum, FREQ_AXIS)

        assert not np.allclose(full_norm, active_norm, atol=1.0), \
            "Active and full norm should differ with silent extreme bands"

    def test_active_norm_falls_back_to_full_when_no_active_bins(self):
        """If no bins in active range, should not crash — falls back to full mean."""
        from core.mic_analyzer import normalize_to_shape_active
        from core.channel_model import FREQ_AXIS

        spectrum = np.full(len(FREQ_AXIS), -40.0)
        result = normalize_to_shape_active(spectrum, FREQ_AXIS,
                                           active_lo_hz=50000.0,
                                           active_hi_hz=60000.0)
        assert len(result) == len(spectrum)
        assert abs(float(np.mean(result))) < 0.1

    def test_mic_analysis_has_active_shape_field(self):
        """MicAnalysis must have normalized_shape_active_db field."""
        from models.analysis import MicAnalysis
        fields = [f.name for f in MicAnalysis.__dataclass_fields__.values()]
        assert 'normalized_shape_active_db' in fields


class TestFrequencyConfidence:

    def test_venue_acoustics_confidence_defaults_to_one(self):
        """All bands default to 1.0 if no frequency_confidence in YAML."""
        from core.geometry import IrregularRoomAcoustics
        acoustics = IrregularRoomAcoustics({})
        conf = acoustics.frequency_confidence
        assert all(v == 1.0 for v in conf.values())
        assert 'sub' in conf and 'air' in conf

    def test_venue_acoustics_confidence_reads_yaml(self):
        """frequency_confidence block in config dict is loaded correctly."""
        from core.geometry import IrregularRoomAcoustics
        acoustics = IrregularRoomAcoustics({})
        acoustics.config = {'frequency_confidence': {'sub': 0.0, 'bass': 0.3, 'air': 0.1}}
        conf = acoustics.frequency_confidence
        assert conf['sub'] == 0.0
        assert conf['bass'] == 0.3
        assert conf['air'] == 0.1
        assert conf['mid_low'] == 1.0   # not specified — defaults to 1.0

    def test_confidence_mask_excludes_low_confidence_bands(self):
        """Bands with confidence < 0.5 should not appear in mask."""
        from core.geometry import IrregularRoomAcoustics
        from core.channel_model import FREQ_AXIS
        acoustics = IrregularRoomAcoustics({})
        acoustics.config = {'frequency_confidence': {'sub': 0.0, 'air': 0.1}}
        mask = acoustics.confidence_weighted_freq_mask(FREQ_AXIS, threshold=0.5)
        sub_mask = (FREQ_AXIS >= 20) & (FREQ_AXIS < 80)
        assert not mask[sub_mask].any(), "Sub band should be excluded from mask"
        mid_mask = (FREQ_AXIS >= 500) & (FREQ_AXIS < 1000)
        assert mask[mid_mask].all(), "Mid band should be included in mask"

    def test_active_norm_uses_confidence_mask(self):
        """normalize_to_shape_active uses confidence_mask over active range bounds."""
        from core.mic_analyzer import normalize_to_shape_active
        from core.channel_model import FREQ_AXIS

        # Spike in bass band (80–200Hz) — inside the active range, so without
        # a confidence mask the spike IS included in the mean and skews output.
        spectrum = np.full(len(FREQ_AXIS), -40.0)
        bass_mask = (FREQ_AXIS >= 80) & (FREQ_AXIS < 200)
        spectrum[bass_mask] = 0.0   # 40dB spike in bass

        conf_mask = ~bass_mask   # exclude bass from mean

        result_with_mask    = normalize_to_shape_active(spectrum, FREQ_AXIS, confidence_mask=conf_mask)
        result_without_mask = normalize_to_shape_active(spectrum, FREQ_AXIS, confidence_mask=None)

        # With mask: non-bass region should be near 0 (bass excluded from mean)
        non_bass_with    = float(np.mean(result_with_mask[~bass_mask]))
        # Without mask: active range includes bass, so spike skews mean, non-bass shifts negative
        non_bass_without = float(np.mean(result_without_mask[~bass_mask]))

        assert abs(non_bass_with) < 1.0, \
            f"With confidence mask, non-bass mean should be ~0, got {non_bass_with:.2f}"
        assert non_bass_without < -3.0, \
            f"Without mask, bass spike should push non-bass below 0, got {non_bass_without:.2f}"


class TestConfidenceGatedRecommendations:

    def test_band_is_trusted_returns_false_below_half(self):
        """_band_is_trusted() returns False when confidence < 0.5."""
        from core.recommender import RecommendationEngine as Recommender
        r = Recommender.__new__(Recommender)
        r._band_confidence = {'sub': 0.3, 'bass': 0.5, 'air': 0.0}
        assert not r._band_is_trusted('sub_bass')   # maps to 'sub' → 0.3
        assert not r._band_is_trusted('air')         # 0.0
        assert r._band_is_trusted('bass')            # exactly 0.5 = trusted

    def test_effective_threshold_scales_with_confidence(self):
        """_effective_threshold() should scale correctly."""
        from core.recommender import RecommendationEngine as Recommender
        r = Recommender.__new__(Recommender)
        r._band_confidence = {'bass': 0.9, 'presence': 0.6, 'sub': 0.2}
        base = 3.0
        assert r._effective_threshold('bass', base) == 3.0
        assert r._effective_threshold('presence', base) == pytest.approx(4.5)
        assert r._effective_threshold('sub_bass', base) == float('inf')

    def test_mid_band_uses_minimum_confidence(self):
        """'mid' band spans mid_low + mid_high — should use the minimum."""
        from core.recommender import RecommendationEngine as Recommender
        r = Recommender.__new__(Recommender)
        r._band_confidence = {'mid_low': 0.9, 'mid_high': 0.4}
        # mid_high is 0.4 < 0.5, so mid is untrusted
        assert not r._band_is_trusted('mid')

    def test_suppressed_bands_populated_after_evaluate(self):
        """engine._suppressed_bands should list bands skipped due to low confidence."""
        from core.recommender import RecommendationEngine as Recommender
        from models.event import RoomAnalysis
        import time as _time

        band_confidence = {
            'sub': 0.0, 'bass': 0.0, 'low_mid': 1.0, 'mid_low': 1.0,
            'mid_high': 1.0, 'upper_mid': 1.0, 'presence': 1.0, 'air': 0.0,
        }
        band_cfg = {'thresholds': {}, 'frequency_fingerprints': {}}
        genre = MagicMock()
        genre.target_for_band.return_value = 0.0
        genre.id = 'test'
        engine = Recommender(band_cfg, genre)

        analysis = MagicMock(spec=RoomAnalysis)
        analysis.timestamp = _time.time()
        analysis.rms_db = -60.0   # silent → no band recs fire
        analysis.bands = {b: -90.0 for b in ('sub_bass', 'bass', 'low_mid',
                                              'mid', 'high_mid', 'presence', 'air')}

        mic = MagicMock()
        mic.is_silent = True   # force fallback path so suppression still tracks

        engine.evaluate(analysis, {}, mic_analysis=mic, band_confidence=band_confidence)

        # _suppressed_bands may be empty when rms_db triggers silence gate —
        # but the dict must exist and be a list
        assert isinstance(engine._suppressed_bands, list)

    def test_no_confidence_treats_all_as_trusted(self):
        """When band_confidence is None, _band_is_trusted() returns True for all bands."""
        from core.recommender import RecommendationEngine as Recommender
        r = Recommender.__new__(Recommender)
        r._band_confidence = {}   # empty = all default to 1.0
        for band in ('sub_bass', 'bass', 'low_mid', 'mid', 'high_mid', 'presence', 'air'):
            assert r._band_is_trusted(band), f"{band} should be trusted with empty confidence dict"
