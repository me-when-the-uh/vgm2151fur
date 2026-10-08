# Operating guide

The README covers the commands. This file covers the workflows that use them
together, plus the environment knobs and the tools that live outside the
package.

## Converting a pack

The menu is the intended path. `python -m vgm2151fur`, then `1`, then the
folder that holds your packs. It lists every pack it finds with the track
count and the chips from the file headers, and remembers the folder for next
time. Picking a pack shows its tracks in `.m3u` playlist order when the pack
has one, with a dash next to the tracks that have no `.fur` yet. From there
`c` converts (all tracks or a selection like `2-6`), `e` adjusts a chip
volume, and `r` renders one track to WAV.

The CLI equivalent for scripts:

```
python -m vgm2151fur "VGM_collection/newest/Metal_Hawk_(Namco_System_2)" -o "output/metal-hawk/fur"
```

The same command on a folder of packs converts every pack. With `-o`, each
pack lands in its own subfolder. `edit` and `render` accept the pack folder
after that: they read the `.fur` files in `fur/`. A pasted path can be
quoted, and a glob the shell did not expand (`*.vgz`) is expanded by the tool.

Conversion renders each finished track once through the Furnace console and
fits the module's master volume to a -1.5 dBFS peak (one render, a few
seconds; docs/mix-levels.md has the method). `--no-normalize` skips the
render and writes the module at unity.

The same pass looks at the note grid and, when the loop is a whole number of
bars, moves it onto the downbeat. It also looks a window on either side of
the marker — four seconds, or the whole loop when that is shorter — for the
phrase start whose copy sits one loop length later, and moves the loop
there: a marker that fell inside the phrase lands on the phrase's own head,
behind it, and the loop keeps the length the VGM gave it, the end of the
log being the other end of that length. A grid that does not repeat, or a
loop with no phrase start in that window, keeps the VGM offset.
`--no-loop-find` keeps it on every track. The convert line says `loop kept`
or how far the marker moved (`loop -1/8`, `loop 226 rows late`,
`loop 158 rows early`).

Each track runs in its own worker process. The default count is pinned to
the logical CPUs: one thread stays free below six, two at six or more, and
16 is the ceiling (a 6 core/12 thread desktop runs 10 workers). The heavy
part of a track is a single-threaded Furnace render, so the count follows
threads rather than cores, and the spare threads keep the desktop
responsive. `--workers N` may go up to one below the thread count — every
thread when there are fewer than eight — with 31 as the safety ceiling:
past that the renders compete and the batch runs slower, not faster, and
the clamp reports that when it applies (it never exceeds the track count
either). `--workers 1` runs the tracks in order on that one worker. The
Python workers and the Furnace renders they start run at low priority, and
Furnace stays off the menu's console so the next prompt still takes a line.
`report` on a pack uses the same count. Files convert independently; one
failure does not stop the batch, and every failure is printed with the file
name. Read the warnings on each line: they name the register oddities the
converter worked around.

If the grid sounds wrong (notes landing between rows), run `report` on the
offending track, find the measured rows per second, and pin it with
`--speed N`. The analyzer prints its estimate and the evidence it used.

## Raising one chip

A converted pack often needs a level tweak before it sits right in a mix.
For example, a Namco C140 track where the samples are too quiet:

```
python -m vgm2151fur edit "output/sled/fur/02 Silent Fight.fur" --list
python -m vgm2151fur edit "output/sled/fur/02 Silent Fight.fur" --chip C140 --factor 1.2
```

The first call prints the chip table with the current values. The second
raises the C140 by 20 percent. The whole folder form takes a folder in place
of the file and applies the same change to every `.fur` in it. Compare the
file before and after with the `render` command when you want to hear the
difference.

Volumes above 1.0 need the INF2 format, which only newer Furnace saves
produce. On an old-style INFO file the tool caps at 1.0 and the listing shows
the capped value.

## Verifying a track

Three levels of checking exist, cheapest first.

1. `compare` (or `tools/vgmdiff.py` on an exported VGM): pitch runs on one
   channel. Fast, no audio, catches base-pitch and glide errors.
2. `tools/vgmchroma.py`: chroma similarity per 100 ms window between the
   source and an exported VGM, both through the same renderer. Finds windows
   where the music differs, then you explain them with level 1.
3. `tools/fur_soundcheck.py`: the full audit. Renders the source, the .fur
   through Furnace, and a VGM export of the .fur, then compares all three by
   pitch and prints the chip clock lines of the two VGM renders.

Level 3 needs `vgm2wav-mute.exe` and a few minutes per track. It was built
for the clock-ratio class of bugs (a C140 file playing 1.5x high) that no
register diff can see.

## Diagnostic tools

All of the commands below add the project root to `sys.path` themselves, so
they can be run from anywhere.

- `tools/fur2vgm.py <file|folder>`: export `.fur` to `.vgm` with Furnace.
- `tools/fur2wav.py <file|folder>`: render `.fur` to WAV with Furnace.
- `tools/reconvert_all.py [--only slug] [--workers N]`: reconvert every
  `output/<slug>/fur` from its VGM_collection source on the estimator's grid
  (a stored speed is never re-passed; see the changelog) and fit the volume;
  prints the gain per track. A track that needs a manual pin converts alone
  with `--speed N` afterwards. `*-old` folders are skipped. The worker
  count and low priority match `convert`.
- `tools/vgmdiff.py <source.vgm> <other.vgm> [ch ...]`: pitch runs between
  two VGMs.
- `tools/vgmchroma.py <source> <export>`: chroma windows, with `--mute` to
  isolate a chip.
- `tools/pitch_shift.py <a.wav> <b.wav>`: the chroma-rotation table one pair
  of renders at a time, used by the soundcheck.
- `tools/vgm_scan.py [root ...]`: which VGM packs use SegaPCM, C140, C352 or
  C219 and at which clock. C219 is a C140-family part the converter does not
  support; never batch-convert those into the C140 target. C352 converts as
  chip 0xD0. The bundled Furnace 0.6.8.3 does not play that chip, but the
  local build in `furnace/` does. See docs/c352.md.

## Environment

- `VGM2151FUR_FURNACE`: path to `furnace.exe`, or to a folder holding it.
  Without it the tools look for a `furnace/` folder next to the project (a
  source checkout with `build/furnace.exe` works too), then the bundled
  headless console build in `third_party/furnace-console/` (the patched
  C352-capable build, ~4 MB, so a clone converts and measures C352 without
  building Furnace), then a `third_party/furnace/` drop-in, then PATH.
- `VGM2151FUR_VGM2WAV`: path to `vgm2wav-mute.exe` for the soundcheck and
  chroma tools.
- `VGM2151FUR_FIXTURES`: folder holding the game packs for the fixture-gated
  tests. The tests skip when a pack is missing.

## Exit codes

`0` success, `1` a file failed, `2` a usage error. `compare` returns 1 when
runs were found, so it doubles as a check in a script.
