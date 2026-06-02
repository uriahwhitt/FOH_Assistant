# FOH Assistant — Scope & Requirements
**Version:** 0.7  
**Status:** Active — Phase 2 in progress  
**Last Updated:** 2026-05-26  
**Author:** Uriah Whittemore

---

## 1. Overview

FOH Assistant is a live sound advisory tool for engineers running Behringer X32 digital mixing consoles. It combines a reference microphone feed, real-time X32 channel data read via OSC, and per-song setlist context to generate labeled, actionable mix recommendations during a live show — without touching the board autonomously.

The engineer retains full control at all times. The tool functions as a second set of ears and a second brain, not a replacement for the engineer's judgment.

---

## 2. Problem Statement

Small-to-mid venue sound engineers — including bands that self-engineer — face several recurring challenges during live shows:

- Overall loudness creep over the course of a set
- Frequency buildup (low-mid mud, high-end harshness) that develops gradually and is hard to catch in real time
- Distraction from mix decisions when managing other show logistics
- No objective reference for what the room is actually hearing vs. what the stage sounds like
- Intentional level changes (solos, dynamic drops) being difficult to distinguish from problematic drift

FOH Assistant addresses these by providing continuous room monitoring with context-aware, instrument-labeled recommendations.

---

## 3. Target Users

### Primary
- Working engineers at 200–500 cap venues running shows solo
- Bands that self-engineer without a dedicated FOH person

### Secondary
- Church AV volunteer teams (consistent setup, repeating program structure)
- Regional touring acts wanting a safety net over unpredictable house engineers

---

## 4. Hardware Context

| Component | Phase 1 | Phase 2 (Current) |
|---|---|---|
| Mixing Console | Behringer X32 | Behringer X32 |
| Control Surface | Tablet running X32-Q or X32-Mix | Tablet running X32-Q or X32-Mix |
| Reference Mic | DJI Mic 2 (clipped mid-room) | Audio Technica AT2035 cardioid condenser |
| Audio Interface | DJI USB receiver | PreSonus Studio 26c USB interface |
| Host Machine | Laptop on same WiFi as X32 | Laptop on same WiFi as X32 |
| Network | Local WiFi — X32 as AP or joined network | Local WiFi — X32 as AP or joined network |

**Phase 3+ addition:** Second mic channel (boundary/PZM) on PreSonus Ch2 for two-point room characterization (see IMP-042).

---

## 5. System Architecture

The architecture uses two primary intelligence sources — the X32 board data and the room microphone — with distinct, non-overlapping roles.

```
┌─────────────────────────────────────────────────────────────────┐
│                    X32 BOARD (Primary Intelligence)              │
│  OSC/UDP over WiFi                                               │
│                                                                  │
│  Per-Channel (50ms):          Main Bus:                         │
│  /meters/1 → RMS, gate GR,   /meters/15 → 100-band RTA         │
│  dyn GR all 32 channels       (via setrtasrc — switchable)      │
│                                                                  │
│  Config (on change):                                            │
│  EQ (4-band), HPF, fader,     /-action/setrtasrc → target RTA  │
│  compressor, gate settings    at any channel or Main L/R        │
└──────────────┬──────────────────────────┬───────────────────────┘
               │                          │
               ▼                          ▼
   [Channel Model]              [RTA Investigation Engine]
   - EQ transfer functions       - Tier 1: Main bus always-on
   - HPF response curves         - Tier 2: Reactive channel scan
   - Contribution curves         - Tier 3: cal command scan
   - Instrument priors           - Returns to Main L/R after each
               │                          │
               └──────────┬───────────────┘
                           │
                    [Forward Mix Model]
                    - predicted = Σ contributions
                    - deviation = measured - predicted
                    - Mix deviation → recommendations
                    - Room deviation → venue profile
                           │
          ┌────────────────┴────────────────┐
          │                                  │
[Reference Mic (Secondary)]        [Recommendation Engine]
AT2035 via PreSonus Studio 26c      - Confidence scoring
- LUFS monitoring (primary use)     - Deficiency response (IMP-044)
- Room acoustic sanity check        - Buildup response
- Geometry-corrected FFT            - Genre profile comparison
- Venue transfer function           - Setlist/song context
  accumulation                      - Baseline drift detection
          │                                  │
          └─────────────────────────────────▶│
                                             │
                                    [Advisory Output]
                                    - Terminal (Phase 1–2)
                                    - UI dashboard (Phase 3)
                                    - Show log (JSON)
```

**Key architecture principles:**
- The X32 board is the primary per-channel intelligence source. It has real data on what each channel is doing at all times.
- The room mic's role is LUFS monitoring and room acoustic characterization — it cannot resolve individual channel contributions from the mixed output.
- The RTA engine (`/meters/15`) provides a single switchable spectrum analyzer. It monitors the main bus continuously and investigates individual channels on demand.
- The forward model combines channel contribution curves (calculated from EQ physics) with actual RMS meter data to predict what the board is outputting. Deviations from the room mic are decomposed into mix problems vs room acoustics.

---

## 6. Core Features

### 6.1 Channel Map Configuration
- One-time setup config file mapping X32 channel numbers to human-readable instrument labels
- Stored per band profile
- Includes vocal usage metadata so engine knows which mics are sparse/open vs actively used
- Channel numbers are placeholders — confirm at first test show

```yaml
band: "The Band Name"
channels:
  # Drums
  1:
    label: "Kick"
    type: instrument
  2:
    label: "Snare"
    type: instrument
  3:
    label: "Hi-Hat"
    type: instrument
  4:
    label: "Overhead L"
    type: instrument
  5:
    label: "Overhead R"
    type: instrument

  # Drum vocal — open but unused most of show
  6:
    label: "Drum Vocal"
    type: vocal
    usage: sparse
    inactive_threshold_db: -35
    lead_songs:
      - "Fight for Your Right"
    backup_songs: []
    notes: "Mic sits open all show — only active for FFYR lead vocal"

  # Bass
  7:
    label: "Bass DI"
    type: instrument

  # Bassist vocal — backup most songs, lead on Kryptonite
  8:
    label: "Bassist Vocal"
    type: vocal
    usage: backup_and_lead
    inactive_threshold_db: -35
    lead_songs:
      - "Kryptonite"
    notes: "Tribute to Brad Arnold — mix this vocal with care on Kryptonite"

  # Guitars — both trade lead and rhythm dynamically
  9:
    label: "Guitar 1"
    type: instrument
    role: shared_lead_rhythm
    notes: "Trades lead/solo with Guitar 2 — solo suppression applies to either"

  10:
    label: "Guitar 2"
    type: instrument
    role: shared_lead_rhythm
    notes: "Trades lead/solo with Guitar 1 — solo suppression applies to either"

  # Keys player — has both keys and guitar channels, one muted at a time
  11:
    label: "Keys"
    type: instrument
    paired_channel: 12
    notes: "Muted when Guitar 3 is active"

  12:
    label: "Guitar 3"
    type: instrument
    paired_channel: 11
    notes: "Keys player rhythm guitar — muted when Keys active"

  # Keys vocal — backup most songs, occasional lead
  13:
    label: "Keys Vocal"
    type: vocal
    usage: backup_and_lead
    inactive_threshold_db: -35
    lead_songs: []           # TBD — confirm songs with band
    notes: "Also sings backup — confirm which songs have keys player on lead"

  # Primary lead vocal
  14:
    label: "Lead Vocal"
    type: vocal
    usage: primary_lead
    priority: very_high
```


### 6.2 Soundcheck Baseline Snapshot
- At soundcheck, script reads and stores full channel state from X32 via OSC:
  - Fader level
  - EQ band settings (frequency, gain, Q for all 4 bands)
  - Compressor settings (threshold, ratio, attack, release)
  - Gate settings
- Baseline stored per show with timestamp
- During show, drift from baseline is tracked and factored into recommendations

### 6.3 Reference Mic Analysis
The room mic serves two specific functions in the current architecture. It is **not** the primary source of per-channel intelligence.

**Function 1 — LUFS monitoring:** Continuous integrated loudness measurement against genre targets. This is the mic's primary use throughout a show.

**Function 2 — Room acoustic characterization:** Geometry-corrected FFT compared against the forward model's predicted spectrum. Systematic deviations (consistent across songs) are captured as the venue's room transfer function and stored in the venue profile for future shows.

**Analysis pipeline:**
- Welch's method FFT (500ms window, 50% overlap) — more stable than single-window FFT
- Interpolated to shared 1000-point log-spaced frequency axis (20Hz–20kHz)
- Geometry correction applied (comb filter notch masking, boundary reinforcement compensation)
- Exponential moving average smoothing (α=0.3)
- 8-band energy summary for LUFS and room acoustic comparison

**What the mic does NOT do:** Resolve individual channel contributions. A room mic receives the summed mix output from all channels simultaneously. The channel-level intelligence comes from X32 OSC meter data and the EQ transfer function model.

### 6.4 X32 Data Acquisition
The X32 is the primary intelligence source. Three data streams:

**Stream 1 — Per-channel meters (`/meters/1`, 50ms):**
- All 32 channel RMS levels simultaneously
- Gate gain reduction and compressor gain reduction per channel
- Used to determine which channels are active and at what level

**Stream 2 — Main bus RTA (`/meters/15`, 50ms):**
- 100-band spectrum, 20Hz–18.66kHz, post-EQ
- Default source: Main L/R (always-on Tier 1 monitoring)
- Source switched via `/-action/setrtasrc` during investigations
- Three operating modes:
  - **Tier 1 (always-on):** Main bus continuous monitoring, triggers investigations
  - **Tier 2 (reactive):** Targeted channel investigation when problem detected, <1 second, returns to Main L/R
  - **Tier 3 (user-triggered):** `cal` command live calibration scan during a song, 3–5 seconds

**Stream 3 — Channel config (on-change via `/xremote`):**
- EQ band settings (type, frequency, gain, Q — 4 bands per channel)
- HPF frequency and slope (`/preamp/hpf`, `/preamp/hpslope`)
- Fader level, mute state
- Compressor and gate settings
- Triggers EQ transfer function recomputation when any config changes

### 6.5 Forward Mix Model
For each active channel, the system computes a frequency-resolved contribution curve:

1. **EQ transfer function** — Mathematically exact from OSC EQ parameters (biquad filter math)
2. **HPF response** — Butterworth filter at the configured cutoff and slope
3. **Instrument prior** — Natural spectral shape of this instrument type before EQ (learned and refined by `cal` scans)
4. **Level scalar** — Post-fade RMS from meter data

Sum all channel contributions → predicted spectrum. Compare to room mic (deviation = mix problems + room acoustics) and to board RTA (deviation = model accuracy). Decompose deviations; generate confidence-gated recommendations.

**Output format:**
```
[HH:MM:SS] CHANNEL — Issue description
  Current state: [relevant EQ/fader info]
  Suggest: [specific actionable adjustment]
```

**Example outputs:**
```
[21:34:12] Guitar 1 — low-mid buildup around 315Hz detected
  Current: EQ Band 2 +1.5dB @ 315Hz, fader at -2dB
  Suggest: EQ Band 2 cut to -1.5dB @ 315Hz or nudge frequency to 250Hz

[21:38:44] Lead Vocal — sitting 3dB hot vs soundcheck baseline
  Current: Fader at +1dB (was -2dB at soundcheck)
  Suggest: Pull fader to -1dB

[21:45:02] Overall — integrated LUFS 4dB above target
  Primary contributors: Guitar 1, Bass DI
  Suggest: Pull main bus -1dB or trim Guitar 1 and Bass DI faders
```

### 6.7 Setlist & Song Context
- Pre-show setlist input (song name, expected duration, annotated events)
- Per-song annotations:
  - Solo events (channel, expected timestamp, expected duration)
  - Dynamic drops/breakdowns (suppress over-sensitivity during quiet passages)
  - Any show-specific notes
- Song library builds over time — annotations persist and improve show to show
- During solos: recommendation engine suppresses flags on the soloing channel and may suggest complementary adjustments (pull supporting instruments to create space)

**Setlist config example:**
```yaml
setlist:
  - song: "Don't Stop Believin'"
    artist: "Journey"
    genre_profile: "AOR"
    duration: 251
    reference_file: null        # future: "references/journey_dont_stop_believin.mp3"
    reference_analyzed: false
    events:
      - type: solo
        channel: "Keys"
        timestamp: 30
        duration: 15
      - type: solo
        channel: "Guitar 1"
        timestamp: 190
        duration: 20

  - song: "Round and Round"
    artist: "Ratt"
    genre_profile: "Glam Metal"
    duration: 240
    reference_file: null        # future: "references/ratt_round_and_round.mp3"
    reference_analyzed: false
    events:
      - type: solo
        channel: "Guitar 1"
        timestamp: 150
        duration: 25

  - song: "Bad Reputation"
    artist: "Joan Jett"
    genre_profile: "Hard Rock"
    duration: 193
    reference_file: null
    reference_analyzed: false
    events: []

  - song: "Highway to Hell"
    artist: "AC/DC"
    genre_profile: "Heavy Rock"
    duration: 208
    reference_file: null
    reference_analyzed: false
    events:
      - type: breakdown
        timestamp: 165
        duration: 10
        note: "Brief pause before final chorus — suppress sensitivity"

  - song: "Breaking the Law"
    artist: "Judas Priest"
    genre_profile: "Heavy Metal"
    duration: 156
    reference_file: null
    reference_analyzed: false
    events:
      - type: solo
        channel: "Guitar 1"
        timestamp: 95
        duration: 20

  - song: "Kryptonite"
    artist: "3 Doors Down"
    genre_profile: "Post-Grunge"
    duration: 234
    reference_file: null
    reference_analyzed: false
    notes: "Tribute to Brad Arnold — bassist on lead vocal, mix with extra care"
    events:
      - type: vocal_lead_change
        channel: "Bassist Vocal"
        timestamp: 0
        note: "Bassist vocal is lead for entire song"

  - song: "Fight for Your Right"
    artist: "Beastie Boys"
    genre_profile: "Party Rock"
    duration: 175
    reference_file: null
    reference_analyzed: false
    events:
      - type: vocal_lead_change
        channel: "Drum Vocal"
        timestamp: 0
        note: "Drummer vocal is lead for entire song — unmute check pre-song"
```

### 6.8 Genre Sound Profiles
- Genre profiles are defined at the **song level** in the setlist — not just the band level
- A cover band playing across multiple subgenres needs the recommendation engine to shift target curves song to song automatically
- When a song starts, the engine loads its tagged genre profile and adjusts all frequency targets, LUFS targets, dynamic range expectations, and instrument weights accordingly
- Band can layer custom overrides on top of any genre template — persisted per band profile and refined over time
- **Phase 4 implication:** genre switching between songs is the trigger for proactive pre-song OSC adjustment suggestions before the first note hits

**Built-in genre library (general):**

| Genre | LUFS Target | Dynamic Range | Character |
|---|---|---|---|
| Blues | -20 | High | Warm low-mid, smooth top end |
| Country | -19 | Medium-High | Clear vocals, bright acoustics |
| Jazz | -22 | Very High | Natural, wide dynamic, airy top |
| R&B / Soul | -18 | Medium | Warm bass, smooth mids, present vocals |
| Hip Hop | -14 | Low | Sub-dominant, punchy kick, clear vocal |
| Pop | -16 | Low-Medium | Bright, vocal-forward, tight low end |
| Acoustic / Folk | -22 | High | Natural, minimal processing character |
| Worship / CCM | -18 | Medium | Vocal clarity priority, full low end |

**Classic rock subgenre profiles (primary for this band):**

| Profile | LUFS Target | Dynamic Range | Examples | Character |
|---|---|---|---|---|
| AOR | -20 | Medium-High | Journey, Foreigner | Vocal-forward, keys prominent, polished, smooth |
| Hard Rock | -18 | Medium | Joan Jett, early Mötley Crüe | Raw, punchy, rhythm-driven, less polish |
| Glam Metal | -18 | Medium | Ratt, Poison, later Mötley Crüe | Guitar-aggressive, big vocals, energetic |
| Heavy Rock | -17 | Medium | AC/DC | Massive rhythm guitar, locked groove, driving |
| Heavy Metal | -16 | Low-Medium | Judas Priest, Pantera | Tight, precise, aggressive high-mid, punchy kick |

**Profile details:**
```yaml
profiles:

  AOR:
    target_lufs: -20
    dynamic_range: medium-high
    frequency_targets:
      sub_bass:    -8
      bass:        +1
      low_mid:      0
      mid:         +2     # keys and vocal forward
      high_mid:    +1
      presence:    +2     # vocal clarity priority
      air:         -1
    instrument_weights:
      Lead Vocal:   priority: very_high
      Keys:         priority: high
      Guitar 1:     priority: medium    # supportive not dominant
    notes: "Optimize for vocal intelligibility above all else"

  Hard Rock:
    target_lufs: -18
    dynamic_range: medium
    frequency_targets:
      sub_bass:    -6
      bass:        +2     # punchy, present
      low_mid:     -1
      mid:         +2     # raw guitar body
      high_mid:    +2
      presence:    +1
      air:         -1
    instrument_weights:
      Guitar 1:     priority: very_high
      Lead Vocal:   priority: high
      Keys:         priority: low

  Glam Metal:
    target_lufs: -18
    dynamic_range: medium
    frequency_targets:
      sub_bass:    -6
      bass:        +1
      low_mid:     -1     # slight scoop
      mid:         +1
      high_mid:    +3     # guitar aggression and pick attack
      presence:    +2
      air:          0
    instrument_weights:
      Guitar 1:     priority: very_high
      Lead Vocal:   priority: very_high
      Kick:
        low_end_target: 80Hz
        acceptable_weight: medium-high

  Heavy Rock:
    target_lufs: -17
    dynamic_range: medium
    frequency_targets:
      sub_bass:    -5
      bass:        +2
      low_mid:      0
      mid:         +2     # rhythm guitar dominates
      high_mid:    +2
      presence:    +1
      air:         -1
    instrument_weights:
      Guitar 1:     priority: very_high
      Guitar 2:     priority: very_high
      Lead Vocal:   priority: high
      Keys:         priority: none      # typically absent
    notes: "Two guitar blend is critical — watch for frequency masking between Guitar 1 and Guitar 2"

  Heavy Metal:
    target_lufs: -16
    dynamic_range: low-medium
    frequency_targets:
      sub_bass:    -4
      bass:        +1
      low_mid:     -2     # tight, scooped
      mid:         +1
      high_mid:    +4     # aggression, pick attack, Dimebag scoop range
      presence:    +2
      air:         +1
    instrument_weights:
      Guitar 1:     priority: very_high
      Kick:
        low_end_target: 60Hz
        acceptable_weight: very_high    # double kick territory
      Lead Vocal:   priority: high
    notes: "Pantera songs — watch for 800Hz-1kHz mid scoop on Guitar 1 as intentional tone characteristic, not a problem"
```

**Band customization layer:**
```yaml
band: "The Band Name"
custom_overrides:
  AOR:
    notes: "Their keys player runs slightly brighter — allow +1dB air vs template"
  Heavy Metal:
    frequency_targets:
      high_mid: +3        # dial back slightly from template for their guitarist's natural tone
  global:
    notes: "Kick consistently runs heavy across all profiles — allow extra weight below 80Hz"
```

### 6.9 Soundcheck & Baseline Mode

Baseline mode is a distinct operating mode run **before the show** during soundcheck. Its sole purpose is helping the engineer dial in a solid starting point across all channels relative to the active genre profile. No per-song logic, no setlist context — just the room, the band, and the target curve.

**Baseline mode workflow:**

1. Engineer launches script in `--baseline` mode
2. Band plays through each instrument individually (standard soundcheck order)
3. Script listens via reference mic and reads X32 channel state simultaneously
4. For each channel, script compares current EQ and level against the genre profile target and logs specific recommendations
5. Engineer makes adjustments on tablet, script re-reads and confirms improvement
6. Once all channels are dialed, band plays together — script assesses combined mix vs genre curve
7. Final baseline snapshot saved — this becomes the reference for the show

**Soundcheck recommendation output:**
```
BASELINE MODE — Genre: AOR (Journey, Foreigner)
Target LUFS: -20 | Dynamic Range: Medium-High

[Kick] — Low end slightly light vs AOR target
  Current: Fader -3dB, EQ Band 1 flat @ 80Hz
  Suggest: EQ Band 1 +2dB @ 70Hz for more punch
  Confirm: Re-check after bass plays together

[Guitar 1] — High-mid 3dB hot vs AOR profile
  Current: EQ Band 3 +3dB @ 3kHz
  Suggest: Pull Band 3 to +1dB — AOR profile favors smooth not aggressive
  Note: Will reassess when both guitars play together

[Lead Vocal] — Well balanced, sitting within AOR target range
  Current: Fader -1dB, presence shelf +1dB @ 8kHz
  Status: No adjustment needed

[Combined Mix] — Low-mid buildup around 300Hz with full band
  Primary contributors: Guitar 1, Guitar 2, Bass DI overlapping
  Suggest: High-pass Guitar 2 at 120Hz, cut Bass DI Band 2 -2dB @ 250Hz
```

**Key behaviors:**
- Recommendations are iterative — after each adjustment the engineer confirms and script re-assesses
- Open/sparse mics (Drum Vocal, Bassist Vocal) flagged if they're contributing noise floor above threshold
- Paired channels (Keys/Guitar 3) — script reminds engineer to check both states during soundcheck
- Baseline snapshot locked when engineer types `confirm` — saved as show reference
- Any channel not assessed during soundcheck is flagged as unverified in the show log


### 6.10 Show Log & Comparison Report

The show log is a continuous, passive record of everything that happens during the show — both what the script recommended and what the engineer actually did. Since the OSC polling loop is already watching all board state, manual adjustments are captured automatically by diffing snapshots between polling cycles. No manual logging required.

**Event types captured:**

| Event Type | Source | Description |
|---|---|---|
| `RECOMMENDATION` | Script | Advisory output generated by recommendation engine |
| `MANUAL_ADJUSTMENT` | X32 poll diff | Engineer-initiated board change detected via OSC |
| `BASELINE_DRIFT` | X32 poll diff | Channel drifted from soundcheck snapshot |
| `SPARSE_MIC_ACTIVE` | X32 poll diff | Open/unused mic crossed activity threshold |
| `GENRE_TRANSITION` | Setlist | Active song genre profile changed |
| `SOLO_WINDOW` | Setlist | Solo suppression window opened or closed |
| `FEEDBACK_SPIKE` | Reference mic | Rapid high-frequency spike detected |

**Log entry format:**
```json
{
  "timestamp": "21:34:12",
  "event": "RECOMMENDATION",
  "channel": "Guitar 1",
  "detail": "Low-mid buildup around 315Hz detected",
  "current_state": { "eq_band_2_gain": 1.5, "eq_band_2_freq": 315, "fader": -2.0 },
  "suggestion": "EQ Band 2 cut to -1.5dB @ 315Hz",
  "genre_profile": "Glam Metal",
  "song": "Round and Round"
}

{
  "timestamp": "21:34:48",
  "event": "MANUAL_ADJUSTMENT",
  "channel": "Guitar 1",
  "before": { "eq_band_2_gain": 1.5, "eq_band_2_freq": 315 },
  "after":  { "eq_band_2_gain": -0.5, "eq_band_2_freq": 315 },
  "prior_recommendation": "21:34:12",
  "recommendation_delta": "Suggested -2dB, applied -2dB (match)",
  "lag_seconds": 36
}

{
  "timestamp": "21:52:18",
  "event": "MANUAL_ADJUSTMENT",
  "channel": "Guitar 2",
  "before": { "fader": -2.0 },
  "after":  { "fader": 1.0 },
  "prior_recommendation": null,
  "context": "Engineer-initiated, no prior recommendation — possible blind spot"
}
```

**Post-show comparison report** generated automatically at session end:

```
FOH ASSISTANT — SHOW REPORT
Date: 2026-05-10 | Venue: [Venue Name] | Band: [Band Name]
Genre profiles active: AOR, Glam Metal, Hard Rock, Heavy Rock
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RECOMMENDATION ACCURACY
  Total recommendations:        24
  Matched by manual adjustment: 16  (67%)
  Partially matched:             4  (17%)
  Ignored:                       4  (17%)

ENGINEER-INITIATED ADJUSTMENTS (no prior recommendation)
  Total:                        11
  Potential blind spots:         8  — review these
  Solo/intentional moves:        3  — correctly suppressed

RECOMMENDATION LAG
  Average time to manual match: 42 seconds
  Fastest:                       8 seconds
  Slowest:                     3:12 (may have been ignored then reconsidered)

CHANNELS WITH MOST ACTIVITY
  Guitar 1:     8 events   (4 recommendations, 4 manual)
  Lead Vocal:   6 events   (3 recommendations, 3 manual)
  Bass DI:      4 events   (1 recommendation, 3 manual — possible blind spot)

BASELINE DRIFT SUMMARY
  Guitar 2 fader:    +3dB drift from soundcheck by end of show
  Keys fader:        +1.5dB drift
  All other channels within ±1dB of baseline

SPARSE MIC EVENTS
  Drum Vocal crossed threshold during "Fight for Your Right" ✓ (expected)
  Bassist Vocal crossed threshold during "Kryptonite" ✓ (expected)
  No unexpected sparse mic activity

FEEDBACK EVENTS
  None detected
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Full event log saved to: shows/2026-05-10_show.json
```

**What the comparison data tells you over time:**
- **Recommendation accuracy trending up** → engine logic is improving
- **Consistent blind spots on a channel** → add frequency fingerprint or threshold adjustment for that instrument
- **High lag time on matched recommendations** → script is detecting problems too slowly, tighten analysis window
- **Engineer adjustments the script never flagged** → most valuable data for tuning the engine



---

## 7. Advisory Mode (MVP)

All recommendations are **read-only advisory only** in v1. The script:

- Does NOT send any OSC commands to the X32
- Does NOT modify any channel settings
- Outputs recommendations to terminal only
- Engineer reads recommendations and executes manually on tablet

This is intentional. Trust must be established before automation is introduced.

---

## 8. Phases / Roadmap

### Phase 1 — MVP ✅ Complete (May 2026)
- [x] OSC connection to X32, read channel fader and EQ state
- [x] Audio capture from DJI Mic 2 USB receiver
- [x] Real-time LUFS and frequency band analysis
- [x] Static channel map config with vocal usage metadata
- [x] Genre profile library (AOR, Hard Rock, Glam Metal, Heavy Rock, Heavy Metal, Post-Grunge, Party Rock)
- [x] Soundcheck mode (`--soundcheck`) with continuous advisory and `confirm` baseline lock
- [x] Show mode — recommendation engine with genre-aware frequency analysis
- [x] Sparse mic handling
- [x] Manual song markers (`n` = next song, `p` = previous, `e` = end early)
- [x] Show log saved to JSON per show
- [x] Manual adjustment detection via OSC poll diff
- [x] Post-show comparison report
- [x] Named EQ moves, Q guidance, psychoacoustic band weighting
- [x] Multi-factor culprit scoring (fingerprint + RMS + EQ boost in band)
- [x] Frequency fingerprint corrections per instrument

**Show 1 — May 9, AJ's Bar:** System ran for full show. LUFS recommendations worked. Channel-level recommendations did not fire — DJI Mic 2 could not resolve individual channels from room mix. House PA configuration (DJ/hip-hop low-end boost) was the primary show problem, corrected at the PA level. Architecture shift initiated.

### Phase 2 — Forward Model + RTA Intelligence (Current — Target: June 2026)

**Board model (IMPL_X32_Board_Model.md):**
- [ ] Extended OSC data acquisition — preamp, comp, gate details
- [ ] `/meters/15` subscription alongside `/meters/1`
- [ ] `channel_model.py` — EQ transfer functions (all band types), HPF response, contribution curves
- [ ] Instrument prior system with per-state shapes (normal, solo_active, etc.)
- [ ] Input state inference per channel (solo detection, gating)

**Mic analyzer (IMPL_Mic_Analyzer.md):**
- [x] AT2035 + PreSonus Studio 26c device detection
- [ ] Welch's method FFT (replaces single-window)
- [ ] Geometry-corrected spectrum via venue acoustics module
- [ ] EMA smoothing, peak detection within bands

**Venue geometry (IMPL_Geometry.md):**
- [ ] `core/geometry.py` — comb filter, room modes, boundary gain, phase calculations
- [ ] Venue profile YAML schema and loader
- [ ] `--setup-venue` measurement wizard
- [ ] AJ's Bar profile, June 13 outdoor patio profile

**Forward mix model (IMPL_Forward_Mix_Model.md):**
- [ ] `core/forward_model.py` — predicted spectrum, deviation decomposition
- [ ] R² correlation metrics vs board RTA and mic
- [ ] Channel contribution scoring per band
- [ ] Confidence scoring gate for recommendations
- [ ] **Passive mode for June 13 show** — logs everything, fires no new recommendations
- [ ] Enhanced log schema (ANALYSIS_CYCLE, INPUT_STATE_EVENT, CONFIG_CHANGE events)

**RTA investigation engine (IMP-043):**
- [ ] `/-action/setrtasrc` control — RTA state machine (MAIN_BUS / INVESTIGATING / CALIBRATING)
- [ ] Tier 1: Main bus continuous 100-band spectrum monitoring
- [ ] Tier 2: Reactive targeted channel investigation (<1 second)
- [ ] `cal` keyboard command — live calibration scan (IMP-045)

**Deficiency response (IMP-044):**
- [ ] Cause classification (overall level / channel shortfall / room absorption)
- [ ] Proportional per-channel boost calculation (fader vs EQ decision)
- [ ] Sequenced output format with "apply in order" language

**Other Phase 2 items:**
- [ ] HPF detection fix — use `hpf_freq_hz > 22Hz` not `hpon` flag (IMP-026)
- [ ] Venue profile system with AJ's Bar PA checklist (IMP-027)
- [ ] Ambient noise baseline capture (IMP-028)
- [ ] X32 channel name pull from board (IMP-030)

**June 13 validation targets:** Forward model R² (predicted vs board RTA) > 0.70. If met → activate channel-level recommendations for the following show.

### Phase 3 — UI + Two-Mic Geometry
- [ ] Web-based dashboard (local, runs on laptop) with real-time spectrum display
- [ ] Recommendations panel with dismiss/acknowledge
- [ ] Setlist management and venue management UI
- [ ] Second mic channel (boundary/PZM on PreSonus Ch2) — IMP-042
- [ ] Two-point room transfer function (coverage verification, room mode prediction from geometry)
- [ ] Solo preset commands (`1`, `2`, `3` keys for guitar/keys solos) — IMP-D03
- [ ] Show replay simulator for offline validation — IMP-D02

### Phase 4 — Automation (Opt-in)
- [ ] OSC write capability (fader adjustments only initially)
- [ ] Configurable automation rules with hard safety limits (max ±2dB per cycle)
- [ ] Manual override always available
- [ ] Automated solo mix — boost pedal detection triggers preset (IMP-D04)
- [ ] Proactive genre switching — suggest pre-song adjustments before first note

### Phase 5 — Intelligence Layer
- [ ] Small local ML model trained on show log correction patterns
- [ ] Pre-show suggestions from historical data
- [ ] LLM (Claude API) conversational layer backed by internal model state
- [ ] Band profile export/import
- [ ] Community genre profile sharing

### Phase 6 — Reference Audio Targeting
- [ ] Per-song reference audio files analyzed locally to extract frequency targets
- [ ] Reference curve replaces genre template when available
- [ ] Cover band delta tracking (consistent differences vs studio recording)
- [ ] Reference analysis is local-only — no audio transmitted or reproduced

---

## 9. Technical Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| OSC Communication | `python-osc` library |
| Audio Capture | `sounddevice` |
| Audio Analysis | `numpy`, `scipy` (Welch FFT, filter math), `pyloudnorm` (LUFS) |
| Config Files | YAML |
| Show Logs | JSON per show |
| UI (Phase 3) | React + Vite (served locally) |
| Data Persistence | Local JSON files (Phase 1–3), SQLite (Phase 4+) |

**IMPL documents (Claude Code implementation references):**
- `IMPL_X32_Board_Model.md` — OSC data acquisition, channel model, EQ transfer functions
- `IMPL_Mic_Analyzer.md` — Audio capture, Welch FFT, geometry correction
- `IMPL_Geometry.md` — Venue acoustics, room modes, comb filtering, boundary gain
- `IMPL_Forward_Mix_Model.md` — Forward model, deviation analysis, enhanced logging

---

## 10. Safety Requirements

- Script MUST never send OSC write commands in Phase 1-2
- All future automation adjustments capped at ±2dB per recommendation cycle
- Automation requires explicit opt-in per session
- Any automated adjustment logged immediately with pre/post state
- Engineer can disable automation instantly from UI or keyboard shortcut
- Feedback spike detection: if reference mic detects rapid high-frequency spike, automation freezes and alerts immediately

---

## 11. Open Questions

- [ ] June 13 outdoor patio — what are the actual PA distances for geometry setup? (measure on arrival with laser rangefinder)
- [ ] Calibration scan (`cal`) — what's the right `α` learning rate? Starting at 0.1; tune from first two shows.
- [ ] Should the `cal` scan update priors in real time or queue updates for engineer approval?
- [ ] Forward model R² validation — if below 0.70 at June 13, what's the diagnosis protocol?
- [x] ~~What is the X32 network IP at typical show venues?~~ — **Resolved:** Static IP assigned, confirmed at AJ's Bar.
- [x] ~~Does X32 OSC support subscribing to meter updates?~~ — **Resolved:** Yes, `/batchsubscribe` with 10-second timeout, renewed every 8 seconds.
- [x] ~~What sample rate does the DJI USB receiver expose?~~ — **Resolved:** AT2035 via PreSonus Studio 26c at 48000Hz is the Phase 2 standard.
- [x] ~~HPF on/off address~~ — **Resolved:** `/preamp/hpon` is phantom power, not HPF. HPF state inferred from `/preamp/hpf` frequency > 22Hz.
- [x] ~~Does the band use two guitarists simultaneously?~~ — **Resolved:** Both active simultaneously, trade lead/solo dynamically. Both channels always open.

---

## 12. Success Criteria

### Phase 1 ✅ Met
- Script connected to X32 and read channel state without errors
- DJI USB receiver recognized as audio input device
- Genre profile loaded and applied to recommendation engine
- Soundcheck mode produced actionable per-channel EQ recommendations
- Manual adjustments detected via OSC poll diff
- Show JSON log readable and complete post-show
- LUFS recommendations fired correctly throughout 3-hour show

### Phase 2 (June 13 Validation Show)
- Forward model R² (predicted vs board RTA) > 0.70
- Forward model R² (predicted vs mic) > 0.55
- Input state events (solo onsets) confirmed by mic > 65%
- Analysis cycles logged > 15,000 (full show coverage)
- Geometry correction reduces systematic mic deviation at known comb notch frequencies
- `cal` command completes within 5 seconds and updates instrument priors
- HPF detection accurate using frequency-based inference (no false negatives)

### Phase 2 (Activation — Show after June 13)
- Channel-level recommendations activate if R² > 0.70 on June 13
- Deficiency recommendations sequenced correctly (no simultaneous multi-channel boosts)
- RTA investigation scans complete in <1 second and return to Main L/R correctly
- No RTA state machine lockups (watchdog fires correctly if stuck >8s)

---

*This document is a living spec. Update version number and Last Updated date with each revision.*
