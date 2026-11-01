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
