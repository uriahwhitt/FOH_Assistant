"""Phase 2 analysis result data models."""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class MicAnalysis:
    """Complete mic analysis result for one 500ms cycle."""

    # LUFS
    lufs: float

    # Spectra (all on FREQ_AXIS, shape (N_FREQS,), dBFS)
    raw_spectrum_db: np.ndarray          # Welch output, no correction
    corrected_spectrum_db: np.ndarray    # + geometry correction
    smoothed_spectrum_db: np.ndarray     # + EMA smoothing (primary analysis signal)
    spectral_delta_db: np.ndarray        # change vs previous cycle

    # Band summary: band_name -> {avg_db, peak_db, peak_hz}
    band_levels: dict

    # Acoustic metadata
    room_mode_flags: np.ndarray          # bool array — True where room mode predicted
    correction_applied_db: np.ndarray    # correction curve used this cycle

    # State
    is_silent: bool
    timestamp_ms: float

    # Mean-subtracted shape spectrum — position-independent (populated by MicAnalyzer.analyze())
    normalized_shape_db: np.ndarray = field(default_factory=lambda: np.zeros(1000))
    # Active-range normalized shape — mean computed over 80Hz–8kHz only (display use)
    normalized_shape_active_db: np.ndarray = field(default_factory=lambda: np.zeros(1000))

    @property
    def spectral_centroid_hz(self) -> float:
        """Frequency center of mass of the smoothed spectrum."""
        from core.channel_model import FREQ_AXIS
        linear = 10.0 ** (self.smoothed_spectrum_db / 10.0)
        total  = np.sum(linear)
        if total < 1e-12:
            return 0.0
        return float(np.sum(FREQ_AXIS * linear) / total)


@dataclass
class ForwardModelResult:
    """Complete forward model output for one analysis cycle."""

    # Core spectra (all on FREQ_AXIS, shape (N_FREQS,), dBFS)
    predicted_db:       np.ndarray
    measured_db:        np.ndarray
    board_rta_db:       np.ndarray

    # Deviations
    deviation_db:       np.ndarray    # measured - predicted (full)
    board_deviation_db: np.ndarray    # board_rta - predicted
    mix_deviation_db:   np.ndarray    # transient component (mix problems)
    room_deviation_db:  np.ndarray    # systematic component (room acoustics)

    # Confidence
    confidence:         np.ndarray    # per-frequency [0.0, 1.0]

    # Attribution
    dominant_channels:    dict        # band_name -> channel_num
    contribution_scores:  dict        # band_name -> dominant channel score
    channel_contributions: dict       # channel_num -> contribution_db array

    # Actionable findings
    actionable_bands: list            # list of dicts per actionable band

    # Validation metrics
    r_squared_mic:   float
    r_squared_board: float

    # Metadata
    passive_mode: bool
    timestamp_ms: float
    cycle_num: int
    is_silent: bool = False
    no_active_channels: bool = False

    @classmethod
    def silent(cls, timestamp_ms: float) -> 'ForwardModelResult':
        """Sentinel result for silent cycles."""
        from core.channel_model import N_FREQS
        empty = np.full(N_FREQS, -90.0)
        return cls(
            predicted_db=empty, measured_db=empty, board_rta_db=empty,
            deviation_db=np.zeros(N_FREQS), board_deviation_db=np.zeros(N_FREQS),
            mix_deviation_db=np.zeros(N_FREQS), room_deviation_db=np.zeros(N_FREQS),
            confidence=np.zeros(N_FREQS),
            dominant_channels={}, contribution_scores={}, channel_contributions={},
            actionable_bands=[], r_squared_mic=0.0, r_squared_board=0.0,
            passive_mode=True, timestamp_ms=timestamp_ms, cycle_num=0,
            is_silent=True,
        )

    @classmethod
    def make_no_channels(cls, timestamp_ms: float) -> 'ForwardModelResult':
        """Sentinel result when no channels have signal."""
        result = cls.silent(timestamp_ms)
        result.is_silent = False
        result.no_active_channels = True
        return result
