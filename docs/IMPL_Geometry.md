# FOH Assistant — Venue Geometry & Acoustic Physics Implementation
**Document Type:** Claude Code Implementation Reference  
**Phase:** 2  
**Last Updated:** 2026-05-26  
**Depends on:** IMPL_X32_Board_Model.md, IMPL_Mic_Analyzer.md  
**Produces:** `core/geometry.py` (new), `models/venue.py` (new), `config/venues/` (new directory)

---

## Purpose

This document specifies the venue geometry system and acoustic physics calculations.
The geometry module produces two primary outputs consumed by the mic analyzer:

1. **Correction curve** — Per-frequency dB adjustment applied to raw mic FFT to remove
   known acoustic artifacts (comb filtering, room modes, boundary reinforcement).
   This converts raw mic readings into measurements that reflect mix quality,
   not measurement position artifacts.

2. **Room mode mask** — Boolean array flagging frequencies where room resonances
   are predicted, so the recommendation engine treats hot readings there differently
   (room acoustic issue vs mix issue).

Additionally, the geometry module provides venue-type-specific adjustments to:
- Genre profile LUFS targets (outdoor shows need more level on the board)
- Sub frequency targets (corner stages have boundary reinforcement)
- Mic reliability weight (outdoor mic readings are more direct and trustworthy)

---

## 1. Venue Profile Schema

Venue profiles live in `config/venues/`. One YAML file per venue.
The active venue is selected at session startup via `--venue` flag or
interactively during the setup flow.

### 1.1 Full Schema

```yaml
# config/venues/ajs_bar.yaml
venue:
  name: "AJ's Bar"
  id: "ajs_bar"
  capacity: 200
  
  stage:
    type: "corner"              # rectangular | corner | open_air | partial_cover |
                                # tent | large_hall | irregular
    sub_type: null              # patio | outdoor_stage | parking_lot (for open_air)
    
    # Rectangular and Corner — room dimensions
    room:
      length_m: 12.5            # stage wall to back wall
      width_m:  8.0             # left wall to right wall
      ceiling_height_m: 3.2
      shape_notes: >
        Partial wall divides stage area from bar side ~10ft high with cutout.
        Corner stage — band plays in SW corner.
    
    # Open air — no room dimensions, but physical context
    environment: null           # leave null for indoor
    
  pa:
    config_type: "stereo_ground_stack"   # stereo_ground_stack | stereo_flown |
                                          # mono_cluster | corner_splayed | line_array
    crossover_hz: 100           # sub/top crossover frequency
    
    top_left:
      model: "QSC KW152"
      position_x_m: 1.2         # from left wall (or left edge of stage)
      position_y_m: 0.0         # from stage front (0 = at front edge)
      height_m: 2.8
    
    top_right:
      model: "QSC KW152"
      position_x_m: 6.8
      position_y_m: 0.0
      height_m: 2.8
    
    sub_left:
      model: "QSC KLA181-BK"
      position_x_m: 1.2
      position_y_m: 0.0
      height_m: 0.0             # ground stacked
      boundary_loaded: true     # touching or within 0.3m of wall
    
    sub_right:
      model: "QSC KLA181-BK"
      position_x_m: 6.8
      position_y_m: 0.0
      height_m: 0.0
      boundary_loaded: true
  
  reference_mic:
    position_x_m: 4.0           # from left wall
    position_y_m: 6.5           # from stage front
    height_m: 1.5
    
  pa_notes: >
    House PA has significant low-end boost configured for DJ/hip-hop use.
    Requires EXT SUB mode on KW152, sub attenuation pulled back, Kosmos off,
    comp off, HPF engaged on Peavey. X32 presets were solid.
    
  computed:
    # These are populated automatically at load time.
    # Do not edit manually.
    dist_mic_to_top_left_m: null
    dist_mic_to_top_right_m: null
    dist_mic_to_sub_left_m: null
    dist_mic_to_sub_right_m: null
    arrival_delta_tops_ms: null
    comb_notch_frequencies_hz: null
    room_modes_hz: null
    sub_phase_at_crossover_deg: null
    sub_boundary_gain_db: null
```

### 1.2 Open Air Venue Schema

```yaml
# config/venues/outdoor_patio_june13.yaml
venue:
  name: "Outdoor Patio — June 13"
  id: "outdoor_patio_june13"
  capacity: 150
  
  stage:
    type: "open_air"
    sub_type: "patio"
    
    room: null    # no room dimensions for open air
    
    environment:
      nearest_wall_distance_m: 8.0    # nearest hard surface (building, fence)
      nearest_wall_direction: "north"
      ground_surface: "concrete"       # concrete | wood_deck | grass | gravel
      pa_height_m: 2.4                 # speaker elevation above ground
      
  pa:
    config_type: "stereo_ground_stack"
    crossover_hz: 100
    top_left:
      position_x_m: 2.0
      position_y_m: 0.0
      height_m: 2.4
    top_right:
      position_x_m: 5.0
      position_y_m: 0.0
      height_m: 2.4
    sub_left:
      position_x_m: 2.0
      position_y_m: 0.0
      height_m: 0.0
      boundary_loaded: false
    sub_right:
      position_x_m: 5.0
      position_y_m: 0.0
      height_m: 0.0
      boundary_loaded: false
      
  reference_mic:
    position_x_m: 3.5
    position_y_m: 5.0
    height_m: 1.5
```

### 1.3 Corner Bar Schema

```yaml
# config/venues/corner_bar_june20.yaml
venue:
  name: "Corner Bar — June 20"
  id: "corner_bar_june20"
  capacity: 100
  
  stage:
    type: "corner"
    room:
      length_m: 9.0
      width_m: 7.0
      ceiling_height_m: 4.8     # high ceilings
      shape_notes: "Band in SW corner. High ceiling increases modal density."
  
  pa:
    config_type: "corner_splayed"
    crossover_hz: 100
    top_left:
      position_x_m: 0.5         # tight to left wall
      position_y_m: 0.0
      height_m: 2.5
    top_right:
      position_x_m: 0.5         # stage corner — both tops relatively close
      position_y_m: 0.5
      height_m: 2.5
    sub_left:
      position_x_m: 0.3
      position_y_m: 0.3
      height_m: 0.0
      boundary_loaded: true
    sub_right:
      position_x_m: 0.3         # sub in corner = maximum boundary loading
      position_y_m: 0.3
      height_m: 0.0
      boundary_loaded: true

  reference_mic:
    position_x_m: 3.5
    position_y_m: 5.0
    height_m: 1.5
```

---

## 2. Distance Calculations

All distances are computed from the 3D positions defined in the venue profile.

```python
import numpy as np
import math

def distance_3d(pos_a: dict, pos_b: dict) -> float:
    """
    Euclidean distance between two 3D positions.
    Positions are dicts with keys: position_x_m, position_y_m, height_m.
    """
    dx = pos_a['position_x_m'] - pos_b['position_x_m']
    dy = pos_a['position_y_m'] - pos_b['position_y_m']
    dz = pos_a.get('height_m', 0.0) - pos_b.get('height_m', 0.0)
    return math.sqrt(dx**2 + dy**2 + dz**2)

def compute_venue_distances(venue_config: dict) -> dict:
    """
    Compute all relevant distances from venue profile positions.
    Returns dict of labeled distances in meters.
    """
    mic = venue_config['reference_mic']
    pa  = venue_config['pa']
    
    distances = {}
    
    for speaker_key in ('top_left', 'top_right', 'sub_left', 'sub_right'):
        if speaker_key in pa:
            distances[f'mic_to_{speaker_key}_m'] = distance_3d(
                mic, pa[speaker_key]
            )
    
    return distances
```

---

## 3. Acoustic Physics — By Venue Type

### 3.1 Comb Filter Prediction (All Indoor Types)

When two speakers are at different distances from the mic, their signals
arrive at different times. This creates constructive and destructive
interference (comb filtering). The notch frequencies are predictable from
the arrival time difference.

```python
SPEED_OF_SOUND_MS = 343.0   # m/s at 20°C

def arrival_delta_ms(dist_a_m: float, dist_b_m: float) -> float:
    """Time difference (ms) between two speaker arrivals at mic."""
    return abs(dist_a_m - dist_b_m) / SPEED_OF_SOUND_MS * 1000.0

def comb_filter_notches_hz(dist_left_m: float, dist_right_m: float,
                             n_harmonics: int = 8) -> list[float]:
    """
    Frequencies (Hz) where destructive interference occurs between
    two speakers at different distances from the mic.
    
    Notches occur at odd multiples of the fundamental notch frequency:
    f_notch = 1 / (2 * delta_t)
    where delta_t is the arrival time difference in seconds.
    
    Returns empty list if arrival delta < 0.5ms (practically in-phase).
    """
    delta_t_s = abs(dist_left_m - dist_right_m) / SPEED_OF_SOUND_MS
    
    if delta_t_s < 0.0005:    # < 0.5ms — not significant
        return []
    
    fundamental = 1.0 / (2.0 * delta_t_s)
    
    notches = []
    for n in range(1, n_harmonics + 1):
        freq = fundamental * (2 * n - 1)   # odd harmonics only
        if 20.0 <= freq <= 20000.0:
            notches.append(freq)
    
    return notches

def comb_filter_correction_curve(notch_freqs: list[float],
                                   freq_axis: np.ndarray,
                                   notch_depth_db: float = 3.0,
                                   notch_width_octaves: float = 0.25) -> np.ndarray:
    """
    Build a correction curve that attenuates frequencies near comb notches.
    This is subtracted FROM the correction (i.e., these are dips in the mic
    reading that should be ignored, not boosted — we zero them out rather
    than correcting them upward, to avoid amplifying noise).
    
    In practice: set correction to 0dB near notch frequencies — these bands
    contribute less reliable data and should be down-weighted in the
    forward model comparison.
    
    Returns: mask array — 0.0 at notch frequencies, 1.0 elsewhere.
    For use as a reliability weight, not an additive correction.
    """
    reliability = np.ones(len(freq_axis))
    
    log_freqs = np.log10(freq_axis)
    half_width = notch_width_octaves / 2.0
    
    for notch_hz in notch_freqs:
        if notch_hz <= 0:
            continue
        log_notch = np.log10(notch_hz)
        near_notch = np.abs(log_freqs - log_notch) < half_width
        reliability[near_notch] = 0.3    # reduce weight, not zero — still some signal
    
    return reliability
```

### 3.2 Room Mode Prediction (Indoor Types)

Room modes (standing waves) are resonant frequencies where sound builds up.
These cause the mic to read hot at those frequencies even when the mix is fine.

```python
def axial_room_modes(length_m: float, width_m: float, 
                      height_m: float, n_modes: int = 6) -> dict:
    """
    Predict axial standing wave frequencies for a rectangular room.
    
    Axial modes are the most significant (strongest) room resonances.
    They arise from parallel wall pairs.
    
    Returns dict: {'length': [f1, f2...], 'width': [...], 'height': [...]}
    
    Note: Tangential and oblique modes exist but are weaker and more numerous.
    At small venues, axial modes dominate the audible coloration.
    """
    modes = {}
    for dimension, label in [(length_m, 'length'),
                               (width_m,  'width'),
                               (height_m, 'height')]:
        if dimension <= 0:
            continue
        modes[label] = [
            (n * SPEED_OF_SOUND_MS) / (2.0 * dimension)
            for n in range(1, n_modes + 1)
            if 20.0 <= (n * SPEED_OF_SOUND_MS) / (2.0 * dimension) <= 500.0
        ]
        # Room modes above 500Hz are too dense to distinguish individually
        # and are captured by the statistical RT60 behavior instead
    
    return modes

def room_mode_mask(mode_dict: dict, freq_axis: np.ndarray,
                    tolerance_hz_fraction: float = 0.15) -> np.ndarray:
    """
    Boolean mask — True where a room mode is predicted.
    tolerance_hz_fraction: flag frequencies within this fraction of mode freq.
    E.g. 0.15 = ±15% of mode frequency is flagged.
    
    Used to tag mic readings at these frequencies as potential room resonances
    rather than mix problems. The recommendation engine uses this to avoid
    recommending EQ cuts for room-acoustic issues.
    """
    mask = np.zeros(len(freq_axis), dtype=bool)
    
    for dimension_modes in mode_dict.values():
        for mode_hz in dimension_modes:
            ratio = freq_axis / mode_hz
            near_mode = (ratio > (1.0 - tolerance_hz_fraction)) & \
                         (ratio < (1.0 + tolerance_hz_fraction))
            mask |= near_mode
    
    return mask
```

### 3.3 Sub/Top Phase Calculation

```python
def sub_top_phase_at_crossover(dist_sub_m: float, dist_top_m: float,
                                 crossover_hz: float) -> float:
    """
    Phase difference (degrees) between sub and top PA at the crossover
    frequency, as seen from the mic position.
    
    Values near 180° indicate near-cancellation at crossover.
    Values near 0° or 360° indicate reinforcement.
    
    This is informational — logged to venue profile at session start.
    If near-cancellation is detected, log a WARNING suggesting the
    engineer check sub polarity/delay settings on the PA.
    """
    delta_dist_m = dist_sub_m - dist_top_m
    wavelength_m = SPEED_OF_SOUND_MS / crossover_hz
    phase_cycles = delta_dist_m / wavelength_m
    phase_degrees = (phase_cycles % 1.0) * 360.0
    return phase_degrees

def sub_top_phase_warning(phase_degrees: float) -> Optional[str]:
    """
    Returns a warning string if phase suggests cancellation, else None.
    """
    # Normalize to 0-360
    phase = phase_degrees % 360.0
    
    if 135.0 <= phase <= 225.0:
        return (f"Sub/top phase at crossover: {phase:.0f}° — "
                f"near cancellation. Check sub polarity and delay.")
    return None
```

### 3.4 Boundary Reinforcement

```python
def boundary_gain_db(boundary_loaded: bool, stage_type: str) -> float:
    """
    Estimated low-frequency boundary reinforcement gain in dB.
    
    Physics:
    - Free space (no boundaries): 0 dB reference
    - One wall (half space): +3 dB (sound can only radiate into half sphere)
    - Two walls / floor + wall: +6 dB
    - Corner (3 surfaces meeting): +9 dB
    
    A ground-stacked sub on the floor already benefits from the floor boundary.
    A sub touching a wall gets another +3 dB.
    A sub in a corner (touching two walls + floor) gets the full +9 dB.
    
    In practice, the floor boundary is always present for ground-stacked subs.
    The additional gain from wall proximity depends on distance from wall.
    Within 0.3m of a wall: counts as wall boundary (+3 dB additional).
    Corner: both wall boundaries active (+6 dB additional on top of floor = +9 dB total).
    """
    if stage_type == 'open_air':
        return 0.0    # no floor boundary indoors, minimal outdoors
    
    # All indoor ground-stacked subs get floor boundary
    base_gain = 3.0    # floor = half space loading
    
    if stage_type == 'corner' and boundary_loaded:
        # Corner = both wall boundaries + floor = full 9dB
        return 9.0
    elif boundary_loaded:
        # Single wall boundary + floor
        return 6.0
    else:
        # Floor only
        return base_gain
```

### 3.5 Ground Reflection (Open Air)

```python
def ground_reflection_comb_notches(speaker_height_m: float,
                                     mic_height_m: float,
                                     mic_distance_m: float,
                                     n_harmonics: int = 5) -> list[float]:
    """
    For open air venues, the ground reflection creates a comb filter
    between the direct path and the reflected path.
    
    The path length difference determines the notch frequencies.
    This is the primary acoustic artifact in open air environments
    (replaces room modes which don't exist outdoors).
    """
    # Direct path: straight line speaker to mic
    direct_path = math.sqrt(
        mic_distance_m**2 + (speaker_height_m - mic_height_m)**2
    )
    
    # Reflected path: speaker → ground reflection point → mic
    # Geometric reflection: image source at negative speaker height
    reflected_path = math.sqrt(
        mic_distance_m**2 + (speaker_height_m + mic_height_m)**2
    )
    
    path_diff_m = reflected_path - direct_path
    
    if path_diff_m < 0.01:    # negligible
        return []
    
    delta_t_s = path_diff_m / SPEED_OF_SOUND_MS
    fundamental = 1.0 / (2.0 * delta_t_s)
    
    return [fundamental * (2*n - 1)
            for n in range(1, n_harmonics + 1)
            if 20.0 <= fundamental * (2*n - 1) <= 20000.0]
```

---

## 4. Venue Acoustics Classes

```python
from abc import ABC, abstractmethod

class VenueAcoustics(ABC):
    """Base class for venue-type-specific acoustic calculations."""
    
    @abstractmethod
    def mic_correction_curve(self) -> np.ndarray:
        """
        Returns additive correction in dB to apply to raw mic spectrum.
        Shape: (N_FREQS,) on FREQ_AXIS.
        """
        pass
    
    @abstractmethod
    def room_mode_mask(self) -> np.ndarray:
        """Boolean mask of predicted room mode frequencies."""
        pass
    
    @abstractmethod
    def lufs_target_adjustment_db(self) -> float:
        """Adjustment to genre LUFS target for this venue type."""
        pass
    
    @abstractmethod
    def sub_target_adjustment_db(self) -> float:
        """Adjustment to sub/bass band target for this venue type."""
        pass
    
    @abstractmethod
    def mic_reliability_weight(self) -> float:
        """
        Overall reliability of mic readings [0.0, 1.0].
        Used to weight mic vs board data in forward model comparison.
        Higher = more reliable (outdoor = 0.90, reverberant indoor = 0.65).
        """
        pass
    
    @abstractmethod
    def comb_reliability_mask(self) -> np.ndarray:
        """
        Per-frequency reliability weight [0.0, 1.0].
        Reduced near comb filter notch frequencies.
        """
        pass
    
    @classmethod
    def from_venue_profile(cls, venue_config: dict) -> 'VenueAcoustics':
        """Factory — instantiate the correct subclass from venue config."""
        stage_type = venue_config['stage']['type']
        
        dispatch = {
            'rectangular':   RectangularRoomAcoustics,
            'corner':        CornerStageAcoustics,
            'open_air':      OpenAirAcoustics,
            'partial_cover': PartialCoverAcoustics,
            'tent':          TentAcoustics,
            'large_hall':    LargeHallAcoustics,
            'irregular':     IrregularRoomAcoustics,
        }
        
        acoustics_class = dispatch.get(stage_type, IrregularRoomAcoustics)
        return acoustics_class(venue_config)
```

### 4.1 Rectangular Room

```python
class RectangularRoomAcoustics(VenueAcoustics):
    
    def __init__(self, venue_config: dict):
        self.config = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._room = venue_config['stage']['room']
        self._pa   = venue_config['pa']
        self._mic  = venue_config['reference_mic']
        
        # Pre-compute
        self._modes = axial_room_modes(
            self._room['length_m'],
            self._room['width_m'],
            self._room['ceiling_height_m']
        )
        self._notch_freqs = comb_filter_notches_hz(
            self._distances.get('mic_to_top_left_m', 5.0),
            self._distances.get('mic_to_top_right_m', 5.0)
        )
    
    def mic_correction_curve(self) -> np.ndarray:
        # Rectangular indoor: no additive correction
        # (room modes are flagged but not corrected — they're real)
        return np.zeros(N_FREQS)
    
    def room_mode_mask(self) -> np.ndarray:
        return room_mode_mask(self._modes, FREQ_AXIS)
    
    def lufs_target_adjustment_db(self) -> float:
        return 0.0   # baseline — genre targets tuned for this type
    
    def sub_target_adjustment_db(self) -> float:
        # Check for boundary loading
        sub_gain = 0.0
        for sub_key in ('sub_left', 'sub_right'):
            if sub_key in self._pa:
                sub_gain = max(sub_gain, boundary_gain_db(
                    self._pa[sub_key].get('boundary_loaded', False),
                    'rectangular'
                ))
        # Subtract boundary gain from target — room is doing work for us
        return -sub_gain / 2.0    # moderate adjustment for rectangular
    
    def mic_reliability_weight(self) -> float:
        return 0.72    # indoor reverberant field reduces reliability
    
    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)
```

### 4.2 Corner Stage

```python
class CornerStageAcoustics(VenueAcoustics):
    
    def __init__(self, venue_config: dict):
        self.config = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._room = venue_config['stage']['room']
        self._pa   = venue_config['pa']
        
        self._modes = axial_room_modes(
            self._room['length_m'],
            self._room['width_m'],
            self._room['ceiling_height_m']
        )
        self._notch_freqs = comb_filter_notches_hz(
            self._distances.get('mic_to_top_left_m', 3.0),
            self._distances.get('mic_to_top_right_m', 6.0)
        )
    
    def mic_correction_curve(self) -> np.ndarray:
        # Corner stage: subs are boundary loaded, so sub frequencies
        # read hot on the mic. Apply a correction curve that reduces
        # the apparent sub boost so we compare against actual mix content,
        # not boundary reinforcement artifact.
        correction = np.zeros(N_FREQS)
        sub_gain = self._get_sub_boundary_gain()
        
        # Taper correction: full gain reduction below 80Hz,
        # tapering to 0 at 200Hz
        for i, freq in enumerate(FREQ_AXIS):
            if freq <= 80.0:
                correction[i] = -sub_gain
            elif freq <= 200.0:
                taper = 1.0 - (freq - 80.0) / 120.0
                correction[i] = -sub_gain * taper
        
        return correction
    
    def _get_sub_boundary_gain(self) -> float:
        for sub_key in ('sub_left', 'sub_right'):
            if sub_key in self._pa:
                return boundary_gain_db(
                    self._pa[sub_key].get('boundary_loaded', False),
                    'corner'
                )
        return 6.0    # default corner assumption
    
    def room_mode_mask(self) -> np.ndarray:
        return room_mode_mask(self._modes, FREQ_AXIS)
    
    def lufs_target_adjustment_db(self) -> float:
        return 0.0
    
    def sub_target_adjustment_db(self) -> float:
        # Boundary gain means the room is adding sub energy automatically.
        # The board needs LESS sub to hit the same perceived level.
        return -self._get_sub_boundary_gain() * 0.6   # partial discount
    
    def mic_reliability_weight(self) -> float:
        return 0.68    # corner reflections reduce reliability more than rectangular
    
    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)
```

### 4.3 Open Air

```python
class OpenAirAcoustics(VenueAcoustics):
    
    def __init__(self, venue_config: dict):
        self.config = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._pa  = venue_config['pa']
        self._mic = venue_config['reference_mic']
        env = venue_config['stage'].get('environment', {}) or {}
        self._pa_height_m = env.get('pa_height_m', 2.4)
        self._ground_surface = env.get('ground_surface', 'concrete')
        
        # Ground reflection from primary speaker to mic
        mic_dist = self._distances.get('mic_to_top_left_m', 5.0)
        self._ground_notches = ground_reflection_comb_notches(
            self._pa_height_m,
            self._mic['height_m'],
            mic_dist
        )
    
    def mic_correction_curve(self) -> np.ndarray:
        # Open air: no room modes. Minimal correction needed.
        # Small positive correction above 2kHz — high frequencies
        # attenuate faster outdoors due to air absorption at distance.
        correction = np.zeros(N_FREQS)
        for i, freq in enumerate(FREQ_AXIS):
            if freq > 8000.0:
                # Approximate air absorption: +0.5dB per doubling above 8kHz
                octaves_above_8k = np.log2(freq / 8000.0)
                correction[i] = 0.5 * octaves_above_8k
        return correction
    
    def room_mode_mask(self) -> np.ndarray:
        # No room modes outdoors
        return np.zeros(N_FREQS, dtype=bool)
    
    def lufs_target_adjustment_db(self) -> float:
        # Outdoor shows lack boundary reinforcement and reverberant field.
        # Engineer needs to push harder to get same perceived loudness.
        return +2.0   # LUFS target shifts 2dB hotter for open air
    
    def sub_target_adjustment_db(self) -> float:
        # No boundary loading outdoors. Sub energy dissipates in all directions.
        # Board needs more sub to compensate. Target shifts +3–4dB in sub band.
        return +3.5
    
    def mic_reliability_weight(self) -> float:
        # Outdoor mic is most reliable — predominantly direct sound field.
        return 0.90
    
    def comb_reliability_mask(self) -> np.ndarray:
        # Only ground reflection notches to mask
        return comb_filter_correction_curve(
            self._ground_notches, FREQ_AXIS, notch_depth_db=2.0
        )
```

### 4.4 Irregular Room (Fallback)

```python
class IrregularRoomAcoustics(VenueAcoustics):
    """
    Used when venue type is 'irregular' or unrecognized.
    Provides minimal correction with reduced confidence.
    """
    
    def __init__(self, venue_config: dict):
        self.config = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._notch_freqs = comb_filter_notches_hz(
            self._distances.get('mic_to_top_left_m', 5.0),
            self._distances.get('mic_to_top_right_m', 5.0)
        )
    
    def mic_correction_curve(self) -> np.ndarray:
        return np.zeros(N_FREQS)   # no correction — unknown room
    
    def room_mode_mask(self) -> np.ndarray:
        return np.zeros(N_FREQS, dtype=bool)
    
    def lufs_target_adjustment_db(self) -> float:
        return 0.0
    
    def sub_target_adjustment_db(self) -> float:
        return 0.0
    
    def mic_reliability_weight(self) -> float:
        return 0.55    # low confidence — unknown room behavior
    
    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)
```

---

## 5. Venue Profile Data Model

```python
from dataclasses import dataclass, field
from typing import Optional

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
    
    # Acoustic corrections
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
    acoustics: VenueAcoustics     # computed subclass instance
    pa_notes: str = ""
    raw_config: dict = field(default_factory=dict, repr=False)
```

---

## 6. Venue Profile Loader

```python
import yaml

def load_venue_profile(venue_id: str,
                        venues_dir: str = 'config/venues') -> VenueProfile:
    """
    Load venue profile from YAML, compute distances and physics,
    instantiate the appropriate VenueAcoustics subclass.
    """
    filepath = f"{venues_dir}/{venue_id}.yaml"
    
    with open(filepath, 'r') as f:
        raw_config = yaml.safe_load(f)
    
    venue_data = raw_config['venue']
    
    # Compute distances
    distances = compute_venue_distances(venue_data)
    
    # Compute room modes if applicable
    room_modes = {}
    if venue_data['stage'].get('room'):
        room = venue_data['stage']['room']
        room_modes = axial_room_modes(
            room.get('length_m', 0),
            room.get('width_m', 0),
            room.get('ceiling_height_m', 0)
        )
    
    # Compute comb notch frequencies
    notch_freqs = comb_filter_notches_hz(
        distances.get('mic_to_top_left_m', 5.0),
        distances.get('mic_to_top_right_m', 5.0)
    )
    
    # Compute sub phase
    sub_phase = 0.0
    crossover = venue_data['pa'].get('crossover_hz', 100)
    if 'sub_left' in venue_data['pa']:
        sub_phase = sub_top_phase_at_crossover(
            distances.get('mic_to_sub_left_m', 5.0),
            distances.get('mic_to_top_left_m', 5.0),
            crossover
        )
    
    # Instantiate acoustics
    acoustics = VenueAcoustics.from_venue_profile(venue_data)
    
    geometry = VenueGeometry(
        venue_id=venue_data['id'],
        venue_name=venue_data['name'],
        stage_type=venue_data['stage']['type'],
        dist_mic_to_top_left_m=distances.get('mic_to_top_left_m', 0.0),
        dist_mic_to_top_right_m=distances.get('mic_to_top_right_m', 0.0),
        dist_mic_to_sub_left_m=distances.get('mic_to_sub_left_m', 0.0),
        dist_mic_to_sub_right_m=distances.get('mic_to_sub_right_m', 0.0),
        arrival_delta_tops_ms=arrival_delta_ms(
            distances.get('mic_to_top_left_m', 5.0),
            distances.get('mic_to_top_right_m', 5.0)
        ),
        comb_notch_frequencies_hz=notch_freqs,
        room_modes_hz=room_modes,
        sub_phase_at_crossover_deg=sub_phase,
        sub_boundary_gain_db=acoustics.sub_target_adjustment_db(),
        lufs_target_adjustment_db=acoustics.lufs_target_adjustment_db(),
        sub_target_adjustment_db=acoustics.sub_target_adjustment_db(),
        mic_reliability_weight=acoustics.mic_reliability_weight(),
    )
    
    return VenueProfile(
        venue_id=venue_data['id'],
        venue_name=venue_data['name'],
        capacity=venue_data.get('capacity', 0),
        stage_type=venue_data['stage']['type'],
        geometry=geometry,
        acoustics=acoustics,
        pa_notes=venue_data.get('pa_notes', ''),
        raw_config=raw_config,
    )
```

---

## 7. Session Startup — Geometry Report

When a venue profile loads, print a geometry summary to terminal:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VENUE ACOUSTICS — AJ's Bar
Stage type:  corner
Capacity:    200

Distances:
  Mic → Left top:   5.2m
  Mic → Right top:  7.4m
  Mic → Left sub:   5.5m
  Mic → Right sub:  7.6m

Arrival delta (L-R tops): 2.6ms
Comb filter notches:  192Hz, 577Hz, 962Hz, 1346Hz (masked in analysis)
Sub boundary gain:    +9.0dB (corner loaded — sub targets adjusted)
Room modes flagged:   34Hz, 68Hz, 103Hz (length)
                      49Hz, 98Hz, 147Hz (width)
                      36Hz, 71Hz, 107Hz (height)
Sub/top phase at 100Hz crossover: 143° ⚠ Near cancellation — check sub polarity

Mic reliability weight: 0.68 (corner stage — moderate)
LUFS target adjustment: 0.0dB (indoor baseline)
Sub band target adjust: -5.4dB (boundary compensated)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 8. Geometry Measurement Input Flow (--setup-venue Mode)

New CLI mode for first-time venue capture. Guides the engineer through
measurements with a laser range finder.

```
python main.py --setup-venue

VENUE SETUP — New Venue
Enter venue name: AJ's Bar
Stage type [rectangular/corner/open_air/irregular]: corner

ROOM DIMENSIONS
  Room length (stage wall to back wall, meters): 12.5
  Room width (left wall to right wall, meters): 8.0
  Ceiling height at FOH position (meters): 3.2

PA POSITIONS
  (Position the reference mic at your usual FOH spot before measuring)
  Distance from LEFT speaker to mic (meters): 5.2
  Height of left speaker (meters): 2.8
  Distance from RIGHT speaker to mic (meters): 7.4
  Height of right speaker (meters): 2.8
  Distance from LEFT sub to mic (meters): 5.5
  Is left sub within 0.3m of a wall? [y/n]: y
  Distance from RIGHT sub to mic (meters): 7.6
  Is right sub within 0.3m of a wall? [y/n]: y

MIC POSITION
  Mic distance from left wall (meters): 4.0
  Mic distance from stage front (meters): 6.5
  Mic height (meters): 1.5

PA crossover frequency (Hz) [default 100]: 100

PA notes (optional — describe any unusual configuration):
> Peavey PV14BT front-of-house. House PA configured for DJ use.

Computing venue acoustics...
[geometry report prints here]

Save as venue ID [ajs_bar]: ajs_bar
Saved to config/venues/ajs_bar.yaml ✓
```

---

## 9. No-Geometry Fallback

If no venue profile is loaded (--no-venue flag or profile not found):
- `correction_curve_db` = all zeros (no correction)
- `room_mode_mask` = all False (no modes flagged)
- `mic_reliability_weight` = 0.60 (reduced confidence)
- `lufs_target_adjustment_db` = 0.0
- Log WARNING: "No venue profile — acoustic corrections disabled"

The system continues to function. Recommendations are based on raw mic
readings without geometric correction. This is equivalent to Phase 1 behavior.

---

## 10. Files to Create

| File | Type | Contents |
|---|---|---|
| `core/geometry.py` | New | All physics functions, VenueAcoustics subclasses, loader |
| `models/venue.py` | New | VenueGeometry, VenueProfile dataclasses |
| `config/venues/ajs_bar.yaml` | New | AJ's Bar profile |
| `config/venues/outdoor_patio_june13.yaml` | New | June 13 show venue |
| `config/venues/corner_bar_june20.yaml` | New | June 20 show venue |

---

*Reference documents: IMPL_X32_Board_Model.md, IMPL_Mic_Analyzer.md, IMPL_Forward_Mix_Model.md*
