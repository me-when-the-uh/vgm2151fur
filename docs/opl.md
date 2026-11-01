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

A log that mixes families (`$5A` and `$5E`, or two different crystals for the
same core) is converted on the family that plays most of it and the report says
so. Chips whose clock field is missing fall back to the family standard
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
counts bits, so it names one higher than the highest set bit), and f-num is what
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

An operator envelope or waveform written *inside* a held note is applied at the
next key-on, not mid-note: Furnace rebuilds an OPL voice from its instrument when
the note starts. Those writes are counted and reported ("N operator envelope or
waveform write(s) landed inside a held note") rather than dropped silently. An
`OPL3` waveform change mid-note (`2Axy`) is not expressed either, since a tracker
row cannot retarget one operator of a voice that is sounding.

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

- A kit column reports nothing: its frequency register is another voice's.
- Divergence counts the row grid as well as pitch, so a few seconds over a
  multi-channel minute is normal - the same measure on a YM2612 rip from this
  collection reads 11948 ms. On these rips: Solar Winds 0 ms, Metal Ace 8988 ms,
  Knights of Xentar 2979 ms, Miwaku mc_01 16539 ms.
- Key-on counts come from the same lattice the grid estimate uses, so a lost note
  shows up as `miss` instead of hiding in the pitch measure.

The regression net is `tests/test_vgm2151fur_opl.py`: the VGM commands and header
bits, Furnace's channel and slot tables including the OPL3 permutation, the pitch
model (a full sweep of every reachable register), rhythm mode's shared registers,
the `.fur` emission of both instrument types, and round trips through Furnace for
synthetic voices and the four rips above.

## Limits

- Waveform and envelope writes inside a held note move at the next key-on.
- Below `note 24` one table step is wider than a half tone, so an unplayably
  deep register lands on the nearest cell; `opl_note_for` clamps into the note
  range instead of raising.
- A block/f-num split a rip chose may differ from the engine's own; pitch is
  unaffected, keycode-based envelope attack might be.
- Two voices on one column inside a single row collapse to the later one, and two
  percussion hits in one sample of a shared register keep the last byte written.
- `YM2608`, `YM2413`/OPLL (fixed patches) and `YMF278b` are not converted, and
  neither is an OPL log whose chip family cannot be told from the header alone.
