from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class EQBand:
    band_num: int       # 1-4
    type: int           # 0=LCut 1=LShv 2=PEQ 3=VEQ 4=HShv 5=HCut
    freq_hz: float      # 20–20000 Hz (already converted from X32 float)
    gain_db: float      # -15 to +15 dB (reads directly in dB from X32)
    q: float            # Q factor


@dataclass
class ChannelState:
    channel_num: int
    label: str                          # from band.yaml channel map
    fader_db: float                     # converted from X32 float
    muted: bool                         # True = muted (mix/on == 0)
    eq: list[EQBand]                    # 4 bands
    comp_on: bool
    comp_threshold_db: float
    comp_ratio_index: int               # 0-11 enum index
    gate_on: bool
    gate_threshold_db: float
    rms_linear: float                   # raw from meter blob [0.0-1.0]
    rms_db: float                       # converted to dBFS
    timestamp: float                    # unix timestamp

    # Channel metadata from band.yaml (not from X32)
    channel_type: str = "instrument"    # instrument | vocal
    usage: Optional[str] = None         # primary_lead | backup_and_lead | sparse
    inactive_threshold_db: Optional[float] = None
    paired_channel: Optional[int] = None
    role: Optional[str] = None          # shared_lead_rhythm
    priority: Optional[str] = None      # very_high | high | medium | low | none

    # Preamp / HPF state (from X32 preamp node — IMP-022)
    hpf_on: bool = False
    hpf_freq_hz: float = 80.0
    hpf_slope: int = 1                  # 0=6dB/oct, 1=12dB/oct, 2=18dB/oct, 3=24dB/oct
    input_gain_db: float = 0.0
    x32_name: str = ""                  # name as set on X32 tablet — from /ch/{nn}/config/name

    def is_active(self) -> bool:
        """Returns False if channel is muted or below its inactive threshold."""
        if self.muted:
            return False
        if self.inactive_threshold_db is not None:
            return self.rms_db > self.inactive_threshold_db
        return True


# ---------------------------------------------------------------------------
# Phase 2 data models — ChannelConfig and ChannelMeterState
# ---------------------------------------------------------------------------

@dataclass
class ChannelConfig:
    """
    Complete static signal chain for one channel.
    Recomputed on startup and whenever engineer changes EQ, HPF, or fader via /xremote.
    """
    channel_num: int
    label: str
    instrument_type: str       # kick | snare | overhead | guitar | guitar_lead | bass_di |
                               # vocal_lead | vocal_bkg | keys

    # Preamp
    trim_db: float
    polarity_inverted: bool
    hpf_enabled: bool          # True when hpf_freq_hz > 20Hz (NOT from hpon — hpon = phantom power)
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
    # Shape: (N_FREQS,) in dB on FREQ_AXIS. None until compute_transfer_curves() is called.
    hpf_curve_db: Optional[np.ndarray] = field(default=None, repr=False)
    eq_curve_db: Optional[np.ndarray] = field(default=None, repr=False)
    transfer_curve_db: Optional[np.ndarray] = field(default=None, repr=False)

    last_config_update: float = 0.0


@dataclass
class ChannelMeterState:
    """Real-time meter readings for one channel. Updated every 50ms."""
    channel_num: int
    timestamp_ms: float

    # From /meters/1 blob
    input_rms_linear: float       # pre-fader input RMS
    gate_gr_linear: float         # gate gain reduction (1.0 = no reduction)
    dyn_gr_linear: float          # compressor gain reduction

    # From /meters/6 (requested on state change events)
    pre_fade_linear: float = 1.0
    post_fade_linear: float = 1.0

    # Derived
    input_rms_db: float = -90.0
    gate_gr_db: float = 0.0       # always <= 0
    dyn_gr_db: float = 0.0        # always <= 0
    post_fade_db: float = -90.0
    effective_gr_db: float = 0.0  # gate_gr_db + dyn_gr_db

    # Input state
    rms_delta_db: float = 0.0
    input_state: str = 'normal'   # normal|solo_onset|solo_active|decay|gated|silent
    prev_input_state: str = 'normal'
