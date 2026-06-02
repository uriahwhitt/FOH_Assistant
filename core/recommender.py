"""Recommendation engine — compares room analysis and board state against the active genre profile."""

import time
from dataclasses import dataclass, field
from typing import Optional

from models.channel import ChannelState
from models.event import RoomAnalysis
from models.genre_profile import GenreProfile

BAND_NAMES = ("sub_bass", "bass", "low_mid", "mid", "high_mid", "presence", "air")

# Frequency band ranges in Hz — used to correlate room bands to channel fingerprints
BAND_RANGES = {
    "sub_bass":  (20,    80),
    "bass":      (80,    250),
    "low_mid":   (250,   500),
    "mid":       (500,   2000),
    "high_mid":  (2000,  6000),
    "presence":  (6000,  12000),
    "air":       (12000, 20000),
}

# IMP-023a — named moves for common EQ corrections
NAMED_MOVES = [
    (20,    120,   "cut",   "HPF"),
    (60,     80,   "boost", "Punch boost"),
    (80,    150,   "boost", "Body boost"),
    (200,   400,   "cut",   "Mud cut"),
    (400,   800,   "cut",   "Boxiness cut"),
    (800,  1500,   "cut",   "Honk cut"),
    (2000, 5000,   "boost", "Presence boost"),
    (3000, 5000,   "cut",   "Harshness cut"),
    (5000, 9000,   "cut",   "Sibilance cut"),
    (10000,20000,  "boost", "Air boost"),
]


_HPF_SUPPRESS_LABELS = {"Bass", "Kick"}


def _named_move(freq_hz: float, direction: str, channel_label: str = "") -> str:
    for lo, hi, move_dir, name in NAMED_MOVES:
        if lo <= freq_hz <= hi and move_dir == direction:
            if name == "HPF" and channel_label in _HPF_SUPPRESS_LABELS:
                return ""
            return name
    return ""


# IMP-023c — perceptual weighting by band
BAND_PERCEPTUAL_WEIGHTS = {
    "sub_bass":  0.6,
    "bass":      0.8,
    "low_mid":   1.0,
    "mid":       1.1,
    "high_mid":  1.3,
    "presence":  1.2,
    "air":       0.9,
}


def _build_band_recommendation_text(band: str,
                                     direction: str,
                                     deviation_db: float,
                                     dominant_channel_label: str,
                                     mic_band_levels: dict = None,
                                     mic_spectrum=None) -> str:
    """Build recommendation text with specific peak frequency from mic analysis.

    Returns a combined two-line string:
      Line 1: named move + band + deviation + peak description
      Line 2:   → channel: EQ action at peak_hz, Q advice
    Falls back to band center when mic data is unavailable or prominence is low.
    """
    # Merge recommender and forward-model band ranges so any band name works
    from core.forward_model import BAND_RANGES as _FM_RANGES
    _all_ranges = {**BAND_RANGES, **_FM_RANGES}
    f_lo, f_hi  = _all_ranges.get(band, (200, 2000))
    band_center = int((f_lo + f_hi) / 2)

    peak_hz    = float(band_center)
    prominence = 0.0

    if mic_spectrum is not None:
        from core.mic_analyzer import find_band_peak, FREQ_AXIS
        peak_hz, prominence = find_band_peak(mic_spectrum, FREQ_AXIS, f_lo, f_hi)
    elif mic_band_levels and band in mic_band_levels:
        lvl        = mic_band_levels[band]
        peak_hz    = float(lvl.get('peak_hz', band_center))
        prominence = float(lvl.get('peak_prominence_db', 0.0))

    move_dir = 'cut' if direction == 'buildup' else 'boost'
    named    = _named_move(peak_hz, move_dir, dominant_channel_label or "")
    prefix   = f"{named} — " if named else ""

    if prominence > 0.5:
        peak_str = f"peak at {peak_hz:.0f}Hz"
        if prominence > 2.0:
            peak_str += f" (+{prominence:.1f}dB above band mean — sharp resonance)"
    else:
        peak_str = f"broad energy in {f_lo}–{f_hi}Hz"

    action_hz  = f"{peak_hz:.0f}Hz" if prominence > 0.5 else f"~{band_center}Hz"
    action_q   = "Q≈2.0" if move_dir == 'cut' else "Q≈1.0"
    ch_str     = f"{dominant_channel_label}: " if dominant_channel_label else ""

    return (
        f"{prefix}{band} {deviation_db:+.1f}dB · {peak_str}\n"
        f"  → {ch_str}EQ {move_dir} at {action_hz}, {action_q}"
    )


def _band_covers_problem(eq_band, lo: int, hi: int) -> bool:
    return lo <= eq_band.freq_hz <= hi


def _q_advice(eq_band, direction: str) -> str:
    q = eq_band.q
    if direction == "cut":
        if q < 1.0:
            return (f"Q={q:.1f} is very broad for a cut "
                    f"— consider Q≈2.0 for focused correction")
        if q > 8.0:
            return (f"Q={q:.1f} is notch-narrow "
                    f"— use Q≈2.0 for mix EQ cuts (reserve Q>8 for feedback notches)")
    elif direction == "boost":
        if q > 2.0:
            return (f"Q={q:.1f} is narrow for a boost "
                    f"— narrow boosts sound unnatural; widen to Q≈1.0")
    return ""


@dataclass
class Recommendation:
    channel_num: Optional[int]
    channel_label: Optional[str]
    issue: str              # e.g. "low_mid_buildup" | "lufs_hot" | "baseline_drift"
    detail: str
    current_state: dict
    suggestion: str
    genre_id: str
    timestamp: float = field(default_factory=time.time)
    timestamp_str: str = ""

    def format_terminal(self) -> str:
        ts = self.timestamp_str or time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        label = self.channel_label or "Overall"
        lines = [f"[{ts}] {label} -- {self.detail}"]
        if self.current_state:
            state_parts = [f"{k}: {v}" for k, v in self.current_state.items()]
            lines.append("  Current:  " + " | ".join(state_parts))
        lines.append(f"  Genre:    {self.genre_id}")
        lines.append(f"  Suggest:  {self.suggestion}")
        return "\n".join(lines)


class RecommendationEngine:
    def __init__(self, band_config: dict, genre_profile: GenreProfile):
        self._cfg = band_config
        self._genre = genre_profile
        self._fingerprints: dict[str, dict] = band_config.get("frequency_fingerprints", {})
        self._thresholds = band_config.get("thresholds", {})

        # Per-channel suppression state: {ch_num: expiry_timestamp}
        self._suppressed_until: dict[int, float] = {}
        # Which trigger caused suppression: {ch_num: "rate_of_change_fader"|"rate_of_change_rms"}
        self._suppression_trigger: dict[int, str] = {}

        # Per-channel last recommendation timestamp
        self._last_rec: dict[int, float] = {}

        # Silence gate state — True when room audio is below threshold.
        # Set by _check_lufs(); gates band analysis in evaluate().
        self._in_silence: bool = False

        # Per-channel fader reference for rate-of-change detection (sliding window).
        # Reference only advances when the window expires OR suppression fires —
        # NOT on every poll.  This lets the window accumulate small increments that
        # add up to a large move (e.g. 1 dB/s over 4 s = 4 dB detected correctly).
        self._last_fader: dict[int, float] = {}
        self._last_fader_time: dict[int, float] = {}

        # Per-channel RMS reference for boost-pedal detection (same sliding logic).
        self._last_rms: dict[int, float] = {}
        self._last_rms_time: dict[int, float] = {}

        # Stability guard: suppress repeat-fire of the same static issue.
        # Keyed on (ch_num, issue_string) e.g. (7, "mid_buildup").
        self._consecutive_fires: dict[tuple[int, str], int] = {}
        self._issue_cooldown: dict[tuple[int, str], float] = {}
        # Per-issue last recommendation timestamp (separate from _last_rec to avoid
        # cross-issue timestamp contamination on the same channel).
        self._last_issue_rec: dict[tuple[int, str], float] = {}
        # Issues active in the previous _check_bands cycle — used to detect resolution.
        self._prev_active_issues: set[tuple[int, str]] = set()

        self._baseline: Optional[dict[int, ChannelState]] = None

        # Transition grace period: suppresses LUFS and band recs during
        # song changes while the engineer resets monitor mixes and tuning.
        # Baseline drift still fires — those board changes are worth logging.
        self._in_transition: bool = False
        self._transition_end: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_genre(self, profile: GenreProfile) -> None:
        self._genre = profile
        # Reset stability guard — genre change invalidates prior stability history
        self._consecutive_fires.clear()
        self._issue_cooldown.clear()
        self._last_issue_rec.clear()
        self._prev_active_issues.clear()

    def set_baseline(self, snapshot: dict[int, ChannelState]) -> None:
        self._baseline = snapshot

    def set_transition(self, active: bool) -> None:
        """Enter or exit a song-change transition grace period.

        While active, LUFS and frequency-band recommendations are suppressed so
        the engineer can reset monitor mixes and tuning without false alerts.
        Baseline drift alerts still fire — large fader moves during transition
        are worth logging for the post-show report.

        The grace period expires automatically after transition_grace_seconds
        (default 30 s) OR when set_transition(False) is called explicitly (e.g.
        when n is pressed to start the next song).
        """
        self._in_transition = active
        if active:
            grace = self._thresholds.get("transition_grace_seconds", 30.0)
            self._transition_end = time.time() + grace
        else:
            self._transition_end = 0.0

    def notify_adjustment(self, ch_num: int) -> None:
        """Reset stability guard for every issue on ch_num.

        Call this when the engineer makes any board adjustment to ch_num so the
        stability guard doesn't suppress legitimate follow-up recommendations.
        """
        keys = [k for k in self._consecutive_fires if k[0] == ch_num]
        for key in keys:
            del self._consecutive_fires[key]
            self._issue_cooldown.pop(key, None)
        keys_to_clear = [k for k in self._last_issue_rec if k[0] == ch_num]
        for k in keys_to_clear:
            del self._last_issue_rec[k]

    def evaluate(self, analysis: RoomAnalysis,
                 channels: dict[int, ChannelState],
                 mic_analysis=None) -> list[Recommendation]:
        """Run one recommendation cycle. Returns list of new recommendations."""
        # Auto-expire transition grace period using wall time
        if self._in_transition and time.time() >= self._transition_end:
            self._in_transition = False

        now = analysis.timestamp
        ts_str = time.strftime("%H:%M:%S", time.localtime(now))
        recs: list[Recommendation] = []

        self._update_suppression(channels, now)

        # Silence gate: update _in_silence; no recommendations from LUFS itself
        self._check_lufs(analysis)

        if not self._in_silence:
            # Per-band frequency deviation (requires active audio)
            band_recs = self._check_bands(analysis, channels, ts_str,
                                          mic_analysis=mic_analysis)
            recs.extend(band_recs)

        # Baseline drift fires even in silence — fader moves are always noteworthy
        if self._baseline:
            drift_recs = self._check_baseline_drift(channels, ts_str)
            recs.extend(drift_recs)

        return recs

    # ------------------------------------------------------------------
    # Internal — checks
    # ------------------------------------------------------------------

    def _check_lufs(self, analysis: RoomAnalysis) -> None:
        """Silence gate only — sets _in_silence flag.

        LUFS is mic-position-dependent and cannot reliably indicate overall
        level (a closer mic always reads higher regardless of mix quality).
        No recommendations are generated from LUFS. LUFS is logged separately
        for post-show analysis via logger.record_lufs().
        """
        self._in_silence = analysis.rms_db < -50.0

    def _check_bands(self, analysis: RoomAnalysis,
                     channels: dict[int, ChannelState],
                     ts_str: str,
                     mic_analysis=None) -> list[Recommendation]:
        if self._in_transition:
            # Clear active issues so we don't falsely reset stability when
            # transition ends — those issues were paused, not resolved.
            self._prev_active_issues = set()
            return []
        threshold = self._thresholds.get("recommendation_trigger_db", 3.0)
        recs = []
        now = analysis.timestamp
        current_active: set[tuple[int, str]] = set()

        # Use mic normalized_shape_db when available — mean-subtracted, position-independent.
        # Falls back to internal median normalization of RoomAnalysis bands.
        if mic_analysis is not None and not mic_analysis.is_silent:
            from core.mic_analyzer import band_average
            normalized_band_levels = {
                band: band_average(mic_analysis.normalized_shape_db,
                                   BAND_RANGES[band])
                for band in BAND_NAMES
            }
            def get_normalized(band: str) -> tuple[float, bool]:
                val = normalized_band_levels[band]
                return val, val > -85.0
        else:
            band_vals = [analysis.bands[b] for b in BAND_NAMES if analysis.bands[b] > -85]
            if not band_vals:
                self._prev_active_issues = set()
                return []
            median_level = sorted(band_vals)[len(band_vals) // 2]
            def get_normalized(band: str) -> tuple[float, bool]:
                raw = analysis.bands[band]
                return raw - median_level, raw > -85.0

        for band in BAND_NAMES:
            normalized, valid = get_normalized(band)
            if not valid:
                continue
            target_offset = self._genre.target_for_band(band)
            raw_deviation = normalized - target_offset
            weight = BAND_PERCEPTUAL_WEIGHTS.get(band, 1.0)
            weighted_deviation = raw_deviation * weight
            if abs(weighted_deviation) <= threshold:
                continue
            deviation = raw_deviation  # report raw in output

            culprit = self._find_culprit(band, channels, deviation)
            if culprit is None:
                continue

            direction = "buildup" if deviation > 0 else "deficiency"
            issue = f"{band}_{direction}"
            current_active.add((culprit.channel_num, issue))

            if self._is_suppressed(culprit.channel_num, now):
                continue
            if not self._issue_cooldown_ok(culprit.channel_num, issue, now):
                continue

            lo, hi = BAND_RANGES[band]
            freq_label = f"{lo}-{hi}Hz"
            eq_detail, eq_suggest = self._eq_recommendation(culprit, band, deviation)

            # Build detail with peak-frequency precision when mic analysis available
            if mic_analysis is not None and not mic_analysis.is_silent:
                combined = _build_band_recommendation_text(
                    band=band, direction=direction, deviation_db=deviation,
                    dominant_channel_label=culprit.label,
                    mic_spectrum=mic_analysis.normalized_shape_db,
                )
                if '\n  → ' in combined:
                    detail_text, fallback_suggest = combined.split('\n  → ', 1)
                else:
                    detail_text      = combined
                    fallback_suggest = f"Reduce {culprit.label} contribution in {freq_label}"
            else:
                detail_text      = f"{band.replace('_', '-')} {direction} around {freq_label} detected"
                fallback_suggest = f"Reduce {culprit.label} contribution in {freq_label}"

            # current_state is built from the culprit object returned this cycle —
            # no possibility of using a stale value from a previous iteration.
            current = {"rms":   f"{culprit.rms_db:.1f}dBFS",
                       "fader": f"{culprit.fader_db:+.1f}dB"}
            if eq_detail:
                current["eq"] = eq_detail

            self._last_rec[culprit.channel_num] = now
            self._last_issue_rec[(culprit.channel_num, issue)] = now
            self._update_stability(culprit.channel_num, issue)
            recs.append(Recommendation(
                channel_num=culprit.channel_num,
                channel_label=culprit.label,
                issue=issue,
                detail=detail_text,
                current_state=current,
                suggestion=eq_suggest or fallback_suggest,
                genre_id=self._genre.id,
                timestamp=analysis.timestamp,
                timestamp_str=ts_str,
            ))

        # Reset stability for issues that resolved this cycle
        for key in self._prev_active_issues - current_active:
            self._consecutive_fires.pop(key, None)
            self._issue_cooldown.pop(key, None)
        self._prev_active_issues = current_active

        return recs

    def _check_baseline_drift(self, channels: dict[int, ChannelState],
                               ts_str: str) -> list[Recommendation]:
        threshold = self._thresholds.get("baseline_drift_trigger_db", 2.0)
        now = time.time()
        recs = []

        for ch_num, current in channels.items():
            baseline = self._baseline.get(ch_num)
            if baseline is None or not current.is_active():
                continue
            if self._is_suppressed(ch_num, now):
                continue
            if not self._cooldown_ok(ch_num, now):
                continue

            drift = current.fader_db - baseline.fader_db
            if abs(drift) <= threshold:
                continue

            direction = "up" if drift > 0 else "down"
            self._last_rec[ch_num] = now
            recs.append(Recommendation(
                channel_num=ch_num,
                channel_label=current.label,
                issue="baseline_drift",
                detail=(f"Fader {abs(drift):.1f}dB {direction} from soundcheck baseline"),
                current_state={
                    "fader": f"{current.fader_db:+.1f}dB",
                    "soundcheck": f"{baseline.fader_db:+.1f}dB",
                },
                suggestion=(f"Return fader to {baseline.fader_db:+.1f}dB "
                             f"or re-evaluate if intentional"),
                genre_id=self._genre.id,
                timestamp=now,
                timestamp_str=ts_str,
            ))

        return recs

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    def _update_suppression(self, channels: dict[int, ChannelState],
                             now: float) -> None:
        """Detect fast fader moves and RMS spikes; suppress affected channels.

        Uses a sliding window for both triggers: the reference point only advances
        when the detection window expires or suppression fires.  Without this, a
        move of 1 dB/s reaching 4 dB in 4 s would never trigger because each
        consecutive-cycle comparison sees only 1 dB of change.

        Trigger A — Fader: fader moves > rate_of_change_suppress_db within
                    rate_of_change_window_s seconds.
        Trigger B — RMS:   RMS rises > rate_of_change_rms_db within
                    rate_of_change_rms_window_s seconds (boost-pedal activation).
        """
        suppress_fader_db = self._thresholds.get("rate_of_change_suppress_db", 3.0)
        window_fader_s    = self._thresholds.get("rate_of_change_window_s", 5.0)
        suppress_rms_db   = self._thresholds.get("rate_of_change_rms_db", 4.0)
        window_rms_s      = self._thresholds.get("rate_of_change_rms_window_s", 2.0)
        duration_s        = self._thresholds.get("suppression_duration_s", 60.0)

        for ch_num, ch in channels.items():
            # ── Fader rate-of-change ──────────────────────────────────────────
            prev_fader      = self._last_fader.get(ch_num)
            prev_fader_time = self._last_fader_time.get(ch_num, now)
            elapsed_fader   = now - prev_fader_time

            if prev_fader is None:
                self._last_fader[ch_num]      = ch.fader_db
                self._last_fader_time[ch_num] = now
            elif elapsed_fader <= window_fader_s:
                if abs(ch.fader_db - prev_fader) >= suppress_fader_db:
                    self._suppressed_until[ch_num]    = now + duration_s
                    self._suppression_trigger[ch_num] = "rate_of_change_fader"
                    # Advance reference after firing so the next window starts fresh
                    self._last_fader[ch_num]      = ch.fader_db
                    self._last_fader_time[ch_num] = now
                # else: still inside window, no trigger — keep reference anchored
                # so accumulated small moves can reach the threshold
            else:
                # Window expired without a trigger — advance reference
                self._last_fader[ch_num]      = ch.fader_db
                self._last_fader_time[ch_num] = now

            # ── RMS rate-of-change (boost-pedal detection) ───────────────────
            prev_rms      = self._last_rms.get(ch_num)
            prev_rms_time = self._last_rms_time.get(ch_num, now)
            elapsed_rms   = now - prev_rms_time

            if prev_rms is None:
                self._last_rms[ch_num]      = ch.rms_db
                self._last_rms_time[ch_num] = now
            elif elapsed_rms <= window_rms_s:
                rms_rise = ch.rms_db - prev_rms   # positive = signal got louder
                if rms_rise >= suppress_rms_db:
                    self._suppressed_until[ch_num]    = now + duration_s
                    self._suppression_trigger[ch_num] = "rate_of_change_rms"
                    self._last_rms[ch_num]      = ch.rms_db
                    self._last_rms_time[ch_num] = now
            else:
                self._last_rms[ch_num]      = ch.rms_db
                self._last_rms_time[ch_num] = now

    def _is_suppressed(self, ch_num: int, now: float) -> bool:
        expiry = self._suppressed_until.get(ch_num, 0)
        return now < expiry

    def _cooldown_ok(self, ch_num: int, now: float) -> bool:
        cooldown = self._thresholds.get("recommendation_cooldown_s", 60.0)
        last = self._last_rec.get(ch_num, 0)
        return (now - last) >= cooldown

    def _issue_cooldown_ok(self, ch_num: int, issue: str, now: float) -> bool:
        """Like _cooldown_ok but uses the per-issue effective cooldown (stability guard)."""
        base = self._thresholds.get("recommendation_cooldown_s", 60.0)
        effective = self._issue_cooldown.get((ch_num, issue), base)
        last = self._last_issue_rec.get((ch_num, issue), 0)
        return (now - last) >= effective

    def _update_stability(self, ch_num: int, issue: str) -> None:
        """Track consecutive fires of (ch_num, issue) and double the cooldown
        from the 3rd fire onward, capping at 4× the base cooldown.

        Consecutive counter and extended cooldown are reset when:
          - notify_adjustment(ch_num) is called (engineer touched the channel)
          - The deviation resolves (drops below threshold between cycles)
          - A new session starts (__init__ zeroes all state)
        """
        base         = self._thresholds.get("recommendation_cooldown_s", 60.0)
        max_cooldown = base * 4
        key          = (ch_num, issue)
        count        = self._consecutive_fires.get(key, 0) + 1
        self._consecutive_fires[key] = count
        if count >= 3:
            current = self._issue_cooldown.get(key, base)
            self._issue_cooldown[key] = min(current * 2, max_cooldown)

    def _top_channels_by_rms(self, channels: dict[int, ChannelState],
                              n: int = 2) -> list[ChannelState]:
        active = [ch for ch in channels.values() if ch.is_active()]
        return sorted(active, key=lambda c: c.rms_db, reverse=True)[:n]

    def _find_culprit(self, band: str, channels: dict[int, ChannelState],
                      deviation: float) -> Optional[ChannelState]:
        """Return the most likely channel contributing to the problem in this band."""
        lo, hi = BAND_RANGES[band]
        candidates = []

        for ch in channels.values():
            if not ch.is_active():
                continue
            if ch.rms_db < -50.0:
                continue
            # Sparse mic guard: skip if below inactive threshold
            if ch.usage == "sparse" and not ch.is_active():
                continue

            fp = self._fingerprints.get(ch.label, {})
            overlap_score = 0.0
            for zone_name, zone_range in fp.items():
                if not (isinstance(zone_range, (list, tuple)) and len(zone_range) == 2):
                    continue
                fp_lo, fp_hi = zone_range
                overlap = min(fp_hi, hi) - max(fp_lo, lo)
                if overlap > 0:
                    band_width = hi - lo
                    overlap_score += overlap / band_width

            if overlap_score > 0:
                eq_boost_in_band = 0.0
                for eq_band in ch.eq:
                    if eq_band.type in (0, 5):
                        continue
                    if lo <= eq_band.freq_hz <= hi and eq_band.gain_db > 0:
                        eq_boost_in_band += eq_band.gain_db
                composite_score = overlap_score * (ch.rms_db + 90) + (eq_boost_in_band * 6.0)
                candidates.append((composite_score, ch))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _eq_recommendation(self, ch: ChannelState, band: str,
                            deviation: float) -> tuple[str, str]:
        """Return (current EQ description, suggestion string) for the problem band.

        Only suggests adjusting an existing EQ band if its freq_hz falls within
        2 octaves of the problem band centre.  If no band qualifies, suggests
        adding a new EQ point instead of adjusting an irrelevant existing band.
        """
        lo, hi = BAND_RANGES[band]
        mid_freq = (lo + hi) // 2

        best_band = None
        best_dist = float("inf")
        for eq_band in ch.eq:
            if eq_band.type in (0, 5):
                continue
            if not (mid_freq / 4.0 <= eq_band.freq_hz <= mid_freq * 4.0):
                continue
            dist = abs(eq_band.freq_hz - mid_freq)
            if dist < best_dist:
                best_dist = dist
                best_band = eq_band

        direction = "cut" if deviation > 0 else "boost"

        if best_band is None:
            action = "cut" if deviation > 0 else "boost"
            non_filter_bands = [b for b in ch.eq if b.type not in (0, 5)]
            if non_filter_bands:
                candidate = min(non_filter_bands, key=lambda b: abs(b.gain_db))
                return "", (
                    f"Move Band {candidate.band_num} "
                    f"(currently {candidate.freq_hz:.0f}Hz, {candidate.gain_db:+.1f}dB) "
                    f"to {mid_freq}Hz and {action}"
                )
            return "", f"Add EQ {action} in {lo}-{hi}Hz range (target ~{mid_freq}Hz)"

        eq_detail = (f"EQ Band {best_band.band_num} {best_band.gain_db:+.1f}dB "
                     f"@ {best_band.freq_hz:.0f}Hz")

        if deviation > 0:
            new_gain = best_band.gain_db - min(abs(deviation), 3.0)
            suggest  = (f"EQ Band {best_band.band_num} cut to "
                        f"{new_gain:+.1f}dB @ {best_band.freq_hz:.0f}Hz")
        else:
            new_gain = best_band.gain_db + min(abs(deviation), 3.0)
            suggest  = (f"EQ Band {best_band.band_num} boost to "
                        f"{new_gain:+.1f}dB @ {best_band.freq_hz:.0f}Hz")

        # IMP-023a — prepend named move
        name = _named_move(best_band.freq_hz, direction, ch.label)
        if name:
            suggest = f"{name} — {suggest}"

        # IMP-023b — position and Q advice
        if not _band_covers_problem(best_band, lo, hi):
            suggest += (f" | Band {best_band.band_num} is at {best_band.freq_hz:.0f}Hz"
                        f" — move to {mid_freq}Hz first")

        q_note = _q_advice(best_band, direction)
        if q_note:
            suggest += f" | {q_note}"

        return eq_detail, suggest
