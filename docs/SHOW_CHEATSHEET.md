# FOH Assistant — Show Night Cheatsheet
## AJ's | May 9, 2026 — Nostalgic Knights

---

## 1. ARRIVE & CONNECT — RUN THESE FIRST

### List audio devices (confirm DJI mic is visible)
```
python main.py --devices
```
Look for your DJI Mic 2 USB receiver in the output. Note the `← use this` marker.
If it's not detected, use `--device-index N` to force it by index number.

### Test X32 connection + read channel state
```
python main.py --test-osc
```
Confirms X32 is reachable and prints all channel faders, mute state, and EQ.
Expected output shows channel names enriched with X32 tablet names, e.g.:
```
  1  Kick (KICK)      -3.0dB  ...
  9  Lead Vocal (LV)   0.0dB  ...
```
If connection fails: check X32 IP in `config/band.yaml` (currently `127.0.0.1` for sim — update to real board IP before the show).

---

## 2. SOUNDCHECK & BASELINE

### Run soundcheck mode
```
python main.py --soundcheck
```
- Gives real-time advisory against genre reference while you dial in the board
- Advisory cooldown is 20s (faster feedback than show mode)
- Watch for HPF, gain staging, and compressor advisories per channel

### Lock the baseline
```
confirm + Enter
```
Saves current channel state as the show baseline. Used for drift detection during the show.
Run this when you're happy with the mix — before doors open.

---

## 3. RUNNING THE SHOW

### Start show mode
```
python main.py --show
```

### Start show mode with a specific X32 IP
```
python main.py --show --x32-ip 192.168.1.XXX
```

### Force a specific audio input device by index
```
python main.py --show --device-index 2
```

---

## 4. AMBIENT BASELINE CAPTURE

Run this **after** connecting but **before** soundcheck for best results.
Can also be run mid-show between sets to update for crowd noise.

```
a + Enter
```
Then answer the prompts:
```
Capture type — empty room (e) or crowd break (c)?  →  e
Duration in seconds [60]:                          →  60
```
- **Empty room (e):** run pre-show, no crowd. Used during soundcheck.
- **Crowd break (c):** run during set break. Corrects readings for crowd noise during Set 2.
- Capture runs in the background — show loop keeps running.
- Once captured, `g` displays both raw and ambient-corrected band readings.

---

## 5. MID-SHOW KEYBOARD COMMANDS

All commands are typed and confirmed with **Enter**.

### Song Navigation

| Command | Action |
|---|---|
| `n` | Advance to next confirmed song |
| `n3` | Jump directly to slot 3 (works for any slot number) |
| `n-1` | Go back one song |
| `skip` | Skip current song, log it, advance to next |
| `ins` | **Insert an alternate as the very next song** — shows numbered list, you pick one. All remaining songs shift down one slot. Nothing is dropped. |
| `sw7` | Swap slot 7 with its pre-designated alternate, replacing it permanently |
| `add` | Prompt for song name + genre, append to end of setlist |

**Example — squeezing in an alternate between slots 15 and 16:**
```
ins + Enter
  Available alternates:
    1.  Hurts So Good (John Mellencamp)  [Hard Rock]
    2.  One Way or Another (Blondie)     [Hard Rock]
    3.  Wanted Dead or Alive (Bon Jovi)  [AOR]
    ...
  Select number:
3 + Enter
  [22:31] Inserted: Wanted Dead or Alive [AOR] — plays next (slot 16)
```
Then `n + Enter` plays the inserted song. Another `n + Enter` continues to what was Separate Ways (now slot 17). Nothing was dropped.

On any navigation event the terminal prints:
- Song name, genre, transition grace timer
- Genre shift details (LUFS target change, frequency band target changes)
- Any channels whose fader is significantly off from the incoming genre's weight targets

### Status & Analysis

| Command | Action |
|---|---|
| `s` | Print current board state — all channels, faders, mute, RMS |
| `g` | Print room analysis — LUFS, RMS, all frequency bands. Shows ambient-corrected readings side by side if a baseline is captured. |
| `b` | Print baseline drift — compare current faders to soundcheck baseline |
| `p` | Print full setlist with current position, played/skipped markers |

### Song Control

| Command | Action |
|---|---|
| `e` | End current song early (starts transition grace timer) |
| `break` | Enter set break mode — ends current song, pauses recommendations, logs the break. Press `n` to resume into the next set. |
| `a` | Capture ambient noise baseline (see Section 4) |

### Exit
```
Ctrl+C
```
Ends show mode and prints the full post-show report.

---

## 6. SET BREAK & SET 2

### Enter set break mode (end of Set 1)
```
break + Enter
```
- Ends the current song cleanly and logs SET_BREAK_START
- Recommendations pause — the board is still monitored, nothing disconnects
- Terminal prints a reminder to run ambient capture

### During the break — capture crowd ambient
```
a + Enter  →  c + Enter  →  60 + Enter
```
Crowd noise baseline updates automatically. Set 2 `g` readings will be corrected against it.

### Check where you are / see both sets
```
p + Enter
```
During the break, `p` shows the full setlist with:
- `>> 16. Separate Ways  <-- HERE` marking your last position
- `** SET BREAK - break in progress **` between the two sets
- Set 2 numbered 1–17 locally (matching your paper setlist)
- `*Ken*` / `*Eric*` flags on vocal-switch songs
- `[done]` / `[skip]` status on completed songs

### Start Set 2
```
n + Enter
```
Clears break state, logs SET_BREAK_END, loads Set 2 opener (Cult of Personality).
Or jump to a specific Set 2 song using the system slot number shown by `p`:
```
n17 + Enter   ← Cult of Personality
n22 + Enter   ← Gimme Three Steps  *Ken*
n25 + Enter   ← Lay Down Sally     *Ken*
n27 + Enter   ← Fire               *Eric*
n33 + Enter   ← Don't Stop Believin' (closer)
```
Run `p + Enter` at any time to see the full numbered list.

---

## 7. TONIGHT'S CHANNEL MAP

| Ch | Label | Who | Notes |
|---|---|---|---|
| 1 | Kick | — | Watch mud zone 300-500Hz |
| 4 | Drum Rack | — | Toms |
| 5 | Floor Tom | — | |
| 6 | Acoustic Guitar | Ken | Active when Ken switches off keys |
| 7 | Guitar 1 | — | Lead/rhythm — solo suppression active |
| 8 | Guitar 2 | — | Lead/rhythm — solo suppression active |
| 9 | Lead Vocal | Stephanie | Primary lead all show |
| 10 | Drum Vocal | Mitch | Open all show — lead only on Fight for Your Right |
| 11 | Bassist Vocal | Eric | Lead: Kryptonite (S1/5), Sharp Dressed Man (S1/13), Fire (S2/11) |
| **12** | **Keys Vocal** | **Ken** | Lead: Can't Get Enough (S1/9), Gimme Three Steps (S2/6), Lay Down Sally (S2/9) |
| 13 | Bass | — | |
| 15 | Keys | Ken | Muted when Ken is on acoustic (ch 6) |

---

## 8. SONGS THAT NEED PRE-SONG CHECKS

| Slot | Song | Flag |
|---|---|---|
| S1/5 | Kryptonite | Eric (ch 11) is lead vocal — confirm mic live |
| S1/9 | Can't Get Enough | Ken (ch 12) is lead vocal — confirm mic live |
| S1/13 | Sharp Dressed Man | Eric (ch 11) is lead vocal — confirm mic live |
| S1/16 | Separate Ways | Keys intro cold — confirm ch 15 level before downbeat |
| S2/2 | Here I Go Again | Keys + vocal open cold — confirm ch 15 and ch 9 before downbeat |
| S2/6 | Gimme Three Steps | Ken (ch 12) is lead vocal — confirm mic live |
| S2/9 | Lay Down Sally | Ken (ch 12) is lead vocal — confirm mic live |
| S2/10 | Me and Bobby McGee | Acoustic start — Ken switches to ch 6, ch 15 muted |
| S2/11 | Fire | Eric (ch 11) is lead vocal — confirm mic live |
| S2/17 | Don't Stop Believin' | Closer — keys intro, confirm ch 15 level |

---

## 9. QUICK REFERENCE — FULL SETLIST

### Set 1 (slots 1–16)
```
 1. Working for the Weekend   (Loverboy)      AOR       cowbell-2-3-4
 2. Any Way You Want It       (Journey)       AOR       1-2 1-2-3-sn-cr
 3. Hit Me With Your Best Shot(Pat Benatar)   Hard Rock 1-2-3-snare
 4. Summer of '69             (Bryan Adams)   Hard Rock 1-2-3-snare
 5. Kryptonite ★ERIC          (3 Doors Down)  Post-Grunge guitar start
 6. Danger Zone               (K. Loggins)    AOR       bass start
 7. Superstition              (Stevie Wonder) Hard Rock drum start
 8. Fallen Angel              (Poison)        Glam Metal guitar start
 9. Can't Get Enough ★KEN     (Bad Company)   Hard Rock 1-2 1-2-3
10. Heartache Tonight         (Eagles)        AOR       1-2 1-2-3-4
11. Fortunate Son             (CCR)           Hard Rock 2x drum intro
12. Sweet Child of Mine       (GNR)           Hard Rock guitar 4x med
13. Sharp Dressed Man ★ERIC   (ZZ Top)        Hard Rock drum intro
14. I Hate Myself for Loving You (Joan Jett)  Hard Rock drum intro
15. Paris                     (Grace Potter)  AOR       guitar start
16. Separate Ways             (Journey)       AOR       keys intro ⚠
```

### Set 2 (slots 17–33, navigate with n17–n33)
```
17. Cult of Personality       (Living Colour) Hard Rock
18. Here I Go Again           (Whitesnake)    AOR       keys-vocal intro ⚠
19. Heartbreaker              (Pat Benatar)   Hard Rock drum/gtr x4
20. Beat It                   (M. Jackson)    Hard Rock keys start
21. Hot Blooded               (Foreigner)     AOR       drum/guitar start
22. Gimme Three Steps ★KEN    (Lynyrd Skynyrd)Hard Rock
23. Round and Round           (Ratt)          Glam Metal click off
24. Your Love                 (The Outfield)  AOR       guitar start
25. Lay Down Sally ★KEN       (Eric Clapton)  Hard Rock drum/gtr
26. Me and Bobby McGee        (Janis Joplin)  Hard Rock acoustic ⚠
27. Fire ★ERIC                (Jimi Hendrix)  Hard Rock
28. Trooper                   (Iron Maiden)   Hard Rock
29. Rock of Ages              (Def Leppard)   Glam Metal drum/cowbell
30. Man in the Box            (Alice in Chains)Hard Rock click off
31. Welcome to the Jungle     (GNR)           Hard Rock gtr riff
32. Crazy Train               (Ozzy)          Hard Rock bass-drums
33. Don't Stop Believin'      (Journey)       AOR       piano/bass ⚠
```
`★` = lead vocal switch  `⚠` = pre-song level check required

---

## 10. SIMULATOR (DEVELOPMENT ONLY)

Run the X32 simulator in a separate terminal before connecting:
```
python simulator/x32_sim.py
python simulator/x32_sim.py --scenario simulator/scenarios/level_creep.yaml
```

Run tests:
```
python -m pytest tests/ -q
```
