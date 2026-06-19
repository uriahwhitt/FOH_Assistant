# FOH Assistant — Mic Analyzer Diagnostic
# Run this standalone to snapshot raw mic data at each pipeline stage.
# Play music through speakers while this runs.
# Output shows where the jaggedness is introduced.
#
# Usage:
#   python tools/mic_diagnostic.py
#
# Output:
#   mic_diagnostic_<timestamp>.json — raw data at each stage
#   Printed summary showing variance at each stage

import json
import time
import numpy as np
from pathlib import Path
from datetime import datetime

def run_diagnostic():
    print("=" * 60)
    print("MIC ANALYZER DIAGNOSTIC")
    print("Play music through your speakers during this test.")
    print("=" * 60)
    print()

    # ── Step 1: Import and set up ──────────────────────────────────────
    from core.audio_capture import AudioCapture
    from core.mic_analyzer import (
        compute_welch_spectrum,
        interpolate_to_freq_axis,
        normalize_to_shape,
        FREQ_AXIS,
        N_FREQS,
    )
    from scipy.signal import welch as scipy_welch
    import scipy.signal

    # ── Step 2: Start audio capture ────────────────────────────────────
    print("Starting audio capture (PreSonus Studio 26c)...")
    capture = AudioCapture(
        device_name_match='Studio 26c',
        preferred_sample_rate=44100,
    )
    capture.start()
    print(f"  Device: {capture.device_name}")
    print(f"  Sample rate: {capture.sample_rate}Hz")
    print(f"  Buffer: {capture._buffer_seconds}s "
          f"({int(capture._buffer_seconds * capture.sample_rate)} samples)")
    print()

    print("Waiting 2 seconds for buffer to fill...")
    time.sleep(2.0)
    print()

    results = {}

    # ── Stage 1: Raw audio buffer ──────────────────────────────────────
    print("STAGE 1: Raw audio buffer")
    audio = capture.get_analysis_window()
    peak_db = 20 * np.log10(max(float(np.max(np.abs(audio))), 1e-10))
    rms_db  = 20 * np.log10(max(float(np.sqrt(np.mean(audio**2))), 1e-10))
    print(f"  Buffer length:  {len(audio)} samples "
          f"({len(audio)/capture.sample_rate*1000:.0f}ms)")
    print(f"  Peak level:     {peak_db:.1f}dBFS")
    print(f"  RMS level:      {rms_db:.1f}dBFS")
    if peak_db < -40:
        print("  WARNING: Very low signal — turn up music volume")
    results['stage1_raw'] = {
        'buffer_samples': len(audio),
        'peak_dbfs': peak_db,
        'rms_dbfs': rms_db,
    }
    print()

    # ── Stage 2: Welch spectrum (analysis path) ────────────────────────
    print("STAGE 2: Welch spectrum (500ms analysis path)")
    freqs_welch, psd_welch = compute_welch_spectrum(audio, capture.sample_rate)
    print(f"  Output bins:    {len(freqs_welch)}")
    print(f"  Freq resolution: {freqs_welch[1] - freqs_welch[0]:.1f}Hz per bin")
    print(f"  Range:          {freqs_welch[0]:.0f}Hz – {freqs_welch[-1]:.0f}Hz")
    print(f"  Level range:    {psd_welch.min():.1f}dB to {psd_welch.max():.1f}dB")
    
    # Check adjacent bin variance in key frequency regions
    for label, lo, hi in [
        ("20–80Hz (sub)",     20, 80),
        ("80–250Hz (bass)",   80, 250),
        ("250–1kHz (lo-mid)", 250, 1000),
        ("1–4kHz (mid)",      1000, 4000),
        ("4–12kHz (hi-mid)",  4000, 12000),
    ]:
        mask = (freqs_welch >= lo) & (freqs_welch <= hi)
        if mask.sum() > 2:
            region = psd_welch[mask]
            diffs  = np.abs(np.diff(region))
            print(f"  {label:25s} bins:{mask.sum():3d}  "
                  f"mean step:{diffs.mean():.1f}dB  max step:{diffs.max():.1f}dB")

    results['stage2_welch'] = {
        'n_bins': int(len(freqs_welch)),
        'freq_resolution_hz': float(freqs_welch[1] - freqs_welch[0]),
        'level_min': float(psd_welch.min()),
        'level_max': float(psd_welch.max()),
        'freqs_sample': freqs_welch[:20].tolist(),
        'psd_sample': psd_welch[:20].tolist(),
    }
    print()

    # ── Stage 3: Single-window FFT (display path) ──────────────────────
    print("STAGE 3: Single-window FFT (100ms display path)")
    display_samples = int(0.1 * capture.sample_rate)
    display_audio   = capture.get_display_window(display_samples)
    n = len(display_audio)
    windowed = display_audio * np.hanning(n)
    spectrum = np.fft.rfft(windowed)
    freqs_single = np.fft.rfftfreq(n, d=1.0/capture.sample_rate)
    psd_single   = 20.0 * np.log10(np.maximum(np.abs(spectrum) / n, 1e-12))

    print(f"  Window size:    {n} samples ({n/capture.sample_rate*1000:.0f}ms)")
    print(f"  Output bins:    {len(freqs_single)}")
    print(f"  Freq resolution: {freqs_single[1] - freqs_single[0]:.1f}Hz per bin")

    for label, lo, hi in [
        ("20–80Hz (sub)",     20, 80),
        ("80–250Hz (bass)",   80, 250),
        ("1–4kHz (mid)",      1000, 4000),
        ("4–12kHz (hi-mid)",  4000, 12000),
    ]:
        mask = (freqs_single >= lo) & (freqs_single <= hi)
        if mask.sum() > 2:
            region = psd_single[mask]
            diffs  = np.abs(np.diff(region))
            print(f"  {label:25s} bins:{mask.sum():3d}  "
                  f"mean step:{diffs.mean():.1f}dB  max step:{diffs.max():.1f}dB")

    results['stage3_single_fft'] = {
        'window_samples': n,
        'freq_resolution_hz': float(freqs_single[1] - freqs_single[0]),
    }
    print()

    # ── Stage 4: Interpolation to FREQ_AXIS ───────────────────────────
    print("STAGE 4: Interpolation to FREQ_AXIS (1000 log-spaced points)")
    
    # Welch interpolated
    welch_on_axis = interpolate_to_freq_axis(freqs_welch, psd_welch)
    diffs_welch   = np.abs(np.diff(welch_on_axis))
    print(f"  Welch→FREQ_AXIS:   mean step:{diffs_welch.mean():.2f}dB  "
          f"max step:{diffs_welch.max():.2f}dB  "
          f"range:{welch_on_axis.max()-welch_on_axis.min():.1f}dB")

    # Single window interpolated
    valid = (freqs_single >= FREQ_AXIS[0]) & (freqs_single <= FREQ_AXIS[-1])
    if valid.sum() > 2:
        single_on_axis = np.interp(
            np.log10(FREQ_AXIS),
            np.log10(freqs_single[valid]),
            psd_single[valid],
        )
        diffs_single = np.abs(np.diff(single_on_axis))
        print(f"  Single→FREQ_AXIS:  mean step:{diffs_single.mean():.2f}dB  "
              f"max step:{diffs_single.max():.2f}dB  "
              f"range:{single_on_axis.max()-single_on_axis.min():.1f}dB")
    else:
        single_on_axis = np.full(N_FREQS, -60.0)
        print("  Single→FREQ_AXIS:  insufficient valid bins")

    results['stage4_interpolated'] = {
        'welch_mean_step': float(diffs_welch.mean()),
        'welch_max_step':  float(diffs_welch.max()),
        'welch_range_db':  float(welch_on_axis.max() - welch_on_axis.min()),
        'welch_full': welch_on_axis.tolist(),
        'single_full': single_on_axis.tolist(),
        'freq_axis': FREQ_AXIS.tolist(),
    }
    print()

    # ── Stage 5: After normalize_to_shape ─────────────────────────────
    print("STAGE 5: After normalize_to_shape()")
    welch_norm  = normalize_to_shape(welch_on_axis)
    single_norm = normalize_to_shape(single_on_axis)
    
    diffs_welch_norm  = np.abs(np.diff(welch_norm))
    diffs_single_norm = np.abs(np.diff(single_norm))

    print(f"  Welch normalized:  mean step:{diffs_welch_norm.mean():.2f}dB  "
          f"max step:{diffs_welch_norm.max():.2f}dB  "
          f"display range:{welch_norm.max()-welch_norm.min():.1f}dB")
    print(f"  Single normalized: mean step:{diffs_single_norm.mean():.2f}dB  "
          f"max step:{diffs_single_norm.max():.2f}dB  "
          f"display range:{single_norm.max()-single_norm.min():.1f}dB")

    results['stage5_normalized'] = {
        'welch_mean_step':  float(diffs_welch_norm.mean()),
        'welch_max_step':   float(diffs_welch_norm.max()),
        'welch_range_db':   float(welch_norm.max() - welch_norm.min()),
        'single_mean_step': float(diffs_single_norm.mean()),
        'single_max_step':  float(diffs_single_norm.max()),
        'single_range_db':  float(single_norm.max() - single_norm.min()),
    }
    print()

    # ── Summary ────────────────────────────────────────────────────────
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    welch_step  = results['stage5_normalized']['welch_mean_step']
    single_step = results['stage5_normalized']['single_mean_step']
    welch_range  = results['stage5_normalized']['welch_range_db']
    single_range = results['stage5_normalized']['single_range_db']
    
    print(f"  Welch (analysis path):")
    print(f"    Mean adjacent bin step: {welch_step:.2f}dB")
    print(f"    Total curve range:      {welch_range:.1f}dB")
    
    print(f"  Single window (display path):")
    print(f"    Mean adjacent bin step: {single_step:.2f}dB")
    print(f"    Total curve range:      {single_range:.1f}dB")
    
    print()
    if single_step > welch_step * 3:
        print("  FINDING: Display path is significantly noisier than analysis path.")
        print("  The jagged display curve is caused by the 100ms single-window FFT.")
        print("  FIX: Use Welch spectrum for display path too (same data, slower update).")
    elif welch_step > 2.0:
        print("  FINDING: Even the Welch path has high adjacent-bin variance.")
        print("  The problem is in the Welch computation or interpolation step.")
        print("  FIX: Increase Welch window segments or smooth after interpolation.")
    else:
        print("  FINDING: Analysis path looks healthy. Display path may need smoothing.")

    # ── Save JSON ──────────────────────────────────────────────────────
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = Path(f'shows/mic_diagnostic_{ts}.json')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2)
    print()
    print(f"  Full data saved: {out_path}")

    capture.stop()
    print("Done.")


if __name__ == '__main__':
    run_diagnostic()
