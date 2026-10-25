# Implementation and stabilisation notes

This is the cross-cutting guide for changing `vgm2151fur` without losing the
behaviour the existing packs already prove. Chip-specific facts live in
`opm-notes.md`, `ym2203.md`, `ym2612.md`, `segapcm-c140.md`, `c352.md`,
`scc-ay8910.md` and `psg-open-items.md`. Read the relevant chip file before
touching its state machine; read this file before deciding where a change
belongs.

The lessons below come from the long harness run that turned the YM2151 path
from a collection of special cases into a first-class converter path. They are
as much about evidence and boundaries as about OPM registers.

## Where a change belongs

```
VGM bytes
  vgm.py             command parsing, chip writes, data blocks, clocks
    |
analyze.py           chip state machines -> Song, NoteEvent, FMPatch, samples
  ym2151 / ym2203 / ym2612 / sn76489 / pcm / ...
    |
furwrite.py          Furnace blocks; instrument, sample-map and pattern layout
    |
furio.py             reader / linter; audit.py and diag.py verify the result
```

Rules that keep the pipeline stable:

- Parse raw bytes in `vgm.py`. Do not reinterpret a VGM command inside a chip
  writer.
- Turn register state into musical events in `analyze.py` (or the chip module
  it calls). The writer must not decide when a note starts, what a key-on
  means, or how a sample is sliced.
- Encode `Song` in `furwrite.py`, through shared helpers. A new chip gets a
  new state machine and a new encoding path, not a copy of an existing one.
- Put verification in `audit.py`, `diag.py` and `tools/`. If a claim cannot be
  measured from the source and the exported module, it is not a regression
  test.
- A fix in a writer for a problem created in `analyze.py` is usually WET and
  will not survive the next feature.

## The stability loop

1. **Reproduce on a real track.** Run `report`, convert it, then compare the
   source with a Furnace export. Use one track that is small enough to iterate
   on and one track that is dense enough to expose collisions.
2. **Classify before editing.** Decide which layer owns the failure:
   parser (wrong write/data), state machine (wrong note/key/sample), pitch
   mapping, row placement, instrument identity, or encoding.
3. **Write a failing synthetic test.** Reduce the case to raw register writes
   or a raw VGM body. The test must fail for the reason observed in the real
   track, not merely fail to parse.
4. **Fix the owning layer.** Re-run the synthetic test, then the whole suite.
5. **Re-convert the reference pack.** Do not hand-edit generated modules.
   `fur-fixed` and `output/` are artifacts; the converter is the source of
   truth.
6. **Audit the export.** A clean unit test does not prove a clean module. Use
   `tools/fur2vgm.py`, `tools/vgmdiff.py`, `tools/vgmchroma.py` or
   `tools/fur_soundcheck.py` as appropriate.
7. **Record the result.** Update the relevant chip doc and the comparison
   table. If a known approximation remains, say so in the warning text and
   in the doc.

The useful order is synthetic -> real track -> whole pack. A full-pack pass
catches instrument-count limits, collisions and timing cases that a single
hand-written test cannot.

## Evidence hierarchy

Use the strongest source available, not the easiest one:

1. **Source write stream / data blocks** — key-on times, KC/KF, DAC bytes,
   sample addresses. This is the specification.
2. **Furnace VGM export** — proves what the module actually writes. Export
   headlessly; do not trust a GUI export taken after playback.
3. **Decoded PCM / pattern data** — proves sample bodies, note selectors,
   operator fields and pan.
4. **Rendered audio** — proves the result is audible and catches clock and
   level errors that register diffs cannot. RMS alone is not enough; use
   envelope correlation, spectral distance and channel-isolated renders.

A conversion is not "verified" until the source and the exported module are
compared on the same timeline. A listening check is the final check, not the
only one.

## What made the YM2151 path stable

The OPM path became reliable when these distinctions were made explicit:

- **Clock is not pitch.** Furnace's `customClock` flag changes the chip clock,
  but Furnace keeps a note at its musical frequency at any clock. The
  converter therefore writes both the source clock and the note offset
  `12 * log2(source / 3579545)`; otherwise a 4 MHz System 16C rip plays flat
  while the exported registers look correct.
- **A note is a state transition, not every write.** `0x08` key-ons are
  compared against the previous KON state. Redundant key-ons do not restart
  an envelope. The same rule applies to OPN masks and to sample playback.
- **E5 is channel state.** Furnace keeps the last `E5xx` fine tune until it is
  changed. Re-send it whenever a note's desired value changes, including back
  to the neutral `0x80`. Writing only non-neutral values leaks one note's
  tuning into the next.
- **The row grid is output, not source.** The measured lattice and per-row
  `EDxx` placement exist so a non-frame-aligned driver still lands where the
  source wrote it. Do not "clean up" a note onto a 60 Hz row.
- **Instrument identity is part of the music.** OPM operators are stored in
  YM2151 register order (`0x40, 0x48, 0x50, 0x58`). OPN register pages and
  KON mask bits use different orders. Algorithm 4's carriers are not the
  obvious pair. Carrier TL is normalised into the volume column; KVS must let
  the volume column affect real carriers only. These are not cosmetic
  choices — they change gain and timbre.
- **The OPM LFO is seeded by hardware reset.** Furnace's arcade core defaults
  PMD/AMD to `0x7f`; the chip resets them to 0. Send the reset before row 0,
  then let the source's own `0x19` writes override it.
- **Pitch movement needs boundary sampling.** `01xx`/`02xx` ramps are rated
  from the trajectory at the row boundary; a smoothed fit smears attacks and
  overshoots dives. A step at the row start becomes `E5`; a step inside the
  row is the ramp's first segment.
- **State that survives a row must be replayed.** Fine tune, pan, LFO
  sensitivity, sample selection and volume-column state are persistent in
  Furnace. The source's per-note writes are often the only thing that resets
  them.

The detailed register and sweep rules are in `opm-notes.md`. The point here
is the pattern: most "pitch bugs" in the harness turned out to be state or
identity bugs, not a wrong formula.

## What went wrong before, and what to do instead

These are concrete failure modes worth checking first:

- **Double PCM conversion.** The DAC bank is unsigned; converting it to signed
  twice turns silence into a `-128` rail and clips the waveform. Convert once,
  at the bank boundary, and keep the signed bytes through sample writing.
- **Pan encoding.** Furnace's split-pan effect uses one nibble per side. YM2612
  `0xC0` means both sides enabled and must be written as `08 FF`; `08 C0` is
  interpreted as hard left. A correct-looking register value can still be the
  wrong tracker effect.
- **Transposed sample-map notes.** A DAC note selects an Amiga sample-map
  slot; it is not a musical pitch. Transposing it selects a different (or
  empty) slot. Only transpose real FM notes.
- **Instrument-index drift.** Appending FM pitch macros or carrier variants
  can insert instruments before sample/tone instruments. Rebase every DAC,
  PCM and tone instrument index after the append.
- **Stale output.** The module comment carries the converter version. If it
  differs from the code's version, the `.fur` is stale. Reconvert before
  comparing.
- **GUI export state.** A Furnace GUI export taken after playback can inherit
  per-channel live state and differ from a fresh export. Use `fur2vgm.py` or a
  freshly loaded song with playback stopped.
- **Comparison from paired onsets.** The converter correctly drops key-ons
  the chip ignores. Pairing every source onset to the next export onset across
  such a gap invents pitch jumps; read the timeline distribution instead.
- **Phantom DAC notes on a disabled DAC.** A log can write `0x2B` without
  ever setting bit 7 (Contra track 01). The DAC writes are then inaudible and
  FM6's key-ons are ordinary FM notes. Collecting the stream regardless of
  `0x2B` creates notes that collide with those FM6 rows. Gate the collector on
  the enable state; only the read cursor advances while disabled.
- **A timeline is not a slice representation.** A continuous 22.05 kHz sample
  preserves the audio but stores holds and silence and cannot be edited per
  hit. The sliced path keeps source runs, merges only same-row runs, and puts
  the note on the preceding tick with the held byte as lead.

## Anti-WET rules

- One sample representation per concept. The signed PCM bytes, sample rate,
  loop points and sample-map slot calculation should be created once and
  reused by every chip.
- One instrument-index allocator. Do not patch indices locally after adding
  instruments; let the `Song`/writer layout be the only source of truth.
- One timing model. A chip-specific writer may choose a row encoding, but the
  event time comes from `NoteEvent` and the shared placement rules.
- One set of constants for Furnace quirks (clock defaults, C-4 compensation,
  pan nibbles, sample-map capacity). Duplicated magic numbers are how a fix
  lands in one path and not another.
- A chip module should not know about `.fur` block offsets, instrument IDs or
  pattern columns beyond the `NoteEvent`/sample objects it returns.

## Regression matrix

Run a small representative set, not only the synthetic suite:

| Path | Reference material | What to check |
| --- | --- | --- |
| YM2151 | `VGM_collection/YM2151/` (`Quester`, `Block Hole`, `Willow`, ...) | clock transposition, KON state, E5 persistence, sweeps, LFO reset, instrument fields |
| YM2203 | `VGM_collection/YM2203` | SSG notes, ext-channel routing, carrier octave, instrument count |
| YM2612 FM + PSG | `VGM_collection/YM2612/Thunder Force IV ...` | 48-track pitch/field audit, PSG period/volume, algorithms |
| YM2612 DAC | `VGM_collection/YM2612/Contra Hard Corps ...` | source run bytes, 0x2B enable, slice starts/bodies, FM6 pan, key-on audit, module size |
| YM2612 sparse DAC | Sonic packs and synthetic sparse slices | sample selection, direct `0x2A`, no spurious notes |
| Sample chips | C140/C352/SegaPCM/K007232/OKI packs | clock flags, sample bodies, loops, instrument indices |

For any pack, `report` first, then convert one track, then the pack. Use
`--variants --verify` only when the row-rate choice is part of the change.

## Definition of done

A converter change is complete only when:

- the synthetic regression test is in the suite;
- the full suite passes;
- the reference pack has been regenerated (no stale files);
- the source/export audit is clean, or the remaining difference is explicitly
  named in a warning and in the chip doc;
- the module still loads through Furnace (`fur2vgm.py` or `fur2wav.py`);
- the relevant docs (`opm-notes.md`, `ym2612.md`, ...) agree with the code;
- if the change affects DAC representation, `dac-redesign.md` and the size
  measurements are updated as well.

## Useful commands

```powershell
cd vgm2151fur
python -m pytest -q
python -m vgm2151fur report "track.vgz"
python -m vgm2151fur convert "track.vgz" -o out --no-normalize
python tools/fur2vgm.py out/track.fur -o out/track.vgm
python tools/vgmdiff.py "track.vgz" out/track.vgm
python tools/vgmchroma.py "track.vgz" out/track.vgm
python tools/fur_soundcheck.py "track.vgz" out/track.fur
```

The source packs are under `..\VGM_collection`; the Furnace console build is
under `..\furnace` or selected with `VGM2151FUR_FURNACE`.
