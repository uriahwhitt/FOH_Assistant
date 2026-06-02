"""Channel contribution model — EQ transfer functions, instrument priors, input state inference.

FREQ_AXIS and N_FREQS are the shared frequency axis constants. Import from here everywhere.
"""

import math
import time
from typing import Optional

import numpy as np

from models.channel import ChannelConfig, ChannelMeterState, EQBand


# ---------------------------------------------------------------------------
# Shared frequency axis — import this everywhere, never redefine
# ---------------------------------------------------------------------------

N_FREQS = 1000
FREQ_AXIS = np.logspace(np.log10(20.0), np.log10(20000.0), N_FREQS)
SILENCE_THRESHOLD_DB = -50.0


# ---------------------------------------------------------------------------
# Conversion utilities
# ---------------------------------------------------------------------------

def eq_float_to_hz(f: float) -> float:
    """Convert X32 EQ freq float [0.0, 1.0] to Hz [20, 20000] (log scale)."""
    log_min = math.log10(20.0)
    log_max = math.log10(20000.0)
    return 10 ** (log_min + f * (log_max - log_min))


def fader_float_to_db(f: float) -> float:
    """Convert X32 fader float [0.0, 1.0] to dB [-90, +10]. Piecewise linear."""
    if f >= 0.5:
        return f * 40.0 - 30.0
    elif f >= 0.25:
        return f * 80.0 - 50.0
    elif f >= 0.0625:
        return f * 160.0 - 70.0
    elif f > 0.0:
        return f * 480.0 - 90.0
    return -90.0


def hpslope_int_to_db_oct(slope_int: int) -> int:
    """Convert X32 hpslope enum to dB/octave."""
    return {0: 12, 1: 18, 2: 24}.get(slope_int, 12)


def linear_to_dbfs(linear: float) -> float:
    """Convert X32 meter linear float to dBFS."""
    if linear <= 0:
        return -90.0
    return max(-90.0, 20.0 * math.log10(max(linear, 1e-9)))


COMP_RATIO_MAP = {
    0: 1.1, 1: 1.3, 2: 1.5, 3: 2.0, 4: 2.5,
    5: 3.0, 6: 4.0, 7: 5.0, 8: 7.0, 9: 10.0, 10: 20.0, 11: 100.0
}


# ---------------------------------------------------------------------------
# EQ band transfer functions
# ---------------------------------------------------------------------------

def peaking_eq_response(freqs: np.ndarray, center_hz: float,
                         gain_db: float, q: float) -> np.ndarray:
    """Parametric peaking EQ response in dB. VEQ treated as PEQ."""
    if abs(gain_db) < 0.1:
        return np.zeros(len(freqs))

    A  = 10 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * center_hz
    w  = 2.0 * np.pi * freqs

    num = (w0 / q * A) ** 2 + (w ** 2 - w0 ** 2) ** 2
    den = (w0 / q / A) ** 2 + (w ** 2 - w0 ** 2) ** 2

    return 10.0 * np.log10(np.maximum(num / den, 1e-10))


def low_shelf_response(freqs: np.ndarray, corner_hz: float,
                        gain_db: float, q: float) -> np.ndarray:
    """Low shelf EQ response in dB."""
    if abs(gain_db) < 0.1:
        return np.zeros(len(freqs))

    A  = 10 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * corner_hz
    w  = 2.0 * np.pi * freqs

    num = A**2 * w0**4 + A * (w0/q)**2 * w**2 + w**4
    den =       w0**4 + (w0/q)**2 * w**2 / A + w**4

    return 10.0 * np.log10(np.maximum(num / np.maximum(den, 1e-20), 1e-10))


def high_shelf_response(freqs: np.ndarray, corner_hz: float,
                         gain_db: float, q: float) -> np.ndarray:
    """High shelf EQ response in dB."""
    if abs(gain_db) < 0.1:
        return np.zeros(len(freqs))

    A  = 10 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * corner_hz
    w  = 2.0 * np.pi * freqs

    num = A**2 * w**4 + A * (w0/q)**2 * w**2 + w0**4
    den =       w**4 + (w0/q)**2 * w**2 / A + w0**4

    return 10.0 * np.log10(np.maximum(num / np.maximum(den, 1e-20), 1e-10))


def hpf_response(freqs: np.ndarray, cutoff_hz: float,
                  slope_db_oct: int) -> np.ndarray:
    """Butterworth high-pass filter response in dB."""
    order_map = {12: 2, 18: 3, 24: 4}
    n = order_map.get(slope_db_oct, 2)

    safe_freqs = np.maximum(freqs, 0.1)
    ratio = cutoff_hz / safe_freqs
    magnitude_sq = 1.0 / (1.0 + ratio ** (2 * n))
    return 10.0 * np.log10(np.maximum(magnitude_sq, 1e-10))


def lpf_response(freqs: np.ndarray, cutoff_hz: float,
                  slope_db_oct: int) -> np.ndarray:
    """Butterworth low-pass filter response in dB."""
    order_map = {12: 2, 18: 3, 24: 4}
    n = order_map.get(slope_db_oct, 2)

    ratio = freqs / max(cutoff_hz, 0.1)
    magnitude_sq = 1.0 / (1.0 + ratio ** (2 * n))
    return 10.0 * np.log10(np.maximum(magnitude_sq, 1e-10))


def eq_band_response(band: EQBand, freqs: np.ndarray) -> np.ndarray:
    """Dispatch to correct transfer function for this EQ band type."""
    t = band.type
    if t == 0:      # LCut — HPF
        return hpf_response(freqs, band.freq_hz, slope_db_oct=12)
    elif t == 1:    # LShv
        return low_shelf_response(freqs, band.freq_hz, band.gain_db, band.q)
    elif t in (2, 3):  # PEQ, VEQ (VEQ approximated as PEQ)
        return peaking_eq_response(freqs, band.freq_hz, band.gain_db, band.q)
    elif t == 4:    # HShv
        return high_shelf_response(freqs, band.freq_hz, band.gain_db, band.q)
    elif t == 5:    # HCut — LPF
        return lpf_response(freqs, band.freq_hz, slope_db_oct=12)
    return np.zeros(len(freqs))


# ---------------------------------------------------------------------------
# Transfer curve computation
# ---------------------------------------------------------------------------

def compute_transfer_curves(config: 'ChannelConfig',
                              freqs: np.ndarray = None) -> 'ChannelConfig':
    """
    Compute and cache HPF, EQ, and combined transfer curves on a ChannelConfig.
    Call after loading config and after any config change.
    """
    if freqs is None:
        freqs = FREQ_AXIS

    if config.hpf_enabled and config.hpf_freq_hz > 20.0:
        config.hpf_curve_db = hpf_response(freqs, config.hpf_freq_hz,
                                             config.hpf_slope_db_oct)
    else:
        config.hpf_curve_db = np.zeros(len(freqs))

    if config.eq_enabled:
        config.eq_curve_db = np.zeros(len(freqs))
        for band in config.eq_bands:
            config.eq_curve_db = config.eq_curve_db + eq_band_response(band, freqs)
    else:
        config.eq_curve_db = np.zeros(len(freqs))

    config.transfer_curve_db = config.hpf_curve_db + config.eq_curve_db
    return config


# ---------------------------------------------------------------------------
# Channel contribution curve
# ---------------------------------------------------------------------------

def compute_contribution_curve(config: 'ChannelConfig',
                                 meter: 'ChannelMeterState',
                                 prior_curve_db: np.ndarray) -> np.ndarray:
    """
    Compute this channel's spectral contribution to the mix in dB.

    Returns -90 dB silence if channel is muted or below SILENCE_THRESHOLD_DB.
    """
    if config.muted:
        return np.full(len(FREQ_AXIS), -90.0)
    if meter.post_fade_db < SILENCE_THRESHOLD_DB:
        return np.full(len(FREQ_AXIS), -90.0)

    assert config.transfer_curve_db is not None, (
        f"compute_transfer_curves() must be called before contribution calc on ch{config.channel_num}"
    )

    shaped = prior_curve_db + config.transfer_curve_db
    shape_mean = np.mean(shaped)
    normalized_shape = shaped - shape_mean

    effective_level_db = meter.post_fade_db + meter.effective_gr_db
    return normalized_shape + effective_level_db + config.trim_db


# ---------------------------------------------------------------------------
# Instrument prior system
# ---------------------------------------------------------------------------

class InstrumentPrior:
    """Normalized spectral shape curves for one instrument type. Interpolated to FREQ_AXIS."""

    def __init__(self, instrument_type: str, prior_config: dict):
        self.instrument_type = instrument_type
        self._curves: dict[str, np.ndarray] = {}

        for state_name, state_data in prior_config.items():
            if not isinstance(state_data, dict) or 'curve' not in state_data:
                continue
            control_points = state_data['curve']
            freqs_ctrl  = np.array([p[0] for p in control_points], dtype=float)
            levels_ctrl = np.array([p[1] for p in control_points], dtype=float)

            log_freqs_ctrl = np.log10(freqs_ctrl)
            log_freq_axis  = np.log10(FREQ_AXIS)

            interpolated = np.interp(
                log_freq_axis, log_freqs_ctrl, levels_ctrl,
                left=levels_ctrl[0], right=levels_ctrl[-1]
            )
            self._curves[state_name] = interpolated - np.mean(interpolated)

        if 'normal' not in self._curves:
            self._curves['normal'] = np.zeros(N_FREQS)

    def get_curve(self, state: str = 'normal') -> np.ndarray:
        """Return prior curve for given state. Falls back to 'normal'."""
        return self._curves.get(state, self._curves['normal']).copy()

    def update_band(self, state: str, freq_lo: float, freq_hi: float,
                    delta_db: float, alpha: float) -> None:
        """Apply a damped update to a frequency band in the prior curve.

        Adds alpha × delta_db to the bins in [freq_lo, freq_hi) on the named
        state curve, then re-normalizes to zero mean.
        """
        if state not in self._curves:
            state = 'normal'
        mask = (FREQ_AXIS >= freq_lo) & (FREQ_AXIS < freq_hi)
        if not mask.any():
            return
        curve = self._curves[state].copy()
        curve[mask] += alpha * delta_db
        self._curves[state] = curve - np.mean(curve)


# ---------------------------------------------------------------------------
# Input state inference
# ---------------------------------------------------------------------------

def infer_input_state(channel_num: int,
                       meter: 'ChannelMeterState',
                       config: 'ChannelConfig',
                       state_history: list) -> str:
    """Infer current input state from meter readings and history."""
    if meter.post_fade_db < SILENCE_THRESHOLD_DB:
        return 'silent'

    if meter.gate_gr_db < -6.0:
        return 'gated'

    if config.instrument_type in ('guitar', 'guitar_lead'):
        if meter.rms_delta_db > 2.0:
            return 'solo_onset'

        current_state = state_history[-1] if state_history else 'normal'

        if current_state in ('solo_onset', 'solo_active'):
            if meter.rms_delta_db < -1.5:
                return 'decay'
            return 'solo_active'

        if current_state == 'decay':
            if meter.rms_delta_db > -0.5:
                return 'normal'
            return 'decay'

    return 'normal'
