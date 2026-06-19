"""Forward mix model — predicts the room spectrum from board state and compares to mic.

Runs in PASSIVE_MODE for Phase 2 (log only, no new recommendations).
Phase 1 LUFS and rule-based recommendations continue unchanged alongside this.
"""

import time
from typing import Optional

import numpy as np

from core.channel_model import FREQ_AXIS, N_FREQS, compute_contribution_curve
from models.analysis import MicAnalysis, ForwardModelResult

# RTA frequency axis (100 bands from /meters/15)
_RTA_FREQS = np.array([
    20, 21, 22, 24, 26, 28, 30, 32, 34, 36, 39, 42, 45, 48, 52, 55, 59,
    63, 68, 73, 78, 84, 90, 96, 103, 110, 118, 127, 136, 146, 156, 167,
    179, 192, 206, 221, 237, 254, 272, 292, 313, 335, 359, 385, 412, 442,
    474, 508, 544, 583, 625, 670, 718, 769, 825, 884, 947, 1020, 1090,
    1170, 1250, 1340, 1440, 1540, 1650, 1770, 1890, 2030, 2180, 2330,
    2500, 2680, 2870, 3080, 3300, 3540, 3790, 4060, 4350, 4670, 5000,
    5360, 5740, 6160, 6600, 7070, 7580, 8120, 8710, 9330, 10000, 10720,
    11490, 12310, 13200, 14140, 15160, 16250, 17410, 18660
], dtype=float)


def _interpolate_rta_to_freq_axis(rta_100) -> np.ndarray:
    """Interpolate 100-band board RTA to FREQ_AXIS (1000 log-spaced points).

    Returns a flat -60dBFS array if rta_100 is None, empty, or not exactly
    100 elements — avoids crashing during simulator startup or when the
    /meters/15 subscription hasn't fired yet.
    """
    if rta_100 is None or len(rta_100) != 100:
        return np.full(len(FREQ_AXIS), -60.0)
    log_rta  = np.log10(_RTA_FREQS)
    log_axis = np.log10(FREQ_AXIS)
    return np.interp(log_axis, log_rta, rta_100,
                     left=rta_100[0], right=rta_100[-1])


def current_time_ms() -> float:
    return time.time() * 1000.0


# ---------------------------------------------------------------------------
# Band ranges (shared with mic_analyzer ANALYSIS_BANDS)
# ---------------------------------------------------------------------------

BAND_RANGES = {
    'sub':       (20,    80),
    'bass':      (80,   200),
    'low_mid':   (200,  500),
    'mid_low':   (500,  1000),
    'mid_high':  (1000, 2000),
    'upper_mid': (2000, 4000),
    'presence':  (4000, 8000),
    'air':       (8000, 20000),
}

CONFIDENCE_THRESHOLD = 0.65


# ---------------------------------------------------------------------------
# Channel contribution scoring
# ---------------------------------------------------------------------------

def score_channel_contributions(contributions: dict,
                                  freq_low: float,
                                  freq_high: float) -> dict:
    """
    Score each channel's contribution to a frequency band as fraction of total energy.
    Returns {channel_num: score}.
    """
    mask = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
    if not mask.any():
        return {}

    band_powers = {}
    for ch_num, curve_db in contributions.items():
        band_linear = np.mean(10.0 ** (curve_db[mask] / 10.0))
        band_powers[ch_num] = max(float(band_linear), 1e-12)

    total_power = sum(band_powers.values())
    if total_power < 1e-12:
        return {ch: 0.0 for ch in band_powers}

    return {ch: power / total_power for ch, power in band_powers.items()}


def find_dominant_channel(contributions: dict, band_name: str) -> tuple:
    """Return (channel_num, score) of the highest-contributing channel in named band."""
    freq_low, freq_high = BAND_RANGES[band_name]
    scores = score_channel_contributions(contributions, freq_low, freq_high)
    if not scores:
        return (-1, 0.0)
    dominant = max(scores, key=scores.get)
    return (dominant, scores[dominant])


# ---------------------------------------------------------------------------
# Deviation decomposition
# ---------------------------------------------------------------------------

def decompose_deviation(deviation_history: list,
                          window_cycles: int = 60) -> tuple:
    """
    Separate systematic (room) from transient (mix) deviation.
    Room deviation = median of recent history (robust to outliers).
    Mix deviation = current - room.
    """
    if len(deviation_history) < 10:
        return np.zeros(N_FREQS), deviation_history[-1].copy()

    history_array = np.array(deviation_history[-window_cycles:])
    room_deviation = np.median(history_array, axis=0)
    mix_deviation  = deviation_history[-1] - room_deviation
    return room_deviation, mix_deviation


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def compute_confidence(mic_analysis: MicAnalysis,
                         board_rta_db: np.ndarray,
                         predicted_db: np.ndarray,
                         dominant_score: float,
                         venue_acoustics) -> np.ndarray:
    """
    Per-frequency confidence score [0.0, 1.0].
    Product of: mic-to-RTA agreement × contribution dominance × venue reliability.
    """
    # 1. Mic-to-RTA agreement: 0dB diff → 1.0, 6dB diff → 0.0
    rta_vs_mic_diff = np.abs(mic_analysis.smoothed_spectrum_db - board_rta_db)
    mic_agreement   = np.clip(1.0 - rta_vs_mic_diff / 6.0, 0.0, 1.0)

    # 2. Contribution dominance
    dominance_scalar = min(dominant_score / 0.5, 1.0)
    dominance        = np.full(N_FREQS, dominance_scalar)

    # 3. Venue reliability (comb filter notch regions get 0.3)
    reliability  = venue_acoustics.comb_reliability_mask()
    mode_mask    = venue_acoustics.room_mode_mask()
    reliability  = np.where(mode_mask, reliability * 0.4, reliability)
    overall_rel  = venue_acoustics.mic_reliability_weight()

    return mic_agreement * dominance * reliability * overall_rel


# ---------------------------------------------------------------------------
# ForwardModel
# ---------------------------------------------------------------------------

class ForwardModel:
    """
    Combines board channel contributions with mic analysis.
    Produces predicted vs measured comparison with confidence scoring.

    PASSIVE_MODE = True in Phase 2: logs results, fires zero new recommendations.
    """

    PASSIVE_MODE = True

    def __init__(self, venue_acoustics):
        self.venue_acoustics = venue_acoustics
        self._deviation_history: list = []
        self._cycle_count = 0
        self._last_result: Optional['ForwardModelResult'] = None

    def run(self,
             channel_configs: dict,
             channel_meters: dict,
             channel_priors: dict,
             mic_analysis: MicAnalysis,
             board_rta_db: np.ndarray) -> ForwardModelResult:
        """Execute one forward model cycle. Call once per 500ms analysis cycle."""
        self._cycle_count += 1

        if mic_analysis.is_silent:
            return ForwardModelResult.silent(timestamp_ms=current_time_ms())

        # 1. Compute per-channel contribution curves
        contributions = {}
        for ch_num, config in channel_configs.items():
            if ch_num not in channel_meters:
                continue
            meter = channel_meters[ch_num]
            prior = channel_priors.get(ch_num)
            if prior is None:
                continue

            prior_curve = prior.get_curve(meter.input_state)
            contributions[ch_num] = compute_contribution_curve(config, meter, prior_curve)

        if not contributions:
            return ForwardModelResult.make_no_channels(timestamp_ms=current_time_ms())

        # 2. Sum contributions → predicted spectrum (linear sum, then dB)
        predicted_linear = np.zeros(N_FREQS)
        for curve_db in contributions.values():
            predicted_linear += 10.0 ** (curve_db / 10.0)
        predicted_db = 10.0 * np.log10(np.maximum(predicted_linear, 1e-12))

        # 3. Deviations — interpolate 100-band board RTA to FREQ_AXIS first
        if board_rta_db is None or len(board_rta_db) != 100:
            board_rta_db = np.full(100, -60.0)
        board_rta_on_axis  = _interpolate_rta_to_freq_axis(board_rta_db)
        mic_deviation_db   = mic_analysis.smoothed_spectrum_db - predicted_db
        board_deviation_db = board_rta_on_axis - predicted_db

        # 4. Accumulate and decompose deviation history
        self._deviation_history.append(mic_deviation_db.copy())
        if len(self._deviation_history) > 120:   # 60 seconds
            self._deviation_history.pop(0)
        room_deviation_db, mix_deviation_db = decompose_deviation(self._deviation_history)

        # 5. R² metrics
        r_sq_mic   = self._compute_r_squared(predicted_db, mic_analysis.smoothed_spectrum_db)
        r_sq_board = self._compute_r_squared(predicted_db, board_rta_on_axis)

        # 6. Channel attribution per band
        dominant_channels   = {}
        contribution_scores = {}
        for band_name in BAND_RANGES:
            dom_ch, dom_score = find_dominant_channel(contributions, band_name)
            dominant_channels[band_name]   = dom_ch
            contribution_scores[band_name] = dom_score

        # 7. Confidence
        avg_dominance = float(np.mean(list(contribution_scores.values()))) if contribution_scores else 0.0
        confidence = compute_confidence(
            mic_analysis, board_rta_on_axis, predicted_db,
            avg_dominance, self.venue_acoustics
        )

        # 8. Identify actionable bands (mix deviation + high confidence)
        actionable_bands = []
        for band_name, (freq_low, freq_high) in BAND_RANGES.items():
            mask             = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
            band_deviation   = float(np.mean(mix_deviation_db[mask]))
            band_confidence  = float(np.mean(confidence[mask]))

            if (abs(band_deviation) > 2.0 and
                band_confidence > CONFIDENCE_THRESHOLD and
                not mic_analysis.is_silent):
                actionable_bands.append({
                    'band':       band_name,
                    'deviation':  band_deviation,
                    'confidence': band_confidence,
                    'direction':  'hot' if band_deviation > 0 else 'low',
                    'channel':    dominant_channels.get(band_name, -1),
                    'ch_score':   contribution_scores.get(band_name, 0.0),
                })

        result = ForwardModelResult(
            predicted_db=predicted_db,
            measured_db=mic_analysis.smoothed_spectrum_db,
            board_rta_db=board_rta_on_axis,
            deviation_db=mic_deviation_db,
            board_deviation_db=board_deviation_db,
            mix_deviation_db=mix_deviation_db,
            room_deviation_db=room_deviation_db,
            confidence=confidence,
            dominant_channels=dominant_channels,
            contribution_scores=contribution_scores,
            channel_contributions=contributions,
            actionable_bands=actionable_bands,
            r_squared_mic=r_sq_mic,
            r_squared_board=r_sq_board,
            passive_mode=self.PASSIVE_MODE,
            timestamp_ms=current_time_ms(),
            cycle_num=self._cycle_count,
        )
        self._last_result = result
        return result

    def predicted_band_db(self, channel_num: int, band_name: str) -> float:
        """Return last-cycle predicted contribution of channel to band (dB).

        Used by the RTA investigation engine to compute per-channel deviations.
        Returns -90.0 if no result yet or channel not found.
        """
        if self._last_result is None:
            return -90.0
        contributions = self._last_result.channel_contributions
        if channel_num not in contributions:
            return -90.0
        freq_lo, freq_hi = BAND_RANGES.get(band_name, (0.0, 20000.0))
        mask = (FREQ_AXIS >= freq_lo) & (FREQ_AXIS < freq_hi)
        if not mask.any():
            return -90.0
        return float(np.mean(contributions[channel_num][mask]))

    @staticmethod
    def _compute_r_squared(predicted: np.ndarray, measured: np.ndarray) -> float:
        """Pearson R² correlation between predicted and measured spectra."""
        if len(predicted) != len(measured):
            return 0.0
        ss_res = float(np.sum((measured - predicted) ** 2))
        ss_tot = float(np.sum((measured - np.mean(measured)) ** 2))
        if ss_tot < 1e-12:
            return 0.0
        return max(0.0, 1.0 - ss_res / ss_tot)
