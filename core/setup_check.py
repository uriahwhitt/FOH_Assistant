"""Initial setup mode — pre-soundcheck audit of X32 state against basic
audio-theory baselines from docs/FOH_Assistant_System_Audio_Guide.md.

One-shot: takes a board snapshot, returns a prioritized list of findings.
No audio capture, no rolling loop.
"""

from dataclasses import dataclass
from typing import Optional

from models.channel import ChannelState


# Severity tiers for findings ordering and printout grouping
CRITICAL    = "critical"
HIGH_VALUE  = "high_value"
ADVISORY    = "advisory"


# Per-instrument HPF expectations — derived from System Audio Guide §3.1, §7.x.
# Each entry: (recommended_low_hz, recommended_high_hz, hpf_required).
# hpf_required=False means HPF should be OFF or below the floor frequency.
HPF_TARGETS: dict[str, tuple[float, float, bool]] = {
    "Kick":            (20.0,   40.0,  False),  # off or 30 Hz floor
    "Bass":            (20.0,   40.0,  False),  # below low E (41 Hz)
    "Floor Tom":       (50.0,   70.0,  True),
    "Drum Rack":       (60.0,   90.0,  True),
    "Acoustic Guitar": (75.0,   95.0,  True),
    "Guitar 1":        (80.0,  120.0,  True),
    "Guitar 2":        (80.0,  120.0,  True),
    "Keys":            (80.0,  120.0,  True),
    "Lead Vocal":      (80.0,  120.0,  True),
    "Drum Vocal":      (80.0,  120.0,  True),
    "Bassist Vocal":   (80.0,  120.0,  True),
    "Keys Vocal":      (80.0,  120.0,  True),
}

# Channels where HPF SHOULD be off — flag if engaged at a damaging frequency.
HPF_OFF_CHANNELS = {"Kick", "Bass"}

# EQ boost thresholds before any audio is added — feedback / headroom risk.
EQ_BOOST_VOCAL_DB    = 5.0   # vocal channels: tighter — feedback path is open
EQ_BOOST_INSTR_DB    = 6.0   # instruments: a little more headroom
FADER_HIGH_DB        = 5.0   # >+5 dB pre-soundcheck = gain-staging inversion
MASTER_OFF_UNITY_DB  = 3.0   # master should sit near unity at startup


@dataclass
class SetupFinding:
    severity: str
    channel: str        # "" for board-level findings (master, etc.)
    issue: str
    action: str
    why: str = ""


# ---------------------------------------------------------------------------
# Per-channel checks
# ---------------------------------------------------------------------------

def _check_mute(ch: ChannelState) -> Optional[SetupFinding]:
    """Backup-vocalist mics (inactive_threshold_db set) and paired channels are
    allowed to be muted before soundcheck. Anything else muted is likely an
    oversight."""
    if not ch.muted:
        return None
    if ch.inactive_threshold_db is not None or ch.paired_channel is not None:
        return None
    return SetupFinding(
        severity=HIGH_VALUE,
        channel=ch.label,
        issue=f"channel MUTED (ch {ch.channel_num})",
        action=f"unmute ch {ch.channel_num} before soundcheck",
        why="non-backup, non-paired channel — needed for soundcheck",
    )


def _check_hpf(ch: ChannelState) -> Optional[SetupFinding]:
    target = HPF_TARGETS.get(ch.label)
    if target is None:
        return None
    low, high, required = target

    # Bass / Kick — flag if HPF is on AND set above the floor (would cut fundamental)
    if ch.label in HPF_OFF_CHANNELS:
        if ch.hpf_on and ch.hpf_freq_hz > high:
            return SetupFinding(
                severity=HIGH_VALUE,
                channel=ch.label,
                issue=f"HPF engaged @ {ch.hpf_freq_hz:.0f}Hz — cuts fundamental",
                action=f"disable HPF, or lower to {low:.0f}-{high:.0f}Hz",
                why=f"{ch.label} fundamental is below 80Hz — HPF here removes weight",
            )
        return None

    # Everyone else — HPF should be on, in the recommended range
    if not ch.hpf_on:
        sev = HIGH_VALUE if ch.channel_type == "vocal" else HIGH_VALUE
        return SetupFinding(
            severity=sev,
            channel=ch.label,
            issue="HPF disabled",
            action=f"engage HPF @ {low:.0f}-{high:.0f}Hz, slope 12dB/oct",
            why="removes rumble, reclaims headroom, reduces feedback risk",
        )

    if ch.hpf_freq_hz < low * 0.6:
        return SetupFinding(
            severity=ADVISORY,
            channel=ch.label,
            issue=f"HPF @ {ch.hpf_freq_hz:.0f}Hz — lower than typical for {ch.label}",
            action=f"raise HPF to {low:.0f}-{high:.0f}Hz",
            why="leaves rumble in the channel; tighter HPF gives more headroom",
        )

    if ch.hpf_freq_hz > high * 1.6:
        return SetupFinding(
            severity=ADVISORY,
            channel=ch.label,
            issue=f"HPF @ {ch.hpf_freq_hz:.0f}Hz — high for {ch.label}",
            action=f"lower HPF toward {low:.0f}-{high:.0f}Hz",
            why="overly aggressive HPF thins the source",
        )

    if ch.hpf_slope == 0:  # 6 dB/oct
        return SetupFinding(
            severity=ADVISORY,
            channel=ch.label,
            issue=f"HPF slope = 6dB/oct (too gentle for rumble)",
            action="switch HPF slope to 12dB/oct or steeper",
            why="6dB/oct lets too much sub-100Hz energy through",
        )

    return None


def _check_eq_boosts(ch: ChannelState) -> list[SetupFinding]:
    """Flag pre-soundcheck EQ boosts — every dB of boost is a dB of feedback risk."""
    findings: list[SetupFinding] = []
    threshold = EQ_BOOST_VOCAL_DB if ch.channel_type == "vocal" else EQ_BOOST_INSTR_DB

    for band in ch.eq:
        if band.gain_db <= threshold:
            continue
        # Type 0 = LCut, 5 = HCut — those don't have meaningful "gain"
        if band.type in (0, 5):
            continue
        sev = CRITICAL if ch.channel_type == "vocal" else HIGH_VALUE
        findings.append(SetupFinding(
            severity=sev,
            channel=ch.label,
            issue=(f"EQ Band {band.band_num} boost {band.gain_db:+.1f}dB "
                   f"@ {band.freq_hz:.0f}Hz"),
            action=f"reduce Band {band.band_num} to ≤+3dB before opening mics",
            why="every dB of EQ boost is a dB lost to feedback margin",
        ))
    return findings


def _check_fader_headroom(ch: ChannelState) -> Optional[SetupFinding]:
    """Pre-soundcheck fader pinned high almost always means weak preamp gain."""
    if ch.muted:
        return None
    if ch.fader_db > FADER_HIGH_DB:
        return SetupFinding(
            severity=HIGH_VALUE,
            channel=ch.label,
            issue=f"fader at {ch.fader_db:+.1f}dB pre-soundcheck",
            action="bring fader to unity (0dB), then set preamp gain at the source",
            why="high fader pre-soundcheck signals a gain-staging inversion",
        )
    return None


def _check_compressor(ch: ChannelState) -> Optional[SetupFinding]:
    """Catch obviously wrong compressor ratios on non-percussion channels."""
    if not ch.comp_on:
        return None
    percussion = {"Kick", "Drum Rack", "Floor Tom"}
    if ch.label not in percussion and ch.comp_ratio_index >= 8:
        return SetupFinding(
            severity=ADVISORY,
            channel=ch.label,
            issue=(f"compressor ratio index {ch.comp_ratio_index} "
                   f"(7:1 or above) on non-percussion source"),
            action="lower ratio to 3:1-5:1 (index 5-7) for normal compression",
            why="ratios above 7:1 act as limiters, not compressors",
        )
    return None


# ---------------------------------------------------------------------------
# Board-level checks
# ---------------------------------------------------------------------------

def _check_master(master_db: float) -> Optional[SetupFinding]:
    if abs(master_db) <= MASTER_OFF_UNITY_DB:
        return None
    return SetupFinding(
        severity=ADVISORY,
        channel="Main LR",
        issue=f"master fader at {master_db:+.1f}dB",
        action="bring master to unity (0dB) before setting channel gains",
        why="unity at master gives faders their reference scale",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_setup_check(channels: dict[int, ChannelState],
                    master_db: float) -> list[SetupFinding]:
    """Run all setup checks against the snapshot. Order: critical, then high
    value, then advisory; within tier, by channel number for stable display."""
    findings: list[SetupFinding] = []

    master_finding = _check_master(master_db)
    if master_finding:
        findings.append(master_finding)

    # Iterate channels in numeric order so output mirrors the patch sheet
    for num in sorted(channels):
        ch = channels[num]

        for check in (_check_mute, _check_hpf, _check_fader_headroom, _check_compressor):
            f = check(ch)
            if f:
                findings.append(f)

        findings.extend(_check_eq_boosts(ch))

    severity_rank = {CRITICAL: 0, HIGH_VALUE: 1, ADVISORY: 2}
    findings.sort(key=lambda f: severity_rank.get(f.severity, 99))
    return findings
