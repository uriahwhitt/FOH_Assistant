"""Venue geometry and acoustic physics for the FOH Assistant.

Provides:
  - VenueAcoustics ABC and concrete subclasses (RectangularRoom, CornerStage, OpenAir, etc.)
  - Distance and comb filter calculations
  - Room mode prediction
  - load_venue_profile() — loads YAML, computes physics, returns VenueProfile
  - run_setup_venue_wizard() — interactive CLI measurement input
"""

import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

from core.channel_model import FREQ_AXIS, N_FREQS
from models.venue import VenueGeometry, VenueProfile


SPEED_OF_SOUND_MS = 343.0   # m/s at 20°C


# ---------------------------------------------------------------------------
# Distance calculations
# ---------------------------------------------------------------------------

def distance_3d(pos_a: dict, pos_b: dict) -> float:
    """Euclidean distance between two 3D positions.
    Dicts have keys: position_x_m, position_y_m, height_m."""
    dx = pos_a['position_x_m'] - pos_b['position_x_m']
    dy = pos_a['position_y_m'] - pos_b['position_y_m']
    dz = pos_a.get('height_m', 0.0) - pos_b.get('height_m', 0.0)
    return math.sqrt(dx**2 + dy**2 + dz**2)


def _compute_distances_from_positions(venue_data: dict) -> dict:
    """Compute mic-to-speaker distances from venue YAML x/y/height positions."""
    mic = venue_data['reference_mic']
    pa  = venue_data['pa']
    distances = {}
    for key in ('top_left', 'top_right', 'sub_left', 'sub_right'):
        if key in pa and pa[key] is not None:
            distances[f'mic_to_{key}_m'] = distance_3d(mic, pa[key])
    return distances


def compute_venue_distances(venue_data: dict,
                              session_distances: dict = None) -> dict:
    """Compute mic-to-speaker distances.

    If session_distances is provided (from MicPlacement.as_geometry_dict()),
    those rangefinder values are used directly and override the 3D position
    calculation from venue YAML coordinates. Remaining keys fall back to
    the computed values so partial overrides work correctly.
    """
    computed = _compute_distances_from_positions(venue_data)
    if session_distances:
        result = dict(computed)
        result.update(session_distances)
        return result
    return computed


# ---------------------------------------------------------------------------
# Acoustic physics
# ---------------------------------------------------------------------------

def arrival_delta_ms(dist_a_m: float, dist_b_m: float) -> float:
    """Time difference (ms) between two speaker arrivals at mic."""
    return abs(dist_a_m - dist_b_m) / SPEED_OF_SOUND_MS * 1000.0


def comb_filter_notches_hz(dist_left_m: float, dist_right_m: float,
                             n_harmonics: int = 8) -> list:
    """
    Frequencies (Hz) where destructive interference occurs between two speakers
    at different distances from the mic. Returns empty list if delta < 0.5ms.
    Notches at odd harmonics of fundamental: f_notch = 1 / (2 * delta_t).
    """
    delta_t_s = abs(dist_left_m - dist_right_m) / SPEED_OF_SOUND_MS

    if delta_t_s < 0.0005:
        return []

    fundamental = 1.0 / (2.0 * delta_t_s)
    notches = []
    for n in range(1, n_harmonics + 1):
        freq = fundamental * (2 * n - 1)
        if 20.0 <= freq <= 20000.0:
            notches.append(freq)
    return notches


def comb_filter_correction_curve(notch_freqs: list,
                                   freq_axis: np.ndarray,
                                   notch_depth_db: float = 3.0,
                                   notch_width_octaves: float = 0.25) -> np.ndarray:
    """
    Per-frequency reliability weight [0.0, 1.0].
    Reduced near comb filter notch frequencies — these regions contribute less
    reliable data to the forward model comparison.
    """
    reliability = np.ones(len(freq_axis))
    log_freqs   = np.log10(np.maximum(freq_axis, 1.0))
    half_width  = notch_width_octaves / 2.0

    for notch_hz in notch_freqs:
        if notch_hz <= 0:
            continue
        log_notch  = np.log10(notch_hz)
        near_notch = np.abs(log_freqs - log_notch) < half_width
        reliability[near_notch] = 0.3

    return reliability


def axial_room_modes(length_m: float, width_m: float,
                      height_m: float, n_modes: int = 6) -> dict:
    """
    Predict axial standing wave frequencies for a rectangular room.
    Returns dict: {'length': [f1, f2...], 'width': [...], 'height': [...]}.
    Only returns modes below 500Hz — above that, modal density makes them indistinct.
    """
    modes = {}
    for dimension, label in [(length_m, 'length'), (width_m, 'width'), (height_m, 'height')]:
        if dimension <= 0:
            continue
        modes[label] = [
            (n * SPEED_OF_SOUND_MS) / (2.0 * dimension)
            for n in range(1, n_modes + 1)
            if 20.0 <= (n * SPEED_OF_SOUND_MS) / (2.0 * dimension) <= 500.0
        ]
    return modes


def room_mode_mask(mode_dict: dict, freq_axis: np.ndarray,
                    tolerance_hz_fraction: float = 0.15) -> np.ndarray:
    """
    Boolean mask — True where a room mode is predicted (±15% of mode frequency).
    Used to tag mic readings as potential room resonances, not mix problems.
    """
    mask = np.zeros(len(freq_axis), dtype=bool)
    for dimension_modes in mode_dict.values():
        for mode_hz in dimension_modes:
            ratio     = freq_axis / mode_hz
            near_mode = (ratio > (1.0 - tolerance_hz_fraction)) & \
                         (ratio < (1.0 + tolerance_hz_fraction))
            mask |= near_mode
    return mask


def sub_top_phase_at_crossover(dist_sub_m: float, dist_top_m: float,
                                 crossover_hz: float) -> float:
    """Phase difference (degrees) between sub and top at crossover, as seen from mic."""
    delta_dist_m  = dist_sub_m - dist_top_m
    wavelength_m  = SPEED_OF_SOUND_MS / crossover_hz
    phase_cycles  = delta_dist_m / wavelength_m
    return (phase_cycles % 1.0) * 360.0


def sub_top_phase_warning(phase_degrees: float) -> Optional[str]:
    """Return a warning string if phase suggests cancellation near crossover."""
    phase = phase_degrees % 360.0
    if 135.0 <= phase <= 225.0:
        return (f"Sub/top phase at crossover: {phase:.0f}° — "
                f"near cancellation. Check sub polarity and delay.")
    return None


def boundary_gain_db(boundary_loaded: bool, stage_type: str) -> float:
    """
    Estimated low-frequency boundary reinforcement gain in dB.
    Floor boundary: +3dB. Wall boundary: additional +3dB. Corner: +9dB total.
    """
    if stage_type == 'open_air':
        return 0.0

    base_gain = 3.0   # floor = half-space loading always present for ground-stacked subs

    if stage_type == 'corner' and boundary_loaded:
        return 9.0    # corner = both wall boundaries + floor
    elif boundary_loaded:
        return 6.0    # single wall + floor
    return base_gain


def ground_reflection_comb_notches(speaker_height_m: float,
                                     mic_height_m: float,
                                     mic_distance_m: float,
                                     n_harmonics: int = 5) -> list:
    """Comb notch frequencies from ground reflection (open air only)."""
    direct_path    = math.sqrt(mic_distance_m**2 + (speaker_height_m - mic_height_m)**2)
    reflected_path = math.sqrt(mic_distance_m**2 + (speaker_height_m + mic_height_m)**2)
    path_diff_m    = reflected_path - direct_path

    if path_diff_m < 0.01:
        return []

    delta_t_s   = path_diff_m / SPEED_OF_SOUND_MS
    fundamental = 1.0 / (2.0 * delta_t_s)

    return [fundamental * (2*n - 1)
            for n in range(1, n_harmonics + 1)
            if 20.0 <= fundamental * (2*n - 1) <= 20000.0]


# ---------------------------------------------------------------------------
# VenueAcoustics — abstract base and concrete subclasses
# ---------------------------------------------------------------------------

class VenueAcoustics(ABC):
    """Base class for venue-type-specific acoustic calculations."""

    @abstractmethod
    def mic_correction_curve(self) -> np.ndarray:
        """Additive correction in dB to apply to raw mic spectrum. Shape: (N_FREQS,)."""
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
        """Overall reliability of mic readings [0.0, 1.0]."""
        pass

    @abstractmethod
    def comb_reliability_mask(self) -> np.ndarray:
        """Per-frequency reliability weight [0.0, 1.0]. Reduced near comb notches."""
        pass

    def silence_threshold_lufs(self) -> float:
        """LUFS silence gate threshold. Overridable via venue YAML silence_threshold_lufs."""
        return float(getattr(self, 'config', {}).get('silence_threshold_lufs', -50.0))

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
        return dispatch.get(stage_type, IrregularRoomAcoustics)(venue_config)


class RectangularRoomAcoustics(VenueAcoustics):

    def __init__(self, venue_config: dict):
        self.config  = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._room   = venue_config['stage']['room']
        self._pa     = venue_config['pa']

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
        return np.zeros(N_FREQS)

    def room_mode_mask(self) -> np.ndarray:
        return room_mode_mask(self._modes, FREQ_AXIS)

    def lufs_target_adjustment_db(self) -> float:
        return 0.0

    def sub_target_adjustment_db(self) -> float:
        sub_gain = 0.0
        for sub_key in ('sub_left', 'sub_right'):
            if sub_key in self._pa and self._pa[sub_key]:
                sub_gain = max(sub_gain, boundary_gain_db(
                    self._pa[sub_key].get('boundary_loaded', False), 'rectangular'
                ))
        return -sub_gain / 2.0

    def mic_reliability_weight(self) -> float:
        return 0.72

    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)


class CornerStageAcoustics(VenueAcoustics):

    def __init__(self, venue_config: dict):
        self.config  = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._room   = venue_config['stage']['room']
        self._pa     = venue_config['pa']

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
        correction = np.zeros(N_FREQS)
        sub_gain   = self._get_sub_boundary_gain()
        for i, freq in enumerate(FREQ_AXIS):
            if freq <= 80.0:
                correction[i] = -sub_gain
            elif freq <= 200.0:
                taper = 1.0 - (freq - 80.0) / 120.0
                correction[i] = -sub_gain * taper
        return correction

    def _get_sub_boundary_gain(self) -> float:
        for sub_key in ('sub_left', 'sub_right'):
            if sub_key in self._pa and self._pa[sub_key]:
                return boundary_gain_db(
                    self._pa[sub_key].get('boundary_loaded', False), 'corner'
                )
        return 6.0

    def room_mode_mask(self) -> np.ndarray:
        return room_mode_mask(self._modes, FREQ_AXIS)

    def lufs_target_adjustment_db(self) -> float:
        return 0.0

    def sub_target_adjustment_db(self) -> float:
        return -self._get_sub_boundary_gain() * 0.6

    def mic_reliability_weight(self) -> float:
        return 0.68

    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)


class OpenAirAcoustics(VenueAcoustics):

    def __init__(self, venue_config: dict):
        self.config  = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._pa     = venue_config['pa']
        self._mic    = venue_config['reference_mic']
        env          = (venue_config['stage'].get('environment') or {})
        self._pa_height_m    = env.get('pa_height_m', 2.4)
        self._ground_surface = env.get('ground_surface', 'concrete')

        mic_dist = self._distances.get('mic_to_top_left_m', 5.0)
        self._ground_notches = ground_reflection_comb_notches(
            self._pa_height_m,
            self._mic['height_m'],
            mic_dist
        )

    def mic_correction_curve(self) -> np.ndarray:
        correction = np.zeros(N_FREQS)
        for i, freq in enumerate(FREQ_AXIS):
            if freq > 8000.0:
                octaves_above_8k = np.log2(freq / 8000.0)
                correction[i]    = 0.5 * octaves_above_8k
        return correction

    def room_mode_mask(self) -> np.ndarray:
        return np.zeros(N_FREQS, dtype=bool)

    def lufs_target_adjustment_db(self) -> float:
        return +2.0

    def sub_target_adjustment_db(self) -> float:
        return +3.5

    def mic_reliability_weight(self) -> float:
        return 0.90

    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(
            self._ground_notches, FREQ_AXIS, notch_depth_db=2.0
        )


class PartialCoverAcoustics(VenueAcoustics):
    """Partial cover (covered patio, stage with roof but open sides)."""

    def __init__(self, venue_config: dict):
        self.config     = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._notch_freqs = comb_filter_notches_hz(
            self._distances.get('mic_to_top_left_m', 5.0),
            self._distances.get('mic_to_top_right_m', 5.0)
        )

    def mic_correction_curve(self) -> np.ndarray:
        return np.zeros(N_FREQS)

    def room_mode_mask(self) -> np.ndarray:
        return np.zeros(N_FREQS, dtype=bool)

    def lufs_target_adjustment_db(self) -> float:
        return +1.0   # partial exposure — between indoor and outdoor

    def sub_target_adjustment_db(self) -> float:
        return +1.5

    def mic_reliability_weight(self) -> float:
        return 0.78

    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)


class TentAcoustics(VenueAcoustics):
    """Tent or soft-wall enclosure."""

    def __init__(self, venue_config: dict):
        self.config     = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._modes     = {}
        if venue_config['stage'].get('room'):
            room = venue_config['stage']['room']
            self._modes = axial_room_modes(
                room.get('length_m', 0), room.get('width_m', 0),
                room.get('ceiling_height_m', 0)
            )
        self._notch_freqs = comb_filter_notches_hz(
            self._distances.get('mic_to_top_left_m', 5.0),
            self._distances.get('mic_to_top_right_m', 5.0)
        )

    def mic_correction_curve(self) -> np.ndarray:
        return np.zeros(N_FREQS)

    def room_mode_mask(self) -> np.ndarray:
        return room_mode_mask(self._modes, FREQ_AXIS) if self._modes else np.zeros(N_FREQS, dtype=bool)

    def lufs_target_adjustment_db(self) -> float:
        return +0.5

    def sub_target_adjustment_db(self) -> float:
        return +1.0

    def mic_reliability_weight(self) -> float:
        return 0.70

    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)


class LargeHallAcoustics(VenueAcoustics):
    """Large hall — higher RT60, denser modal field."""

    def __init__(self, venue_config: dict):
        self.config     = venue_config
        self._distances = compute_venue_distances(venue_config)
        self._modes     = {}
        if venue_config['stage'].get('room'):
            room = venue_config['stage']['room']
            self._modes = axial_room_modes(
                room.get('length_m', 0), room.get('width_m', 0),
                room.get('ceiling_height_m', 0)
            )
        self._notch_freqs = comb_filter_notches_hz(
            self._distances.get('mic_to_top_left_m', 8.0),
            self._distances.get('mic_to_top_right_m', 8.0)
        )

    def mic_correction_curve(self) -> np.ndarray:
        return np.zeros(N_FREQS)

    def room_mode_mask(self) -> np.ndarray:
        return room_mode_mask(self._modes, FREQ_AXIS) if self._modes else np.zeros(N_FREQS, dtype=bool)

    def lufs_target_adjustment_db(self) -> float:
        return -1.0   # large halls can sound louder due to reverberant field

    def sub_target_adjustment_db(self) -> float:
        return 0.0

    def mic_reliability_weight(self) -> float:
        return 0.60   # high reverberant field reduces reliability

    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)


class IrregularRoomAcoustics(VenueAcoustics):
    """Fallback for irregular or unrecognized venue types."""

    def __init__(self, venue_config: dict):
        self.config     = venue_config
        self._distances = compute_venue_distances(venue_config) if venue_config else {}
        self._notch_freqs = comb_filter_notches_hz(
            self._distances.get('mic_to_top_left_m', 5.0),
            self._distances.get('mic_to_top_right_m', 5.0)
        )

    def mic_correction_curve(self) -> np.ndarray:
        return np.zeros(N_FREQS)

    def room_mode_mask(self) -> np.ndarray:
        return np.zeros(N_FREQS, dtype=bool)

    def lufs_target_adjustment_db(self) -> float:
        return 0.0

    def sub_target_adjustment_db(self) -> float:
        return 0.0

    def mic_reliability_weight(self) -> float:
        return 0.55

    def comb_reliability_mask(self) -> np.ndarray:
        return comb_filter_correction_curve(self._notch_freqs, FREQ_AXIS)


# ---------------------------------------------------------------------------
# Venue profile loader
# ---------------------------------------------------------------------------

def load_venue_profile(venue_id: str,
                        venues_dir: str = None,
                        session=None) -> VenueProfile:
    """
    Load venue profile from YAML, compute distances and physics,
    instantiate the appropriate VenueAcoustics subclass.

    If session is provided and has rangefinder distances
    (session.mic_placement.has_distances), those values are used directly
    for geometry calculations, bypassing the 3D position calculation from
    venue YAML x/y coordinates. Rangefinder gives direct distance; venue
    YAML stores estimated position coordinates.
    """
    if venues_dir is None:
        venues_dir = Path(__file__).parent.parent / 'config' / 'venues'
    filepath = Path(venues_dir) / f'{venue_id}.yaml'

    with open(filepath, 'r') as f:
        raw_config = yaml.safe_load(f)

    venue_data = raw_config['venue']

    session_distances = None
    if session is not None and hasattr(session, 'mic_placement'):
        if session.mic_placement.has_distances:
            session_distances = session.mic_placement.as_geometry_dict()

    distances = compute_venue_distances(venue_data, session_distances=session_distances)

    room_modes = {}
    if venue_data['stage'].get('room'):
        room = venue_data['stage']['room']
        room_modes = axial_room_modes(
            room.get('length_m', 0),
            room.get('width_m', 0),
            room.get('ceiling_height_m', 0)
        )

    notch_freqs = comb_filter_notches_hz(
        distances.get('mic_to_top_left_m', 5.0),
        distances.get('mic_to_top_right_m', 5.0)
    )

    sub_phase = 0.0
    crossover  = venue_data['pa'].get('crossover_hz', 100)
    if 'sub_left' in venue_data['pa'] and venue_data['pa']['sub_left']:
        sub_phase = sub_top_phase_at_crossover(
            distances.get('mic_to_sub_left_m', 5.0),
            distances.get('mic_to_top_left_m', 5.0),
            crossover
        )

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


def print_geometry_report(profile: VenueProfile) -> None:
    """Print venue acoustics summary to terminal at session start."""
    g   = profile.geometry
    sep = '=' * 47

    print(sep)
    print(f'VENUE ACOUSTICS — {g.venue_name}')
    print(f'Stage type:  {g.stage_type}')
    print(f'Capacity:    {profile.capacity}')
    print()
    print('Distances:')
    if g.dist_mic_to_top_left_m:
        print(f'  Mic -> Left top:   {g.dist_mic_to_top_left_m:.1f}m')
    if g.dist_mic_to_top_right_m:
        print(f'  Mic -> Right top:  {g.dist_mic_to_top_right_m:.1f}m')
    if g.dist_mic_to_sub_left_m:
        print(f'  Mic -> Left sub:   {g.dist_mic_to_sub_left_m:.1f}m')
    if g.dist_mic_to_sub_right_m:
        print(f'  Mic -> Right sub:  {g.dist_mic_to_sub_right_m:.1f}m')

    print()
    print(f'Arrival delta (L-R tops): {g.arrival_delta_tops_ms:.1f}ms')

    if g.comb_notch_frequencies_hz:
        notch_str = ', '.join(f'{f:.0f}Hz' for f in g.comb_notch_frequencies_hz[:6])
        print(f'Comb filter notches:  {notch_str} (masked in analysis)')

    if g.sub_boundary_gain_db:
        print(f'Sub boundary gain:    {g.sub_boundary_gain_db:+.1f}dB ({g.stage_type} — sub targets adjusted)')

    if g.room_modes_hz:
        for dimension, modes in g.room_modes_hz.items():
            if modes:
                mode_str = ', '.join(f'{f:.0f}Hz' for f in modes[:4])
                print(f'Room modes ({dimension}): {mode_str}')

    phase = g.sub_phase_at_crossover_deg
    phase_warn = sub_top_phase_warning(phase)
    if phase_warn:
        print(f'Sub/top phase at crossover: ⚠  {phase_warn}')
    else:
        print(f'Sub/top phase at crossover: {phase:.0f}°')

    print()
    print(f'Mic reliability weight: {g.mic_reliability_weight:.2f} ({g.stage_type})')
    print(f'LUFS target adjustment: {g.lufs_target_adjustment_db:+.1f}dB')
    print(f'Sub band target adjust: {g.sub_target_adjustment_db:+.1f}dB')

    if profile.pa_notes:
        print()
        print(f'PA notes: {profile.pa_notes.strip()}')

    print(sep)


# ---------------------------------------------------------------------------
# --setup-venue measurement wizard
# ---------------------------------------------------------------------------

def run_setup_venue_wizard(venues_dir: str = None) -> None:
    """Interactive CLI wizard to capture venue measurements and save a YAML profile."""
    if venues_dir is None:
        venues_dir = Path(__file__).parent.parent / 'config' / 'venues'

    print('\nVENUE SETUP — New Venue')

    venue_name = input('Enter venue name: ').strip() or 'Unnamed Venue'

    stage_types = ['rectangular', 'corner', 'open_air', 'partial_cover', 'tent', 'large_hall', 'irregular']
    print(f'Stage type [{"/".join(stage_types)}]:')
    stage_type = input('> ').strip().lower()
    if stage_type not in stage_types:
        print(f'Unknown type, defaulting to "irregular"')
        stage_type = 'irregular'

    venue_id_default = venue_name.lower().replace(' ', '_').replace("'", '').replace('-', '_')
    venue_id_default = ''.join(c for c in venue_id_default if c.isalnum() or c == '_')

    config = {
        'venue': {
            'name': venue_name,
            'id': venue_id_default,
            'capacity': 0,
            'stage': {
                'type': stage_type,
                'room': None,
                'environment': None,
            },
            'pa': {
                'config_type': 'stereo_ground_stack',
                'crossover_hz': 100,
            },
            'reference_mic': {
                'position_x_m': 0.0,
                'position_y_m': 0.0,
                'height_m': 1.5,
            },
        }
    }

    venue_data = config['venue']

    try:
        venue_data['capacity'] = int(input('Capacity (approximate): ').strip() or '0')
    except ValueError:
        pass

    if stage_type in ('rectangular', 'corner', 'large_hall'):
        print('\nROOM DIMENSIONS')
        try:
            length = float(input('  Room length (stage wall to back wall, meters): ').strip())
            width  = float(input('  Room width (left wall to right wall, meters): ').strip())
            height = float(input('  Ceiling height at FOH position (meters): ').strip())
            notes  = input('  Shape notes (optional): ').strip()
            venue_data['stage']['room'] = {
                'length_m': length,
                'width_m':  width,
                'ceiling_height_m': height,
                'shape_notes': notes or None,
            }
        except ValueError:
            print('  Invalid input — room dimensions skipped.')

    elif stage_type == 'open_air':
        print('\nOUTDOOR ENVIRONMENT')
        try:
            wall_dist = float(input('  Nearest hard surface distance (meters): ').strip())
            ground    = input('  Ground surface [concrete/wood_deck/grass/gravel]: ').strip() or 'concrete'
            pa_h      = float(input('  PA height above ground (meters): ').strip())
            venue_data['stage']['environment'] = {
                'nearest_wall_distance_m': wall_dist,
                'nearest_wall_direction':  'north',
                'ground_surface': ground,
                'pa_height_m':    pa_h,
            }
        except ValueError:
            print('  Invalid input — environment info skipped.')

    print('\nPA POSITIONS')
    print('  (Position the reference mic at your usual FOH spot before measuring)')

    pa_config = venue_data['pa']
    try:
        for label, key in [('LEFT top', 'top_left'), ('RIGHT top', 'top_right'),
                            ('LEFT sub', 'sub_left'),  ('RIGHT sub', 'sub_right')]:
            dist  = float(input(f'  Distance from {label} speaker to mic (meters): ').strip())
            height = float(input(f'  Height of {label} speaker (meters): ').strip())
            bl    = 'n'
            if 'sub' in key:
                bl = input(f'  Is {label} within 0.3m of a wall? [y/n]: ').strip().lower()
            pa_config[key] = {
                'position_x_m': 0.0,
                'position_y_m': 0.0,
                'height_m':     height,
                'boundary_loaded': (bl == 'y'),
                '_direct_dist_m': dist,
            }
    except ValueError:
        print('  Invalid input — PA positions may be incomplete.')

    print('\nMIC POSITION')
    try:
        mic_x = float(input('  Mic distance from left wall (meters): ').strip())
        mic_y = float(input('  Mic distance from stage front (meters): ').strip())
        mic_h = float(input('  Mic height (meters): ').strip())
        venue_data['reference_mic'] = {
            'position_x_m': mic_x,
            'position_y_m': mic_y,
            'height_m': mic_h,
        }
    except ValueError:
        print('  Invalid input — mic position skipped.')

    try:
        xo = input('PA crossover frequency (Hz) [default 100]: ').strip()
        if xo:
            pa_config['crossover_hz'] = int(xo)
    except ValueError:
        pass

    pa_notes = input('PA notes (optional): ').strip()
    if pa_notes:
        venue_data['pa_notes'] = pa_notes

    print('\nComputing venue acoustics...')

    try:
        acoustics = VenueAcoustics.from_venue_profile(venue_data)
        distances = {}
        mic = venue_data['reference_mic']
        for key in ('top_left', 'top_right', 'sub_left', 'sub_right'):
            if key in pa_config and pa_config[key]:
                direct = pa_config[key].get('_direct_dist_m')
                if direct:
                    distances[f'mic_to_{key}_m'] = direct
                    pa_config[key]['position_y_m'] = direct

        temp_profile = VenueProfile(
            venue_id=venue_data['id'],
            venue_name=venue_data['name'],
            capacity=venue_data.get('capacity', 0),
            stage_type=stage_type,
            geometry=VenueGeometry(
                venue_id=venue_data['id'],
                venue_name=venue_data['name'],
                stage_type=stage_type,
                dist_mic_to_top_left_m=distances.get('mic_to_top_left_m', 0),
                dist_mic_to_top_right_m=distances.get('mic_to_top_right_m', 0),
                dist_mic_to_sub_left_m=distances.get('mic_to_sub_left_m', 0),
                dist_mic_to_sub_right_m=distances.get('mic_to_sub_right_m', 0),
                arrival_delta_tops_ms=arrival_delta_ms(
                    distances.get('mic_to_top_left_m', 5.0),
                    distances.get('mic_to_top_right_m', 5.0)
                ),
                comb_notch_frequencies_hz=comb_filter_notches_hz(
                    distances.get('mic_to_top_left_m', 5.0),
                    distances.get('mic_to_top_right_m', 5.0)
                ),
                sub_phase_at_crossover_deg=sub_top_phase_at_crossover(
                    distances.get('mic_to_sub_left_m', 5.0),
                    distances.get('mic_to_top_left_m', 5.0),
                    pa_config.get('crossover_hz', 100)
                ),
                sub_boundary_gain_db=acoustics.sub_target_adjustment_db(),
                lufs_target_adjustment_db=acoustics.lufs_target_adjustment_db(),
                sub_target_adjustment_db=acoustics.sub_target_adjustment_db(),
                mic_reliability_weight=acoustics.mic_reliability_weight(),
            ),
            acoustics=acoustics,
            pa_notes=pa_notes,
            raw_config=config,
        )
        print_geometry_report(temp_profile)
    except Exception as e:
        print(f'  Warning: could not compute full acoustics: {e}')

    venue_id_save = input(f'Save as venue ID [{venue_id_default}]: ').strip() or venue_id_default
    venue_data['id'] = venue_id_save

    # Strip helper _direct_dist_m keys from PA config before saving
    for key in ('top_left', 'top_right', 'sub_left', 'sub_right'):
        if key in pa_config and pa_config[key]:
            pa_config[key].pop('_direct_dist_m', None)

    save_path = Path(venues_dir) / f'{venue_id_save}.yaml'
    save_path.parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f'Saved to {save_path} ✓')
