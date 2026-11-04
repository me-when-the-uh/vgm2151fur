# OPL family (YM3526 / YM3812 / YMF262) notes

The OPL chips are the two-operator FM cores that became the AdLib and Sound
Blaster. Furnace implements three systems for them plus a rhythm variant of each:

| Chip | Furnace system | id | channels | with rhythm |
| --- | --- | --- | --- | --- |
| Yamaha YM3526 (OPL) | `OPL` | `0x8f` | 9 | `0xa2`, 11 (6 FM + kit) |
| Yamaha YM3812 (OPL2) | `OPL2` | `0x90` | 9 | `0xa3`, 11 (6 FM + kit) |
| Yamaha YMF262 (OPL3) | `OPL3` | `0x91` | 18 | `0xa4`, 20 (15 FM + kit) |

Rhythm mode is a **separate system**, not a flag on the melodic one: bank A's
last FM channels become the kit, so a track that logs `0xBD` percussion is
converted to the drums system and gets its own columns. Four-operator voices are
not a separate system - they are two hardware channels of one `OPL3` pair driven
as a single voice (see below).

## VGM stream

| What | Where |
| --- | --- |
| YM3812 clock | header `0x50`; bit 30 = a second chip, bit 31 = that pair is hard left/right |
| YM3526 clock | header `0x54` |
| YMF262 clock | header `0x5C`; bit 30 marks a second chip (a YM3812 answering `$AA` is *its* chip; on an OPL3 the second bank is inside one chip) |
| YM3526 writes | command `0x5B` (reg, val) |
| YM3812 writes | command `0x5A`; a second chip uses `0xAA` |
| YMF262 writes | command `0x5E` (port 0) and `0x5F` (port 1) |
| Key on/off | register `0xB0`-`0xB8` bit 5 (per bank on an OPL3) |
| Rhythm mode and triggers | `0xBD`: bit 5 selects rhythm, bits 4-0 latch BD/SD/TM/TP/HH |
| AM / vibrato depth | `0xBD` bits 7 and 6 (OPL only: bit 7 is AM depth, there is no vibrato-depth bit) |
| Four-operator pairs | `0x104` bits 0-5 (bank B port, register `0x04`) |
| Stereo per channel | `0xC0`-`0xC8` / `0x1C0`-`0x1C8` bits 4 and 5 |
| Waveform select | `0xE0`+ (OPL2 and OPL3; the YM3526 has no waveform register) |

The loader keeps an OPL3's two banks in one flat register index: a port-1 write
arrives as `0x100 + register`, the same convention the YM2612's second bank
uses. A `0xAA` write is only accepted when header bit 30 says a second YM3812
exists - otherwise the bytes are dropped and reported, because writing to a chip
that is not there would silently invent a stereo pair.

A log that mixes an OPL family with an arcade FM chip is converted on the side
with more rising key-ons, and the report names both counts and the dropped
side. A log mixing OPL2 and OPL3 picks the kind with more key-ons and warns.
Two crystals of the same core still share one system and the header clock.
Chips whose clock field is missing fall back to the family standard
(3579545 Hz, or 14318180 Hz for a YMF262) with a warning; the `.fur` then
carries `customClock` only when the rip's crystal really differs from Furnace's
default for that system (`COLOR_NTSC`, or `COLOR_NTSC * 4` - within about 2 Hz,
since a rounded header value still leaves the pitch identical).

## Channel layout

An OPL or OPL2 uses nine channels, `FM 1` to `FM 9`. In rhythm mode the last
three become percussion and two more single-operator voices join them, so a kit
module shows eleven:

```
0-5   FM 1-6
6     Kick/FM 7    (BD - still a two-operator voice)
7     Snare        (SD)
8     Tom          (TM)
9     Top Cymbal   (TP)
10    Hi-hat       (HH)
```

Which chip register each kit column comes from, and what it shares:

| Column | Chip channel (`0xBn`) | Operators | Frequency register |
| --- | --- | --- | --- |
| 6 Kick/BD | 7 (`0xB6`) | `0x10` + `0x13` - both of them | `A6/B6`, to itself |
| 7 Snare/SD | 8 (`0xB7`) | `0x14` (modulated) | `A7/B7`, shared with HH |
| 10 Hi-hat/HH | 8 (`0xB7`) | `0x11` (fixed) | `A7/B7`, shared with SD |
| 8 Tom/TM | 9 (`0xB8`) | `0x12` (fixed) | `A8/B8`, shared with TP |
| 9 Top/TP | 9 (`0xB8`) | `0x15` (fixed) | `A8/B8`, shared with TM |

So the bass drum is not a bit competing for someone else's pitch: it owns channel 7
and both of its operators, which is why Furnace lets that column take an ordinary
`DIV_INS_OPL` instrument. Only the pairs inside channels 8 and 9 share a
frequency, which is why their pitch is not compared in the round trip below.

Snare and hi-hat share frequency register `0xA7`/`0xB7`, tom and cymbal share
`0xA8`/`0xB8` - exactly as the chip does. A kit column therefore cannot own a
pitch: whichever of the pair was written last is what a register dump shows.

An OPL3 doubles the channel count and permutes it, because its four-operator
voices pair a bank-A channel with the one three steps later. Furnace's table
interleaves each pair into adjacent columns so a voice is one contiguous block:

```
0-5    channels 0,3 / 1,4 / 2,5 (bank A)   - three pairs
6-11   channels 0,3 / 1,4 / 2,5 (bank B)
12-14  bank B channels 6,7,8                - no partner
15-17  bank A channels 6,7,8
```

`0x104`'s six bits select the pairs in that same order: bit 0 is columns 0 and 1,
bit 2 columns 4 and 5, bit 3 the first bank-B pair. A voice whose pair is
enabled takes two operators from each hardware channel (addresses `0x?0`,
`0x?3`, `0x?8`, `0x?B`) and is keyed on the even column; its partner channel
stays silent, so the odd column's key bit is ignored. The connection field of a
four-operator voice also splits across the pair - Furnace writes `alg & 1` to the
head's `0xCn` and `(alg >> 1) & 1` to the partner's - so the analyzer reads both
to rebuild one algorithm, and only an even column may head a voice.

## Pitch model

Furnace drives the OPL from `DivPitchTable` in **frequency** mode. For an
`OPL2` the base is `32768 * 72`, for an `OPL3` it is `32768 * 288`; with
`COLOR_NTSC` and `COLOR_NTSC * 4` respectively the two give the same phase step
for the same note, which is why a YMF262 board and an AdLib card agree on pitch.
The table holds one octave (13 entries) plus per-semitone differences; the note's
octave shifts a value right, rounding the carry in:

```
unit = table[note % 12] + ((diff[note % 12] * (offset & 127)) >> 7)   after shift
```

and `DivPlatformOPL::toFreq` splits it into block and f-num: the block is
`(unit / 686).bit_length()` when that quotient is non-zero (Furnace's `bsr`
counts bits and names one higher than the highest set bit), and f-num is what
is left after shifting. Two consequences that matter to a conversion:

- Only the product `fnum << block` sets the frequency. A rip may hold a register
  in another split - `mc_01.vgm` logs `(3,256)` where the table produces
  `(2,512)` - and both sound the same. The key-scale rate and thus the envelope
  may still differ, since keycode uses the block bits; a split cannot be undone
  in a tracker, so it is reported rather than "fixed".
- In linear pitch mode (what this project writes) `SharedChannel::calcFreq`'s
  multiplier never reaches the table: one `E5xx` unit is 1/128 semitone and it
  joins the table offset before block and f-num are split, exactly like an
  instrument's `pitch2` macro. Measured exhaustively at both defaults, of the
  8166 registers reachable in the note range 3402 come back as the same
  `(block, f-num)` pair, 3555 as the same phase step in another split and 1209
  land on the nearest cell. The worst error per octave is under three cents from
  C3 up (`note 72`), a fifth of a semitone at the bottom of the ranges a real
  driver uses, and up to 0.81 semitones below `note 24`, where the chip's own
  f-num granularity is coarser than a note.

Notes are labelled at the loudest carrier's octave when its multiplier is an
exact power of two, with a constant `pitch2` macro keeping the f-num, and so the
audio, where the rip had it (`docs/ym2203.md` describes the same device for the
OPN family).

## Instruments and levels

An OPL voice becomes a `DIV_INS_OPL` (type 14) instrument, or `DIV_INS_OPL_DRUMS`
(type 32) for a single-operator percussion voice. Eight bytes per operator, in
the order Furnace reads them:

| Byte | Field |
| --- | --- |
| 0 | `(ksr << 7) \| multiplier` - the `0x2n` register minus am/vib/sustain |
| 1 | `(sus << 7) \| (level + volume move)` - the `0x4n` register |
| 2 | `(vib << 5) \| attack rate` - the `0x6n` register |
| 3 | `(am << 7) \| (ksl << 5) \| decay rate` - the `0x8n` register |
| 4 | key-scale / output flag (`kvs << 5`) |
| 5 | `(sustain level << 4) \| release rate` - the `0xBn` register |
| 6 | reserved: no SSG envelope and no deep vibrato on an OPL |
| 7 | waveform select - the `0 En` register, absent on a YM3526 |

A carrier is marked with `kvs`, which is what makes Furnace apply the volume
column to it. The column's maximum is 63, not 127: an operator level is six bits.
The carrier's own logged attenuation normalizes into the volume column, so a patch
keeps the *relative* levels of its operators and every note opens where the rip
had it; two notes of one patch with different carrier levels share the instrument.
A kit cannot do this - its four single-operator columns read one shared
instrument - so a percussion note moves the column directly instead.

Per-operator am, vibrato and sustain stay in the instrument. The chip-wide depths
(`0xBD` bits 7 and 6) are global state, so they become chip effects on the row
where the log wrote them: `0x10` for amplitude-modulation depth and `0x17` for
vibrato depth.

## Operator levels: row effects and macros

Tyrian's engine scripts the **modulator** output level (`$4n`) hundreds of times
per channel while a note holds. On `14 Gyges, Will You Please Help Me` the four
channels sharing one voice each take ~1,750 mid-note `$4n` writes with the carrier
almost still; on `21 Come Back to Me, Savara` channels 3-5 take 3,021 / 3,191 /
4,566 against two carrier writes. That register *is* the modulation index. A
frozen one sounds like brightness that never evolves, and it hides from any
attack-time check, because every byte written at key-on was correct.

The volume column cannot carry it: `opl.cpp` scales only an operator that is both
marked KVS and an output (its `KVSL` macro). A modulator has no path at all. Two
routes exist, and the converter picks by shape:

- A level that differs **once per note** (the common MoonDriver case) becomes the
  platform's own operator-level effect: `12xx`..`15xx` (`DIV_CMD_FM_TL`, operator
  1-4, value `00`-`3F`, 0 loudest). The shared instrument keeps the loudest
  modulator level (`shape_key`/`loudest`), and each quieter note writes an effect
  on its row. No instrument copy is needed. `06 TIME'S UP!` uses this for the
  `$5n` wave levels too, and `05 encounter the unknown environment` reduces from
  89 to **6 FM instruments** (plus 22 wave bodies) with 2,586 `12xx` effects.
- A **fast intra-note ramp** puts several levels inside one row, where only one
  effect per operator can sound. Those notes keep the per-operator macro on a
  `+lvl` copy. Each operator has an `O1`..`O4` feature and code 6
  (`DIV_MACRO_OP_TL`) is applied as `op.tl = val & 63`; for a non-KVS operator the
  register write is `op.tl | op.ksl << 6`. There is one measured surprise:
  pushing a ramp `3, 7, 11 ... 62` through the player came back as
  `$4n = 60, 56, 52 ... 1`. **The macro stores a level, not an attenuation**: up
  is louder. The writer inverts (`furwrite._write_op_ma`). Everything above it,
  including `analyze`, keeps speaking `$4n`.

The other operator codes live in `DivMacroTypeOp` - 0 AM, 1 AR, 2 DR, 3 MULT,
4 RR, 5 SL, 6 TL, and 15 KSL, 16 SUS, 17 VIB, 18 WS, 19 KSR. Today only TL is
driven. An operator **envelope or waveform** written inside a held note still
applies at the next key-on, since Furnace rebuilds the voice from its instrument
when the note starts; those writes are counted and reported ("N operator envelope
or waveform write(s) landed inside a held note"), as is an `OPL3` mid-note
waveform change (`2Axy`). Each of those codes needs its own measured polarity and
mask before it can be trusted. The TL inversion is exactly that warning.

`analyze._collect_opl` records `$4n` per operator while a voice sounds
(`NoteEvent.opl_op_tl`, primed from the instrument at key-on with the attack
level), and `analyze._bind_op_macros` decides between the row effect and the
macro. An operator macro has **no delay**: it starts with the note, and Furnace
ignores the field on these (measured: an entry list written with `delay=5` still
stepped from tick zero). A level that first moves several ticks in repeats its
starting entry across that hold, and the sweep's resolution pays for those
entries. Speed is the smallest that fits a macro's 255 entries - 1 to 5 ticks
(2-10 ms) on these songs.

Re-exported through the bundled console build, the macro path still reproduces
the dense Tyrian ramps (`tests/test_opl_operator_macro.py` pins the capture, the
inverted file encoding, the all-levels playback, and the per-note `12xx` effect),
while sparse modulator changes survive exactly at the note row.

## Attack order: comparing two streams

A driver that re-attacks quickly writes a release, the new voice's operator bytes
and the key bit in **one sample**. The chip applies them in stream order; a
tracker row holds one value. So the state that matters - and the state the
analyzer snapshots when it opens a note, see `analyze._collect_opl`'s `ordered()`
- is the register set once that sample has been applied in full. The same rule
makes the converter defer key bits to the end of a same-sample group, exactly as
the YM2612 path had to. `diag.opl_attacks()` implements the comparison and is
what the reference-rip tests measure with.

Measured on the rips this model was tuned on (Furnace-converted and re-exported
through the bundled console build, every attack's whole register set):

| Rip | Chip layout | attacks | four-op keys | differing bytes |
| --- | --- | --- | --- | --- |
| `solar1_BH_MU1_trimmed.vgm` | YM3812 + rhythm | 1551 | - | 0 |
| `mc_01.vgm` (Miwaku) | YMF262, 226 patches | 2974 | 1003 | 0 |
| `main_010a.vgm` (Metal Ace) | YMF262 | 5434 | - | 0 |
| `KXEMI1_opl2.vgm` (Knights of Xentar) | 2 x YM3812, hard panned | 22100 | - | 0 |
| `opl1_rhythmtest.vgm` | YM3526 + rhythm | 50 | - | 0 |

Kit columns are compared for their operator bytes; their frequency is not, since
two percussion voices share one register - in the Solar Winds log a snare attack
catches `0xA7` two f-num steps (three cents) away from where the same note's
hi-hat write left it, and that belongs to the other voice.

## Verification

`--verify` measures pitch divergence per channel inside each voice's own key,
trimmed 35 ms at both ends so a row-tick note and the exact sample the driver
used are not counted against each other (`diag.pitch_runs`):

- A kit column reports nothing: its frequency register is another voice's - except
  the kick, whose channel 7 pair it owns outright.
- Divergence counts the row grid as well as pitch, so a few seconds over a
  multi-channel minute is normal - the same measure on a YM2612 rip from this
  collection reads 11948 ms. On these rips: Solar Winds 0 ms, Metal Ace 8988 ms,
  Knights of Xentar 2979 ms, Miwaku mc_01 16539 ms, Dungeon Theme 11570 ms.

Mid-note pitch follows the frequency register's **current** value, not the value
the note started on. Drivers write vibrato by hand - `05 Dungeon Theme [Level 1]`
alternates channel 8's and 9's `0xA7`/`0xA8` between two f-numbers every 150 ms
under a slow attack - and comparing against the note's starting pitch drops every
return leg, leaving one-sided motion that never comes back. The same reason the
level path tracks `tl_last`. Fixing it took that pad from 2 and 3 captured points
per note to 7 and 8, and lowered this rip's own divergence measure.
- Key-on counts come from the same lattice the grid estimate uses, so a lost note
  shows up as `miss` instead of hiding in the pitch measure.

The regression net is `tests/test_vgm2151fur_opl.py`: the VGM commands and header
bits, Furnace's channel and slot tables including the OPL3 permutation, the pitch
model (a full sweep of every reachable register), hand-written vibrato keeping
both its legs, rhythm mode's shared registers on all three chips, the `.fur`
emission of both instrument types, and round trips through Furnace for synthetic
voices and the five rips above - including `opl1_rhythmtest.vgm`, which is what
proves the YM3526 takes rhythm mode exactly like a YM3812.

## Yamaha Y8950

The Y8950 is an OPL2-shaped FM chip (`KIND_Y8950`, waveform select and rhythm
included) with one delta-T (ADPCM-B) voice bolted on. VGM carries it on
command `0x5C` with the header clock at `0x58`, and its ROM arrives as data
block `0x88`.

The FM half runs through this same collector, so instruments, pitch, rhythm
mode and the four-operator tables are the OPL2 ones. The ADPCM half is
`y8950.py`: registers `$07` (control: bit 7 execute, bit 4 repeat, bit 0
reset), `$09`/`$0A` start, `$0B`/`$0C` end (32-byte units), `$10`/`$11` pitch
and `$12` level, decoded into the same `AdpcmBSample` the YM2608 uses.

Furnace gives it system `0xB2` (9 FM + 1 ADPCM = 10 channels) or `0xB3` with
the drums layout (11 FM + 1 ADPCM = 12). The kit is chosen only when a `$BD`
latch actually fires. Verified on Ginga NinkyouDen's `03v`, which uses rhythm
mode: 11 FM columns, the ADPCM voice on column 12, both ADPCM waves
(0xBAE0 and 0x1CAE0) sliced at the chip's 32-byte granularity.

## Yamaha YMF278B (OPL4)

The YMF278B is an OPL3-shaped FM half plus 24 wave (PCM) slots. VGM carries it
on the four-byte command `0xD0 pp aa dd`: port `pp = 0`/`1` is the FM register
space of the two banks, and port `pp = 2` is the wave register space. The clock
lives at header `0x60` (usually 33868800), ROM images arrive as data block
`0x84` and RAM images as `0x87`. The parser folds the port into bit 8 of
`ChipWrite.reg`, and the FM collector skips port 2; otherwise a wave pitch byte
such as `0xB5 = 0x20` would be read as an FM key-on.

The FM half runs through this same OPL3 path with `KIND_OPL4`, but the pitch
table is its own: `DivPlatformOPL::setOPLType` overrides the OPL3 base with
`32768 * 684` for `chipType = 4` (`opl.cpp:3390`). Using the OPL3 base
(`32768 * 288`) at the OPL4 clock puts every FM note about 1.25 octaves flat, so
`opl.pitch_table` carries an optional `freq_base` and `analyze` passes the OPL4
value through `_collect_opl`. Furnace gives the FM half 18 columns (system
`0xAE`, 42 channels with the 24 wave slots) or 15 + 5 kit columns with rhythm
(system `0xAF`, 44). Rhythm follows the OPL3 rule: port 1's `0x1BD`.

### Wave slots

Each slot has ten registers at `0x08 + slot + n*24`:

| n | register |
|---|----------|
| 0 | sample ID low |
| 1 | f-num LSB + sample ID bit 8 |
| 2 | f-num MSB + octave (signed 4-bit, `-8` freezes) |
| 3 | total level (7-bit attenuation) |
| 4 | key/damp/LFO/pan |
| 5-9 | LFO speed, AR/D1R, D1L/D2R, RC/RR, AM |

The sample ID selects a 12-byte descriptor in the wave memory. IDs below 384
read the YRW801 ROM at `id * 12`; the rest read the card RAM at
`(id - 384) * 12`. A descriptor is:

| byte | meaning |
|------|---------|
| 0 | bits 7-6 format (0 = 8-bit signed, 1 = 12-bit, 2 = 16-bit big-endian), bits 5-0 start address bits 16-21 |
| 1-2 | start address bits 8-15, 0-7 |
| 3-4 | loop address (`high << 8 \\| low`, in frames) |
| 5-6 | end address, a *negative* length: frames = `0x10000 - end` |
| 7-11 | envelope/LFO defaults (not converted) |

Playback rate is `clock * step / (768 * 65536)` with
`step = (fnum + 1024) << (5 + octave)`. The converter extracts each distinct
sample body into a `.fur` sample (signed 16-bit), one `DIV_INS_MULTIPCM`
instrument per body, and writes the pattern note as the pitch ratio to the
body's most common step. The body's own C-4 rate is that step's rate. A
descriptor whose loop point sits before the last two frames loops there. A loop
point in the last two frames is the descriptor's padding: the reference modules
trim the body to the loop point and play it once. A body of exactly `0x10000`
frames wraps the slot's 16-bit position counter, so its loop address cannot
take effect; the three 64 KB RAM bodies in "06 TIME'S UP!" are one-shot too.

A TL write (`$50 + slot`) while a slot sounds becomes a volume step on the
pattern rows (the writer's `vol_pts` path), and a key-off (bit 7 of `$68 + slot`
falling) becomes an explicit note-off row. Both are carried per hit, so a drum
sample stops when the log stops it and a fade is no longer frozen at its
attack level.

Some drivers (spc2vgm is one) key a voice at `TL=0x7F` and pan 8, then write
the real level and pan one tick later. At row resolution that is still the
attack, so a write inside `OPL4_ATTACK_GAP` (128 samples) replaces the hit's
opening TL when the key-on level is silent, and the opening pan when the key-on
pan is 8. Without this every such note started at volume 0 and was muted.
An audible key-on keeps its own level; a later TL is a real decay step.

Pan and f-num writes on a held slot are kept too. A pan change becomes a
`08xy` point next to the volume stream (pan 8 has no effect form and rides as a
volume-0 point), and an f-num/octave change becomes a pitch bend: 1/64-semitone
row steps emitted as `E8xx`/`E9xx` plus `E5xx`, the same encoding ADPCM-B uses.
Before this a held OPL4 sample only ever played its key-on pitch.

A trailing `TL=0x7F` (silence) is dropped when no later write raises the level.
The following key-off already stops the note; keeping the step only produced a
volume-00 row that the source player never sounded, and row rounding could push
it past the next key-on and mute a fresh note.

The hardware pan table is 0 = centre, 1-7 = right (left falls), 8 = both off,
9-15 = left (right falls). Furnace's PCM path derives the slot nibble from the
`08xy` effect as `8 ^ min(convertPanSplitToLinearLR(x, y, 15) + 1, 15)`
(`opl.cpp:2267`), which is not a naive gain split: pan 0 is centre, not hard
left, and the sides are reversed. `ymf278b.opl4_pan_fx` inverts that formula so
the exported slot keeps the source nibble; pan 8 has no `08xy` form and is
muted through the volume instead. Treating pan 0 as hard left is what made the
drum slots sound wrong.

Furnace does not load the YRW801 ROM itself, so the ROM bodies are extracted
and embedded. The ROM is looked up in this order: an explicit path,
`VGM2151FUR_YRW801`, `<repo>/furnace/yrw801.rom`, `<cwd>/furnace/yrw801.rom`,
then `<cwd>/yrw801.rom`; a `.zip` containing a 2 MB `.rom` is accepted. Without
it, an OPL4 log that addresses ROM sample IDs loses those notes and the
converter prints a warning naming the variable. Logs that only use RAM sample
IDs need no ROM.

Instrument sharing matters on this chip. A Furnace module addresses
instruments with one byte (at most 256). MoonDriver ramps the modulator's
`$4n` per note (22, 23, 24... on `$40`-`$42`) to fade the FM brightness, which
would otherwise make one instrument per step: the two minutes of "05 encounter
the unknown environment" needed 1106. `_collect_opl` folds the modulator level
out of the patch key (`shape_key`) and keeps the loudest level seen for the
shared shape (`loudest`); the per-note levels then travel as `12xx` effects on
the note rows (see "Operator levels" above). Only a fast intra-note ramp, which
would need several levels inside one row, keeps an operator-TL macro copy.
Base patches are capped at 200, with same-shape reuse and an `ins_reused`
warning past that. Wave bodies add one instrument each. An OPL4 with no FM
notes keeps the empty FM placeholder at instrument 0, so the wave instruments
are 1-based: `0x01` is wave 017, `0x18` is wave 384 and `0x19` is wave 385.
The sample index follows the placeholder, so each PCM column points at exactly
the sample the log loaded. (An older build emitted the placeholder without
reserving its instrument slot, so every PCM column played the previous sample:
"06 TIME'S UP!"'s Ch 0, which loads sample `0x181`, played the `0x180` RAM
sample.)

Verified against the two MoonDriver rips: "05 encounter the unknown
environment" (6 FM shapes + 22 YRW801 bodies = 28 `INS2`, 10641 FM key-ons,
7810 wave key-ons, 2586 `12xx` level effects) and "06 TIME'S UP!" (one empty FM
instrument + 26 bodies = 27 `INS2`, 7538 wave key-ons, 7530 note-offs and 80
mid-note volume steps). Both export through the bundled Furnace build, and the
FM key-on pitch, the wave pan nibble and the wave TL at key-on match the source
exactly.

## Limits

- Waveform and envelope writes inside a held note move at the next key-on. A
  modulator *level* no longer waits: a sparse change is a `12xx`-`15xx` row
  effect, and a fast ramp keeps that operator's `TL` macro. The remaining cases
  are `$6n`, `$8n`, `$2n` and `$En` moves.
- Below `note 24` one table step is wider than a half tone, so an unplayably
  deep register lands on the nearest cell; `opl_note_for` clamps into the note
  range instead of raising.
- A block/f-num split a rip chose may differ from the engine's own; pitch is
  unaffected, keycode-based envelope attack might be.
- Two voices on one column inside a single row collapse to the later one, and two
  percussion hits in one sample of a shared register keep the last byte written.
- `YM2608` and `YM2413`/OPLL have their own docs. The Y8950 is this OPL path
  with `KIND_Y8950` (OPL2-shaped FM) plus one ADPCM column, and the YMF278B's
  FM half will use the same OPL3 path. An OPL log whose chip family cannot be
  told from the header alone is still not converted.
