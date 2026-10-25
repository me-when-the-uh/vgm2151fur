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
      ym2612dac.py            YM2612 DAC collector (chunked default)
      furwrite.py             Furnace .fur writer
      furio.py                .fur reader / linter
      audit.py, diag.py       source-vs-export audits
    docs\                     chip notes and this handoff material
    tests\                    pytest suite (384 passing, 1 skipped)
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
- The YM2612 DAC default is a **release-capped sequence of rate-exact PCM
  chunks**, one FM6 note each, split at every `0xB6` pan change and at most
  every 0.25 s, capped at 240 samples. A chunk starts on audio; silence-only
  phrases/leading silence get no note; a trailing `0x80` sets the hold to
  silence. The rate is `round(44100 / smallest positive interval)`, so every
  source byte has a distinct frame. Track 02 is 75 chunks/notes (one 120-slot
  map; 57 both, 12 right, 6 left), 04 is 219 (two maps), 20 is 240 (two maps).
  The per-run slice path is explicit (`slices=True`) and not release-safe at
  dense sample counts; the old 22.05 kHz body is `timeline=True` for
  comparison only. Do not go back to one sample/note per phrase: it loses the
  source pan changes (the 02 one-phrase note was right-only).
- A `0x2B` write without bit 7 means no DAC audio; its FM6 key-ons stay FM
  notes (Contra track 01). No `0x2B` write means the DAC is assumed enabled.
- The slice mode drops pure-silence rows and extends the preceding sample
  with a zero tail; a row must never get a note whose only content is
  silence.
- The tone-instrument base counts the DAC maps exactly once, including after
  an FM pitch copy. PSG/SCC/AY notes bind to their own STD/SN/AY instrument,
  never a DAC map.
- The SN76489 default `.fur` system volume is **0.500** (32/64), not the
  reference player's 1.0. An absolute VGM extra-header volume entry still
  replaces it; `edit --chip SN76489 --value` overrides a finished module.
- `tests/test_vgm2151fur_ym2612.py` covers the chunked default (a 20 s stream
  becomes >1 and <=240 chunks at the 0.25 s budget; three source pans appear
  on notes and `08` effects; short streams stay one chunk; long gaps split
  audio and drop silence; silence-only has no notes; natural rate; map base
  0), the explicit slice mode, the silence-row rule, PSG binding, PCM round
  trips, pan, key-on masks, operator order and Furnace exports. Full suite:
  384 passed, 1 skipped.
- Current Contra artifacts are under
  `VGM_collection\YM2612\Contra Hard Corps (Mega Drive, Genesis)\`:
  `fur-fixed` (old timeline reference), `fur-vgm2fur` (old baseline) and
  `fur/` (the regenerated chunked default and 1x..4x variants with
  `condense.tsv`; 203 modules, 68.4 MB). The rejected `fur-sliced` tree was
  removed. The current audio check is `tools/fur_soundcheck.py`.
- Release Furnace is part of the contract: check
  `C:\Users\User\Music\furnace-0.6.8.3-win64\furnace.exe`, not only the
  development build. The 75/219/240-sample chunk modules render there; the
  slice modules exceed its sample count.

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

The chunked DAC is implemented and regenerated on the Contra pack: 75/219/240
chunks on 02/04/20, split at pan changes, one sample per 0.25 s maximum,
capped at 240, and all three render in Furnace 0.6.8.3 (including the
240-sample track 20). The slice path remains explicit (`slices=True`) but
exceeds the release sample limit on dense tracks. Read `dac-redesign.md` for
the design and measured cost. Remaining useful work:

- Regenerate the Sonic packs with the PSG fix; the fresh Sonic 01 binds `SN`,
  but the old `fur/` modules still point PSG at a DAC map.
- Split the full audit into FM and PSG. The chunked 1x manifest reports 50,692
  keys (45,177 FM + 5,515 DAC chunks), 0 missed, 13 field differences and up
  to 2,182 ms divergence from the newly audible PSG; the old combined 45,500
  was measured with silent PSG notes.
- Archive the track 04 `fur_soundcheck` output with the pack.
- Keep `fur-sliced` removed. Regenerate `fur/` after any converter change,
  never patch only the reported track.
- The old `timeline=True` body is a comparison/diagnostic, not a default.
