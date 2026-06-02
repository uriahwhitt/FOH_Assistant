"""Venue geometry data models."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VenueGeometry:
    """Computed geometry for a venue. Populated from YAML at load time."""
    venue_id: str
    venue_name: str
    stage_type: str

    # Distances (meters)
    dist_mic_to_top_left_m:  float = 0.0
    dist_mic_to_top_right_m: float = 0.0
    dist_mic_to_sub_left_m:  float = 0.0
    dist_mic_to_sub_right_m: float = 0.0

    # Derived physics
    arrival_delta_tops_ms:      float = 0.0
    comb_notch_frequencies_hz:  list  = field(default_factory=list)
    room_modes_hz:              dict  = field(default_factory=dict)
    sub_phase_at_crossover_deg: float = 0.0
    sub_boundary_gain_db:       float = 0.0

    # Acoustic corrections (from VenueAcoustics subclass)
    lufs_target_adjustment_db: float = 0.0
    sub_target_adjustment_db:  float = 0.0
    mic_reliability_weight:    float = 0.75


@dataclass
class VenueProfile:
    """Complete venue profile loaded from YAML."""
    venue_id: str
    venue_name: str
    capacity: int
    stage_type: str
    geometry: VenueGeometry
    acoustics: Any       # VenueAcoustics instance from core.geometry
    pa_notes: str = ""
    raw_config: dict = field(default_factory=dict, repr=False)
