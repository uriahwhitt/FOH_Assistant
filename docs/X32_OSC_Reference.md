# X32 OSC Protocol Reference
**For use by:** FOH Assistant — Claude Code implementation  
**Source:** Unofficial X32/M32 OSC Remote Protocol v4.02-01 (Jan 2020) by Patrick-Gilles Maillot  
**Scope:** Commands relevant to FOH Assistant Phase 1 (read-only) and future automation phases  
**Note:** Full protocol doc is 175 pages. This reference covers only what FOH Assistant needs.

---

## 1. Connection Basics

| Property | Value |
|---|---|
| Protocol | UDP (OSC over Ethernet) |
| Port | **10023** |
| Direction | Bidirectional — client sends, X32 replies to client's port |
| Max clients | 4 simultaneous |

**Connection modes:**
- **Immediate** — client sends a request, X32 replies once
- **Deferred (`/xremote`)** — X32 pushes all parameter changes to client for 10 seconds; must be renewed every 8 seconds to avoid timeout

**OSC data alignment:** All parameters must be big-endian and 4-byte aligned/padded with null bytes.

---

## 2. Session Management Commands

### `/xremote`
Register for push updates. X32 will send all parameter changes to the client for 10 seconds.  
**Must be sent every 8 seconds** to keep the session alive.

```python
# python-osc keepalive pattern
client.send_message("/xremote", [])
# Repeat every 8 seconds in a background thread
```

### `/info`
Returns console version and model info.  
**Response:** `/info ,ssss <version> <server_name> <model> <fw_version>`  
Example: `/info ,ssss V2.05 osc-server X32 2.12`

### `/xinfo`
Returns network address, name, model, version.  
Example: `/xinfo ,ssss 192.168.1.62 X32-02-4A-53 X32 3.04`

### `/status`
Returns console state and IP.  
Example: `/status ,sss active 192.168.0.64 osc-server`

---

## 3. Reading Parameters (GET)

Send the OSC address with **no arguments** — X32 echoes back the current value.

```
Request:  /ch/01/mix/fader
Response: /ch/01/mix/fader ,f [0.7500]
```

Reading a full node at once (more efficient than reading parameters individually):
```
Request:  /node ,s ch/01/eq/1
Response: node ,s /ch/01/eq/1 <type> <freq> <gain> <q>\n
```

---

## 4. Writing Parameters (SET) — Phase 4+ Only

Send the OSC address **with** the value argument.  
**Phase 1: DO NOT send any write commands. Board is read-only.**

```
# Example (Phase 4+ only):
/ch/01/mix/fader ,f [0.8250]   # Set fader to 3dB
/ch/01/mix/on ,i [0]           # Mute channel (0=OFF, 1=ON)
/ch/01/eq/2/g ,f [0.4375]      # Set EQ band 2 gain
```

---

## 5. Channel Strip Parameters

**Address pattern:** `/ch/[01…32]/...`  
Channel numbers are zero-padded: ch 1 = `/ch/01/`, ch 14 = `/ch/14/`

### 5.1 Config
```
/ch/[01..32]/config/name    string   Channel name (12 chars max)
/ch/[01..32]/config/color   enum     {OFF,RD,GN,YE,BL,MG,CY,WH,...}
```

### 5.2 Mix (Fader, Mute, Pan)
```
/ch/[01..32]/mix/fader      level    [0.0…1.0] float → dB (see section 9)
/ch/[01..32]/mix/on         enum     {OFF=0, ON=1}  ← NOTE: ON=1 means UNMUTED
/ch/[01..32]/mix/st         enum     {OFF=0, ON=1}  stereo bus assignment
/ch/[01..32]/mix/pan        linf     [-100.0, +100.0, step 2.0]
/ch/[01..32]/mix/mono       enum     {OFF=0, ON=1}
```

> **Important:** `/mix/on` value `0` = OFF = **muted**. Value `1` = ON = **active/unmuted**.

### 5.3 EQ (4 bands per channel)
```
/ch/[01..32]/eq/on          enum     {OFF=0, ON=1}
/ch/[01..32]/eq/[1..4]/type enum     {LCut=0, LShv=1, PEQ=2, VEQ=3, HShv=4, HCut=5}
/ch/[01..32]/eq/[1..4]/f    logf     [20.0, 20000.0, 201 steps] Hz
/ch/[01..32]/eq/[1..4]/g    linf     [-15.0, +15.0, step 0.25] dB
/ch/[01..32]/eq/[1..4]/q    logf     [10.0, 0.3, 72 steps]
```

**EQ band shortcuts — read all 4 parameters at once:**
```
/node ,s ch/01/eq/1    # Returns: type freq gain q for band 1
/node ,s ch/01/eq/2    # Band 2, etc.
```

**EQ type reference:**
| Value | Type | Description |
|---|---|---|
| 0 | LCut | Low cut / high pass filter |
| 1 | LShv | Low shelf |
| 2 | PEQ | Parametric EQ (most common) |
| 3 | VEQ | Vintage EQ |
| 4 | HShv | High shelf |
| 5 | HCut | High cut / low pass filter |

### 5.4 Gate
```
/ch/[01..32]/gate/on        enum     {OFF=0, ON=1}
/ch/[01..32]/gate/thr       linf     [-80.0, 0.0, step 0.5] dB
/ch/[01..32]/gate/range     linf     [3.0, 60.0, step 1.0] dB
/ch/[01..32]/gate/attack    linf     [0.0, 120.0, step 1.0] ms
/ch/[01..32]/gate/release   logf     [5.0, 4000.0, 101 steps] ms
/ch/[01..32]/gate/mode      enum     {EXP2=0, EXP3=1, EXP4=2, GATE=3, DUCK=4}
```

### 5.5 Compressor/Dynamics
```
/ch/[01..32]/dyn/on         enum     {OFF=0, ON=1}
/ch/[01..32]/dyn/mode       enum     {COMP=0, EXP=1}
/ch/[01..32]/dyn/thr        linf     [-60.0, 0.0, step 0.5] dB
/ch/[01..32]/dyn/ratio      enum     {1.1=0,1.3=1,1.5=2,2.0=3,2.5=4,3.0=5,
                                       4.0=6,5.0=7,7.0=8,10=9,20=10,100=11}
/ch/[01..32]/dyn/attack     linf     [0.0, 120.0, step 1.0] ms
/ch/[01..32]/dyn/release    logf     [5.0, 4000.0, 101 steps] ms
/ch/[01..32]/dyn/mgain      linf     [0.0, 24.0, step 0.5] dB  (makeup gain)
```

### 5.6 Preamp
```
/ch/[01..32]/preamp/trim    linf     [-18.0, +18.0, step 0.25] dB
/ch/[01..32]/preamp/invert  enum     {OFF=0, ON=1}
/ch/[01..32]/preamp/hpon    enum     {OFF=0, ON=1}  phantom power
/ch/[01..32]/preamp/hpf     logf     [20.0, 400.0, 101 steps] Hz  high pass
/ch/[01..32]/preamp/hpslope enum     {12, 18, 24} dB/oct
```

---

## 6. Bus Masters

**Address pattern:** `/bus/[01…16]/...`  
Buses have **6-band EQ** (vs 4-band on channels).

```
/bus/[01..16]/mix/fader     level    [0.0…1.0] float → dB
/bus/[01..16]/mix/on        enum     {OFF=0, ON=1}
/bus/[01..16]/mix/pan       linf     [-100.0, +100.0, step 2.0]
/bus/[01..16]/eq/on         enum     {OFF=0, ON=1}
/bus/[01..16]/eq/[1..6]/f   logf     [20.0, 20000.0, 201 steps] Hz
/bus/[01..16]/eq/[1..6]/g   linf     [-15.0, +15.0, step 0.25] dB
/bus/[01..16]/eq/[1..6]/q   logf     [10.0, 0.3, 72 steps]
/bus/[01..16]/dyn/thr       linf     [-60.0, 0.0, step 0.5] dB
/bus/[01..16]/dyn/ratio     enum     (same as channel, 0-11)
```

---

## 7. Main Stereo Bus

**Address pattern:** `/main/st/...`

```
/main/st/mix/fader          level    [0.0…1.0] float → dB
/main/st/mix/on             enum     {OFF=0, ON=1}
/main/st/mix/pan            linf     [-100.0, +100.0, step 2.0]
/main/st/eq/on              enum     {OFF=0, ON=1}
/main/st/eq/[1..6]/f        logf     [20.0, 20000.0, 201 steps] Hz
/main/st/eq/[1..6]/g        linf     [-15.0, +15.0, step 0.25] dB
/main/st/eq/[1..6]/q        logf     [10.0, 0.3, 72 steps]
/main/st/dyn/thr            linf     [-60.0, 0.0, step 0.5] dB
/main/st/dyn/ratio          enum     (same as channel, 0-11)
```

---

## 8. Meter Commands

Meter requests return data as **OSC blobs** (binary). Updates arrive every ~50ms for 10 seconds.  
**All meter float values are in range [0.0, 1.0]** representing linear audio level (0 = silence, 1.0 = digital full scale). Values above 1.0 (up to ~8.0) indicate headroom overage (+18 dBfs).

### Key Meter IDs for FOH Assistant

| Meter | Command | Returns | Use |
|---|---|---|---|
| All channel RMS | `/meters/1` | 96 floats: 32 ch input, 32 gate GR, 32 dyn GR | Primary channel level monitoring |
| Channel strip | `/meters/6 ,si <ch_id>` | 4 floats: pre-fade, gate, dyn GR, post-fade | Single channel detail |
| Mix bus levels | `/meters/2` | 49 floats: 16 bus, 6 matrix, 2 LR, 1 mono + GR | Bus monitoring |
| RTA data | `/meters/15` | 50 32-bit values → 100 short ints | Frequency analysis per band |

### /meters/1 — Full Channel Meter Blob (Primary for FOH Assistant)
```
/meters ,si /meters/1 1

Returns 96 floats as blob:
  [0..31]   = 32 input channel RMS levels (ch 1-32)
  [32..63]  = 32 gate gain reductions
  [64..95]  = 32 dynamics gain reductions
```

Python parse:
```python
import struct

def parse_meters_1(blob_data: bytes) -> dict:
    """Parse /meters/1 blob. Returns per-channel RMS as dBFS."""
    # blob format: int1 (4B big-endian length) + int2 (4B little-endian count) + floats (little-endian)
    num_floats = struct.unpack_from('<I', blob_data, 4)[0]
    floats = struct.unpack_from(f'<{num_floats}f', blob_data, 8)
    return {
        'channel_rms':    list(floats[0:32]),    # index 0 = ch01, index 31 = ch32
        'gate_gr':        list(floats[32:64]),
        'dynamics_gr':    list(floats[64:96]),
    }

def linear_to_dbfs(linear: float) -> float:
    """Convert X32 meter float [0.0-1.0] to dBFS."""
    if linear <= 0:
        return -90.0
    import math
    return 20 * math.log10(linear)
```

### /meters/6 — Single Channel Strip Meters
```
/meters ,si /meters/6 <channel_id>

channel_id: 0-based index (ch01 = 0, ch14 = 13, ch32 = 31)

Returns 4 floats:
  [0] pre-fade level
  [1] gate gain reduction
  [2] dynamics gain reduction
  [3] post-fade level  ← most useful for FOH Assistant
```

### /meters/15 — RTA Data
```
/meters ,si /meters/15 1

Returns 50 32-bit values representing 100 successive little-endian short ints.
Each short int range: [0x8000, 0x0000]
Convert to dBFS: short_int_value / 256.0  (range: -128.0 to 0.0)

100 frequency bands (Hz):
20, 21, 22, 24, 26, 28, 30, 32, 34, 36, 39, 42, 45, 48, 52, 55, 59, 63, 68, 73,
78, 84, 90, 96, 103, 110, 118, 127, 136, 146, 156, 167, 179, 192, 206, 221, 237,
254, 272, 292, 313, 335, 359, 385, 412, 442, 474, 508, 544, 583, 625, 670, 718,
769, 825, 884, 947, 1.02K, 1.09K, 1.17K, 1.25K, 1.34K, 1.44K, 1.54K, 1.65K,
1.77K, 1.89K, 2.03K, 2.18K, 2.33K, 2.50K, 2.68K, 2.87K, 3.08K, 3.30K, 3.54K,
3.79K, 4.06K, 4.35K, 4.67K, 5.00K, 5.36K, 5.74K, 6.16K, 6.60K, 7.07K, 7.58K,
8.12K, 8.71K, 9.33K, 10.0K, 10.72K, 11.49K, 12.31K, 13.20K, 14.14K, 15.16K,
16.25K, 17.41K, 18.66K
```

### Subscribing to Continuous Meter Updates
```
# Subscribe to /meters/1 updates every 50ms for 10 seconds
/batchsubscribe ,ssiii /my_meters /meters/1 0 0 1

# Renew before 10s timeout:
/renew ,s /my_meters

# Stop:
/unsubscribe ,s /my_meters
```

---

## 9. Fader Float ↔ dB Conversion

The X32 uses **4 piecewise linear segments** to approximate a logarithmic fader curve.  
Cross points at -60, -30, -10 dB. All fader values are floats in [0.0, 1.0].

```python
def fader_float_to_db(f: float) -> float:
    """Convert X32 OSC fader float [0.0, 1.0] to dB [-90, +10]."""
    if f >= 0.5:
        return f * 40.0 - 30.0       # range: -10 to +10 dB
    elif f >= 0.25:
        return f * 80.0 - 50.0       # range: -30 to -10 dB
    elif f >= 0.0625:
        return f * 160.0 - 70.0      # range: -60 to -30 dB
    elif f > 0.0:
        return f * 480.0 - 90.0      # range: -90 to -60 dB
    else:
        return -90.0                  # fader at minimum = -oo (treated as -90)

def db_to_fader_float(d: float) -> float:
    """Convert dB [-90, +10] to X32 OSC fader float [0.0, 1.0]."""
    if d < -60.0:
        f = (d + 90.0) / 480.0
    elif d < -30.0:
        f = (d + 70.0) / 160.0
    elif d < -10.0:
        f = (d + 50.0) / 80.0
    else:
        f = (d + 30.0) / 40.0
    # Round to nearest known X32 value (1024 steps)
    return int(f * 1023.5) / 1023.0

# Key reference values
# 0.0   → -90 dB (or -oo)
# 0.25  → -30 dB
# 0.50  → -10 dB
# 0.75  →   0 dB  (unity)
# 1.0   → +10 dB
```

---

## 10. EQ Frequency Float Conversion

EQ frequency is stored as a float [0.0, 1.0] on a log scale across 201 steps from 20Hz to 20kHz.

```python
import math

# The X32 uses 201 log-scale steps from 20Hz to 20kHz
FREQ_MIN = 20.0
FREQ_MAX = 20000.0
FREQ_STEPS = 201

def eq_float_to_hz(f: float) -> float:
    """Convert X32 EQ freq float [0.0, 1.0] to Hz [20, 20000]."""
    log_min = math.log10(FREQ_MIN)
    log_max = math.log10(FREQ_MAX)
    return 10 ** (log_min + f * (log_max - log_min))

def hz_to_eq_float(hz: float) -> float:
    """Convert Hz [20, 20000] to X32 EQ freq float [0.0, 1.0]."""
    hz = max(FREQ_MIN, min(FREQ_MAX, hz))
    log_min = math.log10(FREQ_MIN)
    log_max = math.log10(FREQ_MAX)
    return (math.log10(hz) - log_min) / (log_max - log_min)

# Common reference frequencies as floats (approximate)
# 80Hz   ≈ 0.298
# 100Hz  ≈ 0.330
# 200Hz  ≈ 0.465
# 315Hz  ≈ 0.548
# 500Hz  ≈ 0.630
# 1kHz   ≈ 0.750
# 2kHz   ≈ 0.835
# 4kHz   ≈ 0.915
# 8kHz   ≈ 0.965
```

---

## 11. Subscription System

For continuous monitoring without polling, use subscriptions. All subscriptions time out after 10 seconds and must be renewed with `/renew`.

### `/xremote` — Push All Changes
Most useful for FOH Assistant. After sending `/xremote`, the X32 pushes every parameter change to the client — including changes made by other clients (tablet app).

```python
# Send every 8 seconds
client.send_message("/xremote", [])
```

### `/subscribe` — Subscribe to Specific Parameter
```
/subscribe ,si /ch/01/mix/fader 1
# X32 will push fader updates ~200 times over 10s (time_factor=1)
# Increase time_factor to reduce update frequency
```

### `/batchsubscribe` — Subscribe to Meter Data
```
/batchsubscribe ,ssiii /alias /meters/1 0 0 1
# arg1: alias name for this subscription
# arg2: meter command
# arg3-4: meter command arguments (0 if not needed)
# arg5: time_factor (1 = every 50ms)
```

### `/renew` — Renew Active Subscriptions
```
/renew ,s /alias    # Renew specific subscription by alias
/renew              # Renew all active subscriptions
```

### `/unsubscribe` — Stop Subscriptions
```
/unsubscribe ,s /alias    # Stop specific subscription
/unsubscribe              # Stop all
```

---

## 12. Efficient Bulk Reading with `/node`

Reading parameters one at a time is slow on WiFi. Use `/node` to read an entire parameter group in one request.

```
# Read all EQ settings for channel 1, band 1:
/node ,s ch/01/eq/1
# Returns: type freq gain q  (space-separated, ends with \n)

# Read full mix settings for channel 1:
/node ,s ch/01/mix
# Returns all mix parameters for ch01

# Read full EQ for a channel (all 4 bands):
/node ,s ch/01/eq

# Useful nodes for FOH Assistant:
/node ,s ch/01/config    # name, icon, color, source
/node ,s ch/01/eq        # all 4 EQ bands
/node ,s ch/01/mix       # fader, on/off, pan, bus sends
/node ,s ch/01/dyn       # all compressor settings
/node ,s ch/01/gate      # all gate settings
/node ,s main/st/mix     # main LR fader and mute
```

---

## 13. Recommended Poll Strategy for FOH Assistant

```python
# Startup sequence:
# 1. Send /xremote to register for push updates
# 2. Use /node to read full state snapshot for all 14 channels
# 3. Send /batchsubscribe for /meters/1 (all channel RMS, every 50ms)
# 4. Every 8s: send /xremote to renew push updates
# 5. Every 8s: send /renew to renew meter subscription
# 6. Between pushes: diff current state vs previous snapshot

# Efficient channel snapshot (one request per channel):
for ch in range(1, 15):  # channels 1-14
    ch_str = f"{ch:02d}"
    client.send_message("/node", [f"ch/{ch_str}/mix"])    # fader, mute, pan
    client.send_message("/node", [f"ch/{ch_str}/eq"])     # all EQ bands
    client.send_message("/node", [f"ch/{ch_str}/dyn"])    # compressor

# Meters via batchsubscribe (push model — more efficient than polling):
client.send_message("/batchsubscribe", ["/foh_meters", "/meters/1", 0, 0, 1])
```

---

## 14. X32 Emulator (Development & Testing)

Patrick-Gilles Maillot (author of the OSC protocol docs) built a software X32 emulator that responds to all OSC commands over UDP.

**Download:** https://sites.google.com/site/patrickmaillot/x32  
(Look for "X32 emulator" section on the page)

**Capabilities:**
- Full OSC command support (32 channels, 16 bus, 8 effects, 8 Aux, 6 Matrix, L/R, Mono, 8 DCA)
- Responds to all read commands with realistic data
- Accepts all write commands and maintains state
- Multi-client support
- Runs on Windows, Linux, macOS

**Limitations:**
- No actual audio processing
- No MIDI
- No USB audio

**Usage for FOH Assistant development:**
```bash
# Terminal 1: start emulator (default port 10023)
./x32_emulator    # or X32Emulator.exe on Windows

# Terminal 2: run FOH Assistant pointed at localhost
python main.py --show --x32-ip 127.0.0.1
```

**Configure FOH Assistant for emulator:**
```yaml
# config/band.yaml
x32:
  ip: "127.0.0.1"    # emulator on localhost
  port: 10023
```

**Recommended test workflow:**
1. Start emulator
2. Run `python main.py --test-osc` — confirm connection and channel reads
3. Use the X32_Command utility (also from Patrick Maillot's site) to manually send OSC commands to the emulator and watch FOH Assistant detect them as manual adjustments
4. Validate recommendation engine against emulator state changes

---

## 15. Common Pitfalls

| Issue | Cause | Fix |
|---|---|---|
| No response from X32 | Not on same network / wrong IP | Confirm IP with `/xinfo` broadcast or tablet app settings |
| Updates stop after 10s | `/xremote` timeout | Send `/xremote` every 8s in background thread |
| Meter blob parse error | Little-endian vs big-endian confusion | Blob floats are **little-endian**; OSC header is **big-endian** |
| Fader reads 0.75 but board shows 0dB | Expected — 0.75 = unity (0dB) | Use `fader_float_to_db()` conversion |
| `/mix/on` value 0 means muted | Counter-intuitive naming | ON=1 means active/unmuted; OFF=0 means muted |
| WiFi drops UDP packets | 2.4GHz congestion + high `/node` rate | Use 5GHz if available; batch reads with `/node`; avoid rapid-fire requests |
| Channel 1 address | Must zero-pad | `/ch/01/` not `/ch/1/` |

---

## 16. FOH Assistant — OSC Addresses Quick Reference

Commands used by FOH Assistant Phase 1 (read-only):

```
# Session
/xremote                            Register for push updates (renew every 8s)
/info                               Confirm connection

# Per-channel reads (substitute 01..14 for this band's channel map)
/ch/[01..14]/config/name            Channel label (verify matches band.yaml)
/ch/[01..14]/mix/fader              Fader level [0.0-1.0] → dB
/ch/[01..14]/mix/on                 Mute state (0=muted, 1=active)
/ch/[01..14]/eq/[1..4]/f            EQ band frequency [0.0-1.0] → Hz
/ch/[01..14]/eq/[1..4]/g            EQ band gain [0.0-1.0] → dB [-15, +15]
/ch/[01..14]/eq/[1..4]/type         EQ band type [0-5]
/ch/[01..14]/dyn/on                 Compressor on/off
/ch/[01..14]/dyn/thr                Compressor threshold
/ch/[01..14]/dyn/ratio              Compressor ratio

# Efficient bulk reads
/node ,s ch/[01..14]/eq             All 4 EQ bands in one request
/node ,s ch/[01..14]/mix            Fader, mute, pan, bus sends

# Main bus
/main/st/mix/fader                  Main LR fader
/main/st/mix/on                     Main LR mute

# Meters (push subscription — preferred over polling)
/batchsubscribe ,ssiii /foh_m /meters/1 0 0 1    All 32ch RMS every 50ms
/renew ,s /foh_m                                  Renew before 10s timeout
```

---

*Source: Unofficial X32/M32 OSC Remote Protocol v4.02-01, Patrick-Gilles Maillot, Jan 2020*  
*Full document available at: https://sites.google.com/site/patrickmaillot/x32*
