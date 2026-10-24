# vgm2151fur

Converts arcade VGM/VGZ rips into Furnace Tracker modules.
Supported chips:
- Yamaha YM2151 (OPM)
- Yamaha YM2203 (OPN)
- Namco C140
- Namco C352 (requires a development build of Furnace from a compatible fork. A build with C352 support (built from https://github.com/me-when-the-uh/furnace-ports) is located in `./furnace-c352` - use at your own risk, since it's always a good idea to build from source instead of relying on someone else's builds)
- Konami K007232 (experimental)
- Konami SCC / SCC+ (K051649/K052539) (experimental)

It runs on Windows and Linux with Python 3.11 or newer.

## Requirements

- Python 3.11 or newer
- Furnace, for the volume fit and the `render`, `compare`, and `--verify` checks. Put a Furnace build in `furnace/` next to the project, or set `VGM2151FUR_FURNACE` to the executable. Any build works: the tools run it with `-console`. The normal release also includes a console build (`furnace-...-win64-console.zip`) if you want one, but you don't need it. Without Furnace, `convert --no-normalize` still writes modules, just without the level fit

## Quick start

```
cd vgm2151fur
python -m vgm2151fur
```

With no arguments the tool opens a menu. Type a number, or paste a path to a VGM, a pack folder, or a folder of packs. Quotes from Explorer's Copy as path are fine. `q` or Ctrl+C quits

```
 1  packs      scan a folder, then convert, volumes, render
 2  convert    a file, a folder, or a glob
 3  volumes    a .fur file or a folder of them
 4  compare    a .fur against its source VGM
 5  render     .fur to WAV
 6  report     analysis, nothing written
 7  condense   write copies of tracks with 1x..4x lower BPM
```

`packs` is the place to start. It scans a folder (remembered next time), lists the packs with track counts and chips, and opens a track list with a dash next to tracks that have no `.fur` yet. `c` converts all tracks or a selection (`2-6`, `1,4`, or a single number), `e` changes a chip volume on the converted tracks, and `r` renders one to WAV so you can hear it. The menu stores its state in `~/.vgm2151fur.json`.

## Commands

The menu actions are also available as commands:

```
python -m vgm2151fur convert "01 Track.vgz" -o out
python -m vgm2151fur convert "My Gamepack" -o out --workers 6
python -m vgm2151fur report "01 Track.vgz"
python -m vgm2151fur edit out/01.Track.fur --list
python -m vgm2151fur edit out/01.Track.fur --chip C140 --factor 0.9
python -m vgm2151fur compare "01 Track.vgz" out/01.Track.fur --channel 4
python -m vgm2151fur render out/01.Track.fur --loops 2
```

`convert` writes one `.fur` per input. `-o` sets the output folder (the default is a `fur/` folder beside the input). `--speed N` pins ticks per row when the automatic grid estimate is wrong, `--workers N` sets how many tracks run at once, and `--melody-only` / `--pcm-only` restrict the chips. A folder of packs writes each pack to its own `fur/` folder, or to `-o/<pack>`. Every track prints its size, grid speed, instrument and sample counts, and any warnings.

`report` prints the analysis without writing anything. Use it to see which chips a VGM uses, how the grid was estimated, and what was skipped.

`edit` lists or changes chip volumes. `--chip` takes a number, a chip name, or `all`; `--factor 1.1` raises the volume by 10%, and `--value 0.9` sets it outright.

`compare` exports the `.fur` through Furnace and reports stretches where its pitch stream differs from the source VGM by more than half a semitone for at least 100 ms. A run is worth listening to; no runs means the two register streams agree where it matters.

`render` writes a WAV of a `.fur` so you can hear the conversion.

`--variants` writes the 1x/2x/3x/4x row-condensing ladder side by side, and `--optimize lossless` marks the largest factor that adds no structural loss; `--verify` renders each candidate and rejects sustained pitch divergence. `docs/operating.md` has the workflow.

## Tests

```
cd vgm2151fur
python -m pytest tests -q
```

The suite runs without game packs. Tests that need Furnace skip when it is missing.

## Layout

```
vgm2151fur/     the package: analyze, furwrite, convert, edit, diag
tools/          standalone helpers, including fur2vgm and fur2wav
tests/          pytest suite
docs/           operating guide, format notes, chip notes
```

## Known limits

- The YM2151 and YM2203 models cover the OPM/OPN register sets the arcade drivers use. Other chips (YM2612, YM3812, ...) are not supported.
- YM2203 notes are labelled at the highest carrier's octave when a patch uses a power-of-two carrier MULT. A constant pitch macro on the instrument keeps the source f-num/block/audio unchanged, so the pattern note matches the audible carrier.
- The YM2203's channel-3 3-slot chord is reduced to one operator, CSM timer key-ons are not synthesized, and LFO writes are skipped; the report warns when it sees them. Very fast SSG ornaments shorter than a pattern row are simplified (`docs/ym2203.md`).
- Furnace 0.6.8.3 has no C352 core; a build with the ported core can play those modules (`docs/c352.md`).
- Chip volumes above unity only exist in INF2 files.
- Patterns are 256 rows; the row grid is estimated per track and very fine pitch motion is approximated with per-row ramps (`docs/fur-writer.md`).
