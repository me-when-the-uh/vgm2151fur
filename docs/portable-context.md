# Project context: vgm2151fur

`vgm2151fur` is a Python 3.11+ converter from VGM/VGZ rips to Furnace Tracker modules on Windows and Linux. Furnace is needed for the volume fit and for `render`, `compare` and `--verify`; put it in `furnace/` or set `VGM2151FUR_FURNACE`.

Layout: `vgm.py` parses commands and data blocks; `analyze.py` contains the chip state machines; `ym2610.py` holds the OPNB FM/SSG state and the ADPCM-A/B collectors; `ym2612dac.py` builds DAC samples; `fds.py`, `wonderswan.py` and `qsound.py` hold the newer chip collectors; `furwrite.py` writes the `.fur`; `furio.py`, `audit.py` and `diag.py` read and verify it. Helpers are in `tools/`, tests in `tests/`, chip notes in `docs/`.

Current behavior:

- The YM2612 DAC default keeps bank source samples: pointer runs are grouped by bank offset plus a trimmed-mean pitch cluster, the longest read in each cluster is stored once, and shorter reads there are cut by the next note or an explicit note-off. Four/five-sample wait jitter stays in one cluster; a genuinely repitched reuse gets its own sample so the pattern can play the real pitch. Silent, flat-line and near-silent reads take no sample slot. With the optional `lossy_samples` flag the final source map is capped at 32 bodies: the hottest 16 stay, the longest fill the rest, and every other fragment is mapped to the closest anchor by a loose opening-frames comparison. The same optional reduction applies to SSG PCM and NES raw PCM. Without it every distinct body is kept. A sample always starts at frame 0, so only prefix similarity can share a body; the note's own cut still ends it on time. A direct `0x2A` stream or an expanded `0x95` block play with no bank offset falls back to attack detection (rests, sharp onsets and pan changes split candidate bodies; repeated hits share one body; a shorter same-rate prefix uses the longer body) before fixed-time chunks; a `$93` stream is already contiguous and stays rate-sliced. The 240-sample budget still gates the fallback paths; a representation over it joins its least-significant tail into one final sample, and uniform chunks are used only when they fit better. `collect_dac(..., slices=True)` keeps the explicit per-run slice representation for editing; `timeline=True` is the old comparison body. Dense slice sets exceed release Furnace's sample limit.
- The SN76489 `.fur` system volume defaults to 0.500. An absolute VGM extra-header volume entry overrides it; `edit --chip SN76489 --value` changes a finished module.
- PSG/SCC/AY notes use their own instruments. The DAC maps are counted once in the tone base, including after FM pitch copies.
- The YM2151 module volume is unity. Do not apply VGM header `0x7C` to the OPM; on some packs it is `0x18`-`0x30` and would make the module 5x too quiet. Header volume is still parsed for diagnostics. The MSM6258 player default is `0x11E`; its Furnace core is 1.64x louder, so the converter uses 0.61.
- Finished packs keep the old per-module master and chip mix when they are rebuilt from a batch; the converter only supplies chip defaults. The 288 tracks without an old module were gain-matched to their source render and use YM2151 chip 0.5 to keep the old new-pack FM/sample balance.
- Condensed `xN` variants are not emitted when the measured source grid is low-confidence. Keep the 1x module.
- The loop start stays on the VGM marker. The end is the marker plus the header's loop length, rounded to rows. The row length is calibrated to that length, and a repeat is sample exact. Loop modality is 2 ("do nothing"). Held voices and samples survive the jump. A delayed note on the jump row keeps its `EDxx`. The `Bxx` rides a channel with no delay. Events before the wrap that round past the last row stay on it. Each condensed factor re-seats the loop on its own grid and recalibrates the row. Patterns are a whole 8/4 or 16/4 phrase (32 or 64 pulses of the measured 16th), up to 256 rows.
- A held one- or two-step retune becomes `pitch_steps` and is written as E8/E9 plus E5 on every supported FM family. The YM2151 classifier (`_classify_legato`) keeps a staircase, a reversing move, a short final step and a release-tail retune as 01/02 ramps. The OPN and OPL families use the short/steppable step path in `_bind_fm_pitch`; longer motion stays a pitch macro. `report` shows the YM2151 `N legato steps, M single-step ramps`.
- A log that mixes an OPL family with an arcade FM chip converts the side with more rising key-ons and warns with both counts - unless the two can share a module. A YM2151 or YM2612 beside an OPL family keeps both systems, because neither claims a sample engine the other needs; so does a YM2612 whose only audio is its DAC stream. A YM2608 or YM2610 still converts one family only. `docs/merged-logs.md` has the shapes, the channel bases and the round-trip proof.
- A chip that only received its init block earns no system at all, and says so: `_family_sounds()` needs a rising key-on, a tuned SSG voice, a rhythm latch with a trigger bit or an ADPCM-A trigger. The same test drops a SN76489 whose four attenuations never lift instead of adding four dead columns.
- YM2610 support covers the base 14-channel layout and the 16-channel B variant. The FM pitch model uses `fmFreqBase = 9440540`. ADPCM-A uses a fixed `clock/432` rate. ADPCM-B stores the source rate with the `8363/8372` correction. The reference packs are Night Striker, Viewpoint and Metal Slug 3; `docs/ym2610.md` has the audit method and residuals.
- The OPL family (YM3526/OPL, YM3812/OPL2, YMF262/OPL3) is converted with Furnace's own channel and slot tables; `docs/opl.md` has the details. Rhythm mode is its own system (`0xA2`/`0xA3`/`0xA4`) because bank A's last FM channels become the kit, and four-operator voices stay inside `0x91`: an OPL3's `chanMap` interleaves each pair, so only an even column heads a voice and its algorithm is read from two `0xCn` registers. Key bits are deferred to the end of a same-sample group (as on the YM2612) - an attack comparison that reads registers inside a sample reports diffs that are not there, which `diag.opl_attacks()` exists to avoid.
- Famicom Disk System (`docs/fds.md`), WonderSwan (`docs/wonderswan.md`) and Capcom QSound (`docs/qsound.md`) use native Furnace systems and keep their output volume at the player default. FDS keeps either frequency-byte order and its modulation table; WonderSwan keeps big-endian wave RAM, independent left/right volumes, semitone note splits, noise and streamed PCM; QSound keeps PCM/ADPCM bodies, start/phase retriggers, pan/surround and echo.

Commands:

```powershell
cd vgm2151fur
python -m pytest -q
python -m vgm2151fur report "track.vgz"
python -m vgm2151fur convert "track.vgz" -o out --no-normalize
python tools/fur2vgm.py out/track.fur -o out/track.vgm
python tools/fur_soundcheck.py "track.vgz" out/track.fur
```

Read `implementation-notes.md` before changing the pipeline and the relevant chip note before changing a state machine. Run the tests with `python -m unittest discover -s tests` (1,090 tests in the current tree, with three known unrelated failures in condense and level-fit and one skip; no test-runner package is installed - a bare `pytest` fails, and `python -m unittest tests.test_vgm2151fur_opl` is the OPL-family subset). Furnace is needed for the round-trip tests; without it those skip. Regenerate affected packs after a converter change; do not commit generated modules or `.fur` files unless asked.

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
