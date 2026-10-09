# The .fur writer and the pitch model

This is the reference for the decisions baked into `furwrite.py` and the
sweep model. Read it before changing either. The comments in the code point
here instead of repeating it.

## File layout

A module is `FUR_MAGIC` (16 bytes), a version (`157`), a reserved word, and a
pointer to the song info block. The writer emits the old `INFO` block, which
Furnace 0.6 reads and re-saves. Blocks that follow are `ADIR` (empty asset
directories), `INS2` (instruments), `SMP2` (samples), `PATN` (patterns), all
pointed to from the info payload.

The INFO payload order:

```
24-byte song header (timebase, speed, hz, pattern length, orders, counts)
32 chip IDs (first N used, rest zero)
32 volume bytes (64 = unity; old readers use these)
32 pan bytes
128 bytes of chip flag pointers
name, author, tuning, compat phase 1
instrument / sample / pattern pointers
orders, effect columns, channel flags, channel names
comment, master volume, compat phase 2, name fields (8 strings)
per-chip float triples (vol, panL, panR; readers >=135 use these)
patchbay, speed pattern, groove count, ADIR pointers
```

Effect columns are fitted per channel: each channel asks for one column per
effect its busiest row uses (1..8). Furnace derives a pattern column's width
from that count, so a module opens with columns matched to its content
instead of the 8-column build capacity. The count never drops below the
busiest row, or a load-and-re-save in Furnace would drop effects.

Patterns are 256 rows (Furnace's maximum, up from 64). A measured grid runs
100 rows/s and up, so long tracks used to need hundreds of 64-row orders;
256 keeps the order list readable. The loop row starts its own order (the
short order before it ends with `D00`), which is what keeps loop points
precise, and the last order's `Bxx` loop jump sits on its last content row,
not on the padded end. Loop finding can set that end earlier than the last
note; the rows after it are not written.

`edit.py` patches both volume copies, so every reader agrees. The float
triples are located from the payload tail, anchored on the ADIR pointers: the
tail is `[triples][patchbay 4][auto 1][pad 8][speed 17][groove 1]`, so the
triples start 31 bytes plus 12 per chip before the first ADIR pointer.

## Chip IDs and flags

- `0x82` YM2151, `0xC6` K007232, `0xAA` MSM6295, `0xAB` MSM6258, `0x9B`
  SegaPCM, `0xCE` C140, `0xD0` C352.
- The YM2151 flag carries `customClock=` when the source clock is not
  3579545: a 4 MHz OPM plays flat otherwise.
- The K007232 flag carries `stereo=true` and a clock when it differs from the
  default.
- The OKI flag encodes pin 7 and banking; the C140 flag carries the clock and
  the type. The C352 flag is `customClock` set to the crystal a core that
  divides by 288 should run. The local `0xD0` core in `furnace/` ticks at
  `clock/288` and takes this value as-is; a core copied from `c140.cpp`
  instead ticks at clock/192 and would need the C140 2/3 scale. C352
  instruments are Amiga type 4, which that core plays. This writer stores
  the real crystal.
- The volume byte per chip comes from the reference player's mix, not a flat
  unity: libvgm defaults, extra-header volume entries, the C140 core scale
  (2/3) and instance division. `_FURNACE_VOLUME_SCALE` corrects the Furnace
  cores against measured renders.

## Instruments

FM instruments use the INS2 `FM` feature with four operators in YM2151
register order (0x40, 0x48, 0x50, 0x58), which is also Furnace's internal
order. The `MA` feature carries FMS/AMS macros for driver-scripted vibrato
(PMS/AMS steps recorded while the note sustains).

PCM instruments bind one sample each: K007232 two channels, MSM6295 four
voices per chip, MSM6258 one, SegaPCM sixteen, C140 twenty-four, C352
thirty-two. Sample bodies are stored 8-bit signed (C140 and C352 mulaw are
16-bit linear).

## Pitch units

Everything pitch-related in the converter works in kc/kf units: 1/64
semitone. One key-code step is 64 units, and the YM2151 KF register
contributes its top six bits (`kf >> 2`).

Two effects move pitch, and their scales differ per platform:

| Effect | YM2151 | PCM (C140, SegaPCM) |
|---|---|---|
| E5 fine tune, per byte step | 1/64 st | 1/128 st |
| 01xx/02xx slide, per parameter unit per tick | 1 kc/kf unit (1/64 st) | 1/128 st |

E8xx/E9xx quick legato adds semitones to the playing note in one write. It is
cumulative (`note += y`), applies one tick after the row, and resets at the
next note. Measured on 0.6.8.3 through both platforms.

## The sweep model

A driver's pitch motion is a staircase of register writes. The writer turns
the staircase into per-row slides:

- Each row samples the trajectory at its start (one tick in, to catch jumps
  that land just after the boundary) and at its end.
- The row's rate is `(end - current) / span`, where span is the row's tick
  count. The last row of a sweep is rated over its full span against the
  held final value, because the engine's ramp runs past the trajectory's end.
- The written parameter is a whole number per tick, and the model accrues
  exactly what the engine will do: `param` per tick on FM, `param / 2` per
  tick on PCM. Accruing the ideal rate instead compounds a
  fraction-of-a-unit error into semitones over long climbs (see 0.9.29).
- Moves the slide cannot deliver inside a row become E8/E9 transposes, with
  the slide covering the remainder.
- Sub-semitone residue is pinned with E5 on the row after the rate stops.
  The E5 value is an absolute byte around the note's own fine tune, so the
  pin only ever expresses a small correction.
- A sweep stops on the row after its last rated row. When a later note's
  key-on row comes first, the stop lands there instead; the key-on still
  reads its own pitch.

The row grid (`hz` and `speed`) is estimated from key-on spacing and printed
by `--report`. Finer pitch motion than the grid can express is approximated;
row subdivision is the known lever if more fidelity is ever needed.

## The row grid, the BPM, and condensing

Furnace's displayed BPM is `60*hz/(hilight*speed)` (`furnace/src/gui/gui.cpp`,
`calcBPM`), which is 15 x `hz/speed` at the default hilight 4. `hz/speed` is
the row rate, so **only the row length moves the BPM** - changing `speed`
while `hz` tracks it (the tick size) leaves rows/s, and the BPM, exactly where
it was. `--max-bpm N` picks the coarsest row grid whose row rate is under
`N/15`; `--condense N` (and `--variants`) lengthen the chosen grid's rows by N.

`timing.condense_row` multiplies the row length and re-derives `hz`/`speed`
with the same `hz_speed_for_row` helper `estimate_grid` uses, so the two never
disagree. `row_subdiv` (rows per measured 16th) is cosmetic - only `Song.bpm`
reads it - so a 3x factor, which does not land on the subdivision lattice, is
fine.

Condensing is not free. A row carries one note cell and one slide rate:

- two notes in the merged row collide (`note_collision`; the later one wins),
- a PCM restart loses its own row (`pcm_evicted` / `pcm_unplaced`),
- the driver's steps inside a row collapse into one straight line, which
  `dev_max` measures as the worst deviation in 1/64 semitone.

`furwrite` counts all of these into the `stats` dict `write_fur` takes;
`LOSS_KEYS`/`loss_total` sum the structural ones and `dev_max` reads the
fidelity number. `convert_variants` writes the 1x..Nx set and, in `lossless`
mode, marks the largest factor with no added structural loss and no more than
6 units (0.1 st) more deviation than 1x, else falls back to 1x.

`dev_max` saturates on sweep-heavy tracks - Battle Garegga 02 reads 47/52/54/56
units for 1x/2x/3x/4x, so it cannot separate the good 3x from the bad 4x. With
`verify` (CLI `--verify`, always on in the menu) `convert_variants` instead
renders every candidate through Furnace and counts pitch runs against the
source at 0.25 st / 60 ms, accepting a factor only up to `baseline + 1` runs.
On Battle Garegga 02 that is 0/0/1/44 runs - 3x passes, 4x does not; on
Salamander Starfield 4/6/9/23 and on FZ2DX Cholacoray 3/8/7/12, so both stay
at 1x. A factor is a row-length multiplier, so it may be fractional
(`--factors 1,1.5,2,2.5,3,4`). The counters are structural: sub-semitone
shimmer that survives as amplitude or timbre is invisible to them, which is
exactly what `--verify` exists to catch.

`layout="dirs"` (menu default, CLI `--dirs`) moves each accepted factor to
`<out_dir>/default/` or `<out_dir>/xN optimised/` and deletes the rest, and
`convert_folder` appends a `condense.tsv` row per factor so the folder records
which renditions were kept and why. The placement happens after the verdict,
so the flat names are what get rendered for `--verify`, then moved.

