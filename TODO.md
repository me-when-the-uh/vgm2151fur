# TODO

Open follow-ups. The loop, legato, multi-family writing and the newer chip
paths are in the tree. These items are the parts that are not.

## Mixed chip families in one module

`Song.fm_systems` now carries every pitched system, so a YM2151 or YM2612 log
beside an OPL family writes both sets of registers, instruments and volume
ceilings into one module. The analyzer scores each family from its own rising
key-ons and drops a chip that only received its init block. `docs/merged-logs.md`
is the reference and `tests/test_vgm2151fur_combo.py` converts real merged rips
and reads each chip's registers back.

Remaining:

- A YM2610 or YM2608 beside an OPL family still converts one side only. Their
  sample engines and SSG already claim columns a second pitched chip cannot
  share cleanly.
- OPL2 with OPL3 selects the denser kind and warns. Two crystals of the same
  core still share one system and the header clock. A second crystal would need
  a second system.
- `diag.pitch_runs()` chooses its stream from the source log's chip census, so
  `report --verify` on a combined module follows one family whatever channel it
  is asked about. Per-family verification today lives in `TestMergedRips`.
- An OPL4 (YMF278B) side of a merged log is not converted; the module keeps
  only whatever else writes.
- The warning carries writes, key-ons and the selected family. Keep it that
  way.

Related OPL audit gaps on the same pack:

- `TestOplRealRips` finds attacks at the end of the exported tail on melodic
  channels. The seam allowance only covers rhythm channels.
- Pitch cells land up to about 2.5 cents from the source. The pitch table
  documents this rounding, and `compare` does not report it.

## Meter search for misplaced loop markers

The converter trusts the VGM marker. The end is the marker plus the header's
loop length, the row length is calibrated to that length, and pre-wrap events
stay on the last row. That resolves the staggered, duplicate and
cut-sample loops on the reference packs.

Remaining:

- A marker that is off by a phrase still keeps the VGM offset. A
  meter-aware search would need an accent matrix from every voice, a choice
  between 4/4, 3/4, 6/8, 8/8 and 12/8, and a phrase head confirmed by the
  recording's own copy. Do not move a loop unless the accent score clears the
  runner-up and the copy at one loop length later agrees.
- Report the detected meter, bar length, phrase length, marker offset and
  confidence. Do not move the loop when confidence is low.
- Real-pack cases: Vulcan Venture track 15, Metal Slug 3 tracks 12 and 26. Assert the loop
  opens on the detected phrase head and not only on a pitch-class repeat.
- `test_loopfind.py` still exposes the old within-bar `_phase_loop` and
  `_phrase_seam` helpers. Production `apply_loop` does not use them. Remove
  them or make the meter search use them.

## Operator macros beyond the level

A modulator `$4n` that the log moves under a held note now travels as that
operator's TL macro: captured in `_collect_opl`, cut onto a `+lvl` instrument copy
by `_bind_op_macros`, inverted by `furwrite._write_op_ma`, and described with its
measured numbers in `docs/opl.md` ("Operator macros"). Verified through the
bundled console build on two Tyrian rips: 6,358 of 7,046 and 13,253 of 15,222
mid-note operator level writes reach the chip, where before none did.

Remaining:

- Drive the other fields the same way - `$6n` (AR, DR), `$8n` (SL, RR) and the
  OPL2/OPL3 waveform - using `DivMacroTypeOp` codes 1, 2, 4, 5 and 18. Those
  writes still wait for the next key-on and are counted in the report (36 on
  `21 Come Back to Me, Savara`). Measure each code's polarity, mask and hold
  behaviour against the player before encoding it. TL turned out to store a level,
  not an attenuation (`$4n = 63 - value`), and only a round trip showed that.
- An envelope rate written to a keyed operator does not restart its generator.
  Confirm what the player actually does with mid-note AR/DR entries - whether the
  running envelope takes the new rate or it waits for the next release.
- The key-on audit and the `keys`/`miss` counts say nothing about operator macros.
  The instrument listing shows `op1 $4n x168@2t`; a per-track total would make
  the difference visible without reading every patch.

## OPNA, OPLL and the newer sample paths

YM2608, YM2413, Y8950, RF5C68/RF5C164, SegaPCM, C140/C219, SSG PCM and OPL4
wave playback are in the tree, with unit tests and Furnace-backed checks where
the real rips are available. Open:

- **OPLL drums in the corpus sweep.** `mado123-opll` and any rip that toggles
  `$0E` exercise the two-machine writer outside the hand-built test.
- **Corpus sweep.** Convert every rip under `gs-opna\done`, `ed4-opna\done` and
  `mado123-opll` and audit each. Two reference tracks are not a corpus.
- **YM2608 CSM.** Channel-3 Ext, the rhythm kit and delta-T ADPCM-B convert.
  CSM timer key-ons are not synthesized, the same as the rest of the OPN family.
- **OPL4 ROM waves need a YRW801 image.** `VGM2151FUR_YRW801` must point at the
  `.rom` or `.zip`; a missing image drops ROM wave notes and warns. RAM-only
  rips need the RAM dump in the VGM.
- **Looping SSG PCM DAC streams play once.** The AY sample instrument is
  one-shot, so a looping `$93` stream is flattened and reported.
- **SegaPCM/C140 mid-note window writes.** Start/end/loop/bank changes under a
  held voice stay at the key-on values; only volume and frequency follow the
  register stream.
- **AY volume decays.** Per-tick volume steps still lose drum decays at
  condense factors. Moving them into the instrument's volume macro is the next
  step (`docs/psg-open-items.md`).

## Atari POKEY

POKEY is in the tree: four voices per chip, dual-chip logs, per-frame AUDC
volume and waveform instrument macros, AUDCTL macros and `11xx` effects, joined
16-bit pairs, high-clock/15 kHz/filter modes, silent filter-control notes,
exact-register pitch inversion and PAL/custom clocks. The seven-rip pack
(including `porkyspokeys`) covers the common driver shapes, and the modules keep
the source's unity mix.

Remaining:

- **Volume-only mode.** `AUDC` bit 4 is a DC/DAC level Furnace cannot reproduce;
  the converter warns and does not emit it.
- **Two-tone mode.** `SKCTL` bit 7 exists only on the ASAP core; the converter
  reports it instead of converting it.
- **Distortion 7.** Furnace's wave map has only the distortion-6 poly4 tap, so
  source distortion 7 plays as 6 and warns.
- **Filter-control corpus.** The silent note on the filter clock channel is
  verified on `combat_vehicle`; broaden the check to other filter-heavy rips.

## MultiPCM, uPD7759 and 32X PWM

- **MultiPCM envelope writes after key-on.** ADSR/LFO values come from the ROM
  sample header; a driver that rewrites registers 7-10 per note has no
  per-note instrument copy yet.
- **MultiPCM sample-memory limits.** Furnace's platform holds 512 bodies and
  2 MB. A source that triggers more than 512 distinct ROM samples needs a
  clustering/remap policy.
- **uPD7759 slave timing.** The converter decodes the command stream after the
  six-byte handshake. Validate against VGMPlay on the `01 IPL` and
  `07 Picture Book` references, especially FIFO stalls between port writes.
- **uPD7759 rate changes.** Phrase-internal rate changes are resampled to one
  module body; a segmented representation could preserve them exactly.
- **PWM compressed banks.** The n-Bit copy and shift subtypes decode; table
  (`0x7F`) and DPCM-compressed banks are reported as unsupported.
- **PWM chunk reuse.** Attack transients are the primary candidate starts;
  repeated bodies and identical left/right bodies share one module sample.
  Rest boundaries and 0.25-second chunks are the fallbacks.  Bodies shorter
  than a quarter chunk fold into the next body, and silence-only pieces are
  dropped.
- **PWM direct level writes.** A log that writes registers 2/3/4 directly
  without DAC streams only gets point samples, not a continuous stream.

## From the 2026-11 batch (PSG default, Ext columns, OPM key-on bits, OKI length)

- **`02 Zero Cop [Opening]` (Batrider) differs in pitch on FM 4-6.** Four runs,
  all short: `-1.1` to `-2.2 st` over 206-371 ms around t=4.6-5.2 s, and the
  module's loop/tail timing drifts afterwards (`other: 1978888 samples` against a
  `1506339` source). It reproduces at HEAD identically, so it is not from the
  `$08` bit-order or level work - most likely a legato/retune boundary. Start by
  dumping `$28-$37` for those channels in that window against the render.
- **Level parks are not reproduced.** Batrider drivers silence a voice by writing
  `7F` to every operator (`08 Purple Twilight` does it six times per channel);
  the converter ends the note instead, so those register values never appear in
  the export. Musically equivalent, but it keeps `vgmdiff` from being fully clean
  on songs that park rather than key off. Count them in the report so a real
  dropped level is distinguishable from a park.
- **Modulator automation is still static.** `_bind_opm_levels` opens the carriers;
  an operator that modulates and moves mid-note keeps its key-on value, and the
  OPM has no per-operator gate to do otherwise. The YM2151 corpus says automation
  is overwhelmingly on the op3/op4 carrier pair, so the loss is rare - measure it
  before designing anything.
- **`Ext` mid-note `$4X` on a row the note owns is dropped** (`vol_step_dropped`)
  in favour of the note's own volume. `01 Exit Logo` lands 21/21 and 23/23 of its
  scripted levels, so the remainder is a row-boundary rounding; check whether an
  `EDxx` delay would recover it before adding a second volume mechanism.
