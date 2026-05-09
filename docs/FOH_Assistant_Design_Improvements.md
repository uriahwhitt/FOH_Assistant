# FOH Assistant — Design Improvements Tracker
**Status:** Active  
**Last Updated:** 2026-05-06  
**Purpose:** Capture design improvements, feature ideas, and lessons learned during development and testing. Review and incorporate into scope doc between phases.

---

## How to Use This Doc

- Add items as they surface during testing, shows, or design discussions
- Tag each item with a priority and target phase
- Mark items **Implemented** when Claude Code ships them
- Mark items **Deferred** if descoped or pushed to a later phase
- Review and merge into scope doc at the end of each phase

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

## Active Items

---

### IMP-020 — Transition Grace Cancels Immediately on Song Start
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** 📋 Ready for implementation  
**Source:** Design review — medley and direct-roll situations suppress monitoring at the start of a new song, which is exactly when the engine needs to be watching

**Problem:** `set_transition(True)` starts a 30-second timer. If the band rolls directly into the next song and `n` is pressed immediately, the new song's opening is suppressed. The grace was designed for between-song dead time, not for a running song.

**Fix — `_handle_next()` in `main.py` call sequence must be:**
1. `recommender.set_transition(False)` — cancel any active grace immediately
2. `logger.log_song_end()` — close prior song
3. Load next song genre profile
4. `logger.log_song_start(...)` — open new song
5. `recommender.set_transition(True)` — start a short fresh grace for the handoff moment

**Also fix:** `_handle_end_early()` — same pattern: `set_transition(False)` first to reset any prior grace before starting the new one.

**Grace window reduction:** Reduce `transition_grace_seconds` in `config/band.yaml` from 30 to 8 seconds. The purpose of grace is absorbing 2–3 seconds of transition noise at song start, not a full 30-second suppression window. With cancel-on-next behavior, shortening is now safe.

**Test:** Run `level_creep.yaml`, press `e` then immediately `n`. Confirm recommendations resume within one cycle of the `n` keypress, not 30 seconds later. Also confirm a direct-roll `n → n` (medley simulation) resumes recommendations immediately after the second `n`.

---

### IMP-021 — Soundcheck Mode (Continuous Advisory, Confirm Locks Baseline)
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** 📋 Ready for implementation  
**Source:** Design review — `--baseline` is interactive/on-demand; engineers need continuous real-time advisory while actively working the board during soundcheck. Baseline should capture the final state the engineer is satisfied with, not an intermediate state.

**Launch:** `python main.py --soundcheck`

**Core behavior:** Identical to show mode's main loop — 1-second cycles, ref mic active, OSC polling, full recommendation engine running — with these differences:

- No baseline set at startup → `_check_baseline_drift()` disabled
- Genre-profile advisory is the active recommendation source (LUFS + frequency bands)
- Full EQ parametric advisory active (IMP-023)
- HPF and gain staging checks active (IMP-022)
- Compressor sanity checks active (ratio > 7:1 on non-percussion; see Audio Guide Section 5.3)
- Gate threshold sanity check active (gate threshold above active channel RMS = flag)
- Recommendation cooldown shortened to 20 seconds (soundcheck is the time to fix things, not silence them)
- Deviation stability guard disabled in soundcheck mode — repeat flags are desired until fixed

**Keyboard commands during soundcheck:**
```
s        → current board state snapshot (all channels)
g        → current room analysis (LUFS + all frequency bands vs target)
confirm  → lock current board state as baseline, print summary, exit to show mode
Ctrl+C   → abort without saving baseline
```

**Terminal header at launch:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FOH ASSISTANT — SOUNDCHECK MODE
Band:    Nostalgic Knights
Genre:   Hard Rock (soundcheck reference)
X32:     192.168.0.1:10023 ✓
Audio:   DJI Mic 2 ✓
Cooldown: 20s  |  No baseline set — advisory only
Type 'confirm' when satisfied to lock baseline.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**On `confirm` — snapshot and summary:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BASELINE LOCKED — 2026-05-10 19:47:32
Saved: shows/2026-05-10_baseline.json
Channels captured: 14
Overall LUFS at confirm: -17.4dB (Hard Rock target: -17dB ✓)

Unresolved at confirm (accepted as baseline — suppressed in show mode):
  Guitar 7   — low-mid 1.8dB above target (within tolerance, accepted)
  Drum Rack  — HPF off (noted — non-critical)

Compressor flags (review before show):
  Lead Vocal — ratio 7:1 (high for vocal — consider 3:1–4:1)

Launch show mode: python main.py --show
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Log event:** `SOUNDCHECK_COMPLETE` logged with timestamp, channel count, LUFS at confirm, and list of unresolved issues accepted as baseline.

**New CLI argument:** `--soundcheck` added to `main.py` argument parser alongside `--show`, `--baseline`, `--devices`, `--test-osc`. Must be mutually exclusive with the other modes.

**Existing `--baseline` mode:** Retained as-is for methodical per-channel interactive review. It is not replaced by soundcheck mode — both serve different workflows.

---

### IMP-022 — Read HPF State and Input Gain from X32; Include in Soundcheck Advisory
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1 (read + advisory in soundcheck) / Phase 4 (automated fix)  
**Status:** 📋 Ready for implementation  
**Source:** Design review and Audio Guide Section 3 — HPF state and gain staging are foundational; invisible to the engine without reading these parameters

**New OSC addresses to add to `osc_client.py` poll:**

| Parameter | OSC Address | Type | Notes |
|---|---|---|---|
| Input gain | `/ch/nn/preamp/gain` | float | Typically 0–60dB range |
| HPF on/off | `/ch/nn/preamp/hpon` | int | 1=on, 0=off |
| HPF frequency | `/ch/nn/preamp/hpf` | float | Apply same float→Hz conversion as EQ bands |
| HPF slope | `/ch/nn/preamp/hpslope` | int | 0=6dB/oct, 1=12dB/oct, 2=18dB/oct, 3=24dB/oct |

**`ChannelState` model additions (`models/channel.py`):**
```python
hpf_on: bool = False
hpf_freq_hz: float = 80.0
hpf_slope: int = 1          # 0=6dB/oct, 1=12dB/oct, 2=18dB/oct, 3=24dB/oct
input_gain_db: float = 0.0
```

**OSC client additions (`core/osc_client.py`):**
- Add `/ch/nn/preamp` to the `/node` poll request set
- Parse `preamp/gain`, `preamp/hpon`, `preamp/hpf`, `preamp/hpslope` into `ChannelState`
- Apply the existing `x32_float_to_hz()` conversion to HPF frequency

**Add to baseline JSON snapshot** so HPF and gain state are captured at `confirm` and available for show-mode comparison.

**Soundcheck advisory logic — HPF check (run each cycle, per active channel):**

```
For each active instrument channel (not kick, not bass DI):
  If hpf_on == False:
    Fire: "⚠ HPF OFF — {label}: no high-pass filter engaged"
    Suggest frequency by channel type:
      Guitar:           "Enable HPF @ 80–100Hz"
      Keys:             "Enable HPF @ 80–120Hz"
      Acoustic Guitar:  "Enable HPF @ 80Hz"
      Drum Rack:        "Enable HPF @ 60–80Hz"
      Floor Tom:        "Enable HPF @ 60Hz"
      Vocals (all):     "Enable HPF @ 80–120Hz"
  Elif hpf_slope == 0 (6dB/oct):
    Fire: "⚠ HPF SLOPE — {label}: HPF on at {hpf_freq_hz:.0f}Hz but slope is 6dB/oct (gentle)"
    Suggest: "Consider 12dB/oct for more effective rumble removal"
```

**Soundcheck advisory logic — gain staging check (run each cycle, per active channel):**

Thresholds grounded in Audio Guide Section 3.3:
```
Hard flag (gain staging inversion):
  If rms_db < -30.0 AND fader_db > +5.0:
    Fire: "⚠ GAIN STAGING — {label}: signal weak ({rms_db:.0f}dBFS), fader pushed high ({fader_db:+.1f}dB)"
    Suggest: "Increase input gain, reduce fader toward 0dB"

Soft note (fader high, not yet a problem):
  Elif fader_db > +5.0:
    Fire: "ℹ FADER HIGH — {label}: fader at {fader_db:+.1f}dB — monitor for headroom"
    No suggestion required — informational only
```

**In show mode:** HPF and gain staging checks are silent. State is captured at baseline; deviations from HPF state (e.g., HPF accidentally toggled off) can be flagged via baseline drift (Phase 2+).

---

### IMP-023 — Full Parametric EQ Advisory (Named Moves, Q Guidance, Psychoacoustic Weighting, Culprit Scoring)
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** 📋 Ready for implementation  
**Source:** Design review and Audio Guide Sections 4.3, 4.4, 4.5, 12.5 — current engine gives directionally correct but incomplete EQ advice; named moves, Q awareness, perceptual weighting, and multi-factor culprit scoring all missing

This IMP extends `core/recommender.py` in four areas. All four are in the same module; implement together.

---

#### 23a — Named Move Recognition

**Source:** Audio Guide Section 4.4 — "recommendation output should describe the named move rather than just the frequency change"

Add a constant `NAMED_MOVES` table and a lookup function. Insert the named move label at the front of every EQ recommendation string.

```python
# In recommender.py — add as module-level constant
NAMED_MOVES = [
    # (lo_hz, hi_hz, direction, name)
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

def _named_move(freq_hz: float, direction: str) -> str:
    """Return the closest named move label for a given frequency and direction."""
    for lo, hi, move_dir, name in NAMED_MOVES:
        if lo <= freq_hz <= hi and move_dir == direction:
            return name
    return ""  # no match — unnamed move, omit label
```

**In `_eq_recommendation()`:** After determining cut/boost direction and target frequency, call `_named_move()` and prepend its result to the suggestion string:
```
Before: "EQ Band 2 cut to -1.5dB @ 315Hz"
After:  "Mud cut — EQ Band 2 cut to -1.5dB @ 315Hz"
```

---

#### 23b — EQ Band Position and Q Advisory

**Source:** Audio Guide Sections 4.2, 4.3

Extend `_eq_recommendation()` with two additional checks after the existing gain-change logic. Append findings to the suggestion string.

**Band position check:**
```python
def _band_covers_problem(eq_band: EQBand, lo: int, hi: int) -> bool:
    return lo <= eq_band.freq_hz <= hi

# In _eq_recommendation():
if not _band_covers_problem(best_band, lo, hi):
    suggestion += f" | Note: Band {best_band.band_num} is at {best_band.freq_hz:.0f}Hz — move to {mid_freq}Hz first"
```

**Q guidance — cut/boost asymmetric (Audio Guide Section 4.3 rule: cuts narrow Q>2, boosts wide Q<1.5):**
```python
def _q_advice(eq_band: EQBand, direction: str) -> str:
    q = eq_band.q
    if direction == "cut":
        if q < 1.0:
            return f"Q={q:.1f} is very broad for a cut — consider Q≈2.0 for more focused correction"
        if q > 8.0:
            return f"Q={q:.1f} is notch-narrow — appropriate only for feedback elimination; consider Q≈2.0 for mix EQ"
    elif direction == "boost":
        if q > 2.0:
            return f"Q={q:.1f} is narrow for a boost — narrow boosts sound unnatural; widen to Q≈1.0"
    return ""

# In _eq_recommendation():
q_note = _q_advice(best_band, direction)
if q_note:
    suggestion += f" | {q_note}"
```

---

#### 23c — Psychoacoustic Band Weighting

**Source:** Audio Guide Section 12.5 — weight deviations by perceptual sensitivity before threshold comparison

Add weighting to `_check_bands()` so high-mid deviations trigger before equivalent sub-bass deviations.

```python
# In recommender.py — add as module-level constant
BAND_PERCEPTUAL_WEIGHTS = {
    "sub_bass":  0.6,
    "bass":      0.8,
    "low_mid":   1.0,   # reference weight
    "mid":       1.1,
    "high_mid":  1.3,   # most sensitive — harshness range
    "presence":  1.2,
    "air":       0.9,
}
```

**In `_check_bands()`:** Apply weight before the threshold comparison:
```python
raw_deviation = room_band_db - target_band_db
weight = BAND_PERCEPTUAL_WEIGHTS.get(band, 1.0)
weighted_deviation = raw_deviation * weight

if abs(weighted_deviation) >= self._trigger_db:
    # fire recommendation using weighted_deviation for priority ordering
    # but report raw_deviation in the output string (what the engineer acts on)
```

This changes the *order* recommendations fire (high-mid before sub-bass for equal raw deviations) without changing the *reported* deviation magnitude.

---

#### 23d — Multi-Factor Culprit Scoring

**Source:** Audio Guide Section 4.5 — culprit ranking should consider RMS level, EQ boost in the problem band, and baseline drift; not RMS × fingerprint overlap alone

Extend `_find_culprit()` to add an EQ-in-band boost factor to the score:

```python
# Existing score: overlap_score * (ch.rms_db + 90)
# New score adds: EQ boost contribution in the problem band

eq_boost_in_band = 0.0
for eq_band in ch.eq:
    if eq_band.type in (0, 5):   # skip LCut/HCut
        continue
    if lo <= eq_band.freq_hz <= hi and eq_band.gain_db > 0:
        eq_boost_in_band += eq_band.gain_db  # accumulate boosts in the problem zone

# EQ boost adds up to 6dB equivalent to the score — a channel boosting in the
# problem zone is strongly implicated even at moderate RMS
composite_score = overlap_score * (ch.rms_db + 90) + (eq_boost_in_band * 6.0)
candidates.append((composite_score, ch))
```

When two channels are close in RMS, the one with an active EQ boost in the problem band is now correctly ranked higher.

---

### IMP-024 — Soundcheck Reference Song Analysis Tool
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 1.5 (after first show, before second)  
**Status:** 📋 Designed — not blocking for this weekend  
**Source:** Design discussion — pre-analyzing the soundcheck song (Tush, ZZ Top) against the studio recording gives a precise per-segment frequency target and enables per-guitarist solo calibration logging

**New offline tool:** `tools/analyze_reference.py`

Run before the show against a studio audio file. Segments the file by timestamp range, computes average frequency band profile per segment (using the same analyzer pipeline as the live engine), and saves results as a YAML reference target.

```bash
python tools/analyze_reference.py \
  --audio "references/zztop_tush.mp3" \
  --segments "references/tush_segments.yaml" \
  --output "references/tush_targets.yaml"
```

**Segments definition file (`references/tush_segments.yaml`):**
```yaml
song: "Tush"
artist: "ZZ Top"
segments:
  - id: verse_riff
    start_s: 14
    end_s: 44
    description: "Main groove — full band, use for overall mix balance"
  - id: solo
    start_s: 74
    end_s: 94
    description: "Guitar solo — use for solo level calibration"
```

**Generated target file (`references/tush_targets.yaml`):**
```yaml
# Auto-generated by tools/analyze_reference.py — do not edit manually
song: "Tush"
artist: "ZZ Top"
analyzed_at: "2026-05-09T14:23:11"
segments:
  verse_riff:
    sub_bass: -9.1
    bass: +1.2
    low_mid: -0.8
    mid: +1.6
    high_mid: +2.4
    presence: +1.8
    air: -0.5
    lufs: -14.2
  solo:
    sub_bass: -10.2
    bass: +0.9
    low_mid: -1.1
    mid: +0.8
    high_mid: +4.1
    presence: +3.2
    air: +0.2
    lufs: -12.8
```

**New config file `config/soundcheck.yaml`:**
```yaml
soundcheck_song:
  title: "Tush"
  artist: "ZZ Top"
  reference_targets: "references/tush_targets.yaml"
  active_segment: "verse_riff"
  solo_segment: "solo"
```

**Soundcheck mode behavior when reference targets are loaded:**
- Replace genre template frequency targets with reference segment targets for soundcheck
- Print at startup: `Reference target: Tush (ZZ Top) — verse_riff segment`
- When RMS spike detected on a guitar channel (>4dB jump per IMP-009 logic), auto-switch active segment to `solo` for that channel's evaluation window, then restore
- Log solo calibration data per guitarist

**Solo calibration log event (new type):**
```json
{
  "type": "SOLO_CALIBRATION",
  "channel": "Guitar 7",
  "channel_num": 7,
  "trigger": "rms_spike",
  "rms_delta_db": 5.2,
  "duration_s": 24,
  "reference_solo_lufs": -12.8,
  "actual_solo_lufs": -13.1,
  "delta_from_reference": -0.3,
  "timestamp": "19:43:17"
}
```

**Post-soundcheck summary output (printed at `confirm`):**
```
SOLO CALIBRATION SUMMARY
  Guitar 7  — boost pedal: +5.2dB RMS spike, 24s avg duration
  Guitar 8  — boost pedal: +3.8dB RMS spike, 22s avg duration
  Note: Guitar 8 pedal is softer — consider separate suppression threshold
  Calibration data saved to band.yaml for show-mode solo detection.
```

---

### IMP-025 — Frequency Fingerprint Corrections (band.yaml)
**Priority:** `[HIGH]`  
**Phase Target:** Phase 1  
**Status:** 📋 Ready for implementation  
**Source:** Audio Guide Section 7 — per-instrument profiles in the guide conflict with current broad fingerprints in band.yaml; bad fingerprints produce wrong culprit attribution throughout Phase 1

**Config-only change — no Python code modified.** Update `config/band.yaml` `frequency_fingerprints` block.

These corrections improve attribution accuracy in both show mode and soundcheck mode immediately.

**Corrections:**

**Kick (ch1):**
```yaml
# Before
Kick:
  primary: [60, 80]

# After — split into functional zones
Kick:
  fundamental: [50, 80]      # weight / boom
  body: [80, 150]            # chest punch
  click: [2000, 4000]        # beater definition
  mud_zone: [300, 500]       # cut target — high here = cut kick, not add it
```

Note: `mud_zone` is a cut recommendation zone. Engine should treat buildup in 300–500Hz as a reason to *cut* kick in that range, not implicate kick as a primary contributor requiring level reduction.

**Guitar 7 / Guitar 8 (ch7, ch8) — replace single broad fingerprint:**
```yaml
# Before
Guitar 1:
  primary: [200, 5000]

# After
Guitar 1:
  body: [200, 1000]          # rhythm foundation, chords
  bite: [2000, 5000]         # pick attack, presence, cut-through
```

Attribution in low-mid band uses `body`; attribution in high-mid/presence band uses `bite`. This prevents a high-mid problem from wrongly implicating a channel that only contributes in the body range.

**Bass DI (ch13) — add definition zone:**
```yaml
# Before
Bass:
  primary: [80, 250]

# After
Bass:
  primary: [40, 250]         # fundamental — low E = 41Hz
  definition: [700, 1000]    # growl / presence in dense mixes
  attack: [2000, 4000]       # pick or slap transient (present in this band's mix)
```

**Drum Rack (ch4) vs. Floor Tom (ch5) — separate fingerprints:**
```yaml
# Before (both likely under same broad fingerprint)
Drum Rack:
  primary: [100, 500]

# After
Drum Rack:             # rack toms
  primary: [100, 250]
  attack: [3000, 6000]

Floor Tom:             # ch5 — lower fundamental
  primary: [60, 120]
  attack: [3000, 5000]
```

**Keys (ch15) — replace single broad fingerprint:**
```yaml
# Before
Keys:
  primary: [200, 8000]

# After
Keys:
  bass_register: [60, 250]   # lower left hand — where keys competes with bass
  body: [250, 1000]          # mid register — where keys competes with guitar
  brilliance: [2000, 5000]   # upper register, right hand melody
```

**Acoustic Guitar (ch6) — add distinct profile:**
```yaml
Acoustic Guitar:
  body: [150, 400]           # wood/warmth — also where boom lives
  sparkle: [2000, 6000]      # pick attack and clarity
  air: [8000, 16000]         # open-body resonance
```

---

### IMP-012 — Solo Preset Commands
**Priority:** `[HIGH]`  
**Phase Target:** Phase 3.5 (between UI and full automation)  
**Status:** 📋 Designed — not yet implemented  
**Source:** Real-world insight — engineer needs one-keypress solo management without full automation

Single keypress triggers pre-configured set of relative fader adjustments, holds for solo duration, then ramps back automatically. Bridges manual control and full automation.

**Keyboard commands:**
- `1` → Guitar 1 solo preset
- `2` → Guitar 2 solo preset  
- `3` → Keys solo preset
- `0` → Emergency restore (snap all channels to baseline immediately)
- Press same key again → restore early

**Preset behavior:**
- All adjustments relative to current fader position (not absolute values)
- Hold for configurable duration (default 30s, per-song in setlist)
- Restore ramps over 2-3 seconds (not a snap)
- Safety rail: no channel moves more than ±3dB from soundcheck baseline in one command

**Config structure:**
```yaml
solo_presets:
  guitar_1:
    key: "1"
    adjustments:
      Guitar 1:        +1.5
      Guitar 2:        -2.0
      Keys:            -1.5
      Acoustic Guitar: -1.5
      Lead Vocal:      -1.0
      Bass:            -0.5
    hold_seconds: 30
    restore_ramp_seconds: 2

  guitar_2:
    key: "2"
    adjustments:
      Guitar 2:        +1.5
      Guitar 1:        -2.0
      Keys:            -1.5
      Acoustic Guitar: -1.5
      Lead Vocal:      -1.0
      Bass:            -0.5
    hold_seconds: 30
    restore_ramp_seconds: 2

  keys_solo:
    key: "3"
    adjustments:
      Keys:       +2.0
      Guitar 1:   -1.5
      Guitar 2:   -1.5
      Lead Vocal: -1.0
    hold_seconds: 25
    restore_ramp_seconds: 2

  emergency_restore:
    key: "0"
    hold_seconds: 0
    restore_ramp_seconds: 0
```

**Requires:** OSC write access (Phase 4 unlock). Infrastructure, config, and restore logic can be built in Phase 3.5 with writes gated behind a flag.

---

### IMP-013 — Automated Solo Mix in Phase 4
**Priority:** `[FUTURE]`  
**Phase Target:** Phase 4+  
**Status:** 📋 Designed — long term  
**Source:** Extension of IMP-009 and IMP-012

When RMS spike detected (boost pedal) AND/OR fader rate-of-change triggered on a guitar channel:
1. Apply solo preset adjustments automatically (no keypress needed)
2. Estimate solo duration from setlist annotations or learned show history
3. Ramp all channels back to pre-solo position when solo ends (RMS drops + fader returns)
4. Log solo event with duration, peak level, and which trigger fired

**Over multiple shows:**
- System learns solo timestamps per song
- Pre-loads solo mix before boost pedal click
- Engineer becomes safety net rather than primary operator

**The real value:** Any engineer can push the solo guitar up. The skill is simultaneously pulling everything else back. A system watching all 12 channels can do this better than a human with two hands in real time.

---

### IMP-014 — Baseline Snapshot Suppression of Static Deviations
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed — pending baseline mode completion  
**Source:** IMP-011 — static false positives best resolved by comparing to accepted soundcheck state

After soundcheck baseline is captured, suppress frequency band recommendations for deviations that were already present and accepted at soundcheck (within 1dB of the same deviation). Channel was assessed and accepted — shouldn't keep firing for a static condition.

Deviations that develop during the show (drift from baseline) should still fire.

---

### IMP-015 — miniDSP UMIK-2 Calibration File Support
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 2  
**Status:** 📋 Designed  
**Source:** Hardware recommendation discussion — UMIK-2 includes individual calibration file

The miniDSP UMIK-2 measurement mic ships with an individual calibration file that corrects minor frequency response deviations. Loading this file into the analyzer and applying it to FFT output would make frequency band readings accurate rather than just relative.

**Implementation:** Load `.cal` file at startup, apply correction curve to `_fft_bands()` output per frequency bin.

**Also enables:** Room EQ Wizard (REW) integration — run REW in the venue pre-soundcheck to capture room response curve, feed into FOH Assistant as a venue profile layer on top of genre profile.

---

### IMP-016 — Venue Profile Layer
**Priority:** `[MEDIUM]`  
**Phase Target:** Phase 3+  
**Status:** 📋 Designed  
**Source:** REW/UMIK-2 discussion — room acoustics vary significantly by venue

Each venue has unique acoustic characteristics (nodes, reflections, problem frequencies). A venue profile captured via REW pre-soundcheck could be stored and loaded automatically, adjusting the genre target curve to account for known room behavior.

**Over time:** Build a library of venue profiles. When the band plays a known venue, the system pre-loads the room correction and soundcheck starts from a better baseline.

---

### IMP-017 — Reference Audio Targeting
**Priority:** `[FUTURE]`  
**Phase Target:** Phase 6  
**Status:** 📋 Designed — data model stubbed in setlist.yaml  
**Source:** Scope doc Phase 6

Per-song reference audio files analyzed locally to extract precise frequency targets for that specific recording. Replaces genre template as recommendation target when reference file is available.

**Critical dependency:** Trust in automated board control (Phase 4) must be established first. Reference audio precision is most valuable when the system is making autonomous adjustments toward a specific target.

**Cover band delta tracking:** After multiple shows, logs consistent differences between band's live sound and reference recording. "Guitar 1 consistently sits 2dB hotter in high-mid than the Warren DeMartini recorded tone" → useful pre-show insight.

---

### IMP-018 — Post-Show Report as Forum Content
**Priority:** `[LOW]`  
**Phase Target:** Phase 2+  
**Status:** 📋 Designed  
**Source:** Market strategy discussion

Post-show JSON + report is natural forum content. "Here's what a real show looked like with a reference mic in the room" is interesting to the r/livesound and Gearspace communities.

**Consider:** Export a sanitized/formatted version of the post-show report suitable for sharing — remove venue-specific details, highlight interesting recommendation patterns and accuracy metrics.

---

## Implemented — Closed Items

| ID | Description | Phase | Date |
|---|---|---|---|
| IMP-001 | Silence guard | 1 | 2026-05-05 |
| IMP-002 | Channel RMS guard | 1 | 2026-05-05 |
| IMP-003 | Global LUFS cooldown | 1 | 2026-05-05 |
| IMP-004 | EQ band selection by proximity | 1 | 2026-05-05 |
| IMP-005 | OSC ephemeral port trap | 1 | 2026-05-05 |
| IMP-006 | Simulator push broadcast | 1 | 2026-05-05 |
| IMP-007 | OSC poll fallback | 1 | 2026-05-05 |
| IMP-008 | Rate-of-change suppression sliding window | 1 | 2026-05-05 |
| IMP-009 | RMS rate-of-change solo trigger | 1 | 2026-05-05 |
| IMP-010 | Recommendation fader attribution fix | 1 | 2026-05-05 |
| IMP-011 | Deviation stability guard | 1 | 2026-05-05 |
| IMP-019 | Manual song markers with per-song log segments | 1 | 2026-05-06 |

---

## Deferred Items

*None yet.*

---

*This document is reviewed and merged into the scope doc at the end of each phase.*
