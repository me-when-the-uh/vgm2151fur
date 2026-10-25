# The .fur writer and the pitch model

This is the reference for the decisions baked into `furwrite.py` and the
sweep model. Read it before changing either. The comments in the code point
here instead of repeating it.

## File layout

A module is `FUR_MAGIC` (16 bytes), a version (`157`), a reserved word, and a
pointer to the song info block. The writer emits the old `INFO` block, which
Furnace 0.6 reads and re-saves. `write_fur` returns raw bytes; `convert` wraps
the final module in zlib level 5 (`compress_fur`), the only outer compression
Furnace's `.fur` loader recognizes (no LZ4/Zstd). `parse_fur` accepts both
forms. Blocks that follow are `ADIR` (empty asset
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

- `0x82` YM2151, `0x8D` YM2203, `0x83` YM2612 (`0xA0` Ext, `0xC1` CSM),
  `0x03` SN76489 (SMS), `0xC6` K007232, `0xAA` MSM6295, `0xAB` MSM6258,
  `0x9B` SegaPCM, `0xCE` C140, `0xD0` C352, `0xA1` Konami SCC (K051649),
  `0xB4` Konami SCC+ (K052539), `0x80` AY-3-8910. A module lists
  the chips the source had usable samples for: a melody-only track opens with
  the YM2151 alone, with no K007232 pair and no PCM channels it never plays.
  The SCC and AY are tone chips, not sample players: they are written whenever
  the source keys a voice, even under `--melody-only`. A YM2203 is one system
  of six channels (FM 1-3 then PSG 1-3); a YM2612 is six FM channels, or nine
  with channel 3 split into its four operator columns. A Genesis source adds
  a second `0x03` system after it: four SN76489 channels (three tone then
  noise), for ten channels total. A dual source writes two systems in chip
  order.
- The YM2151 flag carries `customClock=` when the source clock is not
  3579545: a 4 MHz OPM plays flat otherwise. Each YM2203 system carries the
  same flag for its own clock (default 3579545). The YM2612 default is the
  Genesis clock truncated to 7670454, so a 7670453 header also gets the flag;
  this keeps the exported f-num/block identical to the source.
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

YM2151 FM instruments use type 33 (OPM) and the INS2 `FM` feature with four
operators in YM2151 register order (0x40, 0x48, 0x50, 0x58), which is also
Furnace's internal order. The `MA` feature carries FMS/AMS macros for
driver-scripted vibrato (PMS/AMS steps recorded while the note sustains).

YM2203 FM instruments use type 1 (`INS_FM`, OPN) and the same feature with
operators in OPN register order (0x30, 0x34, 0x38, 0x3c). The feature carries
the OPN sustain rate (0x70) and SSG-EG (0x90) instead of DT2. A per-note pitch
macro (code 4) on an instrument copy reproduces later f-num writes; the note's
E5 carries the key-on remainder.

YM2612 instruments use the same type 1 and OPN operator layout. They also carry the channel's PMS/AMS in the FM feature, and the carrier TLs are stored relative to the loudest carrier while the note's volume column carries the absolute level (Furnace's `newVolumeScaling` makes that exact). The DAC uses `DIV_INS_AMIGA` sample maps of 120 slots with `first_sample=0`; pattern note `60+slot` selects a release-capped PCM chunk. `collect_dac(..., slices=True)` is the explicit per-run slice mode. The SN76489 uses a plain `DIV_INS_STD` instrument (pitch macro only), a 0-15 volume column, and effect `20xy` for noise mode; its system volume defaults to 0.500, and an absolute extra-header volume entry overrides it. The FM feature's four operator columns of the Ext system share one timbre instrument.

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

The YM2151 and the per-row sweep model work in kc/kf units: 1/64 semitone.
One key-code step is 64 units, and the YM2151 KF register contributes its top
six bits (`kf >> 2`). The YM2203 and YM2612 are the exception: Furnace's OPN
platforms run in **linear pitch** mode, so a pattern note is 128 units per
semitone, E5 is 1/128 semitone, and the per-tick pitch macro uses the same
units. The note is chosen by inverting Furnace's linear note -> block/f-num
conversion; it does not use the per-row sweep model. The YM2612's
`fmFreqBase` is 9437184, twice the YM2203's, so the same note number plays an
octave above a YM2203 and the inverse uses that platform's own formula.

Two effects move pitch, and their scales differ per platform:

| Effect | YM2151 | PCM / YM2203 / YM2612 / period table |
|---|---|---|
| E5 fine tune, per byte step | 1/64 st | 1/128 st |
| 01xx/02xx slide, per parameter unit per tick | 1 kc/kf unit (1/64 st) | 1/128 st |

E8xx/E9xx quick legato adds semitones to the playing note in one write. It is
cumulative (`note += y`), applies one tick after the row, and resets at the
next note. Checked against Furnace 0.6.8.3 on all three platforms.

A held period step of about a semitone is a new note. Vibrato and pitch
slides are the instrument's **pitch macro** (code 4), one value per tick in
1/128 semitone. A waveform morph on a held note is the **wave macro** (code
3). Volume that moves during the note is the **volume macro** (code 0) with
the column at 15. A one-level saw longer than 255 ticks, when it is not one
repeating tooth, is **FAxy** on the pattern instead: `FA01` steps the column
down one level per tick, and that cell's volume is written one higher so the
same-tick slide lands on the source level. A tooth that peaks at 15 plays one
level low, because the column cannot go to 16. `FA00` holds the column. AY
noise, channel mode, envelope shape and envelope period that move during the
note are macros too (codes 2, 3, 6 and 16); the 20xx–24xx effects carry the
state at the note start. Macros are built at the module's final tick rate.
`pitch.py` ports
Furnace's period-mode `DivPitchTable` and inverts it, so a source period is
reproduced exactly up to one register step where the octave shift loses
resolution.

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
with the same `hz_speed_for_row` helper `estimate_grid` uses. `row_subdiv`
records rows per measured 16th and only feeds `Song.bpm`. A 3x condense
divides it, and a fractional result is fine. On an SCC/AY module the tick
stays one driver frame. When two notes share a cell the fit drops ticks per
row. `row_subdiv` is then the 16th's tick count over the written row's tick
count. Highlight A/B are `round(4 * row_subdiv)` and `round(16 * row_subdiv)`,
and Furnace's status-bar BPM stays the quarter-note tempo.

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

