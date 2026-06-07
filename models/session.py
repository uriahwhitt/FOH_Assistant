"""Session configuration — per-show settings that change every show.

Venue profile contains permanent room/PA data.
Session contains: X32 IP, mic placement distances, setlist, tonight's notes.

Mic placement distances are measured with a rangefinder on arrival.
They override the venue YAML's reference_mic x/y positions when present,
giving more accurate geometry correction without editing the venue file.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml

SESSION_FILE = Path('config/sessions/latest_session.yaml')
SESSIONS_DIR = Path('config/sessions')


@dataclass
class MicPlacement:
    """
    Rangefinder measurements taken on arrival.
    Stored as direct distances in meters — no x/y geometry required.
    When present, these override the venue YAML reference_mic positions.
    """
    speaker_l_to_mic:    Optional[float] = None
    speaker_r_to_mic:    Optional[float] = None
    sub_l_to_mic:        Optional[float] = None
    sub_r_to_mic:        Optional[float] = None
    speaker_height:      Optional[float] = None
    mic_height:          float = 1.5
    description:         str  = ""
    distances_confirmed: bool = False
    measured_at:         Optional[str] = None

    @property
    def has_distances(self) -> bool:
        """True if at least the two speaker distances are set."""
        return (self.speaker_l_to_mic is not None and
                self.speaker_r_to_mic is not None)

    @property
    def status(self) -> str:
        if self.distances_confirmed:
            t = self.measured_at or "confirmed"
            return f"✓ confirmed {t}"
        if self.has_distances:
            return "⚠ set but not confirmed"
        return "⚠ distances not measured"

    def as_geometry_dict(self) -> dict:
        """
        Return distances in the format expected by compute_venue_distances()
        when bypassing 3D position calculations.
        Keys match the output of compute_venue_distances().
        """
        d = {}
        if self.speaker_l_to_mic is not None:
            d['mic_to_top_left_m']  = self.speaker_l_to_mic
        if self.speaker_r_to_mic is not None:
            d['mic_to_top_right_m'] = self.speaker_r_to_mic
        if self.sub_l_to_mic is not None:
            d['mic_to_sub_left_m']  = self.sub_l_to_mic
        if self.sub_r_to_mic is not None:
            d['mic_to_sub_right_m'] = self.sub_r_to_mic
        return d


@dataclass
class SessionConfig:
    date:          str  = ""
    venue_id:      str  = ""
    x32_ip:        str  = ""
    x32_port:      int  = 10023
    setlist_file:  str  = ""
    notes:         str  = ""
    mic_placement: MicPlacement = field(default_factory=MicPlacement)

    def save(self, path: Path = SESSION_FILE) -> None:
        """Write to YAML immediately. Creates directory if needed."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        mp = self.mic_placement
        data = {
            'session': {
                'date':         self.date,
                'venue_id':     self.venue_id,
                'x32_ip':       self.x32_ip,
                'x32_port':     self.x32_port,
                'setlist_file': self.setlist_file,
                'notes':        self.notes,
            },
            'mic_placement': {
                'description':         mp.description,
                'distances_confirmed': mp.distances_confirmed,
                'measured_at':         mp.measured_at,
                'distances_m': {
                    'speaker_l_to_mic': mp.speaker_l_to_mic,
                    'speaker_r_to_mic': mp.speaker_r_to_mic,
                    'sub_l_to_mic':     mp.sub_l_to_mic,
                    'sub_r_to_mic':     mp.sub_r_to_mic,
                    'speaker_height':   mp.speaker_height,
                    'mic_height':       mp.mic_height,
                },
            },
        }
        with open(path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    @classmethod
    def load(cls, path: Path = SESSION_FILE) -> 'SessionConfig':
        """Load from YAML. Returns default SessionConfig if file missing."""
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            with open(path, 'r') as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            return cls()
        s  = raw.get('session', {})
        mp = raw.get('mic_placement', {})
        dm = mp.get('distances_m', {})
        return cls(
            date         = s.get('date', ''),
            venue_id     = s.get('venue_id', ''),
            x32_ip       = s.get('x32_ip', ''),
            x32_port     = s.get('x32_port', 10023),
            setlist_file = s.get('setlist_file', ''),
            notes        = s.get('notes', ''),
            mic_placement=MicPlacement(
                description         = mp.get('description', ''),
                distances_confirmed = mp.get('distances_confirmed', False),
                measured_at         = mp.get('measured_at'),
                speaker_l_to_mic    = dm.get('speaker_l_to_mic'),
                speaker_r_to_mic    = dm.get('speaker_r_to_mic'),
                sub_l_to_mic        = dm.get('sub_l_to_mic'),
                sub_r_to_mic        = dm.get('sub_r_to_mic'),
                speaker_height      = dm.get('speaker_height'),
                mic_height          = dm.get('mic_height', 1.5),
            ),
        )

    def archive(self) -> None:
        """Copy current session to a dated archive file at show end."""
        if not self.date:
            return
        name = f"{self.date}_{self.venue_id or 'unknown'}.yaml"
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.save(SESSIONS_DIR / name)
