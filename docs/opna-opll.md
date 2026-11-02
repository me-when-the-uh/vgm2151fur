# YM2608 (OPNA) and YM2413 (OPLL) support plan

Facts were read out of Furnace's sources and the reference rips on 2026-11,
not assumed. Sources cited inline; re-verify a line before trusting it after
a Furnace update.

## Reference material on this machine

- VGM spec v1.71 text: `F:\NASbackup\JamesAlan\_devspace\grbsd\vgmspec171.txt`
- vgmtools opcode reference: <https://github.com/vgmrips/vgmtools/blob/master/vgm2txt.c>
- Furnace checkout: `D:\Projects\Forks\furnace` (engine + bundled ymfm under
  `src/engine/platform/sound/ymfm/`).

## YM2608 (OPNA) - bus and VGM facts (verified in code)

Commands: `$56 aa dd` port A, `$57 aa dd` port B (spec section starting at
line 369 of vgmspec171; vgm2txt passes `Command & 0x01` to its `ym2608_write`).
Second chip instance `$A6/$A7`, gated on clock bit 30 like every YM family.
Clock header offset **0x48** (v1.71 grid). YM2612 remains 0x2C, YM3812 0x50.
Delta-T ROM data blocks are type **0x81**; ADPCMB sample RAM is loaded with
normal `$57` register writes (the reference rip embeds 0x81 blocks).

Bus decode (`ymfm_opn.cpp`, `ym2608::write_data` / `_hi`, lines 1179-1255):

| port | reg byte      | destination                      |
|------|---------------|----------------------------------|
| A    | <0x10         | SSG (same regs as YM2203)        |
| A    | 0x10          | rhythm key-on byte (see below)   |
| A    | 0x18-0x1D     | rhythm voice volume/pan          |
| A    | 0x29          | IRQ mask (ignore)                |
| A    | else >=0x20   | FM block 1 (fmb1), OPN pages     |
| B    | <=0x0F        | ADPCM-B registers                |
| B    | 0x10          | IRQ flag control (ignore)        |
| B    | >=0x11        | FM block 2 (fmb2), OPN pages     |

The FM decode is exactly the YM2612 bank bit: this repo's working `ym2612`
register flattening `(bank << 8) | reg` becomes the natural fit - fmb1 =
bank 0 regs, fmb2 = bank 1. `$56`/`$57` is the port, `bit30 of clock 0x48`
chooses `$A6/$A7` as the second chip.

Rhythm key byte (verified against `adpcm_a_engine::write`, ymfm_adpcm.cpp:345,
plus the Furnace platform emitter in `ym2608.cpp` 1071-1081): write to bus-A
reg `$10`; **bit 7 = 0 keys the set bits ON, bit 7 = 1 keys them OFF**;
bit `i` selects rhythm channel `9+i` (platform: `writeRSSOn |= 1 << (ch-9)`,
ym2608.cpp:988/992). The 6 rhythm voices and their .fur channels are fixed
ROM samples - **the converter never ships sample data for them**.

Voice names, in Furnace channel order (sysDef.cpp:1365-1370):
kick, snare, top cymbal, hihat, tom-tom, rimshot.

Channel layout of a YM2608 system (verified by reading
`DivPlatformYM2608::dispatch`, ym2608.cpp:1157-1341):

| chan | role                       | instrument type       |
|------|----------------------------|-----------------------|
| 0-5  | FM ch1-fmb2ch3             | DIV_INS_FM            |
| 6-8  | SSG (remapped to sub-AY)   | DIV_INS_AY            |
| 9-14 | rhythm voices              | DIV_INS_ADPCMA        |
| 15   | ADPCM-B                    | DIV_INS_ADPCMB/AMIGA  |

Ext mode (`DIV_SYSTEM_YM2608_EXT`) replaces FM3 with four operator columns
exactly like YM2203/2612 Ext: same `orderedOps {0,2,1,3}` twist
(`OPN_ORDERED_OPS` already in `fm.py`); fmb2 keeps three normal channels.

Sysdefs: YM2608 meta id `0x8e`, Ext `0xb7`, CSM `0xc4` (sysDef.cpp
1352/1379/1409). The `vgmOps` writer emits `$56/$57` per bus with the reg
byte = `addr & 0xff` and port byte from `addr >> 8 == 1` (vgmOps.cpp
323-337). Round-trip harness must therefore feed back bank 1 as `$57`.

Delta-T/ADPCM-B: the repo's YM2610 delta-T machinery is reusable almost
verbatim - register numbers, start/reset ($100 bit model in ym2608.cpp
dispatch ~1164-1210) match what `ym2610.py` already implements (sample
banks, `$10X` control). 4-bit IMA PCM stream + block uploads: same pattern
as the MSM6295 bank reader fixed last batch.

## YM2413 (OPLL) - verified in code

Furnace DOES model it (this was missed once via a bad grep - do not trust
the tool, use `findstr`):

- `DIV_SYSTEM_OPLL`, name "Yamaha YM2413 (OPLL)", meta id **0x89**, 9 FM
  channels, all `DIV_INS_OPLL` (sysDef.cpp:1229-1235).
- `DIV_SYSTEM_OPLL_DRUMS`, meta id **0xa7**, 11 channels: melodic 0-5,
  then kick/snare/tom/top-cymbal/hihat as noise channels
  (sysDef.cpp:1880-1906). Same bitmask idea as OPL drums already supported.
- Instrument type `DIV_INS_OPLL = 13` (`engine/instrument.h:50`) - its own
  type, reusing the FM struct's `fm.op[2]`/`fb`/`fms` plus a first-class
  **`fm.opllPreset`** byte.

OPLL bus (ymfm_opl.h "OPLL register map", lines 286-327, and the
`euress opll.vgm` stream observed writing these):

| reg      | meaning                                            |
|----------|----------------------------------------------------|
| 0x0-0x1  | user inst: mod/car AM, VIB, EG(type), KSR, MULT    |
| 0x2      | modulator KSL<<6 / TL(0-63)                        |
| 0x3      | carrier KSL<<6, rect bits, feedback                |
| 0x4-0x5  | mod/car attack<<4/decay                            |
| 0x6-0x7  | mod/car sustain<<4/release                         |
| 0xE      | rhythm: bit5 enable, bits BD SD TOM CYM HH         |
| 0xF      | test                                               |
| 10-18    | f-number low per channel                           |
| 20-28    | bit4 key on, block<<1, fnum high bit               |
| 30-38    | instrument nibble <<4 / volume nibble              |

`commitState` (opll.cpp:448-536): `opllPreset==0` writes the eight
user-instrument bytes `$05-$08/$15-$18`-equivalent (bus 0x0..0x7) before
selecting; presets >0 emit only the `$30+ch` byte. Drum voices key through
the `$0E` bitmask (bits 4..0 in channel order kick,snare,tom,top,hat -
`(0x10>>(i-6))`, and the `$0E` writes captured on `_38.vgm` are exactly
`$20/$30/$28...`).

Preset-nibble convention: on the chip, `$3X` inst value **15 selects the
user instrument**; rom presets are 0-14 (madou `_38` drives voices with
nibbles E/F = the fixed instruments; `euress opll.vgm` pokes `$36/$37=`
F?, matching "inst 15 = custom"). Confirm against the companion file
`D:\Downloads\euress opll.fur` - it is a hand-built Furnace module for the
same VGM; its instrument `opllPreset` bytes are authoritative.

Note Furnace's melodic OPLL system has no drums; a rip that toggles `$0E`
needs `DIV_SYSTEM_OPLL_DRUMS`.

## Multi-system .fur facts (all verified)

- `.fur` stores a machine as `sysCount u16`, then per system one **chip-ID
  byte** from the arcade/meta table (0x8e OPNA etc.) - not the DIV_SYSTEM
  enum. `furio.py`'s machine block already parses arbitrary counts; extend
  the `CHIP_*` table with 0x8e/0xb7 (and 0xa7-style OPLL ids when used).
- Every channel carries its own `DIV_CH_*` type + instrument-type byte, so a
  YM2612 system and an OPL2 system can coexist in one machine - nothing in
  the format stops it. (The user's hunch, confirmed.)
- `analyze` already tags writes per `(chip, chip_id)`; the `$A6/$A7` and
  dual-clock plumbing for a *second OPNA* follows the YM2612 precedent
  already in `vgm.py` lines ~1083-1140.

## z66 merged file (YM2612 + YM3812, DAC stream)

`z66_pc-fm_trimmed_merged_...vgm`: VGM 1.60; clocks: YM3812=3579545 only -
the header has **no YM2612 clock**, yet the stream carries 10 YM2612 port
writes (all init: $B4-$B6 mute, $2A/$2B DAC), a 1.19 MB type-0x00 data
block, and $90-$93 DAC *stream control* (chip type $02=YM2612, 11025 Hz),
plus ~14k YM3812 writes. `chips_in_head` currently reports both because of
`$52/$53`, and `load_vgm` already warns "PCM data block present but no DAC
play stream" - wrong: the stream IS defined, via $90-$93; that message's
detection is stale. Work needed: honor DAC stream control (data block +
frequency), emit the DAC as Furnace PCM/wave samples, and make sure OPL2
routing doesn't swallow the YM2612 side when both are present.

## Verification harnesses to reuse

- `tools/fur2vgm.py` (headless Furnace export) + `tools/vgmdiff.py`
  (per-register run diffs), driven by the cached loop in dbg_rt-style probes.
- Env var `VGM2151FUR_FURNACE=D:\Projects\Forks\vgm2151fur\furnace\furnace.exe`
  (Furnace-gated tests silently skip without it).

## Build order

1. `vgm.py`: `$56/$57/A6/A7` -> writes `ym2608`/`ym2608_ssg`, clock 0x48 +
   dual, block 0x81; `$51` -> `ym2413` writes (clock 0x10 + dual bit via
   cmd $A0-range? spec: YM-family second chip = 0xA1 for `$51` - confirm in
   vgm2txt `case 0xA1`).
2. `analyze.py`: OPNA path (two-bus OPN state = reuse ym2612 flattening);
   rhythm + delta-T; then OPLL path (presets + user bank + drums).
3. `furwrite.py`/`furio.py`: system ids 0x8e (YM2608), ext 0xb7; OPLL
   13-type instruments both systems.
4. z66: DAC $90-$95 stream import + multi-FM-machine routing fixes.
5. Tests per feature (synthetic + the three real files), then corpus sweep:
   `gs-opna\done` (many YM2608) and `mado123-opll` (ym2413 drums).
