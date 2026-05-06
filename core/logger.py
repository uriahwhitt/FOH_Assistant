"""Session event logger, poll diff detection, and post-show report generator."""

import json
import time
from pathlib import Path
from typing import Optional

from models.channel import ChannelState
from models.event import AdjustmentEvent, LogEvent
from core.recommender import Recommendation


SHOWS_DIR = Path(__file__).parent.parent / "shows"
REC_CORRELATION_WINDOW_S = 300      # 5 minutes


class SessionLogger:
    def __init__(self, band_name: str, mode: str, x32_ip: str, genre_default: str):
        SHOWS_DIR.mkdir(exist_ok=True)
        date_str = time.strftime("%Y-%m-%d")
        self._log_path = SHOWS_DIR / f"{date_str}_show.json"
        self._session = {
            "date": date_str,
            "band": band_name,
            "mode": mode,
            "x32_ip": x32_ip,
            "genre_default": genre_default,
            "started_at": time.strftime("%H:%M:%S"),
            "ended_at": None,
        }
        self._events: list[dict] = []
        self._event_counter = 0

        # Most recent RECOMMENDATION per channel for correlation
        self._last_recs: dict[int, tuple] = {}
        self._last_global_rec: Optional[tuple] = None

        # Per-song segment tracking
        self._current_song_info: Optional[dict] = None   # song dict from setlist or None
        self._current_song_start: float = 0.0
        self._song_recs: list[str] = []     # event IDs logged during current song
        self._song_adj: list[str] = []      # adjustment event IDs during current song
        self._song_lufs: list[float] = []   # LUFS samples recorded during current song
        self._between_song_start: float = 0.0
        self._completed_songs: list[dict] = []   # finished segments (songs + gaps)
        self._song_counter: int = 0              # auto-increment for no-setlist mode

    # ------------------------------------------------------------------
    # Song event logging
    # ------------------------------------------------------------------

    def log_song_start(self, song: Optional[dict], position: int,
                       genre_id: str) -> str:
        """Log a SONG_START event.

        song — setlist dict {title, artist, genre_profile, ...} or None for
               no-setlist mode (auto-generates a generic title).
        position — 1-based setlist index (pass 0 for no-setlist mode).
        genre_id — active genre profile ID at song start.
        """
        now = time.time()
        ts_str = time.strftime("%H:%M:%S", time.localtime(now))
        self._song_counter += 1

        # Close any open between-songs gap
        if self._between_song_start > 0.0:
            gap_s = now - self._between_song_start
            self._completed_songs.append({
                "type": "between_songs",
                "duration_s": round(gap_s, 1),
            })
            self._between_song_start = 0.0

        self._current_song_info = song
        self._current_song_start = now
        self._song_recs = []
        self._song_adj  = []
        self._song_lufs = []

        title  = song.get("title",  "") if song else f"Song {self._song_counter}"
        artist = song.get("artist", "") if song else ""

        evt_id = self._next_id()
        self._events.append({
            "id":               evt_id,
            "timestamp":        ts_str,
            "type":             "SONG_START",
            "title":            title,
            "artist":           artist,
            "genre_profile":    genre_id,
            "setlist_position": position,
        })
        self._flush()
        return evt_id

    def log_song_end(self) -> str:
        """Log a SONG_END event and save the completed song segment."""
        now = time.time()
        ts_str = time.strftime("%H:%M:%S", time.localtime(now))
        duration_s = (now - self._current_song_start) if self._current_song_start else 0.0

        song   = self._current_song_info
        title  = song.get("title",  "") if song else f"Song {self._song_counter}"
        artist = song.get("artist", "") if song else ""
        genre  = song.get("genre_profile", "") if song else ""

        evt_id = self._next_id()
        self._events.append({
            "id":         evt_id,
            "timestamp":  ts_str,
            "type":       "SONG_END",
            "title":      title,
            "duration_s": round(duration_s, 1),
        })

        self._completed_songs.append({
            "type":         "song",
            "title":        title,
            "artist":       artist,
            "genre_profile": genre,
            "duration_s":   round(duration_s, 1),
            "rec_ids":      list(self._song_recs),
            "adj_ids":      list(self._song_adj),
            "lufs_samples": list(self._song_lufs),
        })

        # Start the between-songs gap timer
        self._between_song_start = now
        self._current_song_info  = None
        self._current_song_start = 0.0
        self._song_recs = []
        self._song_adj  = []
        self._song_lufs = []

        self._flush()
        return evt_id

    def record_lufs(self, lufs: float) -> None:
        """Record a LUFS sample for the currently active song.
        Silently ignored when no song is active or the sample is silence-sentinel."""
        if self._current_song_start > 0.0 and lufs > -70.0:
            self._song_lufs.append(lufs)

    # ------------------------------------------------------------------
    # Recommendation events
    # ------------------------------------------------------------------

    def log_recommendation(self, rec: Recommendation) -> str:
        evt_id = self._next_id()
        entry = {
            "id":            evt_id,
            "timestamp":     rec.timestamp_str or time.strftime("%H:%M:%S"),
            "type":          "RECOMMENDATION",
            "channel":       rec.channel_label,
            "channel_num":   rec.channel_num,
            "genre_profile": rec.genre_id,
            "issue":         rec.issue,
            "detail":        rec.detail,
            "current_state": rec.current_state,
            "suggestion":    rec.suggestion,
        }
        self._events.append(entry)
        if rec.channel_num is not None:
            self._last_recs[rec.channel_num] = (
                evt_id, rec.timestamp, rec.suggestion, rec.current_state
            )
        else:
            self._last_global_rec = (evt_id, rec.timestamp, rec.suggestion, rec.current_state)
        if self._current_song_start > 0.0:
            self._song_recs.append(evt_id)
        self._flush()
        return evt_id

    # ------------------------------------------------------------------
    # Manual adjustment detection (poll diff)
    # ------------------------------------------------------------------

    def detect_and_log_adjustments(self,
                                    prev: dict[int, ChannelState],
                                    curr: dict[int, ChannelState],
                                    thresholds: dict) -> list[AdjustmentEvent]:
        """Diff two channel snapshots, log detected adjustments, return event list."""
        fader_thr = thresholds.get("adjustment_detect_fader_db", 0.5)
        eq_thr    = thresholds.get("adjustment_detect_eq_db",    0.5)
        freq_thr  = thresholds.get("adjustment_detect_freq_hz",  10.0)

        now    = time.time()
        ts_str = time.strftime("%H:%M:%S", time.localtime(now))
        detected: list[AdjustmentEvent] = []

        for ch_num in curr:
            if ch_num not in prev:
                continue
            p, c = prev[ch_num], curr[ch_num]

            if abs(c.fader_db - p.fader_db) >= fader_thr:
                adj = AdjustmentEvent(ch_num, c.label, "fader", p.fader_db, c.fader_db, now)
                detected.append(adj)
                self._log_adjustment(adj, ts_str)

            if c.muted != p.muted:
                adj = AdjustmentEvent(ch_num, c.label, "mute",
                                      float(p.muted), float(c.muted), now)
                detected.append(adj)
                self._log_adjustment(adj, ts_str)

            for b_idx in range(4):
                pb, cb = p.eq[b_idx], c.eq[b_idx]
                if abs(cb.gain_db - pb.gain_db) >= eq_thr:
                    param = f"eq_band_{b_idx+1}_gain"
                    adj = AdjustmentEvent(ch_num, c.label, param,
                                          pb.gain_db, cb.gain_db, now)
                    detected.append(adj)
                    self._log_adjustment(adj, ts_str)
                if abs(cb.freq_hz - pb.freq_hz) >= freq_thr:
                    param = f"eq_band_{b_idx+1}_freq"
                    adj = AdjustmentEvent(ch_num, c.label, param,
                                          pb.freq_hz, cb.freq_hz, now)
                    detected.append(adj)
                    self._log_adjustment(adj, ts_str)

        return detected

    def _log_adjustment(self, adj: AdjustmentEvent, ts_str: str) -> None:
        evt_id = self._next_id()
        prior_id, match_status, delta_desc, lag = self._correlate(adj)

        entry = {
            "id":                      evt_id,
            "timestamp":               ts_str,
            "type":                    "MANUAL_ADJUSTMENT",
            "channel":                 adj.channel_label,
            "channel_num":             adj.channel_num,
            "parameter":               adj.parameter,
            "before":                  round(adj.before, 2),
            "after":                   round(adj.after,  2),
            "prior_recommendation_id": prior_id,
            "match_status":            match_status,
            "suggestion_delta":        delta_desc,
            "lag_seconds":             lag,
        }
        self._events.append(entry)
        if self._current_song_start > 0.0:
            self._song_adj.append(evt_id)
        self._flush()

    def _correlate(self, adj: AdjustmentEvent) -> tuple:
        """Return (prior_rec_id, match_status, delta_desc, lag_seconds)."""
        rec_info = self._last_recs.get(adj.channel_num)
        if rec_info is None:
            return None, "engineer_initiated", None, None

        prior_id, rec_ts, suggestion, rec_state = rec_info
        lag = adj.timestamp - rec_ts

        if lag > REC_CORRELATION_WINDOW_S:
            return None, "engineer_initiated", None, None

        param = adj.parameter
        if param == "fader" and "fader" in suggestion.lower():
            change = adj.after - adj.before
            match_status = "matched" if abs(change) >= 0.5 else "partial"
            delta = f"Applied {change:+.1f}dB fader change"
        elif "eq" in param and "eq" in suggestion.lower():
            change = adj.after - adj.before
            match_status = "matched" if abs(change) >= 0.5 else "partial"
            delta = f"Applied {change:+.1f}dB EQ change on {param}"
        else:
            match_status = "partial"
            delta = "Parameter type differs from recommendation"

        return prior_id, match_status, delta, round(lag, 1)

    # ------------------------------------------------------------------
    # Generic event logging
    # ------------------------------------------------------------------

    def log_event(self, event_type: str, detail: str = "",
                  channel: str = None, channel_num: int = None,
                  extra: dict = None) -> None:
        entry = {
            "id":          self._next_id(),
            "timestamp":   time.strftime("%H:%M:%S"),
            "type":        event_type,
            "channel":     channel,
            "channel_num": channel_num,
            "detail":      detail,
        }
        if extra:
            entry.update(extra)
        self._events.append(entry)
        self._flush()

    # ------------------------------------------------------------------
    # Post-show report
    # ------------------------------------------------------------------

    def generate_report(self) -> str:
        self._session["ended_at"] = time.strftime("%H:%M:%S")
        self._flush()

        recs        = [e for e in self._events if e["type"] == "RECOMMENDATION"]
        adjustments = [e for e in self._events if e["type"] == "MANUAL_ADJUSTMENT"]

        matched      = [a for a in adjustments if a.get("match_status") == "matched"]
        partial      = [a for a in adjustments if a.get("match_status") == "partial"]
        ignored_recs = [r for r in recs if not any(
            a.get("prior_recommendation_id") == r["id"] for a in adjustments
        )]
        engineer_init = [a for a in adjustments if a.get("match_status") == "engineer_initiated"]

        lags    = [a["lag_seconds"] for a in matched if a.get("lag_seconds") is not None]
        avg_lag = sum(lags) / len(lags) if lags else 0

        ch_activity: dict[str, dict] = {}
        for e in recs:
            lbl = e.get("channel") or "Overall"
            ch_activity.setdefault(lbl, {"recs": 0, "manual": 0})
            ch_activity[lbl]["recs"] += 1
        for e in adjustments:
            lbl = e.get("channel") or "Overall"
            ch_activity.setdefault(lbl, {"recs": 0, "manual": 0})
            ch_activity[lbl]["manual"] += 1

        top_channels  = sorted(ch_activity.items(),
                                key=lambda x: x[1]["recs"] + x[1]["manual"],
                                reverse=True)[:5]
        drift_events  = [e for e in self._events if e["type"] == "BASELINE_DRIFT"]
        sparse_events = [e for e in self._events if e["type"] == "SPARSE_MIC_ACTIVE"]
        feedback_events = [e for e in self._events if e["type"] == "FEEDBACK_SPIKE"]
        total_recs = len(recs)
        sep = "=" * 47

        lines = [
            sep,
            "FOH ASSISTANT -- SHOW REPORT",
            f"Date: {self._session['date']} | Band: {self._session['band']}",
            f"Duration: {self._session['started_at']} - {self._session['ended_at']} | Genre: {self._session['genre_default']}",
            sep,
            "",
            "RECOMMENDATION ACCURACY",
            f"  Total recommendations:    {total_recs:>4}",
            f"  Matched:                  {len(matched):>4}  ({int(len(matched)/max(total_recs,1)*100)}%)",
            f"  Partially matched:        {len(partial):>4}  ({int(len(partial)/max(total_recs,1)*100)}%)",
            f"  Ignored / no follow-up:   {len(ignored_recs):>4}  ({int(len(ignored_recs)/max(total_recs,1)*100)}%)",
            "",
            "ENGINEER-INITIATED ADJUSTMENTS",
            f"  Total adjustments:        {len(adjustments):>4}",
            f"  No prior recommendation:  {len(engineer_init):>4}  <- review for blind spots",
            f"  Avg lag to match:         {avg_lag:.0f}s" if lags else "  Avg lag:                  n/a",
            "",
            "TOP CHANNELS BY ACTIVITY",
        ]
        for lbl, counts in top_channels:
            total = counts["recs"] + counts["manual"]
            lines.append(f"  {lbl:<20} {total} events ({counts['recs']} recs, {counts['manual']} manual)")

        blind_spots = [a["channel"] for a in engineer_init if a.get("channel")]
        if blind_spots:
            from collections import Counter
            top_bs = Counter(blind_spots).most_common(3)
            lines += ["", "TOP BLIND SPOT CHANNELS"]
            for lbl, count in top_bs:
                lines.append(f"  {lbl}: {count} engineer adjustments, 0 recommendations")

        if drift_events:
            lines += ["", "BASELINE DRIFT EVENTS"]
            for e in drift_events:
                lines.append(f"  {e.get('channel','?')}: {e.get('detail','')}")
        else:
            lines += ["", "BASELINE DRIFT", "  No significant drift detected"]

        if sparse_events:
            lines += ["", "SPARSE MIC EVENTS"]
            for e in sparse_events:
                lines.append(f"  {e.get('channel','?')}: {e.get('detail','')}")
        else:
            lines += ["", "SPARSE MIC EVENTS", "  None"]

        lines += ["", "FEEDBACK EVENTS"]
        lines.append(f"  {'None detected' if not feedback_events else str(len(feedback_events)) + ' events -- review log'}")

        # Per-song breakdown
        if self._completed_songs:
            lines += ["", "SONG BREAKDOWN"]
            song_num = 0
            for seg in self._completed_songs:
                if seg["type"] == "between_songs":
                    dur = seg["duration_s"]
                    m, s = divmod(int(dur), 60)
                    lines.append(f"  [Between songs: {m}:{s:02d}]")
                else:
                    song_num += 1
                    dur = seg["duration_s"]
                    m, s = divmod(int(dur), 60)
                    artist_str = f" ({seg['artist']})" if seg.get("artist") else ""
                    genre_str  = f" — {seg['genre_profile']}" if seg.get("genre_profile") else ""
                    lines.append(f"  {song_num}. {seg['title']}{artist_str}{genre_str}   [{m}:{s:02d}]")

                    seg_recs = [e for e in self._events
                                if e["type"] == "RECOMMENDATION" and e["id"] in seg["rec_ids"]]
                    seg_adjs = [e for e in self._events
                                if e["type"] == "MANUAL_ADJUSTMENT" and e["id"] in seg["adj_ids"]]
                    n_recs = len(seg_recs)
                    n_matched = sum(
                        1 for a in seg_adjs
                        if a.get("match_status") == "matched"
                        and a.get("prior_recommendation_id") in seg["rec_ids"]
                    )
                    acc_str = f"{int(n_matched/max(n_recs,1)*100)}%" if n_recs else "n/a"

                    lufs = seg["lufs_samples"]
                    if lufs:
                        peak_lufs = max(lufs)
                        avg_lufs  = sum(lufs) / len(lufs)
                        lufs_str  = f"Peak LUFS: {peak_lufs:.1f}  Avg: {avg_lufs:.1f}"
                    else:
                        lufs_str = "LUFS: no data"

                    lines.append(f"     Recs: {n_recs} | Matched: {n_matched} ({acc_str}) | {lufs_str}")

                    # Notable events in this song
                    song_drift = [e for e in self._events
                                  if e["type"] == "BASELINE_DRIFT"
                                  and e.get("id", "") in {e2["id"] for e2 in seg_adjs}]
                    if song_drift:
                        lines.append(f"     Drift alerts: {len(song_drift)}")

        lines += ["", f"Full log: {self._log_path}", sep]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        self._event_counter += 1
        return f"evt_{self._event_counter:04d}"

    def _flush(self) -> None:
        doc = {"session": self._session, "events": self._events}
        with open(self._log_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)

    @property
    def log_path(self) -> Path:
        return self._log_path
