# Implementation notes

`vgm2151fur` converts VGM/VGZ register and data-block streams into Furnace modules. Keep the layers separate: `vgm.py` parses bytes and data blocks, `analyze.py` turns chip state into `Song`, `NoteEvent` and samples, `furwrite.py` encodes the module, and `furio.py`, `audit.py` and `diag.py` read and verify the result.

For a change, reproduce on a small track and a dense one, identify the owning layer, add a synthetic test with raw writes or VGM bytes, fix that layer, run the suite, convert a reference pack, then compare the source with a headless Furnace export. A rendered WAV is the last check, not the only one. Regenerate affected packs; never hand-edit a `.fur` as the fix.

Rules:

- Do not put parser decisions in a writer, and do not decide notes or slice boundaries in `furwrite.py`.
- A note is a state transition. KON, E5, pan and LFO state persist across rows.
- Clock and pitch are separate. Convert unsigned PCM to signed exactly once. A sample-map note selects a sample, not a pitch.
- The DAC default is release-capped PCM chunks: one note per chunk, split at `0xB6` pan changes, no silence-only notes, and a small fixed sample budget so release Furnace loads the module. `collect_dac(..., slices=True)` is the explicit per-run editor path and is not release-safe at dense counts. `timeline=True` is the old comparison body.
- PSG/SCC/AY notes bind to their own instruments. Count DAC maps once in the tone base, including after FM pitch copies. The SN76489 system volume defaults to 0.500.
- Keep one sample representation, one instrument allocator, one timing model and one set of Furnace constants. Duplicated special cases are how a fix lands in one chip path but not another.

Use `report` before a pack conversion, convert one track before the pack, and check the release Furnace binary when the DAC or PSG changes. The test suite and the reference packs are the authority; a clean unit test alone does not prove a clean module.

Accuracy workflow:

- Read the actual core source before changing LFO or volume behaviour. ymfm resets the OPM LFO phase only through TEST bit 1; Furnace's Nuked-OPM reloads the LFO divider on every `0x18` write. Do not infer either from register traces alone.
- YM2610 ADPCM-A `0x100` is a strobe. Every non-stop write retriggers its selected channels, and drivers often set the real channel level or pan a few samples after the gate. The collector's 64-sample update window is deliberate (`docs/ym2610.md`).
- Use the ten-track gate in `portable-context.md` first. Do not rebuild a pack, and never the collection, until the gate and the old module comparison agree.
- Use a temporary git worktree of the committed converter to separate a pre-existing regression from a new one.
- Preserve the old module's master and chip mix when applying a batch. Copying only the master is not enough: the YM2151/MSM6258 balance is part of the old level.
- A low-confidence grid keeps the 1x module. Do not generate `xN` variants that cannot place the source's notes and modulation.
- Compare pitch, LFO anchors, RMS and chroma. Pitch alone missed the YM2151 header-volume bug that made one pack five times too quiet.
