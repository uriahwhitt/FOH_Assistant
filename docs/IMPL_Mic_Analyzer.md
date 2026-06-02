# FOH Assistant — Reference Microphone Analysis Implementation
**Document Type:** Claude Code Implementation Reference  
**Phase:** 2  
**Last Updated:** 2026-05-12  
**Depends on:** IMPL_Geometry.md (venue acoustic corrections)  
**Produces:** `core/mic_analyzer.py` (new), `core/audio_capture.py` (extended)

---

## Purpose

This document specifies the complete reference microphone analysis pipeline.
The microphone is an equal-weight measurement instrument alongside the X32 board data.
It serves three distinct functions:

1. **LUFS sanity check** — Is overall room loudness on target for the genre?
2. **Spectral ground truth** — What does the room actually hear vs what the board predicts?
3. **Input state event characterization** — When the board detects an energy change on a
   channel, the mic FFT identifies which frequency region absorbed that energy,
   enabling instrument prior state switching (e.g. guitar → solo mode).

The mic analyzer is geometry-aware. Raw FFT output is corrected for known acoustic
artifacts (inter-speaker comb filtering, room modes, ground reflections) before
any comparison or recommendation logic runs. See IMPL_Geometry.md for the correction
curve calculations.

---

## 1. Hardware Configuration

### Phase 2 Target Hardware
- **Microphone:** Audio Technica AT2035 cardioid condenser
- **Interface:** PreSonus Studio 26c USB audio interface
- **Connection:** USB to laptop
- **Placement:** Mid-room, ear height (1.4–1.6m), audience position, facing stage

### Phase 1 Fallback (still supported)
- **Microphone:** DJI Mic 2
- **Interface:** DJI USB receiver

### Device Detection

The system must auto-detect the correct audio interface at startup.
Primary match is AT2035 via PreSonus. Fallback to DJI. Fallback to first
available input device with user warning.

```python
DEVICE_PRIORITY = [
    {'match': 'PreSonus', 'label': 'AT2035 via PreSonus Studio 26c'},
    {'match': 'Studio 26', 'label': 'AT2035 via PreSonus Studio 26c'},
    {'match': 'DJI',       'label': 'DJI Mic 2 USB Receiver'},
    {'match': 'CABLE',     'label': 'VB-Audio Virtual Cable (test mode)'},
]

def detect_audio_device() -> tuple[int, str, int]:
    """
    Returns (device_index, label, sample_rate).
    Raises RuntimeError if no suitable device found.
    """
    import sounddevice as sd
    devices = sd.query_devices()
    
    for priority_entry in DEVICE_PRIORITY:
        for idx, device in enumerate(devices):
            if (priority_entry['match'].lower() in device['name'].lower()
                    and device['max_input_channels'] > 0):
                sample_rate = int(device['default_samplerate'])
                return idx, priority_entry['label'], sample_rate
    
    raise RuntimeError(
        "No suitable audio input device found.\n"
        "Run with --devices to list available devices."
    )
```

### Supported Sample Rates

Accept 44100Hz or 48000Hz. All analysis is sample-rate-agnostic because
FFT results are converted to Hz before any further processing.

---

## 2. Audio Capture

### 2.1 Buffer Architecture

```python
class AudioCapture:
    """
    Manages continuous audio capture from reference microphone.
    Maintains a rolling buffer sized to support both LUFS measurement
    and high-resolution FFT analysis.
    """
    
    BUFFER_SECONDS = 3.0          # 3 seconds rolling — covers LUFS integration
    FFT_WINDOW_SECONDS = 0.5      # 500ms window for FFT analysis
    
    def __init__(self, device_index: int, sample_rate: int):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.buffer_size = int(self.BUFFER_SECONDS * sample_rate)
        self.fft_window_size = int(self.FFT_WINDOW_SECONDS * sample_rate)
        
        self._buffer = np.zeros(self.buffer_size, dtype=np.float32)
        self._stream = None
        self._lock = threading.Lock()
    
    def get_analysis_window(self) -> np.ndarray:
        """Return the most recent FFT_WINDOW_SECONDS of audio."""
        with self._lock:
            return self._buffer[-self.fft_window_size:].copy()
    
    def get_lufs_window(self) -> np.ndarray:
        """Return full 3-second buffer for LUFS measurement."""
        with self._lock:
            return self._buffer.copy()
```

### 2.2 Callback

```python
    def _audio_callback(self, indata, frames, time, status):
        """sounddevice callback — called on audio thread."""
        if status:
            print(f"[AUDIO] {status}")
        
        with self._lock:
            samples = indata[:, 0].astype(np.float32)  # mono
            self._buffer = np.roll(self._buffer, -len(samples))
            self._buffer[-len(samples):] = samples
```

---

## 3. Spectral Analysis Pipeline

### 3.1 Shared Frequency Axis

The mic analyzer uses the same `FREQ_AXIS` as the channel model (1000 log-spaced
points from 20Hz to 20kHz). This is essential — the forward model comparison
requires both the predicted curve and the measured curve to be on identical axes.

```python
from core.channel_model import FREQ_AXIS, N_FREQS
```

### 3.2 Welch's Method FFT

Welch's method averages multiple overlapping FFT windows, reducing variance
(random noise) while preserving systematic spectral features. This is the
correct approach for steady-state audio analysis. A single FFT window
captures transient noise; Welch's method gives a stable, reliable spectrum.

```python
import scipy.signal
import numpy as np

def compute_welch_spectrum(audio_buffer: np.ndarray,
                            sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Compute power spectral density using Welch's method.
    
    Returns:
        freqs_hz: frequency array (Hz)
        psd_db:   power spectral density in dBFS/Hz
    
    Window size of 4096 samples at 48kHz = ~85ms per segment.
    With 50% overlap, a 500ms buffer gives ~11 averaged segments.
    This provides good frequency resolution (~12Hz at low end) with
    significantly reduced variance vs single-window FFT.
    """
    nperseg = 4096
    noverlap = nperseg // 2
    
    freqs, psd = scipy.signal.welch(
        audio_buffer,
        fs=sample_rate,
        window='hann',
        nperseg=nperseg,
        noverlap=noverlap,
        scaling='density'
    )
    
    # Convert to dBFS (reference: full scale = 0 dBFS)
    psd_db = 10.0 * np.log10(np.maximum(psd, 1e-12))
    
    return freqs, psd_db
```

### 3.3 Interpolation to FREQ_AXIS

```python
def interpolate_to_freq_axis(freqs_hz: np.ndarray,
                               spectrum_db: np.ndarray) -> np.ndarray:
    """
    Interpolate spectrum from Welch output frequencies to shared FREQ_AXIS.
    Uses log-frequency interpolation (perceptually correct).
    Clamps to valid range — no extrapolation beyond input freq range.
    """
    # Only interpolate within the valid frequency range of the input
    valid_mask = (freqs_hz >= FREQ_AXIS[0]) & (freqs_hz <= FREQ_AXIS[-1])
    
    if valid_mask.sum() < 2:
        return np.full(N_FREQS, -90.0)
    
    log_freqs_in  = np.log10(freqs_hz[valid_mask])
    log_freq_axis = np.log10(FREQ_AXIS)
    
    interpolated = np.interp(
        log_freq_axis,
        log_freqs_in,
        spectrum_db[valid_mask],
        left=spectrum_db[valid_mask][0],
        right=spectrum_db[valid_mask][-1]
    )
    
    return interpolated
```

### 3.4 Exponential Moving Average Smoothing

EMA smoothing reduces frame-to-frame jitter without introducing the lag
that a simple moving average would cause. Alpha controls the trade-off:
lower alpha = smoother but slower to respond. 0.3 is a reasonable starting point.

```python
class EMAState:
    """Maintains exponential moving average state across analysis cycles."""
    
    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self._state: Optional[np.ndarray] = None
    
    def update(self, new_spectrum: np.ndarray) -> np.ndarray:
        """
        Apply EMA smoothing. First call initializes state.
        Returns smoothed spectrum.
        """
        if self._state is None:
            self._state = new_spectrum.copy()
            return self._state.copy()
        
        self._state = self.alpha * new_spectrum + (1.0 - self.alpha) * self._state
        return self._state.copy()
    
    def reset(self):
        """Reset state — call at song transitions."""
        self._state = None
```

### 3.5 Peak Detection Within Bands

Peak detection finds the frequency of maximum energy within each analysis band.
This is more informative than band average energy for identifying specific
problem frequencies that warrant EQ recommendations.

```python
# Analysis bands (8 bands — tighter midrange split per IMP-024)
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

def compute_band_levels(spectrum_db: np.ndarray) -> dict:
    """
    Compute energy summary per analysis band.
    Returns dict with band_name -> BandAnalysis.
    """
    results = {}
    
    for band_name, freq_low, freq_high in ANALYSIS_BANDS:
        mask = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
        
        if not mask.any():
            continue
        
        band_spectrum = spectrum_db[mask]
        band_freqs    = FREQ_AXIS[mask]
        
        # Convert dB to linear for proper energy averaging
        band_linear = 10.0 ** (band_spectrum / 10.0)
        avg_db  = 10.0 * np.log10(np.maximum(np.mean(band_linear), 1e-12))
        peak_db = np.max(band_spectrum)
        peak_hz = band_freqs[np.argmax(band_spectrum)]
        
        results[band_name] = {
            'avg_db':  avg_db,
            'peak_db': peak_db,
            'peak_hz': peak_hz,
        }
    
    return results
```

---

## 4. LUFS Measurement

```python
import pyloudnorm as pyln

def compute_lufs(audio_buffer: np.ndarray, sample_rate: int) -> float:
    """
    Compute integrated LUFS per ITU-R BS.1770-4.
    
    audio_buffer should be at least 400ms for gating to function correctly.
    3 seconds (AudioCapture.BUFFER_SECONDS) is the recommended minimum.
    
    Returns -inf (represented as -70.0) if buffer is below gating threshold.
    """
    meter = pyln.Meter(sample_rate)
    
    try:
        loudness = meter.integrated_loudness(audio_buffer)
        # pyloudnorm returns -inf for silence — clamp to -70
        if loudness == float('-inf') or np.isnan(loudness):
            return -70.0
        return float(loudness)
    except Exception:
        return -70.0
```

---

## 5. Silence Detection

All analysis suppresses recommendations when the room is silent.
The threshold accounts for ambient noise in a live venue.

```python
ROOM_SILENCE_THRESHOLD_LUFS = -50.0   # Below this = no performance happening

def is_room_silent(lufs: float, band_levels: dict) -> bool:
    """
    Returns True if the room is silent (between songs, before show, etc.)
    Uses LUFS as primary gate with band level confirmation.
    """
    if lufs < ROOM_SILENCE_THRESHOLD_LUFS:
        return True
    
    # Secondary check: if all bands are below noise floor, confirm silence
    # (handles cases where LUFS integration hasn't settled yet)
    if all(b['avg_db'] < -55.0 for b in band_levels.values()):
        return True
    
    return False
```

---

## 6. Geometry Correction

The geometry correction is computed by the venue acoustics module (IMPL_Geometry.md)
and applied here as a simple additive correction to the raw spectrum.

```python
class MicAnalyzer:
    
    def __init__(self, venue_acoustics):
        """
        venue_acoustics: VenueAcoustics instance from geometry module.
        Provides correction_curve_db (np.ndarray on FREQ_AXIS) and
        room_mode_mask (boolean np.ndarray on FREQ_AXIS).
        """
        self.venue_acoustics = venue_acoustics
        self.correction_curve_db = venue_acoustics.mic_correction_curve()
        self.room_mode_mask = venue_acoustics.room_mode_mask()
        self.ema = EMAState(alpha=0.3)
        self._prev_spectrum: Optional[np.ndarray] = None
    
    def analyze(self, audio_capture: AudioCapture) -> 'MicAnalysis':
        """
        Full analysis pipeline. Call once per analysis cycle (500ms).
        """
        sample_rate = audio_capture.sample_rate
        
        # 1. Get audio windows
        fft_window  = audio_capture.get_analysis_window()
        lufs_window = audio_capture.get_lufs_window()
        
        # 2. LUFS
        lufs = compute_lufs(lufs_window, sample_rate)
        
        # 3. Welch FFT
        freqs_hz, raw_psd_db = compute_welch_spectrum(fft_window, sample_rate)
        
        # 4. Interpolate to shared FREQ_AXIS
        raw_spectrum_db = interpolate_to_freq_axis(freqs_hz, raw_psd_db)
        
        # 5. Apply geometry correction
        corrected_spectrum_db = raw_spectrum_db + self.correction_curve_db
        
        # 6. EMA smoothing
        smoothed_spectrum_db = self.ema.update(corrected_spectrum_db)
        
        # 7. Band energy summary
        band_levels = compute_band_levels(smoothed_spectrum_db)
        
        # 8. Silence check
        silent = is_room_silent(lufs, band_levels)
        
        # 9. Room mode tagging
        room_mode_flags = self.room_mode_mask & (smoothed_spectrum_db > -30.0)
        
        # 10. Spectral delta vs previous cycle (for input state detection)
        if self._prev_spectrum is not None:
            spectral_delta = smoothed_spectrum_db - self._prev_spectrum
        else:
            spectral_delta = np.zeros(N_FREQS)
        self._prev_spectrum = smoothed_spectrum_db.copy()
        
        return MicAnalysis(
            lufs=lufs,
            raw_spectrum_db=raw_spectrum_db,
            corrected_spectrum_db=corrected_spectrum_db,
            smoothed_spectrum_db=smoothed_spectrum_db,
            spectral_delta_db=spectral_delta,
            band_levels=band_levels,
            room_mode_flags=room_mode_flags,
            correction_applied_db=self.correction_curve_db,
            is_silent=silent,
            timestamp_ms=current_time_ms()
        )
    
    def reset_ema(self):
        """Call at song transitions to prevent smearing across songs."""
        self.ema.reset()
        self._prev_spectrum = None
```

---

## 7. Input State Event Detection (Mic Side)

When the board (osc_client) detects an RMS spike on a guitar channel,
this method is called to characterize the spectral nature of the event.

```python
def characterize_input_event(self,
                               pre_event_snapshot: np.ndarray,
                               post_event_snapshot: np.ndarray) -> dict:
    """
    Called when board detects an input state change (e.g. solo onset).
    
    pre_event_snapshot:  smoothed_spectrum_db from cycle before event
    post_event_snapshot: smoothed_spectrum_db from cycle after event
    
    Returns characterization dict for INPUT_STATE_EVENT log entry.
    """
    delta = post_event_snapshot - pre_event_snapshot
    
    # Spectral centroid shift
    # Convert dB to linear for centroid calculation
    pre_linear  = 10.0 ** (pre_event_snapshot / 10.0)
    post_linear = 10.0 ** (post_event_snapshot / 10.0)
    
    pre_centroid  = np.sum(FREQ_AXIS * pre_linear)  / np.sum(pre_linear)
    post_centroid = np.sum(FREQ_AXIS * post_linear) / np.sum(post_linear)
    centroid_shift_hz = post_centroid - pre_centroid
    
    # Which band gained most energy?
    band_deltas = {}
    for band_name, freq_low, freq_high in ANALYSIS_BANDS:
        mask = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
        if mask.any():
            band_deltas[band_name] = float(np.mean(delta[mask]))
    
    dominant_band = max(band_deltas, key=lambda k: band_deltas[k])
    
    # Is shift upward (solo) or downward/uniform (rhythm boost)?
    spectral_shift_direction = 'upward' if centroid_shift_hz > 200 else \
                                'downward' if centroid_shift_hz < -200 else 'neutral'
    
    return {
        'centroid_shift_hz':      float(centroid_shift_hz),
        'spectral_shift_direction': spectral_shift_direction,
        'dominant_band':          dominant_band,
        'band_deltas_db':         band_deltas,
        'mic_confirmed_change':   abs(centroid_shift_hz) > 100 or
                                  abs(max(band_deltas.values())) > 1.0,
    }
```

---

## 8. Data Model

```python
from dataclasses import dataclass, field
from typing import Optional
import numpy as np

@dataclass
class MicAnalysis:
    """Complete mic analysis result for one 500ms cycle."""
    
    # LUFS
    lufs: float
    
    # Spectra (all on FREQ_AXIS, shape (N_FREQS,), dBFS)
    raw_spectrum_db: np.ndarray         # Welch output, no correction
    corrected_spectrum_db: np.ndarray   # + geometry correction
    smoothed_spectrum_db: np.ndarray    # + EMA smoothing (primary analysis signal)
    spectral_delta_db: np.ndarray       # change vs previous cycle
    
    # Band summary
    band_levels: dict                   # band_name -> {avg_db, peak_db, peak_hz}
    
    # Acoustic metadata
    room_mode_flags: np.ndarray         # bool array — True where room mode predicted
    correction_applied_db: np.ndarray   # correction curve used this cycle
    
    # State
    is_silent: bool
    timestamp_ms: float
    
    @property
    def spectral_centroid_hz(self) -> float:
        """Frequency center of mass of the corrected spectrum."""
        linear = 10.0 ** (self.smoothed_spectrum_db / 10.0)
        total  = np.sum(linear)
        if total < 1e-12:
            return 0.0
        return float(np.sum(FREQ_AXIS * linear) / total)
```

---

## 9. Snapshot System for Event Logging

The analyzer maintains a ring buffer of recent spectrum snapshots.
This enables logging pre/post event spectra for INPUT_STATE_EVENTs
without blocking the analysis thread.

```python
class SpectrumHistory:
    """
    Ring buffer of recent spectrum snapshots.
    Used to retrieve pre-event spectrum when a board event is detected.
    """
    
    HISTORY_DEPTH = 20   # 20 × 500ms = 10 seconds of history
    
    def __init__(self):
        self._history = []
    
    def push(self, analysis: MicAnalysis):
        self._history.append(analysis)
        if len(self._history) > self.HISTORY_DEPTH:
            self._history.pop(0)
    
    def get_snapshot_before(self, event_timestamp_ms: float,
                              offset_ms: float = 500.0) -> Optional[np.ndarray]:
        """
        Return the spectrum snapshot closest to (event_timestamp - offset_ms).
        Used to capture pre-event state for INPUT_STATE_EVENT logging.
        """
        target_ms = event_timestamp_ms - offset_ms
        
        if not self._history:
            return None
        
        closest = min(
            self._history,
            key=lambda a: abs(a.timestamp_ms - target_ms)
        )
        return closest.smoothed_spectrum_db.copy()
```

---

## 10. Extended Logger Events

The following new event types require mic analysis data.
See IMPL_Forward_Mix_Model.md Section 6 for full schema.

### ANALYSIS_CYCLE (every 500ms during show)
Include:
- `mic.lufs`
- `mic.band_levels` (all 8 bands, avg_db and peak_db each)
- `mic.spectral_centroid_hz`
- `mic.room_mode_flags` (as list of band names where flags are active)
- `mic.is_silent`
- `mic.correction_applied` (true/false — was geometry correction loaded)

### INPUT_STATE_EVENT (on guitar channel state change)
Include full characterization dict from `characterize_input_event()`.

### SOUNDCHECK_COMPLETE
Include:
- `mic.lufs` at confirm moment
- `mic.band_levels` at confirm moment
- `mic.spectral_centroid_hz` at confirm moment

---

## 11. audio_capture.py Extension Checklist

- [ ] Add `detect_audio_device()` with PreSonus/AT2035 priority matching
- [ ] Update session header to show detected device name and sample rate
- [ ] Support both 44100Hz and 48000Hz sample rates
- [ ] Add `get_analysis_window()` (500ms) and `get_lufs_window()` (3s) methods
- [ ] Add device disconnect handler — log WARNING, attempt reconnect × 3,
      then continue with degraded mode (LUFS only from last valid reading)

---

## 12. Testing

### Sine Wave Validation
Feed a 1kHz sine wave via VB-Audio virtual cable.
Expected: single narrow peak at 1kHz in Welch output, all other bands near noise floor.
Confirms FFT and interpolation are working correctly.

### Pink Noise Validation
Feed pink noise (equal energy per octave = flat on log scale).
Expected: all 8 bands should read within ~2dB of each other after correction.
Confirms band energy calculation is correct.

### Geometry Correction Validation
With geometry correction enabled, feed pink noise.
Corrected spectrum should be flatter than raw spectrum if correction is non-zero.
Log both `raw_spectrum_db` and `corrected_spectrum_db` and compare.

### AT2035 vs DJI Comparison
At June 13 show, log raw (uncorrected) spectra from first 30 minutes.
Compare to Phase 1 DJI Mic 2 log from May 9 (same venue, similar material).
AT2035 should show more consistent readings and less variance per cycle.

---

*Reference documents: IMPL_X32_Board_Model.md, IMPL_Geometry.md, IMPL_Forward_Mix_Model.md*
