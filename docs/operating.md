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

Ctrl+C cancels a running command and returns to the menu. At the menu prompt
it quits (exit code 130).

The CLI equivalent for scripts:

```
python -m vgm2151fur "My Rips/Hyper Game (System 2)" -o "output/hyper-game/fur"
```

The same command on a folder of packs converts every pack. With `-o`, each
pack lands in its own subfolder. `edit` and `render` accept the pack folder
after that: they read the `.fur` files in `fur/`. A pasted path can be
quoted, and a glob the shell did not expand (`*.vgz`) is expanded by the tool.

### Condensed variants

Furnace's BPM is `15 x rows/s`, so the way to make a track more readable is to
lengthen its rows. `--variants` writes the 1x/2x/3x/4x set side by side
(`<name>.fur` plus `<name> xN.fur`; the 1x file is never replaced), and
`--optimize` says which one to mark:

```
python -m vgm2151fur convert "pack/02 Track.vgz" -o out --variants --optimize lossless
```

- `none` (default): write only the 1x file, as before.
- `lossless`: pick the largest factor that adds no structural loss and no more
  than 0.1 st of pitch-model deviation over 1x; fall back to 1x when condensing
  is not safe (some dense rips are already lossy at 1x).
- `all`: keep the largest factor.

`--verify` makes `lossless` authoritative: every candidate is rendered through
Furnace and rejected if it adds a sustained pitch divergence over 1x (0.25 st /
60 ms, one run of slack). It costs a render per variant but is the only gate
that catches what the structural counters miss; the menu entry `7` always
verifies. `--condense N` sets the largest factor (default 4) and the ladder
keeps the half steps - `1, 1.5, 2, 2.5, 3, 3.5, 4`. A row does not have to be
a whole number of samples, and a musical unit that lands between two whole
steps would otherwise be skipped. `--factors 1,1.5,2,2.5,3,4` writes an
explicit ladder instead (fractional allowed). Before trusting a whole pack,
run `compare` on the chosen variant.

The grid is measured from a lattice of note starts. The YM2151 key-on register
supplies it when the rip has a usable FM stream. When it does not - the C352
and SegaPCM-only board rips have no YM2151 at all - the sample-chip trigger
times are measured the same way, which keeps those rips off the 60 Hz frame
fallback. That fallback reads as exactly 900 BPM (60 rows/s) and carries no
musical information. A rip whose FM lattice already measures uses it alone.

The TUI runs this by default: a plain `c convert` writes the 1x file to
`default/` and each lossless factor to `x2 optimised/`, `x3 optimised/`, ...,
drops the factors that would lose detail, and records every verdict in
`condense.tsv`. Each manifest row names the grid it came from, which tells a
measured lattice from a frame fallback at a glance. The CLI keeps the
flat `<name> xN.fur` siblings unless you pass `--dirs`. The verdict is
re-derived from the source on every run, so a reconversion
(`tools/reconvert_all.py --optimize lossless [--verify]`) reaches the same set
with or without the manifest - the manifest just lets you read it.

Conversion renders each finished track once through the Furnace console and
fits the module's master volume to a -2.5 dBFS peak (one render per track,
a few seconds). `--no-normalize` skips the render and writes the module at
unity.

The same pass looks at the note grid and, when the loop is a whole number of
bars, moves it onto the downbeat. It also searches a window on either side of
the marker (four seconds, or the whole loop when that is shorter) for the
phrase start whose copy sits one loop length later, and moves the loop there:
a marker that fell inside the phrase lands on the phrase's own head, and the
loop keeps the length the VGM header declares, unless the copy measures a
shorter period, and then the wrap moves onto the copy's own row. The jump sits
exactly one period after the new start, so the recording's overshoot, the next
iteration's own head, stays outside the loop; a jump row carrying notes would
play them and strike the head twice at the seam. Content at or past the wrap
sample stays unwritten at every row rate, so a condensed grid cannot fold the
copy's first row back onto the wrap row. The loop opens one row before the first
commands, because a wider cut lands the seam on rows the log does not repeat
exactly and the loop wobbles there. The cut is then seated: it moves a row or
two only when a neighbouring seat lines the rows after the jump up exactly,
because a shift does not change which rows pair across the seam, only which
pairs sit right after it. A grid that does not repeat, or a loop with no
phrase start in that window, keeps the VGM offset. `--no-loop-find` keeps it
on every track. The convert line says `loop kept` or how far the marker moved
(`loop -1/8`, `loop 30 rows late`, `loop 120 rows early`).

The recording's own copy settles the phase. The rows past the declared loop
end repeat the loop's first rows, and a start whose copy does not line up
there moves onto the copy's first row. A start already faithful to the copy
keeps its seat, and a loop whose copy the recording cuts off early keeps the
VGM offset.

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
python -m vgm2151fur edit "output/game-pack/fur/02 Track.fur" --list
python -m vgm2151fur edit "output/game-pack/fur/02 Track.fur" --chip C140 --factor 1.2
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
- `tools/reconvert_all.py --sources DIR [--output DIR] [--only slug]
  [--workers N]`: reconvert every `output/<slug>/fur` from its source pack
  on the estimator's grid (a stored speed is never re-passed) and fit the
  volume; prints the gain per track. A track that needs a manual pin
  converts alone with `--speed N` afterwards. The worker count and low
  priority match `convert`.
- `tools/vgmdiff.py <source.vgm> <other.vgm> [ch ...]`: pitch runs between
  two VGMs.
- `tools/vgmchroma.py <source> <export>`: chroma windows, with `--mute` to
  isolate a chip.
- `tools/pitch_shift.py <a.wav> <b.wav>`: the chroma-rotation table one pair
  of renders at a time, used by the soundcheck.
- `tools/vgm_scan.py [root ...]`: which VGM packs use SegaPCM, C140, C352 or
  C219 and at which clock. C219 is a C140-family part the converter does not
  support; never batch-convert those into the C140 target. C352 converts as
  chip 0xD0; a Furnace build with a C352 core can play it. See docs/c352.md.

## Environment

- `VGM2151FUR_FURNACE`: path to `furnace.exe`, or to a folder holding it.
  Without it the tools look for a `furnace/` folder next to the project (a
  source checkout with `build/furnace.exe` works too), then a
  `third_party/furnace-console/` or `third_party/furnace/` drop-in, then
  PATH.
- `VGM2151FUR_VGM2WAV`: path to `vgm2wav-mute.exe` for the soundcheck and
  chroma tools.

## Exit codes

`0` success, `1` a file failed, `2` a usage error, `130` cancelled with
Ctrl+C. `compare` returns 1 when runs were found, so it doubles as a check
in a script.
