from dataclasses import dataclass, field
from typing import Optional


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

    def is_active(self) -> bool:
        """Returns False if channel is muted or below its inactive threshold."""
        if self.muted:
            return False
        if self.inactive_threshold_db is not None:
            return self.rms_db > self.inactive_threshold_db
        return True
