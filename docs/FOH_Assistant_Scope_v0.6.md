# FOH Assistant — Scope & Requirements
**Version:** 0.6  
**Status:** Draft  
**Last Updated:** 2026-05-03  
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

| Component | Details |
|---|---|
| Mixing Console | Behringer X32 |
| Control Surface | Tablet running X32-Q or X32-Mix app |
| Reference Mic | DJI Mic 2 (clipped mid-room, ear height, audience position) |
| Audio Interface | DJI USB receiver (plugged into laptop) |
| Host Machine | Laptop on same WiFi network as X32 |
| Network | Local WiFi — X32 acts as access point or joins local network |

---

## 5. System Architecture

```
[DJI Mic 2 - Room Reference]
         |
   [DJI USB Receiver]
         |
      [Laptop]
         |
   ┌─────┴──────────────────────┐
   │                            │
[Audio Analysis]         [OSC Client]
 - LUFS/RMS                     |
 - FFT frequency bands    [X32 over UDP]
 - Rate of change          - Channel fader levels
                           - Channel EQ state (4-band)
                           - Channel compressor state
                           - Channel gate state
                           - Bus/main levels
   └─────────────┬──────────────┘
                 │
        [Recommendation Engine]
         - Frequency fingerprint matching
         - Channel map labeling
         - Genre profile comparison
         - Setlist/song context
         - Solo flag suppression
         - Baseline drift detection
                 │
        [Advisory Output]
         - Terminal (MVP)
         - Simple UI (v2)
         - Show report/log
```

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
- Continuous audio capture from DJI USB receiver
- Real-time analysis:
  - Integrated LUFS (overall loudness)
  - Short-term RMS
  - FFT breakdown by frequency band:
    - Sub bass: 20–80Hz
    - Bass: 80–250Hz
    - Low mid: 250–500Hz
    - Mid: 500Hz–2kHz
    - High mid: 2–6kHz
    - Presence: 6–12kHz
    - Air: 12kHz+
- Rate of change tracking to distinguish intentional moves from gradual drift

### 6.4 X32 Channel Meter Polling
- Poll X32 for per-channel RMS levels via OSC at configurable interval (default: 500ms)
- Correlate channel meter data with frequency band issues detected by reference mic
- Identify likely contributing channels to a detected problem

### 6.5 Frequency Fingerprint Map
- Per-instrument frequency fingerprints used to assist channel-to-frequency correlation:

```yaml
fingerprints:
  Kick:        primary: 60-80Hz,   secondary: 2-5kHz
  Snare:       primary: 150-250Hz, secondary: 5-8kHz
  Hi-Hat:      primary: 8-12kHz
  Guitar 1:    primary: 200-5kHz
  Guitar 2:    primary: 200-5kHz
  Bass DI:     primary: 80-250Hz,  secondary: 500Hz-1kHz
  Keys:        primary: 200Hz-8kHz
  Lead Vocal:  primary: 300Hz-4kHz, secondary: 1-4kHz presence
  Harmony Vocal: primary: 300Hz-4kHz
  Overheads:   primary: 5kHz+
```

### 6.6 Recommendation Engine
Combines reference mic analysis, channel meter data, EQ state, and frequency fingerprints to generate labeled recommendations.

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

### Phase 1 — MVP (Target: Next Show)
- [ ] OSC connection to X32, confirm read access
- [ ] Read channel fader levels and EQ state
- [ ] Read channel mute states (Keys/Guitar 3 paired channel detection)
- [ ] Audio capture from DJI USB receiver
- [ ] Real-time LUFS and frequency band analysis
- [ ] Static channel map config with vocal usage metadata
- [ ] Genre profile library (built-in templates — general + 5 classic rock + Post-Grunge + Party Rock)
- [ ] Band profile config with per-song genre tagging and custom overrides
- [ ] **Baseline / Soundcheck mode** (`--baseline` flag)
  - [ ] Per-channel assessment against genre target curve
  - [ ] Iterative re-assessment after engineer adjustments
  - [ ] Combined mix assessment with full band
  - [ ] Baseline snapshot saved on engineer confirmation
- [ ] Show mode — recommendation engine loads active song genre profile dynamically
- [ ] Sparse mic handling — ignore open unused mics below inactive threshold
- [ ] Show log saved to file (JSON per show)
- [ ] Manual adjustment detection via OSC poll diff
- [ ] Recommendation-to-adjustment correlation and tagging
- [ ] Post-show comparison report generated at session end

### Phase 2 — Setlist Integration
- [ ] Setlist config file support
- [ ] Per-song solo/event annotations
- [ ] Solo suppression logic
- [ ] Song library persistence across shows
- [ ] Soundcheck baseline snapshot

### Phase 3 — Simple UI
- [ ] Web-based dashboard (local, runs on laptop)
- [ ] Real-time level meters and frequency display
- [ ] Recommendations panel with dismiss/acknowledge
- [ ] Setlist management UI
- [ ] Band/channel map management

### Phase 4 — Automation (Opt-in)
- [ ] OSC write capability (fader adjustments only initially)
- [ ] Configurable automation rules with hard safety limits
- [ ] Per-adjustment size caps (e.g. max ±2dB per cycle)
- [ ] Manual override always available
- [ ] Automation log separate from advisory log
- [ ] **Proactive genre switching** — when song changes genre profile, suggest pre-song EQ/fader adjustments before first note hits
- [ ] Opt-in auto-apply of genre transition adjustments with confirmation prompt

### Phase 5 — Intelligence Layer
- [ ] Multi-show pattern recognition per band
- [ ] Pre-show suggestions based on historical data
- [ ] LLM integration for complex mix advisory
- [ ] Band profile export/import
- [ ] Community genre profile sharing (user-submitted profiles)

### Phase 6 — Reference Audio Targeting
- [ ] Per-song reference audio file support (local MP3/WAV/FLAC)
- [ ] One-time reference track analysis pipeline:
  - Integrated LUFS measurement
  - Per-band frequency curve extraction
  - Section detection (verse/chorus/full-band identification)
  - Full-band section weighted as primary target
  - Curve saved to `curves/<song_slug>.json` — not re-analyzed unless file changes
- [ ] Reference curve replaces genre template as recommendation target when available
- [ ] Cover band delta tracking — logs consistent differences between band's live sound and reference recording (instrument tone signatures, not just levels)
- [ ] Pre-show reference curve summary printed at session start:
  ```
  Reference targets loaded:
    Don't Stop Believin' → Journey studio curve (LUFS -19.8)
    Round and Round      → Ratt studio curve (LUFS -18.2)
    Bad Reputation       → genre fallback (Hard Rock) — no reference file
  ```
- [ ] Reference audio never transmitted, reproduced, or stored beyond local analysis curve
- [ ] Terms of service note: reference analysis is local-only, equivalent to using a spectrum analyzer

**Reference song data model (Phase 6 ready, stubbed from Phase 1):**
```yaml
- song: "Round and Round"
  artist: "Ratt"
  album: "Out of the Cellar"
  year: 1984
  genre_profile: "Glam Metal"          # fallback if no reference file
  reference_file: "references/ratt_round_and_round.mp3"
  reference_analyzed: true             # set true after first analysis run
  reference_curve: "curves/ratt_round_and_round.json"
  reference_lufs: -18.2
  reference_notes: "Warren DeMartini tone — heavy @ 3.2kHz, tight low-mid"
  cover_band_delta:                    # populated after show data accumulates
    Guitar_1_high_mid: +1.8dB         # band consistently runs hotter here
    Bass_DI_low_end: -1.2dB           # band runs lighter in sub vs record
```

---

## 9. Technical Stack (Proposed)

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| OSC Communication | `python-osc` library |
| Audio Capture | `sounddevice` |
| Audio Analysis | `numpy`, `scipy` (FFT), `pyloudnorm` (LUFS) |
| Config Files | YAML |
| Show Logs | CSV + JSON |
| UI (Phase 3) | React + Vite (served locally) or simple terminal UI |
| Data Persistence | Local JSON files (Phase 1-3), SQLite (Phase 4+) |

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

- [ ] What is the X32 network IP at typical show venues? (confirm at next show)
- [ ] Does the X32 OSC protocol support subscribing to channel meter updates or does it require polling?
- [ ] What sample rate does the DJI USB receiver expose to the OS?
- [ ] Should the channel map support aux sends and bus routing visibility?
- [ ] Multi-band compressor channels — worth reading/recommending on, or out of scope for MVP?
- [ ] Should genre profiles be community-shareable in a later phase? (user-submitted genre/band profiles)
- [x] ~~What genre does the band primarily play?~~ — **Resolved:** Classic rock cover band spanning AOR, Hard Rock, Glam Metal, Heavy Rock, and Heavy Metal subgenres. Song-level genre tagging adopted.
- [ ] Does the band use two guitarists simultaneously or one at a time? (affects Guitar 1 / Guitar 2 blend recommendations)
- [ ] Do Judas Priest / Pantera songs use the same channel assignments as AOR songs or does the band reconfigure for heavier material?

---

## 12. Success Criteria (Phase 1)

- Script connects to X32 and reads channel state without errors
- DJI USB receiver recognized as audio input device
- Genre profile loads and target curve is applied to recommendation engine
- Baseline mode produces actionable per-channel recommendations during soundcheck
- Baseline snapshot saved and used as show reference
- Manual adjustments detected automatically via OSC poll diff
- Recommendations and manual adjustments logged with correlation tagging
- Post-show comparison report generated with accuracy metrics
- Recommendations reference genre context where relevant
- No false positives during intentional level changes or solo windows
- Show JSON log readable and complete post-show
- **Target accuracy benchmark (Show 1):** 50%+ recommendations matched by manual adjustment
- **Target blind spot benchmark (Show 1):** Engineer-initiated adjustments with no prior recommendation reviewed and used to improve engine by Show 2

---

*This document is a living spec. Update version number and Last Updated date with each revision.*
