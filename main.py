"""FOH Assistant — entry point.

Modes:
  python main.py --show          Live show advisory mode
  python main.py --baseline      Soundcheck / baseline mode
  python main.py --setup         Pre-soundcheck X32 audit (one-shot)
  python main.py --devices       List audio input devices and exit
  python main.py --test-osc      Connect to X32, print channel state, exit
"""

import argparse
import sys
import time
import threading
import select
from typing import Optional

from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.config_loader import (load_band_config, load_genre_profiles, load_setlist,
                                  load_soundcheck, apply_band_overrides)
from core.audio_capture import AudioCapture
from core.analyzer import Analyzer
from core.osc_client import X32OSCClient
from core.recommender import RecommendationEngine
from core.logger import SessionLogger


VERSION = "0.1"
SEP = "=" * 47

# Fader targets implied by genre instrument weight priority
_PRIORITY_FADER_TARGET = {
    "very_high": -3.0, "high": -6.0, "medium": -10.0, "low": -15.0, "none": -90.0,
}


# ---------------------------------------------------------------------------
# Setlist navigator
# ---------------------------------------------------------------------------

class SetlistNavigator:
    """Slot-based setlist navigation with alternate, skip, and swap support."""

    def __init__(self, songs: list):
        self._songs: list = list(songs)
        self._slot_map: dict = {}       # slot_num → index in _songs
        self._played: set = set()
        self._skipped: set = set()
        self._current_slot: Optional[int] = None
        self._assign_slots()

    def _assign_slots(self) -> None:
        counter = 1
        for i, song in enumerate(self._songs):
            if song.get("status", "confirmed") == "alternate":
                continue
            slot = song.get("slot")
            if slot is not None:
                self._slot_map[int(slot)] = i
            else:
                self._slot_map[counter] = i
                counter += 1

    def confirmed_slots(self) -> list:
        return sorted(self._slot_map)

    def song_at_slot(self, slot: int) -> Optional[dict]:
        idx = self._slot_map.get(slot)
        return self._songs[idx] if idx is not None else None

    def alternate_for_slot(self, slot: int) -> Optional[dict]:
        for song in self._songs:
            if song.get("status") == "alternate" and song.get("replaces") == slot:
                return song
        return None

    def alternates(self) -> list:
        return [s for s in self._songs if s.get("status") == "alternate"]

    def alternates_for_current_set(self) -> list:
        """Return alternates whose _set matches the current song's set."""
        current_set = None
        if self._current_slot is not None:
            song = self.song_at_slot(self._current_slot)
            if song:
                current_set = song.get("_set", 1)
        return [
            s for s in self._songs
            if s.get("status") == "alternate"
            and (current_set is None or s.get("_set") == current_set)
        ]

    def _next_unplayed_slot(self, after: Optional[int]) -> Optional[int]:
        slots = self.confirmed_slots()
        if after is None:
            candidates = slots
        else:
            try:
                idx = slots.index(after)
                candidates = slots[idx + 1:]
            except ValueError:
                candidates = [s for s in slots if s > after]
        return next((s for s in candidates if s not in self._skipped), None)

    def advance(self) -> Optional[tuple]:
        slot = self._next_unplayed_slot(self._current_slot)
        if slot is None:
            return None
        self._current_slot = slot
        return slot, self.song_at_slot(slot)

    def jump_to(self, slot: int) -> Optional[tuple]:
        song = self.song_at_slot(slot)
        if song is None:
            return None
        self._current_slot = slot
        return slot, song

    def go_back(self) -> Optional[tuple]:
        slots = self.confirmed_slots()
        if self._current_slot is None:
            return None
        before = [s for s in slots if s < self._current_slot]
        if not before:
            return None
        slot = before[-1]
        self._current_slot = slot
        return slot, self.song_at_slot(slot)

    def skip_current(self) -> None:
        if self._current_slot is not None:
            self._skipped.add(self._current_slot)

    def swap(self, slot: int) -> Optional[tuple]:
        alt = self.alternate_for_slot(slot)
        if alt is None:
            return None
        idx = self._slot_map.get(slot)
        if idx is None:
            return None
        self._songs[idx] = {**alt, "status": "confirmed", "slot": slot}
        return slot, self._songs[idx]

    def insert_after_current(self, song: dict) -> int:
        """Insert a song immediately after the current slot.

        All confirmed slots after the current position are bumped up by 1
        so the inserted song plays next without dropping any existing song.
        """
        if self._current_slot is None:
            # Nothing playing yet — append instead
            return self.add_song(
                song.get("title", "Untitled"), song.get("genre_profile", "")
            )[0]

        # Bump every confirmed slot after current up by 1 (highest first to avoid collisions)
        slots_after = sorted(
            [s for s in self._slot_map if s > self._current_slot], reverse=True
        )
        for old_slot in slots_after:
            song_idx = self._slot_map.pop(old_slot)
            new_slot = old_slot + 1
            self._slot_map[new_slot] = song_idx
            self._songs[song_idx] = {**self._songs[song_idx], "slot": new_slot}

        # Place inserted song at current + 1
        new_slot = self._current_slot + 1
        new_song = {**song, "status": "confirmed", "slot": new_slot}
        self._songs.append(new_song)
        self._slot_map[new_slot] = len(self._songs) - 1
        return new_slot

    def add_song(self, title: str, genre: str) -> tuple:
        slots = self.confirmed_slots()
        new_slot = (max(slots) + 1) if slots else 1
        new_song = {"title": title, "genre_profile": genre,
                    "status": "confirmed", "slot": new_slot}
        self._songs.append(new_song)
        self._slot_map[new_slot] = len(self._songs) - 1
        return new_slot, new_song

    def mark_played(self, slot: int) -> None:
        self._played.add(slot)

    def flag_channels_for_genre(self, genre, channels: dict) -> list:
        """Return warning strings for channels significantly off from genre weight targets."""
        flags = []
        for ch in channels.values():
            weight = genre.weight_for_channel(ch.label)
            if weight is None:
                continue
            target = _PRIORITY_FADER_TARGET.get(weight.priority, -10.0)
            diff = ch.fader_db - target
            if abs(diff) >= 2.0:
                direction = "above" if diff > 0 else "below"
                flags.append(
                    f"  {ch.label} fader {abs(diff):.0f}dB {direction} "
                    f"{genre.id} weight target"
                )
        return flags

    def format_setlist(self, in_set_break: bool = False) -> str:
        sep = "-" * 55
        lines = ["", sep]

        current_set = None
        for slot in self.confirmed_slots():
            song = self.song_at_slot(slot)
            if song is None:
                continue

            song_set = song.get("_set", 1)
            if song_set != current_set:
                if current_set is not None:
                    lines.append("")
                    if in_set_break and current_set == 1 and song_set == 2:
                        lines.append("  ** SET BREAK - break in progress **")
                    lines.append(sep)
                lines.append(f"  SET {song_set}  (use slot numbers below with n{{N}})")
                lines.append(sep)
                current_set = song_set

            title = song.get("title", "?")
            genre = song.get("genre_profile", "")
            genre_str = f"[{genre}]" if genre else ""
            vocalist = song.get("vocalist", "")
            vocal_flag = f" *{vocalist}*" if vocalist and vocalist != "Stephanie" else ""

            if slot == self._current_slot:
                marker = ">>"
                suffix = "  <-- HERE"
            else:
                marker = "  "
                suffix = ""

            if slot in self._played:
                status_str = "done"
            elif slot in self._skipped:
                status_str = "skip"
            else:
                status_str = "    "

            lines.append(
                f"{marker} {slot:>3}.  {title:<30} {genre_str:<14}"
                f" [{status_str}]{vocal_flag}{suffix}"
            )

        # Alternates grouped by set
        alts_by_set: dict = {}
        for alt in self.alternates():
            s = alt.get("_set", 1)
            alts_by_set.setdefault(s, []).append(alt)

        for set_num in sorted(alts_by_set):
            lines.append("")
            lines.append(f"  ALTERNATES - Set {set_num}")
            for alt in alts_by_set[set_num]:
                title = alt.get("title", "?")
                artist = alt.get("artist", "")
                genre = alt.get("genre_profile", "")
                vocalist = alt.get("vocalist", "")
                vocal_flag = f" *{vocalist}*" if vocalist and vocalist != "Stephanie" else ""
                artist_str = f" ({artist})" if artist else ""
                lines.append(f"       {title}{artist_str} [{genre}]{vocal_flag}")

        lines.append(sep)
        return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="FOH Assistant")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show",        action="store_true", help="Live show advisory mode")
    group.add_argument("--baseline",    action="store_true", help="Soundcheck / baseline mode")
    group.add_argument("--soundcheck",  action="store_true", help="Soundcheck advisory mode")
    group.add_argument("--setup",       action="store_true", help="Pre-soundcheck X32 audit (one-shot)")
    group.add_argument("--setup-venue", action="store_true", help="Interactive venue geometry setup wizard")
    group.add_argument('--settings',   action='store_true',
                       help='Open settings menu (venue, session, mic placement)')
    group.add_argument("--devices",     action="store_true", help="List audio input devices")
    group.add_argument("--test-osc",    action="store_true", help="Test X32 connection")
    parser.add_argument("--x32-ip",      help="Override X32 IP address")
    parser.add_argument("--device-index", type=int, default=None,
                        help="Force specific audio device by index (bypasses name matching)")
    parser.add_argument("--venue",     default=None, help="Venue profile ID to load")
    parser.add_argument("--no-venue",  action="store_true", help="Disable venue profile loading")
    parser.add_argument('--display', action='store_true',
                        help='Launch live spectrum display window alongside terminal')
    parser.add_argument("--log-level", default=None, help="Logging level: minimal, summary, full")
    args = parser.parse_args()

    # --setup-venue — no config needed
    if args.setup_venue:
        from core.geometry import run_setup_venue_wizard
        run_setup_venue_wizard()
        return

    # --settings — standalone settings menu, no audio/OSC needed
    if args.settings:
        from core.settings import SettingsMenu
        from models.session import SessionConfig
        session_cfg = SessionConfig.load()
        if args.venue:
            session_cfg.venue_id = args.venue
        SettingsMenu(session=session_cfg, running_show=False).run()
        return

    # --devices — no config needed
    if args.devices:
        band_cfg = load_band_config()
        audio_cfg = band_cfg["audio"]
        audio = AudioCapture(
            device_name_match=audio_cfg.get("device_name_match", ""),
            preferred_sample_rate=audio_cfg.get("preferred_sample_rate", 48000),
        )
        print(audio.list_devices())
        return

    # Load config
    band_cfg = load_band_config()
    profiles = apply_band_overrides(load_genre_profiles(), band_cfg)
    setlist = load_setlist()

    # Load session — provides IP, venue, and mic distances for show/soundcheck modes
    from models.session import SessionConfig
    session_cfg = SessionConfig.load()

    x32_cfg = band_cfg["x32"]
    # CLI flag > session file > band.yaml
    x32_ip = args.x32_ip or session_cfg.x32_ip or x32_cfg["ip"]
    x32_port = session_cfg.x32_port if session_cfg.x32_ip else x32_cfg["port"]
    # CLI --venue > session venue_id > band.yaml default
    if not args.venue and session_cfg.venue_id:
        args.venue = session_cfg.venue_id
    default_genre = band_cfg.get("default_genre", "Glam Metal")

    if default_genre not in profiles:
        print(f"ERROR: default_genre '{default_genre}' not found in genre profiles.")
        print(f"Available: {list(profiles.keys())}")
        sys.exit(1)

    active_genre = profiles[default_genre]

    # --test-osc
    if args.test_osc:
        run_test_osc(band_cfg, profiles, x32_ip, x32_port, active_genre)
        return

    # --setup — pre-soundcheck audit (no audio capture needed)
    if args.setup:
        run_setup_mode(band_cfg, profiles, x32_ip, x32_port, default_genre)
        return

    # Initialize audio
    audio_cfg = band_cfg["audio"]
    capture = AudioCapture(
        device_name_match=audio_cfg.get("device_name_match", ""),
        buffer_seconds=audio_cfg.get("buffer_seconds", 3.0),
        preferred_sample_rate=audio_cfg.get("preferred_sample_rate", 48000),
        forced_device_index=args.device_index,
    )
    try:
        capture.start()
    except RuntimeError as e:
        print(f"\nERROR: {e}")
        sys.exit(1)

    analyzer = Analyzer(sample_rate=capture.sample_rate)

    # Initialize OSC
    channel_map = {int(k): v if isinstance(v, dict) else {"label": v["label"], "type": "instrument"}
                   for k, v in band_cfg["channels"].items()}
    osc = X32OSCClient(x32_ip, x32_port, channel_map,
                       poll_interval_ms=x32_cfg.get("poll_interval_ms", 500))
    try:
        info = osc.connect()
        print(f"X32 connected: {info}")
    except ConnectionError as e:
        print(f"\nERROR: {e}")
        capture.stop()
        sys.exit(1)

    # Initial channel snapshot
    channels = osc.snapshot_all_channels()

    # Session logger
    mode_label = "baseline" if args.baseline else ("soundcheck" if args.soundcheck else "show")
    log_cfg   = band_cfg.get("logging", {})
    log_level = args.log_level or log_cfg.get("level", "summary")
    logger = SessionLogger(
        band_name=band_cfg["band"],
        mode=mode_label,
        x32_ip=x32_ip,
        genre_default=default_genre,
        log_level=log_level,
    )

    # Print session header
    print_header(band_cfg, mode_label, active_genre, x32_ip, x32_port,
                 capture.device_name, logger.log_path, setlist)

    display = None   # may be set below in show mode; initialized here for teardown

    if args.baseline:
        from core.baseline import run_baseline_mode
        run_baseline_mode(band_cfg, profiles, active_genre, osc, capture,
                          analyzer, logger, channels)
    elif args.soundcheck:
        from core.soundcheck import check_hpf, check_gain_staging, check_compressor_sanity
        run_soundcheck_mode(band_cfg, active_genre, profiles, setlist,
                            osc, capture, analyzer, logger, channels,
                            check_hpf, check_gain_staging, check_compressor_sanity)
    else:
        from core.geometry import (load_venue_profile, IrregularRoomAcoustics, print_geometry_report)
        from core.mic_analyzer import MicAnalyzer, SpectrumHistory
        from core.forward_model import ForwardModel
        from core.channel_model import InstrumentPrior

        venue_id = args.venue or band_cfg.get("default_venue")
        if venue_id and not getattr(args, 'no_venue', False):
            try:
                venue_profile = load_venue_profile(venue_id, session=session_cfg)
                venue_acoustics = venue_profile.acoustics
                logger.log_venue_session_start(venue_profile)
                print_geometry_report(venue_profile)
            except Exception as e:
                print(f"[WARNING] Could not load venue '{venue_id}': {e}")
                venue_acoustics = IrregularRoomAcoustics({})
                logger.log_warning(f"Venue load failed: {e}")
        else:
            venue_acoustics = IrregularRoomAcoustics({})
            if not getattr(args, 'no_venue', False):
                logger.log_warning("No venue profile — acoustic corrections disabled")

        _conf = venue_acoustics.frequency_confidence
        _low_conf = {b: v for b, v in _conf.items() if v < 0.8}
        if _low_conf:
            print("Frequency confidence adjustments:")
            for _band, _val in _low_conf.items():
                _status = "EXCLUDED" if _val < 0.5 else "reduced threshold (×1.5)"
                print(f"  {_band:<12} {_val:.1f}  →  {_status}")
        else:
            print("Frequency confidence: all bands fully trusted")

        mic_analyzer = MicAnalyzer(venue_acoustics,
                                    silence_threshold_lufs=venue_acoustics.silence_threshold_lufs())
        forward_model = ForwardModel(venue_acoustics)
        spectrum_history = SpectrumHistory()

        prior_configs = band_cfg.get('instrument_priors', {})
        instrument_priors = {}
        for ch_num, ch_cfg in band_cfg['channels'].items():
            ch_num_int = int(ch_num)
            instr_type = ch_cfg.get('instrument_type', 'guitar') if isinstance(ch_cfg, dict) else 'guitar'
            prior_data = prior_configs.get(instr_type, {})
            instrument_priors[ch_num_int] = InstrumentPrior(instr_type, prior_data)

        from core.rta_engine import RTAEngine
        osc.build_channel_configs()
        osc.set_rta_position(post_eq=True)
        rta_engine = RTAEngine(osc)
        rta_engine.set_main_bus()
        print("RTA: monitoring Main L/R post-EQ")

        from core.display_buffer import DisplayBuffer
        display_buffer = DisplayBuffer()

        if active_genre:
            from core.third_octave import to_third_octave, normalize_third_octave
            display_buffer.update(
                genre_target_bands=normalize_third_octave(
                    to_third_octave(_genre_to_shape_array(active_genre))
                ),
                genre_name=active_genre.id,
            )

        display_buffer.update(band_confidence=venue_acoustics.frequency_confidence)

        if args.display:
            from core.display_window import launch_display
            display = launch_display(display_buffer)

        run_show_mode(band_cfg, active_genre, profiles, setlist,
                      osc, capture, analyzer, logger, channels,
                      mic_analyzer=mic_analyzer, forward_model=forward_model,
                      spectrum_history=spectrum_history, instrument_priors=instrument_priors,
                      rta_engine=rta_engine, display_buffer=display_buffer,
                      session_cfg=session_cfg, venue_id=venue_id)

    if display is not None:
        display.stop()
    capture.stop()
    osc.close()


# ---------------------------------------------------------------------------
# Show mode
# ---------------------------------------------------------------------------

def run_show_mode(band_cfg: dict, genre, profiles: dict, setlist,
                  osc: X32OSCClient, capture: AudioCapture, analyzer: Analyzer,
                  logger: SessionLogger, initial_channels: dict,
                  mic_analyzer=None, forward_model=None,
                  spectrum_history=None, instrument_priors=None,
                  rta_engine=None, display_buffer=None,
                  session_cfg=None, venue_id=None) -> None:
    from types import SimpleNamespace
    from core.ambient import AmbientCapture

    engine        = RecommendationEngine(band_cfg, genre)
    thresholds    = band_cfg.get("thresholds", {})
    grace_s       = thresholds.get("transition_grace_seconds", 30)
    default_genre = band_cfg.get("default_genre", "Glam Metal")

    prev_channels    = initial_channels.copy()
    current_channels = initial_channels.copy()

    navigator = SetlistNavigator(setlist) if setlist else None
    ambient_capture = AmbientCapture()

    st = SimpleNamespace(
        song_active   = False,
        song_idx      = -1,      # slot number of current song (1-based); -1 = none
        song_counter  = 0,       # auto-increment for no-setlist mode
        active_genre  = genre,
        in_set_break  = False,   # True during the break between sets
        pending_cmd   = None,    # multi-step command state
        pending_args  = {},
    )

    print("\nShow mode active. Press Ctrl+C to end and generate report.")
    print("  s = board state   g = room analysis   b = baseline drift")
    print("  n = next song     e = end song early   p = print setlist")
    print("  a = ambient capture   skip = skip song   sw{N} = swap slot N")
    print("  n{N} = jump to slot N   n-1 = go back   add = add song")
    print("  cal = cal scan (live)   iso <n> = iso sample for channel N")
    print("  ins = insert alternate as next song   break = set break mode")
    print("  settings = open settings menu (mic placement, X32 IP, etc.)\n")

    kb_queue: list = []
    _kb_gate = threading.Event()
    _kb_gate.set()          # open by default; cleared while settings menu is active
    _settings_state: dict = {'thread': None, 'result': None, 'active': False}

    def _show_print(msg: str, priority: bool = False) -> None:
        """Print to terminal, suppressing routine output while settings menu is open."""
        if priority:
            print(f"[!] ALERT: {msg}")
        elif not _settings_state['active']:
            print(msg)

    def kb_listener():
        while True:
            _kb_gate.wait()         # pause here when settings has exclusive stdin
            line = sys.stdin.readline().strip().lower()
            kb_queue.append(line)

    kb_thread = threading.Thread(target=kb_listener, daemon=True)
    kb_thread.start()

    # ── Genre transition output ──────────────────────────────────────────

    def _print_genre_transition(prev_g, new_g) -> None:
        from models.event import BAND_NAMES as _BANDS
        print(f"  Genre shift: {prev_g.id} → {new_g.id}")
        if abs(new_g.target_lufs - prev_g.target_lufs) >= 1.0:
            print(f"  Target LUFS: {prev_g.target_lufs:.0f} → {new_g.target_lufs:.0f}")
        for band in _BANDS:
            prev_t = prev_g.target_for_band(band)
            new_t  = new_g.target_for_band(band)
            if abs(new_t - prev_t) >= 1.0:
                prev_str = "neutral" if abs(prev_t) < 0.5 else f"{prev_t:+.0f}dB"
                new_str  = "neutral" if abs(new_t)  < 0.5 else f"{new_t:+.0f}dB"
                label = band.replace("_", "-").title()
                print(f"  {label} target: {prev_str} → {new_str}")
        if navigator:
            for flag in navigator.flag_channels_for_genre(new_g, current_channels):
                print(flag)

    # ── Song transition helpers ──────────────────────────────────────────

    def _end_current_song(silent: bool = False) -> None:
        if not st.song_active:
            return
        engine.set_transition(False)
        logger.log_song_end()
        engine.set_transition(True)
        if not silent:
            print(f"\n[{time.strftime('%H:%M:%S')}] SONG END — transition grace {grace_s}s")
        st.song_active = False

    def _start_song(slot: int, song: Optional[dict], prev_genre=None,
                    nav_type: str = "sequential") -> None:
        # Capture prev song title before state changes
        prev_title = ""
        if navigator and navigator._current_slot is not None:
            prev_s = navigator.song_at_slot(navigator._current_slot)
            prev_title = prev_s.get("title", "") if prev_s else ""

        sg_id = song.get("genre_profile", default_genre) if song else default_genre
        if sg_id in profiles:
            st.active_genre = profiles[sg_id]
            engine.set_genre(st.active_genre)

        engine.set_transition(False)
        logger.log_song_start(song, max(slot, 1), st.active_genre.id)
        engine.set_transition(True)

        ts = time.strftime("%H:%M:%S")
        if song:
            title = song.get("title", "?")
            print(f"\n[{ts}] ▶ Song {slot} — {title} [{st.active_genre.id}]")
        else:
            st.song_counter += 1
            print(f"\n[{ts}] ▶ Song {st.song_counter}")
        print(f"  Transition grace: {grace_s}s")

        if prev_genre and prev_genre.id != st.active_genre.id:
            _print_genre_transition(prev_genre, st.active_genre)

        logger.log_event(
            "SETLIST_NAV",
            detail=(f"nav_type={nav_type} slot={slot} "
                    f"song={song.get('title', '') if song else ''} "
                    f"genre={st.active_genre.id} prev_song={prev_title}"),
        )

        if navigator:
            navigator.mark_played(slot)
            navigator._current_slot = slot

        st.song_idx    = slot
        st.song_active = True

        if display_buffer is not None and st.active_genre:
            try:
                from core.third_octave import to_third_octave, normalize_third_octave
                display_buffer.update(
                    genre_target_bands=normalize_third_octave(
                        to_third_octave(_genre_to_shape_array(st.active_genre))
                    ),
                    song_name=song.get('title', '') if song else '',
                    genre_name=st.active_genre.id,
                )
            except Exception:
                pass

    def _handle_next() -> None:
        if st.in_set_break:
            st.in_set_break = False
            logger.log_event("SET_BREAK_END", detail="set break ended — starting next set")
        prev_genre = st.active_genre
        _end_current_song(silent=True)
        if navigator:
            result = navigator.advance()
            if result:
                slot, song = result
                _start_song(slot, song, prev_genre, nav_type="sequential")
            else:
                print(f"\n[{time.strftime('%H:%M:%S')}] End of setlist — transition grace {grace_s}s")
        else:
            _start_song(st.song_idx + 1, None, prev_genre, nav_type="sequential")

    def _handle_end_early() -> None:
        if not st.song_active:
            print("\nNo song currently active.")
            return
        _end_current_song()

    # ── Ambient capture (runs in background thread) ──────────────────────

    def _do_ambient_capture(bl_type: str, duration_s: int) -> None:
        from models.event import BAND_NAMES as _BANDS
        bl = ambient_capture.capture(capture, analyzer, duration_s, bl_type)
        ts = time.strftime("%H:%M:%S")
        print(f"\n[{ts}] Ambient capture complete — {bl_type} ({duration_s}s)")
        print(f"  LUFS: {bl.lufs:.1f}  RMS: {bl.rms_db:.1f}dB")
        for band in _BANDS:
            print(f"  {band:<12} {bl.bands.get(band, -90.0):>7.1f}dB")
        print("  Ambient baseline saved — crowd noise correction active")
        logger.log_event("AMBIENT_CAPTURE",
                         detail=f"type={bl_type} duration={duration_s}s",
                         extra=ambient_capture.to_log_dict())

    # ── Main loop ────────────────────────────────────────────────────────

    try:
        while True:
            cycle_start = time.time()

            # Apply settings result once the settings thread has finished
            if (_settings_state['active'] and
                    _settings_state['thread'] is not None and
                    not _settings_state['thread'].is_alive()):
                updated = _settings_state['result']
                if updated is not None:
                    if (updated.mic_placement.has_distances and
                            updated.mic_placement.speaker_l_to_mic !=
                            session_cfg.mic_placement.speaker_l_to_mic):
                        try:
                            from core.geometry import load_venue_profile
                            new_profile = load_venue_profile(
                                updated.venue_id or venue_id or '', session=updated
                            )
                            if mic_analyzer is not None:
                                mic_analyzer.venue_acoustics     = new_profile.acoustics
                                mic_analyzer.correction_curve_db = new_profile.acoustics.mic_correction_curve()
                                mic_analyzer.room_mode_mask_arr  = new_profile.acoustics.room_mode_mask()
                                from core.channel_model import FREQ_AXIS as _FA
                                mic_analyzer._confidence_mask = new_profile.acoustics.confidence_weighted_freq_mask(_FA, threshold=0.5)
                            if display_buffer is not None:
                                display_buffer.update(band_confidence=new_profile.acoustics.frequency_confidence)
                            print("[SETTINGS] Geometry reloaded with updated mic distances")
                        except Exception as _e:
                            print(f"[SETTINGS] Could not reload geometry: {_e}")
                    session_cfg = updated
                _settings_state.update({'active': False, 'thread': None, 'result': None})
                _kb_gate.set()   # resume kb_listener

            current_channels = osc.build_channel_states()
            if rta_engine is not None:
                rta_engine.check_watchdog()

            adjustments = logger.detect_and_log_adjustments(
                prev_channels, current_channels, thresholds
            )
            for adj in adjustments:
                engine.notify_adjustment(adj.channel_num)
            prev_channels = {k: v for k, v in current_channels.items()}

            audio_buf, _ = capture.get_buffer()
            analysis = analyzer.analyze(audio_buf)
            logger.record_lufs(analysis.lufs)

            # Phase 2 mic analysis runs first so mic_result is available for the recommender
            mic_result = None
            if mic_analyzer is not None and forward_model is not None:
                if osc._config_dirty:
                    osc.update_dirty_configs()
                mic_result   = mic_analyzer.analyze(capture)
                board_rta_db = osc.board_rta_db
                if display_buffer is not None:
                    from core.mic_analyzer import normalize_to_shape_active
                    from core.channel_model import FREQ_AXIS as _FREQ_AXIS
                    from core.forward_model import _interpolate_rta_to_freq_axis
                    from core.third_octave import to_third_octave, normalize_third_octave
                    rta_1000  = _interpolate_rta_to_freq_axis(board_rta_db)
                    rta_bands = normalize_third_octave(to_third_octave(normalize_to_shape_active(rta_1000, _FREQ_AXIS)))
                    display_buffer.update(board_rta_fast=rta_bands)
                ch_configs   = osc.channel_configs
                ch_meters    = osc.channel_meters
                if spectrum_history is not None:
                    spectrum_history.push(mic_result)
                fm_result = forward_model.run(
                    channel_configs=ch_configs, channel_meters=ch_meters,
                    channel_priors=instrument_priors or {},
                    mic_analysis=mic_result, board_rta_db=board_rta_db,
                )
                if band_cfg.get('logging', {}).get('analysis_cycle', True):
                    logger.log_analysis_cycle(fm_result, mic_result)
                if display_buffer is not None and not mic_result.is_silent:
                    from core.mic_analyzer import normalize_to_shape_active
                    from core.channel_model import FREQ_AXIS as _FREQ_AXIS
                    from core.forward_model import _interpolate_rta_to_freq_axis
                    from core.third_octave import to_third_octave, normalize_third_octave
                    rta_1000  = _interpolate_rta_to_freq_axis(board_rta_db)
                    rta_bands = normalize_third_octave(to_third_octave(normalize_to_shape_active(rta_1000, _FREQ_AXIS)))
                    mic_bands = normalize_third_octave(to_third_octave(mic_result.normalized_shape_active_db))
                    display_buffer.update(
                        board_rta_bands=rta_bands,
                        mic_bands=mic_bands,
                        band_highlights=_compute_band_highlights(mic_result, st.active_genre),
                        band_peaks=_extract_band_peaks(mic_result),
                        lufs=mic_result.lufs,
                        is_silent=False,
                    )
                elif display_buffer is not None and mic_result is not None:
                    display_buffer.update(is_silent=True)
                for ch_num, meter in ch_meters.items():
                    if (meter.input_state == 'solo_onset' and
                            meter.prev_input_state not in ('solo_onset', 'solo_active')):
                        pre_spectrum = (spectrum_history.get_snapshot_before(
                            meter.timestamp_ms, offset_ms=500.0) if spectrum_history else None)
                        characterization = mic_analyzer.characterize_input_event(
                            pre_spectrum, mic_result.smoothed_spectrum_db)
                        logger.log_input_state_event(ch_num, meter.prev_input_state, meter.input_state, meter, characterization)

            if not st.in_set_break:
                _band_conf = (mic_analyzer.venue_acoustics.frequency_confidence
                              if mic_analyzer is not None else {})
                recs = engine.evaluate(analysis, current_channels,
                                       mic_analysis=mic_result,
                                       band_confidence=_band_conf)
                for rec in recs:
                    logger.log_recommendation(rec)
                    _show_print(rec.format_terminal())
                    _show_print("")
                for _sup in engine._suppressed_bands:
                    logger.log_warning(
                        f"[CONFIDENCE] Suppressed {_sup['band']} "
                        f"(conf={_sup['confidence']:.1f}, "
                        f"dev={_sup.get('deviation_db', 0.0):+.1f}dB, "
                        f"reason={_sup['reason']})"
                    )

            while kb_queue:
                cmd = kb_queue.pop(0)

                # ── Multi-step command state machine ─────────────────────

                if st.pending_cmd == "ambient_type":
                    if cmd in ("e", "c"):
                        st.pending_args["ambient_type"] = cmd
                        st.pending_cmd = "ambient_duration"
                        print("Duration in seconds [60]:")
                    else:
                        print("Invalid choice. Press 'a' again.")
                        st.pending_cmd = None
                    continue

                if st.pending_cmd == "ambient_duration":
                    duration = int(cmd) if cmd.strip().isdigit() else 60
                    bl_type = "empty" if st.pending_args.get("ambient_type") == "e" else "crowd"
                    st.pending_cmd = None
                    st.pending_args = {}
                    threading.Thread(
                        target=_do_ambient_capture, args=(bl_type, duration), daemon=True
                    ).start()
                    continue

                if st.pending_cmd == "add_name":
                    st.pending_args["title"] = cmd.strip() or "Untitled"
                    st.pending_cmd = "add_genre"
                    genre_examples = ", ".join(list(profiles.keys())[:4])
                    print(f"Genre (e.g. {genre_examples}):")
                    continue

                if st.pending_cmd == "add_genre":
                    title = st.pending_args.get("title", "Untitled")
                    # Case-insensitive lookup
                    matched = next(
                        (k for k in profiles if k.lower() == cmd.strip()), None
                    )
                    genre_id = matched or default_genre
                    st.pending_cmd = None
                    st.pending_args = {}
                    if navigator:
                        slot, song = navigator.add_song(title, genre_id)
                        ts = time.strftime("%H:%M:%S")
                        print(f"\n[{ts}] Added: {title} [{genre_id}] at slot {slot}")
                        logger.log_event("SETLIST_ADD",
                                         detail=f"slot={slot} title={title} genre={genre_id} status=unplanned")
                    continue

                if st.pending_cmd == "ins_select":
                    alts = st.pending_args.get("alternates", [])
                    st.pending_cmd = None
                    st.pending_args = {}
                    try:
                        pick = int(cmd.strip())
                        if 1 <= pick <= len(alts):
                            chosen = alts[pick - 1]
                            new_slot = navigator.insert_after_current(chosen)
                            ts = time.strftime("%H:%M:%S")
                            title = chosen.get("title", "?")
                            genre_id = chosen.get("genre_profile", "")
                            print(f"\n[{ts}] Inserted: {title} [{genre_id}]"
                                  f" — plays next (slot {new_slot})")
                            logger.log_event(
                                "SETLIST_INSERT",
                                detail=f"slot={new_slot} title={title} "
                                       f"genre={genre_id} nav_type=insert",
                            )
                        else:
                            print("\nInvalid selection — no song inserted.")
                    except ValueError:
                        print("\nInvalid selection — no song inserted.")
                    continue

                # ── Normal commands ───────────────────────────────────────

                if cmd == "s":
                    if st.song_active and navigator and navigator._current_slot is not None:
                        song_info = navigator.song_at_slot(navigator._current_slot)
                        title = song_info.get("title", f"Song {st.song_counter}") if song_info else f"Song {st.song_counter}"
                        artist = song_info.get("artist", "") if song_info else ""
                        elapsed_s = time.time() - logger._current_song_start
                        m, s_el = divmod(int(max(elapsed_s, 0)), 60)
                        artist_str = f" ({artist})" if artist else ""
                        print(f"\nCurrent song: {title}{artist_str}"
                              f" — {st.active_genre.id}  [{m}:{s_el:02d} elapsed]")
                    elif st.song_active:
                        elapsed_s = time.time() - logger._current_song_start
                        m, s_el = divmod(int(max(elapsed_s, 0)), 60)
                        print(f"\nSong {st.song_counter} — {st.active_genre.id}  [{m}:{s_el:02d} elapsed]")
                    print_board_state(current_channels)

                elif cmd == "g":
                    ambient_bl = (ambient_capture.active_baseline(is_show=True)
                                  if ambient_capture.has_baseline() else None)
                    if not ambient_capture.has_baseline():
                        logger.log_event("AMBIENT_WARNING",
                                         detail="No ambient baseline captured — raw readings only")
                    print_room_analysis(analysis, st.active_genre, ambient_bl=ambient_bl)

                elif cmd == "b":
                    print_baseline_drift(current_channels, engine)

                elif cmd == "n":
                    _handle_next()

                elif cmd == "e":
                    _handle_end_early()

                elif cmd == "p":
                    if navigator:
                        print(navigator.format_setlist(in_set_break=st.in_set_break))
                    else:
                        print("\nNo setlist loaded.")

                elif cmd == "break":
                    _end_current_song(silent=True)
                    st.in_set_break = True
                    after = navigator._current_slot if navigator else "?"
                    ts = time.strftime("%H:%M:%S")
                    print(f"\n[{ts}] SET BREAK")
                    print("  Recommendations paused. Board still monitored.")
                    print("  Run 'a' for crowd ambient capture.")
                    print("  Press 'n' when ready to start the next set.\n")
                    logger.log_event("SET_BREAK_START",
                                     detail=f"after_slot={after}")

                elif cmd == "a":
                    st.pending_cmd = "ambient_type"
                    print("\nCapture type — empty room (e) or crowd break (c)?")

                elif cmd == "cal":
                    if rta_engine is None:
                        print("\nCAL: RTA engine not available.")
                    elif mic_result is not None and mic_result.is_silent:
                        print("\nCAL: band not playing — trigger during a verse or chorus")
                    elif not rta_engine.is_available:
                        print("\nCAL: RTA busy — try again in a moment")
                    else:
                        active = [cfg for cfg in osc.channel_configs.values()
                                  if not cfg.muted]
                        if len(active) < 4:
                            print(f"\nCAL: only {len(active)} active channels — need 4+ for meaningful calibration")
                        else:
                            scan_results, prior_updates = rta_engine.run_cal_scan(
                                active, forward_model, mic_analyzer, instrument_priors or {}
                            )
                            if scan_results:
                                print_cal_report(scan_results, prior_updates)
                                logger.log_event("CAL_SCAN",
                                                 detail=f"channels={len(scan_results)} updates={len(prior_updates)}")

                elif cmd.startswith("iso"):
                    if rta_engine is None:
                        print("\nISO: RTA engine not available.")
                    elif not rta_engine.is_available:
                        print("\nISO: RTA busy — try again in a moment")
                    else:
                        parts = cmd.split()
                        ch_num = None
                        if len(parts) == 2 and parts[1].isdigit():
                            ch_num = int(parts[1])
                        else:
                            configs = osc.channel_configs
                            if configs:
                                print("\nAvailable channels:")
                                for n, cfg in sorted(configs.items()):
                                    print(f"  {n:>2}: {cfg.label} ({cfg.instrument_type})")
                                raw = input("Channel number: ").strip()
                                ch_num = int(raw) if raw.isdigit() else None
                        if ch_num and ch_num in osc.channel_configs:
                            sample_result, prior_updates = rta_engine.run_iso_sample(
                                ch_num, osc.channel_configs[ch_num],
                                forward_model, mic_analyzer, instrument_priors or {}
                            )
                            if sample_result:
                                print_iso_report(sample_result, prior_updates)
                                logger.log_event("ISO_SAMPLE",
                                                 detail=f"channel={ch_num} updates={len(prior_updates)}")
                        elif ch_num is not None:
                            print(f"\nISO: channel {ch_num} not in channel map")

                elif cmd == "settings":
                    if _settings_state['active']:
                        print("[SETTINGS] Settings menu is already open.")
                    else:
                        _kb_gate.clear()       # give settings exclusive stdin access
                        _settings_state['active'] = True

                        def _run_settings_thread(_sess=session_cfg):
                            from core.settings import SettingsMenu
                            menu = SettingsMenu(session=_sess, running_show=True)
                            _settings_state['result'] = menu.run()

                        _t = threading.Thread(target=_run_settings_thread, daemon=True)
                        _t.start()
                        _settings_state['thread'] = _t

                elif cmd == "skip":
                    if not st.song_active:
                        print("\nNo song currently active.")
                    elif navigator and navigator._current_slot is not None:
                        slot = navigator._current_slot
                        song_info = navigator.song_at_slot(slot)
                        title = song_info.get("title", "?") if song_info else "?"
                        navigator.skip_current()
                        ts = time.strftime("%H:%M:%S")
                        print(f"\n[{ts}] Skipped: {title}")
                        logger.log_event("SETLIST_SKIP",
                                         detail=f"slot={slot} title={title} status=skipped")
                        _handle_next()

                elif cmd == "add":
                    if navigator:
                        st.pending_cmd = "add_name"
                        print("\nSong name:")
                    else:
                        print("\nNo setlist loaded — cannot add.")

                elif cmd == "ins":
                    if not navigator:
                        print("\nNo setlist loaded.")
                    else:
                        alts = navigator.alternates_for_current_set()
                        if not alts:
                            print("\nNo alternates available for current set.")
                        else:
                            print("\nAlternates:")
                            for i, alt in enumerate(alts, 1):
                                vocalist = alt.get("vocalist", "")
                                flag = f"  *{vocalist}*" if vocalist and vocalist != "Stephanie" else ""
                                print(f"  {i}.  {alt.get('title', '?')}{flag}")
                            print("Number:")
                            st.pending_cmd = "ins_select"
                            st.pending_args["alternates"] = alts

                elif cmd.startswith("sw") and len(cmd) > 2:
                    try:
                        target_slot = int(cmd[2:])
                        if navigator:
                            result = navigator.swap(target_slot)
                            if result:
                                slot, song = result
                                ts = time.strftime("%H:%M:%S")
                                title = song.get("title", "?")
                                genre_id = song.get("genre_profile", st.active_genre.id)
                                new_genre = profiles.get(genre_id, st.active_genre)
                                print(f"\n[{ts}] Swap: slot {slot} → {title} [{new_genre.id}]")
                                if navigator._current_slot == slot:
                                    if new_genre.id != st.active_genre.id:
                                        _print_genre_transition(st.active_genre, new_genre)
                                        st.active_genre = new_genre
                                        engine.set_genre(new_genre)
                                logger.log_event(
                                    "SETLIST_SWAP",
                                    detail=f"slot={slot} title={title} genre={new_genre.id} nav_type=swap",
                                )
                            else:
                                print(f"\nNo alternate for slot {target_slot}.")
                        else:
                            print("\nNo setlist loaded.")
                    except ValueError:
                        pass

                elif cmd.startswith("n") and len(cmd) > 1:
                    suffix = cmd[1:]
                    if suffix == "-1":
                        if navigator:
                            prev_genre = st.active_genre
                            _end_current_song(silent=True)
                            result = navigator.go_back()
                            if result:
                                slot, song = result
                                _start_song(slot, song, prev_genre, nav_type="back")
                            else:
                                print("\nAlready at first song.")
                        else:
                            print("\nNo setlist loaded.")
                    else:
                        try:
                            target_slot = int(suffix)
                            if navigator:
                                prev_genre = st.active_genre
                                _end_current_song(silent=True)
                                result = navigator.jump_to(target_slot)
                                if result:
                                    slot, song = result
                                    _start_song(slot, song, prev_genre, nav_type="jump")
                                else:
                                    print(f"\nNo song at slot {target_slot}.")
                            else:
                                print("\nNo setlist loaded.")
                        except ValueError:
                            pass

            elapsed = time.time() - cycle_start
            time.sleep(max(0, 0.5 - elapsed))

    except KeyboardInterrupt:
        pass
    finally:
        if st.song_active:
            logger.log_song_end()
        if forward_model is not None:
            logger.log_session_summary()
        _stop_display_path()
        if session_cfg is not None:
            session_cfg.archive()
        print(f"\n{SEP}")
        print(logger.generate_report())


# ---------------------------------------------------------------------------
# Soundcheck mode (IMP-021)
# ---------------------------------------------------------------------------

def run_soundcheck_mode(band_cfg: dict, genre, profiles: dict, setlist,
                        osc: X32OSCClient, capture: AudioCapture, analyzer: Analyzer,
                        logger: SessionLogger, initial_channels: dict,
                        check_hpf, check_gain_staging, check_compressor_sanity) -> None:
    """Soundcheck advisory mode — continuous real-time advisory, no drift checks.

    Type 'confirm' to lock baseline and exit.
    Ctrl+C to abort without saving.
    """
    thresholds = band_cfg.get("thresholds", {})
    soundcheck_thresholds = thresholds.copy()
    soundcheck_thresholds["recommendation_cooldown_s"] = 20

    sc_cfg = dict(band_cfg)
    sc_cfg["thresholds"] = soundcheck_thresholds

    engine = RecommendationEngine(sc_cfg, genre)

    _sc_advisory_last: dict[tuple, float] = {}
    SC_ADVISORY_COOLDOWN_S = 60.0

    def _fire_sc_advisory(key: tuple, message: str, now: float) -> None:
        if now - _sc_advisory_last.get(key, 0.0) >= SC_ADVISORY_COOLDOWN_S:
            print(f"[{time.strftime('%H:%M:%S')}] {message}")
            print()
            _sc_advisory_last[key] = now

    current_channels = initial_channels.copy()
    default_genre = band_cfg.get("default_genre", "Glam Metal")

    x32_cfg = band_cfg.get("x32", {})
    x32_ip = x32_cfg.get("ip", "?")
    x32_port = x32_cfg.get("port", 10023)
    print(SEP)
    print("FOH ASSISTANT — SOUNDCHECK MODE")
    print(f"Band:    {band_cfg['band']}")
    print(f"Genre:   {genre.id} (soundcheck reference)")
    print(f"X32:     {x32_ip}:{x32_port}")
    print(f"Cooldown: 20s  |  No baseline set — advisory only")
    print("Type 'confirm' when satisfied to lock baseline.")
    print(SEP)

    kb_queue: list[str] = []
    _sc_kb_gate = threading.Event()
    _sc_kb_gate.set()

    def kb_listener():
        while True:
            _sc_kb_gate.wait()
            line = sys.stdin.readline().strip().lower()
            kb_queue.append(line)

    kb_thread = threading.Thread(target=kb_listener, daemon=True)
    kb_thread.start()

    try:
        while True:
            cycle_start = time.time()

            current_channels = osc.build_channel_states()
            audio_buf, _ = capture.get_buffer()
            analysis = analyzer.analyze(audio_buf)
            logger.record_lufs(analysis.lufs)

            recs = engine.evaluate(analysis, current_channels)
            for rec in recs:
                print(rec.format_terminal())
                print()

            now = time.time()
            for ch in current_channels.values():
                hpf_msg = check_hpf(ch)
                if hpf_msg:
                    _fire_sc_advisory((ch.channel_num, "hpf"), hpf_msg, now)

                gain_msg = check_gain_staging(ch)
                if gain_msg:
                    _fire_sc_advisory((ch.channel_num, "gain_staging"), gain_msg, now)

                comp_msg = check_compressor_sanity(ch)
                if comp_msg:
                    _fire_sc_advisory((ch.channel_num, "compressor"), comp_msg, now)

            while kb_queue:
                cmd = kb_queue.pop(0)
                if cmd == "s":
                    print_board_state(current_channels)
                elif cmd == "g":
                    print_room_analysis(analysis, genre)
                elif cmd == "settings":
                    _sc_kb_gate.clear()
                    def _run_sc_settings():
                        from core.settings import SettingsMenu
                        from models.session import SessionConfig
                        sc = SessionConfig.load()
                        SettingsMenu(session=sc, running_show=True).run()
                        _sc_kb_gate.set()
                    threading.Thread(target=_run_sc_settings, daemon=True).start()
                elif cmd == "confirm":
                    _soundcheck_confirm(current_channels, genre, band_cfg, logger)
                    return

            elapsed = time.time() - cycle_start
            time.sleep(max(0, 1.0 - elapsed))

    except KeyboardInterrupt:
        print("\nSoundcheck aborted — no baseline saved.")


def _soundcheck_confirm(channels: dict, genre, band_cfg: dict,
                        logger: SessionLogger) -> None:
    """Lock baseline at end of soundcheck, print summary, exit."""
    from core.baseline import _save_baseline
    _save_baseline(channels, genre, band_cfg["band"])
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    active_count = sum(1 for ch in channels.values() if ch.is_active())
    print(f"\n{SEP}")
    print(f"BASELINE LOCKED — {ts}")
    print(f"Channels captured: {len(channels)}")
    print(f"Launch show mode: python main.py --show")
    print(SEP)
    logger.log_event("SOUNDCHECK_COMPLETE",
                     detail=f"channels={len(channels)} active={active_count}")


# ---------------------------------------------------------------------------
# Setup mode (pre-soundcheck X32 audit, one-shot)
# ---------------------------------------------------------------------------

def run_setup_mode(band_cfg: dict, profiles: dict, x32_ip: str, x32_port: int,
                   default_genre: str) -> None:
    """One-shot board audit before soundcheck. Selects the genre profile from
    the soundcheck song in setlist.yaml, snapshots the X32, and prints a
    prioritized list of basic audio-theory fixes the engineer should make
    before adding any audio. No audio capture, no rolling loop."""
    from core.setup_check import (run_setup_check, SetupFinding,
                                   CRITICAL, HIGH_VALUE, ADVISORY)

    # Resolve soundcheck song + genre
    sc = load_soundcheck()
    if sc and sc.get("genre_profile") in profiles:
        genre = profiles[sc["genre_profile"]]
    else:
        genre = profiles[default_genre]

    # Connect & snapshot — same pattern as run_test_osc
    channel_map = {int(k): v if isinstance(v, dict) else {"label": str(v)}
                   for k, v in band_cfg["channels"].items()}
    osc = X32OSCClient(x32_ip, x32_port, channel_map)
    try:
        info = osc.connect(timeout=5.0)
        print(f"X32 connected: {info}")
    except ConnectionError as e:
        print(f"FAILED: {e}")
        return

    print("Reading channel state...")
    channels = osc.snapshot_all_channels()
    master_db = osc.read_main_fader()
    osc.close()

    findings = run_setup_check(channels, master_db)
    print_setup_report(band_cfg, sc, genre, channels, master_db, findings)


def print_setup_report(band_cfg: dict, sc: Optional[dict], genre,
                       channels: dict, master_db: float, findings: list) -> None:
    from core.setup_check import CRITICAL, HIGH_VALUE, ADVISORY

    print(f"\n{SEP}")
    print("FOH ASSISTANT — INITIAL SETUP CHECK")
    print(SEP)
    print(f"Band:        {band_cfg['band']}")
    if sc:
        title  = sc.get("title", "?")
        artist = sc.get("artist", "")
        artist_str = f" by {artist}" if artist else ""
        print(f"Soundcheck:  {title}{artist_str}  [{genre.id}]")
    else:
        print(f"Soundcheck:  (no soundcheck song in setlist) [{genre.id}]")
    print(f"Reference:   docs/FOH_Assistant_System_Audio_Guide.md")
    print(f"Channels:    {len(channels)} mapped")
    print(f"Master LR:   {master_db:+.1f} dB")

    # Genre context — informational
    print()
    print("----- GENRE CONTEXT -----")
    print(f"  Target LUFS:     {genre.target_lufs:.0f}")
    print(f"  Dynamic range:   {genre.dynamic_range}")
    very_high = [w.label for w in genre.instrument_weights if w.priority == "very_high"]
    if very_high:
        print(f"  Mix priority:    {' / '.join(very_high)}  (very_high)")
    if genre.notes:
        print(f"  Genre note:      {genre.notes}")

    # Findings, grouped by severity
    sections = [
        (CRITICAL,   "CRITICAL — address before opening any mic"),
        (HIGH_VALUE, "HIGH VALUE — basic audio-theory fixes"),
        (ADVISORY,   "ADVISORY — recommended polish"),
    ]
    counter = 1
    any_findings = False
    for severity, header in sections:
        items = [f for f in findings if f.severity == severity]
        if not items:
            continue
        any_findings = True
        print()
        print(f"----- {header} -----")
        for f in items:
            label = f.channel if f.channel else "Board"
            print(f"  [{counter}]  {label} — {f.issue}")
            print(f"        Action: {f.action}")
            if f.why:
                print(f"        Why:    {f.why}")
            counter += 1

    print()
    if not any_findings:
        print("----- ALL CLEAR -----")
        print("  No setup issues detected. Ready for soundcheck.")
    else:
        print(f"----- NEXT -----")
        print("  Address the items above, then run:")
        print(f"    python main.py --baseline    # capture soundcheck baseline")
    print(SEP)


# ---------------------------------------------------------------------------
# Test OSC mode
# ---------------------------------------------------------------------------

def run_test_osc(band_cfg: dict, profiles: dict, x32_ip: str, x32_port: int, genre) -> None:
    channel_map = {int(k): v if isinstance(v, dict) else {"label": str(v)}
                   for k, v in band_cfg["channels"].items()}
    osc = X32OSCClient(x32_ip, x32_port, channel_map)
    try:
        info = osc.connect(timeout=5.0)
        print(f"Connected: {info}")
    except ConnectionError as e:
        print(f"FAILED: {e}")
        return

    print("\nReading channel state...")
    channels = osc.snapshot_all_channels()
    main_db = osc.read_main_fader()

    print(f"\n{'CH':>3}  {'Label':<22} {'Fader':>7}  {'Muted':>6}  {'RMS':>8}  EQ Band 2")
    print("-" * 69)
    for num in sorted(channels):
        ch = channels[num]
        eq2 = ch.eq[1]
        mute_str = "MUTED" if ch.muted else ""
        print(f"  {num:>2}  {_display_label(ch):<22} {ch.fader_db:>+6.1f}dB  {mute_str:>6}  "
              f"{ch.rms_db:>7.1f}dBFS  Band2: {eq2.gain_db:+.1f}dB @ {eq2.freq_hz:.0f}Hz")

    print(f"\nMain LR fader: {main_db:+.1f}dB")

    # Phase 2 — build channel configs with transfer curves
    print("\nBuilding Phase 2 channel configs (transfer curves)...")
    try:
        configs = osc.build_channel_configs()
        print(f"\n{'CH':>3}  {'Label':<20} {'Instrument':<14} {'HPF':>8}  {'EQ':>6}  {'Curve'}")
        print("-" * 65)
        for ch_num in sorted(configs):
            cfg = configs[ch_num]
            hpf_str = f"{cfg.hpf_freq_hz:.0f}Hz/{cfg.hpf_slope_db_oct}dB" if cfg.hpf_enabled else "off"
            eq_str  = "on" if cfg.eq_enabled else "bypass"
            tc_str  = "ready" if cfg.transfer_curve_db is not None else "NONE"
            print(f"  {ch_num:>2}  {cfg.label:<20} {cfg.instrument_type:<14} {hpf_str:>8}  {eq_str:>6}  {tc_str}")
        print(f"\nPhase 2 channel configs: {len(configs)} channels with transfer curves")
    except Exception as e:
        print(f"  Phase 2 config build failed: {e}")

    # Phase 2 — check board_rta_db from /meters/15
    import time as _time
    _time.sleep(0.6)   # allow /meters/15 subscription to deliver a packet
    rta = osc.board_rta_db
    if rta.max() > -89.0:
        print(f"\nboard_rta_db (/meters/15): 100 bands received  [{rta.min():.1f} to {rta.max():.1f} dBFS]")
    else:
        print(f"\nboard_rta_db (/meters/15): waiting for data (emulator may not push RTA)")

    osc.close()


# ---------------------------------------------------------------------------
# Terminal output helpers
# ---------------------------------------------------------------------------

def print_header(band_cfg, mode, genre, x32_ip, x32_port,
                 device_name, log_path, setlist) -> None:
    mode_label = mode.upper()
    print(SEP)
    print(f"FOH ASSISTANT v{VERSION} -- Phase 2")
    print(f"Band:    {band_cfg['band']}")
    print(f"Mode:    {mode_label}")
    print(f"Genre:   {genre.id}")
    print(f"X32:     {x32_ip}:{x32_port}")
    print(f"Audio:   {device_name}")
    print(f"Log:     {log_path}")
    if setlist:
        print(f"Setlist: {len(setlist)} songs loaded (display only -- Phase 1)")
    print(SEP)


def _display_label(ch) -> str:
    """Return label enriched with X32 name when it differs, for terminal display only."""
    if ch.x32_name and ch.x32_name != ch.label:
        return f"{ch.label} ({ch.x32_name})"
    return ch.label


def print_board_state(channels: dict) -> None:
    print(f"\n--- Board State {time.strftime('%H:%M:%S')} ---")
    print(f"{'CH':>3}  {'Label':<22} {'Fader':>7}  {'Muted':>6}  {'RMS':>8}")
    print("-" * 54)
    for num in sorted(channels):
        ch = channels[num]
        mute_str = "MUTED" if ch.muted else ""
        print(f"  {num:>2}  {_display_label(ch):<22} {ch.fader_db:>+6.1f}dB  {mute_str:>6}  "
              f"{ch.rms_db:>7.1f}dBFS")
    print()


def print_room_analysis(analysis, genre, ambient_bl=None) -> None:
    from models.event import BAND_NAMES
    from core.ambient import AMBIENT_SNR_THRESHOLD_DB
    print(f"\n--- Room Analysis {time.strftime('%H:%M:%S')} ---")
    print(f"LUFS:  {analysis.lufs:.1f}  (target {genre.target_lufs:.0f}  delta {analysis.lufs_delta:+.1f})")
    print(f"RMS:   {analysis.rms_db:.1f}dB")

    if ambient_bl is not None:
        print(f"\n{'Band':<12} {'Raw':>8}  {'Ambient':>8}  {'Corrected':>10}  {'Target':>7}  {'Delta':>7}")
        print("-" * 62)
        for band in BAND_NAMES:
            raw   = analysis.bands.get(band, -90.0)
            amb   = ambient_bl.bands.get(band, -90.0)
            corr  = (raw - amb) if (raw - amb) > AMBIENT_SNR_THRESHOLD_DB else raw
            tgt   = genre.target_for_band(band)
            delta = analysis.band_delta.get(band, 0.0)
            print(f"  {band:<12} {raw:>7.1f}dB  {amb:>7.1f}dB  {corr:>9.1f}dB  "
                  f"{tgt:>+6.1f}dB  {delta:>+6.1f}dB")
    else:
        print(f"\n{'Band':<12} {'Level':>8}  {'Target offset':>14}  {'Delta':>8}")
        print("-" * 48)
        for band in BAND_NAMES:
            level = analysis.bands.get(band, -90.0)
            target = genre.target_for_band(band)
            delta = analysis.band_delta.get(band, 0.0)
            print(f"  {band:<12} {level:>7.1f}dB  {target:>+13.1f}dB  {delta:>+7.1f}dB")
    print()


def print_baseline_drift(channels: dict, engine: RecommendationEngine) -> None:
    baseline = engine._baseline
    if baseline is None:
        print("\nNo baseline captured yet. Run --baseline mode first.\n")
        return
    print(f"\n--- Baseline Drift {time.strftime('%H:%M:%S')} ---")
    for num in sorted(channels):
        ch = channels[num]
        base = baseline.get(num)
        if base is None:
            continue
        drift = ch.fader_db - base.fader_db
        flag = " <-- DRIFT" if abs(drift) >= 2.0 else ""
        print(f"  {ch.label:<18} {drift:>+6.1f}dB from soundcheck{flag}")
    print()


def print_cal_report(results: list, prior_updates: list) -> None:
    from datetime import datetime
    from core.rta_engine import CAL_ALPHA
    print("━" * 60)
    print(f"CAL SCAN — {datetime.now().strftime('%H:%M:%S')} — {len(results)} channels")
    print("━" * 60)
    has_findings = False
    for r in results:
        ch = r.get('channel')
        label = getattr(ch, 'label', r.get('channel', '?'))
        for band, data in r.get('bands', {}).items():
            if abs(data['deviation']) < 1.5:
                continue
            has_findings = True
            sym = '✗' if data['status'] == 'significant' else '⚠'
            print(f"  {sym}  {label:<18} {band:<12} "
                  f"predicted {data['predicted']:>+6.1f}dB  "
                  f"actual {data['actual']:>+6.1f}dB  "
                  f"dev {data['deviation']:>+5.1f}dB")
    if not has_findings:
        print("  All channels within ±1.5dB of prediction — model tracking well")
    if prior_updates:
        print()
        print(f"  Prior updates (α={CAL_ALPHA}):")
        for u in prior_updates:
            print(f"    {u['instrument']:<16} {u['band']:<12} "
                  f"{u['old']:>+6.2f} → {u['new']:>+6.2f}dB")
    print("━" * 60)


def print_iso_report(result: dict, prior_updates: list) -> None:
    if not result:
        return
    print("━" * 60)
    print(f"ISO SAMPLE — {result['channel']} ({result['instrument_type']}) "
          f"— {result['duration_s']:.0f}s")
    print("━" * 60)
    has_findings = False
    for band in result.get('board_vs_prior', {}):
        bv = result['board_vs_prior'][band]
        mv = result['mic_vs_prior'][band]
        if abs(bv) < 0.5 and abs(mv) < 0.5:
            continue
        has_findings = True
        flag = '  ⚠ prior under/over-predicts' if (abs(bv) > 1.5 or abs(mv) > 1.5) else ''
        print(f"  {band:<12} board vs prior {bv:>+6.1f}dB  "
              f"mic vs prior {mv:>+6.1f}dB{flag}")
    if not has_findings:
        print("  All bands within ±0.5dB of prior — prior is accurate")
    if prior_updates:
        print()
        from core.rta_engine import ISO_ALPHA
        print(f"  Prior updates (α={ISO_ALPHA}):")
        for u in prior_updates:
            print(f"    {u['instrument']:<16} {u['band']:<12} "
                  f"{u['old']:>+6.2f} → {u['new']:>+6.2f}dB")
    print("━" * 60)


# ---------------------------------------------------------------------------
# Display buffer helpers (IMP-053)
# ---------------------------------------------------------------------------

BAND_RANGES_DISPLAY = {
    'sub':       (20,    80),
    'bass':      (80,    200),
    'low_mid':   (200,   500),
    'mid_low':   (500,   1000),
    'mid_high':  (1000,  2000),
    'upper_mid': (2000,  4000),
    'presence':  (4000,  8000),
    'air':       (8000,  20000),
}


def _compute_band_highlights(mic_result, active_genre) -> dict:
    """Mic active-normalized shape deviation from genre target per band."""
    if not active_genre or not mic_result:
        return {}
    from core.mic_analyzer import band_average
    import numpy as _np
    mic_shape = mic_result.normalized_shape_db
    if hasattr(mic_result, 'normalized_shape_active_db'):
        _active = mic_result.normalized_shape_active_db
        if isinstance(_active, _np.ndarray) and not _np.all(_active == 0):
            mic_shape = _active
    highlights = {}
    for band, (f_lo, f_hi) in BAND_RANGES_DISPLAY.items():
        mic_avg    = band_average(mic_shape, (f_lo, f_hi))
        target_avg = float(active_genre.frequency_targets.get(band, 0.0))
        highlights[band] = mic_avg - target_avg
    return highlights


def _extract_band_peaks(mic_result) -> dict:
    """Extract (peak_hz, prominence_db) per band from mic analysis band_levels."""
    if not mic_result or not mic_result.band_levels:
        return {}
    return {
        band: (lvl.get('peak_hz', 0.0), lvl.get('peak_prominence_db', 0.0))
        for band, lvl in mic_result.band_levels.items()
    }


def _genre_to_shape_array(genre) -> 'np.ndarray':
    """Convert genre frequency_targets dict to a 1000-point FREQ_AXIS shape array."""
    import numpy as np
    from core.channel_model import FREQ_AXIS
    band_centers = {
        'sub': 50, 'bass': 150, 'low_mid': 350, 'mid_low': 750,
        'mid_high': 1500, 'upper_mid': 3000, 'presence': 6000, 'air': 14000,
    }
    items  = sorted(band_centers.items(), key=lambda x: x[1])
    freqs  = [x[1] for x in items]
    values = [float(genre.frequency_targets.get(x[0], 0.0)) for x in items]
    return np.interp(np.log10(FREQ_AXIS), np.log10(freqs), values,
                     left=values[0], right=values[-1])


# ---------------------------------------------------------------------------
# Display path — 100ms timer loop for fast mic FFT (IMP-053)
# ---------------------------------------------------------------------------

_display_timer = None


def _start_display_path(mic_analyzer, audio_capture, venue_acoustics, display_buffer):
    """Start the 100ms display-path mic FFT update loop."""
    global _display_timer

    def _tick():
        global _display_timer
        if display_buffer is not None and mic_analyzer is not None:
            try:
                fast_shape = mic_analyzer.compute_display_spectrum(
                    audio_capture, venue_acoustics
                )
                display_buffer.update(mic_shape_fast=fast_shape)
            except Exception as e:
                print(f"[DISPLAY PATH] error: {e}")
        _display_timer = threading.Timer(0.1, _tick)
        _display_timer.daemon = True
        _display_timer.start()

    _display_timer = threading.Timer(0.1, _tick)
    _display_timer.daemon = True
    _display_timer.start()


def _stop_display_path():
    global _display_timer
    if _display_timer is not None:
        _display_timer.cancel()
        _display_timer = None


if __name__ == "__main__":
    main()
