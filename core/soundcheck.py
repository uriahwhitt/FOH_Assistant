"""Soundcheck advisory checks — HPF, gain staging, compressor sanity (IMP-021)."""

from typing import Optional

from models.channel import ChannelState

HPF_SUPPRESS_LABELS = {"Kick", "Bass"}

HPF_SUGGESTIONS = {
    "Guitar 1":       "80-100Hz",
    "Guitar 2":       "80-100Hz",
    "Acoustic Guitar": "80Hz",
    "Keys":           "80-120Hz",
    "Drum Rack":      "60-80Hz",
    "Floor Tom":      "60Hz",
}


def check_hpf(ch: ChannelState) -> Optional[str]:
    if ch.label in HPF_SUPPRESS_LABELS:
        return None
    if not ch.is_active():
        return None
    if not ch.hpf_on:
        freq_suggestion = HPF_SUGGESTIONS.get(ch.label, "80-120Hz")
        return (
            f"⚠ HPF OFF — {ch.label}: no high-pass filter engaged\n"
            f"  Suggest: Enable HPF @ {freq_suggestion}"
        )
    elif ch.hpf_slope == 0:
        return (
            f"⚠ HPF SLOPE — {ch.label}: HPF on @ {ch.hpf_freq_hz:.0f}Hz "
            f"but slope is 6dB/oct (too gentle for rumble removal)\n"
            f"  Suggest: Switch to 12dB/oct slope"
        )
    return None


def check_gain_staging(ch: ChannelState) -> Optional[str]:
    if not ch.is_active():
        return None
    if ch.rms_db < -30.0 and ch.fader_db > 5.0:
        return (
            f"⚠ GAIN STAGING — {ch.label}: weak signal ({ch.rms_db:.0f}dBFS), "
            f"fader pushed high ({ch.fader_db:+.1f}dB)\n"
            f"  Suggest: Increase input gain, reduce fader toward 0dB"
        )
    elif ch.fader_db > 5.0:
        return (
            f"ℹ FADER HIGH — {ch.label}: fader at {ch.fader_db:+.1f}dB "
            f"— monitor for headroom"
        )
    return None


def check_compressor_sanity(ch: ChannelState) -> Optional[str]:
    if not ch.comp_on:
        return None
    percussion_labels = {"Kick", "Drum Rack", "Floor Tom"}
    if ch.label not in percussion_labels and ch.comp_ratio_index >= 8:
        return (
            f"⚠ COMP RATIO — {ch.label}: ratio is very high "
            f"(index {ch.comp_ratio_index} = 7:1 or above)\n"
            f"  Suggest: Review — high ratios on non-percussion act as limiters, "
            f"not compressors"
        )
    return None
