# Next-model prompt: sliced YM2612 DAC follow-up

The slice-based FM6 DAC replacement is implemented. This prompt is for the
next session that verifies it, extends it to the remaining packs, or removes
the old timeline fallback. Read the durable docs rather than repeating chip
detail in the prompt.

## Read first

1. `docs/portable-context.md` — paths, current state, invariants, commands.
2. `docs/implementation-notes.md` — evidence loop, layer boundaries and
   anti-WET rules.
3. `docs/dac-redesign.md` — the implemented model, measured cost, acceptance
   tests and remaining limits.
4. `docs/ym2612.md` — DAC registers, FM6, pan, verification numbers.
5. `docs/fur-writer.md` — `.fur` sample maps, rate compensation, instruments.

The source VGM packs and the code are the authority; the docs describe what
was measured.

## Current state

- `ym2612dac.collect_dac` is the sliced path: one canonical decoder for old
  `0xE0`/`0x80`-`0x8F`, direct `0x2A` and modern `0x92`/`0x95`; runs split on
  pauses; rates from each run's own span; dedupe by `(bytes, rate)`; `0x2B`
  enable gate; same-time bursts collapse to the final value with a warning.
- `group_dac_plays` merges runs that start in one tracker row into one
  sample with explicit holds, places the note on the preceding tick and pads
  the sample with the previously held byte. No run is dropped.
- `analyze` creates the DAC notes after the final row grid, inserts their
  sample maps before the PCM/tone instruments and rebases those indices.
- `furwrite` writes 120 slices per `DIV_INS_AMIGA` map; note `60+slot` is a
  selector, not a transpose.
- Contra full pack: 45,500/45,500 key-ons, 0 fields, 0 pitch runs,
  179,831/179,831 DAC notes matched, 0 missing bodies, 7.30 MB compressed
  (timeline 12.85 MB). Track 04 stores 444,425 sample bytes for 426,320
  audible source bytes (428,980 raw plays including the disabled init burst).
- Full suite: 375 passed, 1 skipped.
- `fur-sliced/` holds the current 29 Contra modules beside `fur-fixed/`.

## Remaining tasks, in order

1. **Re-run the acceptance on the current tree.** The durable checks are
   `python -m pytest -q`, the synthetic `TestYm2612DacSlices`, and a
   direct analyze/write/export loop over the five reference tracks (04, 20,
   07, 14, 03). Confirm:
   - every source run's slice body is in the export;
   - every DAC note time has an exported `0x95` start within the audit
     window;
   - the exported `0x92` rate is the intended slice rate (allow 1 Hz of
     integer C-4 rounding).
2. **Sonic sparse DAC.** The three Sonic packs (118 tracks) have sparse or
   absent FM6 banks. Convert at least Emerald Hill, IceCap Act 1 and Data
   Select and check that the sparse slices still become short samples, that
   no spurious notes appear, and that `0x2B` gating does not remove an
   audible sparse stream. Record the per-track slice/note counts.
3. **Last-note edge.** Five Contra tracks (03, 06, 10, 13, 22) have a final
   DAC note just outside a ±2-sample start check against the export's last
   sample. They are inside the 100 ms audit and their bodies are present.
   Decide whether the source end should extend the last pattern row (a writer
   change) or whether this is an acceptable end-of-track cut; document the
   choice.
4. **Fallback decision.** `collect_dac(..., timeline=True)` and
   `_collect_timeline` remain for comparison. If the Sonic and non-Contra
   audits pass, remove the fallback and its dead constants, or keep it as a
   documented diagnostic. Do not leave a second default path.
5. **Artefacts.** If the converter changes, regenerate the whole Contra set
   rather than patching files. Keep `fur-fixed` untouched; write the current
   set to `fur-sliced/` with a fresh `COMPARISON.txt`/`comparison.tsv`.
   Archive the track 04 `fur_soundcheck` WAVs and chroma output.
6. **Docs.** Update `dac-redesign.md`, `ym2612.md`, `portable-context.md` and
   the pack's `COMPARISON.txt` with actual before/after numbers. Do not
   claim a size win that is not measured.

## Commands

```powershell
cd vgm2151fur
python -m pytest -q
python -m vgm2151fur report "..\VGM_collection\YM2612\Contra Hard Corps (Mega Drive, Genesis)\04 - Contra Overdrive.vgz"
python -m vgm2151fur convert "..\VGM_collection\YM2612\Contra Hard Corps (Mega Drive, Genesis)\04 - Contra Overdrive.vgz" -o out --no-normalize
python tools\fur2vgm.py out\04*.fur -o out\04.vgm
python tools\fur_soundcheck.py "..\VGM_collection\YM2612\Contra Hard Corps (Mega Drive, Genesis)\04 - Contra Overdrive.vgz" out\04*.fur --wavdir out\soundcheck
```

Set `VGM2151FUR_FURNACE` when the console build is not under `..\furnace`
and `VGM2151FUR_VGM2WAV` for `fur_soundcheck`/`vgmchroma`. Use headless
exports; a GUI export after playback is not deterministic.

## Working rules

- Read the portable context and the relevant chip docs before editing.
- Do not hand-edit generated `.fur` files; regenerate them.
- Do not commit or rewrite the reference artefacts unless explicitly asked.
- A clean unit test does not prove a clean module. Compare the source and
  the Furnace export on the same timeline.
- Keep the sample-map selector un-transposed, the `0x2B` gate, the shared
  `place_tick` model, and the one note per row rule.
- When the DAC representation changes, update `dac-redesign.md`,
  `ym2612.md`, the portable context and the pack's comparison files.

## Final answer

Include the size table, the exact key-on/field/pitch and DAC start/body/rate
numbers, the audio check, the commands run and any remaining limitations.
