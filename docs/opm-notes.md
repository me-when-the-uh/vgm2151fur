# OPM conversion notes

How YM2151 (OPM) register streams convert, and the timing model that
replaced the old fixed 60 Hz grid. The chip-level notes (clock, macros,
sweeps, verification) apply to every pack; read the "Known gaps" section
before assuming anything is faithful.

## Chip clock

A `.fur` does not store a chip clock in its INFO block; the clock comes from
the chip's *flags*, and Furnace's YM2151 default is 3.579545 MHz. The
converter writes `customClock=<Hz>` in the YM2151's `FLAG` block when the
rip says otherwise.

The clock flag alone does not fix playback pitch: Furnace keeps a note at
its musical frequency at *any* clock (`baseFreqOff` in `arcade.cpp`
compensates the note → KC/KF mapping), so the sounding pitch is set by the
*notes*, not by the clock. A driver's KC/KF registers at 4 MHz sound
12·log2(4/3.579545) = +1.92 semitones above what the same registers mean at
3.579545 MHz, so `analyze.py` shifts every note by that much (in
1/64-semitone units; sweeps are deltas from the note, so they follow for
free). With both halves in place the transposition and Furnace's
compensation cancel exactly in register space, so the export contains the
rip's own KC/KF at the rip's own clock: the register audit stays clean and
the audio matches. The flag is only written when the clock differs from
3.579545 and the transposition is zero for rips already at the default, so
those convert unchanged. `customClock` works for the K007232 the same way.

The file *version* selects slide behavior. Below version 176 Furnace sets
the OPM `brokenPitch` compat flag, which doubles `01xx`/`02xx` porta speed
and pitch-macro output in `arcade.cpp`. The calibration below ("one rate
unit = one kc/kf unit per tick") holds for `brokenPitch=true`; bumping
`FUR_VERSION` past 176 would halve every slide and need a re-measure.

## Timing model

The drivers schedule notes on a unit that is neither 735 nor 882 samples,
and the effective unit differs per channel and per section by a few percent
(a scheduler characteristic, not log noise). `vgm2151fur/timing.py` fits
this lattice from the key-on times: several candidate units are tried
(per-channel median and bucketed mode plus the global mode), each is scored
by how many intervals it explains as integer multiples, and among
near-best candidates the coarsest unit wins. The drivers also quantize
register writes to the 60 Hz video frame, so individual key-ons sit up to
half a frame off the ideal lattice; the fits use the lattice average and
the per-note deviation is absorbed by `EDxx`.

Rows subdivide the measured 16th into a few rows, and each row is given a
handful of ticks (Furnace: `hz` = ticks/second, `speed` = ticks/row)
chosen so one tick is about 2 ms. `Song.place()` floors to the row and
emits `EDxx` for the tick offset (`ED xx` delays the channel row by `xx`
ticks, and the delay must be `< speed` under the strict delay policy). The
result: note starts land within about a millisecond, notes from different
channels that the driver wrote in the same frame land on the same tick, and
doubled lines stay unison instead of splitting a row apart.

Tracks whose unit changes between sections have no single global lattice;
the estimator then keeps the smallest real candidate so the fastest section
still fits without collisions, marks the grid `low confidence, sections
change unit`, and every note is still placed at its true write time via
`EDxx`.

## Loop point

The `.fur` ends with `Bxx` on the last row that carries content in the
final order, not on the last row of the padded order. The final order is
only as long as the music, so jumping at the padded end would insert
silence before every loop restart. Jingles with no VGM loop get no `Bxx`.
The jump row is derived from the last event's row, and `EDxx` is stripped
from it because a delay on the jump row swallows `Bxx` (it is a pre-effect
that returns before later effects on the row are read).

## Vibrato onset (FMS/AMS macros)

Several drivers script the LFO sensitivity: once a note is held, the driver
rewrites `0x38+ch` every ~0.1 s, stepping PMS and AMS up, so the note
starts steady and drifts over ~1 s and resets at the next key-on. The
analyzer picks the most common per-instrument trajectory and the writer
emits it as FMS/AMS macros (codes 10/11) with that step duration in ticks
(`speed = median_step / (44100 / hz)`); a fixed 735-samples-per-tick frame
would make every step ~9× too short, i.e. full vibrato the instant the note
starts. One trajectory per instrument is a known approximation; see the
gaps section.

## Percussion pitch sweeps

The percussion channel does not hold a constant pitch: right after each
key-on the driver writes a KC/KF ramp that glides the note. The analyzer
captures those writes per held note (merging the driver's KC/KF write pairs
a few samples apart), takes the note's pitch from the first pair when it
lands right after the key-on, and `furwrite._emit_sweep` encodes the
source's step trajectory (each merged KC/KF pair is a step) as per-row
01xx/02xx ramps plus E5 steps:

- each row gets the rate that takes the pitch (after any E5 step on that
  row) to the trajectory's value at the row's end: boundary sampling, not a
  smoothed fit. A least-squares window smears a fast step backwards into
  the row before it and averages a steep climb into a too-slow ramp.
- a step that lands on a row's start is absorbed by an E5 change (one
  fine-tune unit per 1/64 semitone, ±1 st range), checked one tick past
  the boundary so a jump just after the row start is still snapped onto
  that row. The note's own row keeps the note's pitch: its first-step
  spread over the row is the attack transient under known gaps. A rate
  starts at the row's start even when the key-on itself is delayed, so a
  fast first-row rate drags the previous note's tail for up to the note's
  `ED` delay; that is a few ticks of the previous, usually already
  bottomed-out, note.
- the row that contains the trajectory's end is rated over the full row
  against the held final value: the ramp physically runs a whole row, so
  rating it over the clipped part would overshoot the bottom of a dive by
  several times the remaining travel, and an E5 pin cannot correct more
  than ±1 st of that.
- the stop lands on the following row. A stop on a later note's own row is
  safe (the key-on still reads its own pitch), and it is the row that must
  carry it when the trajectory runs into that note's row. Such a crossing
  trajectory keeps its ramp through the last row it owns (the ramp dies at
  the later note's own k=0 rate or at the stop, whichever lands first), but
  is never given a *rate* or E5 on any later row: writing both a foreign
  rate and the note's own k=0 rate on one row means two 01xx/02xx codes
  coexist and the net slide becomes column-order luck.
- a written rate of `x` moves the pitch by `x` units of `kc_kf_units` per
  tick, measured by exporting a converted file back to VGM with Furnace and
  comparing the realized trajectory against the source.

The KF register's fraction is its *top* six bits: ymfm computes
`block_freq = (KC << 6) | (KF >> 2)`, 64 steps per semitone. `kf_to_e5`
maps it to E5xx 1:1, one E5 unit = one KF step; values above 0xBF saturate
the register.

**E5 is a channel state.** Furnace keeps the last `E5xx` fine tune until it
is changed, while the source driver re-writes KC/KF at every key-on. The
writer therefore re-sends E5 whenever a note's value differs from the
channel's last one, *including back to the neutral 0x80*, and sweep steps
are recorded per row so the replay emits all E5 changes in row order.
Writing it only for non-neutral values leaks one note's fine tune into
every following note on that channel.

## Verifying against Furnace

`fur2vgm.py` runs the Furnace console build headless:
`furnace -console -vgmout out.vgm song.fur`. The exported VGM is the ground
truth for a conversion. `vgmdiff.py source.vgz export.vgm [channels...]`
pairs key-ons by time and compares the KC/KF history of both files on the
absolute timeline, reporting per channel the onset shift plus the median /
p95 / worst deviation in `kc_kf_units` (64 per key-code step).
Register-exact conversions report a median of 0 and a p95 of a few units;
anything sustained in the tens is audible.

Two traps in reading those numbers. Do not compare from *paired* onsets:
the converter drops key-ons the chip ignores (an operator that is already
keyed does not restart), so a driver that re-strikes a held note leaves the
export with fewer onsets and any pairing offset lands across the steep
KC/KF jumps those drivers write. And a track that loops drifts against its
own loop point sample by sample, so the last notes can show a large
deviation the middle never does; read the distribution, not one worst case.
The tool only reads KC/KF and key-ons: LFO (`0x38`–`0x3F`) and
operator-state differences need a raw write-stream comparison or an ear
check.

### Two traps in producing the export

1. **A stale `.fur`.** Every `.fur` carries the converter version in its
   module comment (`Transcribed from VGM by vgm2151fur <version>`), and
   `python -m vgm2151fur <input> --report` prints the version of the code
   you have (`converter   vgm2151fur <version>`). If the two differ,
   reconvert before listening. Reconversion is safe for generated packs;
   hand edits in your own working copies are the one thing a rebuild
   replaces.
2. **A GUI export taken after playback.** Furnace's GUI export can inherit
   the live engine state (last per-channel fine tune / macro / slide
   state), and the result is not deterministic: two exports of the same
   `.fur` can differ in size with identical key-on counts while the
   polluted one carries per-channel constant detunes, whereas two headless
   exports are byte-identical. Use `fur2vgm.py` (headless) or a freshly
   loaded song with playback stopped.

## Known gaps and edge cases

- **Vibrato varies per note, but the file carries one trajectory per
  instrument.** Notes that deviate from the common PMS/AMS trajectory get
  the common one. `vgmdiff.py` cannot see this; export the track and
  compare `0x38`–`0x3F` writes, or listen.
- **Operator-level key tricks are partly flattened.** The KON mask is
  tracked per operator (an envelope restarts only on a 0→1 bit transition,
  matching ymfm), so redundant key-on writes and per-operator changes while
  a note sounds do not become spurious retriggers. A note that starts with
  some operators un-keyed gets a patch variant with those operators at
  TL 127 (~-95 dB), the nearest a static OPM instrument gets to an envelope
  that never started. Still flattened: an operator released or re-keyed
  mid-note; there is no per-operator gate in Furnace's OPM.
- **Mid-note carrier TL fades.** Some drivers fade a held note by
  rewriting `0x60`-`0x7F` with no retrigger. The key-on snapshot cannot
  carry that, so the analyzer records the carrier TL steps of a held note
  and the writer writes them on the volume column as
  `127 - (TL - instrument TL)`. The OPM volume column only attenuates a
  KVS carrier, and the log curve makes the mapping exact, so a fade-out
  survives; a step *louder* than the instrument cannot (the column's
  ceiling is the instrument TL). A region that also changes the modulator
  stays static.
- **The OPM LFO depth is seeded with the hardware reset.** Furnace's
  arcade core defaults PMD/AMD to `0x7f`, the OPM resets them to 0. A
  driver that never writes `0x19` but loads PMS/AMS into its instruments
  would otherwise play maximum vibrato/tremolo; the analyzer sends
  `1E 00`/`1F 00` before row 0 and any real `0x19` write overrides it.
- **KC/KF movement below the sweep threshold is dropped.**
  `_finalize_sweep` emits a trajectory only when peak-to-peak is ≥ 4 units
  of `kc_kf_units` (≈ 6 cents); register-written vibrato narrower than that
  disappears.
- **The KF's low two bits are dropped** (`kf >> 2`): sub-unit pitch detail
  (≈ 0.8 cents) is not representable. Inaudible, but lossy.
- **Sweep attack transient.** A ramp rate can only change on row
  boundaries, and the drivers write the first sweep step ~2 ticks after the
  key-on, so it is spread over the first row: the steepest sweeps land up
  to ~1.5 st off for the first couple of ticks. Snapping that step with E5
  trades an early blip for the ramp's late lag, so the attack stays on the
  note's own pitch.
- **A sweep that crosses the next note's row.** The tail keeps its ramp
  through the last row it owns and the stop lands on the later note's row
  (or the later sweep's `k=0` rate takes the row over); the later note's
  attack reads its own pitch either way. The crossed tail cannot track the
  part of its trajectory that lies inside the later note's row (up to one
  row of travel). No rate survives into a following note.
- **Slides are calibrated against Furnace 0.6.8.3.** Another Furnace
  version may scale `01xx`/`02xx` differently; re-measure with `fur2vgm.py`
  + `vgmdiff.py` before trusting a different player.
- Long tracks that change their note unit between sections convert with a
  low-confidence grid (smallest candidate unit) and exact per-note
  placement; a single constant row tempo cannot follow the sections.
- Notes E-3/F-3/F#-3 (Furnace 100/101/102) are written as-is: in format 157
  those are ordinary notes; only 180 is note-off.

## Instrument encoding

`.fur` FM instruments (`INS2` + `FM` feature) store the four operators in
**YM2151 register order** (0x40, 0x48, 0x50, 0x58), the order the analyzer
captures them in. Furnace reads them with a straight loop (`readFeatureFM`)
and `arcade.cpp` writes `op[j]` to `ADDR + opOffs[j]` with
`opOffs = {0x00, 0x08, 0x10, 0x18}`; `orderedOps = {0, 2, 1, 3}` there only
drives per-operator effect dispatch. A permutation here would swap the 0x48
and 0x50 contents of every instrument.

## Checking by ear

```powershell
python -m vgm2151fur "track.vgz" --report
python -m vgm2151fur "track.vgz" --out output/pack/fur
& ..\vgm2wav-mute.exe "track.vgz" "$env:TEMP\track.wav"
```

Play the `.fur` next to the WAV (Furnace 0.6+). The grid line in
`--report` names the measured lattice, the rows per second and the
subdivision it chose. Drum-channel rows should show `EDxx` (frame-exact
note starts), `E5xx` (initial KF fine tuning) and `02xx`/`01xx` ramps with
an `xx = 0` stop at the end of each sweep.
