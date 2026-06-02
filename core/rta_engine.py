"""RTA Investigation Engine — manages the X32 /meters/15 analyzer as a shared resource.

The X32 has a single switchable RTA analyzer. This engine coordinates three
operating modes, ensuring only one is active at a time and the analyzer always
returns to Main L/R monitoring after any investigation or calibration scan.

State machine:
    MAIN_BUS      — default; /meters/15 monitoring Main L/R continuously
    INVESTIGATING — Tier 2 reactive channel scan (preempts CALIBRATING)
    CALIBRATING   — Tier 3 cal/iso scan (user-triggered)

Watchdog: if stuck in INVESTIGATING or CALIBRATING > 8 seconds, forces return
to MAIN_BUS and logs an error.
"""

import logging
import time
from enum import Enum

import numpy as np

from core.osc_client import X32OSCClient, channel_to_rta_index, MAIN_LR_RTA_INDEX
from core.mic_analyzer import normalize_to_shape, band_average
from core.forward_model import BAND_RANGES
from core.channel_model import N_FREQS, FREQ_AXIS

log = logging.getLogger(__name__)

CULPRIT_THRESHOLD_DB   = 2.0    # deviation to trigger culprit identification
INVESTIGATION_COOLDOWN = 30.0   # minimum seconds between Tier 2 scans per band
WATCHDOG_TIMEOUT_S     = 8.0    # max seconds before watchdog forces MAIN_BUS
CAL_ALPHA              = 0.1    # learning rate for cal scan prior updates
ISO_ALPHA              = 0.6    # learning rate for iso samples (clean isolated measurement)


class RTAState(Enum):
    MAIN_BUS      = 'main_bus'
    INVESTIGATING = 'investigating'
    CALIBRATING   = 'calibrating'


class RTAEngine:
    """Coordinate X32 RTA source switching and instrument prior updates."""

    def __init__(self, osc_client: X32OSCClient):
        self._osc = osc_client
        self._state = RTAState.MAIN_BUS
        self._state_entered_at = time.time()
        self._last_investigation: dict[str, float] = {}

    # ── State transitions ─────────────────────────────────────────────────

    def set_main_bus(self) -> None:
        """Return to continuous main bus monitoring. Always safe to call."""
        self._osc.set_rta_source(MAIN_LR_RTA_INDEX)
        self._state = RTAState.MAIN_BUS
        self._state_entered_at = time.time()

    def start_investigation(self, channel_rta_index: int) -> bool:
        """Switch RTA to a channel for Tier 2 reactive investigation.

        INVESTIGATING preempts CALIBRATING.
        Returns True if switched, False if already in INVESTIGATING.
        """
        if self._state == RTAState.INVESTIGATING:
            return False
        self._osc.set_rta_source(channel_rta_index)
        self._state = RTAState.INVESTIGATING
        self._state_entered_at = time.time()
        return True

    def check_watchdog(self) -> None:
        """Call every analysis cycle. Forces MAIN_BUS if stuck too long."""
        if self._state != RTAState.MAIN_BUS:
            elapsed = time.time() - self._state_entered_at
            if elapsed > WATCHDOG_TIMEOUT_S:
                log.error(
                    "RTA watchdog: stuck in %s for %.1fs — forcing MAIN_BUS",
                    self._state.value, elapsed,
                )
                self.set_main_bus()

    def investigation_allowed(self, band: str) -> bool:
        """True if cooldown has passed since last Tier 2 scan for this band."""
        return (time.time() - self._last_investigation.get(band, 0.0)) >= INVESTIGATION_COOLDOWN

    def record_investigation(self, band: str) -> None:
        self._last_investigation[band] = time.time()

    @property
    def state(self) -> RTAState:
        return self._state

    @property
    def is_available(self) -> bool:
        return self._state == RTAState.MAIN_BUS

    # ── Tier 2: Reactive channel investigation ────────────────────────────

    def investigate_channel(
        self,
        candidate_channels: list,
        problem_band: str,
        direction: str,
        forward_model,
    ) -> list[dict]:
        """Scan candidate channels in ranked order to identify the culprit.

        Switches RTA source for each candidate, collects 3 × 50ms readings,
        compares to forward model prediction. Returns list of scan results.
        Always returns RTA to MAIN_BUS after completion.

        Caller must verify investigation_allowed(band) before calling.
        """
        results = []

        for ch in candidate_channels[:5]:
            rta_idx = channel_to_rta_index(ch.channel_num)
            self.start_investigation(rta_idx)

            time.sleep(0.05)   # one settling frame

            readings = []
            for _ in range(3):
                readings.append(self._osc.board_rta_db.copy())
                time.sleep(0.05)

            avg_spectrum = np.mean(readings, axis=0)
            actual_db    = band_average(avg_spectrum, BAND_RANGES[problem_band])
            expected_db  = forward_model.predicted_band_db(ch.channel_num, problem_band)
            deviation    = actual_db - expected_db

            is_culprit = (
                deviation >  CULPRIT_THRESHOLD_DB if direction == 'buildup'
                else deviation < -CULPRIT_THRESHOLD_DB
            )

            results.append({
                'channel':     ch,
                'actual_db':   actual_db,
                'expected_db': expected_db,
                'deviation':   deviation,
                'is_culprit':  is_culprit,
            })

            if is_culprit:
                break

        self.set_main_bus()
        self.record_investigation(problem_band)
        return results

    # ── Tier 3: cal keyboard command scan ─────────────────────────────────

    def run_cal_scan(
        self,
        active_channels: list,
        forward_model,
        mic_analyzer,
        instrument_priors: dict,
    ) -> tuple[list[dict], list[dict]]:
        """User-triggered calibration scan (IMP-045).

        Scans active channels in priority order, comparing actual post-EQ RTA
        to forward model prediction. Applies damped prior updates (α=0.1).

        Returns (scan_results, prior_updates).
        Always returns RTA to MAIN_BUS after completion.
        """
        if not self.is_available:
            return [], []

        PRIORITY = {
            'vocal_lead': 0, 'guitar': 1, 'guitar_lead': 2,
            'keys': 3, 'bass_di': 4, 'kick': 5, 'overhead': 6,
        }
        sorted_channels = sorted(
            active_channels,
            key=lambda ch: PRIORITY.get(
                getattr(ch, 'instrument_type', 'guitar') if hasattr(ch, 'instrument_type')
                else 'guitar',
                99,
            ),
        )

        print(f"CAL: scanning {len(sorted_channels)} channels..."
              f" (~{len(sorted_channels) * 0.25:.1f}s)")

        self._state = RTAState.CALIBRATING
        self._state_entered_at = time.time()
        results = []

        try:
            for ch in sorted_channels:
                rta_idx = channel_to_rta_index(ch.channel_num)
                self._osc.set_rta_source(rta_idx)

                time.sleep(0.05)   # settling frame

                readings = []
                for _ in range(4):
                    readings.append(self._osc.board_rta_db.copy())
                    time.sleep(0.05)

                # Discard first reading (settling), average remaining 3
                avg_spectrum = np.mean(readings[1:], axis=0)

                band_results = {}
                instr_type = getattr(ch, 'instrument_type', 'guitar')
                for band_name, (f_lo, f_hi) in BAND_RANGES.items():
                    actual_db    = band_average(avg_spectrum, (f_lo, f_hi))
                    predicted_db = forward_model.predicted_band_db(ch.channel_num, band_name)
                    deviation    = actual_db - predicted_db
                    if abs(deviation) < 1.5:
                        status = 'ok'
                    elif abs(deviation) < 3.0:
                        status = 'notable'
                    else:
                        status = 'significant'
                    band_results[band_name] = {
                        'actual':    actual_db,
                        'predicted': predicted_db,
                        'deviation': deviation,
                        'status':    status,
                    }

                results.append({
                    'channel':         ch,
                    'channel_num':     ch.channel_num,
                    'instrument_type': instr_type,
                    'bands':           band_results,
                })
        finally:
            self.set_main_bus()

        prior_updates = _apply_prior_updates(results, instrument_priors, CAL_ALPHA)
        return results, prior_updates

    # ── iso soundcheck sampling ───────────────────────────────────────────

    def run_iso_sample(
        self,
        channel_num: int,
        channel_config,
        forward_model,
        mic_analyzer,
        instrument_priors: dict,
        duration_s: float = 12.0,
    ) -> tuple[dict, list[dict]]:
        """Soundcheck isolation sample (IMP-049).

        Captures isolated channel RTA + mic reading for direct prior correction.
        Uses ISO_ALPHA=0.6 — larger step because it is a clean, isolated measurement.

        Returns (sample_result, prior_updates).
        """
        if not self.is_available:
            print("ISO: RTA busy — try again in a moment.")
            return {}, []

        label     = getattr(channel_config, 'label', f'Ch{channel_num}')
        instr     = getattr(channel_config, 'instrument_type', 'guitar')

        print(f"\nISO: {label} ({instr})")
        print(f"  Mute all other channels or ensure they are below gate threshold.")
        print(f"  Play a representative {duration_s:.0f}-second passage.")
        print("  Press Enter to begin capture...", end='', flush=True)
        input()

        print(f"  Capturing {duration_s:.0f}s", end='', flush=True)

        rta_idx = channel_to_rta_index(channel_num)
        self._state = RTAState.CALIBRATING
        self._state_entered_at = time.time()
        self._osc.set_rta_source(rta_idx)

        board_readings = []
        mic_readings   = []
        sample_count   = max(1, int(duration_s / 0.2))

        try:
            for i in range(sample_count):
                board_readings.append(self._osc.board_rta_db.copy())
                mic_readings.append(mic_analyzer.get_current_normalized_shape())
                time.sleep(0.2)
                if (i + 1) % 5 == 0:
                    print('.', end='', flush=True)
        finally:
            self.set_main_bus()

        print(' done')

        # Discard first 2 readings (settling), average the rest
        skip = min(2, len(board_readings) - 1)
        board_shape = normalize_to_shape(np.mean(board_readings[skip:], axis=0))
        mic_shape   = normalize_to_shape(np.mean(mic_readings[skip:],   axis=0))

        prior = instrument_priors.get(channel_num)
        prior_curve = prior.get_curve('normal') if prior else np.zeros(N_FREQS)

        board_vs_prior = {}
        mic_vs_prior   = {}
        for band_name, (f_lo, f_hi) in BAND_RANGES.items():
            board_vs_prior[band_name] = float(
                band_average(board_shape, (f_lo, f_hi)) -
                band_average(prior_curve, (f_lo, f_hi))
            )
            mic_vs_prior[band_name] = float(
                band_average(mic_shape, (f_lo, f_hi)) -
                band_average(prior_curve, (f_lo, f_hi))
            )

        result = {
            'channel':         label,
            'channel_num':     channel_num,
            'instrument_type': instr,
            'board_vs_prior':  board_vs_prior,
            'mic_vs_prior':    mic_vs_prior,
            'duration_s':      duration_s,
        }

        prior_updates = _apply_prior_updates(
            [result], instrument_priors, ISO_ALPHA,
            use_mic_vs_prior=True, mic_vs_prior=mic_vs_prior,
        )
        return result, prior_updates


# ── Prior update logic ────────────────────────────────────────────────────

def _apply_prior_updates(
    results: list[dict],
    instrument_priors: dict,
    alpha: float,
    use_mic_vs_prior: bool = False,
    mic_vs_prior: 'dict | None' = None,
) -> list[dict]:
    """Apply damped prior updates from cal or iso scan results.

    alpha = 0.1 for cal scans (conservative — noisy live environment)
    alpha = 0.6 for iso samples (aggressive — clean isolated measurement)

    Minimum update threshold: 0.5 dB deviation required to update;
    0.05 dB minimum change applied.
    """
    updates = []
    for r in results:
        ch_num = r.get('channel_num')
        prior  = instrument_priors.get(ch_num) if ch_num is not None else None
        if prior is None:
            continue

        bands_data = (mic_vs_prior or r.get('bands', {})) if use_mic_vs_prior else r.get('bands', {})

        for band_name, band_info in bands_data.items():
            deviation = band_info if isinstance(band_info, float) else band_info.get('deviation', 0.0)
            if abs(deviation) < 0.5:
                continue
            freq_lo, freq_hi = BAND_RANGES.get(band_name, (0.0, 0.0))
            if freq_hi == 0.0:
                continue

            mask = (FREQ_AXIS >= freq_lo) & (FREQ_AXIS < freq_hi)
            curve = prior._curves.get('normal', np.zeros(N_FREQS))
            old_avg = float(np.mean(curve[mask])) if mask.any() else 0.0

            prior.update_band('normal', freq_lo, freq_hi, deviation, alpha)

            new_curve = prior._curves.get('normal', np.zeros(N_FREQS))
            new_avg = float(np.mean(new_curve[mask])) if mask.any() else 0.0

            if abs(new_avg - old_avg) >= 0.05:
                updates.append({
                    'instrument': prior.instrument_type,
                    'channel_num': ch_num,
                    'band': band_name,
                    'old': round(old_avg, 3),
                    'new': round(new_avg, 3),
                })
    return updates
