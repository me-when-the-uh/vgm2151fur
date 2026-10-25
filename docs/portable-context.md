# Project context: vgm2151fur

`vgm2151fur` is a Python 3.11+ converter from VGM/VGZ rips to Furnace Tracker modules on Windows and Linux. Furnace is needed for the volume fit and for `render`, `compare` and `--verify`; put it in `furnace/` or set `VGM2151FUR_FURNACE`.

Layout: `vgm.py` parses commands and data blocks; `analyze.py` contains the chip state machines; `ym2612dac.py` builds DAC samples; `furwrite.py` writes the `.fur`; `furio.py`, `audit.py` and `diag.py` read and verify it. Helpers are in `tools/`, tests in `tests/`, chip notes in `docs/`.

Current behavior:

- The YM2612 DAC default is release-capped PCM chunks: one FM6 note per chunk, split at every `0xB6` pan change and at most every 0.25 s, with a small fixed sample budget so Furnace 0.6.8.3 loads the module. A chunk starts on audio; silence-only chunks and leading silence get no note. The rate is derived from the smallest write interval. `collect_dac(..., slices=True)` keeps the explicit per-run slice representation for editing; `timeline=True` is the old comparison body. Dense slice sets exceed release Furnace's sample limit.
- The SN76489 `.fur` system volume defaults to 0.500. An absolute VGM extra-header volume entry overrides it; `edit --chip SN76489 --value` changes a finished module.
- PSG/SCC/AY notes use their own instruments. The DAC maps are counted once in the tone base, including after FM pitch copies.

Commands:

```powershell
cd vgm2151fur
python -m pytest -q
python -m vgm2151fur report "track.vgz"
python -m vgm2151fur convert "track.vgz" -o out --no-normalize
python tools/fur2vgm.py out/track.fur -o out/track.vgm
python tools/fur_soundcheck.py "track.vgz" out/track.fur
```

Read `implementation-notes.md` before changing the pipeline and the relevant chip note before changing a state machine. The test suite is `python -m pytest -q` (384 passed, 1 skipped in the current tree). Regenerate affected packs after a converter change; do not commit generated modules or `.fur` files unless asked.
