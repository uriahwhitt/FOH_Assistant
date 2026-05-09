# FOH Assistant — Phase 1 Implementation Plan
**Version:** 1.0  
**Status:** Active  
**Last Updated:** 2026-05-03  
**Author:** Uriah Whittemore  
**Reference:** FOH_Assistant_Scope_v0.6.md

---

## Overview

Phase 1 delivers a working terminal-based advisory tool that:
1. Connects to a Behringer X32 over OSC and reads board state
2. Captures room audio from a DJI Mic 2 USB receiver
3. Analyzes room loudness and frequency balance in real time
4. Compares analysis against a genre profile target curve
5. Outputs labeled, actionable recommendations to terminal
6. Logs all recommendations and manual board adjustments to a JSON show file
7. Generates a post-show comparison report

**No OSC write commands are sent in Phase 1. The board is read-only.**

---

## Constraints

- Python 3.11+
- Runs on Windows laptop (terminal / Git Bash)
- X32 and laptop on same local WiFi network
- DJI Mic 2 USB receiver as audio input device
- All config via YAML files — no UI
- All output to terminal + JSON log file
- Advisory only — zero board writes

---

## Project Structure

```
foh-assistant/
├── main.py                  # Entry point — mode selection and session orchestration
├── config/
│   ├── band.yaml            # Band profile, channel map, vocal metadata
│   ├── genres/
│   │   ├── aor.yaml
│   │   ├── hard_rock.yaml
│   │   ├── glam_metal.yaml
│   │   ├── heavy_rock.yaml
│   │   ├── heavy_metal.yaml
│   │   ├── post_grunge.yaml
│   │   └── party_rock.yaml
│   └── setlist.yaml         # Show setlist with per-song genre tags (optional Phase 1)
├── core/
│   ├── osc_client.py        # X32 OSC connection, polling, state snapshots
│   ├── audio_capture.py     # DJI USB receiver input, audio stream management
│   ├── analyzer.py          # LUFS, RMS, FFT frequency band analysis
│   ├── recommender.py       # Recommendation engine — compares state vs genre target
│   └── logger.py            # Event logging, poll diff detection, show log writer
├── models/
│   ├── channel.py           # Channel state dataclass
│   ├── genre_profile.py     # Genre profile dataclass
│   └── event.py             # Log event dataclass
├── shows/                   # Auto-created — one JSON file per show session
└── requirements.txt
```

---

## Module Specifications

### main.py
Entry point. Parses CLI args and orchestrates the session.

**Modes:**
- `python main.py --baseline` — Soundcheck/baseline mode
- `python main.py --show` — Live show advisory mode
- `python main.py --devices` — List available audio input devices and exit
- `python main.py --test-osc` — Test X32 connection and print channel state, then exit

**Startup sequence (both modes):**
1. Load `config/band.yaml`
2. Load genre profiles from `config/genres/`
3. Load `config/setlist.yaml` if present
4. Initialize OSC client — confirm X32 connection
5. Initialize audio capture — confirm DJI receiver visible
6. Print session header to terminal
7. Hand off to baseline or show mode orchestrator

**Session header example:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FOH ASSISTANT v0.1 — Phase 1
Band:    The Band Name
Mode:    SHOW
Genre:   AOR (default — no setlist active song)
X32:     192.168.0.1:10023 ✓ connected
Audio:   DJI Mic 2 USB Receiver ✓ active
Log:     shows/2026-05-10_show.json
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

### core/osc_client.py
Manages all X32 communication. Read-only in Phase 1.

**Dependencies:** `python-osc`

**Responsibilities:**
- Establish UDP connection to X32 (`/xremote` subscription + keepalive)
- Poll channel state on configurable interval (default 500ms)
- Return structured `ChannelState` snapshots
- Detect state changes between polls (diff engine)
- Expose current board state to other modules

**X32 OSC addresses to read:**

| Data | OSC Address Pattern |
|---|---|
| Channel fader | `/ch/{nn}/mix/fader` |
| Channel mute | `/ch/{nn}/mix/on` |
| Channel EQ band gain | `/ch/{nn}/eq/{b}/g` |
| Channel EQ band freq | `/ch/{nn}/eq/{b}/f` |
| Channel EQ band Q | `/ch/{nn}/eq/{b}/q` |
| Channel EQ band type | `/ch/{nn}/eq/{b}/t` |
| Channel compressor threshold | `/ch/{nn}/dyn/thr` |
| Channel compressor ratio | `/ch/{nn}/dyn/rat` |
| Channel gate threshold | `/ch/{nn}/gate/thr` |
| Main LR fader | `/main/st/mix/fader` |
| Channel RMS meter | `/meters/1` (batch meter blob) |

**Key behaviors:**
- Keepalive: send `/xremote` every 8 seconds or X32 stops responding
- On connection failure: retry 3x then exit with clear error message
- Channel numbers zero-padded to 2 digits: ch 1 = `/ch/01/`
- Fader values from X32 are 0.0–1.0 float — convert to dB using X32 taper curve
- Meter blob requires parsing — returns all channel RMS levels in one message

**X32 fader float-to-dB conversion:**
```python
import math

def fader_to_db(val: float) -> float:
    """Convert X32 fader float (0.0-1.0) to dB value."""
    if val <= 0:
        return -90.0
    elif val < 0.25:
        return -60.0 + (val / 0.25) * 50.0      # -60 to -10
    elif val < 0.5:
        return -10.0 + ((val - 0.25) / 0.25) * 10.0  # -10 to 0
    elif val < 0.75:
        return 0.0 + ((val - 0.5) / 0.25) * 5.0      # 0 to +5
    else:
        return 5.0 + ((val - 0.75) / 0.25) * 5.0     # +5 to +10
```

**ChannelState snapshot (per poll):**
```python
@dataclass
class ChannelState:
    channel_num: int
    label: str                    # from band.yaml channel map
    fader_db: float
    muted: bool
    eq: list[EQBand]             # 4 bands
    comp_threshold: float
    comp_ratio: float
    gate_threshold: float
    rms_db: float                 # from meter blob
    timestamp: float              # unix timestamp
```

---

### core/audio_capture.py
Manages the DJI USB receiver audio stream.

**Dependencies:** `sounddevice`, `numpy`

**Responsibilities:**
- Enumerate available audio input devices on startup
- Identify DJI receiver by device name (partial match: "DJI")
- Open audio stream at device's native sample rate
- Provide rolling audio buffer to analyzer on request
- Handle device disconnection gracefully

**Key behaviors:**
- Buffer size: 2 seconds of audio (rolling)
- If DJI device not found: print available devices and exit with clear message
- Sample rate: use device native rate (likely 48000Hz)
- Channels: mono (1) — reference mic is single channel

**Device listing (--devices mode):**
```
Available audio input devices:
  [0] Microphone (Realtek HD Audio)     — 44100Hz, 2ch
  [1] DJI Mic 2 USB Receiver            — 48000Hz, 1ch  ← use this
  [2] Stereo Mix (Realtek HD Audio)     — 44100Hz, 2ch
```

---

### core/analyzer.py
Processes the audio buffer and returns frequency analysis results.

**Dependencies:** `numpy`, `scipy`, `pyloudnorm`

**Responsibilities:**
- Compute integrated LUFS from rolling buffer
- Compute short-term RMS (300ms window)
- Compute FFT and bin energy into frequency bands
- Compute rate of change vs previous analysis cycle
- Return structured `RoomAnalysis` result

**Frequency bands:**

| Band | Range | Label |
|---|---|---|
| Sub bass | 20–80Hz | `sub_bass` |
| Bass | 80–250Hz | `bass` |
| Low mid | 250–500Hz | `low_mid` |
| Mid | 500Hz–2kHz | `mid` |
| High mid | 2–6kHz | `high_mid` |
| Presence | 6–12kHz | `presence` |
| Air | 12kHz+ | `air` |

**RoomAnalysis dataclass:**
```python
@dataclass
class RoomAnalysis:
    lufs: float                        # integrated LUFS
    rms_db: float                      # short-term RMS
    bands: dict[str, float]            # band label → dB level
    band_delta: dict[str, float]       # change vs previous cycle
    lufs_delta: float                  # LUFS change vs previous cycle
    timestamp: float
```

**Analysis cycle:** run every 1 second (configurable)

---

### core/recommender.py
The recommendation engine. Compares room analysis and board state against the active genre profile and generates advisory output.

**Dependencies:** none external

**Inputs per cycle:**
- Current `RoomAnalysis`
- All current `ChannelState` snapshots
- Active `GenreProfile`
- Baseline snapshot (if captured)
- Active solo suppression windows (Phase 2 — stub for now)

**Recommendation logic:**

```
For each frequency band:
  deviation = room_band_level - genre_target_band_level
  if abs(deviation) > THRESHOLD (default 3dB):
    identify likely contributing channel via:
      1. Channel meter level (loudest active channel in that band's fingerprint range)
      2. Channel EQ state (any boosts in problem frequency range)
      3. Baseline drift on that channel
    generate recommendation with channel label, current state, suggested adjustment

For overall loudness:
  deviation = room_lufs - genre_target_lufs
  if abs(deviation) > THRESHOLD (default 2dB):
    identify top 2 contributing channels by meter level
    generate recommendation

For baseline drift (if baseline captured):
  for each channel:
    fader_drift = current_fader_db - baseline_fader_db
    if abs(fader_drift) > 2dB:
      generate drift alert
```

**Recommendation output format:**
```
[21:34:12] Guitar 1 — low-mid buildup around 315Hz detected
  Current:  EQ Band 2 +1.5dB @ 315Hz | Fader -2.0dB
  Baseline: EQ Band 2 +1.5dB @ 315Hz | Fader -2.0dB (no drift)
  Genre:    Glam Metal target -1dB @ low-mid
  Suggest:  EQ Band 2 cut to -1.5dB @ 315Hz
```

**Suppression rules (Phase 1):**
- Rate of change guard: if a channel fader moved > 3dB in last 5 seconds, suppress recommendations on that channel for 60 seconds (likely intentional move)
- Sparse mic guard: channels marked `usage: sparse` in band.yaml are ignored below their `inactive_threshold_db`
- Paired channel guard: if Keys is muted, use Guitar 3 fingerprint for ch 11; if Guitar 3 is muted, use Keys fingerprint

**Recommendation cooldown:** minimum 60 seconds between recommendations on the same channel (prevent spam)

---

### core/logger.py
Captures all session events to a structured JSON log.

**Responsibilities:**
- Open show log file at session start (`shows/YYYY-MM-DD_show.json`)
- Write recommendation events
- Detect manual board adjustments via OSC poll diff
- Correlate manual adjustments to prior recommendations
- Write post-show comparison report to terminal and file at session end

**Poll diff logic:**
```python
def detect_adjustments(prev: dict[int, ChannelState], 
                        curr: dict[int, ChannelState]) -> list[AdjustmentEvent]:
    events = []
    for ch_num in curr:
        p, c = prev[ch_num], curr[ch_num]
        if abs(c.fader_db - p.fader_db) > 0.5:          # fader moved
            events.append(AdjustmentEvent(ch_num, "fader", p.fader_db, c.fader_db))
        for b in range(4):
            if abs(c.eq[b].gain - p.eq[b].gain) > 0.5:  # EQ gain changed
                events.append(AdjustmentEvent(ch_num, f"eq_band_{b+1}_gain", ...))
            if abs(c.eq[b].freq - p.eq[b].freq) > 10:   # EQ freq shifted
                events.append(AdjustmentEvent(ch_num, f"eq_band_{b+1}_freq", ...))
        if c.muted != p.muted:                            # mute toggled
            events.append(AdjustmentEvent(ch_num, "mute", p.muted, c.muted))
    return events
```

**Recommendation correlation:**
- When a manual adjustment is detected, check if a recommendation was issued for that channel in the last 5 minutes
- If yes: tag as `matched`, compute delta between suggestion and actual adjustment
- If no: tag as `engineer_initiated` — potential blind spot

**Log event schema:**
```json
{
  "session": {
    "date": "2026-05-10",
    "band": "The Band Name",
    "mode": "show",
    "x32_ip": "192.168.0.1",
    "genre_default": "AOR",
    "started_at": "20:45:00",
    "ended_at": "23:12:00"
  },
  "events": [
    {
      "id": "evt_001",
      "timestamp": "21:34:12",
      "type": "RECOMMENDATION",
      "channel": "Guitar 1",
      "channel_num": 9,
      "genre_profile": "Glam Metal",
      "issue": "low_mid_buildup",
      "detail": "315Hz region 3.2dB above Glam Metal target",
      "current_state": { "eq_band_2_gain": 1.5, "eq_band_2_freq": 315, "fader_db": -2.0 },
      "suggestion": "EQ Band 2 cut to -1.5dB @ 315Hz"
    },
    {
      "id": "evt_002",
      "timestamp": "21:34:48",
      "type": "MANUAL_ADJUSTMENT",
      "channel": "Guitar 1",
      "channel_num": 9,
      "parameter": "eq_band_2_gain",
      "before": 1.5,
      "after": -0.5,
      "prior_recommendation_id": "evt_001",
      "match_status": "partial",
      "suggestion_delta": "Suggested -3dB change, applied -2dB change",
      "lag_seconds": 36
    },
    {
      "id": "evt_003",
      "timestamp": "21:52:18",
      "type": "MANUAL_ADJUSTMENT",
      "channel": "Guitar 2",
      "channel_num": 10,
      "parameter": "fader",
      "before": -2.0,
      "after": 1.0,
      "prior_recommendation_id": null,
      "match_status": "engineer_initiated",
      "suggestion_delta": null,
      "lag_seconds": null
    }
  ]
}
```

**Post-show report (terminal output at session end):**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FOH ASSISTANT — SHOW REPORT
Date: 2026-05-10 | Band: The Band Name
Duration: 2h 27m | Genre profiles: AOR, Glam Metal, Hard Rock
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RECOMMENDATION ACCURACY
  Total recommendations:         24
  Matched (within 1dB):          10  (42%)
  Partially matched:              6  (25%)
  Ignored / no follow-up:         8  (33%)

ENGINEER-INITIATED ADJUSTMENTS
  Total:                         11
  No prior recommendation:        8  ← review for blind spots
  Suppressed solo windows:        3  ✓ correctly ignored

TOP BLIND SPOT CHANNELS
  Bass DI:     3 engineer adjustments, 0 recommendations
  Guitar 2:    2 engineer adjustments, 0 recommendations

BASELINE DRIFT (vs soundcheck snapshot)
  Guitar 2 fader:   +3.0dB by end of show
  Keys fader:       +1.5dB by end of show
  All others:       within ±1dB ✓

SPARSE MIC EVENTS
  Drum Vocal:    1 activation (expected — Fight for Your Right) ✓
  Bassist Vocal: 1 activation (expected — Kryptonite) ✓

FEEDBACK EVENTS
  None detected ✓

Full log: shows/2026-05-10_show.json
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Config File Specs

### config/band.yaml
```yaml
band: "The Band Name"
x32:
  ip: "192.168.0.1"          # confirm at first show
  port: 10023
  poll_interval_ms: 500
  keepalive_interval_s: 8

audio:
  device_name_match: "DJI"   # partial match against system device list
  buffer_seconds: 2

channels:
  1:  { label: "Kick",          type: instrument }
  2:  { label: "Snare",         type: instrument }
  3:  { label: "Hi-Hat",        type: instrument }
  4:  { label: "Overhead L",    type: instrument }
  5:  { label: "Overhead R",    type: instrument }
  6:
    label: "Drum Vocal"
    type: vocal
    usage: sparse
    inactive_threshold_db: -35
    lead_songs: ["Fight for Your Right"]
    notes: "Sits open all show — active only for FFYR"
  7:  { label: "Bass DI",       type: instrument }
  8:
    label: "Bassist Vocal"
    type: vocal
    usage: backup_and_lead
    inactive_threshold_db: -35
    lead_songs: ["Kryptonite"]
    notes: "Tribute to Brad Arnold — mix with care on Kryptonite"
  9:
    label: "Guitar 1"
    type: instrument
    role: shared_lead_rhythm
  10:
    label: "Guitar 2"
    type: instrument
    role: shared_lead_rhythm
  11:
    label: "Keys"
    type: instrument
    paired_channel: 12
  12:
    label: "Guitar 3"
    type: instrument
    paired_channel: 11
    notes: "Keys player rhythm guitar — muted when Keys active"
  13:
    label: "Keys Vocal"
    type: vocal
    usage: backup_and_lead
    inactive_threshold_db: -35
    lead_songs: []             # TBD — confirm with band
  14:
    label: "Lead Vocal"
    type: vocal
    usage: primary_lead
    priority: very_high

thresholds:
  recommendation_trigger_db: 3.0      # deviation before recommendation fires
  lufs_trigger_db: 2.0               # LUFS deviation before overall rec fires
  baseline_drift_trigger_db: 2.0     # fader drift before drift alert fires
  rate_of_change_suppress_db: 3.0    # fader move size that triggers suppression
  rate_of_change_window_s: 5         # window to detect intentional move
  suppression_duration_s: 60         # how long to suppress after intentional move
  recommendation_cooldown_s: 60      # min time between recs on same channel
  adjustment_detect_fader_db: 0.5    # minimum fader change to log as adjustment
  adjustment_detect_eq_db: 0.5       # minimum EQ gain change to log
  adjustment_detect_freq_hz: 10      # minimum EQ freq change to log
```

### config/genres/aor.yaml (example)
```yaml
id: "AOR"
name: "AOR / Melodic Rock"
examples: ["Journey", "Foreigner", "REO Speedwagon", "Styx"]
target_lufs: -20
dynamic_range: medium-high

frequency_targets:
  sub_bass:   -8
  bass:       +1
  low_mid:     0
  mid:        +2
  high_mid:   +1
  presence:   +2
  air:        -1

instrument_weights:
  Lead Vocal:  { priority: very_high }
  Keys:        { priority: high }
  Guitar 1:    { priority: medium }
  Guitar 2:    { priority: medium }
  Bass DI:     { priority: medium }
  Kick:        { low_end_target_hz: 80, acceptable_weight: medium }

notes: "Optimize for vocal intelligibility above all else. Keys are a full instrument, not texture."
```

### config/setlist.yaml (optional Phase 1)
```yaml
# If not present, engine uses band default genre (first genre in profile)
# Song matching is case-insensitive partial match on title

songs:
  - title: "Don't Stop Believin'"
    artist: "Journey"
    genre_profile: "AOR"
    duration_s: 251
    reference_file: null
    reference_analyzed: false

  - title: "Round and Round"
    artist: "Ratt"
    genre_profile: "Glam Metal"
    duration_s: 240
    reference_file: null
    reference_analyzed: false

  - title: "Kryptonite"
    artist: "3 Doors Down"
    genre_profile: "Post-Grunge"
    duration_s: 234
    reference_file: null
    reference_analyzed: false
    notes: "Bassist vocal is lead — mix with extra care"

  - title: "Fight for Your Right"
    artist: "Beastie Boys"
    genre_profile: "Party Rock"
    duration_s: 175
    reference_file: null
    reference_analyzed: false
    notes: "Drummer vocal is lead — confirm Drum Vocal unmuted pre-song"
```

---

## Baseline Mode Specification

**Launch:** `python main.py --baseline`

**Flow:**
```
1. Connect to X32, confirm audio input active
2. Print baseline mode header with active genre profile
3. Read full board state snapshot — save as pre-soundcheck reference
4. Enter interactive loop:

   > Enter channel to assess (name or number), or 'band' for full mix, 'done' to finish:
   > kick

   [Kick] — Assessing vs AOR profile...
     Fader:      -3.0dB
     EQ Band 1:  flat @ 80Hz
     Room sub-bass: 4dB below AOR target
     Suggest: EQ Band 1 +2dB @ 70Hz for more weight
     or: Pull fader to -1dB

   > (engineer makes adjustment on tablet)
   > recheck

   [Kick] — Re-assessing...
     Fader:      -1.0dB
     EQ Band 1:  +2.0dB @ 72Hz
     Room sub-bass: within 1dB of AOR target ✓
     Status: Good

5. After all channels assessed, engineer types 'band'
6. Script listens to full band playing together for 30 seconds
7. Generates combined mix assessment vs genre curve
8. Engineer makes adjustments, types 'recheck' to re-assess
9. Engineer types 'confirm' to lock baseline
10. Baseline saved to shows/YYYY-MM-DD_baseline.json
11. Script transitions to show mode or exits
```

---

## Show Mode Specification

**Launch:** `python main.py --show`

**Main loop (runs every 1 second):**
```
1. Poll X32 — get channel state snapshot
2. Diff vs previous snapshot — detect manual adjustments
3. Capture 1s audio chunk — append to rolling buffer
4. Run room analysis on buffer
5. Run recommendation engine:
   a. Check overall LUFS vs genre target
   b. Check each frequency band vs genre target
   c. Identify contributing channels
   d. Apply suppression rules
   e. Apply cooldown rules
   f. Generate recommendations if thresholds exceeded
6. Print any new recommendations to terminal
7. Log all events (recs + manual adjustments) to show JSON
8. Sleep remainder of 1s cycle
```

**Keyboard controls (terminal):**
- `Ctrl+C` — graceful shutdown, generate post-show report, save log
- `s` + Enter — print current board state snapshot
- `g` + Enter — print current room analysis (LUFS + bands)
- `b` + Enter — print baseline drift summary

---

## Requirements.txt

```
python-osc>=1.8.0
sounddevice>=0.4.6
numpy>=1.26.0
scipy>=1.11.0
pyloudnorm>=0.1.1
pyyaml>=6.0.1
librosa>=0.10.0       # audio file loading for simulator and offline validation
soundfile>=0.12.1     # audio file I/O
```

---

## Simulator & Offline Testing

### Overview

Three test components allow full end-to-end validation before the first show:

| Component | Purpose | Tool |
|---|---|---|
| X32 Simulator | Fake board responding to OSC | `simulator/x32_sim.py` |
| Virtual Audio Device | Feed audio files as mic input | VB-Audio Virtual Cable (Windows, free) |
| Offline Validator | Run engine against recordings, no hardware | `tools/validate.py` |

---

### simulator/x32_sim.py

A lightweight OSC server that impersonates an X32 on `127.0.0.1:10023`. The main app cannot tell the difference — it connects, polls, and receives responses identically to a real board.

**Project structure addition:**
```
foh-assistant/
├── simulator/
│   ├── x32_sim.py           # OSC server — fake X32
│   ├── scenarios/
│   │   ├── baseline.yaml    # Realistic starting board state
│   │   ├── level_creep.yaml # Gradual fader drift over time
│   │   ├── low_mid_mud.yaml # EQ buildup scenario
│   │   ├── solo_event.yaml  # Fader spike simulating guitar solo
│   │   └── feedback_risk.yaml # High-freq spike scenario
│   └── README.md
└── tools/
    └── validate.py          # Offline validation runner
```

**Simulator responsibilities:**
- Bind UDP socket on port 10023
- Respond to `/xremote` keepalive with acknowledgment
- Maintain internal board state (all 14 channels, all parameters)
- Respond to OSC poll requests with current state values
- Execute scenario timeline — mutate board state on schedule
- Print state changes to terminal so you can follow along

**Scenario file format:**
```yaml
# scenarios/level_creep.yaml
name: "Gradual Level Creep"
description: "Guitar 1 and 2 faders drift up over 5 minutes, overall LUFS exceeds target"
duration_s: 300

initial_state:
  channels:
    9:  { fader_db: -2.0, eq: [{gain: 1.5, freq: 315, q: 1.0, type: "PEQ"}] }  # Guitar 1
    10: { fader_db: -2.0, eq: [{gain: 0.0, freq: 315, q: 1.0, type: "PEQ"}] }  # Guitar 2
    14: { fader_db: -1.0 }   # Lead Vocal
  main_fader_db: 0.0

timeline:
  - at_s: 60
    action: fader_drift
    channel: 9
    target_db: 0.5
    over_s: 60          # drift gradually over 60 seconds
    note: "Guitar 1 creeping up"

  - at_s: 120
    action: fader_drift
    channel: 10
    target_db: 1.0
    over_s: 90
    note: "Guitar 2 following"

  - at_s: 200
    action: eq_change
    channel: 9
    band: 2
    gain_db: 3.0
    note: "Low-mid boost added on Guitar 1"

  - at_s: 250
    action: fader_move
    channel: 9
    target_db: 4.0
    note: "Simulate solo boost — should trigger suppression"
```

**Running the simulator:**
```bash
# Terminal 1 — start simulator with scenario
python simulator/x32_sim.py --scenario scenarios/level_creep.yaml

# Terminal 2 — run main app pointed at localhost
python main.py --show --x32-ip 127.0.0.1

# Watch recommendations fire as scenario executes
```

**Additional simulator scenarios to build:**

| Scenario | Tests |
|---|---|
| `baseline.yaml` | Flat realistic starting state — no recommendations should fire |
| `level_creep.yaml` | Gradual fader drift — overall LUFS recommendation |
| `low_mid_mud.yaml` | EQ buildup across guitar channels — frequency recommendation |
| `solo_event.yaml` | Fast fader spike — rate-of-change suppression fires correctly |
| `sparse_mic.yaml` | Drum vocal channel crosses threshold mid-scenario |
| `paired_channel.yaml` | Keys muted, Guitar 3 unmuted — fingerprint switches |
| `feedback_risk.yaml` | High-freq spike — feedback alert fires |
| `clean_show.yaml` | Well-run show — engine stays quiet, minimal recommendations |

---

### Virtual Audio Setup (Windows)

**VB-Audio Virtual Cable** creates a virtual audio device that appears in Windows as both an output and an input. You play audio to the virtual output; the app reads from the virtual input.

**Install:**
1. Download VB-Audio Virtual Cable from vb-audio.com (free)
2. Install and reboot
3. In Windows Sound Settings, the device appears as "CABLE Input" (output) and "CABLE Output" (input)

**Usage:**
```
Music player → CABLE Input (virtual output)
                    ↓
              VB-Audio Virtual Cable
                    ↓
              CABLE Output (virtual input)  ← app reads this as mic
```

**App config for virtual audio:**
```yaml
# config/band.yaml — test override
audio:
  device_name_match: "CABLE"    # matches VB-Audio virtual device
```

**Test audio sources:**

| Source | Tests |
|---|---|
| Studio recordings (Journey, Ratt, etc.) | Genre profile targets — engine should be mostly quiet on a well-mixed source |
| Band's live show recordings | Real-world problems — engine should flag what you actually adjusted |
| Sine wave generator | Specific frequency band isolation — confirm FFT binning is accurate |
| Pink noise | Flat spectrum baseline — confirm band levels read as expected |

---

### tools/validate.py — Offline Validation Runner

The offline validator is the most powerful testing tool. It loads an audio file and a simulated board state timeline, runs the full analysis and recommendation engine, and generates a validation report — no hardware required, runs in seconds.

**This is especially valuable for live recordings** — feed in the band's previous show audio alongside a manually annotated ground truth of what you actually adjusted that night, and measure how well the engine would have performed.

**Usage:**
```bash
# Validate against a studio track — should see minimal recommendations
python tools/validate.py \
  --audio "test_audio/journey_dont_stop_believin.mp3" \
  --genre AOR \
  --board scenarios/baseline.yaml

# Validate against a live recording with ground truth
python tools/validate.py \
  --audio "test_audio/live_show_2026_03_15.mp3" \
  --genre Glam Metal \
  --board scenarios/baseline.yaml \
  --ground-truth "test_audio/live_show_2026_03_15_adjustments.yaml"
```

**Ground truth file format:**
```yaml
# live_show_2026_03_15_adjustments.yaml
# Manual log of adjustments made at the actual show
# Used to measure engine accuracy in offline validation

adjustments:
  - timestamp_s: 1452    # 24:12 into recording
    channel: "Guitar 1"
    parameter: "fader"
    before_db: -2.0
    after_db: -4.0
    note: "Was getting too hot vs vocals"

  - timestamp_s: 2134
    channel: "Bass DI"
    parameter: "eq_band_1_gain"
    before_db: 0.0
    after_db: -2.0
    note: "Low-mid was getting muddy"

  - timestamp_s: 3012
    channel: "Lead Vocal"
    parameter: "fader"
    before_db: 0.0
    after_db: 1.5
    note: "Needed to come up over guitars in chorus"
```

**Validation report output:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FOH ASSISTANT — OFFLINE VALIDATION REPORT
Audio: live_show_2026_03_15.mp3 (2h 14m)
Genre: Glam Metal | Board: baseline.yaml
Ground truth: 18 engineer adjustments
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ENGINE PERFORMANCE
  Recommendations generated:    31
  True positives:               12  (matched ground truth adjustment)
  False positives:               8  (recommended, no ground truth match)
  Missed (false negatives):      6  (ground truth adjustment, no recommendation)
  Accuracy:                     39%  ← baseline before tuning

TOP FALSE POSITIVES (over-recommending)
  Keys fader:     flagged 4x — engineer never adjusted  
  Overhead L:     flagged 2x — likely room reflection artifact

TOP MISSED ADJUSTMENTS (blind spots)
  Lead Vocal fader raised at 50:12 — engine did not flag
  Bass DI EQ cut at 35:34 — engine did not flag

RECOMMENDATION LAG (true positives)
  Average:   48 seconds before engine would have flagged
  Fastest:   12 seconds
  Slowest:   3:24

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Threshold tuning suggestions:
  Keys channel: raise inactive_threshold_db from -35 to -28
  Lead Vocal:   lower recommendation_trigger_db from 3.0 to 2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**The validator is a tuning tool** — run it, look at the false positives and blind spots, adjust thresholds in `band.yaml`, run it again. Iterate until accuracy is acceptable before the first live show.

---

## Development & Test Sequence

### Phase A — Unit tests (no hardware, no audio)
1. `pip install -r requirements.txt`
2. Confirm genre YAML files load and parse correctly
3. Confirm band.yaml loads and channel map resolves
4. Confirm logger creates show JSON and writes events correctly
5. Confirm fader float-to-dB conversion produces expected values
6. Confirm frequency band binning produces expected output on synthetic data

### Phase B — Audio pipeline (no hardware)
1. Install VB-Audio Virtual Cable
2. `python main.py --devices` — confirm CABLE Output appears in device list
3. Play pink noise through CABLE Input
4. Run analyzer against CABLE Output — confirm flat spectrum reads as expected
5. Play studio track (e.g. Journey) through CABLE Input
6. Confirm LUFS and band levels look reasonable for AOR genre profile
7. Confirm genre comparison produces minimal recommendations on a well-mixed studio track

### Phase C — Simulator integration (no hardware)
1. `python simulator/x32_sim.py --scenario scenarios/baseline.yaml` (Terminal 1)
2. `python main.py --show --x32-ip 127.0.0.1` (Terminal 2)
3. Confirm connection established, channel state reads back correctly
4. Run each scenario and confirm expected recommendations fire
5. Run `solo_event.yaml` — confirm rate-of-change suppression triggers
6. Run `sparse_mic.yaml` — confirm sparse mic threshold behavior
7. Run `clean_show.yaml` — confirm engine stays quiet on a stable board

### Phase D — Offline validation against live recordings
1. Prepare ground truth YAML for at least one previous show recording
2. `python tools/validate.py --audio <live_recording> --ground-truth <adjustments>`
3. Review accuracy report
4. Tune thresholds in band.yaml
5. Re-run until false positive rate is acceptable
6. Repeat with studio tracks — confirm engine is quiet on clean mixes

### Phase E — Full integration test
1. Run simulator + virtual audio + main app simultaneously
2. Play live recording through VB-Audio while simulator runs `level_creep.yaml`
3. Confirm recommendations from both audio analysis and board state fire correctly
4. Run `--baseline` mode with simulator — confirm full soundcheck flow works
5. `Ctrl+C` — confirm post-show report generates correctly

### At the show — pre-soundcheck
1. Join X32 WiFi on laptop
2. `python main.py --test-osc` — confirm real X32 IP, verify channel state reads
3. Update `config/band.yaml` with confirmed IP and actual channel numbers
4. `python main.py --devices` — confirm DJI receiver visible

### At the show — soundcheck
1. `python main.py --baseline`
2. Work through channels with band playing
3. Confirm and lock baseline

### During the show
1. `python main.py --show`
2. Monitor terminal for recommendations
3. Make adjustments on tablet as normal — let the engine observe

### Post-show
1. `Ctrl+C` — post-show report prints automatically
2. Save show JSON
3. Run `tools/validate.py` against show audio + show JSON as ground truth
4. Compare offline report to live report — identify any discrepancies

---

## Open Items (resolve at first show)

- [ ] Confirm X32 IP address on venue WiFi
- [ ] Confirm actual channel numbers for all 14 channels
- [ ] Confirm DJI USB receiver device name as seen by Windows
- [ ] Confirm X32 OSC meter blob format (test with `--test-osc`)
- [ ] Gather previous live show recordings for offline validation
- [ ] Create ground truth adjustment YAML for at least one previous show

---

*Phase 1 scope only. Do not implement OSC write, automation, UI, or setlist song-switching logic in this phase.*

