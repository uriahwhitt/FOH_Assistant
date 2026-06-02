"""Offline validation runner — test recommendation engine against recorded audio + ground truth.

Usage:
  python tools/validate.py --audio test_audio/live_show.mp3 --genre "Glam Metal"
  python tools/validate.py --audio test_audio/live_show.mp3 --genre "Glam Metal" \\
      --ground-truth test_audio/adjustments.yaml --board simulator/scenarios/baseline.yaml
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config_loader import load_band_config, load_genre_profiles, apply_band_overrides
from core.analyzer import Analyzer
from core.recommender import RecommendationEngine, Recommendation
from models.channel import ChannelState, EQBand

SEP = "=" * 47
ANALYSIS_CHUNK_S = 1.0          # analyze 1s at a time
REC_MATCH_WINDOW_S = 120        # rec within 2min of ground truth adjustment = potential match


def run_show_log_validation(log_path: str, full_report: bool = False) -> None:
    """Validate a show log JSON from Phase 2 session."""
    with open(log_path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    events = doc.get("events", [])

    # Count ANALYSIS_CYCLE events
    cycle_events = [e for e in events if e.get("type") == "ANALYSIS_CYCLE"]
    n_cycles = len(cycle_events)

    # Extract r_squared_board values
    r2_board_vals = [e["r_squared_board"] for e in cycle_events
                     if e.get("r_squared_board") is not None]
    mean_r2_board = float(np.mean(r2_board_vals)) if r2_board_vals else None

    # Input state events
    input_events = [e for e in events if e.get("type") == "INPUT_STATE_EVENT"]
    n_input = len(input_events)
    n_mic_confirmed = sum(1 for e in input_events if e.get("mic_confirmed"))
    confirm_rate = (n_mic_confirmed / n_input) if n_input > 0 else None

    # Systematic deviation by band across all cycles
    band_devs: dict[str, list] = {}
    for e in cycle_events:
        for band, data in (e.get("deviation_by_band") or {}).items():
            band_devs.setdefault(band, []).append(data.get("deviation_db", 0.0))
    band_summary = {b: round(float(np.mean(vals)), 2) for b, vals in band_devs.items() if vals}

    # PASS/FAIL criteria
    coverage_ok = n_cycles >= 15000
    r2_ok       = mean_r2_board is not None and mean_r2_board >= 0.70
    confirm_ok  = confirm_rate is None or confirm_rate >= 0.60

    overall = "PASS" if (coverage_ok and r2_ok and confirm_ok) else "FAIL"

    print(f"\n{SEP}")
    print("FOH ASSISTANT -- SHOW LOG VALIDATION")
    print(f"Log: {log_path}")
    print(SEP)
    print(f"\nCOVERAGE")
    print(f"  Analysis cycles:      {n_cycles:>8}  (target >= 15000)  {'OK' if coverage_ok else 'FAIL'}")
    print(f"\nFORWARD MODEL ACCURACY")
    if mean_r2_board is not None:
        print(f"  Mean R² (board):      {mean_r2_board:>8.4f}  (target >= 0.70)   {'OK' if r2_ok else 'FAIL'}")
        print(f"  R² samples:           {len(r2_board_vals):>8}")
    else:
        print(f"  Mean R² (board):           n/a  (no data)")
    print(f"\nINPUT STATE EVENTS")
    print(f"  Total events:         {n_input:>8}")
    print(f"  Mic confirmed:        {n_mic_confirmed:>8}")
    if confirm_rate is not None:
        print(f"  Confirmation rate:    {confirm_rate:>8.1%}  (target >= 60%)    {'OK' if confirm_ok else 'FAIL'}")
    else:
        print(f"  Confirmation rate:         n/a")
    if band_summary:
        print(f"\nSYSTEMATIC DEVIATION BY BAND (mean dB)")
        for band, dev in sorted(band_summary.items(), key=lambda x: abs(x[1]), reverse=True):
            flag = " <-- review" if abs(dev) >= 3.0 else ""
            print(f"  {band:<14} {dev:>+7.2f}dB{flag}")
    print(f"\nVERDICT: {overall}")
    print(SEP)


def main():
    parser = argparse.ArgumentParser(description="FOH Assistant Offline Validator")
    parser.add_argument("--audio",        default=None,   help="Audio file path (MP3/WAV/FLAC)")
    parser.add_argument("--genre",        default=None,   help="Genre profile ID (e.g. 'Glam Metal')")
    parser.add_argument("--board",        default=None,   help="Simulator scenario YAML for initial board state")
    parser.add_argument("--ground-truth", default=None,   help="Ground truth adjustments YAML")
    parser.add_argument("--summary-only", action="store_true", help="Print only the summary report")
    parser.add_argument("--show-log",     default=None,   help="Path to show log JSON for Phase 2 validation")
    parser.add_argument("--report",       action="store_true", help="Print full report details")
    args = parser.parse_args()

    if args.show_log:
        run_show_log_validation(args.show_log, args.report)
        return
    if not args.audio or not args.genre:
        parser.error("--audio and --genre are required")

    # Load config
    band_cfg = load_band_config()
    profiles = apply_band_overrides(load_genre_profiles(), band_cfg)

    if args.genre not in profiles:
        print(f"ERROR: Genre '{args.genre}' not found. Available: {list(profiles.keys())}")
        sys.exit(1)

    genre = profiles[args.genre]

    # Load audio
    print(f"\nLoading audio: {args.audio}")
    try:
        import librosa
        audio_raw, sr = librosa.load(args.audio, sr=None, mono=True)
        duration_s = len(audio_raw) / sr
        print(f"  Duration: {duration_s/60:.1f}m  |  Sample rate: {sr}Hz")
    except Exception as e:
        print(f"ERROR loading audio: {e}")
        sys.exit(1)

    # Load board state
    channel_map = {int(k): v if isinstance(v, dict) else {"label": str(v)}
                   for k, v in band_cfg["channels"].items()}
    channels = _build_channels_from_scenario(args.board, channel_map)

    # Load ground truth
    ground_truth: list[dict] = []
    if args.ground_truth:
        with open(args.ground_truth, "r", encoding="utf-8") as f:
            raw_gt = yaml.safe_load(f)
        ground_truth = raw_gt.get("adjustments", [])
        print(f"  Ground truth: {len(ground_truth)} engineer adjustments")

    # Run analysis
    analyzer = Analyzer(sample_rate=sr)
    engine = RecommendationEngine(band_cfg, genre)

    all_recs: list[tuple[float, Recommendation]] = []   # (timestamp_s, rec)
    buf_size = int(sr * 2.0)
    chunk_size = int(sr * ANALYSIS_CHUNK_S)
    total_chunks = int(duration_s / ANALYSIS_CHUNK_S)

    print(f"\nRunning analysis ({total_chunks} chunks)...")
    for i in range(total_chunks):
        start = i * chunk_size
        end = start + buf_size
        buf = audio_raw[start:min(end, len(audio_raw))]

        if len(buf) < chunk_size:
            break

        fake_ts = i * ANALYSIS_CHUNK_S
        analysis = analyzer.analyze(buf)
        analysis.timestamp = fake_ts

        recs = engine.evaluate(analysis, channels)
        for r in recs:
            r.timestamp = fake_ts
            r.timestamp_str = _fmt_time(fake_ts)
            all_recs.append((fake_ts, r))
            if not args.summary_only:
                print(f"  {r.timestamp_str}  [{r.channel_label or 'Overall'}]  {r.detail}")

    # Score against ground truth
    report = _build_report(all_recs, ground_truth, duration_s, args.audio, args.genre)
    print(report)


def _build_channels_from_scenario(board_path: str | None,
                                   channel_map: dict) -> dict[int, ChannelState]:
    initial: dict = {}
    if board_path:
        with open(board_path, "r", encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        initial = scenario.get("initial_state", {}).get("channels", {})

    now = time.time()
    channels: dict[int, ChannelState] = {}
    for num, cfg in channel_map.items():
        label = cfg.get("label", f"CH{num:02d}") if isinstance(cfg, dict) else str(cfg)
        ch_init = initial.get(num, initial.get(str(num), {}))
        fader_db = ch_init.get("fader_db", -3.0)

        eq_init = ch_init.get("eq", [])
        eq_map = {e.get("band", i+1): e for i, e in enumerate(eq_init)}
        eq_bands = []
        for b in range(1, 5):
            ei = eq_map.get(b, {})
            eq_bands.append(EQBand(
                band_num=b,
                type=2,
                freq_hz=float(ei.get("freq", 1000.0)),
                gain_db=float(ei.get("gain", 0.0)),
                q=1.0,
            ))

        channels[num] = ChannelState(
            channel_num=num,
            label=label,
            fader_db=fader_db,
            muted=ch_init.get("muted", False),
            eq=eq_bands,
            comp_on=False, comp_threshold_db=-20, comp_ratio_index=3,
            gate_on=False, gate_threshold_db=-40,
            rms_linear=0.3, rms_db=-10.0,
            timestamp=now,
            channel_type=cfg.get("type", "instrument") if isinstance(cfg, dict) else "instrument",
        )
    return channels


def _build_report(all_recs: list, ground_truth: list, duration_s: float,
                  audio_file: str, genre_id: str) -> str:
    lines = [
        "",
        SEP,
        "FOH ASSISTANT -- OFFLINE VALIDATION REPORT",
        f"Audio: {Path(audio_file).name} ({duration_s/60:.1f}m)",
        f"Genre: {genre_id}",
    ]

    if not ground_truth:
        lines += [
            f"Recommendations generated: {len(all_recs)}",
            "(No ground truth — accuracy analysis skipped)",
            SEP,
        ]
        return "\n".join(lines)

    lines.append(f"Ground truth: {len(ground_truth)} engineer adjustments")
    lines.append(SEP)

    # Match recs to ground truth: for each GT adjustment, find the closest
    # preceding recommendation on the same channel within the window
    from collections import defaultdict

    matched_recs: set[int] = set()
    matched_gt: set[int] = set()

    for gt_idx, gt in enumerate(ground_truth):
        gt_ts = float(gt.get("timestamp_s", 0))
        gt_ch = gt.get("channel", "").lower()
        for rec_idx, (rec_ts, rec) in enumerate(all_recs):
            if rec_idx in matched_recs:
                continue
            rec_ch = (rec.channel_label or "").lower()
            if gt_ch and rec_ch and gt_ch not in rec_ch and rec_ch not in gt_ch:
                continue
            if 0 <= (gt_ts - rec_ts) <= REC_MATCH_WINDOW_S:
                matched_recs.add(rec_idx)
                matched_gt.add(gt_idx)
                break

    true_positives = len(matched_recs)
    false_positives = len(all_recs) - true_positives
    false_negatives = len(ground_truth) - len(matched_gt)
    total = len(all_recs)
    accuracy = (true_positives / max(total, 1)) * 100

    lines += [
        "",
        "ENGINE PERFORMANCE",
        f"  Recommendations generated: {total:>4}",
        f"  True positives:            {true_positives:>4}  (matched ground truth)",
        f"  False positives:           {false_positives:>4}  (recommended, no GT match)",
        f"  Missed (false negatives):  {false_negatives:>4}  (GT adjustment, no recommendation)",
        f"  Accuracy:                  {accuracy:.0f}%",
    ]

    # Top false positives by channel
    fp_channels = [all_recs[i][1].channel_label for i in range(len(all_recs))
                   if i not in matched_recs]
    if fp_channels:
        from collections import Counter
        top_fp = Counter(fp_channels).most_common(3)
        lines += ["", "TOP FALSE POSITIVES (over-recommending)"]
        for lbl, count in top_fp:
            lines.append(f"  {lbl or 'Overall'}: flagged {count}x")

    # Missed adjustments
    missed_gt = [ground_truth[i] for i in range(len(ground_truth)) if i not in matched_gt]
    if missed_gt:
        lines += ["", "TOP MISSED ADJUSTMENTS (blind spots)"]
        for gt in missed_gt[:5]:
            ts = _fmt_time(float(gt.get("timestamp_s", 0)))
            lines.append(f"  {gt.get('channel','?')} {gt.get('parameter','?')} at {ts}"
                          f"  {gt.get('note','')}")

    # Threshold suggestions
    lines += ["", "THRESHOLD TUNING HINTS"]
    if fp_channels:
        from collections import Counter
        top_fp_ch = Counter(fp_channels).most_common(1)[0][0]
        lines.append(f"  {top_fp_ch}: consider raising recommendation_trigger_db or inactive_threshold_db")
    if missed_gt:
        missed_chs = list(set(g.get("channel","") for g in missed_gt[:3]))
        lines.append(f"  {', '.join(missed_chs)}: consider lowering recommendation_trigger_db for these channels")

    lines.append(SEP)
    return "\n".join(lines)


def _fmt_time(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


if __name__ == "__main__":
    main()
