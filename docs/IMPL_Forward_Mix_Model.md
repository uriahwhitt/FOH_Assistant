# FOH Assistant — Forward Mix Model & Enhanced Logging Implementation
**Document Type:** Claude Code Implementation Reference  
**Phase:** 2  
**Last Updated:** 2026-06-02  
**Depends on:** IMPL_X32_Board_Model.md, IMPL_Mic_Analyzer.md, IMPL_Geometry.md  
**Produces:** `core/forward_model.py` (new), `core/logger.py` (extended), `models/analysis.py` (new)

---

## Purpose

This document specifies the forward mix model that combines board data and mic data
into a unified prediction-vs-measurement comparison, and the enhanced logging schema
that captures enough data from the June 13 and June 20 shows to validate the model
and begin building the training dataset for future AI/ML work.

The forward model runs in PASSIVE mode for both June shows — it calculates predictions
and logs everything but fires no new recommendations to the terminal. The existing
LUFS and rule-based recommendations from Phase 1 continue to run alongside it.

---

## 1. Forward Model Overview

### The Core Equation

```
predicted(f) = Σ contribution_i(f)  for all active channels i

deviation(f) = measured_mic(f) - predicted(f)
```

Where:
- `contribution_i(f)` = channel i's spectral output (from IMPL_X32_Board_Model.md)
- `measured_mic(f)` = geometry-corrected mic FFT (from IMPL_Mic_Analyzer.md)
- Both are in dB on the shared FREQ_AXIS (1000 log-spaced points, 20Hz–20kHz)

### The Three-Way Comparison

```
Board RTA (/meters/15)   ←→   Predicted   ←→   Mic (room)
     board_deviation              deviation
```

- `board_deviation = board_rta - predicted`
  → How well does the channel model reconstruct the board's own output?
  → R² target: > 0.70 at June 13 show (validation metric)
  → Residuals here = model error (channel model is incomplete or wrong)

- `deviation = mic - predicted`  
  → How does the room differ from what the board is putting out?
  → Residuals here = room acoustics + PA coloration + measurement artifacts
  → Accumulates into venue room transfer function over multiple shows

### Deviation Decomposition

Deviation has two components with different temporal signatures:

**Room deviation** — Slow-moving, consistent across songs. Represents
the venue's acoustic fingerprint: PA coloration, room modes, boundary effects.
Estimated as the low-frequency (in time) component of deviation.

**Mix deviation** — Faster-moving, correlated with specific channel changes.
Represents actual mix problems the engineer can address. Identified by
correlating deviation changes with channel RMS events.

```python
def decompose_deviation(deviation_history: list[np.ndarray],
                          window_cycles: int = 60) -> tuple[np.ndarray, np.ndarray]:
    """
    Separate systematic (room) from transient (mix) deviation.
    deviation_history: last N deviation arrays (500ms cycles each)
    window_cycles: how many cycles to average for room estimate (60 = 30 seconds)
    
    Returns: (room_deviation, mix_deviation)
    """
    if len(deviation_history) < 10:
        # Not enough history — return zeros for room component
        return np.zeros(N_FREQS), deviation_history[-1].copy()
    
    history_array = np.array(deviation_history[-window_cycles:])
    
    # Room deviation = median across recent history (robust to outliers)
    room_deviation = np.median(history_array, axis=0)
    
    # Mix deviation = current - room
    mix_deviation = deviation_history[-1] - room_deviation
    
    return room_deviation, mix_deviation
```

---

## 2. Channel Contribution Scoring

When the mix deviation is significant at a frequency band, identify which
channel is most responsible and therefore most likely to benefit from adjustment.

```python
def score_channel_contributions(contributions: dict[int, np.ndarray],
                                  freq_low: float,
                                  freq_high: float) -> dict[int, float]:
    """
    Score each channel's contribution to a specific frequency band.
    Returns {channel_num: score} where score = fraction of total band energy.
    
    contributions: {channel_num: contribution_curve_db (N_FREQS,)}
    """
    mask = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
    
    if not mask.any():
        return {}
    
    # Convert dB to linear power for proper summation
    band_powers = {}
    for ch_num, curve_db in contributions.items():
        band_linear = np.mean(10.0 ** (curve_db[mask] / 10.0))
        band_powers[ch_num] = max(band_linear, 1e-12)
    
    total_power = sum(band_powers.values())
    
    if total_power < 1e-12:
        return {ch: 0.0 for ch in band_powers}
    
    return {ch: power / total_power for ch, power in band_powers.items()}

def find_dominant_channel(contributions: dict[int, np.ndarray],
                           band_name: str) -> tuple[int, float]:
    """
    Returns (channel_num, score) of the highest-contributing channel
    in the named analysis band.
    """
    freq_low, freq_high = BAND_RANGES[band_name]
    scores = score_channel_contributions(contributions, freq_low, freq_high)
    
    if not scores:
        return (-1, 0.0)
    
    dominant = max(scores, key=scores.get)
    return (dominant, scores[dominant])

BAND_RANGES = {
    'sub':       (20,    80),
    'bass':      (80,   200),
    'low_mid':   (200,  500),
    'mid_low':   (500,  1000),
    'mid_high':  (1000, 2000),
    'upper_mid': (2000, 4000),
    'presence':  (4000, 8000),
    'air':       (8000, 20000),
}
```

---

## 3. Confidence Scoring

Not all deviations should fire recommendations. Confidence scoring gates
which deviations are actionable vs uncertain.

```python
def compute_confidence(mic_analysis: 'MicAnalysis',
                         board_rta_db: np.ndarray,
                         predicted_db: np.ndarray,
                         dominant_score: float,
                         venue_acoustics: 'VenueAcoustics') -> np.ndarray:
    """
    Per-frequency confidence score [0.0, 1.0].
    
    Three components multiplied together:
    - mic_agreement:       How well does mic match board RTA at this frequency?
    - contribution_dominance: Is one channel clearly responsible?
    - reliability_weight:  Is this a trustworthy frequency for this venue?
    
    A score > CONFIDENCE_THRESHOLD warrants a recommendation.
    """
    # 1. Mic-to-RTA agreement: how close is the mic to the board's own output?
    #    High agreement = mic is a reliable indicator of board content here
    rta_vs_mic_diff = np.abs(mic_analysis.smoothed_spectrum_db - board_rta_db)
    # Convert diff to agreement: 0dB diff = 1.0, 6dB diff = 0.0
    mic_agreement = np.clip(1.0 - rta_vs_mic_diff / 6.0, 0.0, 1.0)
    
    # 2. Contribution dominance: is one channel clearly responsible?
    #    (This is a scalar per-band, broadcast to per-frequency for simplicity)
    dominance_scalar = min(dominant_score / 0.5, 1.0)   # 50% share = full confidence
    dominance = np.full(N_FREQS, dominance_scalar)
    
    # 3. Venue reliability weight (from geometry — comb filter notch regions get 0.3)
    reliability = venue_acoustics.comb_reliability_mask()
    
    # Room mode penalty: reduce confidence near predicted room modes
    mode_mask = venue_acoustics.room_mode_mask()
    reliability = np.where(mode_mask, reliability * 0.4, reliability)
    
    # Overall mic reliability for this venue type
    overall_reliability = venue_acoustics.mic_reliability_weight()
    
    confidence = mic_agreement * dominance * reliability * overall_reliability
    
    return confidence

CONFIDENCE_THRESHOLD = 0.65   # minimum to fire a recommendation
```

---

## 4. ForwardModel Class

```python
class ForwardModel:
    """
    Combines board channel contributions with mic analysis to produce
    a predicted vs measured comparison with confidence scoring.
    
    Runs passively in Phase 2 — logs results but does not fire recommendations.
    The flag `passive_mode` controls this. Set to False in Phase 3+.
    """
    
    PASSIVE_MODE = True    # Phase 2: log only, no new recommendations
    
    def __init__(self, venue_acoustics: 'VenueAcoustics'):
        self.venue_acoustics = venue_acoustics
        self._deviation_history: list[np.ndarray] = []
        self._cycle_count = 0
    
    def run(self,
             channel_configs: dict[int, 'ChannelConfig'],
             channel_meters: dict[int, 'ChannelMeterState'],
             channel_priors: dict[int, 'InstrumentPrior'],
             mic_analysis: 'MicAnalysis',
             board_rta_db: np.ndarray) -> 'ForwardModelResult':
        """
        Execute one forward model cycle.
        
        Call once per analysis cycle (500ms).
        Returns ForwardModelResult for logging and (in Phase 3+) recommendation.
        """
        self._cycle_count += 1
        
        # 1. Skip if room is silent
        if mic_analysis.is_silent:
            return ForwardModelResult.silent(timestamp_ms=current_time_ms())
        
        # 2. Compute per-channel contribution curves
        contributions = {}
        for ch_num, config in channel_configs.items():
            if ch_num not in channel_meters:
                continue
            meter = channel_meters[ch_num]
            prior = channel_priors.get(ch_num)
            if prior is None:
                continue
            
            input_state = meter.input_state
            prior_curve = prior.get_curve(input_state)
            
            contributions[ch_num] = compute_contribution_curve(
                config, meter, prior_curve
            )
        
        # 3. Sum contributions → predicted spectrum
        if not contributions:
            return ForwardModelResult.no_active_channels(
                timestamp_ms=current_time_ms()
            )
        
        # Sum in linear, convert to dB
        predicted_linear = np.zeros(N_FREQS)
        for curve_db in contributions.values():
            predicted_linear += 10.0 ** (curve_db / 10.0)
        
        # Avoid log(0)
        predicted_db = 10.0 * np.log10(np.maximum(predicted_linear, 1e-12))
        
        # 4. Compute deviations
        mic_deviation_db   = mic_analysis.smoothed_spectrum_db - predicted_db
        board_deviation_db = board_rta_db - predicted_db
        
        # 5. Accumulate deviation history and decompose
        self._deviation_history.append(mic_deviation_db.copy())
        if len(self._deviation_history) > 120:   # keep 60 seconds
            self._deviation_history.pop(0)
        
        room_deviation_db, mix_deviation_db = decompose_deviation(
            self._deviation_history
        )
        
        # 6. R² correlation metrics
        r_squared_mic   = self._compute_r_squared(
            predicted_db, mic_analysis.smoothed_spectrum_db
        )
        r_squared_board = self._compute_r_squared(predicted_db, board_rta_db)
        
        # 7. Channel attribution per band
        dominant_channels   = {}
        contribution_scores = {}
        
        for band_name in BAND_RANGES:
            dominant_ch, dominant_score = find_dominant_channel(
                contributions, band_name
            )
            dominant_channels[band_name]   = dominant_ch
            contribution_scores[band_name] = dominant_score
        
        # 8. Confidence scoring (use worst-case dominant score across bands)
        avg_dominance = np.mean(list(contribution_scores.values()))
        confidence = compute_confidence(
            mic_analysis, board_rta_db, predicted_db,
            avg_dominance, self.venue_acoustics
        )
        
        # 9. Identify actionable bands (mix deviation + high confidence)
        actionable_bands = []
        for band_name, (freq_low, freq_high) in BAND_RANGES.items():
            mask = (FREQ_AXIS >= freq_low) & (FREQ_AXIS < freq_high)
            band_deviation = float(np.mean(mix_deviation_db[mask]))
            band_confidence = float(np.mean(confidence[mask]))
            
            if (abs(band_deviation) > 2.0 and 
                band_confidence > CONFIDENCE_THRESHOLD and
                not mic_analysis.is_silent):
                actionable_bands.append({
                    'band':       band_name,
                    'deviation':  band_deviation,
                    'confidence': band_confidence,
                    'direction':  'hot' if band_deviation > 0 else 'low',
                    'channel':    dominant_channels.get(band_name, -1),
                    'ch_score':   contribution_scores.get(band_name, 0.0),
                })
        
        return ForwardModelResult(
            predicted_db=predicted_db,
            measured_db=mic_analysis.smoothed_spectrum_db,
            board_rta_db=board_rta_db,
            deviation_db=mic_deviation_db,
            board_deviation_db=board_deviation_db,
            mix_deviation_db=mix_deviation_db,
            room_deviation_db=room_deviation_db,
            confidence=confidence,
            dominant_channels=dominant_channels,
            contribution_scores=contribution_scores,
            channel_contributions=contributions,
            actionable_bands=actionable_bands,
            r_squared_mic=r_squared_mic,
            r_squared_board=r_squared_board,
            passive_mode=self.PASSIVE_MODE,
            timestamp_ms=current_time_ms(),
            cycle_num=self._cycle_count,
        )
    
    @staticmethod
    def _compute_r_squared(predicted: np.ndarray, measured: np.ndarray) -> float:
        """Pearson R² correlation between predicted and measured spectra."""
        if len(predicted) != len(measured):
            return 0.0
        ss_res = np.sum((measured - predicted) ** 2)
        ss_tot = np.sum((measured - np.mean(measured)) ** 2)
        if ss_tot < 1e-12:
            return 0.0
        return float(1.0 - ss_res / ss_tot)
```

---

## 5. ForwardModelResult Data Model

```python
from dataclasses import dataclass, field
from typing import Optional
import numpy as np

@dataclass
class ForwardModelResult:
    """Complete forward model output for one analysis cycle."""
    
    # Core spectra (all on FREQ_AXIS, shape (N_FREQS,), dBFS)
    predicted_db:       np.ndarray    # sum of channel contributions
    measured_db:        np.ndarray    # geometry-corrected mic FFT
    board_rta_db:       np.ndarray    # X32 /meters/15 main bus RTA
    
    # Deviations
    deviation_db:       np.ndarray    # measured - predicted (full)
    board_deviation_db: np.ndarray    # board_rta - predicted
    mix_deviation_db:   np.ndarray    # transient component (mix problems)
    room_deviation_db:  np.ndarray    # systematic component (room acoustics)
    
    # Confidence
    confidence:         np.ndarray    # per-frequency [0.0, 1.0]
    
    # Attribution
    dominant_channels:    dict        # band_name -> channel_num
    contribution_scores:  dict        # band_name -> dominant channel score
    channel_contributions: dict       # channel_num -> contribution_db array
    
    # Actionable findings (non-empty only when passive_mode=False)
    actionable_bands: list            # list of dicts per actionable band
    
    # Validation metrics
    r_squared_mic:   float            # predicted vs mic correlation
    r_squared_board: float            # predicted vs board RTA correlation
    
    # Metadata
    passive_mode: bool
    timestamp_ms: float
    cycle_num: int
    is_silent: bool = False
    no_active_channels: bool = False
    
    @classmethod
    def silent(cls, timestamp_ms: float) -> 'ForwardModelResult':
        """Sentinel result for silent cycles."""
        empty = np.full(N_FREQS, -90.0)
        return cls(
            predicted_db=empty, measured_db=empty, board_rta_db=empty,
            deviation_db=np.zeros(N_FREQS), board_deviation_db=np.zeros(N_FREQS),
            mix_deviation_db=np.zeros(N_FREQS), room_deviation_db=np.zeros(N_FREQS),
            confidence=np.zeros(N_FREQS),
            dominant_channels={}, contribution_scores={}, channel_contributions={},
            actionable_bands=[], r_squared_mic=0.0, r_squared_board=0.0,
            passive_mode=True, timestamp_ms=timestamp_ms, cycle_num=0,
            is_silent=True
        )
    
    @classmethod
    def no_active_channels(cls, timestamp_ms: float) -> 'ForwardModelResult':
        """Sentinel result when no channels have signal."""
        result = cls.silent(timestamp_ms)
        result.is_silent = False
        result.no_active_channels = True
        return result
```

---

## 6. Enhanced Log Schema

### 6.1 New Event Types

The following event types are added to the logger in Phase 2.
All new events are logged alongside the existing Phase 1 events.

---

#### ANALYSIS_CYCLE
Logged every 500ms during the show when room is not silent.
This is the primary data record for post-show analysis and model validation.

```json
{
  "id": "evt_XXXX",
  "timestamp": "21:34:15.220",
  "type": "ANALYSIS_CYCLE",
  "cycle_num": 847,
  "song": "Working for the Weekend",
  "genre": "AOR",
  
  "forward_model": {
    "passive_mode": true,
    "r_squared_mic": 0.74,
    "r_squared_board": 0.81,
    "actionable_bands": [],
    
    "deviation_by_band": {
      "sub":       {"deviation_db": -0.4, "confidence": 0.71},
      "bass":      {"deviation_db":  1.2, "confidence": 0.68},
      "low_mid":   {"deviation_db": -0.8, "confidence": 0.74},
      "mid_low":   {"deviation_db":  0.3, "confidence": 0.72},
      "mid_high":  {"deviation_db": -0.2, "confidence": 0.69},
      "upper_mid": {"deviation_db":  2.1, "confidence": 0.61},
      "presence":  {"deviation_db":  0.8, "confidence": 0.66},
      "air":       {"deviation_db": -0.1, "confidence": 0.55}
    },
    
    "dominant_channels": {
      "sub":       1,
      "bass":      3,
      "low_mid":   7,
      "mid_low":   11,
      "mid_high":  11,
      "upper_mid": 7,
      "presence":  7,
      "air":       2
    },
    
    "channel_contributions_snapshot": {
      "1":  {"label": "Kick",       "post_fade_db": -18.2, "input_state": "normal"},
      "3":  {"label": "Bass DI",    "post_fade_db": -20.1, "input_state": "normal"},
      "7":  {"label": "Guitar 1",   "post_fade_db": -16.8, "input_state": "normal"},
      "8":  {"label": "Guitar 2",   "post_fade_db": -17.2, "input_state": "normal"},
      "11": {"label": "Lead Vocal", "post_fade_db": -19.0, "input_state": "normal"}
    }
  },
  
  "mic": {
    "lufs": -17.8,
    "spectral_centroid_hz": 842,
    "is_silent": false,
    "correction_applied": true,
    "band_levels": {
      "sub":       {"avg_db": -42.1, "peak_db": -38.4, "peak_hz": 63.0},
      "bass":      {"avg_db": -38.2, "peak_db": -34.1, "peak_hz": 125.0},
      "low_mid":   {"avg_db": -36.4, "peak_db": -33.2, "peak_hz": 312.0},
      "mid_low":   {"avg_db": -37.8, "peak_db": -35.1, "peak_hz": 630.0},
      "mid_high":  {"avg_db": -38.9, "peak_db": -36.2, "peak_hz": 1250.0},
      "upper_mid": {"avg_db": -39.2, "peak_db": -36.8, "peak_hz": 2500.0},
      "presence":  {"avg_db": -41.0, "peak_db": -38.1, "peak_hz": 5000.0},
      "air":       {"avg_db": -46.2, "peak_db": -43.0, "peak_hz": 10000.0}
    },
    "room_mode_active_bands": []
  },
  
  "board_rta": {
    "band_levels": {
      "sub":       -41.8,
      "bass":      -37.1,
      "low_mid":   -37.2,
      "mid_low":   -37.5,
      "mid_high":  -38.7,
      "upper_mid": -37.1,
      "presence":  -40.2,
      "air":       -45.9
    }
  }
}
```

---

#### INPUT_STATE_EVENT
Logged when a channel's inferred input state changes (e.g. solo onset detected).

```json
{
  "id": "evt_XXXX",
  "timestamp": "21:48:32.140",
  "type": "INPUT_STATE_EVENT",
  "channel": "Guitar 1",
  "channel_num": 7,
  "from_state": "normal",
  "to_state": "solo_onset",
  
  "board": {
    "rms_delta_db": 3.2,
    "post_fade_db_before": -16.8,
    "post_fade_db_after": -13.6,
    "gate_gr_db": 0.0,
    "dyn_gr_db": -1.8
  },
  
  "mic": {
    "centroid_shift_hz": 840,
    "spectral_shift_direction": "upward",
    "dominant_band": "upper_mid",
    "band_deltas_db": {
      "sub":       -0.2,
      "bass":      -0.4,
      "low_mid":   -0.6,
      "mid_low":    0.3,
      "mid_high":   1.1,
      "upper_mid":  2.8,
      "presence":   1.4,
      "air":        0.6
    },
    "mic_confirmed_change": true
  },
  
  "song": "Round and Round",
  "genre": "Glam Metal"
}
```

---

#### VENUE_SESSION_START
Logged once at show start after geometry loads.

```json
{
  "id": "evt_0001",
  "timestamp": "20:27:22.000",
  "type": "VENUE_SESSION_START",
  "venue_id": "outdoor_patio_june13",
  "venue_name": "Outdoor Patio — June 13",
  "stage_type": "open_air",
  
  "geometry": {
    "dist_mic_to_top_left_m": 4.8,
    "dist_mic_to_top_right_m": 5.2,
    "dist_mic_to_sub_left_m": 5.1,
    "dist_mic_to_sub_right_m": 5.5,
    "arrival_delta_tops_ms": 1.2,
    "comb_notch_frequencies_hz": [417],
    "room_modes_hz": {},
    "sub_phase_at_crossover_deg": 87.0,
    "sub_boundary_gain_db": 0.0
  },
  
  "acoustic_adjustments": {
    "lufs_target_adjustment_db": 2.0,
    "sub_target_adjustment_db": 3.5,
    "mic_reliability_weight": 0.90,
    "correction_applied": true
  },
  
  "mic_device": "AT2035 via PreSonus Studio 26c",
  "sample_rate_hz": 48000,
  "sub_phase_warning": null
}
```

---

#### CONFIG_CHANGE
Logged when engineer changes EQ, HPF, or fader (detected via /xremote push).
Enhanced from Phase 1 to include the recomputed transfer curve snapshot.

```json
{
  "id": "evt_XXXX",
  "timestamp": "20:45:18.330",
  "type": "CONFIG_CHANGE",
  "channel": "Guitar 1",
  "channel_num": 7,
  "parameter": "eq_band_2_gain",
  "before": -1.0,
  "after":  -3.5,
  
  "eq_state_after": {
    "eq_enabled": true,
    "bands": [
      {"num": 1, "type": "LCut", "freq_hz": 120.0, "gain_db": 0.0,  "q": 0.7},
      {"num": 2, "type": "PEQ",  "freq_hz": 320.0, "gain_db": -3.5, "q": 1.4},
      {"num": 3, "type": "PEQ",  "freq_hz": 2500.0,"gain_db": 1.5,  "q": 2.1},
      {"num": 4, "type": "HShv", "freq_hz": 8000.0,"gain_db": 1.0,  "q": 0.7}
    ]
  },
  
  "transfer_curve_snapshot_db": [...],
  
  "forward_model_context": {
    "r_squared_before_change": 0.72,
    "deviation_at_change_db": {
      "low_mid": -2.8,
      "mid_low": -0.4
    },
    "was_flagged_by_model": false
  },
  
  "song": "Working for the Weekend",
  "genre": "AOR"
}
```

---

#### SESSION_SUMMARY
Logged at show end (Ctrl+C or end of setlist).

```json
{
  "id": "evt_FINAL",
  "timestamp": "23:20:08.000",
  "type": "SESSION_SUMMARY",
  
  "duration_s": 10366,
  "songs_played": 32,
  "total_analysis_cycles": 20732,
  
  "forward_model_performance": {
    "mean_r_squared_mic": 0.71,
    "mean_r_squared_board": 0.79,
    "r_squared_by_song": [...],
    "cycles_above_threshold": 15840,
    "pct_cycles_above_threshold": 76.4
  },
  
  "mic_summary": {
    "mean_lufs": -17.4,
    "lufs_std_dev": 1.8,
    "mean_spectral_centroid_hz": 924
  },
  
  "input_state_events": {
    "total": 18,
    "solo_onsets": 14,
    "mic_confirmed": 12,
    "pct_mic_confirmed": 85.7
  },
  
  "config_changes": {
    "total": 31,
    "by_channel": {
      "Guitar 1": 8,
      "Guitar 2": 6,
      "Lead Vocal": 5,
      "Bass DI": 4,
      "Kick": 4,
      "Keys": 4
    }
  },
  
  "venue_id": "outdoor_patio_june13"
}
```

---

### 6.2 Retained Phase 1 Event Types

The following events from Phase 1 continue unchanged:
- `RECOMMENDATION` — LUFS and rule-based recommendations
- `MANUAL_ADJUSTMENT` — Fader moves logged as engineer-initiated
- `SONG_START`, `SONG_END` — Setlist navigation
- `SETLIST_NAV` — Navigation events
- `AMBIENT_WARNING` — Ambient baseline not captured

`MANUAL_ADJUSTMENT` is enhanced with forward model context:
```json
{
  "type": "MANUAL_ADJUSTMENT",
  "channel": "Guitar 1",
  "channel_num": 7,
  "parameter": "fader",
  "before": -16.8,
  "after": -18.2,
  "prior_recommendation_id": "evt_0842",
  "match_status": "confirms_recommendation",
  "lag_seconds": 47,
  
  "forward_model_context": {
    "model_was_flagging_band": "upper_mid",
    "deviation_at_adjustment_db": 2.4,
    "confidence_at_adjustment": 0.71
  }
}
```

---

## 7. logger.py Extension Checklist

- [ ] Add `log_analysis_cycle(result: ForwardModelResult, mic: MicAnalysis)` method
      Log every 500ms during show. Only log when not silent.
      Include full band breakdown and channel contributions snapshot.

- [ ] Add `log_input_state_event(channel_num, from_state, to_state, board_data, mic_data)` method

- [ ] Add `log_venue_session_start(venue_profile)` method
      Call immediately after venue profile loads at session start.

- [ ] Add `log_config_change(channel_num, parameter, before, after, eq_state, model_context)` method
      Replace existing config change logging with enhanced version.

- [ ] Add `log_session_summary(session_stats)` method
      Called on session end instead of existing post-show report.

- [ ] Enhance `log_manual_adjustment()` to include `forward_model_context` field

- [ ] Reduce ANALYSIS_CYCLE log verbosity setting:
      In config, allow `log_level: full | summary | minimal`
      - `full`:    log every cycle including full spectrum arrays (June 13 validation)
      - `summary`: log every cycle, band levels only (normal show use)
      - `minimal`: log 1 in 10 cycles (bandwidth-constrained situations)
      Default: `summary`. Set to `full` for June 13 and June 20 shows.

---

## 8. main.py Integration

### 8.1 Startup Sequence Addition

After existing startup (connect X32, init audio capture):

```python
# Load venue profile
venue_id = args.venue or band_config.get('default_venue')
if venue_id:
    venue_profile = load_venue_profile(venue_id)
    venue_acoustics = venue_profile.acoustics
    logger.log_venue_session_start(venue_profile)
else:
    venue_acoustics = IrregularRoomAcoustics({})
    logger.log_warning("No venue profile loaded — acoustic corrections disabled")

# Initialize analysis components
mic_analyzer   = MicAnalyzer(venue_acoustics)
forward_model  = ForwardModel(venue_acoustics)
spectrum_history = SpectrumHistory()
```

### 8.2 Analysis Loop Addition

In the main 500ms analysis loop, after existing LUFS check:

```python
# Get latest data
mic_result   = mic_analyzer.analyze(audio_capture)
board_rta_db = osc_client.board_rta_db   # from /meters/15 subscription
channel_configs = osc_client.channel_configs
channel_meters  = osc_client.channel_meters

# Store mic snapshot for event history
spectrum_history.push(mic_result)

# Run forward model (passive — logs only, no new recs in Phase 2)
fm_result = forward_model.run(
    channel_configs=channel_configs,
    channel_meters=channel_meters,
    channel_priors=instrument_priors,
    mic_analysis=mic_result,
    board_rta_db=board_rta_db,
)

# Log cycle
logger.log_analysis_cycle(fm_result, mic_result)

# Input state event detection
for ch_num, meter in channel_meters.items():
    if meter.input_state in ('solo_onset',) and \
       meter._prev_input_state not in ('solo_onset', 'solo_active'):
        pre_spectrum = spectrum_history.get_snapshot_before(
            meter.timestamp_ms, offset_ms=500.0
        )
        post_spectrum = mic_result.smoothed_spectrum_db
        characterization = mic_analyzer.characterize_input_event(
            pre_spectrum, post_spectrum
        )
        logger.log_input_state_event(
            ch_num, meter._prev_input_state, meter.input_state,
            meter, characterization
        )
```

### 8.3 CLI Flag Addition

```
python main.py --show --venue ajs_bar
python main.py --show --venue outdoor_patio_june13
python main.py --show --no-venue         # skip geometry
python main.py --setup-venue             # venue geometry capture wizard
python main.py --show --log-level full   # full spectrum logging (June shows)
```

---

## 9. Post-Show Validation Report

After the June 13 show, run the validation tool against the log:

```bash
python tools/validate.py --show-log shows/2026-06-13_show.json --report
```

Report should include:
- Mean R² (board model accuracy target: > 0.70)
- R² distribution across songs
- Bands where model consistently over/underpredicts
- Input state events confirmed by mic (target: > 70%)
- Any systematic deviation pattern (→ candidate for room transfer function)

This report tells us whether the forward model is working and what needs
refinement before activating recommendations from it at the June 20 show.

---

## 10. Forward Model Validation Target (June 13)

| Metric | Target | Notes |
|---|---|---|
| Mean R² (predicted vs board RTA) | > 0.70 | Primary model validation |
| Mean R² (predicted vs mic) | > 0.55 | Lower — mic includes room coloration |
| Input state events, mic confirmed | > 65% | Solo detection working |
| Analysis cycles logged | > 15000 | Full show coverage |
| ANALYSIS_CYCLE events per song | > 200 | ~2min per song minimum |

If R² (board) < 0.70: primary cause is likely incorrect instrument priors
or HPF/EQ transfer function errors. Review CONFIG_CHANGE events and compare
transfer curve snapshots to actual mix behavior.

If R² (board) > 0.70 but R² (mic) < 0.55: room deviation is larger than
expected. Check venue geometry measurements — comb filter correction may be
insufficient. This data populates the room transfer function for future shows.

---

## 9. Display Buffer Integration (IMP-051)

The forward model result feeds the `DisplayBuffer` each analysis cycle. This is the bridge between the analysis loop and the live spectrum window.

### Update Points

**Every 500ms analysis cycle** (after `ForwardModelResult` is computed):

```python
if display_buffer:
    # Mic deviation from target → band highlights
    highlights = _compute_band_highlights(mic_result, active_genre)
    # Peak hz per band → peak markers on display
    peaks      = _extract_band_peaks(mic_result)

    display_buffer.update(
        board_rta_shape = normalize_to_shape(fm_result.board_rta_db),
        mic_shape       = mic_result.normalized_shape_db,
        band_highlights = highlights,
        band_peaks      = peaks,
        lufs            = mic_result.lufs,
        is_silent       = mic_result.is_silent,
    )
```

**On song change** (when `current_song` or `active_genre` changes):

```python
if display_buffer and active_genre:
    display_buffer.update(
        genre_target = _genre_to_shape_array(active_genre),
        song_name    = current_song.title if current_song else "",
        genre_name   = active_genre.name if active_genre else "",
    )
```

**Every 50ms** (in `/meters/15` OSC handler — fast path):

```python
if display_buffer:
    display_buffer.update(
        board_rta_fast = normalize_to_shape(osc.board_rta_db)
    )
```

### `_compute_band_highlights()`

Computes mic normalized shape deviation from genre target per band. This is the only input to highlight colors — board RTA is never used.

```python
def _compute_band_highlights(mic_result: MicAnalysis,
                               genre: GenreProfile) -> dict:
    highlights = {}
    for band, (f_lo, f_hi) in BAND_RANGES_DISPLAY.items():
        mic_avg    = band_average(mic_result.normalized_shape_db, (f_lo, f_hi))
        target_avg = genre.frequency_targets.get(band, 0.0)
        highlights[band] = mic_avg - target_avg   # positive = excess, negative = deficiency
    return highlights
```

### `_extract_band_peaks()`

```python
def _extract_band_peaks(mic_result: MicAnalysis) -> dict:
    peaks = {}
    for band in BAND_RANGES_DISPLAY:
        if band in mic_result.band_levels:
            lvl = mic_result.band_levels[band]
            peaks[band] = (lvl['peak_hz'], lvl.get('peak_prominence_db', 0.0))
    return peaks
```

### `_genre_to_shape_array()`

Converts genre YAML `frequency_targets` dict (band_name → dB offset) to a 1000-point FREQ_AXIS array for smooth display rendering. Called once on song change, not every cycle.

```python
def _genre_to_shape_array(genre: GenreProfile) -> np.ndarray:
    band_centers = {
        'sub': 50, 'bass': 150, 'low_mid': 350, 'mid_low': 750,
        'mid_high': 1500, 'upper_mid': 3000, 'presence': 6000, 'air': 14000,
    }
    freqs  = sorted(band_centers.values())
    values = [genre.frequency_targets.get(b, 0.0)
              for b in sorted(band_centers, key=lambda x: band_centers[x])]
    log_centers = np.log10(freqs)
    log_axis    = np.log10(FREQ_AXIS)
    return np.interp(log_axis, log_centers, values,
                     left=values[0], right=values[-1])
```

---

*Reference documents: IMPL_X32_Board_Model.md, IMPL_Mic_Analyzer.md, IMPL_Geometry.md*

