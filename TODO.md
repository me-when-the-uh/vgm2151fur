# TODO

Open follow-ups from the OPL merge and the current chip models. The OPL
family is merged into master. These items are not blockers for that merge.

## Mixed OPL + YM2203 logs lose one chip's music

(Found in: Bubble Bobble (Arcade) track 02 "Hurry! ~ Main Theme (faster)".)

Track 02 contains 22,088 OPL writes and 870 YM2203 writes. `analyze` sets
`is_opn` whenever a YM2203 write exists and clears `opl_kind`, and the
converter writes the YM2203 side only. The module has 12 FM notes and no
OPL main theme. The pre-merge master did the same. The warning names the
loss, but the track is not a conversion of the source.

The grid makes the difference worse. The 1x conversion reports speed 5,
while the pre-merge master reports speed 2. The analyzer measures the OPL
key-on lattice (1,821 intervals) even though the OPL events are dropped,
and the 12 YM2203 notes land on a grid that belongs to the other chip.

The README and `docs/opl.md` say a mixed log is converted on the family
that plays most of it. The code gives the arcade FM chip priority in every
mix, and that statement is false for OPL + YM2203.

Work:

- Select by audible content first. Count key-ons, held time, or FM notes
  per family. Print both counts and the selected family in the warning.
  For track 02 that means either the OPL main theme with a documented loss
  of 12 OPN SFX notes, or a clear report that the dominant side is being
  dropped.
- Better, support two FM families in one module. The writer already
  carries a chip list and a channel offset. Add a second system for an
  OPL + YM2203 track, map both collectors onto separate channel bases, and
  keep both instrument lists and mix entries. Check the `.fur` system
  table and Furnace's multi-chip playback first.
- Feed the grid estimator only with the selected family's key-ons. A
  YM2203-only fallback must keep the pre-merge master's grid.
- Correct the README and `docs/opl.md` wording, or implement the dominant
  family rule they describe.
- Add a diagnostic that prints per-family writes, key-ons, and selected
  family. A dropped family must never be silent.

Related OPL audit gaps seen on the same pack:

- The strict `TestOplRealRips` helper, run manually on the pure OPL
  Bubble tracks, finds attacks at the end of the exported tail on melodic
  channels. The test's seam allowance only covers rhythm channels.
- Pitch cells land up to about 2.5 cents from the source. The pitch table
  documents this rounding, and `compare` does not report it.

Tests:

- A synthetic mixed OPL + YM2203 log with a known dominant family. Assert
  the selection and the warning.
- Bubble Bobble track 02 as a real-rip case. Assert that the selected
  family is the dominant one, or that both families survive in one
  module. Assert that the grid uses the selected family's key-ons.
- The seven pure OPL Bubble tracks in the real-rip audit, with the
  documented pitch-cell tolerance and a melodic seam allowance. If the
  writer is fixed instead, the audit can keep its exact rule.

## YM2151 legato pitch changes become slides

Proposal from another user: "also need a better heuristic for notes that
change pitch in legato instead of using pitch slide commands".

What happens today:

- `analyze._finalize_sweep` attaches every mid-note KC/KF write to the
  held note as a `sweep` when peak-to-peak is at least 4 kc/kf units
  (about 6 cents). It merges the driver's KC/KF pairs and refits the note
  pitch from the first pair after key-on.
- `furwrite._emit_sweep` samples the trajectory at row boundaries and
  writes `01xx`/`02xx` ramps. E5 pins the sub-semitone residue. E8/E9
  quick-legato transposes are used only when a move is larger than one
  row's slide can deliver.
- A single legato step is therefore ambiguous. One KC/KF change that
  holds until the next key-on looks like a short trajectory. A modest
  step is written as a row ramp, and the source's jump is heard as a
  slur.
- The E8/E9 machinery already exists. `_emit_steps` is the short
  held-step encoder used by the OPN and PCM paths, and `pitch_steps` is
  the matching event field. The YM2151 path does not feed it.

Work:

- Classify each held-note KC/KF trajectory before writing it:
  - vibrato or periodic motion: keep the current ramp or macro.
  - a staircase with many steps or continuous writes: keep the ramp.
  - a legato step: one or two changes, and the last value holds for at
    least one row or a fixed tick count. Encode it with `_emit_steps`
    (E8xx/E9xx plus E5) on the row after the change.
- A step at a row boundary can go straight to E8/E9. A mid-row step can
  use a short ramp and place the transpose on the next free row. Respect
  `legato_skipped`: an E8/E9 must not replace a later note's own effect.
- Do not classify a release-tail write as a legato note. The current
  sweep keeps moving after key-off until the next key-on. Use the key-off
  time and the envelope tail to separate a release glide from a
  held-note retune.
- Keep the instrument pitch macro for smooth motion. E8/E9 should only
  carry a step that holds.
- Add a counter for single-step sweeps, and for sweeps written as ramps
  instead of transposes. The digest should make the remaining slurs
  visible.

Tests:

- A synthetic YM2151 note with one held KC/KF step of +1, +2, +7, +12
  and -12 semitones. Assert E8/E9 plus E5, then round-trip through
  Furnace and compare the KC/KF stream at the step.
- A staircase glide and a vibrato must stay ramps or macros. A release
  tail write must not become a legato transpose.
- A real YM2151 rip with a single-step legato change. Vulcan Venture's
  sweeps are multi-step percussion bends (2 to 35 steps in the scanned
  rows, with spans of several semitones). That pack does not cover this
  case. Find a melodic example or keep a synthetic fixture in the suite.

## Meter-aware loop seeking (4/4 and 8/8)

Proposal from the project owner: "work on the loop seeking feature further,
to understand what a 4/4 and 8/8 rhythm is, since hardly any VGM rip has
perfectly placed loop points - the loop points are not placed perfectly,
and sometimes it's off by several seconds".

What happens today:

- `loopfind._clock` takes the most regular inter-onset gap (3 to 96 rows)
  as a pulse. `_period` scores repeating cells at 4, 8, 12 and 16 pulses.
  `_whole_bars` accepts a loop length that is a whole number of those
  cells. `_phase_shift` votes a downbeat from ensemble and lead entries.
  `_entry_shift` moves to the lead's entry. `_phrase_seam` searches for
  the recording's copy one declared loop length later. `_seat_seam` moves
  a row or two for an exact seam.
- The search window is four seconds of rows, or the whole loop when it is
  shorter (`apply_loop`). `--no-loop-find` keeps the marker.
- There is no meter model. The fixed 4/8/12/16 pulse ladder does not know
  whether a cell is one 4/4 bar, one 8/8 bar, two bars of either, or a
  phrase. The clock comes from note spacing and not from accents. A
  marker more than four seconds off can fall outside the search.
- The finder already moves markers by seconds on real packs:
  - Vulcan Venture 15 Fire Dragon: 249 rows at 640.5 samples/row, about
    3.6 s.
  - Metal Slug 3 12 Hard Water: 348 rows at 462.5 samples/row, about
    3.65 s.
  - Metal Slug 3 26 Final Attack: 380 rows at 429.5 samples/row, about
    3.70 s.
  - Vulcan Venture 08 Crystal World: 97 rows at 714.2 samples/row, about
    1.57 s.
  These sit near the four-second cap. A larger error is not searched.

Work:

- Measure the real error first. For every reference pack track, record the
  header loop sample, the selected row, the row length, and the offset in
  seconds. Report the distribution and the tracks where the listener says
  the loop is wrong. This gives the meter work a target.
- Build an accent matrix from every voice, not only the lead. Use note
  density, bass motion, chord changes, and drum hits. Reuse `_clock` for
  the pulse, then score meters:
  - 4/4: four quarter-note beats, strongest accent on beat 1, secondary
    on beat 3.
  - 3/4: three quarter-note beats.
  - 6/8: two groups of three eighth notes.
  - 8/8: eight eighth-note beats, grouped 3+3+2 or 4+4, with the
    secondary accent at the group boundary.
  - 12/8: four groups of three eighth notes.
  Pick a meter and a bar phase only when the best score clears the
  runner-up by a margin.
- Replace the fixed 4/8/12/16 pulse periods with bar lengths from the
  meter. Enumerate phrase lengths of 1, 2, 4 and 8 bars. A 4/4 bar and an
  8/8 bar can both contain eight eighth-note pulses. The accent grouping
  is what separates them, and the period search must not treat equal
  pulse counts as equal cells.
- Search phrase heads over a window of at least one phrase, not a fixed
  four seconds. Keep the declared loop length and the copy-one-length-
  later seam test. Use the recording's own repeat to find the head when
  the copy exists. Fall back to the meter phase and the header offset
  when the copy is truncated or the confidence is low.
- Handle pickups and markers inside a phrase. The current `_entry_shift`
  and `_snap_empty_marker` are a start. The meter phase should decide
  whether a lead entry is a pickup or the downbeat.
- Report the detected meter, bar length, phrase length, marker offset and
  confidence in the report or a diagnostic command. Do not move the loop
  when confidence is low.

Tests:

- Synthetic 4/4, 3/4, 6/8 and 8/8 patterns, with 8/8 in both 3+3+2 and
  4+4 groupings. Shift the marker by a beat, a bar, two bars and several
  seconds. Assert the chosen head, the kept length and the meter.
- A 4/4 and an 8/8 pattern with the same pulse count must choose
  different accent phases. A marker inside a pickup must not be treated
  as the downbeat.
- Real-pack cases: Vulcan Venture 15, Metal Slug 3 12 and 26. Assert the
  loop opens on the detected phrase head and not only on a pitch-class
  repeat.
- Keep `--no-loop-find` and the low-confidence path unchanged.
