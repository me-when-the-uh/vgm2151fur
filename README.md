# vgm2151fur

Transcribes arcade VGMs into Furnace Tracker modules. It reads YM2151 (OPM)
register streams plus the chips next to them: the sample players K007232,
MSM6295, MSM6258, SegaPCM, Namco C140 and Namco C352, and the pitched Konami
SCC / SCC+ (K051649 / K052539) and AY-3-8910. The output is a `.fur` sequence
file that Furnace plays and a human can finish by hand.

uPD7759 voice ROMs are not converted (Furnace has no core for the chip), but
the stand-alone ROM can be decoded outside the .fur path:
`python tools/upd7759_dump.py <rom.bin or mame.zip>` writes one WAV per
phrase plus an index (`--help` lists the options). The decode matches MAME's
state machine including the 36 clocks a block header costs.

Non-track sound codes (SFX, one-shots, speech) come from the ripper's
stop-first records: `python tools/sfx_extract.py --wav-dir output/<game>/wav
--out output/other/<game>/sfx --target-peak 21000` finds each code's sound in
its recording, trims it, mixes to mono and peak-matches it. 21000 (-3.9 dBFS)
is the delivered level: a whole code is a dense mix whose crest factor is much
lower than one building-block sample's, so a full-scale peak plays back about
a third too loud next to the existing sets. `upd7759_dump.py --peak 21000`
renders the uPD7759 voice set to the same level. The full recipe, including
the Gradius III plan, is docs/sfx-code-rip.md in the ripper root.

Windows, Linux and macOS, Python 3.11 or later. No install; run it from the
checkout.

## Getting started

```
cd vgm2151fur
python -m vgm2151fur
```

That opens the menu, which is the everyday way to use this. Type a number,
or paste a path to a VGM, a pack folder, or a folder of packs. A quoted path
is fine. `q` or Ctrl+C quits.

`1 packs` is the one to start from: it scans a folder (remembered for next
time) and lists the packs with track counts and chips. Open one to see its
tracks, then `c` converts them (all, or a selection like `2-6`), `e` raises
or cuts a chip volume, and `r` renders a track to WAV so you can hear it.
The menu keeps its state (last folder, pack and output) in
`~/.vgm2151fur.json`.

## What you need

- Python 3.11 or later.
- A Furnace console build for the per-track volume fit, `render`, `compare`
  and variant verification: put a checkout or release next to the project as
  `furnace/`, or set `VGM2151FUR_FURNACE` to the binary. Conversion still
  runs without it (`--no-normalize --optimize none`), just unnormalized.
- Optional: `vgm2wav-mute.exe`, the reference renderer for
  `tools/fur_soundcheck.py` and `tools/vgmchroma.py`
  (`VGM2151FUR_VGM2WAV`).

## The menu

```
 1  packs      scan a folder, then convert, volumes, render
 2  convert    a file, a folder, or a glob
 3  volumes    a .fur file or a folder of them
 4  compare    a .fur against its source VGM
 5  render     .fur to WAV
 6  report     analysis, nothing written
 7  condense   write 1x..4x BPM-variant copies, pick the safe one
```

A pasted file converts directly, a pack opens, a folder of packs lists them.
`m` reprints the menu. Ctrl+C cancels a running command, and quits at the
prompt.

## Command line

Everything the menu does is also a command, for scripts and agents:

```
python -m vgm2151fur convert "01 Track.vgz" -o out
python -m vgm2151fur report "01 Track.vgz"
python -m vgm2151fur edit out/01.Track.fur --chip C140 --factor 0.9
python -m vgm2151fur compare "01 Track.vgz" out/01.Track.fur --channel 4
python -m vgm2151fur render out/01.Track.fur --loops 2
```

- `convert` writes one `.fur` per input (default: a `fur/` folder beside it).
  `--speed N` pins ticks per row, `--workers N` sets parallel tracks,
  `--melody-only` / `--pcm-only` restrict the chips.
- `report` prints the analysis without writing anything.
- `edit` lists, scales or sets chip volumes. `--chip` takes a number, a
  name, or `all`; `--factor 1.1` raises by 10%, `--value 0.9` sets outright.
- `compare` exports the `.fur` and reports where its pitch stream leaves the
  source VGM's. A run is worth a listen; none means the registers agree.
- `render` writes a WAV so you can hear the conversion.

`--variants` with `--optimize lossless` writes the 1x..4x row-condensing
ladder and picks the safest factor; `--verify` renders each candidate to
judge it. docs/operating.md has the workflow.

## Tests

```
cd vgm2151fur
python -m pytest tests -q
```

No game packs needed. Tests that want the Furnace console build skip when it
is missing.

## Project layout

```
vgm2151fur/          the package: analyze, furwrite, convert, edit, diag
tools/               thin CLIs over the package: fur2wav, fur2vgm,
                     fur_soundcheck, vgmdiff, vgmchroma, pitch_shift,
                     vgm_scan, upd7759_dump, sfx_extract
tests/               pytest suite
docs/                operating guide, format notes, chip notes
```

## Known limits

- The YM2151 model covers the OPM register set the arcade drivers use. Other
  chips (YM2612, YM3812, ...) are not supported.
- Furnace 0.6.8.3 has no C352 core; a ported core can play those modules
  (docs/c352.md).
- Chip volumes above unity only exist in INF2 files.
- The pattern window follows the track (last content row + 10, capped at
  Furnace's 256); the row grid is estimated per track and very fine pitch
  motion is approximated with per-row ramps (docs/fur-writer.md).
