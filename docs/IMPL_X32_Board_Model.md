# FOH Assistant — X32 Board Model Implementation
**Document Type:** Claude Code Implementation Reference  
**Phase:** 2  
**Last Updated:** 2026-05-26  
**Depends on:** X32_OSC_Reference.md, FOH_Assistant_Phase1_Implementation.md  
**Produces:** `core/osc_client.py` (extended), `core/channel_model.py` (new), `models/channel.py` (extended)

---

## Purpose

This document specifies the complete X32 data acquisition and channel contribution model.
The goal is to compute, for every active channel, a frequency-resolved contribution curve
representing that channel's actual spectral output into the mix at every point in time.

This curve is the primary input to the Forward Mix Model (see IMPL_Forward_Mix_Model.md).

---

## 1. Data Architecture — Two Layers

Channel state is split into two distinct data structures with different update rates.

### ChannelConfig — Static Signal Chain
Updated only when the engineer touches the board (via `/xremote` push) or at startup.
Represents the configured signal chain: EQ, HPF, fader position, compressor settings.

### ChannelMeterState — Real-Time Dynamics
Updated every 50ms via `/meters/1` and `/meters/6` subscriptions.
Represents what is actually happening right now: energy levels, gate behavior,
compressor gain reduction, input state.

These two structures are combined in the contribution curve calculation.
Keeping them separate means EQ transfer functions are only recomputed when
configuration actually changes — not on every 50ms meter cycle.

---

## 2. Extended OSC Data Acquisition

### 2.1 What to Read at Startup (Full Channel Snapshot)

For each active channel (1–14 per band.yaml channel map), read via `/node`:

```
/node ,s ch/{nn}/config     → name, color
/node ,s ch/{nn}/preamp     → trim, invert, hpon (phantom), hpf, hpslope
/node ,s ch/{nn}/eq         → eq/on, then all 4 bands (type, f, g, q each)
/node ,s ch/{nn}/mix        → fader, on/off, pan
/node ,s ch/{nn}/dyn        → on, mode, thr, ratio, attack, release, mgain
/node ,s ch/{nn}/gate       → on, thr, range, attack, release, mode
/node ,s main/st/mix        → main LR fader, on/off
```

**Important:** `/node` for `ch/{nn}/eq` returns the global EQ on/off status plus
all 4 bands' type/freq/gain/q in one response. Parse carefully — see Section 2.4.

### 2.2 Meter Subscriptions (Real-Time)

Start two subscriptions at session init. Renew both every 8 seconds.

**Subscription 1 — All channel RMS (primary):**
```
/batchsubscribe ,ssiii /foh_ch_meters /meters/1 0 0 1
```
Returns 96 floats as blob every 50ms:
- `[0..31]` — 32 channel input RMS levels (linear 0.0–1.0, ch1=index0)
- `[32..63]` — 32 gate gain reductions (linear)
- `[64..95]` — 32 dynamics gain reductions (linear)

**Subscription 2 — Main bus RTA (forward model validation):**
```
/batchsubscribe ,ssiii /foh_rta /meters/15 1 0 1
```
Returns 100 frequency bands (20Hz–18.66kHz) as short ints.
See Section 2.5 for parse details.

**Single-channel detail meter (request on demand, not subscribed):**
```
/meters ,si /meters/6 {channel_id_0based}
```
Returns 4 floats: [pre_fade, gate_gr, dyn_gr, post_fade]
Request this for any channel where input state change is detected.

### 2.3 OSC Address Quick Reference (Full)

```
# Preamp
/ch/{nn}/preamp/trim        float   [-18.0, +18.0] dB
/ch/{nn}/preamp/invert      int     {0=normal, 1=inverted}
/ch/{nn}/preamp/hpon        int     {0=off, 1=on}  ← PHANTOM POWER, not HPF
/ch/{nn}/preamp/hpf         float   [20.0, 400.0] Hz (log scale)
/ch/{nn}/preamp/hpslope     int     {0=12dB/oct, 1=18dB/oct, 2=24dB/oct}

# EQ
/ch/{nn}/eq/on              int     {0=bypass, 1=active}
/ch/{nn}/eq/{b}/type        int     {0=LCut, 1=LShv, 2=PEQ, 3=VEQ, 4=HShv, 5=HCut}
/ch/{nn}/eq/{b}/f           float   [0.0, 1.0] log scale → Hz (see conversion)
/ch/{nn}/eq/{b}/g           float   [-15.0, +15.0] dB
/ch/{nn}/eq/{b}/q           float   [0.3, 10.0] log scale

# Mix
/ch/{nn}/mix/fader          float   [0.0, 1.0] → dB (see piecewise conversion)
/ch/{nn}/mix/on             int     {0=MUTED, 1=ACTIVE} ← 0 means muted
/ch/{nn}/mix/pan            float   [-100.0, +100.0]

# Dynamics
/ch/{nn}/dyn/on             int     {0=off, 1=on}
/ch/{nn}/dyn/thr            float   [-60.0, 0.0] dB
/ch/{nn}/dyn/ratio          int     {0=1.1, 1=1.3, 2=1.5, 3=2.0, 4=2.5, 5=3.0,
                                      6=4.0, 7=5.0, 8=7.0, 9=10, 10=20, 11=100}
/ch/{nn}/dyn/attack         float   [0.0, 120.0] ms
/ch/{nn}/dyn/release        float   [5.0, 4000.0] ms (log)
/ch/{nn}/dyn/mgain          float   [0.0, 24.0] dB

# Gate
/ch/{nn}/gate/on            int     {0=off, 1=on}
/ch/{nn}/gate/thr           float   [-80.0, 0.0] dB
/ch/{nn}/gate/range         float   [3.0, 60.0] dB
/ch/{nn}/gate/mode          int     {0=EXP2, 1=EXP3, 2=EXP4, 3=GATE, 4=DUCK}

# Main bus
/main/st/mix/fader          float   [0.0, 1.0] → dB
/main/st/mix/on             int     {0=MUTED, 1=ACTIVE}
```

**HPF vs Phantom Power disambiguation:**
`/preamp/hpon` is PHANTOM POWER (+48V). It is NOT the high-pass filter.
The HPF is `/preamp/hpf` (frequency) and `/preamp/hpslope` (slope).
This was the source of IMP-026 false negatives in Phase 1. Do not confuse them.

### 2.4 EQ Float Conversion

EQ frequency is stored as a float [0.0, 1.0] on a log scale. Always convert before use:

```python
import math

FREQ_MIN = 20.0
FREQ_MAX = 20000.0

def eq_float_to_hz(f: float) -> float:
    """Convert X32 EQ freq float [0.0, 1.0] to Hz."""
    log_min = math.log10(FREQ_MIN)
    log_max = math.log10(FREQ_MAX)
    return 10 ** (log_min + f * (log_max - log_min))

def fader_float_to_db(f: float) -> float:
    """Convert X32 fader float [0.0, 1.0] to dB. Piecewise linear."""
    if f >= 0.5:
        return f * 40.0 - 30.0        # -10 to +10 dB
    elif f >= 0.25:
        return f * 80.0 - 50.0        # -30 to -10 dB
    elif f >= 0.0625:
        return f * 160.0 - 70.0       # -60 to -30 dB
    elif f > 0.0:
        return f * 480.0 - 90.0       # -90 to -60 dB
    else:
        return -90.0

def hpslope_int_to_db_oct(slope_int: int) -> int:
    """Convert X32 hpslope enum to dB/octave value."""
    return {0: 12, 1: 18, 2: 24}.get(slope_int, 12)

COMP_RATIO_MAP = {
    0: 1.1, 1: 1.3, 2: 1.5, 3: 2.0, 4: 2.5,
    5: 3.0, 6: 4.0, 7: 5.0, 8: 7.0, 9: 10.0, 10: 20.0, 11: 100.0
}
```

### 2.5 Meter Blob Parsing

```python
import struct
import numpy as np

def parse_meters_1(blob_data: bytes) -> dict:
    """
    Parse /meters/1 blob.
    Returns per-channel RMS, gate GR, dynamics GR as linear floats [0.0, 1.0+].
    Convert to dBFS with linear_to_dbfs().
    """
    num_floats = struct.unpack_from('<I', blob_data, 4)[0]
    floats = struct.unpack_from(f'<{num_floats}f', blob_data, 8)
    return {
        'channel_rms':    list(floats[0:32]),    # ch1=index0, ch32=index31
        'gate_gr':        list(floats[32:64]),
        'dynamics_gr':    list(floats[64:96]),
    }

def parse_meters_15(blob_data: bytes) -> np.ndarray:
    """
    Parse /meters/15 RTA blob.
    Returns array of 100 dBFS values corresponding to RTA_FREQS_HZ.
    """
    num_ints = struct.unpack_from('<I', blob_data, 4)[0]
    raw_shorts = struct.unpack_from(f'<{num_ints}h', blob_data, 8)
    # Convert: short_int / 256.0 gives dBFS in range [-128.0, 0.0]
    return np.array([s / 256.0 for s in raw_shorts[:100]])

def parse_meters_6(blob_data: bytes) -> dict:
    """
    Parse /meters/6 single-channel strip meter.
    Returns pre_fade, gate_gr, dyn_gr, post_fade as linear floats.
    """
    num_floats = struct.unpack_from('<I', blob_data, 4)[0]
    floats = struct.unpack_from(f'<{num_floats}f', blob_data, 8)
    return {
        'pre_fade':  floats[0],
        'gate_gr':   floats[1],
        'dyn_gr':    floats[2],
        'post_fade': floats[3],
    }

def linear_to_dbfs(linear: float) -> float:
    """Convert X32 meter linear float to dBFS."""
    if linear <= 0:
        return -90.0
    return 20.0 * math.log10(max(linear, 1e-9))

# RTA frequency axis — 100 bands (Hz)
RTA_FREQS_HZ = [
    20, 21, 22, 24, 26, 28, 30, 32, 34, 36, 39, 42, 45, 48, 52, 55, 59,
    63, 68, 73, 78, 84, 90, 96, 103, 110, 118, 127, 136, 146, 156, 167,
    179, 192, 206, 221, 237, 254, 272, 292, 313, 335, 359, 385, 412, 442,
    474, 508, 544, 583, 625, 670, 718, 769, 825, 884, 947, 1020, 1090,
    1170, 1250, 1340, 1440, 1540, 1650, 1770, 1890, 2030, 2180, 2330,
    2500, 2680, 2870, 3080, 3300, 3540, 3790, 4060, 4350, 4670, 5000,
    5360, 5740, 6160, 6600, 7070, 7580, 8120, 8710, 9330, 10000, 10720,
    11490, 12310, 13200, 14140, 15160, 16250, 17410, 18660
]
```

---

## 3. Data Models

### 3.1 EQBand Dataclass

```python
from dataclasses import dataclass, field
from typing import Optional
import numpy as np

EQ_TYPE_NAMES = {
    0: 'LCut', 1: 'LShv', 2: 'PEQ', 3: 'VEQ', 4: 'HShv', 5: 'HCut'
}

@dataclass
class EQBand:
    band_num: int          # 1–4
    type_int: int          # raw X32 enum 0–5
    freq_hz: float         # converted from X32 log float
    gain_db: float         # [-15, +15]
    q: float               # [0.3, 10.0]
    
    @property
    def type_name(self) -> str:
        return EQ_TYPE_NAMES.get(self.type_int, 'PEQ')
    
    @property
    def is_filter(self) -> bool:
        """True for LCut and HCut — these have no gain parameter."""
        return self.type_int in (0, 5)
```

### 3.2 ChannelConfig Dataclass

```python
@dataclass
class ChannelConfig:
    """
    Complete static signal chain for one channel.
    Recomputed on startup and whenever engineer changes EQ, HPF, or fader.
    """
    channel_num: int
    label: str
    instrument_type: str       # from band.yaml: kick, snare, bass_di, guitar,
                               # guitar_lead, keys, vocal_lead, vocal_bkg, overhead

    # Preamp
    trim_db: float
    polarity_inverted: bool
    hpf_enabled: bool
    hpf_freq_hz: float
    hpf_slope_db_oct: int      # 12, 18, or 24

    # EQ
    eq_enabled: bool
    eq_bands: list             # list of EQBand (4 bands)

    # Fader and routing
    fader_db: float
    muted: bool
    pan: float                 # [-100, +100]

    # Dynamics
    comp_enabled: bool
    comp_threshold_db: float
    comp_ratio: float
    comp_attack_ms: float
    comp_release_ms: float
    comp_makeup_db: float

    # Gate
    gate_enabled: bool
    gate_threshold_db: float
    gate_range_db: float

    # Derived curves — computed by channel_model.py after config is loaded.
    # These are np.ndarray of shape (N_FREQS,) in dB, on FREQ_AXIS.
    # Set to None until compute_transfer_curves() is called.
    hpf_curve_db: Optional[np.ndarray] = field(default=None, repr=False)
    eq_curve_db: Optional[np.ndarray] = field(default=None, repr=False)
    transfer_curve_db: Optional[np.ndarray] = field(default=None, repr=False)

    # Metadata
    last_config_update: float = 0.0   # unix timestamp
```

### 3.3 ChannelMeterState Dataclass

```python
@dataclass
class ChannelMeterState:
    """
    Real-time meter readings for one channel. Updated every 50ms.
    """
    channel_num: int
    timestamp_ms: float

    # From /meters/1 blob
    input_rms_linear: float       # pre-fader input RMS
    gate_gr_linear: float         # gate gain reduction (1.0 = no reduction)
    dyn_gr_linear: float          # compressor gain reduction

    # From /meters/6 (requested on state change events)
    pre_fade_linear: float = 1.0
    post_fade_linear: float = 1.0

    # Derived — computed on update
    input_rms_db: float = -90.0
    gate_gr_db: float = 0.0       # always <= 0
    dyn_gr_db: float = 0.0        # always <= 0
    post_fade_db: float = -90.0
    effective_gr_db: float = 0.0  # gate_gr_db + dyn_gr_db

    # Input state — inferred from meter history
    rms_delta_db: float = 0.0     # change vs previous cycle
    input_state: str = 'normal'   # normal|solo_onset|solo_active|decay|gated|silent

    # Previous cycle RMS for delta calculation
    _prev_input_rms_db: float = field(default=-90.0, repr=False)
```

---

## 4. Channel Model — Transfer Function Calculations

### 4.1 Frequency Axis

All curves are evaluated on a shared log-spaced frequency axis. Define once and import everywhere:

```python
# In a shared constants module or at top of channel_model.py
import numpy as np

N_FREQS = 1000
FREQ_AXIS = np.logspace(np.log10(20.0), np.log10(20000.0), N_FREQS)
SILENCE_THRESHOLD_DB = -50.0
```

### 4.2 EQ Band Transfer Functions

**Peaking EQ (PEQ, VEQ — treat VEQ as PEQ with note in code):**

```python
def peaking_eq_response(freqs: np.ndarray, center_hz: float,
                         gain_db: float, q: float) -> np.ndarray:
    """
    Parametric peaking EQ response in dB across frequency array.
    Uses analog prototype biquad magnitude formula.
    VEQ type is treated as PEQ — minor inaccuracy acknowledged.
    """
    if abs(gain_db) < 0.1:
        return np.zeros(len(freqs))
    
    A  = 10 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * center_hz
    w  = 2.0 * np.pi * freqs
    
    num = (w0 / q * A) ** 2 + (w ** 2 - w0 ** 2) ** 2
    den = (w0 / q / A) ** 2 + (w ** 2 - w0 ** 2) ** 2
    
    return 10.0 * np.log10(np.maximum(num / den, 1e-10))
```

**Low Shelf:**

```python
def low_shelf_response(freqs: np.ndarray, corner_hz: float,
                        gain_db: float, q: float) -> np.ndarray:
    """Low shelf EQ response in dB. Gain applies below corner_hz."""
    if abs(gain_db) < 0.1:
        return np.zeros(len(freqs))
    
    A  = 10 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * corner_hz
    w  = 2.0 * np.pi * freqs
    
    num = A**2 * w0**4 + A * (w0/q)**2 * w**2 + w**4
    den =       w0**4 + (w0/q)**2 * w**2 / A + w**4
    
    # Clamp near-zero denominator to avoid numerical issues
    return 10.0 * np.log10(np.maximum(num / np.maximum(den, 1e-20), 1e-10))
```

**High Shelf:**

```python
def high_shelf_response(freqs: np.ndarray, corner_hz: float,
                         gain_db: float, q: float) -> np.ndarray:
    """High shelf EQ response in dB. Gain applies above corner_hz."""
    if abs(gain_db) < 0.1:
        return np.zeros(len(freqs))
    
    A  = 10 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * corner_hz
    w  = 2.0 * np.pi * freqs
    
    num = A**2 * w**4 + A * (w0/q)**2 * w**2 + w0**4
    den =       w**4 + (w0/q)**2 * w**2 / A + w0**4
    
    return 10.0 * np.log10(np.maximum(num / np.maximum(den, 1e-20), 1e-10))
```

**Low Cut / High Pass Filter (LCut):**

```python
def hpf_response(freqs: np.ndarray, cutoff_hz: float,
                  slope_db_oct: int) -> np.ndarray:
    """
    Butterworth high pass filter response in dB.
    slope_db_oct: 12, 18, or 24 dB/octave.
    At cutoff_hz: returns -3dB. Below cutoff: rolls off at specified slope.
    """
    order_map = {12: 2, 18: 3, 24: 4}
    n = order_map.get(slope_db_oct, 2)
    
    # Avoid division by zero at DC (freq=0)
    safe_freqs = np.maximum(freqs, 0.1)
    ratio = cutoff_hz / safe_freqs       # f_cutoff / f
    
    magnitude_sq = 1.0 / (1.0 + ratio ** (2 * n))
    return 10.0 * np.log10(np.maximum(magnitude_sq, 1e-10))
```

**High Cut / Low Pass Filter (HCut):**

```python
def lpf_response(freqs: np.ndarray, cutoff_hz: float,
                  slope_db_oct: int) -> np.ndarray:
    """Butterworth low pass filter response in dB."""
    order_map = {12: 2, 18: 3, 24: 4}
    n = order_map.get(slope_db_oct, 2)
    
    ratio = freqs / cutoff_hz
    magnitude_sq = 1.0 / (1.0 + ratio ** (2 * n))
    return 10.0 * np.log10(np.maximum(magnitude_sq, 1e-10))
```

**EQ band dispatcher:**

```python
def eq_band_response(band: EQBand, freqs: np.ndarray) -> np.ndarray:
    """Dispatch to the correct transfer function for this band's type."""
    if band.type_int == 0:   # LCut — HPF at band frequency
        # Band 1 LCut uses hpf_slope from preamp config if available,
        # otherwise default 12dB/oct
        return hpf_response(freqs, band.freq_hz, slope_db_oct=12)
    elif band.type_int == 1: # LShv
        return low_shelf_response(freqs, band.freq_hz, band.gain_db, band.q)
    elif band.type_int in (2, 3): # PEQ, VEQ (VEQ treated as PEQ)
        return peaking_eq_response(freqs, band.freq_hz, band.gain_db, band.q)
    elif band.type_int == 4: # HShv
        return high_shelf_response(freqs, band.freq_hz, band.gain_db, band.q)
    elif band.type_int == 5: # HCut — LPF at band frequency
        return lpf_response(freqs, band.freq_hz, slope_db_oct=12)
    else:
        return np.zeros(len(freqs))
```

### 4.3 Full Channel Transfer Curve

```python
def compute_transfer_curves(config: ChannelConfig,
                              freqs: np.ndarray = FREQ_AXIS) -> ChannelConfig:
    """
    Compute and cache HPF, EQ, and combined transfer curves on config.
    Call this after loading config from X32 and after any config change.
    Returns the config with curves populated in-place.
    """
    # HPF curve
    if config.hpf_enabled and config.hpf_freq_hz > 20.0:
        config.hpf_curve_db = hpf_response(
            freqs, config.hpf_freq_hz, config.hpf_slope_db_oct
        )
    else:
        config.hpf_curve_db = np.zeros(len(freqs))
    
    # EQ curve — sum of all 4 bands (only if EQ is enabled globally)
    if config.eq_enabled:
        config.eq_curve_db = np.zeros(len(freqs))
        for band in config.eq_bands:
            config.eq_curve_db += eq_band_response(band, freqs)
    else:
        config.eq_curve_db = np.zeros(len(freqs))
    
    # Combined transfer curve
    config.transfer_curve_db = config.hpf_curve_db + config.eq_curve_db
    
    return config
```

### 4.4 Channel Contribution Curve

```python
def compute_contribution_curve(config: ChannelConfig,
                                 meter: ChannelMeterState,
                                 prior_curve_db: np.ndarray) -> np.ndarray:
    """
    Compute this channel's spectral contribution to the mix in dB.
    
    prior_curve_db: normalized instrument prior shape from InstrumentPrior.
                    Shape: (N_FREQS,). Should sum (in linear) to a known reference
                    level, e.g. 0 dBFS at the instrument's natural output.
    
    Returns: np.ndarray of shape (N_FREQS,) in dB, representing the channel's
             contribution at each frequency in FREQ_AXIS.
    
    Returns -90 dB (silence) if channel is muted or post-fade level is below
    SILENCE_THRESHOLD_DB.
    """
    # Gate out silent or muted channels
    if config.muted:
        return np.full(len(FREQ_AXIS), -90.0)
    if meter.post_fade_db < SILENCE_THRESHOLD_DB:
        return np.full(len(FREQ_AXIS), -90.0)
    
    # 1. Spectral shape: prior modified by signal chain
    #    config.transfer_curve_db must already be computed
    assert config.transfer_curve_db is not None, \
        f"compute_transfer_curves() must be called before contribution calc on ch{config.channel_num}"
    
    shaped = prior_curve_db + config.transfer_curve_db
    
    # 2. Normalize shape to 0dB mean, then add actual level scalar
    shape_mean = np.mean(shaped)
    normalized_shape = shaped - shape_mean
    
    # 3. Effective output level after dynamics
    effective_level_db = meter.post_fade_db + meter.effective_gr_db
    
    # 4. Final contribution
    contribution = normalized_shape + effective_level_db + config.trim_db
    
    return contribution
```

---

## 5. Instrument Prior System

### 5.1 Role of the Prior

The instrument prior represents the natural spectral shape of an instrument before
any board processing. It answers: "where does this instrument's energy naturally live?"

A guitar playing power chords has most energy in 100–500Hz with harmonics above.
The same guitar playing a solo has its spectral centroid shifted upward (1–5kHz dominant).
A kick drum has energy concentrated below 100Hz with a transient click at 2–4kHz.

Priors are normalized shapes — they represent relative frequency distribution,
not absolute level. The ChannelMeterState post-fade level provides the absolute scalar.

### 5.2 Prior States Per Instrument

Each instrument type supports multiple named states:

| Instrument Type | States |
|---|---|
| guitar | normal, solo_onset, solo_active, clean, heavy |
| guitar_lead | normal, solo_onset, solo_active |
| bass_di | normal, slap, picked |
| kick | normal |
| snare | normal |
| overhead | normal |
| vocal_lead | normal, belting |
| vocal_bkg | normal |
| keys | normal, pad, lead |

### 5.3 Prior Definition Format (in band.yaml)

```yaml
instrument_priors:
  guitar:
    normal:
      # dB values at reference frequencies — interpolated to FREQ_AXIS at load time
      # Format: [freq_hz, level_db] pairs, normalized so mean = 0dB
      curve:
        - [80,   -8.0]
        - [160,  -3.0]
        - [250,   0.0]
        - [400,   2.0]
        - [630,   1.5]
        - [1000,  0.0]
        - [2000, -1.5]
        - [4000, -3.0]
        - [8000, -6.0]
        - [16000,-10.0]
    solo_active:
      curve:
        - [80,  -12.0]
        - [160,  -6.0]
        - [250,  -2.0]
        - [400,   0.0]
        - [630,   1.0]
        - [1000,  2.0]
        - [2000,  3.0]
        - [4000,  2.5]
        - [8000,  0.0]
        - [16000, -4.0]
```

### 5.4 Prior Loading and Interpolation

```python
class InstrumentPrior:
    """
    Holds normalized spectral shape curves for one instrument type.
    Interpolated to FREQ_AXIS at load time.
    """
    
    def __init__(self, instrument_type: str, prior_config: dict):
        self.instrument_type = instrument_type
        self._curves = {}
        
        for state_name, state_data in prior_config.items():
            control_points = state_data['curve']
            freqs_ctrl = np.array([p[0] for p in control_points])
            levels_ctrl = np.array([p[1] for p in control_points])
            
            # Interpolate to full FREQ_AXIS (log-space interpolation)
            log_freqs_ctrl = np.log10(freqs_ctrl)
            log_freq_axis  = np.log10(FREQ_AXIS)
            
            interpolated = np.interp(
                log_freq_axis, log_freqs_ctrl, levels_ctrl,
                left=levels_ctrl[0], right=levels_ctrl[-1]
            )
            
            # Normalize so mean = 0dB
            self._curves[state_name] = interpolated - np.mean(interpolated)
        
        # Ensure 'normal' state always exists
        if 'normal' not in self._curves:
            self._curves['normal'] = np.zeros(N_FREQS)
    
    def get_curve(self, state: str = 'normal') -> np.ndarray:
        """Return prior curve for given state. Falls back to 'normal'."""
        return self._curves.get(state, self._curves['normal']).copy()
```

---

## 6. Input State Inference

### 6.1 State Machine Per Channel

Each channel maintains an input state that is inferred from meter behavior,
not read directly from the board. State transitions are triggered by:

- RMS spike (> 2dB in < 200ms) on guitar/guitar_lead channels → solo_onset
- Sustained elevated RMS (> 1.5dB above baseline for > 3 cycles) → solo_active
- RMS returning to within 1dB of pre-spike baseline for > 2 cycles → decay → normal
- Post-fade level below SILENCE_THRESHOLD_DB → silent
- Gate GR > 6dB → gated

```python
def infer_input_state(channel_num: int,
                       meter: ChannelMeterState,
                       config: ChannelConfig,
                       state_history: list) -> str:
    """
    Infer current input state from meter readings and history.
    state_history: list of last N input_state strings for this channel.
    """
    if meter.post_fade_db < SILENCE_THRESHOLD_DB:
        return 'silent'
    
    if meter.gate_gr_db < -6.0:
        return 'gated'
    
    # Solo detection — guitar channels only
    if config.instrument_type in ('guitar', 'guitar_lead'):
        if meter.rms_delta_db > 2.0:
            return 'solo_onset'
        
        current_state = state_history[-1] if state_history else 'normal'
        
        if current_state in ('solo_onset', 'solo_active'):
            # Check if level has returned to normal
            if meter.rms_delta_db < -1.5:
                return 'decay'
            return 'solo_active'
        
        if current_state == 'decay':
            if meter.rms_delta_db > -0.5:
                return 'normal'
            return 'decay'
    
    return 'normal'
```

### 6.2 Input State Event Logging

When input state changes, log an INPUT_STATE_EVENT (see logger spec).
The state transition also triggers an immediate `/meters/6` request for
the affected channel to get pre-fade/post-fade detail at the event moment.

---

## 7. osc_client.py — Extension Checklist

The following additions are needed to osc_client.py beyond Phase 1:

- [ ] Read `/ch/{nn}/preamp` node at startup — parse trim, invert, hpf, hpslope
      **Note:** hpon = phantom power. HPF state = `hpf` field (frequency > 20Hz means enabled)
- [ ] Subscribe to `/meters/15` via batchsubscribe alongside existing `/meters/1`
- [ ] Parse `/meters/15` blob using `parse_meters_15()` — expose as `board_rta_db`
- [ ] Add `request_meters_6(channel_id_0based)` method for on-demand single channel detail
- [ ] Read `comp_attack_ms`, `comp_release_ms`, `comp_makeup_db` from `/dyn` node
- [ ] Read `gate_range_db`, `gate_mode` from `/gate` node
- [ ] On `/xremote` push for any `/eq/` or `/preamp/` address:
      - Re-read full channel via `/node`
      - Call `compute_transfer_curves()` on updated config
      - Log CONFIG_CHANGE event
- [ ] Expose `board_rta_db` property: latest 100-band RTA as np.ndarray

---

## 7b. RTA Investigation Engine

The X32 RTA is a single switchable analyzer. It must be managed as a shared resource with a state machine to prevent conflicts between continuous monitoring, reactive investigations, and the `cal` calibration scan.

### OSC Primitives

```python
# Set RTA source
# 0–31: Ch 01–32 (post-EQ when rta/pos=1)
# 70:   Main L/R  ← default always-on position
# 71:   Mono/Center
osc.send('/-action/setrtasrc', source_int)

# Set chain position (always post-EQ for FOH Assistant)
osc.send('/-prefs/rta/pos', 1)   # 0=pre-EQ, 1=post-EQ

# Subscribe to /meters/15
# /batchsubscribe ,ssiii /foh_rta /meters/15 1 0 1
# Renew every 8 seconds
```

### RTA State Machine

```python
from enum import Enum

class RTAState(Enum):
    MAIN_BUS      = 'main_bus'       # default — /meters/15 on Main L/R
    INVESTIGATING = 'investigating'  # Tier 2 reactive channel scan
    CALIBRATING   = 'calibrating'    # Tier 3 cal command scan

class RTAEngine:
    """
    Manages the X32 RTA as a shared resource.
    Exactly one state at all times. INVESTIGATING preempts CALIBRATING.
    Watchdog timer forces return to MAIN_BUS if stuck > 8 seconds.
    """
    WATCHDOG_TIMEOUT_S = 8.0
    INVESTIGATION_COOLDOWN_S = 30.0   # min gap between Tier 2 scans per band
    
    def __init__(self, osc_client):
        self._osc = osc_client
        self._state = RTAState.MAIN_BUS
        self._state_entered_at = time.time()
        self._last_investigation_by_band: dict[str, float] = {}
    
    def _set_state(self, state: RTAState, source_int: int):
        self._osc.send('/-action/setrtasrc', source_int)
        self._state = state
        self._state_entered_at = time.time()
    
    def set_main_bus(self):
        """Return to continuous main bus monitoring."""
        self._set_state(RTAState.MAIN_BUS, 70)
    
    def start_investigation(self, channel_rta_index: int) -> bool:
        """
        Switch RTA to a specific channel for Tier 2 investigation.
        Returns False if cooldown prevents investigation.
        """
        if self._state == RTAState.CALIBRATING:
            # INVESTIGATING preempts CALIBRATING
            pass
        self._set_state(RTAState.INVESTIGATING, channel_rta_index)
        return True
    
    def start_cal_scan(self, channel_rta_index: int) -> bool:
        """
        Switch RTA to a channel for Tier 3 cal scan.
        Only allowed from MAIN_BUS state.
        """
        if self._state != RTAState.MAIN_BUS:
            return False
        self._set_state(RTAState.CALIBRATING, channel_rta_index)
        return True
    
    def check_watchdog(self):
        """Call every cycle. Forces MAIN_BUS if stuck too long."""
        if self._state != RTAState.MAIN_BUS:
            elapsed = time.time() - self._state_entered_at
            if elapsed > self.WATCHDOG_TIMEOUT_S:
                self.set_main_bus()
                # Log error: RTA watchdog fired
    
    @property
    def state(self) -> RTAState:
        return self._state
```

### Channel RTA Index Mapping

The `/-action/setrtasrc` integer index for channels is 0-based and differs from the `/meters/1` index:

```python
def channel_to_rta_index(channel_num: int, post_eq: bool = True) -> int:
    """
    Convert 1-based channel number to setrtasrc integer.
    post_eq=True: adds 98 to get post-EQ index (ch01 post-EQ = 98).
    post_eq=False: 0-based (ch01 pre-EQ = 0).
    Always use post_eq=True for FOH Assistant investigations.
    """
    if post_eq:
        return (channel_num - 1) + 98   # ch01=98, ch14=111, ch32=129
    else:
        return channel_num - 1          # ch01=0, ch32=31

# Main L/R is always 70 (pre or post equivalent for main bus)
MAIN_LR_RTA_INDEX = 70
```

### Tier 2: Reactive Investigation Loop

```python
async def investigate_channel(rta_engine: RTAEngine,
                               osc_client,
                               candidate_channels: list,
                               problem_band: str,
                               direction: str) -> dict:
    """
    Scan candidate channels in ranked order to find culprit.
    Returns dict with culprit channel and scan results.
    Caller must call rta_engine.set_main_bus() after this returns.
    """
    results = []
    
    for ch in candidate_channels[:5]:   # max 5 candidates
        rta_idx = channel_to_rta_index(ch.channel_num, post_eq=True)
        
        if not rta_engine.start_investigation(rta_idx):
            continue
        
        await asyncio.sleep(0.05)   # one settling frame
        
        readings = []
        for _ in range(3):          # 150ms total (3 × 50ms)
            readings.append(await osc_client.get_meters_15())
        
        # Average the 3 readings
        avg_spectrum = np.mean(readings, axis=0)
        actual_db = band_average(avg_spectrum, BAND_RANGES[problem_band])
        expected_db = ch.model.predicted_band_db(problem_band)
        deviation = actual_db - expected_db
        
        is_culprit = (
            deviation > CULPRIT_THRESHOLD if direction == 'buildup'
            else deviation < -CULPRIT_THRESHOLD
        )
        
        results.append({
            'channel': ch,
            'actual_db': actual_db,
            'expected_db': expected_db,
            'deviation': deviation,
            'is_culprit': is_culprit,
        })
        
        if is_culprit:
            break   # exit early — found it
    
    # Always return to main bus
    rta_engine.set_main_bus()
    return results

CULPRIT_THRESHOLD = 2.0   # dB deviation from model to be considered culprit
```

### Tier 3: cal Command Scan

```python
async def run_cal_scan(rta_engine: RTAEngine,
                        osc_client,
                        active_channels: list,
                        forward_model) -> list:
    """
    User-triggered calibration scan. Scans all active channels, compares
    actual RTA to model prediction, updates instrument priors.
    Returns list of cal results per channel.
    """
    results = []
    total = len(active_channels)
    
    print(f"CAL: scanning {total} channels... (~{total * 0.2:.1f}s)")
    
    for ch in active_channels:
        rta_idx = channel_to_rta_index(ch.channel_num, post_eq=True)
        
        if not rta_engine.start_cal_scan(rta_idx):
            print("CAL: interrupted — investigation in progress")
            break
        
        await asyncio.sleep(0.05)   # settling
        
        readings = []
        for _ in range(4):          # 200ms per channel
            readings.append(await osc_client.get_meters_15())
        
        # Discard first (may be settling), average remaining 3
        avg_spectrum = np.mean(readings[1:], axis=0)
        
        band_results = {}
        for band_name, (freq_lo, freq_hi) in BAND_RANGES.items():
            actual_db = band_average(avg_spectrum, (freq_lo, freq_hi))
            predicted_db = forward_model.predicted_band_db(ch.channel_num, band_name)
            deviation = actual_db - predicted_db
            
            status = '✓' if abs(deviation) < 1.5 else ('⚠' if abs(deviation) < 3.0 else '✗')
            band_results[band_name] = {
                'actual': actual_db,
                'predicted': predicted_db,
                'deviation': deviation,
                'status': status,
            }
        
        results.append({'channel': ch, 'bands': band_results})
    
    rta_engine.set_main_bus()
    return results

ALPHA = 0.1   # prior update learning rate

def apply_prior_updates(cal_results: list, instrument_priors: dict):
    """Apply damped prior updates from cal scan results."""
    updated = []
    for r in cal_results:
        for band, data in r['bands'].items():
            dev = data['deviation']
            if abs(dev) < 0.5:
                continue   # sub-threshold — skip
            inst = r['channel'].instrument_type
            old = instrument_priors[inst][band]
            new = old + ALPHA * dev
            if abs(new - old) < 0.05:
                continue   # sub-perceptual update — skip
            instrument_priors[inst][band] = new
            updated.append({'instrument': inst, 'band': band, 'old': old, 'new': new})
    return updated
```

---

## 8. New File: core/channel_model.py

Create this file. It contains:
- All transfer function math (Section 4)
- `compute_transfer_curves(config)` function
- `compute_contribution_curve(config, meter, prior_curve_db)` function
- `infer_input_state(...)` function
- `InstrumentPrior` class (Section 5.4)
- `FREQ_AXIS`, `N_FREQS`, `SILENCE_THRESHOLD_DB` constants
- All conversion utilities (Section 2.4) if not already in osc_client.py

---

## 9. Session Startup Sequence (Updated)

```
1. Load band.yaml → channel map, instrument types, prior configs
2. Load venue profile → geometry, acoustic corrections (see IMPL_Geometry.md)
3. Load genre profiles
4. Load setlist.yaml if present
5. Connect X32 — send /xremote, confirm with /info
6. For each active channel:
   a. /node ch/{nn}/config  → label
   b. /node ch/{nn}/preamp  → trim, hpf, hpslope
   c. /node ch/{nn}/eq      → all 4 bands
   d. /node ch/{nn}/mix     → fader, mute
   e. /node ch/{nn}/dyn     → compressor settings
   f. /node ch/{nn}/gate    → gate settings
   g. compute_transfer_curves(config)
7. /batchsubscribe /foh_ch_meters /meters/1 0 0 1
8. /batchsubscribe /foh_rta /meters/15 1 0 1
9. Initialize InstrumentPrior objects per instrument type
10. Initialize mic analyzer with venue geometry (see IMPL_Mic_Analyzer.md)
11. Begin main analysis loop
```

---

## 10. Error Handling and Edge Cases

| Condition | Handling |
|---|---|
| Channel EQ is bypassed (`eq/on = 0`) | Set eq_curve_db to all zeros — HPF still applies |
| HPF frequency at minimum (20Hz) | Treat as HPF disabled — no meaningful filtering |
| EQ band gain of 0.0dB | Skip that band's computation — return zeros |
| fader at 0.0 (−90dB) | Return silence curve (−90dB) — channel is off |
| mute = 0 (muted) | Return silence curve regardless of meter state |
| transfer_curve_db is None | Raise AssertionError — config was not initialized |
| VEQ band type | Process as PEQ, add `# VEQ approximated as PEQ` comment |
| meters/6 request timeout | Use meters/1 pre-fade value as fallback |
| X32 WiFi dropout | Retain last known config, freeze meter state, log WARNING |

---

*Reference documents: IMPL_Mic_Analyzer.md, IMPL_Forward_Mix_Model.md, X32_OSC_Reference.md*
