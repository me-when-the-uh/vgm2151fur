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

The pattern window follows the content: the last row that carries anything
plus ten empty rows, capped at Furnace's 256-row maximum, which a long track
still reaches. Furnace holds one pattern length for the whole module, so a
short jingle opens as a short pattern instead of one padded to 256 - every
order is that long, which is why the window is fitted to the whole module and
not to one order. The loop row starts its own order (the short order before it
ends with `D00`), which is what keeps loop points precise, and the last
order's `Bxx` loop jump sits on its last content row, not on the padded end.
Loop finding can set that end earlier than the last note; the rows after it
are not written.

`edit.py` patches both volume copies, so every reader agrees. The float
triples are located from the payload tail, anchored on the ADIR pointers: the
tail is `[triples][patchbay 4][auto 1][pad 8][speed 17][groove 1]`, so the
triples start 31 bytes plus 12 per chip before the first ADIR pointer.

## Chip IDs and flags

- `0x82` YM2151, `0xC6` K007232, `0xAA` MSM6295, `0xAB` MSM6258, `0x9B`
  SegaPCM, `0xCE` C140, `0xD0` C352, `0xA1` Konami SCC (K051649), `0xB4`
  Konami SCC+ (K052539), `0x80` AY-3-8910. A module lists the chips the
  source had usable samples for: a melody-only track opens with the YM2151
  alone, with no K007232 pair and no PCM channels it never plays. The SCC and
  AY are tone chips, not sample players: they are written whenever the source
  keys a voice, even under `--melody-only`.
- The YM2151 flag carries `customClock=` when the source clock is not
  3579545: a 4 MHz OPM plays flat otherwise.
- The K007232 flag carries `stereo=true` and a clock when it differs from the
  default.
- The OKI flag encodes pin 7 and banking; the C140 flag carries the clock and
  the type. The C352 flag is `customClock` set to the crystal a core that
  divides by 288 should run. A core that ticks at `clock/288` takes this
  value as-is; a core copied from `c140.cpp` instead ticks at clock/192 and
  would need the C140 2/3 scale. C352 instruments are Amiga type 4, which
  such a core plays. This writer stores the real crystal.
- The SCC and AY flags are empty when the rip's clock is within a hertz of
  Furnace's COLOR_NTSC/2 default (1789772.7) and carry `customClock=` for
  anything else. Furnace builds its pitch table from the double, so sending a
  nearly identical clock would shift a table entry here and there. The AY
  flag also carries `chipType=1` for the YM2149 family (VGM type 0x10/0x11).
- The volume byte per chip comes from the reference player's mix, not a flat
  unity: libvgm defaults, extra-header volume entries, the C140 core scale
  (2/3) and instance division. `_FURNACE_VOLUME_SCALE` corrects the Furnace
  cores against measured renders (AY 1.46, SCC 1.04; measured on two-chip
  modules because the player normalizes a lone chip and Furnace does not).

## Instruments

FM instruments use the INS2 `FM` feature with four operators in YM2151
register order (0x40, 0x48, 0x50, 0x58), which is also Furnace's internal
order. The `MA` feature carries FMS/AMS macros for driver-scripted vibrato
(PMS/AMS steps recorded while the note sustains).

PCM instruments bind one sample each: K007232 two channels, MSM6295 four
voices per chip, MSM6258 one, SegaPCM sixteen, C140 twenty-four, C352
thirty-two. Sample bodies are stored 8-bit signed (C140 and C352 mulaw are
16-bit linear).

SCC instruments (`DIV_INS_SCC`, type 18) carry one 32-byte waveform through
the `WS` wave-synth feature; the wave itself lives in the module's wavetable
list (`WAVE` blocks, addressed by the INFO wave pointer table). The chip
reads signed bytes and Furnace's wavetable is unsigned with 128 as the
center, so each byte is stored as `signed + 128`. Notes with the same image
and the same macros share an instrument.

The AY instrument (`DIV_INS_AY`, type 6) carries a phase-reset macro (file
code 14) - the chip restarts its hardware envelope only when the shape
register is written, and Furnace's platform skips a write whose value did not
change, so a drum hit reusing the previous shape would keep the stale
envelope state - plus the pitch macro when the note's driver wrote one.

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
next note. Checked against Furnace 0.6.8.3 on both platforms.

The SCC and AY do not use the slide effects. Their register is a 12-bit
period, and a mid-note pitch change becomes the instrument's **pitch macro**
(code 4): one value per tick in the engine's pitch unit, which the platform
adds straight to the period. A mid-note waveform morph becomes the **wave
macro** (code 3), a per-tick list of wavetable indices. Both are 16-bit signed
where they can exceed a byte, both carry their own speed and loop, and both
are deduplicated per trajectory, so a recurring bend or morph is one
instrument rather than a cell per step. `pitch.py` ports Furnace's period-mode
`DivPitchTable` and inverts it (including the offset search the macros need),
so a source period is reproduced exactly up to one register step where the
table's octave shift loses resolution.

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
  fraction-of-a-unit error into semitones over long climbs.
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

`estimate_grid` reads its lattice from `vgm.writes` by default (the YM2151
key-on register, reg 0x08). `analyze` can hand it an `onsets` mapping instead,
`{channel: [sample, ...]}`, when the FM stream is too thin to measure. The
C352 and SegaPCM-only board rips have no YM2151 at all, and their only lattice
is the sample-chip trigger times the chip modules already extracted. Triggers
100 samples apart on one voice merge first, because one trigger is often a
key-on followed by its own pitch and volume writes. The FM lattice wins
whenever it measures, which keeps a working grid from moving. Only the 60 Hz
fallback (900 BPM, 60 rows/s) is replaced.

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

`dev_max` saturates on sweep-heavy tracks, so it cannot always separate a
good coarser grid from a bad one. With `verify` (CLI `--verify`, always on in
the menu) `convert_variants` instead renders every candidate through Furnace
and counts pitch runs against the source at 0.25 st / 60 ms, accepting a
factor only up to `baseline + 1` runs. A factor is a row-length multiplier,
and it may be fractional. The default ladder walks the half steps
(`1, 1.5, 2, 2.5, ...`, `default_factors`) and `--factors 1,1.5,2,2.5,3,4`
overrides it. The counters are structural: sub-semitone shimmer that
survives as amplitude or timbre is invisible to them, which is exactly what
`--verify` exists to catch.

`layout="dirs"` (menu default, CLI `--dirs`) moves each accepted factor to
`<out_dir>/default/` or `<out_dir>/xN optimised/` and deletes the rest, and
`convert_folder` appends a `condense.tsv` row per factor so the folder records
which renditions were kept and why. The placement happens after the verdict,
so the flat names are what get rendered for `--verify`, then moved.

