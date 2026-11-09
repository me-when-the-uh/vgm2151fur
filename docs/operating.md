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
- `all`: keep the largest factor. A condensed file the gate rejects is named
  `<name> xN (lossy).fur`.

`--verify` makes `lossless` authoritative: every candidate is rendered through
Furnace and rejected if it adds a sustained pitch divergence over 1x (0.25 st /
60 ms, one run of slack). It costs a render per variant but is the only gate
that catches what the structural counters miss; the menu asks for it and
defaults to no. `--condense N` sets the largest factor (default 4) and the ladder
keeps the half steps - `1, 1.5, 2, 2.5, 3, 3.5, 4`. A row does not have to be
a whole number of samples, and a musical unit that lands between two whole
steps would otherwise be skipped. `--factors 1,1.5,2,2.5,3,4` writes an
explicit ladder instead (fractional allowed). Before trusting a whole pack,
run `compare` on the chosen variant.

Every variant line and `condense.tsv` row now carries `keys`/`miss`:
`keys` is the source key-on count the writer considered and `miss` is the
number it could not put on a free pattern cell (same-row collisions, evicted
PCM, truncated orders). This is cheap and available without `--verify`.
With `--verify`, the same row adds `aud matched/total miss N fld N`: the
exported module is compared against the source on every key-on, including
the SN76489 notes, and `fld` counts effective FM operator-field mismatches.
A source with DAC phrases also shows `dac M/T`, matched against the exported
DAC stream starts. The full audit costs no extra render - it consumes the
export the pitch check already produced. `condense.tsv` has the
corresponding `audit_miss`, `fields`, `tl`, `psg_miss` and `dac_miss`
columns.

On an OPL-family module the `aud` half reads `0/0`: that field audit walks YM2151
kc/kf snapshots. `keys`/`miss` do work there - the key-on lattice comes from each
chip's own `0xBn` bit 5, or from the latch bits of `0xBD` in rhythm mode - and
`div` measures pitch inside every voice's own key, trimmed 35 ms at both ends so a
row-tick note and the sample the driver used are not charged against each other. A
kit column is left out of that measure: two percussion voices share one frequency
register, and the byte at a hit belongs to whichever was written last. The
full-register form of the check - every operator page at every attack, compared
sample-atomically after a Furnace round trip - is asserted by
`tests.test_vgm2151fur_opl.TestOplRealRips`; `diag.opl_attacks()` is the helper to
call by hand on a pack (`docs/opl.md`).

The grid is measured from a lattice of note starts. The YM2151, YM2203 or
YM2612 key-on register supplies it when the rip has a usable FM stream. When
it does not - the C352, K053260 and SegaPCM-only board rips have no FM chip at all -
the sample-chip trigger times are measured the same way, which keeps those
rips off the 60 Hz frame fallback. That fallback reads as exactly 900 BPM
(60 rows/s) and carries no musical information. A rip whose FM lattice
already measures uses it alone. A YM2203 or YM2612 whose driver writes pitch
faster than the resulting tick gets `hz` and `speed` raised together (same
row length and BPM), so the pitch macro still sees each write.

The TUI asks `optimise? (Y/n)` and defaults to yes, the `all` pick: a plain
`c convert` writes the 1x file to `default/` and every condensed factor to
`x2 optimised/`, `x3 optimised/`, ..., with ` (lossy)` on the factors the
gate rejects, and records every verdict in `condense.tsv`. Each manifest row
names the grid it came from, which tells a measured lattice from a frame
fallback at a glance. The CLI keeps the flat `<name> xN.fur` siblings unless
you pass `--dirs`. The verdict is re-derived from the source on every run, so
a reconversion (`tools/reconvert_all.py --optimize lossless [--verify]`)
reaches the same set with or without the manifest - the manifest just lets
you read it. A separate `experimental aggressive (lossy) sample clustering?`
question defaults to no; the same switch is `--lossy-sample-clustering` on
the CLI. It reduces busy YM2612 DAC, SSG PCM and NES raw-PCM maps to about 32
bodies by mapping short fragments to similar long/hot samples.

Genesis PSG notes are converted automatically; `--melody-only` keeps the DAC (and other sample-chip) data out, while the FM/PSG side still converts. The YM2612 DAC default is release-capped PCM chunks: one FM6 note per chunk, split at every `0xB6` pan change and at most every 0.25 s, with a small sample budget so release Furnace loads the module. Silence-only chunks and leading silence get no note. `collect_dac(..., slices=True)` is the explicit per-run slice mode; `timeline=True` is the old comparison body. The SN76489 system volume defaults to 0.500; an absolute VGM extra-header volume entry overrides it.

When requested, conversion renders each finished track once through the
Furnace console and fits the module's master volume to a -2.5 dBFS peak (one
render per track, a few seconds). The menu asks
`normalize/verify through Furnace? (y/N)` and defaults to no, so a normal
menu conversion never launches Furnace. On the command line, `--no-normalize`
skips the render and writes the module at unity.

The conversion keeps the VGM marker. A marker placed after a pickup leaves
that pickup at the end of each iteration, where the source plays it.
Opening the loop on the pickup would strike it twice per repeat. The loop
end is the marker plus the header's loop length, rounded to rows, and the
row length is re-derived from that length. One repeat is then sample exact.
The tempo correction is under 1%. Events just before the wrap that the
writer would round past the last row stay on that row with a delay. A
post-marker event that the row grid puts before the start moves to the
first loop row. The jump rides a channel whose row has no delay. A delayed
note on the last row still lands before it. Content at or past the wrap
sample stays unwritten at every row rate. A condensed grid cannot fold the
next iteration's first row back onto the wrap row. A copy that measures a
different period within a couple of rows moves the wrap to that row. The
module uses loop modality 2. Furnace keeps held voices and samples across
the jump. `--no-loop-find` keeps the raw marker and the source tail. The
convert line says `loop kept`. A marker that moved is named in rows. Each
condensed factor re-seats the loop on its own grid and recalibrates the
row. A copied 1x loop point would land on the previous row and add one
condensed row per repeat.

Each track runs in its own worker process. The default count is pinned to
the logical CPUs: one thread stays free below six, two at six or more, and
16 is the ceiling (a 6 core/12 thread desktop runs 10 workers). The heavy
part of a track is a single-threaded Furnace render, so the count follows
threads rather than cores, and the spare threads keep the desktop
responsive. `--workers N` may go up to one below the thread count (every
thread when there are fewer than eight), with 31 as the safety ceiling:
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
- `tools/vgm_scan.py [root ...]`: which VGM packs use SegaPCM, C140, C219,
  K054539, C352, K053260, YMZ280B, Virtual Boy VSU, SCC/SCC+, AY-8910,
  YM2203 or YM2612 and at which clock. C219 converts as its native chip 0xCF;
  do not fold it into the C140 target. K054539 uses eight Generic PCM DAC
  columns. C352 converts as chip 0xD0; a Furnace build with a C352 core can
  play it. YM2203 converts as chip 0x8D (3 FM + 3 SSG voices per chip);
  YM2612 as chip 0x83 (6 FM) or 0xA0 when the source uses the
  channel-3-operator layout. See docs/c219.md, docs/k054539.md,
  docs/c352.md, docs/scc-ay8910.md, docs/ym2203.md and docs/ym2612.md.

## Environment

- `VGM2151FUR_FURNACE`: path to `furnace.exe`, or to a folder holding it.
  Without it the tools look for a `furnace/` folder next to the project (a
  source checkout with `build/furnace.exe` works too), then a
  `third_party/furnace-console/` or `third_party/furnace/` drop-in, then
  PATH.
- `VGM2151FUR_VGM2WAV`: path to `vgm2wav-mute.exe` for the soundcheck and
  chroma tools.
- `VGM2151FUR_OPNA_RIPS`: the folder of YM2608 and YM2413 rips
  (`F:\NASbackup\JamesAlan\_devspace\grbsd\ripwip\_finished`). Only
  `TestYm2608RealRips` and the OPLL real-rip tests read it; without it they
  skip silently, which is how a broken converter looks green.
- `VGM2151FUR_MERGED_RIPS`: the folder of merged logs - arcade chip plus OPL
  card in one file (`F:\NASbackup\JamesAlan\_devspace\grbsd`).
  `TestMergedRips` in `tests/test_vgm2151fur_combo.py` converts each one and
  exports it back through Furnace. Unset, it falls back to the grandparent of
  `VGM2151FUR_OPNA_RIPS`; if neither has a `*_merged.vgm`, the tests skip.
  See docs/merged-logs.md for what each log holds.
- `VGM2151FUR_SAMPLE_RIPS`, `VGM2151FUR_SEGACD_RIPS`, `VGM2151FUR_FMTOWNS_RIPS`:
  roots the RF5C real-rip tests search (in addition to the two above). The FM
  Towns Xenon rip lives under the OPNA root (`xenon-fmtowns\done`), and a Sega
  CD pack such as Popful Mail sits outside it, so each can be named directly.
  See docs/segapcm-c140.md for the RF5C model.
- `VGM2151FUR_Y8950_RIPS`: a folder holding the Y8950 rip the real-rip test
  converts (`03v - Credit Inserted (with voice).vgm`). Without it that test
  skips. See docs/opl.md for the Y8950 model.
- `VGM2151FUR_YRW801`: path to the 2 MB YMF278B wave ROM (a `.rom`, or a
  `.zip` containing one). The OPL4 converter embeds the ROM sample bodies
  because Furnace does not load the ROM itself. Without the variable it also
  looks for `furnace/yrw801.rom` under the project and the current directory,
  then `yrw801.rom` in the current directory. A log whose wave slots address
  ROM sample IDs loses those notes if the ROM cannot be found, and the
  converter warns.
- `VGM2151FUR_OPL4_RIPS`: a folder holding the MoonDriver OPL4 rips the
  real-rip tests convert (`05 encounter the unknown environment.vgz` and
  `06 TIME'S UP!.vgz`). Without it those tests skip. See docs/opl.md for the
  YMF278B model, the wave descriptor format and the pan table.

## Exit codes

`0` success, `1` a file failed, `2` a usage error, `130` cancelled with
Ctrl+C. `compare` returns 1 when runs were found, so it doubles as a check
in a script.
