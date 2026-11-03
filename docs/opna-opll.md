# YM2608 (OPNA) and YM2413 (OPLL) support plan

Facts were read out of Furnace's sources and the reference rips on 2026-11,
not assumed. Sources cited inline; re-verify a line before trusting it after
a Furnace update.

## Reference material on this machine

- VGM spec v1.71 text: `F:\NASbackup\JamesAlan\_devspace\grbsd\vgmspec171.txt`
- vgmtools opcode reference:
  <https://github.com/vgmrips/vgmtools/blob/master/vgm2txt.c>
- Furnace checkout: `D:\Projects\Forks\furnace` (engine + bundled ymfm under
  `src/engine/platform/sound/ymfm/`).

## YM2608 (OPNA) - ports and VGM facts (verified in code)

Commands: `$56 aa dd` port 0, `$57 aa dd` port 1 (spec section starting at
line 369 of vgmspec171; vgm2txt passes `Command & 0x01` to its `ym2608_write`).
Second chip instance `$A6/$A7`, gated on clock bit 30 like every YM family.
Clock header offset **0x48** (v1.71 grid). YM2612 remains 0x2C, YM3812 0x50.
Delta-T ROM data blocks are type **0x81**; ADPCMB sample RAM is loaded with
normal `$57` register writes (the reference rip embeds 0x81 blocks).

Port decode (`ymfm_opn.cpp`, `ym2608::write_data` / `_hi`, lines
1179-1255). The spec calls the two address ports "port 0" and "port 1"
(vgmspec171.txt:369-370) and so does this repo: in the flattened register
number, bit 8 *is* the port.

| port | reg byte      | destination                      |
|------|---------------|----------------------------------|
| 0    | <0x10         | SSG (same regs as YM2203)        |
| 0    | 0x10          | rhythm key-on byte (see below)   |
| 0    | 0x18-0x1D     | rhythm voice volume/pan          |
| 0    | 0x29          | IRQ mask (ignore)                |
| 0    | else >=0x20   | FM block 1 (fmb1), OPN pages     |
| 1    | <=0x0F        | ADPCM-B registers                |
| 1    | 0x10          | IRQ flag control (ignore)        |
| 1    | >=0x11        | FM block 2 (fmb2), OPN pages     |

The flattening is the YM2612's own, so `ym2608` writes reuse it: block 1
is `(0 << 8) | reg`, block 2 `(1 << 8) | reg`. The spec calls the header
clock's "typical value" 8000000; a real FM Towns rip carries 7987200, which
is one of Furnace's named selections rather than a custom clock.

Rhythm key byte (verified against `adpcm_a_engine::write`,
ymfm_adpcm.cpp:345, plus the Furnace platform emitter in `ym2608.cpp`
1071-1081): write to port 0 register `$10`; **bit 7 = 0 keys the set bits ON,
bit 7 = 1 keys them OFF**;
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

Ext mode (`DIV_SYSTEM_YM2608_EXT`) replaces FM3 with four operator
columns exactly like YM2203/2612 Ext: the same `orderedOps {0,2,1,3}` twist
(`OPN_ORDERED_OPS` in `fm.py`). The second block does *not* keep channels
3-5: it moves to **6, 7, 8**, because the four columns took their slots. Not
a convention - `DivPlatformYM2608Ext::dispatch` hands every channel above 5
to the base platform as `chan - 3`, so a bass note written to column 3 would
key an operator of the split channel instead. See "Ext splits channel 3 in
place" below.

Sysdefs: YM2608 meta id `0x8e`, Ext `0xb7`, CSM `0xc4` (sysDef.cpp
1352/1379/1409). The `vgmOps` writer emits `$56/$57` per port with the reg
byte = `addr & 0xff` and port byte from `addr >> 8 == 1` (vgmOps.cpp
323-337). Round-trip harness must therefore feed back port 1 as `$57`.

Delta-T/ADPCM-B: the YM2610's machinery carries over almost verbatim -
register numbers and the start/reset model in ym2608.cpp's dispatch
(~1164-1210) match what `ym2610.py` already implements. Two things do not
carry over: which port it lives on, and how a body gets cut. Both are under
"Delta-T: cut at 32, store at 256" below.

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

OPLL register map (ymfm_opl.h lines 286-327, and the `euress opll.vgm`
stream observed writing these):

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
user-instrument bytes `$05-$08/$15-$18`-equivalent (registers 0x0..0x7) before
selecting; presets >0 emit only the `$30+ch` byte. Drum voices key through
the `$0E` bitmask (bits 4..0 in channel order kick,snare,tom,top,hat -
`(0x10>>(i-6))`, and the `$0E` writes captured on `_38.vgm` are exactly
`$20/$30/$28...`).

Preset-nibble convention (corrected 2026-11 after reading the platform, which
is what decides playback - an earlier note here had it backwards): on the
YM2413
the instrument nibble in `$30+ch` selects **ROM voice 1-15** and **0 is the
user instrument**, which lives in `$00-$07`. `DivPlatformOPLL::commitState`
writes the eight user bytes when the nibble is 0 and only `$30+ch` otherwise,
so a module's `opllPreset` 0 means "custom bank". The rip agrees: madou `_38`
and `euress opll.vgm` both key their custom voices with nibble 0.

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

## Empirical OPLL .fur layout (from `D:\Downloads\euress opll.fur`, verified
2026-11: Furnace re-exports it **byte-exact** to the source VGM via
`tools/fur2vgm.py`, 0 diff runs, equal loop and totals)

- `INS2` record: **u16** fur version, then u8 type byte. The OPLL voices in
  the reference are stored with the FM "FM" feature at **36 bytes = opCount
  4 slots x 8 + four header bytes** (Furnace keeps four op rows; the engine
  custom path reads only op[0]/op[1]). Preset lives in FM byte3 bits 0-4
  (`(fms<<7)|(ams<<6)|(four<<5)|preset`; opllPreset==0 means custom bank).
  The earlier "version=140 type=0" note here was a mis-offset read of the
  u16 - corrected; re-dump if it matters. Verify every byte against the
  Furnace-authored reference module before trusting any of this.
- LD feature seen on these instruments: `00 20 05 50 05 c0 01` - drum fixed
  frequencies (kickFreq/snareHatFreq/tomTopFreq) stored 16-bit; irrelevant
  for melodic OPLL but must be reproduced when a module has drums.
- The author's module is 1 chip `0x89`; `n_ch` in INFO was 1 channel while
  the system offers 9 - our furio CHIP_CHANNELS needs `{0x89: 9, 0xa7: 11}`.

## Furnace internal facts confirmed by the platform map (2026-11)

- Rhythm ROM is compiled into Furnace (`platform/sound/rss.h`, 8 KiB
  `YM2608_ADPCM_ROM`), with per-voice start/end hardcoded for the ymfm
  core (`ymfm_opn.cpp:1005-1011`); LLE keeps a nibble-swapped copy.
  => the converter ships no rhythm samples, exactly as djtub said.
- Rhythm engine writes: `$56 reg $10` key mask (bit7=1 => those bits go
  OFF), `$56 $11` global level, `$18+d` per-voice `(pan<<6)|vol`.
  Channel map for our writer: dispatch 9..14 = BD SD TP HH TM RM,
  `DIV_CH_NOISE`, instrument type DIV_INS_ADPCMA (sample ignored).
- ADPCM-B is on **port 1** $100-$10F (the YM2610 keeps it on port 0);
  start/end in 32-byte units at $102-$105, freq $109/10A, vol $10B.
  Furnace exports its RAM as data block type 0x81 - our parser already
  ingests that.
- Ext channel: enable = write `$56` register `$27` bit 0x40, the same trick as
  YM2203/YM2612 - ym2612.py already carries the ext detector.
- YM3812 and YM2612 can coexist in one machine (no compound check beyond
  count limits; max two instances of any family on export).

## YM2608 in this converter (implemented, round-trip verified)

Terminology: the chip has two **address ports**, and VGM records which one a
write went to in bit 8 of our flattened register number. "Bank" is reserved
here for things that really are banks: the YM2612's DAC byte stream at
`$2A`, the YM2610's ADPCM ROM banks, an OKI NMK112 bank.

### FM addressing is the YM2612's, so notes are too

ymfm's OPN2-family `channel_offset(chnum) = (chnum + (chnum >> 2)) * 4` gives
the YM2612, the YM2608 *and* the YM2610B one shape: two blocks of three, and a
single `$28` key-on register on port 0 whose channel field is 0-2 for the first
block and **4-6** for the second, bit 2 selecting it. That is this repo's
`KON_6`, and both rips confirm it: every `$28` in N.G. sits on port 0 with a
field of 0,1,2,4,5 or 6, and port 1 never keys anything.

`DivPlatformYM2608::setFlags` sets `fmFreqBase = 9437184` at its default /6
prescale - `YM2612_FREQ_BASE` to the unit - so there is exactly one note
formula in play for the family: `ym2612_note_for(block, fnum, clock)`, per-chip
clock only. The YM2610 shares the formula and differs in its base (9440540),
which is why `ym2610_note_for` passes that argument rather than carrying a
second implementation.

### Ext splits channel 3 in place, which pushes the second block along

`sysDefs[DIV_SYSTEM_YM2608_EXT]` (sysDef.cpp:1379-1402) names its columns FM 1,
FM 2, FM 3 OP1-4, **then** FM 4-6; `DivPlatformYM2608Ext::dispatch` hands every
channel above 5 to the base platform as `chan - 3`, exactly like
`genesisext.cpp:71-84`. So a channel-3 key opens columns 2-5 and the second
block's voices must land on **6, 7, 8** - leaving them at 3-5 would stack the
bass onto an operator column. `analyze.opn_event_ch` does that shift for both
chips, and `test_vgm2151fur_ym2608.TestYm2608ExtChannel3` pins it down.

### The two sample engines swap ports relative to the YM2610

| page | YM2610                | YM2608                                   |
|------|-----------------------|------------------------------------------|
| p0   | delta-T `$10-$1B`     | rhythm `$10` key, `$11` global, `$18+d`  |
| p1   | ADPCM-A `$00-$2D`     | delta-T `$00-$0B` (`+0x10` = YM2610 numbering) |

`YM2608State` therefore subclasses `YM2610State(b=True)` and re-points only
those two pages, handing each to the decoder that already understands it.

### Defaults are the chip's, not zero

ymfm resets every ADPCM-A level byte to `0xDF` - pan both, level 31 - with the
comment "some neogeo homebrews rely on this" (`ymfm_adpcm.cpp:48-51`), and both
platforms write the global rhythm level to `0x3F` and `$10B` (delta-T volume)
to `0xFF` at init. A driver that keys a voice without ever setting its byte is
therefore *not* silent, so `YM2610State` starts `a_vol` at 31, `a_pan`/`b_pan`
at 3 and the levels full. Zeroed defaults folded an unwritten voice to silence
and then discarded its notes as muted.

Combined attenuation is `(31 ^ level) + (63 ^ global)` (`ymfm_adpcm.cpp:226`),
so the converter folds the source's `$11` into each hit's 0-31 column and
leaves Furnace's global at its full default: verified, every level the log
asked for comes back through Furnace unchanged.

### Delta-T: cut at 32, store at 256

`$102-$105` address blocks of **32** bytes, so `collect_ym2608_delta_t` slices
exactly the span the log named. It must not round down to a 256-byte bank the
way the YM2610's ADPCM-A does - that would put up to 255 bytes of whatever
precedes the sample in front of its attack. `DivSample` then sizes any depth-6
body as `(((count+1)/2)+255)&~0xff` (`sample.cpp:593`) and the platform plays
to `offset + lengthB - 1`, so `deltat_store_body` adds the idle tail at write
time: short of that, Furnace would read into the *next* sample.

### Machine shape

- `0x8e` YM2608: 16 channels - FM 0-5, SSG 6-8, rhythm 9-14 (Kick Snare Top
  HiHat Tom Rim), delta-T 15. `0xb7` Ext: 19, FM columns becoming
  FM1 FM2 O1-O4 FM4-FM6.
- The rhythm columns share one empty `DIV_INS_ADPCMA` map instrument; the voice
  *is* the channel, and Furnace compiles the ROM itself.
- The delta-T channel uses `DIV_INS_ADPCMB` sample-map instruments like the
  YM2610's, and its samples render to bank **0** - `renderSamples` reads
  `renderOn[0][sysID]` (`ym2608.cpp:2032`) because the OPNA has one sample
  bank, not the YM2610's two.
- `38400*13*16` = 7987200 is one of Furnace's *named* OPNA clocks
  (`clockSel=1`), so an FM Towns rip writes that instead of a custom clock.

### Verification

```
VGM2151FUR_FURNACE=D:\Projects\Forks\vgm2151fur\furnace\furnace.exe
VGM2151FUR_OPNA_RIPS=F:\NASbackup\JamesAlan\_devspace\grbsd\ripwip\_finished
uv run --with pytest python -m pytest tests/test_vgm2151fur_ym2608.py -q
```

`TestYm2608RealRips` converts a whole track, exports it back through Furnace
and requires: every rhythm gate matched with the same per-voice level, every
delta-T gate byte-identical over the span Furnace actually plays with its step
within 8 LSB, and no `pitch_runs` divergence above 0.25 st sustained for 60 ms
on any FM or SSG channel - twelve channels on the Ext rip including all four
split columns, nine on the base one. Both rips pass clean.

## Status and what is left

Done, each with a test that drives Furnace and reads the registers back:

- VGM parsing for both chips (`$56/$57`, `$A6/$A7`, `$51/$A1`, clock 0x48/0x10,
  ROM block 0x81) and the chip-presence report.
- YM2413 end to end: notes, presets and the user bank, `DIV_INS_OPLL` (13),
  machines `0x89` and the kit's `0xa7`, verified against `euress opll.vgm` and
  madou `_38`.
- YM2608 end to end: FM, SSG, channel-3 Ext with the column shift, rhythm kit
  with the `$11` fold, delta-T samples - machines `0x8e` and `0xb7`, verified
  on N.G. (Ext) and The Great Priest Bellias (base layout). Both rips come
  back with nothing in the audit left to explain and no pitch divergence.
- z66 (YM2612 + YM3812 with a DAC stream) end to end, and with it the general
  rule that a log may keep more than one FM family. `$90`-`$95` record a
  `DacStreamPlay` per `$93`; the bank is sliced into rate-exact chunks inside
  the same release budget; `Song.fm_systems` carries every pitched system and
  `write_fur` builds its chips, flags, column counts and labels from it.
  Verified on Cosmic Dance (OPM + YMF262), Zone 66 (DAC stream + OPL2) and
  `29test` (OPN2 Ext + YM2413 kit + SN76489). `docs/merged-logs.md` is the
  reference, `tests/test_vgm2151fur_combo.py` the regression net.

Left:

1. **OPLL drums in the corpus sweep**: `mado123-opll` and any rip toggling
   `$0E`, which exercises the two-machine writer outside a hand-built test.
2. **Corpus sweep**: convert every rip under `gs-opna\done`, `ed4-opna\done`
   and `mado123-opll` and audit each - that is where an assumption that happened
   to suit two tracks gets caught by a third.
3. **Per-family `--verify`**: `diag.pitch_runs()` still chooses its stream from
   the source's chip census, so on a combined module it follows one family no
   matter which channel it is asked about. The merged tests read each chip's
   own registers instead.
