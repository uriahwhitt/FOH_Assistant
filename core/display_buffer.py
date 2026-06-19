"""Thread-safe shared state between the analysis loop and the display window.

Main thread writes via update(). Display thread reads via snapshot().
All spectrum arrays are 31-point 1/3-octave bands.
"""

import threading
import numpy as np
from dataclasses import dataclass, field

from core.third_octave import N_THIRD_OCTAVE


@dataclass
class DisplayBuffer:
    # Analysis-path curves — 31-point 1/3-octave (updated every 500ms)
    board_rta_bands:    np.ndarray = field(default_factory=lambda: np.zeros(N_THIRD_OCTAVE))
    mic_bands:          np.ndarray = field(default_factory=lambda: np.zeros(N_THIRD_OCTAVE))
    genre_target_bands: np.ndarray = field(default_factory=lambda: np.zeros(N_THIRD_OCTAVE))

    # Fast-path curves — 31-point (updated every 50–100ms, falls back to above)
    board_rta_fast: np.ndarray = field(default_factory=lambda: np.zeros(N_THIRD_OCTAVE))
    mic_shape_fast: np.ndarray = field(default_factory=lambda: np.zeros(N_THIRD_OCTAVE))

    # Band data — mic deviation from genre target (drives highlight colors)
    band_highlights: dict = field(default_factory=dict)  # band → deviation_db
    band_peaks:      dict = field(default_factory=dict)  # band → (peak_hz, prominence_db)

    # Metadata
    song_name:  str   = ""
    genre_name: str   = ""
    lufs:       float = -60.0
    is_silent:  bool  = True

    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, **kwargs) -> None:
        """Write new values atomically. Called from main thread."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k) and not k.startswith('_'):
                    setattr(self, k, v)

    def snapshot(self) -> dict:
        """Read all values atomically. Called from display thread."""
        with self._lock:
            return {
                'board_rta_bands':    self.board_rta_bands.copy(),
                'board_rta_fast':     self.board_rta_fast.copy(),
                'mic_bands':          self.mic_bands.copy(),
                'mic_shape_fast':     self.mic_shape_fast.copy(),
                'genre_target_bands': self.genre_target_bands.copy(),
                'band_highlights':    dict(self.band_highlights),
                'band_peaks':         dict(self.band_peaks),
                'song_name':          self.song_name,
                'genre_name':         self.genre_name,
                'lufs':               self.lufs,
                'is_silent':          self.is_silent,
            }
