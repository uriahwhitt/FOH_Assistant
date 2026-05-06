"""FOH Assistant — entry point.

Modes:
  python main.py --show          Live show advisory mode
  python main.py --baseline      Soundcheck / baseline mode
  python main.py --devices       List audio input devices and exit
  python main.py --test-osc      Connect to X32, print channel state, exit
"""

import argparse
import sys
import time
import threading
import select

from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.config_loader import load_band_config, load_genre_profiles, load_setlist, apply_band_overrides
from core.audio_capture import AudioCapture
from core.analyzer import Analyzer
from core.osc_client import X32OSCClient
from core.recommender import RecommendationEngine
from core.logger import SessionLogger


VERSION = "0.1"
SEP = "=" * 47


def main() -> None:
    parser = argparse.ArgumentParser(description="FOH Assistant")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show",       action="store_true", help="Live show advisory mode")
    group.add_argument("--baseline",   action="store_true", help="Soundcheck / baseline mode")
    group.add_argument("--soundcheck", action="store_true", help="Soundcheck advisory mode")
    group.add_argument("--devices",    action="store_true", help="List audio input devices")
    group.add_argument("--test-osc",   action="store_true", help="Test X32 connection")
    parser.add_argument("--x32-ip",      help="Override X32 IP address")
    parser.add_argument("--device-index", type=int, default=None,
                        help="Force specific audio device by index (bypasses name matching)")
    args = parser.parse_args()

    # --devices — no config needed
    if args.devices:
        band_cfg = load_band_config()
        audio_cfg = band_cfg["audio"]
        audio = AudioCapture(
            device_name_match=audio_cfg["device_name_match"],
            preferred_sample_rate=audio_cfg.get("preferred_sample_rate", 48000),
        )
        print(audio.list_devices())
        return

    # Load config
    band_cfg = load_band_config()
    profiles = apply_band_overrides(load_genre_profiles(), band_cfg)
    setlist = load_setlist()

    x32_cfg = band_cfg["x32"]
    x32_ip = args.x32_ip or x32_cfg["ip"]
    x32_port = x32_cfg["port"]
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

    # Initialize audio
    audio_cfg = band_cfg["audio"]
    capture = AudioCapture(
        device_name_match=audio_cfg["device_name_match"],
        buffer_seconds=audio_cfg.get("buffer_seconds", 2.0),
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
    logger = SessionLogger(
        band_name=band_cfg["band"],
        mode=mode_label,
        x32_ip=x32_ip,
        genre_default=default_genre,
    )

    # Print session header
    print_header(band_cfg, mode_label, active_genre, x32_ip, x32_port,
                 capture.device_name, logger.log_path, setlist)

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
        run_show_mode(band_cfg, active_genre, profiles, setlist,
                      osc, capture, analyzer, logger, channels)

    capture.stop()
    osc.close()


# ---------------------------------------------------------------------------
# Show mode
# ---------------------------------------------------------------------------

def run_show_mode(band_cfg: dict, genre, profiles: dict, setlist,
                  osc: X32OSCClient, capture: AudioCapture, analyzer: Analyzer,
                  logger: SessionLogger, initial_channels: dict) -> None:
    from types import SimpleNamespace

    engine        = RecommendationEngine(band_cfg, genre)
    thresholds    = band_cfg.get("thresholds", {})
    grace_s       = thresholds.get("transition_grace_seconds", 30)
    default_genre = band_cfg.get("default_genre", "Glam Metal")

    prev_channels    = initial_channels.copy()
    current_channels = initial_channels.copy()

    # Mutable show state shared across inner functions via SimpleNamespace
    st = SimpleNamespace(
        song_active  = False,   # True while a song is in progress
        song_idx     = -1,      # 0-based setlist index; -1 = none
        song_counter = 0,       # auto-increment for no-setlist mode
        active_genre = genre,   # updated when n loads a new song's profile
    )

    print("\nShow mode active. Press Ctrl+C to end and generate report.")
    print("  s = board state   g = room analysis   b = baseline drift")
    print("  n = next song     e = end song early\n")

    kb_queue: list[str] = []
    def kb_listener():
        while True:
            line = sys.stdin.readline().strip().lower()
            kb_queue.append(line)

    kb_thread = threading.Thread(target=kb_listener, daemon=True)
    kb_thread.start()

    # ── Song transition helpers ──────────────────────────────────────────

    def _end_current_song(silent: bool = False) -> None:
        if not st.song_active:
            return
        engine.set_transition(False)   # cancel any active grace first
        logger.log_song_end()
        engine.set_transition(True)
        if not silent:
            print(f"\n[{time.strftime('%H:%M:%S')}] SONG END — transition grace {grace_s}s")
        st.song_active = False

    def _start_song(idx: int, song) -> None:
        # Load genre profile if the song specifies one
        if song:
            sg_id = song.get("genre_profile", default_genre)
            if sg_id in profiles:
                st.active_genre = profiles[sg_id]
                engine.set_genre(st.active_genre)

        engine.set_transition(False)   # clear any leftover grace from previous song
        logger.log_song_start(song, max(idx + 1, 1), st.active_genre.id)
        engine.set_transition(True)    # enter grace for this new song

        ts = time.strftime("%H:%M:%S")
        if song:
            title  = song.get("title",  "?")
            artist = song.get("artist", "?")
            print(f"\n[{ts}] SONG START: {title} ({artist}) — {st.active_genre.id}")
        else:
            st.song_counter += 1
            print(f"\n[{ts}] SONG START #{st.song_counter}")
        print(f"  Transition grace: {grace_s}s")

        st.song_idx    = idx
        st.song_active = True

    def _handle_next() -> None:
        _end_current_song(silent=True)   # end silently; _start_song prints header

        if setlist:
            next_idx = st.song_idx + 1
            if next_idx < len(setlist):
                _start_song(next_idx, setlist[next_idx])
            else:
                print(f"\n[{time.strftime('%H:%M:%S')}] End of setlist — transition grace {grace_s}s")
        else:
            _start_song(-1, None)

    def _handle_end_early() -> None:
        if not st.song_active:
            print("\nNo song currently active.")
            return
        _end_current_song()

    # ── Main loop ────────────────────────────────────────────────────────

    try:
        while True:
            cycle_start = time.time()

            current_channels = osc.build_channel_states()

            adjustments = logger.detect_and_log_adjustments(
                prev_channels, current_channels, thresholds
            )
            for adj in adjustments:
                engine.notify_adjustment(adj.channel_num)
            prev_channels = {k: v for k, v in current_channels.items()}

            audio_buf, _ = capture.get_buffer()
            analysis = analyzer.analyze(audio_buf)
            logger.record_lufs(analysis.lufs)

            recs = engine.evaluate(analysis, current_channels)
            for rec in recs:
                logger.log_recommendation(rec)
                print(rec.format_terminal())
                print()

            while kb_queue:
                cmd = kb_queue.pop(0)
                if cmd == "s":
                    if st.song_active:
                        song_info  = (setlist[st.song_idx]
                                      if setlist and 0 <= st.song_idx < len(setlist)
                                      else None)
                        title  = (song_info.get("title",  f"Song {st.song_counter}")
                                  if song_info else f"Song {st.song_counter}")
                        artist = song_info.get("artist", "") if song_info else ""
                        elapsed_s = time.time() - logger._current_song_start
                        m, s = divmod(int(max(elapsed_s, 0)), 60)
                        artist_str = f" ({artist})" if artist else ""
                        print(f"\nCurrent song: {title}{artist_str}"
                              f" — {st.active_genre.id}  [{m}:{s:02d} elapsed]")
                    print_board_state(current_channels)
                elif cmd == "g":
                    print_room_analysis(analysis, st.active_genre)
                elif cmd == "b":
                    print_baseline_drift(current_channels, engine)
                elif cmd == "n":
                    _handle_next()
                elif cmd == "e":
                    _handle_end_early()

            elapsed = time.time() - cycle_start
            time.sleep(max(0, 1.0 - elapsed))

    except KeyboardInterrupt:
        pass
    finally:
        if st.song_active:
            logger.log_song_end()
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

    def kb_listener():
        while True:
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

            for ch in current_channels.values():
                hpf_msg = check_hpf(ch)
                if hpf_msg:
                    print(f"[{time.strftime('%H:%M:%S')}] {hpf_msg}")
                    print()
                gs_msg = check_gain_staging(ch)
                if gs_msg:
                    print(f"[{time.strftime('%H:%M:%S')}] {gs_msg}")
                    print()
                comp_msg = check_compressor_sanity(ch)
                if comp_msg:
                    print(f"[{time.strftime('%H:%M:%S')}] {comp_msg}")
                    print()

            while kb_queue:
                cmd = kb_queue.pop(0)
                if cmd == "s":
                    print_board_state(current_channels)
                elif cmd == "g":
                    print_room_analysis(analysis, genre)
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

    print(f"\n{'CH':>3}  {'Label':<18} {'Fader':>7}  {'Muted':>6}  {'RMS':>8}  EQ Band 2")
    print("-" * 65)
    for num in sorted(channels):
        ch = channels[num]
        eq2 = ch.eq[1]
        mute_str = "MUTED" if ch.muted else ""
        print(f"  {num:>2}  {ch.label:<18} {ch.fader_db:>+6.1f}dB  {mute_str:>6}  "
              f"{ch.rms_db:>7.1f}dBFS  Band2: {eq2.gain_db:+.1f}dB @ {eq2.freq_hz:.0f}Hz")

    print(f"\nMain LR fader: {main_db:+.1f}dB")
    osc.close()


# ---------------------------------------------------------------------------
# Terminal output helpers
# ---------------------------------------------------------------------------

def print_header(band_cfg, mode, genre, x32_ip, x32_port,
                 device_name, log_path, setlist) -> None:
    mode_label = mode.upper()
    print(SEP)
    print(f"FOH ASSISTANT v{VERSION} -- Phase 1")
    print(f"Band:    {band_cfg['band']}")
    print(f"Mode:    {mode_label}")
    print(f"Genre:   {genre.id}")
    print(f"X32:     {x32_ip}:{x32_port}")
    print(f"Audio:   {device_name}")
    print(f"Log:     {log_path}")
    if setlist:
        print(f"Setlist: {len(setlist)} songs loaded (display only -- Phase 1)")
    print(SEP)


def print_board_state(channels: dict) -> None:
    print(f"\n--- Board State {time.strftime('%H:%M:%S')} ---")
    print(f"{'CH':>3}  {'Label':<18} {'Fader':>7}  {'Muted':>6}  {'RMS':>8}")
    print("-" * 50)
    for num in sorted(channels):
        ch = channels[num]
        mute_str = "MUTED" if ch.muted else ""
        print(f"  {num:>2}  {ch.label:<18} {ch.fader_db:>+6.1f}dB  {mute_str:>6}  "
              f"{ch.rms_db:>7.1f}dBFS")
    print()


def print_room_analysis(analysis, genre) -> None:
    from models.event import BAND_NAMES
    print(f"\n--- Room Analysis {time.strftime('%H:%M:%S')} ---")
    print(f"LUFS:  {analysis.lufs:.1f}  (target {genre.target_lufs:.0f}  delta {analysis.lufs_delta:+.1f})")
    print(f"RMS:   {analysis.rms_db:.1f}dB")
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


if __name__ == "__main__":
    main()
