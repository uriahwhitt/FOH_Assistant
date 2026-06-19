"""Reference microphone analysis pipeline.

Welch FFT → geometry correction → EMA smoothing → band analysis → LUFS.
Geometry-aware: raw mic readings are corrected for comb filtering and room modes
before comparison with the forward mix model prediction.
"""

import time
from typing import Optional

import numpy as np
import scipy.signal
import pyloudnorm as pyln

from core.channel_model import FREQ_AXIS, N_FREQS
from models.analysis import MicAnalysis


# ---------------------------------------------------------------------------
# Analysis bands (8 bands — per IMP-024 tighter midrange split)
# ---------------------------------------------------------------------------

ANALYSIS_BANDS = [
    ('sub',        20,    80),
    ('bass',       80,   200),
    ('low_mid',   200,   500),
    ('mid_low',   500,  1000),
    ('mid_high', 1000,  2000),
    ('upper_mid',2000,  4000),
    ('presence', 4000,  8000),
    ('air',      8000, 20000),
]

ROOM_SILENCE_THRESHOLD_LUFS = -50.0
DISPLAY_WINDOW_SECONDS = 0.1
DISPLAY_EMA_ALPHA_MIC  = 0.40


def current_time_ms() -> float:
    return time.time() * 1000.0


# ---------------------------------------------------------------------------
# Spectral shape utilities (module-level — importable by RTA engine, recommender)
# ---------------------------------------------------------------------------

def normalize_to_shape(spectrum_db: np.ndarray,
                       freq_mask: 'np.ndarray | None' = None) -> np.ndarray:
    """Subtract mean level to produce a shape-only, position-independent spectrum.

    Parameters
    ----------
    spectrum_db : ndarray
        Raw spectrum in dBFS (any length).
    freq_mask : ndarray of bool, optional
        Restrict the mean calculation to specific bins (e.g. 80Hz–16kHz).
        If None, mean is computed across all bins.

    Returns
    -------
    ndarray
        Shape-normalized spectrum where 0 dB = average energy level.
        Independent of mic placement distance.
    """
    if freq_mask is not None and freq_mask.any():
        mean_db = float(np.mean(spectrum_db[freq_mask]))
    else:
        mean_db = float(np.mean(spectrum_db))
    return spectrum_db - mean_db


def find_band_peak(spectrum_db: np.ndarray,
                   freq_axis: np.ndarray,
                   band_lo: float,
                   band_hi: float) -> tuple[float, float]:
    """Find the peak frequency and its prominence above the band mean.

    Parameters
    ----------
    spectrum_db : ndarray
        Full spectrum on FREQ_AXIS.
    freq_axis : ndarray
        Corresponding frequency values in Hz (FREQ_AXIS).
    band_lo, band_hi : float
        Band boundaries in Hz.

    Returns
    -------
    peak_hz : float
        Frequency of the peak within the band.
    peak_prominence_db : float
        How far the peak sits above the band's arithmetic mean.
        High = sharp resonance (specific EQ target).
        Low = broad energy (use band center instead).
    """
    mask = (freq_axis >= band_lo) & (freq_axis < band_hi)
    if not mask.any():
        return (band_lo + band_hi) / 2.0, 0.0

    band_spectrum = spectrum_db[mask]
    band_freqs    = freq_axis[mask]
    band_mean     = float(np.mean(band_spectrum))
    peak_idx      = int(np.argmax(band_spectrum))

    return float(band_freqs[peak_idx]), float(band_spectrum[peak_idx]) - band_mean


def band_average(spectrum_db: np.ndarray, freq_range: tuple) -> float:
    """Average spectrum energy in a frequency range using the shared FREQ_AXIS.

    Parameters
    ----------
    spectrum_db : ndarray shape (N_FREQS,)
        Spectrum on FREQ_AXIS (1000 log-spaced points 20Hz–20kHz).
    freq_range : (float, float)
        (freq_low_hz, freq_high_hz) band boundaries.

    Returns
    -------
    float
        Mean dB level across the band, or -90.0 if no bins fall in range.
    """
    freq_low, freq_high = freq_range
    mask = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
    if not mask.any():
        return -90.0
    return float(np.mean(spectrum_db[mask]))


# ---------------------------------------------------------------------------
# Spectral analysis primitives
# ---------------------------------------------------------------------------

def compute_welch_spectrum(audio_buffer: np.ndarray,
                            sample_rate: int) -> tuple:
    """
    Compute power spectral density using Welch's method.
    Returns (freqs_hz, psd_db).
    4096-sample window (~85ms at 48kHz) with 50% overlap gives ~11 averaged
    segments over a 500ms buffer — significantly reduces variance vs single FFT.
    """
    nperseg  = min(4096, len(audio_buffer))
    noverlap = nperseg // 2

    freqs, psd = scipy.signal.welch(
        audio_buffer,
        fs=sample_rate,
        window='hann',
        nperseg=nperseg,
        noverlap=noverlap,
        scaling='density',
    )

    psd_db = 10.0 * np.log10(np.maximum(psd, 1e-12))
    return freqs, psd_db


def interpolate_to_freq_axis(freqs_hz: np.ndarray,
                               spectrum_db: np.ndarray) -> np.ndarray:
    """Interpolate spectrum from Welch output frequencies to shared FREQ_AXIS (log-space)."""
    valid_mask = (freqs_hz >= FREQ_AXIS[0]) & (freqs_hz <= FREQ_AXIS[-1])

    if valid_mask.sum() < 2:
        return np.full(N_FREQS, -90.0)

    log_freqs_in  = np.log10(freqs_hz[valid_mask])
    log_freq_axis = np.log10(FREQ_AXIS)

    return np.interp(
        log_freq_axis,
        log_freqs_in,
        spectrum_db[valid_mask],
        left=spectrum_db[valid_mask][0],
        right=spectrum_db[valid_mask][-1],
    )


def compute_band_levels(spectrum_db: np.ndarray) -> dict:
    """Compute energy summary per analysis band.

    Returns band_name → {avg_db, peak_db, peak_hz, peak_prominence_db}.
    avg_db uses energy averaging (physically correct).
    peak_prominence_db uses arithmetic mean (consistent with find_band_peak()).
    """
    results = {}
    for band_name, freq_low, freq_high in ANALYSIS_BANDS:
        mask = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
        if not mask.any():
            results[band_name] = {
                'avg_db': -90.0, 'peak_db': -90.0,
                'peak_hz': (freq_low + freq_high) / 2.0,
                'peak_prominence_db': 0.0,
            }
            continue
        band_spectrum = spectrum_db[mask]
        band_freqs    = FREQ_AXIS[mask]

        band_linear = 10.0 ** (band_spectrum / 10.0)
        avg_db      = 10.0 * np.log10(np.maximum(np.mean(band_linear), 1e-12))
        peak_idx    = int(np.argmax(band_spectrum))
        peak_db     = float(band_spectrum[peak_idx])
        peak_hz     = float(band_freqs[peak_idx])
        prominence  = peak_db - float(np.mean(band_spectrum))

        results[band_name] = {
            'avg_db':             float(avg_db),
            'peak_db':            peak_db,
            'peak_hz':            peak_hz,
            'peak_prominence_db': prominence,
        }
    return results


def compute_lufs(audio_buffer: np.ndarray, sample_rate: int) -> float:
    """Compute integrated LUFS per ITU-R BS.1770-4. Returns -70.0 for silence."""
    meter = pyln.Meter(sample_rate)
    try:
        loudness = meter.integrated_loudness(audio_buffer)
        if loudness == float('-inf') or (loudness != loudness):  # nan check
            return -70.0
        return float(loudness)
    except Exception:
        return -70.0


def is_room_silent(lufs: float, band_levels: dict,
                   threshold_lufs: float = ROOM_SILENCE_THRESHOLD_LUFS) -> bool:
    """True if the room is silent (between songs, before show, etc.)."""
    if lufs < threshold_lufs:
        return True
    if all(b['avg_db'] < -55.0 for b in band_levels.values()):
        return True
    return False


# ---------------------------------------------------------------------------
# EMA smoothing state
# ---------------------------------------------------------------------------

class EMAState:
    """Exponential moving average state across analysis cycles."""

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self._state: Optional[np.ndarray] = None

    def update(self, new_spectrum: np.ndarray) -> np.ndarray:
        if self._state is None:
            self._state = new_spectrum.copy()
            return self._state.copy()
        self._state = self.alpha * new_spectrum + (1.0 - self.alpha) * self._state
        return self._state.copy()

    def reset(self):
        self._state = None


# ---------------------------------------------------------------------------
# Spectrum history ring buffer
# ---------------------------------------------------------------------------

class SpectrumHistory:
    """Ring buffer of recent spectrum snapshots for pre-event retrieval."""

    HISTORY_DEPTH = 20   # 20 × 500ms = 10 seconds

    def __init__(self):
        self._history: list = []

    def push(self, analysis: MicAnalysis) -> None:
        self._history.append(analysis)
        if len(self._history) > self.HISTORY_DEPTH:
            self._history.pop(0)

    def get_snapshot_before(self, event_timestamp_ms: float,
                              offset_ms: float = 500.0) -> Optional[np.ndarray]:
        """Return smoothed spectrum closest to (event_timestamp - offset_ms)."""
        target_ms = event_timestamp_ms - offset_ms
        if not self._history:
            return None
        closest = min(self._history, key=lambda a: abs(a.timestamp_ms - target_ms))
        return closest.smoothed_spectrum_db.copy()


# ---------------------------------------------------------------------------
# MicAnalyzer
# ---------------------------------------------------------------------------

class MicAnalyzer:
    """
    Complete reference microphone analysis pipeline.
    Geometry-aware — applies acoustic corrections from VenueAcoustics.
    Call analyze() once per 500ms analysis cycle.
    """

    def __init__(self, venue_acoustics, silence_threshold_lufs: float = -50.0):
        """
        venue_acoustics: VenueAcoustics instance from core.geometry.
        Pass IrregularRoomAcoustics({}) when no venue profile is loaded.
        """
        self.venue_acoustics     = venue_acoustics
        self.correction_curve_db = venue_acoustics.mic_correction_curve()
        self.room_mode_mask_arr  = venue_acoustics.room_mode_mask()
        self._ema_analysis       = EMAState(alpha=0.3)
        self._ema_display        = EMAState(alpha=DISPLAY_EMA_ALPHA_MIC)
        self._prev_spectrum: Optional[np.ndarray] = None
        self._current_analysis: Optional['MicAnalysis'] = None
        self._silence_threshold  = silence_threshold_lufs

    def analyze(self, audio_capture) -> MicAnalysis:
        """Full analysis pipeline. Call once per 500ms cycle."""
        sample_rate = audio_capture.sample_rate

        fft_window  = audio_capture.get_analysis_window()
        lufs_window = audio_capture.get_lufs_window()

        lufs = compute_lufs(lufs_window, sample_rate)

        if len(fft_window) < 512:
            empty = np.full(N_FREQS, -90.0)
            result = MicAnalysis(
                lufs=lufs, raw_spectrum_db=empty, corrected_spectrum_db=empty,
                smoothed_spectrum_db=empty, normalized_shape_db=np.zeros(N_FREQS),
                spectral_delta_db=np.zeros(N_FREQS),
                band_levels={}, room_mode_flags=np.zeros(N_FREQS, dtype=bool),
                correction_applied_db=self.correction_curve_db,
                is_silent=True, timestamp_ms=current_time_ms(),
            )
            self._current_analysis = result
            return result

        freqs_hz, raw_psd_db = compute_welch_spectrum(fft_window, sample_rate)
        raw_spectrum_db = interpolate_to_freq_axis(freqs_hz, raw_psd_db)

        corrected_spectrum_db = raw_spectrum_db + self.correction_curve_db
        smoothed_spectrum_db  = self._ema_analysis.update(corrected_spectrum_db)
        normalized_shape_db   = normalize_to_shape(smoothed_spectrum_db)

        band_levels  = compute_band_levels(smoothed_spectrum_db)
        silent       = is_room_silent(lufs, band_levels, self._silence_threshold)

        room_mode_flags = self.room_mode_mask_arr & (smoothed_spectrum_db > -30.0)

        if self._prev_spectrum is not None:
            spectral_delta = smoothed_spectrum_db - self._prev_spectrum
        else:
            spectral_delta = np.zeros(N_FREQS)
        self._prev_spectrum = smoothed_spectrum_db.copy()

        result = MicAnalysis(
            lufs=lufs,
            raw_spectrum_db=raw_spectrum_db,
            corrected_spectrum_db=corrected_spectrum_db,
            smoothed_spectrum_db=smoothed_spectrum_db,
            normalized_shape_db=normalized_shape_db,
            spectral_delta_db=spectral_delta,
            band_levels=band_levels,
            room_mode_flags=room_mode_flags,
            correction_applied_db=self.correction_curve_db,
            is_silent=silent,
            timestamp_ms=current_time_ms(),
        )
        self._current_analysis = result
        return result

    def get_current_normalized_shape(self) -> np.ndarray:
        """Return the current smoothed spectrum normalized to shape.

        Used by cal scan and iso sampling. Returns zeros if no data yet.
        """
        if self._current_analysis is None:
            return np.zeros(N_FREQS)
        return normalize_to_shape(self._current_analysis.smoothed_spectrum_db)

    def compute_display_spectrum(self,
                                  audio_capture,
                                  venue_acoustics=None) -> np.ndarray:
        """Fast display-path spectrum. Single Hanning window, 100ms.

        Returns normalized_shape_db on FREQ_AXIS.
        For display only — never used for recommendations, logging, or cal/iso scans.
        """
        n_samples    = int(DISPLAY_WINDOW_SECONDS * audio_capture.sample_rate)
        window_audio = audio_capture.get_display_window(n_samples)

        if window_audio is None or len(window_audio) < 512:
            return np.zeros(N_FREQS)

        n        = len(window_audio)
        windowed = window_audio * np.hanning(n)
        spectrum = np.fft.rfft(windowed)
        freqs_hz = np.fft.rfftfreq(n, d=1.0 / audio_capture.sample_rate)
        psd_db   = 20.0 * np.log10(np.maximum(np.abs(spectrum) / n, 1e-12))

        interpolated = interpolate_to_freq_axis(freqs_hz, psd_db)

        corr = venue_acoustics.mic_correction_curve() if venue_acoustics is not None \
               else self.correction_curve_db
        try:
            interpolated = interpolated + corr
        except Exception:
            pass

        smoothed = self._ema_display.update(interpolated)
        return normalize_to_shape(smoothed)

    def reset_ema(self) -> None:
        """Call at song transitions to prevent smearing across songs."""
        self._ema_analysis.reset()
        self._prev_spectrum = None

    def characterize_input_event(self,
                                   pre_event_snapshot: np.ndarray,
                                   post_event_snapshot: np.ndarray) -> dict:
        """
        Called when board detects an input state change (e.g. solo onset).
        Returns characterization dict for INPUT_STATE_EVENT log entry.
        """
        if pre_event_snapshot is None:
            pre_event_snapshot = np.full(N_FREQS, -60.0)

        delta = post_event_snapshot - pre_event_snapshot

        pre_linear  = 10.0 ** (pre_event_snapshot / 10.0)
        post_linear = 10.0 ** (post_event_snapshot / 10.0)

        pre_centroid  = float(np.sum(FREQ_AXIS * pre_linear)  / max(np.sum(pre_linear),  1e-12))
        post_centroid = float(np.sum(FREQ_AXIS * post_linear) / max(np.sum(post_linear), 1e-12))
        centroid_shift_hz = post_centroid - pre_centroid

        band_deltas = {}
        for band_name, freq_low, freq_high in ANALYSIS_BANDS:
            mask = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
            if mask.any():
                band_deltas[band_name] = float(np.mean(delta[mask]))

        dominant_band = max(band_deltas, key=lambda k: band_deltas[k]) if band_deltas else 'mid_low'

        spectral_shift_direction = (
            'upward'   if centroid_shift_hz >  200 else
            'downward' if centroid_shift_hz < -200 else
            'neutral'
        )

        return {
            'centroid_shift_hz':        float(centroid_shift_hz),
            'spectral_shift_direction': spectral_shift_direction,
            'dominant_band':            dominant_band,
            'band_deltas_db':           band_deltas,
            'mic_confirmed_change':     (abs(centroid_shift_hz) > 100 or
                                         abs(max(band_deltas.values(), default=0)) > 1.0),
        }
