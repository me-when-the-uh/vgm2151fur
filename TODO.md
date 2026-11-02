# TODO

Open follow-ups. The loop, legato and mixed-family selection work is in the
tree. These items are the parts that are not.

## Mixed OPL + arcade logs convert one family

The analyzer now counts rising key-ons per family and converts the side with
more, and the report names both counts and the loss. Bubble Bobble track 02
keeps its OPL main theme and reports the 12 dropped YM2203 notes.

Remaining:

- Support two FM families in one module. The writer already carries a chip
  list and a channel offset. A mixed module would need both family blocks,
  both instrument lists and per-system volume maxima. Check Furnace's system
  table and multi-chip playback first.
- OPL2 with OPL3 now selects the denser kind and warns. Two crystals of the
  same core still share one system and the header clock. A second crystal
  would need a second system.
- The warning now carries writes, key-ons and the selected family. Keep it
  that way when two-family modules land.

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
  behaviour against the player before encoding it: TL turned out to store a level,
  not an attenuation (`$4n = 63 - value`), and only a round trip showed that.
- An envelope rate written to a keyed operator does not restart its generator, so
  confirm what the player actually does with mid-note AR/DR entries - whether the
  running envelope takes the new rate or it waits for the next release.
- The key-on audit and the `keys`/`miss` counts say nothing about operator macros.
  The instrument listing shows `op1 $4n x168@2t`; a per-track total would make
  the difference visible without reading every patch.

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
