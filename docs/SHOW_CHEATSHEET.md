# FOH Assistant — Show Night Cheatsheet
## Bogue Sound Distillery | June 19, 2026 — Nostalgic Knights

---

## 1. PACK LIST

- [ ] Laptop + power cable
- [ ] AT2035 condenser mic + XLR cable
- [ ] PreSonus Studio 26c + USB cable
- [ ] Mic stand + foam windscreen
- [ ] Laser rangefinder
- [ ] Ethernet cable (backup if WiFi flaky)

---

## 2. ARRIVE & SETUP — IN ORDER

### Step 1 — Connect hardware
- Plug PreSonus Studio 26c into USB
- Plug AT2035 into PreSonus **Ch1**
- Enable **48V phantom power** on Ch1 (button on front panel)
- Set gain to ~12 o'clock to start

### Step 2 — Confirm mic is detected
```
python main.py --devices
```
Look for `Microphone (Studio 26c)` with `← AT2035 via PreSonus Studio 26c` marker.

### Step 3 — Open settings and configure for tonight
```
python main.py --settings
```

**Settings menu navigation:**

```
SETTINGS MAIN MENU
  1. Venue
  2. Session
  3. Mic & Audio
  4. Band
  5. System
  0. Exit
```

**→ 1 (Venue) → 1 (Select venue)**
Pick: `bogue_sound_distillery`
Then `0` back to venue menu.

**→ 2 (Session) → 1 (X32 Connection)**
Enter the X32 IP address (check tablet or ask John)
Then `0` back.

**→ 2 (Session) → 2 (Mic Placement)**
After measuring with rangefinder (do this once mic stand is in final position):
```
  1. Speaker L to mic    → enter distance in meters
  2. Speaker R to mic    → enter distance in meters
  3. Sub L to mic        → enter distance in meters
  4. Sub R to mic        → enter distance in meters
  5. Speaker height      → enter height in meters
  6. Mic height          → default 1.5m, adjust if different
  8. Mark confirmed      → locks distances with timestamp
```
`0` back when done.

**→ 2 (Session) → 3 (Setlist)**
Pick: `setlist_distillery_20260619`
`0` back.

**→ 2 (Session) → 5 (Print checklist)**
Run through PA items before soundcheck.

**→ 3 (Mic & Audio) → 2 (Test input level)**
Play something through PA or speak loudly. Aim for **-12 to -6dBFS** peaks.
Adjust PreSonus gain knob until level is in range.
`0` back.

**→ 0 (Exit settings)**

### Step 4 — Test X32 connection
```
python main.py --test-osc
```
Should print all channels with faders, mute state, EQ.
If it fails: re-enter IP in Settings → Session → X32 Connection.

---

## 3. DISTILLERY-SPECIFIC NOTES

**Corner stage** — Band plays from corner, not flat wall.
- Both subs have two-wall boundary reinforcement → expect +4–6dB extra sub
- Pull sub attenuation back before soundcheck
- Mic should be placed **diagonally out from the corner**, centered in the audience area — not straight in front of stage
- Try to be equidistant from left and right speaker stacks

**High ceilings** — More natural reverb, but possible flutter echo in 2–4kHz range.
If vocals sound harsh or "slappy", check for flutter echo in upper-mid.

**PA settings checklist (confirm before soundcheck):**
- [ ] Sub attenuation pulled back (corner loading adds significant sub)
- [ ] Sub phase alignment checked
- [ ] PA coverage angle reaches full audience from corner position

---

## 4. SOUNDCHECK

```
python main.py --soundcheck --display
```

**Before band plays — capture empty room ambient:**
```
a + Enter → e + Enter → 60 + Enter
```
Runs 60s background capture. Show loop keeps running.

**Run soundcheck, dial in the board**

**During first full-band passage — run cal scan:**
```
cal + Enter
```
Prints comparison of each channel's actual spectrum vs model prediction.
Updates instrument priors. Run once or twice during soundcheck.

**For individual channel isolation (if time permits):**
```
iso 7 + Enter    ← Guitar 1
iso 9 + Enter    ← Lead Vocal
iso 13 + Enter   ← Bass
```
Solo that channel on the board first, then press Enter when ready.
12-second capture. Updates priors more accurately than cal scan.

**Lock the baseline when mix is dialed in:**
```
confirm + Enter
```

---

## 5. RUNNING THE SHOW

```
python main.py --show --display
```
(No IP or venue flags needed — session file carries them from settings)

**The display window shows three curves:**
- **White/gray** = Board RTA (what the board is outputting)
- **Amber/orange** = Room Mic (what the audience is hearing)
- **Cyan dashed** = Genre Target (what we want)
- **Warm highlights** (yellow/orange/red) = mic reading above target in that band
- **Cool highlights** (blue) = mic reading below target
- **Dimmed bands** = low confidence (less reliable data)

**Before Set 1 — capture ambient if not done in soundcheck:**
```
a + Enter → e + Enter → 60 + Enter
```

---

## 6. MID-SHOW KEYBOARD COMMANDS

### Song Navigation
| Command | Action |
|---|---|
| `n` | Next song |
| `n3` | Jump to slot 3 |
| `n-1` | Go back one song |
| `skip` | Skip current song and advance |
| `ins` | Insert alternate as next song (shows numbered list) |
| `p` | Print full setlist with current position |

### Analysis
| Command | Action |
|---|---|
| `s` | Board state — all channels, faders, mute, RMS |
| `g` | Room analysis — LUFS, all frequency bands |
| `b` | Baseline drift — compare current board to soundcheck |
| `cal` | Live calibration scan (run during stable full-band passage) |

### Mid-Show Settings (doesn't stop the show)
| Command | Action |
|---|---|
| `settings` | Open settings menu — show keeps running in background |

Use `settings` if:
- Mic stand gets moved → update Mic Placement distances
- X32 IP changes → update Connection
- Need to check/adjust anything without restarting

### Set Break
| Command | Action |
|---|---|
| `break` | End Set 1, pause recommendations, log break |
| `a` → `c` | Capture crowd ambient during break |
| `n` | Start Set 2 (after break) |

### Exit
```
Ctrl+C
```
Prints full post-show report and archives session.

---

## 7. TONIGHT'S CHANNEL MAP

| Ch | Label | Who | Notes |
|---|---|---|---|
| 1 | Kick | — | |
| 4 | Drum Rack | — | |
| 5 | Floor Tom | — | |
| 6 | Acoustic Guitar | Ken | Active when Ken off keys |
| 7 | Guitar 1 | — | |
| 8 | Guitar 2 | — | |
| 9 | Lead Vocal | Stephanie | Primary lead all show |
| 10 | Drum Vocal | Mitch | Open all show |
| 11 | Bassist Vocal | Eric | Lead: Kryptonite, Fire |
| 12 | Keys Vocal | Ken | Lead: Gimme Three Steps, Lonely Ole Night |
| 13 | Bass | — | |
| 15 | Keys | Ken | Muted when Ken on acoustic (ch 6) |

---

## 8. PRE-SONG CHECKS

| Slot | Song | Check |
|---|---|---|
| S1/7 | Kryptonite | ★ Eric (ch 11) lead — confirm mic live |
| S1/13 | Gimme Three Steps | ★ Ken (ch 12) lead — confirm mic live |
| S1/18 | Separate Ways | ⚠ Keys intro cold — confirm ch 15 level |
| S2/2 | Here I Go Again | ⚠ Keys + vocal cold — confirm ch 15 + ch 9 |
| S2/10 | Lonely Ole Night | ★ Ken (ch 12) lead + bass to Ken |
| S2/11 | Fire | ★ Eric (ch 11) lead + bass to Ken |
| S2/12 | Me and Bobby McGee | ⚠ Acoustic start — Ken → ch 6, ch 15 muted |

---

## 9. SETLIST

### Set 1 (18 songs)
```
 1. Working for the Weekend   (Loverboy)       AOR        cowbell-2-3-4
 2. Any Way You Want It       (Journey)        AOR        1-2 1-2-3-sn-cr
 3. Hurts So Good             (Mellencamp)     Hard Rock  2x drum intro
 4. One Way or Another        (Blondie)        Hard Rock  guitar start
 5. Hit Me With Your Best Shot(Pat Benatar)    Hard Rock  1-2-3-snare
 6. Summer of '69             (Bryan Adams)    Hard Rock  1-2-3-snare
 7. Kryptonite         ★ERIC  (3 Doors Down)   Post-Grunge guitar start
 8. Danger Zone               (K. Loggins)     AOR        bass start ½↓
 9. Superstition              (Stevie Wonder)  Hard Rock  drum start ½↓
10. Nightrain                 (Guns N Roses)   Hard Rock  cowbell start ½↓
11. Fallen Angel              (Poison)         Glam Metal guitar start
12. Fortunate Son             (CCR)            Hard Rock  2x drum intro
13. Gimme Three Steps  ★KEN   (Lynyrd Skynyrd) Hard Rock  guitar start
14. Lovin' Touchin' Squeezin' (Journey)        AOR        guitar/drums
15. Heartache Tonight         (Eagles)         AOR        1-2 1-2-3-4
16. I Hate Myself...          (Joan Jett)      Hard Rock  drum intro
17. Paris                     (Grace Potter)   AOR        guitar start
18. Separate Ways             (Journey)        AOR        keys intro ⚠
```

### Set 2 (18 songs — navigate with n19–n36)
```
19. Cult of Personality       (Living Colour)  Hard Rock
20. Here I Go Again           (Whitesnake)     AOR        keys-vocal ⚠
21. Heartbreaker              (Pat Benatar)    Hard Rock  drum/gtr x4 ½↓
22. Beat It                   (M. Jackson)     Hard Rock  keys start ½↓
23. Hot Blooded               (Foreigner)      AOR        drum/guitar
24. Play That Funky Music     (Wild Cherry)    Hard Rock  bass start
25. Brick House               (Commodores)     Hard Rock
26. Round and Round           (Ratt)           Glam Metal click off
27. Your Love                 (The Outfield)   AOR        guitar start
28. Lonely Ole Night   ★KEN   (Mellencamp)     Hard Rock  guitar/drums
29. Fire               ★ERIC  (Jimi Hendrix)   Hard Rock  1-2-1-2-3
30. Me and Bobby McGee        (Janis Joplin)   Hard Rock  acoustic ⚠
31. The Trooper               (Iron Maiden)    Heavy Metal
32. Nothing But a Good Time   (Poison)         Glam Metal guitar start
33. Rock of Ages              (Def Leppard)    Glam Metal drum/cowbell
34. Welcome to the Jungle     (GNR)            Hard Rock  gtr riff
35. Thunderstruck             (AC/DC)          Hard Rock  ½↓
36. Don't Stop Believin'      (Journey)        AOR        piano/bass ⚠
```
`★` = vocal switch  `⚠` = pre-song level check  `½↓` = half step down for vocalist

### Alternates (use `ins` command)
```
Pour Some Sugar on Me  (Def Leppard)    Glam Metal
Sweet Emotion          (Aerosmith)      Hard Rock
Rock & Roll All Night  (KISS)           Hard Rock
Walking on Sunshine    (Katrina/Waves)  Party Rock
You Oughta Know        (Alanis)         Post-Grunge
Sweet Child of Mine    (GNR)            Hard Rock
Wanted Dead or Alive   (Bon Jovi)       AOR
Rock You Like Hurricane(Scorpions)      Hard Rock
Blister in the Sun     (Violent Femmes) Party Rock  ★KEN
Kiss Me Deadly         (Lita Ford)      Glam Metal  ½↓
Lay Down Sally  ★KEN   (Eric Clapton)   Hard Rock
Man in the Box         (Alice in Chains)Post-Grunge ½↓
Walk This Way          (Aerosmith)      Hard Rock   ½↓
Crazy Train            (Ozzy)           Hard Rock
```

---

## 10. IF SOMETHING GOES WRONG

**Display window freezes:**
Close the window — it will reopen automatically in 2 seconds.

**Settings froze the show:**
This was fixed — settings now runs in background. If it somehow freezes,
Ctrl+C and restart. Session file preserves your venue/setlist/distances.

**Lost X32 connection:**
`python main.py --test-osc` to confirm IP. Re-enter in settings if needed.
Show log is saved continuously — no data lost on restart.

**Mic not detected:**
Check PreSonus USB is connected and 48V is on. Run `--devices` to confirm.

**Start over mid-show:**
```
python main.py --show --display
```
Use `n{slot}` to jump back to where you were in the setlist.
Previous show log is preserved with a timestamp.
