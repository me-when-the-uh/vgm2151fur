# vgm2151fur

Transcribes arcade VGMs into Furnace Tracker modules. It reads YM2151 (OPM)
register streams and the sample chips that sit next to them on the boards it
was built against: K007232, MSM6295, MSM6258, SegaPCM, Namco C140 and Namco
C352. The output is a `.fur` sequence file, but due to the complexity of the
VGM format and the tracks themselves, this may not be a 120% perfect import.

It was pretty challenging to implement this level of broad support. If you
spot any spots in your .fur files that are rough around the edges, this should
give you an idea of how difficult it really was.

The converter runs on Windows and Linux (Python 3.11 or later). It needs a
Furnace console build only for the render/export checks; the Windows-only
verification tools call the same builds.

## Requirements

- Python 3.11 or later.
- The Furnace console build for render/export checks. Furnace is not
  vendored. Keep a checkout or an unpacked release next to the project as
  `furnace/` (the tools also find the build directory, `furnace`,
  `furnace.exe` on Windows), or set `VGM2151FUR_FURNACE` to the binary. A
  local `third_party/furnace/` drop-in works too and is gitignored.
- Optional: `vgm2wav-mute.exe` (the reference VGM renderer) for
  `tools/fur_soundcheck.py` and `tools/vgmchroma.py`. Set
  `VGM2151FUR_VGM2WAV` to point at it.

## Quick start

```
cd vgm2151fur
python -m vgm2151fur
```

With no arguments the tool opens a short menu. Numbered commands, and a path
pasted at the prompt. Quotes from Explorer's Copy as path are fine.

```
 1  packs      scan a folder, then convert, volumes, render
 2  convert    a file, a folder, or a glob
 3  volumes    a .fur file or a folder of them
 4  compare    a .fur against its source VGM
 5  render     .fur to WAV
 6  report     analysis, nothing written
```

A pasted file converts. A pasted pack opens so you can pick tracks. A pasted
folder of packs lists them. On the command line those paths convert:

```
python -m vgm2151fur "03 Game BGM 1.vgz"
python -m vgm2151fur "Metal_Hawk_(Namco_System_2)"
```

`packs` is the one to start from. It scans a folder (the last one is
remembered), lists what it found with track counts and chips, and a pack view
lists the tracks in playlist order:

```
pack VGM_collection/new/Hyper_Duel_(Arcade)  (22 tracks, YM2151, MSM6295, playlist order)
  1. 01 Credit
  2. 02 Select
  ...
  3. 03 Buster Gear Cyber Fleet (Stage 1)   -
c convert, e volumes, r render, b back (a '-' means no .fur yet)
```

`c` converts all tracks or a selection (`2-6`, `1,4`, or a single number),
`e` raises or cuts a chip volume on the converted tracks, and `r` renders one
to WAV so you can hear it. The menu writes its own state to
`~/.vgm2151fur.json` (last folder, last pack, last output folder).

The same actions are available as commands for scripts and agents:

```
python -m vgm2151fur convert "03 Game BGM 1.vgz" -o out
python -m vgm2151fur convert "Metal_Hawk_(Namco_System_2)" -o out --workers 6
python -m vgm2151fur report "03 Game BGM 1.vgz"
python -m vgm2151fur edit out/03.Game.BGM.1.fur --list
python -m vgm2151fur edit out/03.Game.BGM.1.fur --chip C140 --factor 0.9
python -m vgm2151fur compare "03 Game BGM 1.vgz" out/03.Game.BGM.1.fur --channel 4
python -m vgm2151fur render out/03.Game.BGM.1.fur --loops 2
```

## Converting

`convert` runs the analyzer (notes, row grid, pitch trajectories, samples)
and writes one `.fur` per input next to the name you give with `-o`
(default: a `fur/` folder beside the input). A folder of packs writes each
pack to its own `fur/` folder, or to `-o/<pack>` when `-o` is set.
`--speed N` pins ticks per row when the automatic grid estimate is not what
you want. `--workers N` sets how many tracks run at once (default: the
logical CPUs minus one thread, minus two when there are six or more, capped
at 16, because the heavy part of a track is a single-threaded Furnace
render; the workers and their renders run at low priority). An explicit
count may go up to one below the logical thread count — every thread when
there are fewer than eight — with 31 as the safety ceiling: past that the
renders compete and the batch runs slower, not faster, and the clamp says
so when it applies. `--melody-only` skips the sample chips and `--pcm-only`
skips the YM2151. Every file reports its size, grid speed, instrument and
sample counts and any warnings. `report` uses the same worker count.

`report` prints the same analysis without writing anything. Use it to see
which chips a VGM uses and how the grid was estimated.

## Editing chip volumes

Furnace stores one volume per chip, not per channel. `edit` reads a `.fur`,
lists its chips, and scales or sets their volumes:

```
  1. YM2151  1.000
* 2. C140    0.900
```

`--chip` takes a number, a chip name, or `all`. `--factor 1.1` raises the
volume by 10%, `--factor 0.75` cuts it by a quarter, `--value 0.9` sets it
outright. The menu asks the same question with the same syntax, where a
leading `=` means "set". A pack folder is accepted: the `.fur` files in
`fur/` are the ones edited.

A `.fur` keeps the chip volume twice: a byte per chip that old readers use
(64 = unity) and a float triple behind the sample data that current readers
use. `edit` patches both so every reader agrees. The byte field cannot hold
more than unity, so an old-format file caps at 1.0; INF2 files (newer
Furnace saves) accept up to 4.0.

## Verifying a conversion

`compare` reads the source VGM, exports the `.fur` to VGM with the local
Furnace build, and reports stretches where the two pitch streams sit apart by
more than half a semitone for at least 100 ms:

```
python -m vgm2151fur compare "Song.vgz" "Song.fur" --channel 4
channel 4: 1 run(s) of divergence
   71.469- 71.740s (  111 ms)  +1.02 st
```

A run is worth investigating: a held wrong pitch is worse than a missed note.
No runs means the two register streams agree where it matters.

`tools/fur_soundcheck.py` goes further and compares rendered audio. It needs
the reference renderer (see Requirements) and takes a few minutes per track.
`tools/vgmdiff.py` prints the same pitch runs as `compare` for two VGMs you
have already exported.

## Tests

```
cd vgm2151fur
python -m pytest tests -q
```

The suite runs without game packs. Tests that need a VGM pack skip when the
pack is not on disk; point `VGM2151FUR_FIXTURES` at the folder that holds
packs (`Gradius II AC Rip`, `Gradius_III_(Arcade)`, `VGM_collection/...`) to
run them. The Furnace round-trip tests skip when the console build is
missing.

## Project layout

```
vgm2151fur/          the package: analyze, furwrite, convert, edit, diag
tools/               thin CLIs over the package: fur2wav, fur2vgm,
                     fur_soundcheck, vgmdiff, vgmchroma, pitch_shift, vgm_scan
tests/               pytest suite
docs/                operating guide, changelog, format notes, chip notes
```

## Known limits

- The YM2151 model covers the OPM register set the arcade drivers use. Other
  chips (YM2612, YM3812, ...) are not supported.
- Chip volumes above unity only exist in INF2 files.
- The row grid is fixed at 64 rows per pattern. Very fine pitch motion is
  approximated with per-row ramps; see docs/fur-writer.md for the model.
