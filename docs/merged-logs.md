# Merged logs: several chip families in one module

A "merged" rip is a hand-assembled soundtrack: someone took an arcade log and
an OPL or MSX arrangement of the same tune and interleaved them into one file,
so the players hear both engines at once. The `*_merged.vgm` set under
`F:\NASbackup\JamesAlan\_devspace\grbsd` is that batch, and it is the reason
this tool no longer picks one FM family per log.

Facts here were read out of the logs themselves (via `load_vgm`, cross-checked
against `vgm2txt` dumps) and out of Furnace 0.6.8.x behaviour on the exported
modules, 2026-11.

## What a merged log looks like on disk

The chip census is the whole diagnosis. `Counter(w.chip for w in vgm.writes)`
over the corpus gives four shapes:

| log | families that write | who plays |
|-----|--------------------|-----------|
| `z66_pc-fm_trimmed_merged.vgm` | `opl2`, `ym2612`, `sn76489` | OPL2 melody (2716 key-ons); the YM2612 keys nothing and streams its DAC; the PSG is init-only |
| `15 Cosmic Dance [Round 7]_merged.vgm` | `ym2151`, `opl3` | both: 1410 OPM notes on eight voices under a 2697-note YMF262 arrangement |
| `0VGMLog_merged.vgm` | `ym2612`, `opl3` | both, plus `$95` DAC blocks on the OPN2 |
| `29test-…_merged_merged.vgm`, `mu6_100-…` | `ym2612`, `opll`, `sn76489` | OPN2 melody, YM2413 kit where it latches, PSG drums; in some of them the OPLL only ever got its init block |

Two of those four have two *pitched* families. The one-family rule that used to
live at the top of `analyze()` could only ever keep half the music.

## The model

`Song.fm_systems` is a list of `FmSystem(kind, chip_id, fm_channels, ext, csm,
drums, clock)` in module order - one entry per pitched system, arcade families
first, then the OPL ones. Every channel base in `analyze`, and the chip list,
flags, column counts and channel labels in `write_fur`, is derived from it, so
a single-family module is just the one-element case.

- `FmSystem.channels` is the span that system occupies in Furnace (FM plus its
  own SSG and sample columns). `system_channel_ranges()` turns the list into
  `(system, first channel)` pairs; that is what keeps an OPLL kit from being
  read as OPN2 channels.
- The arcade family keeps the plain column tags (`F1-1`, or `O1-2` for a split
  channel-3 operator). An OPL family that shares the module is numbered by its
  module channel instead (`F9`…`F17`), because `F1` already means something on
  the chip beside it. A lone OPL keeps Furnace's own `FM 1`…`FM 9`/`F1` tags.
- The volume column's ceiling is a chip property, so `_channel_vol_max()`
  gives each system's FM columns their own: 127 on the OPM and OPN family,
  63 on an OPL, 15 on the YM2413. `Song.fm_vol_max` keeps the value of the
  first family, which is what a single-family module always used.

`estimate_grid()` takes the union of every converted family's writes, so a PCM
stream whose onsets are the only rhythm still measures the melody's tempo.

YMZ280B is a sample engine rather than an FM family. Its systems follow the
same module order as the other sample chips: after the FM block and K053260,
before SCC/AY/NES/PCE/Game Boy. A YMZ-only log suppresses the legacy YM2151
placeholder, so `07 Stage4.vgm` becomes two `0xB8` systems and sixteen real
columns (`docs/ymz280b.md`).

MultiPCM, uPD7759 and 32X PWM follow YMZ280B in the sample-engine order.
MultiPCM is a native 28-column system per chip (`docs/multipcm.md`); uPD7759
and PWM use Generic PCM DAC systems because Furnace has no native platform
(`docs/upd7759.md`, `docs/pwm32x.md`).  A log with only these chips likewise
suppresses the legacy YM2151 placeholder.

Virtual Boy VSU is a pitched chip and comes after Game Boy: six columns per
chip (`0x9C`). A VSU-only log likewise suppresses the placeholder, so the
Jack Bros and Galactic Pinball references are six real columns
(`docs/vsu.md`). Its wave changes use `10xx` effects, so a merged module
keeps the VSU wave blocks after the SCC/PCE/GB blocks and drops a wave
change that would land past index 255.

## Who is allowed to share

`share_fm`: one arcade family plus one OPL-family system coexist when the
arcade side is a YM2151 or a YM2612. Those two have nothing to argue about -
no ADPCM-A bank, no delta-T engine and no second PSG of their own - so each
system keeps its own registers, instruments and clock. A YM2610 or YM2608
beside an OPL is still one family only: their sample engines and SSG already
claim columns a second pitched chip cannot share cleanly.

Within the arcade side, precedence is unchanged: OPNA over OPNB over OPN2 over
OPN, a live YM2151 over a YM2203.

`is_combo`: the narrower Zone 66 shape - a YM2612 with zero FM key-ons but real
DAC audio beside an OPL that carries the tune. The OPN2 contributes a system
with no notes on five of its six columns and PCM on the sixth.

## Silence is not a system

A merged log opens every chip it knows about and then scores one of them. The
18 YM2608 writes in `1-ay-ym2612_merged.vgm` are `$10`-`$1F` set to zero at
sample 0: an init block, nothing more. `_family_sounds()` decides who plays by
looking for something that can make a noise -

- a rising key-on (via `_family_keyons`), which covers FM, OPL and YM2413;
- an SSG voice someone actually tuned (a period byte in `$0`-`$5`);
- an OPNA rhythm latch with a trigger bit, or a YM2610 ADPCM-A `$10` with its
  trigger bits set.

- and a chip that has none of those loses its system, named in the report:

```
warning  YM2608 write no notes; the chip only received its init block, so it gets no system
```

The same test drops a silent SN76489 rather than carrying four dead columns
(`SN76489 writes carry no audible note; the PSG system was dropped`).

Every family that writes and is *not* converted is still reported, after the
decision rather than before it - the old messages were emitted while the code
had not yet chosen, so a log that kept both sides used to claim a loss it did
not suffer.

## The DAC stream (`$90`-`$95`)

See `ym2612.md` for the chunk model. The short version for a merged log:
`$90 ss 02 00 2A` names the YM2612 DAC driver, `$92` gives its byte rate, and
each `$93` is one contiguous range of the type-`0x00` PCM database.
`vgm.DacStreamPlay` carries the range with its mode; `collect_dac()` slices
that bank instead of expanding per-byte writes. A play at or past the header's
`total_samples` is the exporter's end-of-file flush and is dropped
(`dac_stream_tail_plays`), because Furnace re-emits one when it writes the
module back out.

## Verified shapes

With `VGM2151FUR_FURNACE` and `VGM2151FUR_MERGED_RIPS` set,
`tests/test_vgm2151fur_combo.py::TestMergedRips` converts each real log,
exports it through Furnace and reads the registers back:

| log | chips (hex) | channels | what proves it |
|-----|-------------|----------|----------------|
| `15 Cosmic Dance [Round 7]_merged.vgm` | `0x82`, `0x91` | 26 (8 + 18) | the same eight OPM voices key on both sides, the same YMF262 channels answer, clocks 4 MHz and 14.31818 MHz survive |
| `z66_pc-fm_trimmed_merged.vgm` | `0x83`, `0x90` | 15 (6 + 9) | DAC audit misses nothing (`compare_dac.missed == 0`) over 240 chunks, the export's PCM covers 99 %+ of the source stream, and the OPL2 still keys its melody |
| `29test-…_merged_merged.vgm` | `0xa0`, `0xa7`, `0x3` | 24 (9 + 11 + 4) | all three engines still write after the round trip; `ym2413_clock` reads 3579545 |

The whole `*_merged.*` batch - 72 files - converts and writes with no channel
mismatch against `furio.CHIP_CHANNELS`.

## Running the sweep yourself

```powershell
$env:VGM2151FUR_FURNACE = "D:\Projects\Forks\vgm2151fur\furnace\furnace.exe"
$env:VGM2151FUR_MERGED_RIPS = "F:\NASbackup\JamesAlan\_devspace\grbsd"
uv run --with pytest -q python -m pytest tests/test_vgm2151fur_combo.py -q
```

`_merged_root()` also falls back to the grandparent of
`VGM2151FUR_OPNA_RIPS`, since that batch lives in the same tree.

## Known rough edges

- `diag.pitch_runs()` still picks its stream from the *source* log's chip
  census, so `report --verify` on a combined module follows the arcade family
  whatever channel it is asked about. Per-family verification today happens in
  `TestMergedRips` by reading each chip's own registers.
- An OPL4 (YMF278B) side, as in `merged_opl4_fmpcm_*.vgm`, is not emulated by
  this tool at all; those logs keep only whatever else writes.
- A YM2610 or YM2608 merged with an OPL family still converts one side only.
