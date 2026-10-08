# Salamander OPM conversion (Starfield / Destroy Them All)

How the Salamander pair `05 Starfield (Stage 4 BGM).vgz` / `07 Destroy Them All (Stage 6 BGM).vgz`
is converted, what the reference cover `Salamander_Starfield.fur` is good for, and the timing
model that replaced the old fixed 60 Hz grid. Numbers below come from the two rips in the repo
root; the same code path runs for every pack (`python -m vgm2151fur ...`) — Surprise Attack, Willow,
Block Hole, Gradius II/III and Super Contra convert into `output/<game>/fur/`, and the sweep,
vibrato and verification notes below were measured on those as well.  See "Known gaps and edge
cases" before assuming anything is faithful.

## Board and rips

- MAME `konami/nemesis.cpp` (GX587 "salamand" board): **one** YM2151 (3.579545 MHz) + K007232
  PCM + VLM5030 speech. Video is 256×224 at 18.432 MHz / 3 / (384×264) = **60.606 Hz**.
- The two rips here are VGM 1.51 by *Dario011*, FM only — **no K007232 writes and no ROM
  blocks**, so the K007232 path stays idle for them. `Salamander_Starfield.fur` is FM-only too.
- The pack file layout is intro + loop passes: `loop_sample` = 318593 (Starfield, 7.22 s) and
  819647 (Destroy Them All, 18.59 s). The converter keeps the whole file and drops `Bxx` on the
  order that starts at the loop row.

### How close is a converted track, and what the ceiling is

This is the honest summary; the details (and how each number was measured) are below.
Measured with `fur2vgm.py` + `vgmdiff.py` (register space, absolute timeline) and renders;
the per-channel distributions are in [Verifying against Furnace](#verifying-against-furnace).

**Faithful (register-exact, measured):**

- **Note pitch and fine tune.** Every note's `(note, E5)` converts back to the source's
  KC/KF. Audited packs: median **0** units of 1/64 semitone on all channels, p95 ≤ 12
  units (~0.19 st) on vibrato-heavy channels, means under 0.16 st. A 34-track Fantasy Zone
  II DX audit and Battle Garegga's 18 tracks agree; rendered audio on the tested channels
  is 0.0 cents median.
- **Key-on times.** Identical frames: worst-case placement 1.02 ms, zero events beyond
  2 ms, no dropped notes (the converter drops only key-ons the chip itself ignores).
- **Patch bytes.** Operator registers (DT/MUL, TL, KS/AR, AM/DR, DT2/D2R, SL/RR), algorithm,
  feedback, AMS/PMS are copied from the source's write stream; the .fur stores them in chip
  order. The OPM clock is carried in the `FLAG` block (`customClock`) and the notes are
  transposed by the clock ratio, so the exported registers equal the source's.
- **Timing model.** The measured key-on lattice (one row = 1/8 of the detected 16th, EDxx
  for the residue), not a guessed 60 Hz grid. A track whose *fastest* note value is still
  longer than a 16th note (Sega System 16C shop/ending themes run 9.5k–19k samples per
  melodic event) has that unit folded back into the 16th-note band instead of falling back
  to the 60 Hz heuristic: Mysterious Shop, Hereafter, Flow My Tears and Fantastic Refrain
  went from 15.0/15.0/15.0/7.5 to 73.0/75.4/77.3/76.8 rows/s this way. Rows are what carry
  pitch effects, and those drivers rewrite KC/KF almost every frame — at 15 rows/s most of
  that motion had nowhere to go.

**Approximate, and why (the ceiling):**

| # | What | Why it cannot be exact in a `.fur` | Measured cost |
|---|------|------------------------------------|---------------|
| 1 | **Vibrato / scripted LFO** | Furnace plays *one macro trajectory per instrument*; the driver re-writes `0x38+ch` per note, per section, and sometimes stops mid-note. A `.fur` instrument cannot carry "this note vibratos like this, that one like that". | Instantaneous deviation on vibrato channels is ±10–40 units (~0.15–0.6 st) around a correct center (median 0); the *shape* differs, the pitch does not drift. |
| 2 | **Per-operator gates** | There is no per-operator key-on/off in Furnace's OPM instrument: an operator released or re-keyed mid-note stays sounding. | Notes that start with operators un-keyed get a TL-127 patch variant (34 notes across the packs); mid-note operator changes are flattened. |
| 3 | **Sweeps / pitch ramps** | A ramp rate can only change on row boundaries, and slides are one rate per row (per tick). | Steep percussive dives still land up to ~1.5 st off for the first ~2 ticks (attack blip), then track to single digits. Sparse staircases keep a ≤ 1-KC-step ripple. The tail pin used to subtract the last row's travel twice, which left held notes up to 1.3 st flat until the next key-on (Fantasy Zone II DX risers); fixed in 0.9.15, and the pin can still only express ±1 st through E5. |
| 4 | **Sub-unit pitch** | KC is whole semitones, KF's low 2 bits (~0.8 cents) are not representable, E5 is 1/64 st. | ≤ 0.8 cents per note; inaudible. |
| 5 | **Slide calibration** | `01xx`/`02xx` are 1 `kc_kf_units`/tick/unit only for Furnace 0.6.8.3 (`brokenPitch` from version 157). Another build can scale them differently. | Re-measure with `fur2vgm.py` + `vgmdiff.py` if the Furnace version changes. |
| 6 | **Verification reach** | `vgmdiff.py` sees KC/KF and key-ons only. LFO (`0x38–0x3F`), TL and operator state need a raw write diff or ears; and Furnace's **VGM export does not carry K007232 at all**, so a `.fur`'s PCM cannot be checked that way — render WAVs instead. | Not a conversion error, a blind spot of the audit. |
| 7 | **Driver aliasing** | A driver that re-strikes a held note, or changes timbre mid-note without re-keying, has no representation; the converter normalizes what the chip plays (aliased key nibbles become the same note). | Rare; the note set stays musically identical. |

Everything *not* in that table is expected to be exact. If something sounds off beyond these
classes, it is either a stale `.fur` (see "Two traps in producing the export" under
Verifying against Furnace) or a bug worth reproducing.

## Chip clock (a rip's clock must reach the .fur)

A `.fur` does not store a chip clock in its INFO block; the clock comes from the chip's *flags*,
and Furnace's YM2151 default is the Konami **3.579545 MHz**.  The converter writes
`customClock=<Hz>` in the YM2151's `FLAG` block when the rip says otherwise (`_write_flag` in
`furwrite.py`, `CHECK_CUSTOM_CLOCK` in Furnace's `dispatch.h`).  Sega System 16C (Fantasy Zone II
DX) runs the OPM at **4 MHz**, so its files carry `customClock=4000000`.

The clock flag alone does **not** fix playback pitch: Furnace keeps a note at its musical frequency
at *any* clock (`baseFreqOff` in `arcade.cpp` compensates the note→KC/KF mapping), so the sounding
pitch is set by the *notes*, not by the clock.  A driver's KC/KF registers at 4 MHz sound
12·log2(4/3.579545) = **+1.92 semitones** above what the same registers mean at 3.579545 MHz, so
`analyze.py` shifts every note by that much (`opm_pitch_offset`, 1/64-semitone units, applied to
each note/E5 pair; sweeps are deltas from the note, so they follow for free).  With both halves in
place the transposition and Furnace's compensation cancel *exactly* in register space (Furnace's
offset is round(1536·log2(3579545/4000000)) = −246 of its 1/128-semitone units = −123 of ours), so
the export contains the rip's own KC/KF at the rip's own clock: the register-space audit stays
clean **and** the audio matches.

Verified by rendering both sides and measuring: Fantasy Zone II DX `03 Start ~ Cholacoray` channel
0 was 72.52 Hz before (identical to the source with its clock patched to 3.579545) and 80.46 Hz
after — bit-identical to the source render; windowed medians on other tracks/channels land within
±2 cents (E5→KF quantization is 1/64 semitone).

The flag is only written when the clock differs from 3.579545 and the transposition is zero for
those rips, so the Konami packs convert unchanged.  `customClock` also works for the K007232 (same
macro), written when its clock is not the default.

Another clock-shaped trap: the file *version* selects slide behavior.  These files say **157**,
and any version below 176 makes Furnace set the OPM `brokenPitch` compat flag, which **doubles**
`01xx`/`02xx` porta speed and pitch-macro output in `arcade.cpp`.  The measured "one rate unit =
one `kc_kf_units` step per tick" calibration below holds for `brokenPitch=true`; bumping
`FUR_VERSION` past 176 would halve every slide and need a re-measure.

## The cover as a Rosetta stone

`Salamander_Starfield.fur` is an old-format file (version 136, `INFO` + `PATR` blocks, 8 FM
channels, 5 orders of 64 rows, speed 7 @ 60 Hz, 23 hand-named instruments: Kick/Snare/HiHat/
Toms/Strings/Trumpet/Brass/Bass). It is an *arrangement*, not a transcription, so use it for:

- **Pitch anchoring.** Its arpeggio channel spells C5-E5-G5-C6 / C5-Eb5-Ab5-C6 / Bb4-D5-F5-Bb5 /
  B4-D5-G5-B5 (C minor, i–VI–VII–V). The original's channel 0 key-ons decode to the same notes
  (KC `4E/54/58/5E` = C5/E5/G5/C6), which matches an isolated-channel render of the VGM
  (`vgm2wav-mute --only YM2151#0.Ch0` + Goertzel: 523.25/659.25/783.99/1046.5 Hz; B/D#/F#
  candidates score ~0). The KC table in `analyze.py` is therefore correct as written.
- **Instrument/arrangement reference.** Its patch usage and drum triggers are a useful target for
  a hand-finished module.

Do **not** copy its timing: it runs ~3% slower than the rip and its melody channels sit two
octaves below the original (an arrangement choice). The original's own lattice is the reference
for the converter.

## Timing model (the fix)

The drivers schedule notes on a unit that is neither 735 nor 882 samples, and the effective unit
differs per channel and per section by ~2% (a Konami scheduler characteristic, not log noise):
per-channel 16th-note medians measured over the whole rip:

| track | ch0 | ch1/ch3 | ch2 | ch4 | ch5 | weighted fit |
|-------|-----|---------|-----|-----|-----|--------------|
| Starfield | 4946 | 5027 | 5010 | 5052 | 5006 | 5025.3 → 131.6 BPM |
| Destroy Them All | 5740 | 5780 | 5771 | 5769 | 5794 | 5760.6 → 114.8 BPM |

`vgm2151fur/timing.py` fits this lattice from the key-on times.  Several candidate units are tried
(per-channel median and bucketed mode plus the global mode), each is scored by how many intervals
it explains as integer multiples, and among near-best candidates the coarsest unit wins — finer
note values still land exactly on the row grid.  The drivers also quantize every register write to
the 60 Hz video frame, so individual key-ons sit up to half a frame (≈8 ms) off the ideal lattice;
the fits use the lattice *average* and the per-note deviation is absorbed by `EDxx`.

Rows subdivide the measured 16th into 8, and each row is given a handful of **ticks**
(Furnace: `hz` = ticks/second, `speed` = ticks/row) chosen so one tick is ~2 ms:

| track | row (samples) | speed | hz (ticks/s) | rows/s |
|-------|---------------|-------|--------------|--------|
| Starfield | 628 | 7 | 491.4 | 70.21 |
| Destroy Them All | 720 | 8 | 489.9 | 61.24 |

`Song.place()` floors to the row and emits `EDxx` for the tick offset (verified against Furnace
`playback.cpp processRow`/`nextTick`/master: `ED xx` delays the channel row by `xx` ticks and the
delay must be `< speed` under the strict delay policy).  Result, measured against the original
key-ons: **worst-case placement error 1.02 ms on both tracks, zero events beyond 2 ms**, no
accumulation and no dropped notes (old grid: ±50–150 ms with 7–36% of events on busy channels
misplaced beyond 150 ms).  Notes from *different* channels that the driver wrote at the same
frame now land on the same tick, so doubled lines stay unison instead of splitting a row apart.

Tracks whose unit changes between sections (several Gradius II/III BGMs) have no single global
lattice; the estimator then keeps the smallest real candidate so the fastest section still fits
without collisions, marks the grid `low confidence, sections change unit`, and every note is
still placed at its true write time via `EDxx`.

## Loop point

The `.fur` ends with `Bxx` on the **last row that carries content** in the final order, not on
row 63 of the padded order.  The final order is only as long as the music (e.g. 20 rows for
Destroy Them All, 61 for Starfield), so jumping at row 63 inserted up to 0.7 s of silence before
every loop restart.  The jump row is derived from the last event's row; the resulting loop length
matches the VGM header's loop length within 1–2 rows (Starfield 2050 vs 2048.7, Destroy Them All
2017 vs 2015.6, Attack the Enemy 3116.6 vs 3115.7).
(`tests/test_vgm2151fur_timing.py::TestLoopJump`)

## Lead-pair pitch and timbre (channels 2/4)

The doubled lead (VGM ch1/ch3) is register-faithful, verified end to end:

- every note's `(note, e5)` converts back to the source's KC/KF pitch (`TestLeadPitchRoundTrip`;
  the source uses aliased key nibbles like 0x2B/0x2C and 0x27/0x28, which the chip plays as the
  same semitone);
- the operator bytes in the file are byte-identical to the chip's patch registers (DT/DT2, the
  KSR living in the FM feature's `rs` field, AM enable included);
- ch3 is detuned **+12.5 cents** by the driver from the loop section on (one KF write of 0x20,
  written while the channel was silent) — the file carries exactly that via `E5 88`;
- a "fur → VGM" replay of the file's ch1/ch3 content through vgm2wav-mute renders the same
  partials as the original to **0.0 cents**, with the same attack-envelope shape.  The hand-made
  cover is an arrangement here: its "Strings 2" patch has different TLs and its `E5 7C`/`84`
  detune is ±3.1 cents — its own chorus choice, not the chip's +12.5.
- FM pan uses Furnace's split-4-bit `08xx` (`00`/`F0` = left, `0F` = right, `FF` = both),
  matching the OPM's RL bits (`playback.cpp` case 0x08 panning (split 4-bit)).

## Vibrato onset (FMS/AMS macros)

Several drivers (Surprise Attack, Willow, …) script the LFO sensitivity:
once a note is held, the driver writes `0x38+ch` again every ~0.1 s, stepping PMS
0 → 7 and AMS 0 → 3, so the note starts steady and goes "drunk" over ~1 s (and
resets to 0/0 at the next key-on).  `analyze._pick_lfo_macros` picks the most
common per-instrument trajectory and the writer emits it as FMS/AMS macros
(codes 10/11) with that step duration in **ticks** — `speed = median_step /
(44100 / hz)`.  Using the old fixed 735-samples-per-tick frame made every step
~9× too short, i.e. full vibrato the instant the note started
(`TestVibratoRamp`; Attack the Enemy steps now land at 5379 samples vs 5355 in
the source).  One trajectory per instrument is a known approximation — notes
that deviate from it still get the common shape; see the gaps section below.

## Percussion pitch sweeps (channel 5 → Furnace channel 6)

The percussion channel does not hold a constant pitch: right after each key-on the driver writes
a KC/KF ramp that glides the note down.  Rate and depth are per driver: Salamander steps one KC
value every ~88 samples for ~4100 samples (≈93 ms, 0.7–8.6 semitones deep), Surprise Attack every
~190 samples for up to ~5800 samples (≈132 ms, up to 29 semitones), and Block Hole goes as long as
~0.38 s and 39 semitones.  `analyze.py` captures those writes per held note (merging the
driver's KC/KF write pairs a few samples apart), takes the note's pitch from the first pair when
it lands right after the key-on (≤ 120 samples), and `furwrite._emit_sweep` encodes the source's
*step trajectory* (each merged KC/KF pair is a step) as **per-row 01xx/02xx ramps + E5 steps**:

- each row gets the rate that takes the pitch (after any E5 step on that row) to the trajectory's
  value at the row's *end* — boundary sampling, not a smoothed fit.  The old ±1-row least-squares
  window (`_sweep_slope`, removed) smeared a fast step backwards into the row before it (dragging
  the attack) and averaged a steep climb into a too-slow ramp, leaving whole dives 2–3 semitones
  flat mid-way;
- a step that lands on a row's start is absorbed by an E5 change (one fine-tune unit per 1/64
  semitone, ±1 st range), checked one tick past the boundary so a jump just after the row start
  is still snapped onto that row.  The note's own row keeps the note's pitch: its first-step
  spread over the row is the attack transient under known gaps, and the watch-out there is the
  other way around — the row's ramp starts at the row's start even when the key-on itself is
  delayed (measured), so a fast first-row rate drags the *previous* note's tail for up to the
  note's `ED` delay.  That is a few ticks of the previous, usually already bottomed-out, note;
  a rate deferred to the next row would lose the new note's whole first row instead.
- the row that contains the trajectory's end is rated over the **full row** against the held
  final value: the ramp physically runs a whole row, so rating it over the clipped part made the
  pitch overshoot the bottom of a dive by several times the remaining travel — and an E5 pin
  cannot correct more than ±1 st of that, which left whole dives ~4 semitones flat (Emergency!!);
- the stop lands on the following row.  A stop on a later note's own row is safe (the key-on
  still reads its own pitch; the stop only kills the ramp — measured), and it is the row that
  must carry it when the trajectory runs into that note's row.  Such a crossing trajectory keeps
  its ramp through the last row it owns (the ramp dies at the later note's own k=0 rate or at the
  stop, whichever lands first), but is never given a *rate* on any later row and never an E5
  there: writing both a foreign rate and the note's own k=0 rate on one row means two `01xx`/
  `02xx` codes coexist and the net slide becomes column-order luck — that corrupted a whole new
  note's attack and dive (1408 units flat, Run Through).
- A written rate of `x` moves the pitch by `x` units of `kc_kf_units` per tick.  That unit was
  measured by exporting a converted file back to VGM with Furnace (`fur2vgm.py`) and comparing
  the realized pitch trajectory against the source rip (`vgmdiff.py`); the earlier 1/128-semitone
  assumption and its ×2 scale made every sweep run ~2× fast.

Measured end to end (fur → VGM replay vs the source rip): Burning Fighter ch5/ch6 worst
45/59 → 24/26 units, Run Through the Battlefield ch1/ch2/ch4 216/199/96 → 110/112/130 (and the
full-note scan shows its 1376-unit runaway ramp is gone), Block Hole's sweep channels 70–119,
Make'em Dance ≤ 93, Crush the Harrier ≤ 85.  The remaining error is the ~2-tick attack transient
(and, on the sparsest dives, a ≤ one-step ripple) described under known gaps — the sustained
mid-dive tracking is now single digits.  (These figures and the ones under known gaps were taken
with the earlier per-note-pairing metric; `vgmdiff.py` now reports a median/p95/worst distribution
on the absolute timeline — see "Verifying against Furnace".)

The KF register's field matters here: ymfm computes the OPM pitch as
`block_freq = (KC << 6) | (KF >> 2)` — the fraction is the *top* six bits (64 steps per
semitone, 1.5625 cents), not the low six.  `kf_to_e5` maps it to E5xx 1:1: **one E5 unit is one
KF step** (measured through the bundled 0.6.8.3 console build: `E5 0x80+f` exports `KF = f << 2`
and values above 0xBF saturate the register).  An earlier revision doubled it on the assumption
that E5 was 1/128 semitone; that made every fine tune 2× (up to +63/64 st sharp) and saturated
on a quarter of the range — the "notes slightly off" class of report.

**E5 is a channel state.**  Furnace keeps the last `E5xx` fine tune until it is changed, while
the source driver re-writes KC/KF at every key-on.  The writer therefore re-sends E5 whenever a
note's value differs from the channel's last one — *including back to the neutral 0x80* — and
sweep steps are recorded per row so the replay (`_build_patterns`) emits all E5 changes in row
order.  Writing it only for non-neutral values leaked one note's fine tune into every following
note on that channel (up to ~1.6 semitones sharp — the "notes are all over the place" reports),
and writing sweep steps without that replay let them mask the next note's own fine tune.

## Verifying against Furnace

`fur2vgm.py` runs the bundled Furnace console build (`third_party/furnace/furnace.exe`, win64
console release) headless: `furnace -console -vgmout out.vgm song.fur`.  The exported VGM is the
ground truth for a conversion — `vgmdiff.py source.vgz export.vgm [channels...]` pairs key-ons by
time and then compares the KC/KF history of both files **on the absolute timeline** (same sample
position), reporting per channel the onset shift plus the median / p95 / worst deviation in
`kc_kf_units` (64 per key-code step).  Register-exact conversions report a median of 0 and a p95 of
a few units; anything sustained in the tens is audible.

Two traps in reading those numbers.  Do not compare from *paired* onsets: this converter drops
key-ons the chip ignores (an operator that is already keyed does not restart), so a driver that
re-strikes a held note leaves the export with fewer onsets and any pairing offset lands across the
steep KC/KF jumps those drivers write — that is what reported 4672 units (73 st) on a Fantasy Zone
II DX bass note whose trajectory is in fact identical.  And a track that loops drifts against its
own loop point sample by sample, so the last notes can show a large deviation that the middle never
does; that is why the tool prints a distribution rather than one worst case.  It only reads KC/KF
and key-ons: LFO (`0x38`–`0x3F`) and operator-state differences are invisible to it, so those need
a raw write-stream comparison (or an ear check) instead.

### Two traps in producing the export (both look like converter bugs)

1. **A stale `.fur`.** The converter's output changes when its code changes; judging a fix on a
   file built by an older version produces exactly the "still slightly off" loop.  Every `.fur`
   now carries the converter version in its module comment — `Transcribed from VGM by vgm2151fur
   <version>` — and `python -m vgm2151fur <input> --report` prints the version of the code you have
   (`converter   vgm2151fur <version>`).  **If the two differ, reconvert before listening.**  Files
   without a version stamp predate the clock-transposition and linear-K007232-level fixes and
   should be rebuilt.  `tests/test_vgm2151fur.py` pins behavior, not file bytes, so reconversion is
   always safe for generated packs (hand edits in `output/*/hybrid/Furnace sequences` are the one
   thing a rebuild replaces).

2. **A GUI export taken after playback.**  Furnace's GUI export can inherit the live engine state
   (last per-channel fine tune / macro / slide state), and the result is *not* deterministic.
   Measured on Battle Garegga "Megalomaniac": two exports of the same `.fur` were 1 016 358 and
   1 272 150 bytes with identical key-on counts and times, but the polluted one had 33 % more
   pitch writes and per-channel *constant* detunes (up to +0.56 st) with two channels untouched —
   while two **headless** exports were byte-identical, and the clean GUI export matched them
   exactly.  So: `fur2vgm.py` (headless) or a freshly (re)loaded song with playback stopped is the
   only trustworthy export.  If an export "drifts" while the `.fur` re-imports clean, this is why.

## Known gaps and edge cases

What is still wrong or lossy, and how to see it.  `fur2vgm.py` + `vgmdiff.py` are the first step
for anything audible, with the caveat above: they only see KC/KF pitch and key-ons.

- **Vibrato varies per note, but the file carries one trajectory per instrument.**  Drivers
  script it by re-writing `0x38+ch` (PMS/AMS) while the note is held; `analyze._pick_lfo_macros`
  picks the *most common* trajectory per instrument and the writer attaches it as an FMS/AMS
  macro (codes 10/11).  Notes that deviate — fewer steps, a different endpoint, no ramp at all —
  get the common one.  This is the known remaining vibrato problem (`vgmdiff.py` cannot see it;
  export the track and compare `0x38`–`0x3F` writes, or listen).
- **Operator-level key tricks are partly flattened.**  `analyze.py` now tracks the KON mask per
  operator (an envelope restarts only on a 0→1 bit transition, matching ymfm), so redundant
  key-on writes and per-operator changes while a note sounds no longer become spurious retriggers
  — Gradius II writes those hundreds of times per track (`21 Ranking`: 529 such writes, 263 fewer
  spurious note events after the fix).  A note that starts with some operators
  un-keyed gets a patch variant with those operators at TL 127 (~-95 dB), the nearest a static
  OPM instrument gets to an envelope that never started (3-op percussion/brass, 34 notes across
  the packs).  Still flattened: an operator *released* or *re-keyed* mid-note (0x78 → 0x70) —
  the note keeps all four operators sounding; there is no per-operator gate in Furnace's OPM.
  Identification route: scan `0x08` writes for masks other than `00`/`78` and compare with the
  converted `.fur` note counts.
  A carrier TL write while the voice is held *is* now carried: it becomes a volume-column step
  relative to the instrument's own carrier TL (0.9.37; see "Mid-note carrier TL fades").  Only
  the carrier is representable — a modulator rewrite still leaves the timbre static.
- **Mid-note carrier TL fades.**  Some drivers (Namco System 86: Genpei Toumaden) fade a held note
  by rewriting `0x60`-`0x7F` with no retrigger.  The key-on snapshot cannot carry that, so
  `analyze` records the carrier TL steps of a held note and `furwrite` writes them on the volume
  column as `127 - (TL - instrument TL)`.  The OPM volume column only attenuates a KVS carrier,
  and the log curve (`newVolumeScaling`) makes the mapping exact, so a fade-out survives; a step
  *louder* than the instrument cannot (the column's ceiling is the instrument TL).  A region that
  also changes the modulator stays static.  On "14 Game Over" the exported envelope now tracks the
  source within ~1 dB; before, the held chord stayed ~50 dB loud through the fade-out.
- **The OPM LFO depth is seeded with the hardware reset.**  Furnace's arcade core defaults PMD/AMD
  to `0x7f`, the OPM resets them to 0.  A driver that never writes `0x19` but loads PMS/AMS into
  its instruments (Namco System 86) would otherwise play maximum vibrato/tremolo the rip never
  had; `analyze` sends `1E 00`/`1F 00` before row 0 and any real `0x19` write overrides it.

- **KC/KF movement below the sweep threshold is dropped.**  `_finalize_sweep` emits a trajectory
  only when peak-to-peak is ≥ 4 units of `kc_kf_units` (≈ 6 cents); register-written vibrato
  narrower than that disappears entirely.
- **The KF's low two bits are dropped** (`kf >> 2` in `kc_kf_units`/`kf_to_e5`): sub-unit pitch
  detail (≈ 0.8 cents) is not representable.  Inaudible, but lossy.
- **Sweep attack transient.**  A ramp rate can only change on row boundaries, and the drivers
  write the first sweep step ~2 ticks after the key-on, so it is spread over the first row: the
  steepest sweeps still land up to ~1.5 semitones off for the first couple of ticks.  Snapping
  that step with E5 was measured to be a wash (the early blip trades against the ramp's late
  lag), so the attack stays on the note's own pitch.  Rebuilt pack, measured: steep-sweep
  channels 24–130 units worst per note (Burning Fighter 24/26, Run Through 110/112/130,
  Emergency!! dives ≤ ~190, Block Hole 70–119), everything else ≤ 100 and mostly single digits.
  Every deviation above ~60 is a short attack blip or a ≤ one-KC-step ripple on the sparsest
  staircases (Make'em Dance 91), not a sustained pitch error.
- **A sweep that crosses the next note's row.**  The tail keeps its ramp through the last row it
  owns and the stop lands on the later note's own row (or the later sweep's `k=0` rate takes the
  row over); the later note's attack reads its own pitch either way (measured).  The cost: a rate
  on a row starts at the row's start even when the key-on is delayed, so a fast first-row rate
  drags the *previous* note's last few ticks, and the crossed tail cannot track the part of its
  trajectory that lies inside the later note's row (up to one row of travel).  Note-rows carrying
  a stop are fine, so no rate survives into a following note; the old audit invariant (*no
  note-on row may have a nonzero incoming `01xx`/`02xx` rate* — read patterns with
  `vgm2151fur.furio.parse_fur` and scan each channel's order in sequence) now only sees each sweep's
  own note-on row rate, which is intended, plus the crossed tail's stop.
- **Slides are calibrated against Furnace 0.6.8.3** (the bundled console build).  Another Furnace
  version may scale `01xx`/`02xx` differently — re-measure with `fur2vgm.py` + `vgmdiff.py`
  before trusting a different player.
- A handful of long tracks change their note unit between sections (Gradius II "The Old Stone
  Age", Gradius III "A Long Time Ago", Super Contra "Creature From Outer Space").  They convert
  with a low-confidence grid (smallest candidate unit) and exact per-note placement, but a single
  constant row tempo cannot follow the sections; per-section `09xx` speed changes would.
- The Salamander drivers never touch the OPM LFO registers (`0x18/0x19/0x1B` write counts: 0)
  and write PMS/AMS only 8–9 times, so the LFO macro path is inert there — Surprise Attack and
  Willow do use it (see the vibrato bullet).
- Notes E-3/F-3/F#-3 (Furnace 100/101/102) are written as-is: in format 157 those are ordinary
  notes; only 180 is note-off. (`tests/test_vgm2151fur_timing.py::test_note_values_100_102_survive_roundtrip`)

## Instrument encoding

`.fur` FM instruments (`INS2` + `FM` feature) store the four operators in **YM2151 register
order** (0x40, 0x48, 0x50, 0x58) — the same order the analyzer captures them in. Furnace reads
them with a straight loop (`readFeatureFM`) and `arcade.cpp` writes `op[j]` to `ADDR +
opOffs[j]` with `opOffs = {0x00, 0x08, 0x10, 0x18}`; `orderedOps = {0, 2, 1, 3}` there only
drives per-operator effect dispatch. An earlier revision permuted the operators when writing,
which swapped the 0x48 and 0x50 contents of every instrument — inaudible where both pairs are
alike (most Konami patches), but it wrecked timbres whose pairs differ (Destroy Them All's riff
patch: alg 4 with carrier TL 0x0C against modulator TL 0x1E, so the character, not the pitch,
came out wrong). `tests/test_vgm2151fur.py::TestFMOperatorOrder` locks the straight order.

## Checking by ear

```powershell
python -m vgm2151fur "05 Starfield (Stage 4 BGM).vgz" --report
python -m vgm2151fur "05 Starfield (Stage 4 BGM).vgz" --out output/salamander/fur
& ..\vgm2wav-mute.exe "05 Starfield (Stage 4 BGM).vgz" "$env:TEMP\starfield.wav"
```

Play the `.fur` next to the WAV (Furnace 0.6+). The grid line in `--report` should read
`491.4 Hz, speed 7 -> 628.0 samples/row` (Starfield) or `489.9 Hz, speed 8 -> 720.1 samples/row`
(Destroy Them All) with `key-on lattice ..., 100% within 6%; row = 1/8 of the measured 16th,
7/8 ticks/row`. Drum-channel rows should show `EDxx` (frame-exact note starts), `E5xx` (initial
KF fine tuning) and `02xx`/`01xx` ramps with an `xx = 0` stop at the end of each sweep.
