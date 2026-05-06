"""Soundcheck / baseline mode — interactive per-channel assessment and snapshot."""

import json
import time
from pathlib import Path

from models.channel import ChannelState
from models.event import BAND_NAMES
from core.recommender import RecommendationEngine

SHOWS_DIR = Path(__file__).parent.parent / "shows"
BAND_LISTEN_SECONDS = 30
SEP = "=" * 47


def run_baseline_mode(band_cfg, profiles, active_genre, osc, capture, analyzer, logger, initial_channels):
    engine = RecommendationEngine(band_cfg, active_genre)
    channel_map = {int(k): v if isinstance(v, dict) else {"label": str(v)}
                   for k, v in band_cfg["channels"].items()}

    # Build a label → channel_num lookup
    label_to_num: dict[str, int] = {}
    for num, cfg in channel_map.items():
        lbl = cfg.get("label", f"CH{num:02d}") if isinstance(cfg, dict) else str(cfg)
        label_to_num[lbl.lower()] = num
        label_to_num[str(num)] = num

    print(f"\nBASELINE MODE -- Genre: {active_genre.id}")
    print(f"Target LUFS: {active_genre.target_lufs:.0f} | Dynamic Range: {active_genre.dynamic_range}")
    print(f"Channels available: {', '.join(c.get('label', str(n)) if isinstance(c, dict) else str(c) for n, c in sorted(channel_map.items()))}")
    print("\nCommands: <channel name or number> | 'recheck' | 'band' | 'done' | 'confirm'")
    print(SEP)

    current_channels = osc.build_channel_states()
    last_assessed_num: int | None = None

    while True:
        try:
            user_input = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nBaseline mode exited.")
            return

        if not user_input:
            continue

        if user_input == "done":
            print("Baseline not confirmed. Use 'confirm' to lock the snapshot.")
            print("Type 'confirm' to save, or Ctrl+C to abort.")
            continue

        if user_input == "confirm":
            _save_baseline(current_channels, active_genre, band_cfg["band"])
            engine.set_baseline(current_channels)
            print(f"\nBaseline locked. Saved to shows/")
            print("Transitioning to show mode... (restart with --show)\n")
            return

        if user_input == "recheck":
            if last_assessed_num is None:
                print("No channel assessed yet. Specify a channel first.")
                continue
            current_channels = osc.snapshot_all_channels()
            _assess_channel(last_assessed_num, current_channels, capture, analyzer,
                            active_genre, channel_map, "Re-assessing")
            continue

        if user_input == "band":
            _assess_full_band(current_channels, capture, analyzer, active_genre, osc)
            continue

        # Try to resolve channel
        ch_num = label_to_num.get(user_input)
        if ch_num is None:
            # Try partial label match
            for lbl, num in label_to_num.items():
                if user_input in lbl:
                    ch_num = num
                    break

        if ch_num is None:
            print(f"Unknown channel '{user_input}'. Try the label name, number, or 'band'.")
            continue

        current_channels = osc.snapshot_all_channels()
        last_assessed_num = ch_num
        _assess_channel(ch_num, current_channels, capture, analyzer,
                        active_genre, channel_map, "Assessing")


def _assess_channel(ch_num: int, channels: dict[int, ChannelState],
                    capture, analyzer, genre, channel_map: dict,
                    action: str = "Assessing") -> None:
    ch = channels.get(ch_num)
    if ch is None:
        print(f"Channel {ch_num} not in snapshot.")
        return

    cfg = channel_map.get(ch_num, {})
    label = ch.label

    print(f"\n[{label}] -- {action} vs {genre.id} profile...")
    print(f"  Fader:  {ch.fader_db:+.1f}dB")
    print(f"  Muted:  {'YES -- unmute to assess' if ch.muted else 'No'}")
    for eq in ch.eq:
        if abs(eq.gain_db) > 0.1:
            print(f"  EQ Band {eq.band_num}: {eq.gain_db:+.1f}dB @ {eq.freq_hz:.0f}Hz  Q:{eq.q:.1f}")

    # Capture a short audio sample for analysis
    print("  Listening 3s...", end=" ", flush=True)
    time.sleep(3.0)
    audio_buf, sr = capture.get_buffer()
    analysis = analyzer.analyze(audio_buf)
    print(f"LUFS: {analysis.lufs:.1f}  RMS: {analysis.rms_db:.1f}dB")

    issues_found = False

    # LUFS vs target
    lufs_dev = analysis.lufs - genre.target_lufs
    if abs(lufs_dev) > 2.0:
        direction = "hot" if lufs_dev > 0 else "low"
        print(f"\n  Overall level {abs(lufs_dev):.1f}dB {direction} vs {genre.id} target "
              f"({genre.target_lufs:.0f} LUFS)")
        if lufs_dev > 0:
            print(f"  Suggest: Pull fader to {ch.fader_db - abs(lufs_dev):.1f}dB")
        else:
            print(f"  Suggest: Push fader to {ch.fader_db + abs(lufs_dev):.1f}dB")
        issues_found = True

    # Per-band comparison
    band_vals = [analysis.bands[b] for b in BAND_NAMES if analysis.bands[b] > -85]
    if band_vals:
        median = sorted(band_vals)[len(band_vals) // 2]
        for band in BAND_NAMES:
            raw = analysis.bands[band]
            if raw <= -85:
                continue
            normalized = raw - median
            target = genre.target_for_band(band)
            dev = normalized - target
            if abs(dev) > 3.0:
                direction = "above" if dev > 0 else "below"
                print(f"\n  [{band.replace('_', ' ').upper()}] {abs(dev):.1f}dB {direction} {genre.id} target")
                # Find relevant EQ
                _suggest_eq_for_band(ch, band, dev)
                issues_found = True

    if not issues_found:
        print(f"  Status: Within {genre.id} target range")

    print()


def _assess_full_band(channels: dict[int, ChannelState], capture, analyzer,
                      genre, osc) -> None:
    print(f"\n[Combined Mix] -- Listening {BAND_LISTEN_SECONDS}s with full band...")
    print("Have the full band play together now.")
    time.sleep(BAND_LISTEN_SECONDS)

    audio_buf, sr = capture.get_buffer()
    analysis = analyzer.analyze(audio_buf)

    print(f"\nFull band assessment vs {genre.id}:")
    print(f"  LUFS: {analysis.lufs:.1f}  (target: {genre.target_lufs:.0f})")

    band_vals = [analysis.bands[b] for b in BAND_NAMES if analysis.bands[b] > -85]
    if not band_vals:
        print("  No audio detected. Is the band playing?")
        return

    median = sorted(band_vals)[len(band_vals) // 2]
    issues = []
    for band in BAND_NAMES:
        raw = analysis.bands[band]
        if raw <= -85:
            continue
        normalized = raw - median
        target = genre.target_for_band(band)
        dev = normalized - target
        if abs(dev) > 3.0:
            direction = "above" if dev > 0 else "below"
            issues.append((band, dev, direction))

    if not issues:
        print(f"  Full band mix within {genre.id} target range")
        print("  Ready to confirm baseline.")
    else:
        print(f"\n  Issues detected ({len(issues)}):")
        for band, dev, direction in issues:
            print(f"    {band.replace('_',' ').upper()}: {abs(dev):.1f}dB {direction} target")
        print("\n  Type 'recheck' after adjustments or 'confirm' when satisfied.")
    print()


def _suggest_eq_for_band(ch: ChannelState, band: str, deviation: float) -> None:
    from core.recommender import BAND_RANGES
    lo, hi = BAND_RANGES[band]
    mid_freq = (lo + hi) // 2

    best = None
    best_dist = float("inf")
    for eq in ch.eq:
        if eq.type in (0, 5):
            continue
        d = abs(eq.freq_hz - mid_freq)
        if d < best_dist:
            best_dist = d
            best = eq

    if best:
        new_gain = best.gain_db - (abs(deviation) if deviation > 0 else -abs(deviation))
        new_gain = max(-12.0, min(12.0, new_gain))
        action = "cut" if deviation > 0 else "boost"
        print(f"  Suggest: EQ Band {best.band_num} {action} to {new_gain:+.1f}dB @ {best.freq_hz:.0f}Hz")
    else:
        lo, hi = BAND_RANGES[band]
        action = "cut" if deviation > 0 else "boost"
        print(f"  Suggest: Add EQ {action} in {lo}-{hi}Hz range")


def _save_baseline(channels: dict[int, ChannelState], genre, band_name: str) -> None:
    SHOWS_DIR.mkdir(exist_ok=True)
    date_str = time.strftime("%Y-%m-%d")
    path = SHOWS_DIR / f"{date_str}_baseline.json"

    snapshot = {
        "date": date_str,
        "time": time.strftime("%H:%M:%S"),
        "band": band_name,
        "genre": genre.id,
        "channels": {},
    }
    for num, ch in sorted(channels.items()):
        snapshot["channels"][str(num)] = {
            "label": ch.label,
            "fader_db": round(ch.fader_db, 2),
            "muted": ch.muted,
            "eq": [
                {"band": eq.band_num, "type": eq.type,
                 "freq_hz": round(eq.freq_hz, 1),
                 "gain_db": round(eq.gain_db, 2),
                 "q": round(eq.q, 2)}
                for eq in ch.eq
            ],
            "comp_on": ch.comp_on,
            "comp_threshold_db": ch.comp_threshold_db,
            "gate_on": ch.gate_on,
            "gate_threshold_db": ch.gate_threshold_db,
            "hpf_on": ch.hpf_on,
            "hpf_freq_hz": round(ch.hpf_freq_hz, 1),
            "hpf_slope": ch.hpf_slope,
            "input_gain_db": round(ch.input_gain_db, 2),
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    print(f"  Baseline saved: {path}")
