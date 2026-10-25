# Portable context: vgm2151fur

This is the compact handoff context for a fresh coding session. It is not a
replacement for the chip docs; it tells you where you are, what to read first,
and which invariants must survive.

## Workspace

```
C:\Users\User\Desktop\Pets\ripper\tools\arcade-vgm-rip\
  vgm2151fur\                 Python converter package and tests
    vgm2151fur\               implementation
      vgm.py                  VGM parser and data blocks
      analyze.py              chip state machines -> Song / NoteEvent / samples
      ym2612dac.py            YM2612 DAC collector (sliced; timeline fallback)
      furwrite.py             Furnace .fur writer
      furio.py                .fur reader / linter
      audit.py, diag.py       source-vs-export audits
    docs\                     chip notes and this handoff material
    tests\                    pytest suite (375 passing, 1 skipped)
    tools\                    fur2vgm, fur2wav, vgmdiff, vgmchroma, soundcheck
  VGM_collection\             local reference packs
    YM2151\                   Quester, Block Hole, Willow, ...
    YM2203\                   YM2203 corpus
    YM2612\                   Thunder Force IV, Contra, Sonic
  furnace\                    Furnace console build (or VGM2151FUR_FURNACE)
```

The project is Python 3.11+ and runs on Windows or Linux. Generated `.fur`
files are zlib-wrapped at level 5; `write_fur` itself returns raw bytes unless
`compress=True`.

## Read order

1. This file.
2. `docs/implementation-notes.md` — the cross-cutting implementation and
   stabilisation method.
3. The relevant chip doc:
   - `docs/opm-notes.md` for YM2151
   - `docs/ym2203.md` for YM2203
   - `docs/ym2612.md` for YM2612, PSG and DAC
   - `docs/segapcm-c140.md`, `docs/c352.md`, `docs/scc-ay8910.md`
4. `docs/dac-redesign.md` if the task touches FM6 DAC.
5. `docs/fur-writer.md` for the `.fur` block and pitch encoding.
6. `docs/operating.md` for commands, variants, verification and environment.

## Current state

- YM2151 (OPM), YM2203 (OPN), YM2612 (OPN2), Genesis SN76489, K007232,
  MSM6295, MSM6258, SegaPCM, C140, C352, SCC/SCC+ and AY-8910 paths exist.
- The YM2151 path includes clock transposition, custom clock flags, the
  measured timing lattice, ED placement, KON-state handling, E5 persistence,
  per-row sweep encoding, LFO reset, PMS/AMS macros, carrier-TL normalization
  and correct OPM operator order. Details are in `opm-notes.md`.
- YM2612 FM + PSG is audited on Thunder Force IV; Contra Hard Corps is the DAC
  reference.
- The YM2612 DAC default is **deduplicated source slices**. Runs are split by
  time, not by pointer changes; runs that start in one tracker row merge into
  one sample with explicit holds; the note is placed on the preceding tick and
  the sample starts with the previously held byte. The old 22,050 Hz timeline
  is `collect_dac(..., timeline=True)` for comparison only.
- A `0x2B` write without bit 7 means no DAC audio; its FM6 key-ons stay FM
  notes (Contra track 01). No `0x2B` write means the DAC is assumed enabled.
- `tests/test_vgm2151fur_ym2612.py` covers the sliced DAC path (direct `0x2A`,
  gaps, holds, same-row merges, >120 slices, multiple maps, pan changes,
  disabled DAC, modern blocks), PCM round trips, pan, key-on masks, operator
  order and full Furnace exports.
- Current Contra artifacts are under
  `VGM_collection\YM2612\Contra Hard Corps (Mega Drive, Genesis)\`:
  `fur-fixed` (timeline reference), `fur-sliced` (current sliced set with its
  `COMPARISON.txt` and `comparison.tsv`) and `fur-vgm2fur` (old vgm2fur
  baseline). The older `wave-compare` tree is not present in this checkout;
  the current audio check is `tools/fur_soundcheck.py`.

## Invariants

- Source VGM/data blocks are the specification. Generated `.fur` files are
  artifacts; never hand-edit them as the fix.
- Parse in `vgm.py`; decide musical state in `analyze.py`; encode only in
  `furwrite.py`.
- A note is a state transition, not every register write. KON masks, held
  notes, E5, pan and LFO state persist across rows.
- Clock and pitch are separate: write `customClock` and shift notes when the
  source clock differs from Furnace's default.
- A DAC sample-map note selects a sample; it is not a musical transpose.
- Convert unsigned PCM to signed exactly once. `0x80` is silence, not a
  `-128` rail.
- Furnace split pan is one nibble per side: `08 FF` both, `08 F0` left,
  `08 0F` right.
- Rebase every non-FM instrument index after appending FM instruments.
- Do not regenerate the user's artifacts unless the task asks for it. When
  the converter changes, regenerate the affected outputs instead of patching
  individual files.
- Do not commit unless explicitly asked; leave changes in the working tree.

## Commands

```powershell
cd vgm2151fur
python -m pytest -q
python -m vgm2151fur report "path\to\track.vgz"
python -m vgm2151fur convert "path\to\track.vgz" -o out --no-normalize
python tools/fur2vgm.py out\track.fur -o out\track.vgm
python tools/fur2wav.py out\track.fur -o out\track.wav
python tools/vgmdiff.py "path\to\track.vgz" out\track.vgm
python tools/vgmchroma.py "path\to\track.vgz" out\track.vgm
python tools/fur_soundcheck.py "path\to\track.vgz" out\track.fur
```

Set `VGM2151FUR_FURNACE` when the console build is not under `..\furnace`.
Use headless exports; a GUI export after playback is not deterministic.

## Evidence to collect before changing code

- The smallest source track that shows the bug.
- A register/PCM dump around the bad event, not just a screenshot.
- A failing synthetic test with raw writes or raw VGM bytes.
- A source-vs-export comparison on the same timeline.
- A before/after size and byte count when sample data changes.

Do not use a rendered WAV as the only proof. It can show that something is
wrong but not which sample, event or instrument is wrong.

## Next task

The slice-based DAC replacement is implemented and audited: the Contra pack
keeps 45,500/45,500 key-ons with zero field/pitch differences, all 179,831
DAC notes match, and the set is 7.30 MB compressed instead of 12.85 MB. Read
`dac-redesign.md` for the design and measured cost. Remaining useful work:

- Regenerate `fur-sliced` and its comparison files after any converter change
  (`scratchpad` scripts are not durable; use `convert` or a small direct
  analyze/write loop).
- Archive the track 04 `fur_soundcheck` WAVs and chroma output with the pack.
- Decide whether the timeline fallback should be deleted now that the sliced
  path passes; it is currently only `collect_dac(..., timeline=True)`.
- Sonic Emerald Hill is audited: 1,319 DAC slices/notes, 0 pitch runs,
  0 bad rates and one explicit same-time burst warning. The remaining Sonic
  packs and a non-Contra DAC source are still worth the same check before
  broadening the default.
