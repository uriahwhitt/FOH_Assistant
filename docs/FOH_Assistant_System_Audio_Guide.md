# FOH Assistant — System Audio Guide
**Version:** 1.1
**Status:** Active reference — feeds recommendation engine, analyzer, and target curve refinement
**Last Updated:** 2026-05-26
**Author:** Uriah Whittemore

> **Architecture note (updated May 2026):** This guide was written when the room microphone was the primary per-channel intelligence source. The architecture has since shifted. The room mic is now used for LUFS monitoring and room acoustic characterization only. Per-channel intelligence comes from X32 OSC meter data and EQ transfer function calculations. Sections in this guide that reference "the mic detecting which channel is causing a problem" should be understood as referring to the forward model's channel contribution analysis, not direct mic detection. All frequency engineering principles, thresholds, instrument profiles, and genre targets remain correct and are still consumed by the recommendation engine.
**Companion to:** FOH_Assistant_Scope_v0.6.md, FOH_Assistant_Phase1_Implementation.md, FOH_Assistant_Design_Improvements.md

---

## Purpose

This document is the audio-engineering knowledge layer that informs the FOH Assistant's analysis algorithms, recommendation engine, and target curves. The scope and implementation docs describe *what the system does*; this document describes *what the system knows about sound* and *why each recommendation is the right one*.

It exists for three reasons:

1. **Algorithm grounding.** Every threshold, fingerprint, target, and rule in the engine has a defensible audio-engineering basis. When tuning thresholds or adjusting frequency targets, this document is the reference for whether a change reflects established practice or contradicts it.
2. **Recommendation quality.** Recommendations should sound like they came from a competent FOH engineer — not a generic spectrum-matcher. That requires the system to understand the *why* behind the move, not just the deviation magnitude.
3. **Refinement source.** As the engine evolves toward Phase 4+ (automated adjustments) and Phase 6 (reference-audio targeting), this document is the source for converting general principles into specific algorithmic rules.

The structure goes broad to narrow: signal-flow theorem first, then live-sound discipline, then FOH mixing technique, then the per-instrument and per-genre detail the engine actually uses.

---

## Part 1 — Signal Theorem and Acoustic Foundations

### 1.1 The decibel — three flavors that get conflated

The engine deals in three different decibel scales, and conflating them is the most common source of bad logic in audio software.

| Scale | Reference | What it measures | Where it appears in FOH Assistant |
|---|---|---|---|
| **dB SPL** | 20 µPa (threshold of hearing) | Acoustic pressure in air | Room loudness, hearing-safety limits, venue volume targets |
| **dBFS** | Digital full scale (clipping) | Digital signal level relative to clipping ceiling | X32 channel meters (`/meters/1`), DJI USB receiver capture levels |
| **dBu / dBV** | 0.775V / 1V RMS | Analog electrical signal level | Not directly visible — implied by preamp trim and console gain stages |

**LUFS** is a fourth, perceptual scale built on dBFS but K-weighted (rolling off below ~100 Hz, accentuating above ~2 kHz) to model how the ear actually weights frequencies. LUFS is the only loudness scale that correlates well with how loud a mix *feels* across different content; it's why the engine uses integrated LUFS as the primary loudness target rather than RMS or peak.

**Decibels are logarithmic.** A 3 dB change is roughly a doubling of acoustic power, but only a ~25% change in perceived loudness. A 10 dB change is the rule-of-thumb "twice as loud" in human perception. This matters for the engine in two places:

- **Threshold sensitivity.** A 3 dB band deviation is a real, audible problem; a 1 dB deviation is below most engineers' decision threshold. The default `recommendation_trigger_db: 3.0` is consistent with this — at that threshold, the deviation is large enough to be heard and acted on.
- **Fader resolution.** Faders are logarithmic, so the *visual* distance for a 1 dB move at the bottom of fader travel (-30 dB and below) is tiny, while a 1 dB move near unity is much wider. The X32 fader float-to-dB curve is piecewise linear specifically to give engineers fine resolution near unity where mixing actually happens.

### 1.2 The frequency spectrum and what lives where

The audible range is 20 Hz to 20 kHz. The engine bins it into seven bands (sub bass, bass, low mid, mid, high mid, presence, air) which align with how engineers actually talk about frequency problems. The boundaries are chosen to put each band's center where the perceptual character is consistent, not at evenly-log-spaced cutoffs.

**Sub bass — 20–80 Hz.** Felt more than heard. Kick drum bottom, bass guitar fundamentals on low strings, room rumble, HVAC, stage vibration, mic-handling noise. Almost always benefits from a high-pass filter on every channel except kick and bass. Below ~25 Hz is functionally inaudible but eats headroom and can damage subs.

**Bass — 80–250 Hz.** Fundamental power of kick, bass guitar, low piano, low male vocal body, floor toms. The "warmth and body" range. Rooms typically have modal problems here that are room-specific, not source-specific — meaning a 200 Hz buildup in one venue may be the room, not the band.

**Low mid — 250–500 Hz.** "Mud" range. The single most common live-sound complaint ("the mix sounds muddy") almost always traces to buildup in this band. Multiple sources stack here: kick body, snare body, guitar body, low vocal range, piano, keys. Subtractive EQ on individual sources here usually clears mixes more than any other intervention.

**Mid — 500 Hz to 2 kHz.** The "meat" of most instruments and the bulk of vocal intelligibility. Where guitar tone lives, where snare crack lives, where vocal articulation lives. The most crowded band in a typical rock mix. Boost here makes things prominent; cut here pushes things back without changing volume.

**High mid — 2–6 kHz.** The presence and attack range. Where the human ear is most sensitive (per the equal-loudness contours below). Vocal clarity, guitar bite, snare attack, cymbal stick definition. Also the harshness range — too much here causes ear fatigue and is the most common "the PA is killing me" complaint.

**Presence — 6–12 kHz.** Air and clarity, sibilance ('s', 'sh', 't'), cymbal sheen, guitar string noise. Sibilance lives 4–10 kHz depending on the singer. Too much makes vocals harsh and brittle; too little makes them dull.

**Air — 12 kHz+.** Sparkle, sense of space, transient detail. Often the difference between "good" and "expensive-sounding." Below the noise floor of many cheap mics; can be safely high-shelved up on quality vocal mics. Easily damaged by over-compressed sources.

### 1.3 Equal-loudness contours (Fletcher-Munson) — why mix volume matters

The human ear is not equally sensitive to all frequencies. The Fletcher-Munson curves (and their modern successor ISO 226:2003) show that at low listening volumes, we hear midrange most clearly and lose both lows and highs; as volume rises, our response flattens out.

**The implications for FOH:**

1. **A mix that sounds balanced at quiet rehearsal volume will sound bass-heavy and bright at show volume.** Mixing decisions made at one SPL don't translate directly to another.
2. **The 2–4 kHz band is where the ear is most sensitive at all volumes.** Cuts here are perceived as much larger than the actual dB amount; boosts cause ear fatigue fast. The engine should weight deviations in high mid more heavily than equivalent deviations in sub bass for psychoacoustic accuracy.
3. **K-weighting in LUFS is exactly this compensation, applied to measurement.** LUFS rolls off below 100 Hz and accentuates above 2 kHz, which is why integrated LUFS correlates with perceived loudness even when peak and RMS don't.
4. **Reference monitor calibration matters.** When the reference mic (DJI Mic 2) is at audience ear height, its readings reflect what the audience perceives — but only at that specific position. Room nodes and listener position vs. PA position can shift band readings by ±6 dB across a venue.

**Engine application:** The frequency band targets in genre profiles (e.g., AOR `presence: +2`) are written assuming a show-volume listening context (95–105 dB SPL). At that volume, the ear's response is closer to flat than at quiet volumes, so the targets are honest representations of "balanced at this volume." If the band is rehearsing at 75 dB SPL and the engine compares to a show-volume target curve, false positives in low and high will increase. A future enhancement (Phase 3+) should consider applying an inverse Fletcher-Munson correction to the target curve based on measured room SPL.

### 1.4 Headroom, noise floor, and signal-to-noise

Every gain stage has a ceiling (clipping) and a floor (noise). The space between them is the dynamic range of that stage. Headroom is how much room remains above the average signal before clipping; signal-to-noise is how much remains above the noise floor.

**The classic gain-staging mistake:** Set preamp trim too low, then push faders up to compensate. The fader-stage amplifier is amplifying the noise floor along with the signal, so the system gets noisier and feedback-prone. The correct approach is to maximize gain at the first stage (preamp), then leave the rest of the chain near unity.

**For live sound, the rule of thumb:**
- Set preamp trim so the loudest expected peaks light the clip indicator briefly, then back off ~10–15 dB.
- Channel fader at unity (0 dB / -10 dB) at typical mix position.
- Master fader at unity.
- All processing (EQ, compression) operates in the headroom window between average signal and the ceiling.

**Engine application:** When the engine sees a channel with `fader_db > +5`, this is a flag — the engineer is compensating for low gain at the preamp. Recommendations should consider preamp trim before recommending further fader moves. Phase 1 doesn't read preamp trim directly, but `/ch/##/preamp/trim` is available and should be incorporated in Phase 2+ as part of the channel state snapshot.

### 1.5 The seven gain stages and where they go wrong

In a typical X32-fed PA, signal passes through:

1. **Source level** — instrument output, vocal projection, drum hit force
2. **Microphone or DI** — capsule sensitivity, pickup output
3. **Console preamp (trim)** — primary gain stage, maximizes S/N
4. **Channel processing** — gate, EQ, dynamics (each can boost or cut)
5. **Channel fader** — mix balance
6. **Bus / DCA / Main fader** — group and master level
7. **System processor / amplifier / speaker** — drive level, room coupling

Distortion, noise, and feedback can originate at any stage. The diagnostic discipline is: **identify which stage is misbehaving before adjusting any other stage.** Pulling a fader down to fix preamp distortion only buries the distortion under the noise floor; the distortion is still in the recording and the monitor send.

**Engine application:** This is the conceptual backbone of the recommendation engine's culprit attribution. When a frequency band deviation is detected in the room, the engine asks "which stage is responsible?" — channel meters identify stage 3; channel EQ state identifies stage 4; fader position identifies stage 5; baseline drift identifies whether the problem is new or pre-existing. Reference mic readings alone don't tell you which stage to fix, only that something is wrong somewhere.

---

## Part 2 — Live Sound vs. Studio Mixing

The engine is deliberately tuned for live FOH, not studio mixing. The differences are not cosmetic — they change what counts as a problem, what counts as a fix, and how aggressive a recommendation should be.

### 2.1 Different goals

| | Studio | Live FOH |
|---|---|---|
| Primary goal | Lasting recorded artifact, listenable on every system | Audience experience right now, in this room |
| Time horizon | Hours to weeks per song | Real-time, no second take |
| Loudness target | Platform-normalized (-14 LUFS streaming) | Show-appropriate SPL (95–110 dB SPL typical) |
| Dynamic range | Preserved or reduced as artistic choice | Compressed for intelligibility against ambient noise |
| EQ philosophy | Surgical, source-of-truth corrections | Pragmatic, room-and-volume context corrections |
| Boost vs. cut | Boost freely if it sounds good | Cut almost always — boost increases feedback risk |
| Reference | Studio monitors in treated room | Reference mic at audience position |
| Reset between attempts | Unlimited | None — every move is committed live |

### 2.2 Cut, don't boost

The most repeated principle in live sound: **subtractive EQ first.** There are three reasons.

1. **Feedback.** Every dB of EQ boost is a dB of additional loop gain through the mic-to-monitor-to-mic feedback path. Boosting a frequency on a mic channel is the easiest way to ring a stage. Cutting a frequency is acoustically free — it cannot cause feedback.
2. **Headroom.** Boost adds gain at that stage. If a channel was already 6 dB below clipping and you add a +6 dB EQ boost, you're at the ceiling. Cuts give back headroom.
3. **Mix space.** Most "this isn't loud enough" problems are actually masking problems — the source is buried in another source's frequency range. Cutting the masking source brings out the buried source without raising any fader, and the mix gets clearer instead of louder.

**Engine application:** When choosing between recommending a cut on a contributing channel vs. a boost on a deficient channel, the engine should default to cut. The exception is air-band content (12 kHz+) on vocals, which is often safely shelved up because it's above the typical feedback-prone range and below the level where most live PA mics deliver air without help.

### 2.3 The room is half the mix

In studio, the monitors are calibrated and the room is treated. In live, the room is a giant unequal-frequency-response filter you cannot remove. Standing waves, reflections, audience absorption, HVAC, stage bleed — every venue has its own signature.

**The implication:** The same band, same instruments, same console settings will sound different in every venue. A recommendation that's correct in one room is wrong in another. The engine must be able to represent venue character separately from band character separately from genre target — which is why IMP-016 (Venue Profile Layer) is a roadmap item.

Until venue profiles are captured, the baseline-snapshot mechanism is the partial solution: the soundcheck baseline implicitly captures the room's contribution to the band's sound at this venue today. Deviations from baseline are venue-aware in a way that genre-target comparisons aren't.

### 2.4 Stage volume vs. PA mix

In a small venue with loud guitar amps and a kit not in the PA, the PA is doing reinforcement, not full reproduction. The audience hears stage sound + PA sound combined. In a large venue, the PA is doing everything; stage volume is irrelevant to the audience.

**Reference mic implications:**
- In a small venue (typical bar gig), the DJI Mic 2 captures stage sound dominated by drums and guitar amps, with vocals and direct-input instruments coming primarily from the PA.
- In a large venue, the reference mic captures essentially the PA mix.
- The engine cannot distinguish "loud guitar amp at 200 Hz" from "loud guitar in PA at 200 Hz" without channel-meter correlation. This is exactly why the recommendation engine combines reference-mic frequency analysis with X32 channel meter levels — channel meters reveal what the PA is contributing; reference mic shows what the audience hears; the difference is room and stage bleed.

---

## Part 3 — Gain Staging in Live FOH

Gain staging is not glamorous and not usually visible to the engine — but it is the foundation everything else sits on. Bad gain staging causes feedback, noise, and distortion regardless of how good the EQ is.

### 3.1 The textbook process

1. **Channel fader to unity** (0 dB / -10 dB depending on console preference).
2. **Engage PFL** (pre-fader listen) on the channel so you hear it independently.
3. **Source plays at expected loudest performance level.** "Sing the loudest part of the loudest song" — not "check, check, one two."
4. **Bring up preamp trim** until the channel meter peaks ~-6 to -10 dBFS (15 dB headroom typical for live).
5. **High-pass filter engaged** on everything except kick, bass, floor tom, and any source with fundamental content below 80–100 Hz.
6. **Repeat for every channel.**
7. **Bring channels into the mix** at unity, balance via faders.

### 3.2 Why this specific order

If you set preamp gain with the fader anywhere other than unity, your relationship between visible fader position and actual signal level is broken — every fader becomes a unique relative scale. Setting all preamps with faders at unity means the mix-down position is your reference, not some arbitrary intermediate state.

### 3.3 Where engineers commonly fail

- **"Set it once at sound check and forget it."** Singers project differently when the room is full. A vocal mic that was perfect at sound check often clips during the show as the singer pushes harder; or runs too quiet because the singer was warming up. Engine implication: large fader moves on vocal channels mid-show are often gain-staging compensation, not mix decisions.
- **Preamp gain too low, faders pushed up to compensate.** Symptom: noisy mix, feedback at lower master levels than expected. Engine implication: if a channel reads moderate RMS but the fader is at +5 or higher, flag as possible gain-staging issue.
- **Pads engaged unnecessarily.** A 20 dB pad reduces signal-to-noise ratio by 20 dB. Only engage when the input would otherwise overload the preamp at minimum trim.

### 3.4 Engine extensions for gain staging awareness

The X32 OSC protocol exposes everything the engine needs:

```
/ch/##/preamp/trim    — primary gain stage
/ch/##/preamp/hpon    — high-pass engagement
/ch/##/preamp/hpf     — high-pass corner frequency
/ch/##/preamp/hpslope — high-pass slope
```

Phase 2+ should include preamp state in `ChannelState` and add the following recommendation triggers:

- Preamp trim above 50 dB on a non-dynamic-mic source → likely a low-output source or a too-quiet performer; consider mic technique
- Channel RMS below -30 dBFS with fader above +5 dB → gain-staging inversion, recommend trim increase + fader to unity
- High-pass filter disabled on a vocal channel → recommend HPF at 80 Hz minimum (suppress for kick/bass)

---

## Part 4 — EQ in Live FOH

### 4.1 The three EQ stages

A complete live mix uses EQ at three distinct points, each for a different purpose. Conflating them produces cascading problems.

**Stage 1: Channel EQ (per-source).** Corrects the individual instrument or voice. Removes mud, sibilance, harshness, rumble specific to that channel. Lives on the X32 channel strip — 4 bands of fully parametric EQ. This is where the engine focuses its recommendations.

**Stage 2: Output / system EQ.** Tunes the PA system to the room. Compensates for room modes and PA frequency response. Lives on the main bus EQ (`/main/st/eq`) and bus EQ — 6-band on X32. Set during system tuning before sound check, rarely touched during the show.

**Stage 3: Monitor / feedback EQ.** Notches out feedback frequencies on individual monitor mixes. Lives on each monitor bus (`/bus/##/eq`). Engineering goal is gain-before-feedback, not tone.

**Engine application:** Phase 1 reads channel EQ only, which is correct — those are the mixing-decision EQ moves. Bus EQ should be read but not flagged for recommendations in early phases (it's typically set once and forgotten). Monitor EQ is out of scope until monitor-mix awareness is added (Phase 4+).

### 4.2 Filter types — when to use what

The X32's 4-band channel EQ supports six filter types:

| Type | Use case |
|---|---|
| **LCut (high-pass)** | Default Band 1 on vocals and most instruments. Set 80–100 Hz, 12 dB/oct. Removes rumble and gives 6+ dB headroom back. |
| **LShv (low shelf)** | Boost or cut everything below the corner. Use for adding warmth (low shelf boost on bass) or removing it (low shelf cut on hi-hat). |
| **PEQ (parametric)** | Workhorse — full Q control, surgical or broad. Use for problem-solving. |
| **VEQ (vintage)** | Same parameters as PEQ but with non-linear behavior emulating analog EQs. Sounds smoother under heavy boost but less precise. |
| **HShv (high shelf)** | Boost or cut everything above the corner. Use for adding air (high shelf at 10 kHz on vocals) or removing brightness (high shelf cut on harsh source). |
| **HCut (low-pass)** | Remove everything above the corner. Use sparingly — only when high-end content is genuinely unwanted (sub-bass channel, muddy bass DI, heavy stage bleed on a drum mic). |

**Engine application — band selection logic** (already implemented per IMP-004): When recommending an EQ adjustment, match the band to the problem frequency within 2 octaves. If no band qualifies, recommend adding a new EQ point at the problem frequency rather than misusing an unrelated band. Default Band 1 to LCut at 80 Hz on every vocal and most instruments unless the engineer has set otherwise.

### 4.3 Q (bandwidth) and surgical vs. tonal EQ

Q controls the width of an EQ band. High Q (narrow) is surgical — for notching out a specific resonance or feedback frequency. Low Q (wide) is tonal — for shaping overall character.

**Rule of thumb (live sound):**
- **Cuts: narrow Q (Q > 2)** when fixing a problem frequency.
- **Boosts: wide Q (Q < 1.5)** because the ear hates narrow boosts; they sound unnatural and call attention to the EQ.
- **Notches: very narrow Q (Q > 8)** for feedback elimination — minimal damage to surrounding frequencies.

**Engine application:** When recommending a cut, default to Q ≈ 2.0 (moderate-narrow). When recommending a boost, default to Q ≈ 1.0 (wide). When recommending a feedback notch (Phase 2+ feature), default to Q ≈ 10.0.

### 4.4 The classic EQ moves by purpose

These are the dozen or so moves that solve 80% of live-mix problems. The engine should recognize the *purpose* of each, because that's how engineers think — not in raw Hz/dB but in named moves.

| Named move | Frequency | Direction | Purpose |
|---|---|---|---|
| HPF | 60–120 Hz | Cut everything below | Remove rumble, free up headroom |
| Mud cut | 200–400 Hz | -2 to -6 dB, wide Q | Clear up muddy mix |
| Boxiness cut | 400–800 Hz | -2 to -4 dB, medium Q | Remove "behind a curtain" sound |
| Honk cut | 800 Hz–1.5 kHz | -2 to -4 dB, medium Q | Remove nasal/honky character |
| Presence boost | 2–5 kHz | +1 to +3 dB, wide Q | Vocal/instrument intelligibility |
| Harshness cut | 3–5 kHz | -1 to -3 dB, medium Q | Tame fatiguing high mid |
| Sibilance control | 5–8 kHz | -1 to -3 dB, narrow Q | Tame harsh "s/sh/ch" sounds |
| Air boost | 10–15 kHz, shelf | +1 to +3 dB | Open up the top end |
| Body boost | 80–150 Hz | +1 to +3 dB, wide Q | Add fullness to thin sources |
| Punch boost | 60–80 Hz | +2 to +4 dB, narrow-medium Q | Kick/bass weight |

**Engine application:** Recommendation output should describe the *named move* rather than just the frequency change. "Mud cut at 315 Hz" is more useful to the engineer than "Reduce 315 Hz by 2 dB." This requires the recommendation engine to recognize which named move a deviation falls into and label it accordingly.

### 4.5 Frequency masking — the subtraction principle

Two sources at the same frequency don't add; the louder one masks the quieter. This is the most important concept in mixing because it explains why "turn up the vocals" rarely works — the vocal isn't quiet, it's masked by a guitar in the same range.

**Common masking pairs:**

| Sources | Conflict zone | Resolution |
|---|---|---|
| Kick + bass | 50–150 Hz | Decide which owns sub (kick: 60 Hz, bass: 80–100 Hz) and cut the other in the rival's center |
| Bass + low guitars | 80–250 Hz | HPF guitars at 100 Hz; cut bass at 200 Hz |
| Vocals + guitars | 1–4 kHz | Cut guitars 2–3 dB at 2–3 kHz; vocal sits in the pocket |
| Vocals + cymbals | 6–10 kHz | Cut cymbals slightly at 7 kHz; let vocal sibilance live there |
| Two guitars | 200 Hz–5 kHz | Pan apart; carve complementary EQ — one gets 800 Hz, other gets 1.5 kHz |
| Snare + vocal | 200 Hz, 3 kHz | Cut snare body slightly at 200 Hz; vocal stays full |

**Engine application — multi-channel attribution:** When the reference mic detects a band buildup, the engine should consider all channels with frequency fingerprints overlapping that band, not just the loudest one. The contributor identification should rank by combined heuristic: (1) RMS level in the band, (2) EQ boost in the band, (3) baseline drift in the band. Any channel that's currently boosting in the conflict zone is suspect even if its RMS is moderate.

---

## Part 5 — Dynamics Processing

### 5.1 Compression — what it does and why

A compressor reduces the dynamic range of a signal by attenuating peaks above a threshold. The four primary controls:

- **Threshold:** Level above which compression begins
- **Ratio:** Compression strength (e.g., 4:1 means 4 dB above threshold becomes 1 dB above threshold)
- **Attack:** How fast compression engages once threshold is crossed
- **Release:** How fast compression disengages once signal drops below threshold

Plus **makeup gain** (compensates for compression-induced loss of average level) and **knee** (hard knee = abrupt, soft knee = gradual onset of compression).

### 5.2 Live compression vs. studio compression

In studio, compression is often used as a tone-shaping tool — slow attacks that emphasize transients, parallel compression for character, multi-stage chains. In live, compression has three jobs:

1. **Catch peaks** so the signal doesn't clip the PA chain
2. **Even out level** so quiet performers don't get lost and loud ones don't bury everything
3. **Increase apparent loudness** without increasing peak level

That's it. Live compression is utility, not character. **Heavy compression on the FOH mix bus is inadvisable** — it pumps with whatever is loudest (usually kick), modulating the entire mix. Compress at the channel and bus level, not the main bus, except for very gentle bus-glue compression (1.5:1 to 2:1, slow attack, fast release, 1–2 dB GR).

### 5.3 Live compression starting points by source

These are starting points, not absolutes. The engine can use these to validate that compressor settings are at least in the reasonable range.

| Source | Threshold | Ratio | Attack | Release | Notes |
|---|---|---|---|---|---|
| **Lead vocal** | -18 to -12 dB (~3–6 dB GR avg) | 3:1 to 4:1 | 5–15 ms | 80–150 ms | Auto release often helpful; aggressive if singer is dynamic |
| **Backing vocal** | similar to lead | 4:1 | 5 ms | 100 ms | Tighter than lead — they sit in the bed |
| **Kick** | -12 to -8 dB | 4:1 to 6:1 | 10–20 ms (let beater through) | 50–80 ms | Hard knee, aggressive |
| **Snare** | -15 to -10 dB | 4:1 to 5:1 | 5–15 ms | 100–200 ms | Slow enough to keep crack |
| **Toms** | -15 to -10 dB | 4:1 | 10 ms | 150 ms | Often gated more than compressed |
| **Bass** | -15 to -10 dB | 4:1 to 6:1 | 20–40 ms (let initial transient through) | 100–200 ms | Heavier comp than studio — keeps it locked |
| **Electric guitar** | only if dynamic | 2:1 to 3:1 | 10–20 ms | 80–120 ms | Distortion is already compressed; only needed for clean parts |
| **Acoustic guitar** | -18 to -12 dB | 3:1 | 10 ms | 100 ms | Smooth out picking dynamics |
| **Keys** | -18 to -12 dB | 2:1 to 3:1 | 10 ms | 100 ms | Light hand — let the player's dynamics live |
| **Bus glue (subgroups)** | gentle | 1.5:1 to 2:1 | slow (30+ ms) | fast | 1–2 dB GR max |

**X32 ratio enum reference:** Values 0–11 mapping to ratios 1.1, 1.3, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0, 20.0, 100.0. The engine sees these as enum indices; `COMP_RATIO_MAP` in `core/osc_client.py` already handles this.

### 5.4 Engine application — dynamics state

Phase 1 reads `comp_threshold`, `comp_ratio`, `gate_threshold`, but doesn't analyze them deeply. Phase 2+ should add:

- **Compressor sanity check:** If `comp_ratio > 7:1` on a non-percussion channel without explanation, flag for review (likely set up as limiter, not compressor)
- **Gate threshold sanity check:** If gate threshold is above the channel's typical RMS, the source will get cut off — flag if RMS rarely exceeds threshold during active periods
- **Gain reduction monitoring:** `/meters/1` returns gate GR and dyn GR per channel. Average GR > 8 dB sustained indicates over-compression; near-zero GR indicates the compressor isn't doing anything (consider lowering threshold or removing the comp)
- **Pumping detection:** GR oscillating rapidly between 0 and -6 dB indicates fast attack/release misconfiguration on a source with sustained content — recommend slower release

### 5.5 Gates and expanders

Gates close below a threshold; expanders attenuate proportionally. On the X32, both live in the gate section with `mode` setting (EXP2/3/4 = expanders, GATE = full gate, DUCK = ducker).

**Live use cases:**

- **Drum gates:** Tom mics gated to silence between hits. Kick can be gated but be careful — fast kick patterns may close before the next hit.
- **Vocal gate:** Open-but-unused vocal mics gated to remove stage bleed. The DJI mic in the FOH Assistant context is doing this conceptually for analysis, but the actual board can also gate the vocal channel.
- **Ducker:** Mode = DUCK. Useful for announcements over music — main mix ducks when announce mic opens.

**Engine application — sparse mic guard:** The existing `usage: sparse` and `inactive_threshold_db` config in band.yaml is essentially a software gate for analysis purposes. The threshold should match (or be slightly above) the actual gate threshold on the channel — otherwise the engine and the board disagree about when a mic is "active."

---

## Part 6 — Loudness and SPL Targets in Live Sound

### 6.1 LUFS for live mix targeting

LUFS was developed for broadcast and streaming, not live sound — but it's the best available perceptual loudness metric, and integrated LUFS over a song-length window is a reasonable target for "is this song hitting the right perceived level."

The engine's genre LUFS targets are set with these realities in mind:

| Genre | Target LUFS | Rationale |
|---|---|---|
| AOR | -20 | Vocal-forward, dynamic — preserves expression |
| Hard Rock | -18 | Punchy, present — moderate compression |
| Glam Metal | -18 | Energy-forward, similar to Hard Rock |
| Heavy Rock | -17 | Locked groove, slightly hotter |
| Heavy Metal | -16 | Tight and aggressive — close to "wall of sound" |
| Post-Grunge | -17 | Modern rock, compressed feel |
| Party Rock | -16 | High energy, dance-floor loudness |

**Note:** These are *integrated LUFS measured at the reference mic position.* They do not directly correspond to streaming-target LUFS (which are measured at the source). A live mix with -18 LUFS at the audience position might measure -14 LUFS if recorded direct from the board — the room and audience absorb energy, and the reference mic is K-weighted.

### 6.2 SPL targets for live shows

Independent of LUFS, the engine should be aware of absolute SPL — both for hearing safety and for venue-appropriateness.

| Venue type | Typical mix SPL (A-weighted, 15-min Leq) | Considerations |
|---|---|---|
| Small bar / club | 95–100 dBA | Tight rooms; over 100 dBA is unpleasantly loud |
| Theater / mid-size venue | 100–105 dBA | Sweet spot for rock — energetic without hostile |
| Large venue / arena | 100–108 dBA | More headroom, clarity at distance more important |
| Festival main stage | 103–110 dBA | Local noise ordinances often cap |
| EDM / dance event | 105–115 dBA | Usually pushes further — dance floor expectation |

**Hearing safety reference (NIOSH):**
- 85 dBA → 8 hour safe exposure
- 88 dBA → 4 hours
- 91 dBA → 2 hours
- 94 dBA → 1 hour
- 97 dBA → 30 minutes
- 100 dBA → 15 minutes
- 103 dBA → 7.5 minutes

A typical 90-minute rock set at 100 dBA averaged exceeds the daily safe exposure. This is why earplugs matter — and why the engine's mix targets should land at or below 105 dBA when possible. Above that, every 3 dB doubles damage rate.

### 6.3 Crest factor and dynamic range

Crest factor is the ratio of peak to RMS — a measure of how "spiky" a signal is. Drums have high crest factor (transient peaks well above sustained level); pads and synths have low crest factor (peaks barely above RMS). 

In live mixing, **a moderate crest factor is desirable.** Too high and the mix feels uneven and the loudest hits are uncomfortable; too low and the mix sounds squashed and lifeless. Genre-appropriate dynamic range is one of the genre profile's targets:

| Dynamic range | LRA (Loudness Range, LU) | Feel |
|---|---|---|
| Very high | > 15 LU | Jazz, classical — full expressive range preserved |
| High | 10–15 LU | Acoustic, folk, ballads |
| Medium-high | 8–12 LU | AOR, melodic rock |
| Medium | 6–9 LU | Hard rock, glam metal |
| Low-medium | 5–8 LU | Heavy metal, modern rock |
| Low | 3–6 LU | Hip-hop, EDM, "wall of sound" |

**Engine application:** The `dynamic_range` field in genre profiles maps to LRA expectations. Phase 2+ should compute LRA from the rolling reference mic buffer (pyloudnorm supports this) and flag deviations — a Glam Metal song running at 14 LU is over-dynamic for the genre; an AOR song at 4 LU is over-compressed.

---

## Part 7 — Per-Instrument Reference Profiles

This section is the master reference for what each instrument actually does, frequency-wise, in a live context. The current frequency fingerprints in `band.yaml` should be cross-checked against this and updated where needed.

### 7.1 Kick drum

**Fundamental range:** 50–80 Hz
**Body:** 80–150 Hz (chest punch)
**Click/beater:** 2–4 kHz (definition, audibility in the mix)
**Mud zone:** 300–500 Hz (often cut)
**Mix character:** Foundation. Owns the sub-bass with bass guitar.

**Live mix moves:**
- HPF off, or set very low (30 Hz) to remove rumble
- +3 to +5 dB at 60–80 Hz for weight
- -3 to -6 dB at 300–500 Hz for tightness (the "shoe box" cut)
- +2 to +4 dB at 2.5–4 kHz for beater click
- Compression: 4:1 to 6:1, fast attack (5–10 ms), medium release

**Fingerprint:** `primary: 50-80Hz, body: 80-150Hz, click: 2-4kHz, mud: 300-500Hz`

### 7.2 Snare drum

**Fundamental:** 150–250 Hz (the "thunk")
**Body:** 250–500 Hz (warmth — also where mud lives)
**Crack:** 3–6 kHz (the snap, what cuts through)
**Air:** 8–12 kHz (snare wires, sheen)

**Live mix moves:**
- HPF at 80–100 Hz
- +2 to +4 dB at 200 Hz for body (or cut if too thick)
- -2 to -4 dB at 400–800 Hz for boxiness
- +2 to +5 dB at 4–6 kHz for crack
- Compression: 4:1 to 5:1, 5–15 ms attack, 100–200 ms release

**Fingerprint:** `primary: 150-250Hz, body: 250-500Hz, crack: 3-6kHz`

### 7.3 Hi-hat

**Primary content:** 8–15 kHz
**Body:** 200–500 Hz (often cut to remove bleed/mud)
**Stick attack:** 2–5 kHz

**Live mix moves:**
- HPF aggressive — 250–400 Hz (hi-hat doesn't need lows)
- -3 to -6 dB at 200–300 Hz to remove kick/snare bleed
- Subtle high-shelf boost at 10 kHz if PA is dull

**Fingerprint:** `primary: 8-15kHz, attack: 2-5kHz`

### 7.4 Toms

**Fundamental (rack):** 100–250 Hz
**Fundamental (floor):** 60–120 Hz
**Attack:** 3–6 kHz

**Live mix moves:**
- HPF at appropriate fundamental
- +2 to +4 dB at fundamental for body
- -2 to -4 dB at 400–800 Hz for boxiness
- +2 to +3 dB at 4 kHz for stick definition
- Gates almost always — toms ring otherwise

**Fingerprint:** `rack_tom_primary: 100-250Hz, floor_tom_primary: 60-120Hz, attack: 3-6kHz`

### 7.5 Overheads

**Primary content:** 5 kHz+ (cymbals)
**Drum kit body bleed:** 100–500 Hz (often cut)

**Live mix moves:**
- HPF aggressive — 200–400 Hz
- Used for cymbal capture and "glue" feel of the kit
- Subtle high-shelf at 10 kHz for sparkle

**Fingerprint:** `primary: 5-15kHz, kit_bleed: 100-500Hz`

### 7.6 Bass guitar (DI)

**Fundamental:** 40–80 Hz (low E = 41 Hz)
**Body / fundamentals:** 80–250 Hz
**Definition / "growl":** 700 Hz–1 kHz
**String / pick attack:** 2–4 kHz
**Top end (slap, rare):** 4–6 kHz

**Live mix moves:**
- HPF at 30–40 Hz (below low E) to remove rumble
- +2 to +3 dB at 80–100 Hz for weight (or cut if room is boomy)
- -2 to -4 dB at 200–300 Hz for boom/mud
- +1 to +3 dB at 800 Hz for definition in dense mixes
- Compression: 4:1 to 6:1, medium attack (20–40 ms), longer release (100–200 ms)
- Sidechain to kick if low-end is congested (advanced — Phase 4+)

**Fingerprint:** `primary: 40-250Hz, definition: 700-1kHz, attack: 2-4kHz`

### 7.7 Electric guitar (mic'd or DI'd)

**Fundamental low E:** 82 Hz (rarely the lowest useful content in mix)
**Body / power:** 200 Hz–1 kHz
**Bite / presence:** 2–5 kHz
**Harshness zone:** 3–6 kHz (often cut)
**Air (above the amp):** 6 kHz+

**Live mix moves:**
- HPF at 80–120 Hz (or higher if it's clashing with bass)
- -2 to -4 dB at 200–400 Hz for clarity (especially if multiple guitars)
- -2 to -3 dB at 3–5 kHz if harsh
- Boost at 800 Hz to 2 kHz for cutting through the mix
- Distorted guitars rarely need additional compression

**Fingerprint:** `primary: 200Hz-5kHz, body: 200-1kHz, bite: 2-5kHz`

### 7.8 Acoustic guitar

**Fundamental:** 80–200 Hz
**Body:** 150–400 Hz (where "wood" lives — also where boom lives)
**Sparkle:** 2–6 kHz
**Air:** 8 kHz+

**Live mix moves:**
- HPF at 80 Hz (sometimes higher if competing with bass)
- -2 to -4 dB at 200–300 Hz for tubbiness
- +1 to +2 dB at 3–5 kHz for sparkle
- High-shelf boost at 10 kHz for air
- Light compression — 3:1, 10 ms attack

**Fingerprint:** `primary: 80Hz-6kHz, body: 150-400Hz, sparkle: 2-6kHz`

### 7.9 Keys / piano

**Fundamental low:** 28 Hz (A0) — rarely useful in live mix
**Bass register:** 60–250 Hz
**Body / mid:** 250 Hz–1 kHz
**Brilliance:** 2–5 kHz
**Air:** 8 kHz+

**Live mix moves:**
- HPF at 80–120 Hz (depending on register being played)
- -2 to -4 dB at 250–500 Hz if muddy
- +1 to +2 dB at 3 kHz for presence
- Light compression — 2:1 to 3:1

**Fingerprint:** `primary: 60Hz-8kHz`

### 7.10 Lead vocal

**Fundamental:** 80–250 Hz (males) / 150–400 Hz (females)
**Body:** 200–500 Hz (warmth — and the "head cold" zone if too much)
**Intelligibility / presence:** 1–4 kHz
**Sibilance:** 5–9 kHz (varies by singer)
**Air:** 10 kHz+

**Live mix moves:**
- HPF at 80–120 Hz (males) / 100–150 Hz (females)
- -2 to -3 dB at 200–300 Hz if "stuffy"
- +1 to +3 dB at 2–4 kHz for clarity
- De-essing at 5–9 kHz (varies — find the offender, narrow Q cut)
- High-shelf +1 to +3 dB at 10 kHz for air
- Compression: 3:1 to 4:1, 5–15 ms attack, 80–150 ms release

**Fingerprint:** `primary: 100Hz-4kHz, intelligibility: 1-4kHz, sibilance: 5-9kHz`

### 7.11 Backing / harmony vocal

Same fingerprint as lead vocal, but tighter compression and often slightly less air. The engine should treat backing vocals as priority `medium` while lead is priority `very_high`.

---

## Part 8 — Mix Building Principle: Bottom-Up vs. Top-Down

Two valid approaches. Genre dictates which is appropriate.

### 8.1 Bottom-up (rock and roll priority list)

Build foundation first, add melody and lead last. Ordering:

1. **Kick** — establish lowest part
2. **Bass** — lock with kick
3. **Snare** — drive the rhythm
4. **Other drums** — complete the kit
5. **Rhythm guitar(s)** — harmonic foundation
6. **Keys / additional rhythm instruments**
7. **Lead instruments**
8. **Backing vocals**
9. **Lead vocal** — sits on top of finished bed

**Best for:** Hard Rock, Heavy Rock, Heavy Metal, Glam Metal, Party Rock

### 8.2 Top-down (vocal-priority)

Build the vocal first, then everything supports it. Ordering:

1. **Lead vocal** — set show level
2. **Drums** (kick, then snare, then kit)
3. **Bass**
4. **Rhythm instruments**
5. **Lead instruments**
6. **Backing vocals**

**Best for:** AOR, ballads, vocal-forward styles

### 8.3 Engine implications

The `instrument_weights` field in genre profiles is essentially a priority ordering. When the engine finds multiple competing problems, it should resolve in priority order — fix the highest-priority instrument's problem first, then re-evaluate. This prevents recommendation thrash where a low-priority fix masks the real high-priority issue.

---

## Part 9 — Feedback Theory and Suppression

### 9.1 What feedback actually is

Feedback is a closed-loop oscillation. Sound from a speaker reaches a microphone, gets re-amplified, returns to the speaker louder, and the loop closes. The first frequency to feed back is the one with the most loop gain — the path's natural resonance frequency.

**The math:** Feedback occurs when loop gain exceeds 1 (0 dB) at any frequency. Every dB of EQ boost at a feedback-prone frequency reduces gain-before-feedback by 1 dB. Every dB of EQ cut increases it by 1 dB.

### 9.2 The feedback path

For a typical FOH situation:
1. Vocal mic on stage
2. Sound to monitors and PA
3. PA returns to stage (front-fill spillage, bouncing off back wall)
4. Monitor sound at the mic capsule
5. Back into the mic
6. Closed loop

**Reduction strategies in priority order:**
1. **Mic technique** — singer's hand position, mic distance from speaker
2. **Speaker placement** — main PA in front of mic line, monitors angled away from null axis
3. **High-pass filter on vocal channel** — removes feedback-prone low-mid energy
4. **Notch filters** — narrow Q cuts at known problem frequencies (the "ringing out" process)
5. **Reduced EQ boost on vocal** — every boosted dB is a dB of feedback risk

### 9.3 Common feedback frequency ranges

These ring first because they're where most rooms and monitor systems have peaks:

- **125 Hz, 250 Hz** — small-room boom modes
- **400–500 Hz** — boxiness frequencies, "ringing" in many rooms
- **800 Hz–1.5 kHz** — common monitor wedge resonance
- **2–4 kHz** — vocal mic + wedge sweet spot for feedback
- **4–8 kHz** — high-end "screech" feedback (most painful, least common)

### 9.4 Engine application — feedback risk and detection

Phase 1 includes a feedback spike event (rapid high-frequency RMS spike), but a more nuanced approach is possible:

- **Pre-feedback detection:** If reference mic shows narrow-band buildup over a few seconds in a feedback-prone range while the channel meter for the open mic isn't rising correspondingly, it's likely room/monitor ringing rather than performer pushing.
- **Post-feedback notch suggestion:** When feedback fires, the engine can identify the exact frequency from the FFT spike and recommend a narrow-Q cut on the contributing channel's EQ.
- **Risk score per channel:** Channels with vocal mics, EQ boosts in feedback-prone bands, and high gain are high feedback risk. The engine could maintain a risk score and flag when boosting or pushing further would significantly increase it.

---

## Part 10 — Genre Profile Refinement

The current genre profiles in the project are good starting points. This section provides the audio-engineering reasoning to refine them.

### 10.1 What a genre profile should encode

1. **Frequency target curve** — the relative balance of energy across bands that defines the genre's character at the audience position
2. **LUFS target** — appropriate perceived loudness for the energy level
3. **Dynamic range expectation** — how compressed the mix should feel
4. **Instrument priority ordering** — who owns the mix
5. **Per-instrument fingerprint emphasis** — which fingerprint frequencies matter most for this genre
6. **Common deviations to flag vs. allow** — genre-typical "wrong-but-on-purpose" moves

### 10.2 Profile refinement notes

**AOR (Journey, Foreigner, Toto):**
- Vocal is everything — `Lead Vocal: very_high`, all other instruments support
- Smooth keys mid (300 Hz–3 kHz) — keys are foundational, not background
- Mid +2 dB target reflects the keys+vocal frontness
- Air +1 dB for that polished studio feel
- Dynamic range medium-high — singer needs room to express
- LUFS -20 — quieter average than other rock styles, more dynamic
- Watch for: low-mid mud from heavy keys/guitar overlap (300–500 Hz)

**Hard Rock (Joan Jett, early Mötley Crüe, Heart, Cheap Trick):**
- Guitar dominant — `Guitar 1: very_high`
- Raw, less polish than AOR — slightly less air, more mid
- Bass +2 dB target — punchy, locked with kick
- Less keys content — keys often `low` priority
- Dynamic range medium
- LUFS -18 — hotter than AOR, more energy
- Watch for: guitar/vocal masking around 2–3 kHz

**Glam Metal (Ratt, Poison, Mötley Crüe later, Warrant):**
- Guitar-aggressive — both Guitar 1 and Guitar 2 `very_high`
- Big vocals with reverb — maintain vocal priority `high` despite guitar dominance
- Higher mid emphasis (+2 dB) — squealing solos, harmonics
- LUFS -18 — energetic but not crushed
- Watch for: dual-guitar masking 200 Hz–5 kHz; vocal getting buried 2–4 kHz

**Heavy Rock (AC/DC, ZZ Top, Status Quo):**
- Massive rhythm guitar foundation — both guitars `very_high`
- Locked groove — drums and bass emphasis
- Less polish — air -1 (no studio sparkle)
- LUFS -17 — hotter, driving
- Keys typically absent — weight `none`
- Watch for: low-mid buildup from the wall of guitars (250–400 Hz)

**Heavy Metal (Judas Priest, Pantera, Iron Maiden):**
- Tight, precise — high-mid +2 for aggressive bite
- Kick `medium` priority — needs to articulate fast patterns
- Less low-mid (+0 or -1) — keep tightness, no mud
- LUFS -16 — close to maximum reasonable live SPL
- Dynamic range low-medium — wall of sound feel
- Watch for: kick getting lost behind guitars; cymbals masking vocal

**Post-Grunge (3 Doors Down, Nickelback, Creed, Theory of a Deadman):**
- Modern rock sheen — slight air boost
- Vocal-forward but with full guitars
- Compression-heavy genre — dynamic range low-medium
- LUFS -17
- Watch for: over-compressed feel (LRA too narrow)

**Party Rock (Beastie Boys, KISS, Twisted Sister):**
- Fun, loud, no precision required
- Drum-and-vocal-centric
- High energy — LUFS -16
- Watch for: PA running out of headroom; everything just gets louder

### 10.3 Refining targets from data

Once the engine has 5–10 shows of recorded reference-mic audio + manual-adjustment data, the refinement process is:

1. For each genre profile, compute the *actual* integrated LUFS and per-band averages from songs the engineer didn't flag as problematic
2. Compare to current profile targets
3. If consistent deviation > 1 dB across multiple songs and the engineer didn't fix it, the target should move toward the actual
4. If consistent deviation > 1 dB and the engineer *did* fix it, the target is correct and the engine should continue flagging

This is essentially supervised learning with the engineer's manual adjustments as labels.

---

## Part 11 — Reference Audio and the Path to Phase 6

Phase 6 introduces per-song reference audio targeting — replacing genre templates with the actual studio recording's frequency curve as the target. This section anticipates the audio-engineering challenges.

### 11.1 What can and cannot transfer from studio to live

**Transfers well:**
- Relative frequency balance (which bands are emphasized)
- Approximate spectral tilt (warm vs. bright)
- Dynamic range character (compressed vs. open)
- Instrument balance hierarchy (vocal forward vs. guitar forward)

**Does not transfer:**
- Absolute LUFS (studio masters at -8 LUFS aren't achievable or appropriate live)
- Stereo width (live PAs are mostly mono in audience perception)
- Reverb tails (the room provides its own)
- Sub-bass extension (most live PAs roll off below 40 Hz; many studio masters have content to 25 Hz)

### 11.2 Reference curve extraction

When a reference song is analyzed:

1. **Section detection** — identify verse, chorus, bridge, full-band sections
2. **Weighting** — full-band sections (loudest with all instruments) weighted as primary target; intros and breakdowns less so
3. **Per-band integrated levels** — averaged across full-band sections, K-weighted
4. **LUFS integrated** — overall loudness for context only (not directly used as target)
5. **Spectral tilt fitting** — characterize as warm/neutral/bright relative to genre baseline
6. **Save curve** — JSON file referenced by the song in setlist

### 11.3 Cover band delta tracking

Once reference curves are in use, the engine can track the consistent difference between the band's live sound and the studio reference:

- Band consistently runs Guitar 1 +2 dB hotter at 3 kHz than Warren DeMartini's recorded tone
- Band's Bass DI sits 1 dB lighter in sub than the record

These deltas are useful pre-show context: "Last 5 shows, this band's Lead Vocal sat 1.5 dB louder relative to instruments than the reference. If they're hitting target tonight, the vocal will come up." This is information the engineer benefits from knowing without having to act on it.

---

## Part 12 — Algorithm and Threshold Tuning Reference

This section concentrates the engineering-derived thresholds and algorithm rules from this document into a single reference.

### 12.1 Recommendation triggers

| Parameter | Default | Rationale |
|---|---|---|
| `recommendation_trigger_db` | 3.0 | Below this, deviations are below most engineers' decision threshold |
| `lufs_trigger_db` | 2.0 | Slightly tighter on overall loudness — perceived loudness changes are felt sooner |
| `baseline_drift_trigger_db` | 2.0 | Drift accumulates; flag earlier than absolute deviations |
| `rate_of_change_suppress_db` | 3.0 | Larger than this in 5s is intentional engineer action |
| `rate_of_change_window_s` | 5 | Engineers complete a deliberate move in under 5s |
| `suppression_duration_s` | 60 | Channel-cool-off after intentional move |
| `recommendation_cooldown_s` | 60 | Per-channel cooldown prevents spam |
| `silence_threshold_db` | -50.0 | RMS floor below which all recommendations suppress |
| `channel_active_threshold_db` | -50.0 | Channel must exceed this to be considered for attribution |

### 12.2 EQ recommendation logic (refined)

When deciding between cut on culprit vs. boost on victim:

1. **Default to cut on the contributing channel.** Live-sound principle.
2. **Cut size:** typically 2–4 dB. Larger cuts (6+ dB) are uncomfortable to recommend without high confidence.
3. **Boost size:** typically 1–3 dB. Anything more risks feedback or distortion.
4. **Q selection:** narrow (Q=2.0+) for cuts, wide (Q=1.0) for boosts.
5. **Frequency placement:** match the band's center frequency to the closest existing EQ band within 2 octaves; otherwise suggest adding a new band.

### 12.3 Compression recommendation logic (Phase 2+)

| Condition | Recommendation |
|---|---|
| Average gain reduction > 8 dB sustained | Reduce compression — over-compressed |
| Average gain reduction < 0.5 dB | Increase compression — comp not engaged |
| GR oscillates rapidly between 0 and -6 dB | Slow release — pumping detected |
| Ratio > 7:1 on non-percussive source | Review — likely set up as limiter |
| Threshold above typical channel RMS | Lower threshold or remove comp |

### 12.4 Gate recommendation logic (Phase 2+)

| Condition | Recommendation |
|---|---|
| Gate threshold above active RMS | Lower threshold — source getting cut off |
| Gate threshold below noise floor + 6 dB | Raise threshold — gate not doing anything |
| Drum tom mic with no gate | Add gate at -30 to -40 dB threshold |

### 12.5 Loudness band weighting (psychoacoustic correction)

For more accurate "perceived deviation" scoring, weight each band's deviation by the ear's sensitivity:

| Band | Weight | Rationale |
|---|---|---|
| sub_bass | 0.6 | Less sensitive at typical mix volumes |
| bass | 0.8 | Approaching sensitive range |
| low_mid | 1.0 | Reference |
| mid | 1.1 | Sensitive |
| high_mid | 1.3 | Most sensitive — also harshness range |
| presence | 1.2 | Sensitive |
| air | 0.9 | Subtler perceptual impact |

Apply as: `weighted_deviation = raw_deviation * band_weight`. A 4 dB deviation in high_mid (weighted 5.2) is a more urgent recommendation than a 4 dB deviation in sub_bass (weighted 2.4).

---

## Part 13 — Open Questions and Areas for Refinement

Audio engineering is partly settled science, partly stylistic preference, partly venue-and-band specific. The following areas remain open and should be revisited as data accumulates.

1. **Band-specific frequency target deltas.** No two bands sound the same even within a genre. Persistent deviations from genre target that the engineer accepts at every show are the band's signature, not problems. Phase 5+ should learn these deltas per band and adjust targets accordingly.

2. **Venue-specific spectral coloration.** Same band, different rooms, different reference mic readings. Phase 3+ venue profiles should capture room contribution separately from band contribution.

3. **Time-of-show drift.** Audience fills the room and absorbs high frequencies. Stage volume creeps up over the show. The mix that was right at song 1 isn't right at song 12. The engine should track and recommend gradual compensation (Phase 4+ as part of automated drift correction).

4. **Songs with intentionally non-genre character.** A power ballad in a Heavy Metal set deliberately deviates from the genre target. Setlist annotations should support per-song target overrides without requiring a full custom genre profile (Phase 2 enhancement).

5. **Reference mic position calibration.** A measurement-grade mic with calibration file (UMIK-2, IMP-015) gives band-readings true acoustic accuracy rather than relative-only. Until then, all band targets are relative to the DJI Mic 2's response curve, which has its own midrange-emphasis bias.

6. **Multi-channel attribution confidence.** When several channels could plausibly be the culprit, the engine currently picks one. A confidence score and "or possibly X channel" secondary attribution would help engineers when the primary is wrong.

---

## Part 14 — How This Document Feeds the Engine

The connections between this guide and the implementation are:

| Guide section | Engine consumer |
|---|---|
| 1.2 Frequency bands | `analyzer.py` band binning, `band.yaml` fingerprints |
| 1.3 Equal-loudness | Future psychoacoustic weighting in `recommender.py` |
| 1.4 Gain stages | Phase 2+ preamp state in `ChannelState` |
| 4.1 EQ stages | Phase 1 channel EQ focus is correct; bus EQ deferred |
| 4.4 Named moves | Recommendation output formatting — name the move |
| 4.5 Masking | Multi-channel attribution in `_find_culprit` |
| 5.3 Compression starting points | Phase 2+ compressor sanity checks |
| 6.1 LUFS targets | `genres/*.yaml` `target_lufs` fields |
| 6.2 SPL targets | Future hearing-safety advisory |
| 7.x Per-instrument | `band.yaml` `frequency_fingerprints` |
| 8.x Mix building | `instrument_weights` priority ordering |
| 9.x Feedback | Future feedback-risk scoring; existing feedback-spike detection |
| 10.x Genre refinement | `genres/*.yaml` content; learning loop from manual adjustments |
| 11.x Reference audio | Phase 6 `curves/*.json` extraction logic |
| 12.x Algorithm tuning | `band.yaml` `thresholds` block |

When extending the engine, this guide is the first place to look for whether a proposed change is grounded in audio-engineering principle or is an idiosyncratic tweak. Idiosyncratic tweaks are sometimes correct (every band is unique) but should be conscious choices, not accidents.

---

*End of System Audio Guide v1.0*
