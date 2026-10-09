# vgm2151fur

Transcribes arcade VGMs into Furnace Tracker modules. It reads YM2151 (OPM)
register streams and the sample chips that sit next to them on the boards it
was built against: K007232, MSM6295, MSM6258, SegaPCM, Namco C140 and Namco
C352. The output is a `.fur` sequence file that Furnace plays and a human can
finish by hand.

It runs on Windows and Linux, Python 3.11 or later.

## Requirements

- Python 3.11 or later.
- A Furnace console build for the render/export checks (the volume fit,
  `render`, `compare`, `--verify`). Furnace is not vendored: keep a checkout
  or an unpacked release next to the project as `furnace/`, or set
  `VGM2151FUR_FURNACE` to the binary.
- Optional: `vgm2wav-mute.exe` (the reference VGM renderer) for
  `tools/fur_soundcheck.py` and `tools/vgmchroma.py`. Set
  `VGM2151FUR_VGM2WAV` to point at it.

## Quick start

```
cd vgm2151fur
python -m vgm2151fur
```

With no arguments the tool opens a short menu. Numbered commands, and a path
pasted at the prompt. Quotes from Explorer's Copy as path are fine. `q` or
Ctrl+C quits.

```
 1  packs      scan a folder, then convert, volumes, render
 2  convert    a file, a folder, or a glob
 3  volumes    a .fur file or a folder of them
 4  compare    a .fur against its source VGM
 5  render     .fur to WAV
 6  report     analysis, nothing written
 7  condense   write 1x..4x BPM-variant copies, pick the safe one
```

A pasted file converts. A pasted pack opens so you can pick tracks. A pasted
folder of packs lists them.

`packs` is the one to start from. It scans a folder (the last one is
remembered), lists what it found with track counts and chips, and a pack view
lists the tracks in playlist order, with a dash next to the ones that have no
`.fur` yet. `c` converts all tracks or a selection (`2-6`, `1,4`, or a single
number), `e` raises or cuts a chip volume on the converted tracks, and `r`
renders one to WAV so you can hear it. The menu writes its state to
`~/.vgm2151fur.json` (last folder, last pack, last output folder).

## Commands

The same actions are available as commands for scripts and agents:

```
python -m vgm2151fur convert "01 Track.vgz" -o out
python -m vgm2151fur convert "My Gamepack" -o out --workers 6
python -m vgm2151fur report "01 Track.vgz"
python -m vgm2151fur edit out/01.Track.fur --list
python -m vgm2151fur edit out/01.Track.fur --chip C140 --factor 0.9
python -m vgm2151fur compare "01 Track.vgz" out/01.Track.fur --channel 4
python -m vgm2151fur render out/01.Track.fur --loops 2
```

`convert` runs the analyzer (notes, row grid, pitch trajectories, samples)
and writes one `.fur` per input. `-o` sets the output folder (default: a
`fur/` folder beside the input). `--speed N` pins ticks per row when the
automatic grid estimate is not what you want, `--workers N` sets how many
tracks run at once (default: close to the logical CPU count, low priority),
and `--melody-only` / `--pcm-only` restrict the chips. A folder of packs
writes each pack to its own `fur/` folder, or to `-o/<pack>` when `-o` is
set. Every file reports its size, grid speed, instrument and sample counts,
and any warnings.

`report` prints the same analysis without writing anything. Use it to see
which chips a VGM uses, how the grid was estimated and what was skipped.

`edit` reads a `.fur`, lists its chips, and scales or sets their volumes.
`--chip` takes a number, a chip name, or `all`; `--factor 1.1` raises the
volume by 10%, `--value 0.9` sets it outright. A pack folder is accepted:
the `.fur` files in `fur/` are the ones edited.

`compare` reads the source VGM, exports the `.fur` to VGM with the local
Furnace build, and reports stretches where the two pitch streams sit apart
by more than half a semitone for at least 100 ms. A run is worth
investigating: a held wrong pitch is worse than a missed note. No runs means
the two register streams agree where it matters.

`render` writes a WAV of a `.fur` so you can hear the conversion.

`--variants` writes the 1x/2x/3x/4x row-condensing ladder side by side and
`--optimize lossless` marks the largest factor that adds no structural loss;
`--verify` renders each candidate through Furnace and rejects sustained
pitch divergence. docs/operating.md has the workflow.

## Tests

```
cd vgm2151fur
python -m pytest tests -q
```

The suite runs without game packs. Round-trip tests that need the Furnace
console build skip when it is missing.

## Project layout

```
vgm2151fur/          the package: analyze, furwrite, convert, edit, diag
tools/               thin CLIs over the package: fur2wav, fur2vgm,
                     fur_soundcheck, vgmdiff, vgmchroma, pitch_shift, vgm_scan
tests/               pytest suite
docs/                operating guide, format notes, chip notes
```

## Known limits

- The YM2151 model covers the OPM register set the arcade drivers use. Other
  chips (YM2612, YM3812, ...) are not supported.
- Furnace has no C352 core in the 0.6.8.3 release that this was built
  against; a ported core can play the modules (docs/c352.md).
- Chip volumes above unity only exist in INF2 files.
- Patterns are 256 rows; the row grid is estimated per track and very fine
  pitch motion is approximated with per-row ramps (docs/fur-writer.md).
