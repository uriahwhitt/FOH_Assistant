"""Settings mode — navigable terminal menu for venue, session, mic, and system config.

Access:
  python main.py --settings          # standalone before show
  'settings' typed in show loop      # mid-show access (analysis continues)

All changes save immediately to config/sessions/latest_session.yaml.
Venue YAML edits save immediately to config/venues/<id>.yaml.
"""

import os
import time
from pathlib import Path
from typing import Optional

import yaml

from models.session import SessionConfig, MicPlacement


# ── Helpers ───────────────────────────────────────────────────────────────

def _clear() -> None:
    os.system('cls' if os.name == 'nt' else 'clear')

def _sep(width: int = 42) -> str:
    return '━' * width

def _prompt(prefix: str = '') -> str:
    try:
        return input(f"{prefix}> ").strip()
    except (KeyboardInterrupt, EOFError):
        return '0'

def _edit_float(label: str, current: Optional[float],
                 unit: str = 'm') -> Optional[float]:
    """Prompt for optional float. Enter keeps current. 'x' clears to None."""
    cur_str = f"{current:.2f}" if current is not None else "not set"
    val = input(f"  {label} [{cur_str} {unit}] (Enter=keep, x=clear): ").strip()
    if val == '':
        return current
    if val.lower() == 'x':
        return None
    try:
        return float(val)
    except ValueError:
        print(f"  Invalid value -- keeping {cur_str}")
        return current

def _edit_str(label: str, current: str, allow_empty: bool = False) -> str:
    """Prompt for string. Enter keeps current."""
    cur_str = current or "not set"
    val = input(f"  {label} [{cur_str}] (Enter=keep): ").strip()
    if val == '' and not allow_empty:
        return current
    return val if val else current


# ── Main class ────────────────────────────────────────────────────────────

class SettingsMenu:
    """
    Terminal settings menu.
    Instantiate, call run(). Returns updated SessionConfig when user exits.
    """

    def __init__(self,
                  session: Optional[SessionConfig] = None,
                  running_show: bool = False):
        self.session      = session or SessionConfig.load()
        self.running_show = running_show

    def run(self) -> SessionConfig:
        """Enter the menu. Blocks until user exits. Returns updated session."""
        if self.running_show:
            self._quick_access()
        else:
            self._main_menu()
        return self.session

    # ── Main menu ──────────────────────────────────────────────────────────

    def _main_menu(self) -> None:
        while True:
            _clear()
            vname  = self._venue_name()
            sdate  = self.session.date or "no date"
            print(_sep())
            print("FOH ASSISTANT -- SETTINGS")
            print(f"Venue: {vname}  |  Session: {sdate}")
            print(_sep())
            print()
            print(f"  1. Venue          {vname}")
            print(f"  2. Session        {self._session_status()}")
            print(f"  3. Mic & Audio    {self._mic_status()}")
            print(f"  4. Band           {self._band_status()}")
            print(f"  5. System")
            print()
            print("  0. Exit settings")
            print()
            c = _prompt()
            if   c == '1': self._venue_menu()
            elif c == '2': self._session_menu()
            elif c == '3': self._mic_menu()
            elif c == '4': self._band_menu()
            elif c == '5': self._system_menu()
            elif c == '0': break

    # ── Quick access (mid-show) ────────────────────────────────────────────

    def _quick_access(self) -> None:
        while True:
            _clear()
            print(_sep())
            print("SETTINGS  (show continues in background)")
            print(_sep())
            print()
            print(f"  1. Mic Placement    {self.session.mic_placement.status}")
            print(f"  2. X32 Connection   {self.session.x32_ip or 'not set'}")
            print(f"  3. Test mic level")
            print(f"  4. Full settings")
            print()
            print("  0. Return to show")
            print()
            c = _prompt()
            if   c == '1': self._mic_placement_screen()
            elif c == '2': self._x32_screen()
            elif c == '3': self._test_level()
            elif c == '4': self._main_menu()
            elif c == '0': break

    # ── Venue ──────────────────────────────────────────────────────────────

    def _venue_menu(self) -> None:
        while True:
            _clear()
            vname = self._venue_name()
            vid   = self.session.venue_id or "none"
            print(_sep())
            print(f"VENUE")
            print(f"Active: {vname} ({vid})")
            print(_sep())
            print()
            print("  1. Select venue")
            print("  2. Create new venue")
            print("  3. Edit room details")
            print("  4. Edit PA hardware")
            print("  5. PA settings checklist")
            print("  6. View geometry report")
            print("  7. Frequency confidence")
            print()
            print("  0. Back")
            print()
            c = _prompt()
            if   c == '1': self._select_venue()
            elif c == '2': self._create_venue()
            elif c == '3': self._edit_venue_yaml('room')
            elif c == '4': self._edit_venue_yaml('pa')
            elif c == '5': self._edit_pa_checklist()
            elif c == '6': self._geometry_report()
            elif c == '7': self._edit_confidence_screen()
            elif c == '0': break

    def _select_venue(self) -> None:
        """List venue YAMLs, let user pick one."""
        _clear()
        venues_dir = Path('config/venues')
        files = sorted(venues_dir.glob('*.yaml')) if venues_dir.exists() else []
        if not files:
            print("  No venue profiles found. Use option 2 to create one.")
            input("  Press Enter...")
            return
        print(_sep())
        print("SELECT VENUE")
        print(_sep())
        print()
        for i, f in enumerate(files, 1):
            marker = " <- active" if f.stem == self.session.venue_id else ""
            print(f"  {i}. {f.stem}{marker}")
        print()
        print("  0. Cancel")
        print()
        c = _prompt()
        if c == '0':
            return
        try:
            idx = int(c) - 1
            if 0 <= idx < len(files):
                self.session.venue_id = files[idx].stem
                self.session.save()
                print(f"\n  Venue set to: {self.session.venue_id}")
                time.sleep(0.7)
        except (ValueError, IndexError):
            pass

    def _create_venue(self) -> None:
        """Create a minimal new venue profile."""
        _clear()
        print(_sep())
        print("CREATE NEW VENUE")
        print(_sep())
        print()
        name = input("  Venue name: ").strip()
        if not name:
            return
        vid = name.lower().replace(' ', '_').replace("'", '').replace('-', '_')
        vid = ''.join(c for c in vid if c.isalnum() or c == '_')
        vid_input = input(f"  Venue ID [{vid}]: ").strip()
        if vid_input:
            vid = vid_input

        print("  Acoustic class:")
        print("    1. open_air       (outdoor -- no room modes)")
        print("    2. rectangular    (standard indoor room)")
        print("    3. corner         (band plays in a corner)")
        print("    4. irregular      (unusual / unknown shape)")
        ac_map = {'1': 'open_air', '2': 'rectangular', '3': 'corner', '4': 'irregular'}
        ac = ac_map.get(_prompt("  "), 'irregular')

        cap_str = input("  Capacity (approx): ").strip()
        cap = int(cap_str) if cap_str.isdigit() else 0

        notes = input("  Notes (optional): ").strip()

        venue_data = {
            'venue': {
                'name': name,
                'id': vid,
                'capacity': cap,
                'stage': {
                    'type': ac,
                    'room': None if ac == 'open_air' else {
                        'length_m': None, 'width_m': None,
                        'ceiling_height_m': None, 'shape_notes': notes or None,
                    },
                    'environment': {
                        'ground_surface': 'concrete',
                        'nearest_wall_distance_m': None,
                        'pa_height_m': None,
                    } if ac == 'open_air' else None,
                },
                'pa': {
                    'config_type': 'stereo_ground_stack',
                    'crossover_hz': 100,
                    'top_left':  {'position_x_m': 0.0, 'position_y_m': 0.0, 'height_m': 2.4},
                    'top_right': {'position_x_m': 0.0, 'position_y_m': 0.0, 'height_m': 2.4},
                    'sub_left':  {'position_x_m': 0.0, 'position_y_m': 0.0, 'height_m': 0.0,
                                  'boundary_loaded': ac == 'corner'},
                    'sub_right': {'position_x_m': 0.0, 'position_y_m': 0.0, 'height_m': 0.0,
                                  'boundary_loaded': ac == 'corner'},
                },
                'pa_settings': [],
                'pa_notes': notes or '',
                'reference_mic': {'position_x_m': 0.0, 'position_y_m': 5.0, 'height_m': 1.5},
            }
        }

        venues_dir = Path('config/venues')
        venues_dir.mkdir(parents=True, exist_ok=True)
        fpath = venues_dir / f'{vid}.yaml'
        with open(fpath, 'w') as f:
            yaml.dump(venue_data, f, default_flow_style=False, sort_keys=False)

        self.session.venue_id = vid
        self.session.save()
        print(f"\n  Created: {fpath}")
        print(f"  Active venue: {vid}")
        print("  Use Edit room/PA to fill in details.")
        input("\n  Press Enter...")

    def _edit_venue_yaml(self, section: str) -> None:
        """Edit room or PA hardware in the active venue YAML."""
        if not self._require_venue():
            return
        fpath = Path(f'config/venues/{self.session.venue_id}.yaml')
        _clear()
        with open(fpath, 'r') as f:
            data = yaml.safe_load(f)
        vd = data['venue']

        print(_sep())
        if section == 'room':
            print(f"EDIT ROOM -- {vd.get('name', '?')}")
            print(_sep())
            print()
            room = vd['stage'].get('room') or {}
            room['length_m']         = _edit_float("Room length (stage to back wall)", room.get('length_m'))
            room['width_m']          = _edit_float("Room width (left to right wall)", room.get('width_m'))
            room['ceiling_height_m'] = _edit_float("Ceiling height at FOH", room.get('ceiling_height_m'))
            notes = _edit_str("Shape notes", room.get('shape_notes', ''))
            if notes: room['shape_notes'] = notes
            vd['stage']['room'] = room

        elif section == 'pa':
            print(f"EDIT PA HARDWARE -- {vd.get('name', '?')}")
            print(_sep())
            print("  Speaker positions are measured from stage center (x=left/right, y=depth)")
            print()
            pa = vd.get('pa', {})
            for side in ('top_left', 'top_right', 'sub_left', 'sub_right'):
                node = pa.get(side, {})
                print(f"  {side.upper()}:")
                node['position_x_m'] = _edit_float("    X position", node.get('position_x_m'))
                node['position_y_m'] = _edit_float("    Y position (depth)", node.get('position_y_m'))
                node['height_m']     = _edit_float("    Height", node.get('height_m'))
                if 'boundary_loaded' in node:
                    bl = input(f"    Boundary loaded [{node['boundary_loaded']}] (y/n/Enter=keep): ").strip()
                    if bl.lower() == 'y':   node['boundary_loaded'] = True
                    elif bl.lower() == 'n': node['boundary_loaded'] = False
                pa[side] = node
                print()
            xov = _edit_float("  Crossover frequency", pa.get('crossover_hz', 100), unit='Hz')
            if xov: pa['crossover_hz'] = int(xov)
            vd['pa'] = pa

        with open(fpath, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        print(f"\n  Saved to {fpath}")
        time.sleep(0.6)

    def _edit_pa_checklist(self) -> None:
        """Add/remove PA settings checklist items."""
        if not self._require_venue():
            return
        fpath = Path(f'config/venues/{self.session.venue_id}.yaml')
        while True:
            _clear()
            with open(fpath, 'r') as f:
                data = yaml.safe_load(f)
            items = data['venue'].get('pa_settings', []) or []
            print(_sep())
            print("PA SETTINGS CHECKLIST")
            print(_sep())
            print()
            if items:
                for i, item in enumerate(items, 1):
                    req = " [required]" if item.get('required') else ""
                    print(f"  {i}. {item.get('label', '?')}{req}")
            else:
                print("  (no checklist items)")
            print()
            print("  a. Add item   r. Remove item   0. Done")
            print()
            c = _prompt()
            if c == '0':
                break
            elif c == 'a':
                label = input("  Label: ").strip()
                if label:
                    req = input("  Required? (y/n): ").strip().lower() == 'y'
                    items.append({'label': label, 'required': req})
                    data['venue']['pa_settings'] = items
                    with open(fpath, 'w') as f:
                        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                    print("  Added.")
                    time.sleep(0.4)
            elif c == 'r':
                idx_s = input("  Remove item number: ").strip()
                try:
                    idx = int(idx_s) - 1
                    if 0 <= idx < len(items):
                        removed = items.pop(idx)
                        data['venue']['pa_settings'] = items
                        with open(fpath, 'w') as f:
                            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                        print(f"  Removed: {removed['label']}")
                        time.sleep(0.4)
                except (ValueError, IndexError):
                    pass

    def _edit_confidence_screen(self) -> None:
        """Adjust per-band frequency confidence weights."""
        if not self._require_venue():
            return

        fpath = Path(f'config/venues/{self.session.venue_id}.yaml')
        BANDS = ['sub', 'bass', 'low_mid', 'mid_low',
                 'mid_high', 'upper_mid', 'presence', 'air']

        while True:
            _clear()
            with open(fpath, 'r') as f:
                data = yaml.safe_load(f)

            conf = data.get('frequency_confidence', {})
            print(_sep())
            print("FREQUENCY CONFIDENCE")
            print(f"Venue: {self._venue_name()}")
            print(_sep())
            print("Set reliability of each band (0.0 = exclude, 1.0 = fully trust)")
            print("Low confidence bands appear dimmed in the display.")
            print()

            for i, band in enumerate(BANDS, 1):
                val = conf.get(band, 1.0)
                filled = int(val * 10)
                bar = '█' * filled + '░' * (10 - filled)
                print(f"  {i}. {band:<12} [{bar}] {val:.1f}")

            print()
            print("  a. Reset all to 1.0 (full trust)")
            print("  0. Back")
            print()
            print("Enter band number to edit:")
            c = _prompt()

            if c == '0':
                break
            elif c == 'a':
                for band in BANDS:
                    conf[band] = 1.0
                data['frequency_confidence'] = conf
                with open(fpath, 'w') as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                print("  All bands reset to 1.0")
                time.sleep(0.5)
            elif c.isdigit() and 1 <= int(c) <= len(BANDS):
                band = BANDS[int(c) - 1]
                current = conf.get(band, 1.0)
                val_str = input(
                    f"  {band} confidence [{current:.1f}] (0.0–1.0, Enter=keep): "
                ).strip()
                if val_str:
                    try:
                        new_val = max(0.0, min(1.0, float(val_str)))
                        conf[band] = round(new_val, 1)
                        data['frequency_confidence'] = conf
                        with open(fpath, 'w') as f:
                            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                        print(f"  {band} confidence set to {new_val:.1f}")
                        time.sleep(0.4)
                    except ValueError:
                        print("  Invalid — must be 0.0–1.0")
                        time.sleep(0.8)

    def _geometry_report(self) -> None:
        """Load venue with session distances and print geometry report."""
        _clear()
        if not self._require_venue(silent=True):
            print("  No venue selected.")
            input("  Press Enter...")
            return
        try:
            from core.geometry import load_venue_profile, print_geometry_report
            profile = load_venue_profile(self.session.venue_id, session=self.session)
            print_geometry_report(profile)
            if self.session.mic_placement.has_distances:
                print("  (Distances from session -- rangefinder measurements)")
            else:
                print("  (Distances computed from venue YAML positions -- no rangefinder data)")
        except Exception as e:
            print(f"  Error: {e}")
        print()
        input("  Press Enter to continue...")

    # ── Session ────────────────────────────────────────────────────────────

    def _session_menu(self) -> None:
        while True:
            _clear()
            print(_sep())
            print(f"SESSION -- {self.session.date or 'no date'}")
            print(f"Venue: {self._venue_name()}")
            print(_sep())
            print()
            x32 = f"{self.session.x32_ip}" if self.session.x32_ip else "not set"
            sl  = self.session.setlist_file or "not set"
            print(f"  1. X32 Connection   {x32}")
            print(f"  2. Mic Placement    {self.session.mic_placement.status}")
            print(f"  3. Setlist          {sl}")
            print(f"  4. Notes            {(self.session.notes or 'none')[:45]}")
            print(f"  5. Print checklist")
            print()
            print("  0. Back")
            print()
            c = _prompt()
            if   c == '1': self._x32_screen()
            elif c == '2': self._mic_placement_screen()
            elif c == '3': self._setlist_screen()
            elif c == '4': self._notes_screen()
            elif c == '5': self._print_checklist()
            elif c == '0': break

    def _x32_screen(self) -> None:
        _clear()
        print(_sep())
        print("X32 CONNECTION")
        print(_sep())
        print()
        ip = _edit_str("X32 IP address", self.session.x32_ip)
        if ip != self.session.x32_ip:
            self.session.x32_ip = ip
        port_str = input(f"  Port [{self.session.x32_port}] (Enter=keep): ").strip()
        if port_str.isdigit():
            self.session.x32_port = int(port_str)
        self.session.save()
        print(f"\n  Saved: {self.session.x32_ip}:{self.session.x32_port}")
        input("  Press Enter to continue...")

    def _mic_placement_screen(self) -> None:
        """Mic placement distances -- the primary arrival task."""
        while True:
            _clear()
            mp = self.session.mic_placement
            print(_sep())
            print(f"MIC PLACEMENT  ({self._venue_name()})")
            print(_sep())
            print("Measure with rangefinder -- mic stand in final position.")
            print("Changes save automatically.")
            print()

            def _f(v): return f"{v:.2f}m" if v is not None else "not set"
            print(f"  1. Speaker L to mic    {_f(mp.speaker_l_to_mic)}")
            print(f"  2. Speaker R to mic    {_f(mp.speaker_r_to_mic)}")
            print(f"  3. Sub L to mic        {_f(mp.sub_l_to_mic)}")
            print(f"  4. Sub R to mic        {_f(mp.sub_r_to_mic)}")
            print(f"  5. Speaker height      {_f(mp.speaker_height)}")
            print(f"  6. Mic height          {_f(mp.mic_height)}")
            print(f"  7. Description         {mp.description or 'not set'}")
            print()
            conf = f"confirmed {mp.measured_at}" if mp.distances_confirmed else "not confirmed"
            print(f"  8. Mark confirmed      [{conf}]")
            print()
            print("  0. Back")
            print()
            c = _prompt()
            if c == '0': break
            elif c == '1': mp.speaker_l_to_mic = _edit_float("Speaker L to mic", mp.speaker_l_to_mic)
            elif c == '2': mp.speaker_r_to_mic = _edit_float("Speaker R to mic", mp.speaker_r_to_mic)
            elif c == '3': mp.sub_l_to_mic     = _edit_float("Sub L to mic",     mp.sub_l_to_mic)
            elif c == '4': mp.sub_r_to_mic     = _edit_float("Sub R to mic",     mp.sub_r_to_mic)
            elif c == '5': mp.speaker_height   = _edit_float("Speaker height",   mp.speaker_height)
            elif c == '6': mp.mic_height       = _edit_float("Mic height",       mp.mic_height) or 1.5
            elif c == '7': mp.description      = _edit_str("Description",        mp.description)
            elif c == '8':
                if mp.has_distances:
                    mp.distances_confirmed = True
                    mp.measured_at = time.strftime("%H:%M")
                    print(f"  Confirmed at {mp.measured_at}")
                    time.sleep(0.5)
                else:
                    print("  Set speaker L and R distances first.")
                    time.sleep(1.0)
            self.session.save()

    def _setlist_screen(self) -> None:
        _clear()
        print(_sep())
        print("SETLIST")
        print(_sep())
        print()
        setlists = (sorted(Path('config').glob('setlist*.yaml')) +
                    sorted(Path('config').glob('*setlist*.yaml')))
        if setlists:
            print("  Available:")
            for i, f in enumerate(setlists, 1):
                m = " <- active" if f.name == self.session.setlist_file else ""
                print(f"    {i}. {f.name}{m}")
            print()
            c = _prompt("  Pick number or type filename")
            if c.isdigit():
                idx = int(c) - 1
                if 0 <= idx < len(setlists):
                    self.session.setlist_file = setlists[idx].name
            elif c:
                self.session.setlist_file = c
        else:
            sf = _edit_str("Setlist filename", self.session.setlist_file)
            self.session.setlist_file = sf
        self.session.save()
        input("  Saved. Press Enter...")

    def _notes_screen(self) -> None:
        _clear()
        print(_sep())
        print("SESSION NOTES")
        print(_sep())
        print(f"  Current: {self.session.notes or 'none'}")
        print()
        notes = input("  New notes (Enter=keep): ").strip()
        if notes:
            self.session.notes = notes
            self.session.save()
        input("  Saved. Press Enter...")

    def _print_checklist(self) -> None:
        """Pre-show checklist derived from venue PA settings + session state."""
        _clear()
        print(_sep())
        print("PRE-SHOW CHECKLIST")
        print(f"{self._venue_name()}  |  {self.session.date or 'no date'}")
        print(_sep())
        print()

        if self.session.venue_id:
            fpath = Path(f'config/venues/{self.session.venue_id}.yaml')
            if fpath.exists():
                with open(fpath, 'r') as f:
                    vd = yaml.safe_load(f)
                pa_items = (vd.get('venue', {}).get('pa_settings') or [])
                if pa_items:
                    print("  PA SETTINGS:")
                    for item in pa_items:
                        tag = "  [REQUIRED]" if item.get('required') else ""
                        print(f"  [ ] {item['label']}{tag}")
                    print()
                stage_type = vd.get('venue', {}).get('stage', {}).get('type', '')
                if stage_type == 'open_air':
                    print("  [ ] Windscreen fitted on AT2035  [REQUIRED -- outdoor]")
                    print()

        print("  MIC SETUP:")
        print("  [ ] Phantom power ON (PreSonus Ch1 48V)")
        mp = self.session.mic_placement
        if mp.distances_confirmed:
            print(f"  [x] Mic distances confirmed ({mp.measured_at})")
        else:
            print("  [ ] Enter mic distances: Settings -> Session -> Mic Placement")
        print()

        print("  X32:")
        ip = self.session.x32_ip
        print(f"  [ ] Confirm X32 IP: {ip if ip else '(not set -- enter in Session -> X32 Connection)'}")
        print()

        print("  BEFORE SOUNDCHECK:")
        print("  [ ] Run ambient capture ('a' -> 'e') with PA on, no one playing")
        print()
        input("  Press Enter to continue...")

    # ── Mic & Audio ────────────────────────────────────────────────────────

    def _mic_menu(self) -> None:
        while True:
            _clear()
            print(_sep())
            print("MIC & AUDIO")
            print(_sep())
            print()
            print(f"  1. Audio device    {self._mic_status()}")
            print(f"  2. Test input level  (5s live meter)")
            if self._is_outdoor():
                print()
                print("  [!!] WINDSCREEN REQUIRED -- outdoor venue")
            print()
            print("  0. Back")
            print()
            c = _prompt()
            if   c == '1': self._list_devices()
            elif c == '2': self._test_level()
            elif c == '0': break

    def _list_devices(self) -> None:
        _clear()
        try:
            from core.audio_capture import AudioCapture
            ac = AudioCapture(device_name_match='', preferred_sample_rate=48000)
            print(ac.list_devices())
        except Exception as e:
            print(f"  Could not list devices: {e}")
        input("\n  Press Enter...")

    def _test_level(self) -> None:
        """5-second live input level meter."""
        _clear()
        print(_sep())
        print("MIC INPUT LEVEL TEST -- 5 seconds")
        print("Target: -12dBFS to -6dBFS peaks during loud passages")
        print(_sep())
        print()
        try:
            import sounddevice as sd
            import numpy as np
            print("  Listening... play or speak loudly:")
            print()
            for _ in range(5):
                audio = sd.rec(int(0.3 * 48000), samplerate=48000,
                               channels=1, dtype='float32', blocking=True)
                peak = float(np.max(np.abs(audio)))
                peak_db = 20 * np.log10(max(peak, 1e-10))
                bar_len = int(max(0, peak_db + 60) / 60 * 32)
                bar  = '#' * bar_len + '.' * (32 - bar_len)
                note = (" <- TOO HOT" if peak_db > -6
                        else " OK"     if peak_db > -12
                        else " <- low")
                print(f"  [{bar}]  {peak_db:+5.1f}dBFS{note}")
        except Exception as e:
            print(f"  Level test unavailable: {e}")
        print()
        input("  Press Enter...")

    # ── Band ───────────────────────────────────────────────────────────────

    def _band_menu(self) -> None:
        while True:
            _clear()
            print(_sep())
            print("BAND")
            print(_sep())
            print()
            print(f"  1. View channel map")
            print(f"  2. View instrument priors  ({self.session.venue_id or 'no venue'})")
            print(f"  3. Reset priors for this venue")
            print()
            print("  0. Back")
            print()
            c = _prompt()
            if   c == '1': self._view_channels()
            elif c == '2': self._view_priors()
            elif c == '3': self._reset_priors()
            elif c == '0': break

    def _view_channels(self) -> None:
        _clear()
        try:
            with open('config/band.yaml', 'r') as f:
                band = yaml.safe_load(f)
            chs = band.get('channels', {})
            print(_sep())
            print("CHANNEL MAP")
            print(_sep())
            print()
            print(f"  {'Ch':>3}  {'Label':<22}  {'Instrument type'}")
            print(f"  {'-'*50}")
            for num, ch in sorted(chs.items()):
                label = ch.get('label', '?')
                instr = ch.get('instrument_type', '?')
                print(f"  {num:>3}  {label:<22}  {instr}")
        except Exception as e:
            print(f"  Error: {e}")
        print()
        input("  Press Enter...")

    def _view_priors(self) -> None:
        _clear()
        vid   = self.session.venue_id or 'default'
        fpath = Path(f'config/instrument_priors_{vid}.yaml')
        print(_sep())
        print(f"INSTRUMENT PRIORS -- {vid}")
        print(_sep())
        print()
        if not fpath.exists():
            print(f"  No prior file for {vid}.")
            print("  Default priors from band.yaml will be used.")
        else:
            try:
                with open(fpath, 'r') as f:
                    priors = yaml.safe_load(f) or {}
                for instr, bands in priors.items():
                    print(f"  {instr}:")
                    if isinstance(bands, dict):
                        for band, val in bands.items():
                            print(f"    {band:<16} {val:+.2f}dB")
                    print()
            except Exception as e:
                print(f"  Error: {e}")
        print()
        input("  Press Enter...")

    def _reset_priors(self) -> None:
        vid   = self.session.venue_id or 'default'
        fpath = Path(f'config/instrument_priors_{vid}.yaml')
        _clear()
        if not fpath.exists():
            print(f"  No prior file to reset for {vid}.")
        else:
            c = input(f"  Reset all priors for {vid}? Type 'yes' to confirm: ").strip()
            if c.lower() == 'yes':
                fpath.unlink()
                print("  Priors reset to defaults.")
            else:
                print("  Cancelled.")
        input("  Press Enter...")

    # ── System ─────────────────────────────────────────────────────────────

    def _system_menu(self) -> None:
        _clear()
        print(_sep())
        print("SYSTEM")
        print(_sep())
        print()
        print("  Runtime options are set via CLI flags:")
        print("  --display       launch spectrum display window")
        print("  --log-level     minimal | summary | full")
        print("  --no-venue      disable venue profile loading")
        print()
        print("  Passive mode is ON -- forward model logs only.")
        print("  Activates channel-level recommendations after")
        print("  R^2 > 0.70 is validated on June 13.")
        print()
        try:
            import importlib.metadata
            ver = importlib.metadata.version('foh-assistant')
        except Exception:
            ver = "Phase 2"
        print(f"  FOH Assistant {ver}")
        print()
        input("  Press Enter to continue...")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _venue_name(self) -> str:
        if not self.session.venue_id:
            return "no venue"
        fpath = Path(f'config/venues/{self.session.venue_id}.yaml')
        if not fpath.exists():
            return self.session.venue_id
        try:
            with open(fpath, 'r') as f:
                d = yaml.safe_load(f)
            return d.get('venue', {}).get('name', self.session.venue_id)
        except Exception:
            return self.session.venue_id

    def _session_status(self) -> str:
        mp = self.session.mic_placement
        if mp.distances_confirmed:
            return f"mic confirmed {mp.measured_at}"
        if mp.has_distances:
            return "distances set, not confirmed"
        return "mic distances not set"

    def _mic_status(self) -> str:
        try:
            from core.audio_capture import AudioCapture
            ac = AudioCapture(device_name_match='PreSonus', preferred_sample_rate=48000)
            ac.find_device()   # raises RuntimeError if not found
            return "AT2035 via PreSonus [OK]"
        except Exception:
            pass
        return "[!!] check connection"

    def _band_status(self) -> str:
        try:
            with open('config/band.yaml', 'r') as f:
                band = yaml.safe_load(f)
            n    = len(band.get('channels', {}))
            name = band.get('band', 'Unknown Band')
            return f"{name} -- {n} channels [OK]"
        except Exception:
            return "[!!] band.yaml not found"

    def _is_outdoor(self) -> bool:
        if not self.session.venue_id:
            return False
        fpath = Path(f'config/venues/{self.session.venue_id}.yaml')
        try:
            with open(fpath, 'r') as f:
                d = yaml.safe_load(f)
            return d.get('venue', {}).get('stage', {}).get('type') == 'open_air'
        except Exception:
            return False

    def _require_venue(self, silent: bool = False) -> bool:
        """Return True if a venue is selected. Print warning if not."""
        if self.session.venue_id:
            return True
        if not silent:
            print("  No venue selected. Use 'Select venue' first.")
            input("  Press Enter...")
        return False
