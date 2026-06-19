"""
1/3-octave band averaging for display.
Converts a 1000-point FREQ_AXIS spectrum to 31 standard 1/3-octave bands.
Used for display only — analysis engine uses full resolution spectrum.
"""

import numpy as np
from core.channel_model import FREQ_AXIS

# Standard 31 1/3-octave center frequencies (ISO 266)
THIRD_OCTAVE_CENTERS = np.array([
    20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
    630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
    10000, 12500, 16000, 20000,
], dtype=float)

N_THIRD_OCTAVE = len(THIRD_OCTAVE_CENTERS)   # 31

# Factor for 1/3-octave band edges: f_center × 2^(±1/6)
_EDGE_FACTOR = 2.0 ** (1.0 / 6.0)

# Precompute band edge masks on FREQ_AXIS (done once at import)
_BAND_MASKS = []
for _fc in THIRD_OCTAVE_CENTERS:
    _lo = _fc / _EDGE_FACTOR
    _hi = _fc * _EDGE_FACTOR
    _BAND_MASKS.append((FREQ_AXIS >= _lo) & (FREQ_AXIS < _hi))


def to_third_octave(spectrum_db: np.ndarray) -> np.ndarray:
    """
    Average a 1000-point FREQ_AXIS spectrum into 31 1/3-octave bands.

    Parameters
    ----------
    spectrum_db : ndarray, shape (1000,)
        Spectrum on FREQ_AXIS — normalized or raw dB values.

    Returns
    -------
    ndarray, shape (31,)
        Mean energy in each 1/3-octave band in dB.
        Bands with no FREQ_AXIS bins return -90.0.
    """
    result = np.full(N_THIRD_OCTAVE, -90.0)
    for i, mask in enumerate(_BAND_MASKS):
        if mask.any():
            result[i] = float(np.mean(spectrum_db[mask]))
    return result


def normalize_third_octave(bands_db: np.ndarray) -> np.ndarray:
    """
    Subtract mean to produce a shape-only representation.
    Same as normalize_to_shape() but for 31-band arrays.
    """
    return bands_db - float(np.mean(bands_db))
