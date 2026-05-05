from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


BAND_NAMES = ("sub_bass", "bass", "low_mid", "mid", "high_mid", "presence", "air")


@dataclass
class RoomAnalysis:
    lufs: float                         # integrated LUFS
    rms_db: float                       # short-term RMS
    bands: dict[str, float]             # band label → dB level
    band_delta: dict[str, float]        # change vs previous cycle
    lufs_delta: float                   # LUFS change vs previous cycle
    timestamp: float                    # unix timestamp


@dataclass
class LogEvent:
    id: str                             # evt_NNN
    timestamp: str                      # HH:MM:SS
    event_type: str                     # RECOMMENDATION | MANUAL_ADJUSTMENT | BASELINE_DRIFT | etc.
    channel: Optional[str] = None
    channel_num: Optional[int] = None
    genre_profile: Optional[str] = None
    song: Optional[str] = None
    issue: Optional[str] = None
    detail: Optional[str] = None
    current_state: Optional[dict] = None
    suggestion: Optional[str] = None

    # For MANUAL_ADJUSTMENT events
    parameter: Optional[str] = None
    before: Optional[float] = None
    after: Optional[float] = None
    prior_recommendation_id: Optional[str] = None
    match_status: Optional[str] = None  # matched | partial | ignored | engineer_initiated
    suggestion_delta: Optional[str] = None
    lag_seconds: Optional[float] = None


@dataclass
class AdjustmentEvent:
    channel_num: int
    channel_label: str
    parameter: str                      # fader | eq_band_N_gain | eq_band_N_freq | mute
    before: float
    after: float
    timestamp: float
