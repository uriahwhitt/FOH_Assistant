# FOH Assistant — Design Improvements Tracker
**Version:** 2.0  
**Status:** Active  
**Last Updated:** 2026-06-02 — IMP-051/052/053: spectrum display window, peak detection, dual-path mic FFT  
**Purpose:** Capture design improvements, architecture decisions, and lessons learned. Supersedes all prior versions. Reflects current architecture as of May 2026.

---

## Architecture Overview (Current)

The analyzer architecture shifted significantly after Show 1 (May 9, AJ's Bar). The original design used the room microphone as the primary per-channel intelligence source — this failed because a room mic cannot resolve individual channel contributions from the mixed output. The current architecture is:

**Primary per-channel intelligence: X32 OSC meter data + parametric EQ model**  
**Room mic role: overall LUFS monitoring + room acoustic sanity check only**  
**Main bus spectrum: X32 `/meters/15` RTA (100-band, post-EQ) via `/-action/setrtasrc`**  
**Per-channel spectrum: calculated from EQ transfer functions (IMP-034) + RTA investigation scans (IMP-043)**

---

## Priority Legend

| Tag | Meaning |
|---|---|
| `[CRITICAL]` | Blocks correct behavior — fix immediately |
| `[HIGH]` | Significantly improves usefulness — prioritize in current phase |
| `[MEDIUM]` | Good improvement — schedule for next phase |
| `[LOW]` | Nice to have — backlog |
| `[FUTURE]` | Long-term vision — capture for later phases |

---

## Status Legend

| Symbol | Meaning |
|---|---|
| ✅ Implemented | Shipped by Claude Code, tests passing |
| 🔧 In Progress | Claude Code prompt written, implementation underway |
| 📋 Designed | Fully spec'd, ready for Claude Code |
| 💬 Discussed | Design discussed, not yet fully spec'd |
| 🚫 Superseded | Replaced by newer architecture — do not implement |

---

## Active Items

---

### IMP-020 — Transition Grace Cancels Immediately on Song Start
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** ✅ Implemented  
**Source:** Design review — medley and direct-roll situations suppress monitoring at the start of a new song

`set_transition(False)` is called first in `_handle_next()` to cancel any active grace before opening the new song. Grace window reduced from 30s to 8s. Direct-roll `n → n` resumes recommendations within one cycle.

---

### IMP-021 — Soundcheck Mode (`--soundcheck`)
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** ✅ Implemented  
**Source:** Design review — `--baseline` was interactive/on-demand; engineers need continuous advisory while working the board

Continuous real-time advisory during soundcheck. Recommendation cooldown shortened to 20s. Deviation stability guard disabled (repeat flags desired until fixed). `confirm` keypress locks current board state as baseline and exits to show mode. HPF and gain staging checks active in this mode only.

---

### IMP-022 — HPF State and Input Gain from X32
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** ✅ Implemented — HPF on/off advisory suppressed pending bug fix (see IMP-026)  
**Source:** Design review — HPF and gain staging are foundational and invisible without reading these parameters

Reads `/ch/nn/preamp/hpf` (frequency), `/ch/nn/preamp/hpslope` (slope), `/ch/nn/preamp/hpon` (phantom/HPF status), and gain. Added to `ChannelState` and baseline snapshot. Soundcheck advisory fires per active channel when HPF appears off. **See IMP-026 for HPF false negative — on/off detection is currently unreliable.**

---

### IMP-023 — Full Parametric EQ Advisory
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** ✅ Implemented  
**Source:** Design review and Audio Guide Sections 4.3, 4.4, 4.5, 12.5

Four extensions to `core/recommender.py`:
- **Named move recognition** — "Mud cut", "Presence boost", etc. prefixed to all EQ suggestions
- **EQ band position and Q advisory** — warns when band frequency doesn't cover the problem zone; Q too narrow for boost or too broad for cut
- **Psychoacoustic band weighting** — high-mid deviations weighted 1.3×, sub-bass 0.6× before threshold comparison
- **Multi-factor culprit scoring** — EQ boost in problem band adds to composite score alongside RMS × fingerprint overlap

---

### IMP-024 — Soundcheck Reference Song Analysis Tool
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 1.5  
**Status:** 📋 Designed  
**Source:** Design discussion — pre-analyzing the soundcheck song gives a precise per-segment frequency target

Offline tool `tools/analyze_reference.py` segments a studio audio file by timestamp range, computes average frequency band profile per segment, saves as YAML reference target. Soundcheck mode uses reference segment targets instead of genre template when loaded. RMS spike on guitar channel auto-switches to solo segment for that channel's evaluation window.

---

### IMP-025 — Frequency Fingerprint Corrections
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** ✅ Implemented  
**Source:** Audio Guide Section 7 — broad fingerprints produced wrong culprit attribution

Config-only change to `config/band.yaml`. Corrected fingerprints:
- **Kick:** split into fundamental (50–80Hz), body (80–150Hz), click (2–4kHz), mud_zone (300–500Hz cut target)
- **Guitar 1/2:** split into body (200–1kHz) and bite (2–5kHz)
- **Bass DI:** added definition (700–1kHz) and attack (2–4kHz) zones
- **Drum Rack / Floor Tom:** separated with distinct fundamentals
- **Keys:** split into bass_register, body, brilliance zones
- **Acoustic Guitar:** body (150–400Hz), sparkle (2–6kHz), air (8–16kHz)

---

### IMP-026 — HPF Status False Negative
**Priority:** `[CRITICAL]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed — investigation complete  
**Source:** Show 1 — HPF confirmed set on tablet but system reported not set

**Root cause identified:** `/ch/[01..32]/preamp/hpon` controls **phantom power**, not HPF enable — the OSC documentation explicitly states this. There is no separate HPF on/off address in the protocol. The HPF appears to be active whenever `/preamp/hpf` is set above ~20Hz (minimum value = filter off).

**Fix:** Remove `hpf_on` boolean from `ChannelState`. Treat HPF as engaged when `hpf_freq_hz > 22.0`. Read `/preamp/hpf` frequency and `/preamp/hpslope` — report the actual cutoff value in advisory output rather than a binary on/off state.

**Recommended live test:** With `/xremote` active, toggle HPF on/off from tablet and capture raw OSC broadcast to confirm which address fires. The board broadcasts all parameter changes to registered clients — this will give definitive truth regardless of documentation.

---

### IMP-027 — Venue Profile System
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Show 1 — house PA configured for DJ/hip-hop caused problems the mix model couldn't account for

Per-venue YAML profiles stored in `config/venues/`. Loaded at session start by venue name. Profile contains room acoustics notes, PA hardware and configuration, and critical pre-show checklist items derived from profile settings.

**AJ's Bar profile (first venue):**
```yaml
venue:
  name: "AJ's Bar"
  capacity: 200
  room:
    dimensions_approx: "medium club"
    acoustic_notes: >
      Partial wall divides stage area from bar side ~10ft high with cutout.
      Significant level drop on bar side. Bass frequencies pass wall freely,
      mids and highs are attenuated. Mic placement: bar side, center of
      cutout, ear height (4-5ft), 8-10ft past wall.
  pa:
    tops: "QSC KW152"
    subs: "QSC KLA181-BK"
    configuration: "ground stacked, one top + one sub each side of stage"
    front_end: "Peavey PV14BT"
    signal_chain: "X32 main out → Peavey PV14BT → KLA181 sub → KW152 tops"
  pa_settings:
    kw152_lf_mode: "EXT_SUB"       # Critical — must be set at every show
    kw152_hf_mode: "FLAT"
    kla181_attenuation: "pulled back ~4dB from default"
    peavey_comp: "OFF"             # Was on — smashing mix dynamics
    peavey_kosmos: "OFF"           # Was on — artificially boosting low end
    peavey_hpf: "engaged on all active channels"
  notes: >
    House PA configured for DJ/hip-hop — significant low-end boost.
    EXT SUB mode + sub attenuation + Kosmos off = transformative improvement.
    X32 presets were solid — room/PA was the problem, not the mix.
```

**Session start behavior:** When venue is selected, system prints PA checklist — "Confirm KW152 LF Mode is EXT SUB", "Confirm Kosmos is OFF", etc. Engineer checks each item before soundcheck begins.

---

### IMP-028 — Ambient Noise Baseline Capture
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Show 1 — AMBIENT_WARNING fired at show start because no empty-room baseline was captured

Two capture types:
- `ambient_empty` — captured pre-show with PA on but band not playing. Establishes noise floor.
- `ambient_crowd` — captured during set break. Tracks crowd noise impact on mic readings.

Reference mic LUFS and per-band readings during these captures stored in show log. Used during show to correct band readings — crowd noise floor subtracted from room mic analysis. **At AJ's Bar:** place mic on bar side of partial wall at ear height before running empty room capture. Crowd baseline most useful during set break when jukebox is off.

---

### IMP-029 — Setlist Navigation Improvements
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Show 1 operational feedback

- `p` command — go to previous song (mispress recovery)
- Setlist position display in terminal header (e.g. "Song 4/22 — Round and Round")
- Between-song elapsed timer displayed at FOH so engineer knows how long gap has been
- `--song <n>` startup flag to begin session mid-setlist (resuming after technical restart)

---

### IMP-030 — X32 Channel Name Pull from Board
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Band may rename channels on board — config drift between band.yaml and actual board labels

At startup, read `/ch/[01..32]/config/name` from X32 and compare against `band.yaml` channel labels. If mismatch detected, print warning: "Ch 07 board name 'GTR1' — band.yaml label 'Guitar 1' (OK)" or flag significant mismatches. Does not auto-update config — engineer confirms manually.

---

### IMP-031 — Reference Mic Placement Documentation (AJ's Bar)
**Priority:** `[LOW]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Show 1 — mic placement behind partial wall caused inconsistent readings

Confirmed optimal placement for AJ's Bar: bar side of partial wall, center of cutout opening, ear height (4–5ft), 8–10ft past the wall. Add to venue profile and print at session start. Level targets: peaks hitting -12 to -6dBFS on laptop input meter during loud sections.

---

### IMP-032 — DJI Mic 2 Transmitter Level Note
**Priority:** `[LOW]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Show 1 — transmitter was set to -6dB for video recording use

Transmitter attenuated at capsule (-6dB) is the correct configuration for both this application and video recording — preserves headroom in the analog chain. Do not change for video use. For show use: if readings consistently below -20dBFS, bump transmitter to -3dB.

---

### IMP-033 — Genre Profile: Funk/R&B Gap
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Identified  
**Source:** Setlist review — Superstition, Cult of Personality, Play That Funky Music, Brick House all map to Hard Rock as fallback

Funk/R&B character doesn't fit Hard Rock profile targets. Bass sits heavier relative to guitar, low-mid is tighter, rhythm guitar is more percussive. Recommended: add `funk_rock.yaml` genre profile after reviewing show log data from June 13.

---

### IMP-034 — Forward Mix Model: Parametric EQ Transfer Function Calculator
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 🔧 In Progress — Claude Code prompt written  
**Source:** Architecture shift — build predicted room spectrum from OSC board data

New module `core/channel_model.py`. For each channel, reconstruct the full frequency response curve mathematically from the four parametric EQ bands reported via OSC (center frequency F, gain G in dB, Q factor). Standard second-order IIR biquad filter — frequency response is exactly calculable.

```python
def parametric_eq_response(freqs: np.ndarray, center_hz: float,
                            gain_db: float, q: float) -> np.ndarray:
    w0 = 2 * np.pi * center_hz
    w  = 2 * np.pi * freqs
    A  = 10 ** (gain_db / 40)
    numerator   = (w0/q * A) ** 2 + (w**2 - w0**2)**2
    denominator = (w0/q / A) ** 2 + (w**2 - w0**2)**2
    return 10 * np.log10(np.maximum(numerator / denominator, 1e-10))
```

Apply for all four bands per channel, sum responses → complete EQ transfer function. Multiply by channel RMS scalar → predicted frequency contribution of that channel to the mix.

**Note:** Channel EQ is 4 bands (confirmed from OSC PDF). Bus and main EQ is 6 bands. HPF modeled separately using `/preamp/hpf` frequency and `/preamp/hpslope`.

---

### IMP-035 — Forward Mix Model: Instrument Prior Library
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 🔧 In Progress  
**Source:** Architecture discussion — baseline spectral shape needed before EQ is applied

Per-instrument empirical spectral shapes stored in `config/instrument_priors_<venue>.yaml`. Represents natural frequency distribution before EQ. Dominates the model when channel has flat/minimal EQ; EQ transfer function dominates when significant EQ is applied.

**Refinement strategy:** After each `cal` scan (IMP-045), priors are updated with α=0.1 dampening toward measured reality. Persisted per venue — priors converge over multiple shows at the same location.

---

### IMP-036 — Forward Mix Model: Channel Contribution Scoring
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 🔧 In Progress  
**Source:** Architecture discussion — replaces fingerprint matching as the primary attribution mechanism

```python
def contribution_score(channel: int, freq_hz: float,
                        channel_outputs: dict, total_mix: np.ndarray,
                        freq_bins: np.ndarray) -> float:
    bin_idx = np.argmin(np.abs(freq_bins - freq_hz))
    channel_energy = channel_outputs[channel][bin_idx]
    total_energy   = total_mix[bin_idx]
    if total_energy <= 0:
        return 0.0
    return float(channel_energy / total_energy)
```

Channel with highest contribution score at a problem frequency receives the recommendation. If no single channel dominates (all scores below 0.4), system flags diffuse problem and reports top two with scores.

---

### IMP-037 — Forward Mix Model: Predicted vs Measured Comparison
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 🔧 In Progress  
**Source:** Architecture discussion — core of the new analysis architecture

At each analysis cycle:
1. Build predicted mix spectrum from channel contributions (IMP-034 + IMP-035 + IMP-036)
2. Read measured spectrum from room mic FFT
3. Compute `deviation(f) = measured(f) - predicted(f)`
4. Decompose: **mix deviation** (correlates with specific channel changes → actionable) vs **room deviation** (systematic offset not explained by channels → venue profile)

**Deviation thresholds:** `> +3dB` → excess energy, check highest contributing channel. `< -3dB` → absorption, likely room not mix. Consistent across songs → room transfer function. Variable song to song → mix problem.

---

### IMP-038 — Forward Mix Model: Room Transfer Function Capture
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2–3  
**Status:** 📋 Designed  
**Source:** Architecture discussion

The systematic component of `measured - predicted` that persists across songs is the room's acoustic fingerprint. Accumulated across N songs where mix deviation is low:

```python
room_transfer(f) = mean(deviation(f)) across songs where mix_deviation is low
```

Stored in venue profile as `room_transfer_function`. Loaded at next visit and applied as correction to predicted spectrum before comparison. Converges to accurate room model over multiple shows without dedicated acoustic measurement.

---

### IMP-039 — Forward Mix Model: Recommendation Confidence Scoring
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Architecture discussion — gates recommendations to reduce false positives

```python
confidence = mic_agreement × contribution_dominance × stability
```

- `mic_agreement`: does room mic confirm the predicted deviation? (1.0 = agreement, 0.0 = contradiction)
- `contribution_dominance`: is one channel clearly responsible? (contribution_score / 0.6, clamped 0–1)
- `stability`: has deviation been present for > N seconds? (seconds / threshold, clamped 0–1)

Fire recommendation only when `confidence > 0.65`. Starting threshold — tune from June 13 log data. Log confidence score for every potential recommendation (fired or suppressed) for post-show threshold calibration.

---

### IMP-040 — Hardware Upgrade: AT2035 + PreSonus Studio 26c
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2 — June 13 show  
**Status:** 📋 Ready to implement  
**Source:** Neighbor loan — 2026-05-09

Replace DJI Mic 2 USB receiver with AT2035 large diaphragm cardioid condenser via PreSonus Studio 26c USB interface. AT2035 has flat response 20Hz–20kHz with slight presence boost ~10kHz, self-noise 12dB(A).

**Setup:**
- Pad: OFF. High-pass filter: OFF (let analyzer see full range).
- Channel 1: AT2035. Channel 2: available for boundary mic (Phase 3).
- Gain: peaks at -12 to -6dBFS during full band play. 48V engaged on Ch1.

**Code changes in `core/audio_capture.py`:**
- Device detection: search for "PreSonus" or "Studio 26"
- Add device name to `band.yaml` config (not hardcoded)
- Startup reminder: "AT2035 requires +48V phantom power — confirm PreSonus Ch1 48V is engaged"
- Mono from channel 1 (left channel, index 0). Sample rate: 48000Hz.

**June 13 protocol:** AT2035 only — no boundary mic on channel 2 yet. Single variable.

---

### IMP-041 — June 13 Show: Passive Logging Validation Protocol
**Priority:** `[CRITICAL]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Architecture decision — validate forward model before activating channel-level recommendations

June 13 is a passive logging show. Forward mix model (IMP-034–039) runs silently. Existing recommendation engine (LUFS + broad band) continues active. No new channel-level recommendations fire.

**What runs silently (logged, not displayed):**
- Forward mix model predictions per cycle
- Predicted vs measured deviation per band
- Channel contribution scores per cycle
- Confidence scores for all potential recommendations
- Room transfer function accumulation

**Post-show validation targets:**
1. Predicted spectrum vs measured spectrum R² > 0.7
2. Contribution scores match engineering intuition
3. Systematic room deviation matches known AJ's bass-heavy character
4. Confidence scores are sane for recommendations that would have fired

**Decision point:** If R² > 0.7 and confidence scoring looks correct → activate channel-level recommendations for the following show. If R² poor → investigate instrument priors and EQ transfer function accuracy first.

---

### IMP-042 — Phase 3: Two-Mic Geometry System
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 3  
**Status:** 📋 Designed — do not implement before June 13 validation  
**Source:** Neighbor suggestion — boundary mic + geometry calculations

After AT2035 single-mic baseline is validated (IMP-041), add a boundary/PZM mic on the back wall using channel 2 of the PreSonus Studio 26c.

**What two mics enable:**
- Room mode prediction from geometry: `room_mode_hz = 343 / (2 × room_dimension_m)`
- Coverage verification: >6dB level drop FOH→back wall in mid/high = coverage problem, not mix
- Time-of-flight room characterization: systematic differences after accounting for ~40ms travel time = room absorption profile
- Two-point room transfer function: separates direct sound (FOH mic) from reverberant field (back wall mic)

---

### IMP-043 — RTA Investigation Engine: Targeted Channel Spectrum Scanning
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed — 2026-05-26  
**Source:** OSC protocol review — `/-action/setrtasrc` enables remote RTA source switching

The X32 RTA (`/meters/15`, 100 bands, 20Hz–18.66kHz) can be pointed at any channel via `/-action/setrtasrc ,i <int>`. This enables targeted investigation without continuous per-channel scanning.

**OSC primitives:**
```
/-action/setrtasrc ,i <int>   # switch RTA source
  0–31:   Ch 01–32
  70:     Main L/R           ← default always-on position
/-prefs/rta/pos ,i 1          # Post-EQ — always use for FOH Assistant
/meters/15                    # 100-band spectrum blob, 50ms updates
```

**RTA state machine:**
```
MAIN_BUS      ← default; /meters/15 runs continuously on Main L/R
INVESTIGATING ← Tier 2 reactive scan (preempts CALIBRATING)
CALIBRATING   ← cal scan (IMP-045, user-triggered)

Watchdog: if stuck in INVESTIGATING or CALIBRATING > 8s → force MAIN_BUS + log error
```

**Startup:** `setrtasrc 70` (Main L/R) + `rta/pos 1` (post-EQ). Subscribe to `/meters/15` with continuous renewal.

---

#### IMP-043a — Tier 1: Main Bus Continuous Monitoring

Always running. Rolling 2-second average per band (40 samples at 50ms). Trigger Tier 2 when any band deviates from genre target by more than `recommendation_trigger_db` (3.0dB default) for more than `stability_window_s` (4.0s default).

Suppressed when: LUFS below `silence_threshold_db`; main bus level changing faster than 2dB/s (intentional engineer move); Tier 2 cooldown active (30s minimum between investigations on same band).

---

#### IMP-043b — Tier 2: Reactive Channel Investigation

Triggered by Tier 1 breach. Candidate ranking: fingerprint overlap × contribution score × (rms_db + 90). Top 5 candidates investigated in order, 150ms per channel (3 × 50ms updates). Exit loop on culprit found. Return to Main L/R immediately after.

**Culprit threshold:** 2.0dB deviation from model prediction (tighter than the 3.0dB genre trigger — problem already confirmed, now finding who's responsible).

**Tablet disruption:** <1 second total. Acceptable at show. Minimum 30s between Tier 2 investigations on the same band.

Log entry per investigation: trigger band, direction, deviation, candidates scanned, culprit identified, culprit actual vs expected dB, scan duration.

---

#### IMP-043c — Tier 3: Automated Background Calibration

**Deferred to Phase 3.** Automated sweep between songs on a timer. User-triggered version (IMP-045) ships in Phase 2 instead.

---

### IMP-044 — Deficiency Response: Proportional Multi-Channel Boost Logic
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed — 2026-05-26  
**Source:** Architecture discussion — deficiency requires different logic than buildup; boosting multiple channels simultaneously is a feedback and balance risk

---

#### IMP-044a — Cause Classification

Three causes with different correct responses — classify before any recommendation fires:

```python
def classify_deficiency(band, shortfall_db, lufs_deviation):
    if lufs_deviation < -LUFS_TRIGGER_DB:
        return 'overall_level'        # Cause A: everything is just low
    if room_profile.is_known_absorption_band(band):
        return 'room_absorption'      # Cause C: known room behavior, not a mix problem
    return 'channel_shortfall'        # Cause B: specific channels under-contributing
```

**Cause A:** Single recommendation only — "Overall level low — raise master or lift all active channel faders proportionally." No per-channel investigation, no Tier 2 scan.

**Cause C:** Informational only, not displayed unless engineer requests via `g` command. "Mid-band deficiency consistent with [venue] room profile — room absorption. Not a mix problem."

**Cause B:** Proceed to IMP-044b.

---

#### IMP-044b — Per-Channel Shortfall Calculation

Run Tier 2 RTA investigation with `direction='deficiency'`. For each candidate, calculate:

```python
boost_db = min(channel_shortfall, MAX_BOOST_DB, genre_shortfall_db)
boost_db = round(boost_db * 2) / 2   # nearest 0.5dB

# Prefer fader over EQ for pure level corrections
if fader_headroom >= boost_db and not (rms_ok and channel_shortfall > 2.0):
    action = 'fader'
else:
    action = 'eq_boost'   # RMS adequate but frequency content low → shape the channel
```

**Constants:** `MAX_BOOST_DB = 3.0`, `MIN_BOOST_DB = 1.0` (sub-perceptual — skip if below).

**Fader vs EQ decision:**

| Situation | Action | Reason |
|---|---|---|
| Channel RMS low, fader has headroom | Raise fader | Pure level correction |
| Channel RMS adequate, band frequency content low | EQ boost, wide Q | Instrument not producing that frequency |
| Channel at fader ceiling (+10dB) | EQ boost, wide Q | No fader room |
| Multiple channels need same band boost | Fader on each | Maintain relative balance |

---

#### IMP-044c — Sequenced Output Format

Never output all deficiency recommendations simultaneously. Display as a ranked, explicitly sequenced list:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⬇  HIGH-MID DEFICIENCY — 3.2dB below target
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Channels under-contributing in 2kHz–6kHz:

  1. Guitar 1    → raise fader +2.5dB      [shortfall: 3.1dB]
  2. Keys        → raise fader +1.5dB      [shortfall: 2.0dB]  ← after #1
  3. Lead Vocal  → EQ boost +1.0dB @3kHz  [shortfall: 1.2dB]  ← if still needed

Apply in order. Re-evaluate after each step before proceeding.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Cooldown after deficiency output: 45s per band (shorter than buildup 60s — deficiency fixes are often iterative).

---

### IMP-045 — Live Calibration Scan: `cal` Keyboard Command
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed — 2026-05-26  
**Source:** Architecture discussion — automated periodic calibration is inappropriate during songs; user triggers during a stable musical passage

**Command:** `cal` (typed during show mode, same as `n`, `p`, `s`, `g`, `b`, `a`)

Compares each channel's actual RTA spectrum against the model's prediction. Computes per-band deviations. Applies damped update to instrument priors. Total scan time: 3–5 seconds.

---

#### IMP-045a — Preconditions

```python
def cal_preconditions_met(channels, lufs_db):
    active_channels = [ch for ch in channels if ch.rms_db > CHANNEL_ACTIVE_THRESHOLD]
    if lufs_db < SILENCE_THRESHOLD_DB:
        return False, "CAL: band not playing — trigger during a verse or chorus"
    if len(active_channels) < 4:
        return False, f"CAL: only {len(active_channels)} active channels — need 4+ for meaningful calibration"
    if rta_state != MAIN_BUS:
        return False, "CAL: RTA investigation in progress — try again in a moment"
    return True, None
```

---

#### IMP-045b — Scan Loop

For each active channel in priority order (vocal, guitars, keys, bass, drums):
1. `setrtasrc` → channel post-EQ
2. Collect 4 updates (200ms); discard first (settling), average remaining 3
3. Compare per-band actual vs model prediction
4. Store deviation per channel per band

Total: ~14 channels × 200ms = ~2.8 seconds. Return to Main L/R immediately after.

---

#### IMP-045c — Prior Update

```python
ALPHA = 0.1   # 10% step per scan — conservative to avoid over-fitting one song

def update_priors(cal_results):
    for r in cal_results:
        for band, data in r['bands'].items():
            deviation = data['deviation']
            if abs(deviation) < 0.5:
                continue   # sub-threshold — skip
            old = instrument_priors[r['channel'].instrument_type][band]
            new = old + (ALPHA * deviation)
            instrument_priors[r['channel'].instrument_type][band] = new
```

**Prior update floor:** Skip if update would move prior by less than 0.05dB. Priors persisted to `config/instrument_priors_<venue>.yaml`. After 10 consistent scans the prior converges fully; after 3–4 it's most of the way there.

---

#### IMP-045d — Terminal Output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAL SCAN — 21:34:15 — 14 channels — 2.8s
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Channel         Band       Predicted  Actual   Dev
Guitar 1        high_mid   -12.0dB   -13.2dB  -1.2dB  ✓
Guitar 2        high_mid   -14.0dB   -11.1dB  +2.9dB  ⚠
Keys            mid        -15.0dB   -18.3dB  -3.3dB  ✗
Lead Vocal      mid        -10.0dB    -9.8dB  -0.2dB  ✓
Bass DI         bass       -11.0dB   -10.4dB  +0.6dB  ✓
...

PRIOR UPDATES (α=0.1):
  Keys        mid:      -1.50 → -1.83dB
  Guitar 2    high_mid: +0.80 → +1.09dB

✓ good (<1.5dB)  ⚠ notable (1.5–3.0dB)  ✗ significant (>3.0dB)
Logged: shows/2026-05-26_cal.json
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Recommended practice:** Run `cal` once during soundcheck (any stable full-band passage) and once during the first full-band song of the show. After that, as-needed when contribution scores feel wrong.

**Feedback loop with IMP-044:** Cal scan corrects priors → better contribution scores → deficiency attribution becomes accurate → recommendations target the right channels.

---

### IMP-046 — Spectral Shape vs LUFS Separation
**Priority:** `[CRITICAL]`  
**Phase Target:** Phase 2 — before marina show  
**Status:** 📋 Designed — 2026-05-26  
**Source:** Architecture discussion — LUFS is position-dependent and should never drive mix recommendations; spectral shape is position-independent and is the mic's actual useful output

The reference microphone serves as a **spectrometer**, not a loudness meter. Its job is to report what frequencies are present in what proportions — the tonal shape of what the audience is actually hearing at the listening position.

**The correct hierarchy:**
- **Genre target curve** — what we want the audience to hear. The goal.
- **Room mic normalized shape** — what the audience is actually hearing. The measurement of reality. The only signal that drives recommendations.
- **Board RTA** — what the board is outputting before the room does anything to it. Context and diagnostic tool, not a goal.

The board RTA is not the target. The board is a tool used to achieve the target at the listening position. A board curve that perfectly matches the genre target is meaningless if the room is adding 6dB of low-mid buildup or absorbing 4dB of high-mid. The engineer needs to compensate on the board for the room's character so the audience hears the target. The mic is the feedback loop that makes that possible.

This is exactly the value of the system over saved X32 presets. A venue preset captures "the EQ that worked last time in this room." The mic tells you whether tonight's room matches that — different crowd density, temperature, PA configuration, or ambient conditions all shift the acoustic response. The preset is a starting point. The mic is the truth.

**The fundamental problem with the old design:** LUFS was used to drive mix level recommendations. A mic placed 5 feet closer to the PA reads 3dB higher LUFS — that is not a mix problem. This was wrong and should never have been the design.

**What changes:**

---

#### 46a — Normalize Spectrum to Shape Before Comparison

All spectral comparisons against genre targets and forward model predictions use **normalized spectrum** — the smoothed mic FFT with its mean dB level subtracted across the displayed frequency range. This removes the positional level scalar entirely and compares shape only.

```python
def normalize_to_shape(spectrum_db: np.ndarray,
                        freq_mask: np.ndarray = None) -> np.ndarray:
    """
    Subtract mean level to produce a shape-only representation.
    freq_mask: optional boolean array to restrict the mean calculation
               to a specific frequency range (e.g. 80Hz–16kHz, excluding
               sub and air extremes which may be noisy).
    
    Result: spectrum where 0dB = average energy level.
    A band at +3dB means 3dB above the mix's own average — a real tonal imbalance.
    A band at -2dB means 2dB below average — potentially thin in that region.
    This is independent of whether the mic reads -20dBFS or -40dBFS overall.
    """
    if freq_mask is not None:
        mean_db = np.mean(spectrum_db[freq_mask])
    else:
        mean_db = np.mean(spectrum_db)
    return spectrum_db - mean_db
```

Add `normalized_spectrum_db` to `MicAnalysis` dataclass alongside `smoothed_spectrum_db`. All recommendation engine comparisons use `normalized_spectrum_db`. Forward model logging retains `smoothed_spectrum_db` for room transfer function accumulation (which does need absolute level).

---

#### 46b — LUFS Role Restricted to Silence Gate and Logging

LUFS has exactly two remaining functions:

1. **Silence gate** — LUFS below `ROOM_SILENCE_THRESHOLD_LUFS` (-50dB) means band is not playing. Suppress all recommendations. This is a binary "is there sound" check, not a level recommendation.

2. **Session logging** — LUFS logged every cycle for post-show data. Useful for reviewing show dynamics, set break detection, and eventual audience-facing metrics. Not used for real-time recommendations.

Remove from recommendation engine:
- LUFS too high → "reduce master fader" recommendations
- LUFS too low → "raise master fader" recommendations (these are now Cause A deficiency, IMP-044a)

The Cause A deficiency classification (IMP-044a) handles "overall level low" correctly — it detects this from the board's own data (multiple channels reading below expected levels) not from the mic's absolute LUFS reading.

---

#### 46c — Outdoor Venues: LUFS Recommendations Explicitly Disabled

Add to `OpenAirAcoustics` and venue YAML schema:

```python
class OpenAirAcoustics(VenueAcoustics):
    
    @property
    def lufs_recommendations_enabled(self) -> bool:
        return False   # outdoor — position dependency too high
    
    @property
    def spectral_shape_analysis_enabled(self) -> bool:
        return True    # always active
```

Print at outdoor session start:
```
OUTDOOR VENUE — marina_outdoor
  Spectral shape analysis: ACTIVE
  LUFS level recommendations: DISABLED (outdoor — position dependent)
  LUFS data logged for post-show analysis only.
```

Indoor venues: `lufs_recommendations_enabled` defaults to `False` unless `mic_position_calibrated = true` in venue profile (see IMP-047).

---

#### 46d — Genre Target Curves Also Normalized to Shape

Genre target YAML curves are stored as relative shape values, not absolute dBFS levels. They already are in practice (the targets are deviations from flat, not absolute levels) but this should be made explicit in the data model and any comparison code.

```yaml
# Example: glam_metal.yaml target curve
# Values are dB relative to mix average (0dB = flat/neutral)
# Positive = more energy desired in this band
target_shape:
  sub:        -2.0    # tight, not boomy
  bass:       +1.5    # full but controlled
  low_mid:    -1.0    # scooped slightly
  mid:        -0.5    
  high_mid:   +2.0    # presence and cut
  upper_mid:  +1.5    # guitar bite
  presence:   +1.0    # air and sparkle
  air:        +0.0
```

Deviation from target = `normalized_mic_shape[band] - target_shape[band]`. This is what the recommendation engine compares. It is fully position-independent.

---

### IMP-047 — Mic Position Calibration Flag
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2 — before marina show  
**Status:** 📋 Designed — 2026-05-26  
**Source:** Architecture discussion — if an engineer does calibrate mic position deliberately, LUFS can be a valid advisory signal; the system should support this explicitly rather than silently being wrong

For engineers who want LUFS-based level recommendations, the system supports an explicit calibration step that marks the mic position as intentionally set and records a reference level. Without this step, LUFS recommendations never fire.

---

#### 47a — Venue Profile Calibration Fields

```yaml
reference_mic:
  position_x_m: 4.0
  position_y_m: 6.5
  height_m: 1.5
  mic_position_calibrated: false       # set true after cal step below
  calibrated_reference_lufs: null      # filled by calibration step
  calibrated_reference_dbfs: null      # input level at calibration
  calibration_notes: ""                # e.g. "center of room, ear height, 20ft from PA"
```

---

#### 47b — Calibration Step (`cal-mic` command or `--calibrate-mic` flag)

During soundcheck, when the mix is dialed in and confirmed good:

```
cal-mic + Enter

MIC POSITION CALIBRATION
  Current mic reading: -22.4 LUFS  (-18.3dBFS peak)
  This will be saved as the reference level for LUFS recommendations.
  Mix must be dialed in and confirmed good before running this.

  Confirm? [y/n]: y

  Reference LUFS: -22.4 saved to config/venues/marina_outdoor.yaml
  LUFS recommendations now active for this session.
  Threshold: ±3.0dB from reference before recommendation fires.
```

Once calibrated, LUFS recommendations fire with appropriate framing:
```
LEVEL — 3.1dB above calibrated reference
  Mix level elevated vs soundcheck position.
  If PA level is correct, consider: mic may have shifted, or room fill has increased.
  (Not necessarily a mix problem — verify with your ears.)
```

Note the explicit "not necessarily a mix problem" framing. Even when calibrated, LUFS level is advisory with lower confidence than spectral recommendations. It always defers to ears.

---

#### 47c — Phase 3 UI: LUFS Indicator vs Spectral Display

In the Phase 3 UI, LUFS is shown as a separate numeric indicator (a loudness meter bar, essentially) — not as part of the three-curve spectral overlay. The spectral display is always normalized shape. LUFS is a separate panel that's visually distinct from the frequency analysis.

This reinforces the separation architecturally and visually: the spectral curves tell you about tonal balance, the LUFS meter tells you about level, and they are not the same thing.

---

### IMP-048 — Phase 3 UI: Three-Curve Spectral Overlay Display
**Priority:** `[HIGH]`  
**Phase Target:** Phase 3  
**Status:** 📋 Designed — 2026-05-26  
**Source:** Architecture discussion — visual correlation of board output, room measurement, and genre target is the most actionable display an engineer can have

A real-time spectral overlay panel showing three normalized curves simultaneously. This is the primary display the engineer watches during a show.

---

#### 48a — The Three Primary Curves

| Curve | Source | Color | Update Rate |
|---|---|---|---|
| Board RTA | `/meters/15` main bus, normalized | White / light gray | 50ms (display EMA α=0.15) |
| Room Mic | AT2035 FFT, geometry-corrected, normalized | Amber / orange | 500ms analysis cycle |
| Genre Target | Active genre profile shape curve | Cyan / green | On song change |

All three curves displayed on the same log-frequency axis, 20Hz–20kHz. Y-axis: ±12dB relative to normalized mean. Zero line = neutral/flat.

**What the visual relationships mean** (shown as a legend or tooltip):

| Relationship | Meaning | Action |
|---|---|---|
| Mic above Target | Audience hearing too much here | Cut on board to compensate |
| Mic below Target | Audience not hearing enough here | Boost on board to compensate |
| Mic above Target, Board on Target | Room adding energy (mode, reflection, boundary) | Still cut on board — room is the cause but board is the fix |
| Mic on Target, Board below Target | Board compensating correctly for room absorption | No action — this is working as intended |
| Mic on Target, Board above Target | Board compensating correctly for room mode | No action — this is working as intended |
| Mic and Board both above Target | Board output + room both contributing excess | Cut on board, likely significant |
| All three converge | Mix and room on target | Nothing to do |

**The critical insight:** The board curve deviating from the target curve is expected and correct when the room requires compensation. An engineer using saved venue presets has already done this compensation manually. The system's job is to help achieve and maintain the target at the mic position — not at the board output. The board curve is shown to explain *why* the mic reads the way it does, not to judge whether the mix is correct.

---

#### 48b — Fourth Curve (Toggle): Forward Model Prediction

| Curve | Source | Color | Display |
|---|---|---|---|
| Model Prediction | Sum of channel contributions, normalized | Purple / violet | Toggle — default off during show |

Shows the mathematically calculated board output from EQ transfer functions. Gap between this and Board RTA = forward model error. Useful during calibration and validation sessions, hidden during normal show operation to reduce visual noise.

Toggle via keyboard shortcut (`m` for model) or UI button.

---

#### 48c — Vertical Band Highlight System

Highlights sit as a background layer behind the three curves. Driven entirely by **mic normalized shape deviation from genre target** — the only comparison that matters for recommendations. Board RTA is never used to gate or color highlights.

**Color scheme — excess (mic above target):**

| Deviation | Color | Meaning |
|---|---|---|
| < ±1.5dB | No highlight | Within tolerance |
| +1.5 to +3.0dB | Yellow / amber | Notable — watch it |
| +3.0 to +5.0dB | Orange | Recommendation territory |
| > +5.0dB | Red | Significant — recommendation firing or imminent |

**Color scheme — deficiency (mic below target):**

| Deviation | Color | Meaning |
|---|---|---|
| -1.5 to -3.0dB | Light blue | Notable deficiency |
| -3.0 to -5.0dB | Blue | Recommendation territory |
| < -5.0dB | Deep blue / indigo | Significant deficiency |

Warm colors (excess) and cool colors (deficiency) are visually distinct at a glance — the engineer reads "cut something here" vs "add something here" without reading any text. Critical for a display that needs to be readable from across a console in a dim venue.

**Highlight spans the full band width** — not a thin line at the peak frequency. The entire high-mid region (2kHz–4kHz) glows orange if that band is hot. Readable from across the room.

**Diagnostic overlay on highlight:** When the board RTA and mic diverge significantly within a highlighted band, a small indicator inside the highlight shows the cause:
- `R` (room) — board is near target but mic is off → room acoustics causing the problem, board compensation needed
- `B` (board) — board and mic are both off together → board is the primary cause
- `✓` — board already compensating (board opposite of mic deviation) → working correctly, monitor only

This gives the engineer immediate context on what's driving the problem without requiring them to read the curves in detail.

---

#### 48d — Confidence De-emphasis

Frequency regions where model confidence is low (comb filter notches, room modes, sub-noise-floor frequencies) shown with reduced opacity or dashed line styling. Engineer learns not to read those regions as reliable data.

---

#### 48e — LUFS Panel (Separate from Spectral Display)

Separate panel, visually distinct from the three-curve overlay:
- Large numeric LUFS readout
- Rolling 10-second bar graph
- Reference level marker (if `mic_position_calibrated = true`)
- Deviation from reference shown in color (green = within ±2dB, yellow = ±2–4dB, red = >4dB)
- If not calibrated: "Level display only — not used for recommendations"

This makes the architectural separation between shape analysis and level monitoring visually explicit to the engineer.

---

### IMP-049 — Soundcheck Channel Isolation Sampling (`iso` command)
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2–3  
**Status:** 📋 Designed — 2026-05-26  
**Source:** Architecture discussion — isolated channel measurement during soundcheck gives clean ground-truth prior data that `cal` scans during a show cannot match

During soundcheck, an engineer can bring up one channel with everything else muted or gated. The `iso` command captures a clean measurement of that channel's actual spectral character and compares it directly against the current instrument prior — bypassing the forward model's mathematical separation entirely. This is the fastest path to accurate priors.

---

#### 49a — The Isolation Advantage

During a show, the `cal` command (IMP-045) must separate each channel's contribution from the full mix mathematically — the mic hears everything simultaneously. The separation relies on the contribution model being reasonably accurate already. This means early `cal` scans have modest impact and convergence is slow.

During soundcheck isolation, the mic hears one channel with the room's response applied. That reading is directly attributable to that instrument in this room with this PA. No model separation required. The deviation between the current prior and the isolated measurement is the exact correction needed.

After a thorough soundcheck isolation session across all primary instruments, the priors can be corrected to this specific band's gear in this specific room in one session — equivalent to 5–6 shows worth of `cal` scan convergence.

---

#### 49b — Command

```
iso     ← prompted channel selection
iso 9   ← direct channel number
```

Available in soundcheck mode. Also available in show mode when RTA is in MAIN_BUS state (opportunistic use between songs or during a set break).

---

#### 49c — Isolation Sampling Flow

```python
async def run_isolation_sample(channel_num: int,
                                rta_engine: RTAEngine,
                                osc_client,
                                mic_analyzer: MicAnalyzer,
                                forward_model,
                                duration_s: float = 12.0) -> dict:
    """
    Capture isolated channel measurement.
    Engineer plays instrument for duration_s seconds.
    Returns deviation between actual and prior, suggested update.
    """
    ch = osc_client.channel_configs[channel_num]

    print(f"\nISO: {ch.label} ({ch.instrument_type})")
    print(f"  Mute all other channels or ensure they are below gate threshold.")
    print(f"  Play a representative {duration_s:.0f}-second passage when ready.")
    print(f"  Press Enter to begin capture...")
    await wait_for_enter()

    print(f"  Capturing {duration_s:.0f}s... ", end='', flush=True)

    rta_idx = channel_to_rta_index(channel_num, post_eq=True)
    rta_engine.start_cal_scan(rta_idx)

    board_readings = []
    mic_readings   = []

    sample_count = int(duration_s / 0.2)   # 200ms per sample
    for i in range(sample_count):
        board_readings.append(await osc_client.get_meters_15())
        mic_readings.append(mic_analyzer.get_current_normalized_shape())
        await asyncio.sleep(0.2)
        if (i + 1) % 5 == 0:
            print(".", end='', flush=True)

    print(" done")
    rta_engine.set_main_bus()

    # Discard first 2 readings (settling), average the rest
    board_shape = normalize_to_shape(np.mean(board_readings[2:], axis=0))
    mic_shape   = normalize_to_shape(np.mean(mic_readings[2:],   axis=0))
    prior_curve = forward_model.get_prior(ch.instrument_type, 'normal')

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

    return {
        'channel': ch.label,
        'channel_num': channel_num,
        'instrument_type': ch.instrument_type,
        'board_shape': board_shape,
        'mic_shape': mic_shape,
        'prior_curve': prior_curve,
        'board_vs_prior': board_vs_prior,
        'mic_vs_prior': mic_vs_prior,
        'duration_s': duration_s,
    }
```

---

#### 49d — Terminal Output and Confirmation

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ISO SAMPLE — Guitar 1 (electric_guitar) — 12s
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                  Board vs Prior    Mic vs Prior
  sub                  -0.3dB          -0.5dB    ✓
  bass                 +0.8dB          +1.2dB    ✓
  low_mid              -1.1dB          -0.9dB    ✓
  mid                  +0.4dB          +0.6dB    ✓
  high_mid             +3.2dB          +2.8dB    ⚠  prior under-predicts
  upper_mid            +1.8dB          +1.4dB    ⚠
  presence             -0.6dB          -1.2dB    ✓
  air                  -1.4dB          -2.1dB    ✓

Room delta (mic − board): high_mid −0.4dB, air −0.7dB
  (room absorbing slightly at high frequencies — normal for this venue)

Prior update available:
  high_mid:   +0.80 → +3.68dB  (α=0.6 step from isolation sample)
  upper_mid:  +0.20 → +1.76dB

Apply prior updates? [y / n / partial]:
```

Three options:
- `y` — apply all significant deviations (>1.5dB) immediately
- `n` — log the data, don't update priors (useful if passage wasn't representative)
- `partial` — walks through each band individually for selective acceptance

**Update rate:** Isolation samples use α=0.6 (vs α=0.1 for `cal` scans) because the measurement is clean and trustworthy. Still not a full replacement in one pass — 60% step leaves room for non-representative playing to not overcorrect.

---

#### 49e — Room Delta as Venue Data

The difference between board shape and mic shape during isolation is a clean per-instrument room response for this frequency range:

```
room_delta[instrument][band] = mic_shape[band] - board_shape[band]
```

This is more precise than full-mix room transfer function accumulation (IMP-038) because there's no source mixing ambiguity. Logged to `config/venues/<venue_id>_iso_samples.json` and used to seed the venue room transfer function without waiting for multiple shows.

---

#### 49f — Soundcheck Integration

Recommended soundcheck workflow with isolation sampling:
1. Dial in each channel individually as normal
2. Run `iso` on the 4–5 primary instruments (kick, guitar 1, guitar 2, bass, lead vocal)
3. `confirm` to lock baseline

Total additional time: ~3 minutes. Prior accuracy payoff: equivalent to 5+ shows of `cal` convergence.

Print at soundcheck startup:
```
  iso <n>    → isolation sample channel N (solo that channel, mute others first)
```

---

### IMP-050 — Prior Convergence Tracking and Confidence Display
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 3  
**Status:** 📋 Designed — 2026-05-26  
**Source:** Architecture discussion — the system should know how confident it is in each instrument prior and communicate that to the engineer; gates fourth curve display

The system tracks how many isolation samples and `cal` scans have contributed to each instrument prior, and how stable those measurements have been. This convergence score gates the fourth curve (model prediction) display and informs how much weight to give model-based recommendations.

---

#### 50a — Prior Confidence Score Per Instrument Per Band

```python
@dataclass
class PriorConfidence:
    instrument_type: str
    band: str

    iso_sample_count: int  = 0       # isolation samples contributing
    cal_scan_count:   int  = 0       # cal scans contributing
    variance_db:      float = 99.0   # std dev across measurements — high = unreliable
    last_updated:     float = 0.0    # unix timestamp

    @property
    def confidence_score(self) -> float:
        """
        0.0 = no data / completely uncertain
        1.0 = well-sampled, low variance, recently confirmed

        Isolation samples count 3× a cal scan (cleaner measurement).
        Saturates at 10 effective samples. Variance penalizes noisy priors.
        """
        effective = self.iso_sample_count * 3 + self.cal_scan_count
        sample_score    = min(effective / 10.0, 1.0)
        variance_penalty = max(0.0, 1.0 - self.variance_db / 3.0)
        return sample_score * variance_penalty

    @property
    def ready_for_fourth_curve(self) -> bool:
        return self.confidence_score >= 0.6
```

Fourth curve (model prediction) toggle is disabled until the mean confidence across all active channels exceeds 0.6. When enabled, low-confidence channels are rendered with a dashed contribution line in the fourth curve so the engineer knows which instruments are contributing reliable predictions vs estimates.

---

#### 50b — UI Confidence Indicators (Phase 3)

In the channel map panel, each instrument shows a small confidence bar:

```
Guitar 1    ████░  0.72  (2 iso + 4 cal)
Guitar 2    ██░░░  0.41  (1 iso + 2 cal)
Keys        █░░░░  0.18  (1 cal only)
Lead Vocal  ░░░░░  0.04  (default prior — uncalibrated)
Kick        ████░  0.80  (3 iso samples)
```

---

#### 50c — Cross-Show Persistence

Prior confidence data persists across shows in `config/instrument_priors_<venue>.yaml` alongside the prior values. At the same venue, measurements accumulate across shows — the system gets more accurate every time. At a new venue, confidence resets to zero and the first soundcheck isolation session rebuilds it quickly.

---



---

### IMP-D01 — AI/ML Learning Layer
**Priority:** `[FUTURE]`  
**Phase Target:** Phase 5  
**Status:** 💬 Discussed  
**Source:** May 12 planning session

Small local model trained on show log data to learn from engineer correction patterns. Separate from the deterministic physics layer (IMP-034–039) — the ML layer runs on top of an accurate deterministic foundation, not instead of it.

**Design dependencies:** Forward mix model (IMP-034–039) must be validated (IMP-041) before ML training is meaningful. Training data schema needs to be defined and logged starting June 13.

**Planned architecture:** Local inference on laptop. Two phases — (1) small model learning from this band's show history, (2) LLM (Claude API) conversational layer that the engineer can talk to about mix decisions, backed by the internal model's real-time state.

---

### IMP-D02 — Show Replay Simulator
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2–3  
**Status:** 📋 Designed — Claude Code prompt written (`CLAUDE_CODE_SHOW_REPLAY_PROMPT.md`)  
**Source:** May 12 planning session — needed to validate forward model against Show 1 log

`tools/show_replay.py` replays a show JSON at configurable speed. Two mic modes:
- `derived` — mic spectrum mathematically derived from board state (R² target >0.90 — confirms model math is correct)
- `noise-injected` — mic spectrum derived + realistic noise (R² target 0.65–0.75 — confirms model is realistic)

Port map: 10023/10024 X32 OSC (existing), 19876 mic injection (UDP), 19877 board RTA broadcast (UDP).

---

### IMP-D03 — Solo Preset Commands
**Priority:** `[HIGH]`  
**Phase Target:** Phase 3.5  
**Status:** 📋 Designed  
**Source:** Real-world insight — engineer needs one-keypress solo management without full automation

Keys `1`, `2`, `3` trigger pre-configured relative fader adjustments for Guitar 1 solo, Guitar 2 solo, Keys solo. Hold for configurable duration, then ramp back automatically over 2–3 seconds. Key `0` = emergency restore. Safety rail: no channel moves more than ±3dB from soundcheck baseline in one command. **Requires OSC write access — Phase 4 unlock.**

---

### IMP-D04 — Automated Solo Mix
**Priority:** `[FUTURE]`  
**Phase Target:** Phase 4+  
**Status:** 💬 Discussed  
**Source:** Extension of IMP-D03

When RMS spike + fader rate-of-change triggers on a guitar channel, apply solo preset automatically. System learns solo timestamps per song across shows. Pre-loads solo mix before the boost pedal click. Engineer becomes safety net rather than primary operator.

---

### IMP-D05 — Baseline Snapshot Suppression of Static Deviations
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Static false positives in show mode

After soundcheck baseline captured, suppress frequency band recommendations for deviations already present and accepted at soundcheck (within 1dB of same deviation). Deviations that develop during the show still fire.

---

### IMP-D06 — Reference Audio Targeting
**Priority:** `[FUTURE]`  
**Phase Target:** Phase 6  
**Status:** 💬 Discussed  
**Source:** Scope doc Phase 6

Per-song reference audio files analyzed locally to extract precise frequency targets. Replaces genre template when reference is available. Cover band delta tracking after multiple shows. Reference audio never transmitted or reproduced — local analysis only.

---

### IMP-D07 — miniDSP UMIK-2 Calibration File Support
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 3  
**Status:** 📋 Designed  
**Source:** Hardware recommendation — UMIK-2 includes individual calibration file

Load `.cal` file at startup, apply correction curve to FFT output per frequency bin. Makes frequency band readings accurate rather than just relative. Also enables Room EQ Wizard (REW) integration for pre-show room capture.

---

### IMP-D08 — Funk/R&B Genre Profile
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Identified (see IMP-033)  
**Source:** Setlist review

Add `funk_rock.yaml` after reviewing June 13 show log data. Songs: Superstition, Play That Funky Music, Brick House, Cult of Personality.

---

### IMP-D09 — Post-Show Report as Forum Content
**Priority:** `[LOW]`  
**Phase Target:** Phase 2+  
**Status:** 📋 Designed  
**Source:** Market strategy

Export sanitized post-show report suitable for r/livesound and Gearspace sharing. Remove venue-specific details, highlight recommendation patterns and accuracy metrics.

---

## Implemented — Closed Items

| ID | Description | Phase | Date |
|---|---|---|---|
| IMP-001 | Silence guard on recommendation engine | 1 | 2026-05-05 |
| IMP-002 | Channel RMS guard in culprit attribution | 1 | 2026-05-05 |
| IMP-003 | Global LUFS recommendation cooldown | 1 | 2026-05-05 |
| IMP-004 | EQ band selection by proximity | 1 | 2026-05-05 |
| IMP-005 | OSC client ephemeral port trap | 1 | 2026-05-05 |
| IMP-006 | Simulator push broadcast missing | 1 | 2026-05-05 |
| IMP-007 | OSC client poll fallback for stale state | 1 | 2026-05-05 |
| IMP-008 | Rate-of-change suppression sliding window | 1 | 2026-05-05 |
| IMP-009 | RMS rate-of-change as solo trigger | 1 | 2026-05-05 |
| IMP-010 | Recommendation fader attribution fix | 1 | 2026-05-05 |
| IMP-011 | Deviation stability guard | 1 | 2026-05-05 |
| IMP-019 | Manual song markers with per-song log segments | 1 | 2026-05-06 |
| IMP-020 | Transition grace cancels immediately on song start | 1 | 2026-05-06 |
| IMP-021 | Soundcheck mode (`--soundcheck`) | 1 | 2026-05-06 |
| IMP-022 | HPF state and input gain from X32 (advisory suppressed — see IMP-026) | 1 | 2026-05-06 |
| IMP-023 | Full parametric EQ advisory | 1 | 2026-05-06 |
| IMP-025 | Frequency fingerprint corrections | 1 | 2026-05-06 |

---

## Superseded Items (Do Not Implement)

| ID | Description | Reason Superseded |
|---|---|---|
| *(original IMP-020)* | Analyzer architecture: OSC meters drive channel recommendations | Replaced by IMP-034–039 forward mix model — more rigorous approach |
| *(original IMP-021–024 from project knowledge)* | FFT analyzer: Welch's method, peak detection, smoothing, A-weighting | Room mic is no longer primary per-channel source; these improvements apply only to LUFS/overall monitoring, scope reduced |

---

*This document is reviewed and merged into the scope doc at the end of each phase.*  
*Supersedes `FOH_Assistant_Design_Improvements.md` (all prior versions).*
### IMP-051 — Live Spectrum Display Window (`--display` flag)
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2 — June 13 show  
**Status:** 📋 Designed — 2026-06-02  
**Source:** Architecture discussion — visual correlation of the three curves during a show enables the engineer to see what the analysis engine is seeing in real time

A lightweight popup window alongside the terminal, launched with `--display`. Uses matplotlib `FuncAnimation` in a dedicated thread — zero impact on the analysis loop when not enabled. The window is not the Phase 3 UI; it is a diagnostic visualization tool for Phase 2. It looks like a tool, not a product — that is acceptable for June 13.

---

#### 51a — Architecture: Thread-Safe Display Buffer

The analysis loop (main thread) writes to a shared buffer. The display thread reads from it. No other shared state.

```python
# core/display_buffer.py

import threading
import numpy as np
from dataclasses import dataclass, field

@dataclass
class DisplayBuffer:
    """
    Thread-safe shared state between the analysis loop and the display window.
    Main thread writes via update(). Display thread reads via snapshot().
    """
    # Spectra — all on FREQ_AXIS (1000 points, 20Hz–20kHz), normalized to shape
    board_rta_shape:  np.ndarray = field(default_factory=lambda: np.zeros(1000))
    mic_shape:        np.ndarray = field(default_factory=lambda: np.zeros(1000))
    genre_target:     np.ndarray = field(default_factory=lambda: np.zeros(1000))

    # Fast-path board RTA — updated every 50ms from OSC subscription
    # Separate from the 500ms analysis cycle board_rta_shape
    board_rta_fast:   np.ndarray = field(default_factory=lambda: np.zeros(1000))

    # Band highlights — driven by mic deviation from target
    # band_name → deviation_db (positive = excess, negative = deficiency)
    band_highlights:  dict = field(default_factory=dict)

    # Band peak markers — for named move precision display
    # band_name → (peak_hz, peak_deviation_db)
    band_peaks:       dict = field(default_factory=dict)

    # Metadata
    song_name:   str = ""
    genre_name:  str = ""
    lufs:        float = -60.0
    is_silent:   bool = True

    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, **kwargs) -> None:
        """Write new values. Called from analysis loop (main thread)."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self) -> dict:
        """Read all values atomically. Called from display thread."""
        with self._lock:
            return {
                'board_rta_shape': self.board_rta_shape.copy(),
                'board_rta_fast':  self.board_rta_fast.copy(),
                'mic_shape':       self.mic_shape.copy(),
                'genre_target':    self.genre_target.copy(),
                'band_highlights': dict(self.band_highlights),
                'band_peaks':      dict(self.band_peaks),
                'song_name':       self.song_name,
                'genre_name':      self.genre_name,
                'lufs':            self.lufs,
                'is_silent':       self.is_silent,
            }
```

---

#### 51b — Display Update Points in main.py

```python
# After OSC /meters/15 update (every 50ms — fast path):
board_shape_fast = normalize_to_shape(osc.board_rta_db)
display_buffer.update(board_rta_fast=board_shape_fast)

# After each 500ms analysis cycle:
display_buffer.update(
    board_rta_shape = normalize_to_shape(osc.board_rta_db),
    mic_shape       = mic_result.normalized_shape_db,
    genre_target    = active_genre.target_shape_array,   # pre-computed on song change
    band_highlights = _compute_band_highlights(mic_result, active_genre),
    band_peaks      = _extract_band_peaks(mic_result),
    song_name       = current_song.title if current_song else "",
    genre_name      = active_genre.name if active_genre else "",
    lufs            = mic_result.lufs,
    is_silent       = mic_result.is_silent,
)
```

---

#### 51c — Band Highlight Computation

```python
BAND_RANGES_DISPLAY = {
    'sub':       (20,    80),
    'bass':      (80,    200),
    'low_mid':   (200,   500),
    'mid_low':   (500,   1000),
    'mid_high':  (1000,  2000),
    'upper_mid': (2000,  4000),
    'presence':  (4000,  8000),
    'air':       (8000,  20000),
}

def _compute_band_highlights(mic_result: MicAnalysis,
                               genre: GenreProfile) -> dict:
    """
    Compute per-band deviation of mic normalized shape from genre target shape.
    Returns band_name → deviation_db.
    Positive = excess (warm color), negative = deficiency (cool color).
    Driven by mic vs target only — board RTA never used to compute highlights.
    """
    highlights = {}
    for band, (f_lo, f_hi) in BAND_RANGES_DISPLAY.items():
        mic_avg    = band_average(mic_result.normalized_shape_db, (f_lo, f_hi))
        target_avg = genre.target_shape_db_for_band(band)  # from genre YAML
        highlights[band] = mic_avg - target_avg
    return highlights
```

---

#### 51d — Band Peak Extraction

```python
def _extract_band_peaks(mic_result: MicAnalysis) -> dict:
    """
    Extract peak frequency and deviation within each band from mic analysis.
    Used to place peak markers on the display curves.
    Returns band_name → (peak_hz, peak_db_above_band_mean).
    """
    peaks = {}
    for band, (f_lo, f_hi) in BAND_RANGES_DISPLAY.items():
        if band in mic_result.band_levels:
            level = mic_result.band_levels[band]
            peaks[band] = (level['peak_hz'], level['peak_db'] - level['avg_db'])
    return peaks
```

---

#### 51e — Display Window (`core/display_window.py`)

```python
"""
Live spectrum display window — Phase 2 diagnostic visualization.
Runs in a daemon thread. Launched by --display flag.
Uses matplotlib FuncAnimation at ~10fps.
"""

import threading
import numpy as np
import matplotlib
matplotlib.use('TkAgg')   # Windows-compatible backend
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.animation import FuncAnimation

from core.channel_model import FREQ_AXIS, N_FREQS
from core.display_buffer import DisplayBuffer

# ── Color scheme ──────────────────────────────────────────────────────────
BG_COLOR       = '#0d0d0d'
GRID_COLOR     = '#2a2a2a'
ZERO_COLOR     = '#444444'

COLOR_BOARD    = '#d0d0d0'   # white/light gray — board RTA
COLOR_MIC      = '#e8a020'   # amber/orange    — room mic
COLOR_TARGET   = '#20c0a0'   # cyan/teal       — genre target

# Band highlight colors — excess (warm) / deficiency (cool)
HIGHLIGHT_COLORS = {
    'excess': {
        1.5:  ('#604000', 0.25),   # yellow: 1.5–3.0dB
        3.0:  ('#803800', 0.35),   # orange: 3.0–5.0dB
        5.0:  ('#902020', 0.50),   # red:    >5.0dB
    },
    'deficiency': {
        1.5:  ('#002860', 0.25),   # light blue: 1.5–3.0dB
        3.0:  ('#001880', 0.35),   # blue:       3.0–5.0dB
        5.0:  ('#100060', 0.50),   # indigo:     >5.0dB
    },
}

# Band x-axis positions (Hz boundaries for shading)
BAND_EDGES = {
    'sub':       (20,    80),
    'bass':      (80,    200),
    'low_mid':   (200,   500),
    'mid_low':   (500,   1000),
    'mid_high':  (1000,  2000),
    'upper_mid': (2000,  4000),
    'presence':  (4000,  8000),
    'air':       (8000,  18660),  # /meters/15 max
}

BAND_LABELS = {
    'sub':       'SUB',
    'bass':      'BASS',
    'low_mid':   'L-MID',
    'mid_low':   'MID-L',
    'mid_high':  'MID-H',
    'upper_mid': 'U-MID',
    'presence':  'PRES',
    'air':       'AIR',
}

# Display EMA — slower than analysis EMA, cosmetic smoothing only
DISPLAY_EMA_ALPHA = 0.15


class SpectrumDisplay:
    """
    Live three-curve spectrum display window.
    Instantiate and call start() to launch in a background thread.
    """

    def __init__(self, buffer: DisplayBuffer):
        self._buf = buffer
        self._thread = None
        self._fig = None
        self._anim = None

        # Display-layer EMA state for each curve
        self._ema_board = None
        self._ema_mic   = None

    def start(self) -> None:
        """Launch display in a daemon thread. Returns immediately."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Main display loop — runs on display thread."""
        self._fig, self._ax = plt.subplots(figsize=(12, 6))
        self._fig.patch.set_facecolor(BG_COLOR)
        self._setup_axes()
        self._init_artists()

        self._anim = FuncAnimation(
            self._fig,
            self._update_frame,
            interval=100,    # 10fps
            blit=True,
            cache_frame_data=False,
        )

        plt.tight_layout(pad=1.5)
        plt.show()   # blocks this thread until window is closed

    def _setup_axes(self) -> None:
        ax = self._ax
        ax.set_facecolor(BG_COLOR)
        ax.set_xscale('log')
        ax.set_xlim(20, 18660)
        ax.set_ylim(-14, 14)

        # x-axis ticks at standard octave frequencies
        x_ticks = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
        x_labels = ['31', '63', '125', '250', '500', '1k', '2k', '4k', '8k', '16k']
        ax.set_xticks(x_ticks)
        ax.set_xticklabels(x_labels, color='#888888', fontsize=8)

        # y-axis
        ax.set_yticks([-12, -9, -6, -3, 0, 3, 6, 9, 12])
        ax.yaxis.set_tick_params(labelcolor='#888888', labelsize=8)
        ax.set_ylabel('dB (normalized shape)', color='#666666', fontsize=8)

        # Grid
        ax.grid(True, which='both', color=GRID_COLOR, linewidth=0.5, alpha=0.8)
        ax.axhline(0, color=ZERO_COLOR, linewidth=1.0, zorder=2)

        # Band label annotations at bottom of plot
        for band, (f_lo, f_hi) in BAND_EDGES.items():
            center_log = 10 ** ((np.log10(f_lo) + np.log10(f_hi)) / 2)
            ax.text(center_log, -13.2, BAND_LABELS[band],
                    ha='center', va='bottom', color='#555555',
                    fontsize=6.5, fontfamily='monospace')

        # Title placeholder
        self._title = ax.set_title(
            "FOH Assistant  ·  —  ·  —",
            color='#aaaaaa', fontsize=10, pad=8,
            fontfamily='monospace',
        )

        # LUFS readout — top right
        self._lufs_text = ax.text(
            0.99, 0.97, "LUFS: —",
            transform=ax.transAxes,
            ha='right', va='top',
            color='#666666', fontsize=8, fontfamily='monospace',
        )

        for spine in ax.spines.values():
            spine.set_edgecolor(GRID_COLOR)

    def _init_artists(self) -> None:
        """Create all matplotlib artists. Returns list for blit."""
        ax = self._ax

        # Background band highlight patches (one per band)
        self._highlight_patches = {}
        for band, (f_lo, f_hi) in BAND_EDGES.items():
            patch = mpatches.Rectangle(
                (f_lo, -14), f_hi - f_lo, 28,
                facecolor='none', edgecolor='none',
                alpha=0.0, zorder=1,
            )
            ax.add_patch(patch)
            self._highlight_patches[band] = patch

        # Peak frequency tick marks (small vertical lines inside highlight)
        self._peak_lines = {}
        for band in BAND_EDGES:
            line, = ax.plot([], [], color='#ffffff', linewidth=1.0,
                            alpha=0.4, zorder=4)
            self._peak_lines[band] = line

        # Three main curves
        self._line_board, = ax.plot(
            FREQ_AXIS, np.zeros(N_FREQS),
            color=COLOR_BOARD, linewidth=1.2,
            alpha=0.7, zorder=5, label='Board RTA',
        )
        self._line_mic, = ax.plot(
            FREQ_AXIS, np.zeros(N_FREQS),
            color=COLOR_MIC, linewidth=1.8,
            alpha=0.9, zorder=6, label='Room Mic',
        )
        self._line_target, = ax.plot(
            FREQ_AXIS, np.zeros(N_FREQS),
            color=COLOR_TARGET, linewidth=1.2,
            alpha=0.6, linestyle='--', zorder=7, label='Target',
        )

        # Legend
        ax.legend(
            loc='upper left', fontsize=7,
            facecolor='#1a1a1a', edgecolor=GRID_COLOR,
            labelcolor='#aaaaaa',
        )

        # Collect all artists for blit
        self._all_artists = (
            list(self._highlight_patches.values()) +
            list(self._peak_lines.values()) +
            [self._line_board, self._line_mic, self._line_target,
             self._title, self._lufs_text]
        )

    def _update_frame(self, frame: int) -> list:
        """Called by FuncAnimation every 100ms. Returns updated artists for blit."""
        snap = self._buf.snapshot()

        if snap['is_silent']:
            return self._all_artists   # no update during silence

        # ── EMA smoothing on board (fast path available) ──
        board_raw = snap['board_rta_fast']
        if self._ema_board is None:
            self._ema_board = board_raw.copy()
        else:
            self._ema_board = (DISPLAY_EMA_ALPHA * board_raw +
                               (1 - DISPLAY_EMA_ALPHA) * self._ema_board)

        # ── EMA smoothing on mic ──
        mic_raw = snap['mic_shape']
        if self._ema_mic is None:
            self._ema_mic = mic_raw.copy()
        else:
            self._ema_mic = (DISPLAY_EMA_ALPHA * mic_raw +
                             (1 - DISPLAY_EMA_ALPHA) * self._ema_mic)

        # Clip to display range
        board_disp  = np.clip(self._ema_board, -14, 14)
        mic_disp    = np.clip(self._ema_mic,   -14, 14)
        target_disp = np.clip(snap['genre_target'], -14, 14)

        self._line_board.set_ydata(board_disp)
        self._line_mic.set_ydata(mic_disp)
        self._line_target.set_ydata(target_disp)

        # ── Band highlights ──
        highlights = snap['band_highlights']
        peaks      = snap['band_peaks']

        for band, (f_lo, f_hi) in BAND_EDGES.items():
            dev = highlights.get(band, 0.0)
            patch = self._highlight_patches[band]

            if abs(dev) < 1.5:
                patch.set_alpha(0.0)
            else:
                direction = 'excess' if dev > 0 else 'deficiency'
                color, alpha = self._highlight_color(abs(dev), direction)
                patch.set_facecolor(color)
                patch.set_alpha(alpha)

            # Peak frequency tick mark within band
            peak_line = self._peak_lines[band]
            if band in peaks and abs(dev) >= 1.5:
                peak_hz, _ = peaks[band]
                if f_lo <= peak_hz <= f_hi:
                    peak_line.set_data([peak_hz, peak_hz], [-14, 14])
                    peak_line.set_alpha(0.3)
                else:
                    peak_line.set_data([], [])
            else:
                peak_line.set_data([], [])

        # ── Title and LUFS ──
        song   = snap['song_name'] or '—'
        genre  = snap['genre_name'] or '—'
        self._title.set_text(f"FOH Assistant  ·  {song}  ·  {genre}")
        self._lufs_text.set_text(f"LUFS: {snap['lufs']:.1f}")

        return self._all_artists

    @staticmethod
    def _highlight_color(abs_dev: float, direction: str) -> tuple:
        """Return (facecolor, alpha) for a band highlight."""
        thresholds = HIGHLIGHT_COLORS[direction]
        selected = None
        for threshold in sorted(thresholds.keys()):
            if abs_dev >= threshold:
                selected = thresholds[threshold]
        if selected is None:
            return ('#000000', 0.0)
        return selected


def launch_display(buffer: DisplayBuffer) -> SpectrumDisplay:
    """
    Create and start the display window in a background thread.
    Returns the SpectrumDisplay instance (call .start() has already been called).
    """
    display = SpectrumDisplay(buffer)
    display.start()
    return display
```

---

#### 51f — main.py Integration

```python
# CLI flag:
parser.add_argument('--display', action='store_true',
                    help='Launch live spectrum display window')

# Session setup — after OSC and mic confirmed working:
display_buffer = DisplayBuffer()
display = None
if args.display:
    from core.display_window import launch_display
    display = launch_display(display_buffer)
    print("DISPLAY: spectrum window launched")

# On song change — update genre target curve:
if display_buffer and active_genre:
    target_array = _genre_to_shape_array(active_genre)
    display_buffer.update(genre_target=target_array, song_name=..., genre_name=...)

# Fast path — in /meters/15 OSC handler (every 50ms):
if display_buffer:
    display_buffer.update(
        board_rta_fast=normalize_to_shape(osc.board_rta_db)
    )
```

---

#### 51g — Genre Target to Shape Array

```python
def _genre_to_shape_array(genre: GenreProfile) -> np.ndarray:
    """
    Convert genre YAML frequency_targets dict to a 1000-point FREQ_AXIS array.
    Genre targets are dB relative to flat (e.g. high_mid: +3).
    Interpolates between band centers to produce a smooth shape curve.
    """
    from core.channel_model import FREQ_AXIS
    # Band center frequencies for interpolation
    band_centers = {
        'sub':       50,   'bass':      150,  'low_mid':   350,
        'mid_low':   750,  'mid_high':  1500, 'upper_mid': 3000,
        'presence':  6000, 'air':       14000,
    }
    freqs, values = [], []
    for band, center_hz in sorted(band_centers.items(), key=lambda x: x[1]):
        target_db = genre.frequency_targets.get(band, 0.0)
        freqs.append(center_hz)
        values.append(target_db)

    log_centers = np.log10(freqs)
    log_axis    = np.log10(FREQ_AXIS)
    return np.interp(log_axis, log_centers, values,
                     left=values[0], right=values[-1])
```

---

#### 51h — Dependencies

Add to `requirements.txt`:
```
matplotlib>=3.8.0
```

`TkAgg` backend ships with most matplotlib installs on Windows. If `TkAgg` is not available, fall back to `Qt5Agg` (requires `PyQt5`) and print a warning. Do not fail silently — print "DISPLAY: backend TkAgg not available, trying Qt5Agg" before retrying.

---

### IMP-052 — Peak Detection Within Bands + Named Move Precision
**Priority:** `[HIGH]`  
**Phase Target:** Phase 2 — June 13 show  
**Status:** 📋 Designed — 2026-06-02  
**Source:** Architecture discussion — 8-band averages obscure intra-band peaks; the recommendation engine should report the specific Hz of a problem, not just the band name

The recommendation engine currently reports "low-mid buildup" without specifying where in the low-mid the energy is concentrated. With 1000-point mic resolution, we can find the peak frequency within each band and report it as part of the recommendation — giving the engineer a specific EQ target rather than a hunt-and-sweep.

---

#### 52a — `find_band_peak()` in `core/mic_analyzer.py`

```python
def find_band_peak(spectrum_db: np.ndarray,
                    freq_axis: np.ndarray,
                    band_lo: float,
                    band_hi: float) -> tuple[float, float]:
    """
    Find the peak frequency and its level above the band mean
    within a frequency band.

    Parameters
    ----------
    spectrum_db : ndarray
        Full spectrum on FREQ_AXIS (normalized or raw, same result for peak_hz).
    freq_axis : ndarray
        Corresponding frequency values in Hz (i.e. FREQ_AXIS).
    band_lo, band_hi : float
        Band boundaries in Hz.

    Returns
    -------
    peak_hz : float
        Frequency of peak within the band.
    peak_above_mean_db : float
        How far the peak sits above the band's own mean level.
        High value = sharp resonance. Low value = broad shelf.
    """
    mask = (freq_axis >= band_lo) & (freq_axis < band_hi)
    if not mask.any():
        return (band_lo + band_hi) / 2.0, 0.0

    band_spectrum = spectrum_db[mask]
    band_freqs    = freq_axis[mask]
    band_mean     = float(np.mean(band_spectrum))

    peak_idx  = int(np.argmax(band_spectrum))
    peak_hz   = float(band_freqs[peak_idx])
    peak_prom = float(band_spectrum[peak_idx]) - band_mean

    return peak_hz, peak_prom
```

---

#### 52b — Extend `compute_band_levels()` in `core/mic_analyzer.py`

The existing `compute_band_levels()` already returns `{avg_db, peak_db, peak_hz}` per band in the log schema. Confirm the actual implementation matches the schema — if it only returns `avg_db`, add `peak_db` and `peak_hz` using `find_band_peak()`:

```python
def compute_band_levels(spectrum_db: np.ndarray) -> dict:
    """
    Compute energy summary per analysis band.
    Returns band_name → {avg_db, peak_db, peak_hz, peak_prominence_db}.
    peak_prominence_db: how far peak sits above band mean — high = sharp resonance.
    """
    levels = {}
    for band_name, (f_lo, f_hi) in BAND_RANGES_DISPLAY.items():
        mask     = (FREQ_AXIS >= f_lo) & (FREQ_AXIS < f_hi)
        if not mask.any():
            levels[band_name] = {'avg_db': -90.0, 'peak_db': -90.0,
                                  'peak_hz': (f_lo + f_hi) / 2, 'peak_prominence_db': 0.0}
            continue

        band_spec = spectrum_db[mask]
        avg_db    = float(np.mean(band_spec))
        peak_idx  = int(np.argmax(band_spec))
        peak_db   = float(band_spec[peak_idx])
        peak_hz   = float(FREQ_AXIS[mask][peak_idx])
        prominence = peak_db - avg_db

        levels[band_name] = {
            'avg_db':          avg_db,
            'peak_db':         peak_db,
            'peak_hz':         peak_hz,
            'peak_prominence_db': prominence,
        }
    return levels
```

---

#### 52c — Named Move Lookup with Peak Frequency

The named move system in `recommender.py` already uses the EQ band frequency to label recommendations. Extend it to also use the mic `peak_hz` when the recommendation is driven by a mic band deviation (not an EQ state):

```python
def _recommendation_with_peak(band: str,
                                direction: str,
                                mic_result: MicAnalysis,
                                deviation_db: float) -> str:
    """
    Build recommendation text incorporating the specific peak frequency
    from the mic analysis, not just the band center.

    Example output:
        "Mud cut — low_mid +3.8dB · peak at 315Hz
         → EQ cut at 315Hz, Q≈2.0"
    """
    band_lo, band_hi = BAND_RANGES_DISPLAY[band]
    band_levels = mic_result.band_levels.get(band, {})
    peak_hz     = band_levels.get('peak_hz', (band_lo + band_hi) / 2)
    prominence  = band_levels.get('peak_prominence_db', 0.0)

    named = _named_move(peak_hz, 'cut' if direction == 'buildup' else 'boost')
    label = f"{named} — " if named else ""

    peak_str = f"peak at {peak_hz:.0f}Hz"
    if prominence > 2.0:
        peak_str += f" (+{prominence:.1f}dB above band mean — sharp resonance)"

    action_hz = f"{peak_hz:.0f}Hz"
    action_q  = "Q≈2.0" if direction == 'buildup' else "Q≈1.0"

    return (
        f"{label}{band} {deviation_db:+.1f}dB · {peak_str}\n"
        f"  → EQ {'cut' if direction == 'buildup' else 'boost'} "
        f"at {action_hz}, {action_q}"
    )
```

**Before:**
```
high_mid buildup +3.8dB — Guitar 1 dominant
  → EQ Band 2 cut to -1.5dB @ 2000Hz
```

**After:**
```
Harshness cut — upper_mid +3.8dB · peak at 3150Hz (+2.1dB above band mean — sharp resonance)
  → Guitar 1: EQ cut at 3150Hz, Q≈2.0
```

---

#### 52d — Display: Peak Marker on Curves

The `DisplayBuffer.band_peaks` dict (from IMP-051) carries `(peak_hz, peak_prominence_db)` per band. The display window renders a small vertical tick mark at `peak_hz` within the highlight region — white, low opacity, visible without dominating.

Peak markers only render when:
- The band highlight is active (|deviation| ≥ 1.5dB)
- `peak_hz` falls within the band's frequency boundaries
- `peak_prominence_db` > 0.5dB (not just average noise)

---

#### 52e — Tests

```python
def test_find_band_peak_returns_correct_frequency():
    """Peak in a flat spectrum plus a spike should return the spike frequency."""

def test_find_band_peak_prominence_high_for_sharp_resonance():
    """A sharp spike should have high prominence; a broad shelf should have low prominence."""

def test_compute_band_levels_includes_peak_fields():
    """band_levels should have avg_db, peak_db, peak_hz, peak_prominence_db for every band."""

def test_recommendation_text_includes_peak_hz():
    """Band deviation recommendation should include specific Hz from mic peak."""

def test_named_move_uses_peak_hz_not_band_center():
    """Named move should be based on actual peak frequency, not band center frequency."""
```

---

### IMP-053 — Dual-Path Mic FFT: Display vs Analysis
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2 — June 13 show  
**Status:** 📋 Designed — 2026-06-02  
**Source:** Architecture discussion — the 500ms Welch analysis path is stable but visually sluggish; a parallel 100ms single-window path makes the mic curve as visually responsive as the board RTA without affecting recommendation accuracy

Two parallel FFT paths from the same `AudioCapture` buffer. They share the audio data — only the analysis differs.

---

#### 53a — Two Paths, One Buffer

```
AudioCapture rolling buffer (3s)
         │
         ├── ANALYSIS PATH (500ms window, every 500ms)
         │     Welch's method, 50% overlap, geometry correction, EMA α=0.3
         │     → MicAnalysis.smoothed_spectrum_db
         │     → MicAnalysis.normalized_shape_db
         │     → Drives recommendations, cal scans, forward model
         │     → Written to DisplayBuffer.mic_shape (2fps visual update)
         │
         └── DISPLAY PATH (100ms window, every 100ms)
               Single Hanning window, light EMA α=0.4
               Geometry correction applied (same correction curve)
               No band analysis, no LUFS, no output events
               → Written to DisplayBuffer.mic_shape_fast (10fps visual update)
               → Display only — never drives recommendations
```

---

#### 53b — Implementation in `core/mic_analyzer.py`

Add a `compute_display_spectrum()` method to `MicAnalyzer`:

```python
DISPLAY_WINDOW_SECONDS = 0.1     # 100ms window for display path
DISPLAY_EMA_ALPHA_MIC  = 0.40    # faster than analysis EMA (0.3) — more responsive

class MicAnalyzer:
    def __init__(self, ...):
        # Existing analysis path state
        self._ema_analysis = EMAState(alpha=0.3)
        # New display path state
        self._ema_display  = EMAState(alpha=DISPLAY_EMA_ALPHA_MIC)

    def compute_display_spectrum(self,
                                   audio_capture: AudioCapture,
                                   venue_acoustics=None) -> np.ndarray:
        """
        Fast display-path spectrum. Single Hanning window, 100ms.
        Returns normalized_shape_db on FREQ_AXIS.
        For display only — never used for recommendations.
        """
        display_samples = int(DISPLAY_WINDOW_SECONDS * audio_capture.sample_rate)
        window_audio = audio_capture.get_display_window(display_samples)

        if len(window_audio) < 512:
            return np.zeros(N_FREQS)

        # Single Hanning window FFT
        n = len(window_audio)
        windowed = window_audio * np.hanning(n)
        spectrum  = np.fft.rfft(windowed)
        freqs_hz  = np.fft.rfftfreq(n, d=1.0 / audio_capture.sample_rate)
        psd_db    = 20.0 * np.log10(np.maximum(np.abs(spectrum) / n, 1e-12))

        # Interpolate to FREQ_AXIS
        interpolated = interpolate_to_freq_axis(freqs_hz, psd_db)

        # Apply geometry correction if available (same curve as analysis path)
        if venue_acoustics is not None:
            correction = venue_acoustics.mic_correction_curve(FREQ_AXIS)
            interpolated = interpolated + correction

        # EMA smoothing — faster than analysis path
        smoothed = self._ema_display.update(interpolated)

        return normalize_to_shape(smoothed)
```

Add `get_display_window()` to `AudioCapture`:

```python
def get_display_window(self, n_samples: int) -> np.ndarray:
    """Return the most recent n_samples from the rolling buffer. For display path only."""
    with self._lock:
        return self._buffer[-n_samples:].copy()
```

---

#### 53c — Display Buffer Update for Fast Mic Path

Add `mic_shape_fast` to `DisplayBuffer`:

```python
# In DisplayBuffer:
mic_shape_fast: np.ndarray = field(default_factory=lambda: np.zeros(1000))
```

In `main.py`, run the display path every 100ms in a lightweight timer or by piggybacking on the OSC fast path:

```python
# Every 100ms (can be a simple threading.Timer loop):
if display_buffer and mic_analyzer and audio_capture:
    fast_mic_shape = mic_analyzer.compute_display_spectrum(
        audio_capture, venue_acoustics
    )
    display_buffer.update(mic_shape_fast=fast_mic_shape)
```

In `SpectrumDisplay._update_frame()`, use `mic_shape_fast` instead of `mic_shape` for the displayed mic curve:

```python
mic_raw = snap.get('mic_shape_fast', snap['mic_shape'])
```

The analysis path `mic_shape` (500ms Welch) is still used for band highlights, peak detection, and recommendations. The display path `mic_shape_fast` is used for the visual curve only.

---

#### 53d — When NOT to Use the Display Path

The display path is never:
- Used to compute band highlights
- Used to compute peak_hz for recommendation text
- Used for cal scans or iso samples
- Logged in show events
- Compared against genre targets for thresholds

If the display window is not open (`--display` not set), the display path does not run at all. Zero overhead.

---

#### 53e — Tests

```python
def test_display_spectrum_returns_correct_shape():
    """compute_display_spectrum() returns array of shape (N_FREQS,)."""

def test_display_spectrum_mean_approximately_zero():
    """Output is normalized to shape — mean should be near 0."""

def test_display_path_does_not_affect_analysis_ema():
    """Display EMA state is separate from analysis EMA state."""

def test_get_display_window_returns_n_samples():
    """get_display_window(4800) returns exactly 4800 samples."""
```

## Deferred / Future Items

---

### IMP-D01 — AI/ML Learning Layer
**Priority:** `[FUTURE]`  
**Phase Target:** Phase 5  
**Status:** 💬 Discussed  
**Source:** May 12 planning session

Small local model trained on show log data to learn from engineer correction patterns. Separate from the deterministic physics layer (IMP-034–039) — the ML layer runs on top of an accurate deterministic foundation, not instead of it.

**Design dependencies:** Forward mix model (IMP-034–039) must be validated (IMP-041) before ML training is meaningful. Training data schema needs to be defined and logged starting June 13.

**Planned architecture:** Local inference on laptop. Two phases — (1) small model learning from this band's show history, (2) LLM (Claude API) conversational layer that the engineer can talk to about mix decisions, backed by the internal model's real-time state.

---

### IMP-D02 — Show Replay Simulator
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2–3  
**Status:** 📋 Designed — Claude Code prompt written (`CLAUDE_CODE_SHOW_REPLAY_PROMPT.md`)  
**Source:** May 12 planning session — needed to validate forward model against Show 1 log

`tools/show_replay.py` replays a show JSON at configurable speed. Two mic modes:
- `derived` — mic spectrum mathematically derived from board state (R² target >0.90 — confirms model math is correct)
- `noise-injected` — mic spectrum derived + realistic noise (R² target 0.65–0.75 — confirms model is realistic)

Port map: 10023/10024 X32 OSC (existing), 19876 mic injection (UDP), 19877 board RTA broadcast (UDP).

---

### IMP-D03 — Solo Preset Commands
**Priority:** `[HIGH]`  
**Phase Target:** Phase 3.5  
**Status:** 📋 Designed  
**Source:** Real-world insight — engineer needs one-keypress solo management without full automation

Keys `1`, `2`, `3` trigger pre-configured relative fader adjustments for Guitar 1 solo, Guitar 2 solo, Keys solo. Hold for configurable duration, then ramp back automatically over 2–3 seconds. Key `0` = emergency restore. Safety rail: no channel moves more than ±3dB from soundcheck baseline in one command. **Requires OSC write access — Phase 4 unlock.**

---

### IMP-D04 — Automated Solo Mix
**Priority:** `[FUTURE]`  
**Phase Target:** Phase 4+  
**Status:** 💬 Discussed  
**Source:** Extension of IMP-D03

When RMS spike + fader rate-of-change triggers on a guitar channel, apply solo preset automatically. System learns solo timestamps per song across shows. Pre-loads solo mix before the boost pedal click. Engineer becomes safety net rather than primary operator.

---

### IMP-D05 — Baseline Snapshot Suppression of Static Deviations
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Static false positives in show mode

After soundcheck baseline captured, suppress frequency band recommendations for deviations already present and accepted at soundcheck (within 1dB of same deviation). Deviations that develop during the show still fire.

---

### IMP-D06 — Reference Audio Targeting
**Priority:** `[FUTURE]`  
**Phase Target:** Phase 6  
**Status:** 💬 Discussed  
**Source:** Scope doc Phase 6

Per-song reference audio files analyzed locally to extract precise frequency targets. Replaces genre template when reference is available. Cover band delta tracking after multiple shows. Reference audio never transmitted or reproduced — local analysis only.

---

### IMP-D07 — miniDSP UMIK-2 Calibration File Support
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 3  
**Status:** 📋 Designed  
**Source:** Hardware recommendation — UMIK-2 includes individual calibration file

Load `.cal` file at startup, apply correction curve to FFT output per frequency bin. Makes frequency band readings accurate rather than just relative. Also enables Room EQ Wizard (REW) integration for pre-show room capture.

---

### IMP-D08 — Funk/R&B Genre Profile
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Identified (see IMP-033)  
**Source:** Setlist review

Add `funk_rock.yaml` after reviewing June 13 show log data. Songs: Superstition, Play That Funky Music, Brick House, Cult of Personality.

---

### IMP-D09 — Post-Show Report as Forum Content
**Priority:** `[LOW]`  
**Phase Target:** Phase 2+  
**Status:** 📋 Designed  
**Source:** Market strategy

Export sanitized post-show report suitable for r/livesound and Gearspace sharing. Remove venue-specific details, highlight recommendation patterns and accuracy metrics.

---

## Implemented — Closed Items

| ID | Description | Phase | Date |
|---|---|---|---|
| IMP-001 | Silence guard on recommendation engine | 1 | 2026-05-05 |
| IMP-002 | Channel RMS guard in culprit attribution | 1 | 2026-05-05 |
| IMP-003 | Global LUFS recommendation cooldown | 1 | 2026-05-05 |
| IMP-004 | EQ band selection by proximity | 1 | 2026-05-05 |
| IMP-005 | OSC client ephemeral port trap | 1 | 2026-05-05 |
| IMP-006 | Simulator push broadcast missing | 1 | 2026-05-05 |
| IMP-007 | OSC client poll fallback for stale state | 1 | 2026-05-05 |
| IMP-008 | Rate-of-change suppression sliding window | 1 | 2026-05-05 |
| IMP-009 | RMS rate-of-change as solo trigger | 1 | 2026-05-05 |
| IMP-010 | Recommendation fader attribution fix | 1 | 2026-05-05 |
| IMP-011 | Deviation stability guard | 1 | 2026-05-05 |
| IMP-019 | Manual song markers with per-song log segments | 1 | 2026-05-06 |
| IMP-020 | Transition grace cancels immediately on song start | 1 | 2026-05-06 |
| IMP-021 | Soundcheck mode (`--soundcheck`) | 1 | 2026-05-06 |
| IMP-022 | HPF state and input gain from X32 (advisory suppressed — see IMP-026) | 1 | 2026-05-06 |
| IMP-023 | Full parametric EQ advisory | 1 | 2026-05-06 |
| IMP-025 | Frequency fingerprint corrections | 1 | 2026-05-06 |

---

## Superseded Items (Do Not Implement)

| ID | Description | Reason Superseded |
|---|---|---|
| *(original IMP-020)* | Analyzer architecture: OSC meters drive channel recommendations | Replaced by IMP-034–039 forward mix model — more rigorous approach |
| *(original IMP-021–024 from project knowledge)* | FFT analyzer: Welch's method, peak detection, smoothing, A-weighting | Room mic is no longer primary per-channel source; these improvements apply only to LUFS/overall monitoring, scope reduced |

---

*This document is reviewed and merged into the scope doc at the end of each phase.*  
*Supersedes `FOH_Assistant_Design_Improvements.md` (all prior versions).*
