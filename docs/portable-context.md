# Project context: vgm2151fur

`vgm2151fur` is a Python 3.11+ converter from VGM/VGZ rips to Furnace Tracker modules on Windows and Linux. Furnace is needed for the volume fit and for `render`, `compare` and `--verify`; put it in `furnace/` or set `VGM2151FUR_FURNACE`.

Layout: `vgm.py` parses commands and data blocks; `analyze.py` contains the chip state machines; `ym2610.py` holds the OPNB FM/SSG state and the ADPCM-A/B collectors; `ym2612dac.py` builds DAC samples; `furwrite.py` writes the `.fur`; `furio.py`, `audit.py` and `diag.py` read and verify it. Helpers are in `tools/`, tests in `tests/`, chip notes in `docs/`.

Current behavior:

- The YM2612 DAC default is release-capped PCM chunks: one FM6 note per chunk, split at every `0xB6` pan change and at most every 0.25 s, with a small fixed sample budget so Furnace 0.6.8.3 loads the module. A chunk starts on audio; silence-only chunks and leading silence get no note. The rate is derived from the smallest write interval. `collect_dac(..., slices=True)` keeps the explicit per-run slice representation for editing; `timeline=True` is the old comparison body. Dense slice sets exceed release Furnace's sample limit.
- The SN76489 `.fur` system volume defaults to 0.500. An absolute VGM extra-header volume entry overrides it; `edit --chip SN76489 --value` changes a finished module.
- PSG/SCC/AY notes use their own instruments. The DAC maps are counted once in the tone base, including after FM pitch copies.
- The YM2151 module volume is unity. Do not apply VGM header `0x7C` to the OPM; on some packs it is `0x18`-`0x30` and would make the module 5x too quiet. Header volume is still parsed for diagnostics. The MSM6258 player default is `0x11E`; its Furnace core is 1.64x louder, so the converter uses 0.61.
- Finished packs keep the old per-module master and chip mix when they are rebuilt from a batch; the converter only supplies chip defaults. The 288 tracks without an old module were gain-matched to their source render and use YM2151 chip 0.5 to keep the old new-pack FM/sample balance.
- Condensed `xN` variants are not emitted when the measured source grid is low-confidence. Keep the 1x module.
- The loop start stays on the VGM marker. The end is the marker plus the header's loop length, rounded to rows. The row length is calibrated to that length, and a repeat is sample exact. Loop modality is 2 ("do nothing"). Held voices and samples survive the jump. A delayed note on the jump row keeps its `EDxx`. The `Bxx` rides a channel with no delay. Events before the wrap that round past the last row stay on it. Each condensed factor re-seats the loop on its own grid and recalibrates the row. Patterns are a whole 8/4 or 16/4 phrase (32 or 64 pulses of the measured 16th), up to 256 rows.
- A held one- or two-step YM2151 retune becomes `pitch_steps` and is written as E8/E9 plus E5. A staircase, a reversing move, a short final step and a release-tail retune stay 01/02 ramps. `report` shows `N legato steps, M single-step ramps`.
- A log that mixes an OPL family with an arcade FM chip converts the side with more rising key-ons and warns with both counts. Two FM families in one module are not supported.
- YM2610 support covers the base 14-channel layout and the 16-channel B variant. The FM pitch model uses `fmFreqBase = 9440540`. ADPCM-A uses a fixed `clock/432` rate. ADPCM-B stores the source rate with the `8363/8372` correction. The reference packs are Night Striker, Viewpoint and Metal Slug 3; `docs/ym2610.md` has the audit method and residuals.
- The OPL family (YM3526/OPL, YM3812/OPL2, YMF262/OPL3) is converted with Furnace's own channel and slot tables; `docs/opl.md` has the details. Rhythm mode is its own system (`0xA2`/`0xA3`/`0xA4`) because bank A's last FM channels become the kit, and four-operator voices stay inside `0x91`: an OPL3's `chanMap` interleaves each pair, so only an even column heads a voice and its algorithm is read from two `0xCn` registers. Key bits are deferred to the end of a same-sample group (as on the YM2612) - an attack comparison that reads registers inside a sample reports diffs that are not there, which `diag.opl_attacks()` exists to avoid.

Commands:

```powershell
cd vgm2151fur
python -m pytest -q
python -m vgm2151fur report "track.vgz"
python -m vgm2151fur convert "track.vgz" -o out --no-normalize
python tools/fur2vgm.py out/track.fur -o out/track.vgm
python tools/fur_soundcheck.py "track.vgz" out/track.fur
```

Read `implementation-notes.md` before changing the pipeline and the relevant chip note before changing a state machine. Run the tests with `python -m unittest discover -s tests` (455 passed, 4 skipped in the current tree; no test-runner package is installed - a bare `pytest` fails, and `python -m unittest tests.test_vgm2151fur_opl` is the OPL-family subset). Furnace is needed for the round-trip tests; without it those skip. Regenerate affected packs after a converter change; do not commit generated modules or `.fur` files unless asked.

## Accuracy gate

Do not bulk-rebuild a pack until this set passes against the source and the
previous module:

- Namachuukei 68: `15 Change`, `21 Unknown Track`, `17 Pro Baseball News Jingle`, `20 Ending`, `11 Hit #1`
- Hyper Duel: `03 Buster Gear Cyber Fleet (Stage 1)`
- Final Round: `09 Power Up (Training BGM)`
- MIA: `05 Boss 1`
- Super Contra: `10 No Escape (BGM 4)`
- Gradius II: `10 The Old Stone Age 1 ~ 2`

For a change, compare against both the old output and the committed
converter. A regression that is already present at `HEAD` is not introduced
by the change, but it still must not be copied over a better old module.
Preserve the old module and its xN files when that happens.

Current YM2151 output: 1,072 tracks. Nine tracks are still served by
modules from before the current sweep encoder. The current converter handles
short setup notes correctly; UN Squadron 25 and Valkyrie 16 now measure
0 ms. These residuals still need work:

- Ninja Spirit 03, 06, 08, 10; Varth 06, 13, 16.
- Willow 12 Night Sea is 973 ms with both the current and the old module;
  Willow's other tracks are 0 ms.
- LFRQ coverage is 0.901 vs 0.916 old; TEST coverage is 0.500 vs 0.095 old.
- 221 tracks keep only 1x because their source grid is low-confidence.

The report for the last applied re-rip is
`output/_ym2151_regression/rerip.md`.
