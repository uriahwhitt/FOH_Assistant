"""Real-time room audio analysis: LUFS, RMS, FFT frequency bands."""

import time
import math
import numpy as np
from scipy import signal as sp_signal
import pyloudnorm as pyln
from models.event import RoomAnalysis

# Frequency band boundaries in Hz
BAND_EDGES = {
    "sub_bass":  (20,    80),
    "bass":      (80,    250),
    "low_mid":   (250,   500),
    "mid":       (500,   2000),
    "high_mid":  (2000,  6000),
    "presence":  (6000,  12000),
    "air":       (12000, 20000),
}
BAND_NAMES = ("sub_bass", "bass", "low_mid", "mid", "high_mid", "presence", "air")


class Analyzer:
    def __init__(self, sample_rate: int = 48000):
        self._sr = sample_rate
        self._meter = pyln.Meter(sample_rate)
        self._prev_bands: dict[str, float] = {b: -90.0 for b in BAND_NAMES}
        self._prev_lufs: float = -70.0

    def analyze(self, audio: np.ndarray, ambient=None) -> RoomAnalysis:
        """Analyze a mono float32 audio buffer and return RoomAnalysis.

        When ambient (an AmbientBaseline) is provided, band readings are corrected
        by subtracting the ambient level for bands where SNR > AMBIENT_SNR_THRESHOLD_DB.
        """
        now = time.time()

        if audio is None or len(audio) < self._sr // 10:
            return self._silent_result(now)

        lufs = self._integrated_lufs(audio)
        rms_db = self._short_term_rms(audio)
        bands = self._fft_bands(audio)

        if ambient is not None:
            from core.ambient import AMBIENT_SNR_THRESHOLD_DB
            corrected = {}
            for band, level in bands.items():
                offset = ambient.bands.get(band, -90.0)
                corrected[band] = (level - offset
                                   if level - offset > AMBIENT_SNR_THRESHOLD_DB
                                   else level)
            bands = corrected

        band_delta = {b: bands[b] - self._prev_bands[b] for b in BAND_NAMES}
        lufs_delta = lufs - self._prev_lufs

        self._prev_bands = bands.copy()
        self._prev_lufs = lufs

        return RoomAnalysis(
            lufs=lufs,
            rms_db=rms_db,
            bands=bands,
            band_delta=band_delta,
            lufs_delta=lufs_delta,
            timestamp=now,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _integrated_lufs(self, audio: np.ndarray) -> float:
        try:
            # pyloudnorm expects shape (samples,) or (samples, channels)
            loudness = self._meter.integrated_loudness(audio.astype(np.float64))
            return float(loudness) if math.isfinite(loudness) else -70.0
        except Exception:
            return -70.0

    def _short_term_rms(self, audio: np.ndarray, window_s: float = 0.3) -> float:
        n = int(self._sr * window_s)
        chunk = audio[-n:] if len(audio) >= n else audio
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        if rms <= 0:
            return -90.0
        return max(-90.0, 20 * math.log10(rms))

    def _fft_bands(self, audio: np.ndarray) -> dict[str, float]:
        n = len(audio)
        window = np.hanning(n)
        fft_mag = np.abs(np.fft.rfft(audio * window))
        freqs = np.fft.rfftfreq(n, d=1.0 / self._sr)

        bands: dict[str, float] = {}
        for band_name, (lo, hi) in BAND_EDGES.items():
            mask = (freqs >= lo) & (freqs < hi)
            if not np.any(mask):
                bands[band_name] = -90.0
                continue
            energy = float(np.mean(fft_mag[mask] ** 2))
            if energy <= 0:
                bands[band_name] = -90.0
            else:
                bands[band_name] = max(-90.0, 10 * math.log10(energy))
        return bands

    def _silent_result(self, ts: float) -> RoomAnalysis:
        bands = {b: -90.0 for b in BAND_NAMES}
        return RoomAnalysis(
            lufs=-70.0,
            rms_db=-90.0,
            bands=bands,
            band_delta={b: 0.0 for b in BAND_NAMES},
            lufs_delta=0.0,
            timestamp=ts,
        )
