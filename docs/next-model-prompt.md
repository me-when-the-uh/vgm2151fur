# Handoff: chunked PCM DAC, pan, silence rule, release compatibility

The YM2612 DAC default is a release-capped sequence of rate-exact PCM chunks,
one FM6 note each, split at every `0xB6` pan change and at most every 0.25 s,
capped at 240 samples. A chunk starts on audio; silence-only phrases and
leading silence get no note. The per-run slice representation is explicit
(`collect_dac(..., slices=True)`) and is not release-safe on dense tracks.
The old 22.05 kHz body is `timeline=True` for comparison only.

## Read first

1. `docs/portable-context.md` — paths, current state, invariants, commands.
2. `docs/implementation-notes.md` — evidence loop, layer boundaries, release
   lessons.
3. `docs/dac-redesign.md` — chunked default, pan rule, silence rule, release
   evidence, measured cost.
4. `docs/ym2612.md` — DAC/PSG registers, verification and remaining PSG
   quality.
5. `docs/fur-writer.md` — sample map base 0, instruments, pan.

## Current state

- `ym2612dac.collect_dac` decodes old `0xE0`/`0x80`-`0x8F`, direct `0x2A` and
  modern `0x92`/`0x95`, gates on `0x2B`, collapses same-time bursts to the
  final value, and defaults to `_collect_packed` (chunked).
- A phrase splits only on a pause >2,048 VGM samples. Within it, audio splits
  at every pan change and into <=0.25 s chunks, capped at 240. The rate is
  `round(44100 / smallest positive interval)`; every source byte has a
  distinct frame. Leading silence and silence-only phrases are skipped; one
  trailing `0x80` frame sets the DAC hold to silence.
- `group_dac_plays` places each chunk on the preceding tick and emits one
  note; chunks that land in one row merge without dropping audio. The slice
  grouper skips pure-silent rows and gives the preceding sample a zero tail.
- `furwrite` writes maps with `first_sample=0`; a nonzero base makes 0.6.8.3
  say `invalid sample count!`. Each chunk note gets the source `0xB6` pan as
  `08 xy`.
- `analyze` has `dac_slices=False`; pass `dac_slices=True` only for the
  editing/slice tests. The tone base counts DAC maps once, including after
  an FM pitch copy; PSG notes bind to `SN`.
- Contra `fur/` has 203 regenerated chunked variants (29 tracks × 1..4) with
  `condense.tsv`; the rejected `fur-sliced/` was removed; `fur-fixed/` and
  `fur-vgm2fur/` remain references. 02/04/20 have 75/219/240 chunks and
  1/2/2 sample maps.
- Release Furnace 0.6.8.3 renders the 75/219/240 chunk modules (including
  track 20, 240 samples); the WAVs have the same duration as the old
  timeline. The one-phrase default is gone: one note held the first pan for
  the whole track.
- Full suite: 384 passed, 1 skipped. The SN76489 default `.fur` system volume
  is 0.500 (32/64); an absolute VGM extra-header volume still overrides it.
- The chunked 1x manifest reports 50,692 keys (45,177 FM + 5,515 DAC chunk
  notes), 0 missed, `psg_miss = 0`, 13 field differences and up to 2,182 ms
  divergence from the newly audible PSG. The old combined 45,500/0/0 was
  measured with the PSG silent (its notes pointed at a DAC map).

## Remaining work, in order

1. **Sonic packs.** Regenerate them with the PSG fix. A fresh Sonic 01 binds
   every PSG note to INS_STD (`SN`); the old modules still point PSG at DAC 1.
   Confirm the fixed noise channel is audible, not silent, in release Furnace.
2. **Split audit.** Run the key-on/field/pitch audit separately for FM and
   PSG on the chunked `fur/`. The 45,177 FM keys are clean; the newly audible
   PSG needs its own number. Do not quote the old silent-PSG 45,500 as an
   all-channel clean result.
3. **Release matrix.** Render more chunked defaults and x4s with
   `C:\Users\User\Music\furnace-0.6.8.3-win64\furnace.exe`; the sample count
   is 75/219/240 on 02/04/20 and must stay <=240.
4. **Performance.** The chunked path has ~200 samples/notes; the remaining
   cost is per-factor `analyze` plus the Furnace verify/normalize renders.
   Measure the full pack; cache chip analysis or verify selectively if
   needed. Do not reintroduce thousands of sample maps.
5. **Docs.** Keep `dac-redesign.md`, `ym2612.md`, `portable-context.md`,
   `README.md`, `implementation-notes.md` and `fur/condense.tsv` in sync.

## Commands

```powershell
cd vgm2151fur
python -m pytest -q
python -m vgm2151fur report "..\VGM_collection\YM2612\Contra Hard Corps (Mega Drive, Genesis)\04 - Contra Overdrive.vgz"
python -m vgm2151fur convert "..\VGM_collection\YM2612\Contra Hard Corps (Mega Drive, Genesis)\04 - Contra Overdrive.vgz" -o out --no-normalize
python tools\fur2vgm.py out\04*.fur -o out\04.vgm
python tools\fur_soundcheck.py "..\VGM_collection\YM2612\Contra Hard Corps (Mega Drive, Genesis)\04 - Contra Overdrive.vgz" out\04*.fur --wavdir out\soundcheck
```

Set `VGM2151FUR_FURNACE` for the development console build and
`VGM2151FUR_VGM2WAV` for `fur_soundcheck`/`vgmchroma`. The release binary's
headless form is `furnace.exe -console -noreport -loops 1 -output <wav> <fur>`.

## Working rules

- No note or sample-map selector on a silence-only row/phrase. A note must
  have audio content.
- Dense DAC uses the chunked default, <=240 samples, split at pan changes.
  The slice path is for editing and short tracks; check the release sample
  limit before shipping slices. Never collapse a track to one sample/note.
- Every DAC map's `first_sample` is 0.
- PSG/SCC/AY notes bind to their own instrument; count DAC maps once.
- Regenerate the whole pack and all variants after a converter change; do not
  patch a single track and do not keep old generated files.
- A clean unit test does not prove a clean module; render in release Furnace
  and compare the source/export.

## Final answer

Include the representation (chunk sequence, <=240, split at pan changes, map
base 0), the 75/219/240 counts and pan distribution, the silence counts
removed, the release renders, the chunk vs slice vs old timeline sizes, the
tests, the PSG caveat, and the remaining limits (internal silence holds, x4
FM condense, PSG quality, the 02 one-map capacity).
