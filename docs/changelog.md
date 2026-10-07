# Release notes

One entry per released version. The numbers are measured against the
reference renderer and the bundled Furnace build, not estimated.

Cue the song "Take Me Baby" by Mickey B.

## 0.9.35

Convert moves a VGM loop onto the downbeat when the note grid repeats every
4, 8, 12, or 16 pulses and the loop is already a whole number of those
bars. The order cut is one row before the downbeat commands, so a sample
keyed a row early still plays and the downbeat is not the `Bxx` row. A weak
grid, or a loop that is not a whole bar, keeps the VGM offset unless the
phrase that returns at the end of the track begins in the four seconds
after the marker (or inside the loop, when the loop is shorter than four
seconds). `--no-loop-find` keeps the VGM offset on every track. The convert
line reports `loop kept` or the move (`loop -1/8`, `loop 226 rows late`).

## 0.9.34

C352 conversion fixes for the three defect reports (missing samples, sample
loudness drifting, wrong notes). Each was reproduced against the source rip:

- The wave ROM reads fold through the window, the way libvgm's core does
  (`wave[pos & pow2_mask(size)]`). Namco System 11/12 drivers hand the chip
  0x80B1D1AE-style addresses (a ROM-base byte on the 24-bit offset) and the
  converter treated them as outside the dump: Tekken 2's "17 Mid Boss" lost
  1037 of 2587 triggers, including the opening orchestra hit, and Tekken
  Tag's "05 Jin Stage BGM" folded 293 more LINK targets into the window
  (link-outs 362 -> 69). A folded address that lands in an undumped hole is
  still silent and still counted.
- The pitch refit window is one 60 Hz frame (735 samples), not 120. These
  drivers strobe first and write the note's own pitch inside the same frame:
  the key-on register holds the *previous* note's frequency, so "19 Ending
  BGM" started 232 hits (up to +18.9 st) at the wrong pitch and glided;
  Ridge Racer 2 does it 1032 times per track. The first in-frame write is
  now the note; later writes stay slides.
- The first in-frame *volume* write now sets the note's balance for the same
  reason (Ridge Racer 2 writes it 8 ms after the strobe, 8694 of 9711 hits,
  and zeroes the register when it stops a voice). The write lands on the
  key-on's own row, where no volume step is placed, so the attack used to
  play at the stale level - often silence - and each hit's loudness followed
  whatever the previous note left in the register. The track's volume peak
  for the 0-15-driver scale now also counts these in-frame writes.

Measured after the fixes: Mid Boss 2587/2587 triggers play; Ending BGM
237/237 late pitches are the note; Ridge Racer 2 02 713 attacks lifted,
unity-render level recovered ~2 dB on the dense drums. The residual
`.fur`-render level against the source (Ridge Racer 2 ~-10 dB, Tekken 2
~-4.4 dB, changing with content) is the documented export/mix-metadata gap.
These fixes leave it unchanged, and the per-track fit absorbs it.

The same round handles the loop families these drivers use, both found on
Tekken Tag 05's "05 Jin Stage BGM":

- `wave_loop` one past `wave_end` (end 0x...8d, loop 0x...8e): the chip jumps
  to the contiguous byte, plays a full bank there - until the low 16 bits
  meet `wave_end` again - and repeats. The loop point used to be dropped as
  "outside the body", so the sample played its intro once and stopped.
- Bodies only carry a loop region when a hit of theirs actually reaches it:
  a note the driver stops before the running address meets `wave_end` never
  loops, so its payload stays the intro. The loop-carrying bodies are the
  notes that hold past the intro.
- Mid-note geometry rewrites (start/end/loop/bank writes with no strobe): the
  jump and the loop region are planned from the `wave_start`/`wave_loop` a
  rewrite leaves while the voice has not crossed `wave_end`, not from the
  key-on snapshot; the intro, `wave_end` and the bank keep their key-on
  values (those rewrites are stop/shorten tricks), and a post-crossing write
  belongs to the next pass. On Tekken Tag 05 the drivers rewrite start/loop
  8 ms into the note (`start=0x4cd4` / `loop=0x009b` becomes `start=0x009b` /
  `loop=0x928e`), which points the LINK jump at the byte after the intro.
  The key-on pair sends it to a far-away address that folds into unrelated
  ROM, so every note that rang past the intro played that ROM as a seamless
  loop - the "extremely distorted" tail on channel 14 (`C352 1-14`, voice
  13). Verified with a held-note fixture: fur/ref band deltas stay within
  +-0.6 dB through the intro and the loop region (the correlation past the
  intro was 0.0 before). Track 05 carries 28 bodies / 1.34 MB against 25 /
  1.25 MB before - the extra bodies are the correct regions for the notes
  that hold past the intro.

C140 carries the same one-frame pitch refit as the C352 (Starblade's "Theme
of STARBLADE" keys a voice with a stale 0x0d1a and writes 0x861a - 40
semitones up - a few ms later). Its loudness side is not affected: the C140
drivers write the volumes before the key-on write, so the stale-volume
attack family that shows up all over Ridge Racer 2 does not occur there
(0-0.6% of hits against 88% on Ridge Racer 2).

Convert and report give each track its own worker process, up to one less
than the physical core count. The workers and the Furnace renders they
start run at low priority. Furnace stays off the menu's console, so the
next prompt still takes a line and Ctrl+C.

## 0.9.33

The command line takes the path you give it.

- `python -m vgm2151fur "Song.vgz"` converts that file. A pack folder converts
  in playlist order. A folder of packs converts each pack into its own `fur/`
  folder, or into `-o/<pack>` when `-o` is set.
- Quotes from Explorer's Copy as path, a leading `&`, `file://` links, and a
  glob that cmd.exe left unexpanded (`*.vgz`) are accepted.
- `edit` and `render` find `.fur` files in the folder, in its `fur/` child, or
  one level down, so a pack folder works after a convert.
- An `.m3u` line that points at a missing subdirectory still matches the file
  of that name in the pack. Tracks the playlist does not name stay on the list.
- The menu takes a pasted path at the prompt. A file converts, a pack opens,
  a folder of packs lists them.

## 0.9.32

C352 stabilisation round over the full pack set (9 packs plus Tekken Tag,
193 modules; `tools/reconvert_c352.py` reconverts all of them now). Every
rule below was checked against the reference player with a synthetic fixture
before it was kept:

- A flags write that clears BUSY stops the voice at the write (libvgm plays
  only BUSY voices). Drivers end notes with 0x0000/0x2000 flags writes; the
  module used to play the sample out, or loop it forever. Tekken Tag "05 Jin
  Stage BGM" windows 3.50 s and 68.50 s - the module droning while the
  source is digitally silent - now match sample for sample. A clear that is
  followed by a key-on at the next strobe still stays one note.
- A scheduled natural end (loop cleared mid-note) is no longer dropped by
  the next unrelated strobe; it survives until its time.
- Reverse one-shots that start below wave_end walk down through the *bank
  boundary* into the previous bank instead of being dropped; the fixture
  render confirms the reference plays that wrapped run. A wrap that would
  leave the 16 MB window (bank 0) stays silent, as the reference does.
- Ping-pong voices that start below the bounce region keep the pre-attack
  (the fixture render confirms the reference plays it first): the body
  starts at the written start and the loop sits at the region.
- PCM slides were only written on rows where the rate *changed*; the engine
  slides only while the effect is present, so a constant-rate glide froze
  after its first row. The ramp is now written on every active row; a
  synthetic C352 staircase tracks tick for tick. Fast driver sweeps that
  ride E8xx/E9xx quick legato still land a few semitones short over seconds
  (next round).
- Pattern columns fit their content: a channel now carries one effect column
  per effect its busiest row uses (1..8) instead of the 8-column build
  capacity, so a converted rip opens at a readable width. On the 32-voice
  "13 Counterblow" the grid drops from 1248 to 416 columns.
- Patterns are 256 rows (Furnace's maximum), up from 64. A measured grid
  like Fantasy Zone II DX's 100 rows/s used to make 91 orders per channel;
  it is 23 now. The VGM loop row still forces its own order boundary, so
  loop points cannot move, and the loop jump (Bxx) still lands on the last
  content row.
- `tools/reconvert_all.py` never re-passes a stored speed; every track
  reconverts on the estimator's grid. `--speed N` is the legacy manual path
  and forces hz to 60, so feeding a measured grid's speed back through it
  collapses the row rate: Fantasy Zone II DX "03 Start ~ Cholacoray" went
  from 500 Hz / speed 5 (100 rows/s) to 60 Hz / speed 5 (12 rows/s), and
  Assault 04 from 497.7 Hz / speed 8 (62 rows/s) to 7.5 rows/s - notes
  missing or misplaced, vibrato and sub-row detail swallowed. The estimator
  reproduces the stored grids (checked against the hand-kept
  `fantasy-zone-2-dx-old` copies and the sources via `vgmdiff`; FZ II DX 03:
  3023 note rows, 500 Hz / speed 5; Assault 04: 6053 note rows and 41248
  effects against 5380 / 16471 pinned). A track that truly needs a manual
  pin converts alone with `--speed N` afterwards.
- Converted tracks are peak-normalised to -1.5 dBFS. The converter renders each
  finished module once through the Furnace console at a quarter master volume
  with f32 output (the render clamps at full scale, so a hot module cannot be
  measured at unity), reads the peak from the WAV, and writes the song master
  volume that leaves 1.5 dB of headroom. Loud rips that clipped in Furnace
  playback get real headroom and the quiet mu-law family comes up as one
  master gain; the per-chip balance is untouched and the VGM export's global
  volume byte follows the master volume. The CLI and the menu convert with
  the fit on; `--no-normalize` skips the render. `tools/reconvert_all.py`
  reconverts every pack under `output/` on the estimator's grid.
- The repo bundles the headless console build of the patched Furnace
  (`third_party/furnace-console/furnace.exe`, ~4 MB), so a clone converts
  and measures C352 packs without building Furnace first. A build that
  cannot load a module - stock 0.6.8.3 refuses `0xD0` with "unrecognized
  system ID" - makes the converter skip the volume fit with a warning
  instead of measuring silence; every other chip family loads on a stock
  build.

Checked and rejected in this round: masking sample addresses into the 16 MB
ROM window. A fixture with a base (and separately a LINK target) beyond the
window rendered silent in the reference, so the pre-0.9.32 rule stood. (That
reading was wrong - the fixture's fold point was a hole, not a dumped slice;
0.9.34 applied the fold and Tekken 2 17 Mid Boss recovered 1037 triggers.
See the 0.9.34 entry.) The same fixture technique confirmed the reverse-wrap,
ping-pong, BUSY-clear and slide fixes.

Canary battery after the round (regdiff): hit counts equal or +0..+9 from
retrigger splits, bodies shared (apart from LINK folds), frequency medians
+0.11%, flags equal apart from LINK->LOOP. Soundcheck medians 0.88-0.99 with
shift 0 and equal clocks. The mu-law/16-bit level family and the E8
quick-legato sweep deficit stay open (see docs/testing-pipeline.md).

## 0.9.31

C352 audit round, run against a local Furnace build with a working `0xD0`
core. A module with no FM key-ons no longer carries an empty YM2151: the
dead chip added a second device to every export, and the sample chips now
shift down into the missing FM block, which restores hits on voices 24-31
(SoulCalibur "The New Legend": 29 of 41 samples and 2886 of 4457 hits
before, all of both after). C352 restarts feed the same row tightening as
SegaPCM (Tekken Tag track 05 keeps all 61 hits of its drum roll, row 364
samples). A LINK voice whose target falls outside the dumped ROM now
plays the intro once instead of looping it (the chip reads silence past
the target, so the module matches an ambient the source lets go quiet;
362 such hits on track 05). The module also runs to its last fade or chip
write, not just its last note, so a note keyed at zero gets its ramp
instead of ending with it. The core exports VGM now - command `0xE1`,
ROM block `0x92`, divider byte 72, the frequency register written before
the key-on strobe - so `fur2vgm.py` and `fur_soundcheck.py` audit a C352
conversion like any other chip: register diff 1712/1712 hits, frequencies
within 0.11% (C-4 rate rounding), flags equal apart from LINK folded to
LOOP, bodies byte equal. The `.fur` render of the pack sits at 0.75-1.00x
the source render. See docs/c352.md.

## 0.9.30

Namco C352. Command `0xE1` and ROM block `0x92` become 32 channels per
chip on system `0xD0`, with Amiga instruments (type 4). Header 24192000
with divider byte 72, the same crystal with divider 0, and legacy 336000
with divider 1 all resolve to 24192000. An extra-header clock stays a
crystal: 12000000 beside divider byte 36 stays 12000000. Frequency
`0x8000` at that clock plays at 42000 Hz. A one-shot from `0x100` to
`0x1C8` is 200 frames and omits the end address; a loop to end `0x1C7`
includes that byte and loops at offset `0x20`. Key on and key off wait
for the `0x202` strobe, and a second strobe does not retrigger. Mulaw
byte `0x01` expands to int16 32. Chip volume is the player default 0.25
(0.125 with two instances). Two chips land on channels 8 and 40.
Furnace 0.6.8.3 does not emulate `0xD0`, so this was checked with the
unit tests only. See docs/c352.md.

## 0.9.29

Slides now accrue the movement the engine makes, not the ideal rate. A glide
that wants 2.66 units per tick is written as `01 03`, so the engine runs
about 13 percent fast while the model credited only 2.66. The error
compounded over long climbs and left held notes on the wrong pitch: Hyper
Duel's three +5 st ramps ended 31, 65 and 111 units sharp, up to 1.7 st, and
stayed there for seconds. The model now adds the written parameter per tick,
so the E5 pins only clean up sub-unit residue.

## 0.9.28

E5 pin scale on PCM. One E5 byte step is 1/64 st on the FM platforms but
1/128 st on PCM (E5 0x40 sits exactly -0.5 st below 0x80 on the C140), so
every PCM fine-tune pin was applied at half strength. That was the
percent-level "99/101%" family of pitch offsets. Pin offsets now convert per
platform.

## 0.9.27

Instant pitch jumps via quick legato. A move bigger than the slide can
deliver inside a row used to slur: the rate caps at 2 st per tick on PCM and
4 st per tick on FM. Driver register jumps, dive attacks and attack bends
landed late by a few rows. Furnace's E8xx/E9xx adds semitones to the playing
note in one write and holds until the next note, on both the C140 and YM2151
platforms. The sweep writer lands the overshoot with E8/E9 and lets the
slide cover the sub-semitone remainder.

## 0.9.26

PCM slide scale and FM RL cuts. One slide parameter unit moves 1/128 st per
tick on PCM platforms, half the FM rate, so PCM glides ran at half speed and
stopped short: Cyber Sled's Silent Fight descends 3 st per row in the source
and did 1.5 st here. The written parameter is doubled now, with the ideal
rate capped at the engine's 255. A mid-note RL/FB/CON write that clears the
output bits cuts the channel on hardware; Silent Fight ends its keyed sirens
that way, which is why two FM channels rang to the end of the track. Such
writes emit the note-off.

## 0.9.25

PCM note rows keep their pitch. A PCM sweep's note row carried its first
slide rate, and Furnace applies part of that travel inside the key-on write.
Attacks landed rate/128 semitones off, measured +2.0 st at rate 0xFF on
Cyber Sled 02 and -2.0 st on Valkyrie "Prologue" dives. PCM ramps start on
the row after the note now; the pins and the final stop still land the
trajectory. FM sweeps keep their measured behavior.

## 0.9.24

Played-frequency references. Drivers that key a voice on before writing its
frequency left zero or stale values in the key-on snapshot, and the sample's
reference rate came from the most common raw key-on value. A sample keyed
that way got the (108, 0x80) fallback note and a 1000 Hz rate: wildly wrong
pitch that chroma checks cannot see, because a 5-octave error reads as a
small rotation. Valkyrie "Prologue" had a 24-hit sample like that. The
reference now uses the played frequency and ignores zeros.

## 0.9.23

C140 fade-in keys. A hit keyed with both volume registers at zero used to
write pan 08 00 ("both off") from the zero balance, which no later volume
step can undo, so the note stayed silent. Starblade had 94 hits like that,
Cyber Sled 45, Valkyrie 16 and Mirai Ninja 5. The first trigger of a voice
is the common case, because the registers still hold zeros. Such hits start
silent on the default balance now, and the volume steps carry the L/R pair
as balance updates. True zero-register mutes keep the 08 00.

## 0.9.22

C140 mid-note writes. Volume-register steps while a voice is keyed become
volume-column updates. Software fades survive. Frequency-register steps
become pitch sweeps through the same ramp encoder as the FM sweeps.

## 0.9.21

Chip output levels. The `.fur` chip volumes now follow the reference
player's mix: libvgm defaults, the file's extra-header entries, the C140
core patch, and instance division. SegaPCM 1.5, C140 2/3 times 0.7 against
Furnace's core, MSM6295 honoring its entries (Hyper Duel's 0.375). Reconvert
the sample packs.

## 0.9.20

C140 key-offs and legacy type. Mode bit 7 clear emits a note-off now, so a
looping voice releases instead of ringing until the next note; Starblade had
about 1400 stuck voices per track. Pre-1.61 files that log the legacy 21390
rate get the System 21 address mapping. Starblade used to read as System 2:
track 01 covered 1598 of 1679 triggers, track 02 covered none. Both complete
now.

## 0.9.19

SegaPCM cursor. Registers 0x84/0x85 are the live playback address. A CPU
rewrite of the same start after the cursor has moved is a new note: Galaxy
Force frame grains, Out Run one-shot replays. 0.9.18 kept a note only when
the written start, the bank or the enable bit changed. Those retriggers
were dropped. One Z80 register dump, address then end and delta then
control within 4 VGM samples, is one note.

## 0.9.18

C140 pitch. Rates target the reference step rate (freq * clock / 18874368,
that is clock/288 with a 16.16 position) and the Furnace clock flag carries
2/3 of the crystal, so `.fur` playback, the exported clock and the exported
registers all match the players. 0.9.17 played 1.5x high and exported
18432000 where the source says 12288000.

## 0.9.17

SegaPCM bank (header 0x3C/0x3E) and C140 byte address handling, including
mulaw-16. 0.9.16 paired C140 addresses as words and ignored the SegaPCM
bank. Bodies were silence or scrambled.

## 0.9.16

SegaPCM (16 voices) and Namco C140 (24 voices) sample support: 0xC0/0xD4
writes, 0x80/0x8D ROM blocks, loop points, per-chip instruments.

## 0.9.15

The grid estimator folds over-long measured units back into the 16th-note
band. Sega System 16C slow tracks got a 15 rows/s fallback and could not
carry per-frame pitch writes.

## 0.9.14

OPM clock-ratio note transposition. K007232 levels are linear in the
register with the rip's own mix. Pan carries both sides, because 08xy is two
levels. The converter version is stamped into every `.fur` comment and into
`--report` output. Stale files stay visible.
