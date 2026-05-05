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

        # Per-channel suppression state
        # {ch_num: expiry_timestamp}
        self._suppressed_until: dict[int, float] = {}
        # Per-channel last recommendation timestamp
        self._last_rec: dict[int, float] = {}
        # Per-channel last fader snapshot (for rate-of-change detection)
        self._last_fader: dict[int, float] = {}
        self._last_fader_time: dict[int, float] = {}

        self._baseline: Optional[dict[int, ChannelState]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_genre(self, profile: GenreProfile) -> None:
        self._genre = profile

    def set_baseline(self, snapshot: dict[int, ChannelState]) -> None:
        self._baseline = snapshot

    def evaluate(self, analysis: RoomAnalysis,
                 channels: dict[int, ChannelState]) -> list[Recommendation]:
        """Run one recommendation cycle. Returns list of new recommendations."""
        now = analysis.timestamp
        ts_str = time.strftime("%H:%M:%S", time.localtime(now))
        recs: list[Recommendation] = []

        self._update_suppression(channels, now)

        # 1. Overall LUFS vs genre target
        lufs_rec = self._check_lufs(analysis, channels, ts_str)
        if lufs_rec:
            recs.append(lufs_rec)

        # 2. Per-band frequency deviation
        band_recs = self._check_bands(analysis, channels, ts_str)
        recs.extend(band_recs)

        # 3. Baseline drift
        if self._baseline:
            drift_recs = self._check_baseline_drift(channels, ts_str)
            recs.extend(drift_recs)

        return recs

    # ------------------------------------------------------------------
    # Internal — checks
    # ------------------------------------------------------------------

    def _check_lufs(self, analysis: RoomAnalysis,
                    channels: dict[int, ChannelState],
                    ts_str: str) -> Optional[Recommendation]:
        threshold = self._thresholds.get("lufs_trigger_db", 2.0)
        deviation = analysis.lufs - self._genre.target_lufs
        if abs(deviation) <= threshold:
            return None

        direction = "hot" if deviation > 0 else "low"
        top = self._top_channels_by_rms(channels, n=2)
        top_labels = [f"{ch.label} ({ch.rms_db:.1f}dBFS)" for ch in top]
        contributors = ", ".join(top_labels) if top_labels else "unknown"

        if deviation > 0:
            suggest = (f"Pull main bus -{abs(deviation):.1f}dB "
                       f"or trim {' and '.join(c.label for c in top[:2])}")
        else:
            suggest = f"Main bus may be conservative — check fader levels"

        return Recommendation(
            channel_num=None,
            channel_label=None,
            issue="lufs_hot" if deviation > 0 else "lufs_low",
            detail=(f"Integrated LUFS {abs(deviation):.1f}dB {direction} vs "
                    f"{self._genre.id} target ({self._genre.target_lufs:.0f} LUFS)"),
            current_state={"lufs": f"{analysis.lufs:.1f}", "target": str(self._genre.target_lufs)},
            suggestion=suggest,
            genre_id=self._genre.id,
            timestamp=analysis.timestamp,
            timestamp_str=ts_str,
        )

    def _check_bands(self, analysis: RoomAnalysis,
                     channels: dict[int, ChannelState],
                     ts_str: str) -> list[Recommendation]:
        threshold = self._thresholds.get("recommendation_trigger_db", 3.0)
        recs = []
        now = analysis.timestamp

        # Compute a "balanced" band level — normalize relative to overall RMS
        # so we're comparing shape, not absolute level
        band_vals = [analysis.bands[b] for b in BAND_NAMES if analysis.bands[b] > -85]
        if not band_vals:
            return []
        median_level = sorted(band_vals)[len(band_vals) // 2]

        for band in BAND_NAMES:
            raw_level = analysis.bands[band]
            if raw_level <= -85:
                continue
            normalized = raw_level - median_level       # relative to mix median
            target_offset = self._genre.target_for_band(band)
            deviation = normalized - target_offset
            if abs(deviation) <= threshold:
                continue

            # Find likely contributing channel
            culprit = self._find_culprit(band, channels, deviation)
            if culprit is None:
                continue

            # Suppression and cooldown checks
            if self._is_suppressed(culprit.channel_num, now):
                continue
            if not self._cooldown_ok(culprit.channel_num, now):
                continue

            direction = "buildup" if deviation > 0 else "deficiency"
            lo, hi = BAND_RANGES[band]
            freq_label = f"{lo}-{hi}Hz"

            # Find the most relevant EQ band on this channel
            eq_detail, eq_suggest = self._eq_recommendation(
                culprit, band, deviation
            )

            current = {"rms": f"{culprit.rms_db:.1f}dBFS",
                       "fader": f"{culprit.fader_db:+.1f}dB"}
            if eq_detail:
                current["eq"] = eq_detail

            self._last_rec[culprit.channel_num] = now
            recs.append(Recommendation(
                channel_num=culprit.channel_num,
                channel_label=culprit.label,
                issue=f"{band}_{direction}",
                detail=f"{band.replace('_', '-')} {direction} around {freq_label} detected",
                current_state=current,
                suggestion=eq_suggest or f"Reduce {culprit.label} contribution in {freq_label}",
                genre_id=self._genre.id,
                timestamp=analysis.timestamp,
                timestamp_str=ts_str,
            ))

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
        """Detect fast fader moves and suppress those channels."""
        suppress_db = self._thresholds.get("rate_of_change_suppress_db", 3.0)
        window_s = self._thresholds.get("rate_of_change_window_s", 5.0)
        duration_s = self._thresholds.get("suppression_duration_s", 60.0)

        for ch_num, ch in channels.items():
            prev_fader = self._last_fader.get(ch_num)
            prev_time = self._last_fader_time.get(ch_num, now)

            if prev_fader is not None:
                elapsed = now - prev_time
                if elapsed <= window_s and abs(ch.fader_db - prev_fader) >= suppress_db:
                    self._suppressed_until[ch_num] = now + duration_s

            self._last_fader[ch_num] = ch.fader_db
            self._last_fader_time[ch_num] = now

    def _is_suppressed(self, ch_num: int, now: float) -> bool:
        expiry = self._suppressed_until.get(ch_num, 0)
        return now < expiry

    def _cooldown_ok(self, ch_num: int, now: float) -> bool:
        cooldown = self._thresholds.get("recommendation_cooldown_s", 60.0)
        last = self._last_rec.get(ch_num, 0)
        return (now - last) >= cooldown

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
            # Sparse mic guard: skip if below inactive threshold
            if ch.usage == "sparse" and not ch.is_active():
                continue

            fp = self._fingerprints.get(ch.label, {})
            primary = fp.get("primary", [])
            secondary = fp.get("secondary", [])

            # Check if this channel's fingerprint overlaps with the problem band
            overlap_score = 0
            if primary and len(primary) == 2:
                fp_lo, fp_hi = primary
                overlap = min(fp_hi, hi) - max(fp_lo, lo)
                if overlap > 0:
                    band_width = hi - lo
                    overlap_score = (overlap / band_width) * 2.0   # primary weight
            if secondary and len(secondary) == 2:
                fp_lo, fp_hi = secondary
                overlap = min(fp_hi, hi) - max(fp_lo, lo)
                if overlap > 0:
                    band_width = hi - lo
                    overlap_score += overlap / band_width           # secondary weight

            if overlap_score > 0:
                candidates.append((overlap_score * (ch.rms_db + 90), ch))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _eq_recommendation(self, ch: ChannelState, band: str,
                            deviation: float) -> tuple[str, str]:
        """Return (current EQ description, suggestion string) for the problem band."""
        lo, hi = BAND_RANGES[band]
        mid_freq = (lo + hi) // 2

        # Find the EQ band closest to the problem frequency
        best_band = None
        best_dist = float("inf")
        for eq_band in ch.eq:
            if eq_band.type in (0, 5):   # skip LCut/HCut — not gain EQs
                continue
            dist = abs(eq_band.freq_hz - mid_freq)
            if dist < best_dist:
                best_dist = dist
                best_band = eq_band

        if best_band is None:
            return "", f"Consider cutting {ch.label} in the {lo}-{hi}Hz range"

        eq_detail = (f"EQ Band {best_band.band_num} {best_band.gain_db:+.1f}dB "
                     f"@ {best_band.freq_hz:.0f}Hz")

        if deviation > 0:
            # Too much energy — suggest cut
            new_gain = best_band.gain_db - min(abs(deviation), 3.0)
            suggest = (f"EQ Band {best_band.band_num} cut to "
                       f"{new_gain:+.1f}dB @ {best_band.freq_hz:.0f}Hz")
        else:
            # Too little energy — suggest boost
            new_gain = best_band.gain_db + min(abs(deviation), 3.0)
            suggest = (f"EQ Band {best_band.band_num} boost to "
                       f"{new_gain:+.1f}dB @ {best_band.freq_hz:.0f}Hz")

        return eq_detail, suggest
