# YM2612 DAC: deduplicated slices for FM6

Status: **implemented**. The continuous 22.05 kHz timeline remains available
through `collect_dac(vgm, timeline=True)` for comparison, but the converter's
default is the sliced path described here. Read `ym2612.md` for the chip
registers and `implementation-notes.md` for the stabilisation loop.

## What the converter does

`vgm2151fur/ym2612dac.py` has one canonical decoder and one scheduler:

1. **Decode.** Old `0xE0`/`0x80`-`0x8F` streams, direct `0x2A` writes and
   modern `0x92` rate plus `0x95` block plays become one ordered event stream.
   Writes while `0x2B` bit 7 is clear are inaudible and are skipped; the read
   cursor still advances, so the next enabled byte lands at its source
   address. A log with no `0x2B` write is treated as DAC-enabled.  A group of
   bytes at one VGM timestamp is a driver burst; only its final value sounds
   in the reference player, so it becomes one byte and the dropped
   intermediate values are counted in the warning.
2. **Slice.** A *run* is a maximal stretch of bytes with no pause. A pointer
   change does not split it; only a gap between the previous byte's wait and
   the next byte does. The run rate is
   `44100 * (bytes - 1) / (last_time - first_time)`, so the bytes play at the
   driver's own write rate instead of a fixed timeline. A run whose bytes are
   all equal stores one frame and relies on the DAC's hold. Runs are
   deduplicated by `(bytes, rate)`.
3. **Place.** The source is monophonic, but a tracker row carries one note on
   FM6. Runs whose starts land in the same row are merged into one sample:
   their bytes stay in source order, and the gap between them becomes an
   explicit run of the previous byte. The note is placed on the preceding
   tick (`place_tick(..., floor=True)`) and the sample begins with the value
   the DAC was holding, so the audible content starts at its source frame
   instead of the row boundary. Runs in later rows keep their own notes, so
   the stream stays slice-editable. One note per row is the only constraint;
   no run is dropped.
4. **Emit.** Slices are grouped 120 per `DIV_INS_AMIGA` sample-map instrument.
   A pattern note `60 + slot` selects a slice and is never musically
   transposed. The sample's C-4 rate is compensated for Furnace's reference,
   so the exported `0x92` rate matches the intended rate within 1 Hz of
   integer rounding. Pan comes from `0xB6` at each note; a pan change inside
   a row becomes an `08xy` channel effect at the row start, because an ED
   delay on that cell would move the note too.

Warnings are emitted for a disabled DAC, a rate clamped to 192,000 Hz, a
multi-value burst at one VGM timestamp (collapsed to its final value, because
the reference player writes them at one output sample), a read past the bank,
an overshoot when a merged sample cannot fit, and the 250-instrument /
65,535-sample limits. A source with no usable DAC bank keeps the old warning.

## What the timeline did (historical)

The old path rendered one continuous 22.05 kHz sample per phrase. It was
accurate for the existing Contra set and passed the old byte/rate checks, but
it stored every held and silent frame, was not slice-editable, and forced a
separate `_stream_mode` heuristic. It is now a comparison fallback only.

Current measured cost (timeline vs slices, full-track direct write, zlib
level 5):

| Measure | Timeline | Slices |
| --- | ---: | ---: |
| Contra pack, raw `.fur` total | 46.53 MB | 30.07 MB |
| Contra pack, zlib level 5 total | 12.85 MB | 7.30 MB |
| Largest Contra module | 1.29 MB | 0.75 MB (track 20) |
| Track 04 source bank | 132,055 bytes | 132,055 bytes |
| Track 04 stored sample bytes | 1,206,705 | 444,425 |
| Track 04 stored / bank | 9.1x | 3.4x |
| Track 04 audible source bytes | 426,320 | 444,425 stored (1.04x) |
| Track 20 stored sample bytes | 4,110,624 | 1,501,683 |
| Track 20 source bytes | 1,448,979 | 1,501,683 stored (1.04x) |
| vgm2fur Contra baseline | 2.31 MB total | 237 KB largest |

The pack is still larger than vgm2fur's 2.31 MB because every source byte and
every source onset is kept, and same-row runs are merged into
context-specific samples. It is 1.76x smaller compressed than the timeline
and 1.55x smaller raw, while the stored sample bytes are roughly the source
played bytes instead of a 2.2-3.8x materialised timeline.

Reference tracks (compressed `.fur`; sample bytes; slices; maps; notes):

| Track | Old z5 | New z5 | Old sample | New sample | Slices | Maps | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 01 Konami Music (DAC disabled) | 1,750 | 1,466 | n/a | 0 | 0 | 0 | 0 |
| 03 Charshoe X | 360,004 | 245,986 | 938,389 | 326,053 | 4,017 | 34 | 4,636 |
| 04 Contra Overdrive | 367,285 | 248,959 | 1,206,705 | 444,425 | 3,399 | 29 | 4,341 |
| 07 Alien's Den | 712,283 | 342,737 | 1,775,209 | 689,831 | 7,193 | 60 | 7,551 |
| 14 Guillotine | 431,294 | 234,910 | 1,199,495 | 507,539 | 3,248 | 28 | 3,260 |
| 20 Moonlit Army | 1,289,422 | 753,355 | 4,110,624 | 1,501,683 | 13,044 | 109 | 15,251 |

Full pack: 154,155 slices, 15.94 MB of stored sample bodies, 179,831 DAC
notes, 240,278 source runs. The largest map count is 109 (track 20), which
leaves room for the FM instruments under the 250-slot limit.

## Accuracy evidence

The acceptance run is `scratchpad/full_audit.py` in the development session;
the durable checks are the tests in `tests/test_vgm2151fur_ym2612.py` and
`audit.compare_keyons`.

Full Contra pack (29 tracks, direct write, every module exported through
Furnace):

- **45,500 / 45,500** source `0x28` key-ons matched; **0 missed**.
- **0** effective FM operator-field mismatches.
- **0** divergent pitch runs at 0.25 semitone / 60 ms on any track.
- **179,831 / 179,831** DAC notes matched to an exported `0x95` start within
  100 ms.
- **0** missing sample bodies: every stored slice body appears in the export.
- Sample-map selectors match every note; every block body is byte-identical
  to its slice after the signed/unsigned boundary.
- Exported `0x92` rates match the intended slice rate exactly or one hertz
  low because of integer C-4 rounding; the rate check reports 0 bad rates on
  every track.
- Tracks 09, 10 and 13 have extra exported starts from the module's loop
  replay. No source note is missed (`dac_missed = 0` on all 29 tracks).
- The old timeline path is retained for comparison; the source bytes and the
  source-versus-export audit are the authority, not the old module bodies.

Strict end-of-track check: five notes (tracks 03, 06, 10, 13 and 22) are the
last run of the track and fall outside a ±2-sample start check against the
export's final length. They are inside the 100 ms audit window and their
sample bodies are present; the module's last row is cut by the source's end,
not by a collision. Keep this in mind if a future change needs frame-exact
last-note starts.

Audio on Contra track 04, using the project's `fur_soundcheck.py` with
`vgm2wav-mute`:

| | best shift | chroma median (fur) | chroma median (export) |
| --- | ---: | ---: | ---: |
| Timeline | 0 | 0.995 | 0.996 |
| Slices | 0 | 0.996 | 0.996 |

The full-mix 5-30 s comparison (same script, gain-normalised) is within
0.008 envelope correlation and 0.05 log-spectral distance of the timeline:
new fur 0.952 / 3.03 / RMS 0.709 vs old 0.960 / 3.14 / 0.714; new export
0.957 / 0.333 / 0.397 vs old 0.962 / 0.281 / 0.400. The project's chroma
check, which is the acceptance tool, is equal or slightly better.

## Acceptance tests added

`tests/test_vgm2151fur_ym2612.py::TestYm2612DacSlices`:

- direct `0x2A` stream becomes one slice;
- a 4,410-sample gap stores no frames;
- 32 identical bytes collapse to one frame + the DAC hold;
- two runs in one row merge without a note collision;
- 130 distinct slices produce two sample maps and two instruments;
- a pan change between rows reaches the note; a pan change inside a row
  becomes a channel effect;
- a modern `0x92`/`0x95` block play is a slice, and a repeated block is
  deduplicated;
- a disabled DAC leaves FM6 key-ons as FM notes;
- a same-time multi-value burst keeps only the final value and warns;
- a Furnace export round trip matches every start and body.

The existing dense-path test now expects the source rate (11,025 Hz for a
four-sample wait) instead of the old 22,050 Hz timeline, and a separate test
keeps the explicit `timeline=True` fallback working for comparison.

## Why the merge is necessary

A tracker row has one FM6 note. The source's DAC bytes arrive every 3-5 VGM
samples, so one note per source run is impossible. The old timeline avoided
the problem by embedding the whole phrase in one sample, at the cost of
materialising frames. The sliced path merges only runs that share a row,
keeps the source bytes, and stores the inter-run gap as an explicit hold.
The note is on the preceding tick and the sample starts with the held byte,
so the merge does not move the audible content to the row boundary.

## Remaining limits

- A group of distinct bytes at one VGM timestamp cannot be timed; only the
  final value is kept and the dropped bytes are counted in the warning. The
  Contra set has no such audible burst (its initialization bursts are silence
  or happen while the DAC is disabled); Sonic Emerald Hill has one 1,612-byte
  burst and reports it.
- A source that disables the DAC mid-phrase stops new DAC slices, but there
  is no explicit DAC note-off: the last sample can hold its final value until
  the next FM6 key-on. The reference packs do not toggle mid-track; add a DAC
  note-off at the 0x2B transition if a future source needs it.
- Five Contra tracks (03, 06, 10, 13, 22) have their final DAC note just
  outside a ±2-sample start check against the export's last sample. They are
  inside the 100 ms audit and their bodies are present; the cut is the
  source's end, not a collision.
- Tracks 09, 10 and 13 have extra exported starts from the module's loop
  replay; no source note is missed.
- The old timeline is retained as `collect_dac(..., timeline=True)` for
  comparison; it is not the default and should not gain features.
- The pack remains larger than vgm2fur because every source byte and onset is
  kept and same-row runs are context-merged. That is the deliberate
  accuracy-over-size choice.

## Do not do this

- Do not re-introduce a 22.05 kHz timeline as the default. It is a comparison
  fallback only.
- Do not lower the sample rate or use 4-bit samples to save space.
- Do not transpose a sample-map selector; `60 + slot` is not a pitch.
- Do not add a second sample-map or instrument-index allocator inside
  `ym2612dac.py`; the writer's shared layout is the only source of truth.
- Do not skip a run because its row already has a note. Merge it, or report
  an overshoot; a silent drop is the old vgm2fur failure mode.
- Do not ignore `0x2B`: a disabled DAC must not create phantom FM6 notes.

## Related files

- `../vgm2151fur/ym2612dac.py` — decoder, run collector and row grouper.
- `../vgm2151fur/analyze.py` — DAC notes, instrument bases, disabled-DAC
  handling and the report.
- `../vgm2151fur/furwrite.py` — sample maps, sample-rate compensation and
  FM6 encoding.
- `../vgm2151fur/vgm.py` — DAC command/data-block parsing.
- `../vgm2151fur/audit.py` — source/export key-on, DAC start and rate audit.
- `../tests/test_vgm2151fur_ym2612.py` — synthetic slices and Furnace round
  trips.
- `ym2612.md` — register model, current audit numbers and known limits.
- `implementation-notes.md` — evidence loop and anti-WET rules.
