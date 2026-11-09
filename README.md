# vgm2151fur

Converts arcade and console VGM/VGZ rips into Furnace Tracker modules.
Supported chips:
- Yamaha: YM2151, YM2203, YM2612, YM2610/YM2610B, YM2608, YM2413, YM3526, YM3812, YMF262, Y8950, YMF278B, YMZ280B, YMW258-F (MultiPCM) and YM2149.
- General Instrument: AY-3-8910.
- Konami: VRC7, K007232, K053260, K054539 and SCC/SCC+.
- Atari: POKEY and Lynx/Mikey.
- Seta: X1-010.
- Capcom: QSound.
- Bandai: WonderSwan.
- Philips: SAA1099.
- Texas Instruments: SN76489 and T6W28.
- NEC: uPD7759 (stand-alone phrase ROMs and slave streams).
- Sega: SegaPCM and 32X PWM.
- Nintendo: NES APU (2A03), Famicom Disk System, Game Boy DMG, Virtual Boy VSU and Hudson HuC6280 (PC Engine).
- OKI: MSM5205, MSM5232, MSM6295 and MSM6258.
- Namco: C140, C219 and C352.
- Irem: GA20.
- Ensoniq: ES5503.
- Ricoh: RF5C68 and RF5C164.

A log that mixes several chip families is written as one module when the combination is supported. `docs/merged-logs.md` lists the rules. A clock field's dual bit keeps both chip instances, even when one never sounds a note; T6W28 is the exception, since its two bits describe one stereo chip.

It runs on Windows and Linux with Python 3.11 or newer.

## Requirements

- Python 3.11 or newer
- Furnace, for the volume fit and the `render`, `compare`, and `--verify` checks. Put a Furnace build in `furnace/` next to the project, or set `VGM2151FUR_FURNACE` to the executable. Any build works: the tools run it with `-console`. The normal release also includes a console build (`furnace-...-win64-console.zip`) if you want one, but you don't need it. Without Furnace, `convert --no-normalize` still writes modules, just without the level fit.

## Quick start

```
cd vgm2151fur
python -m vgm2151fur
```

With no arguments the tool opens a menu. Type a number, or paste a path to a VGM, a pack folder, or a folder of packs. Quotes from Explorer's Copy as path are fine. `q` or Ctrl+C quits

```
 1  packs      scan a folder, then convert, volumes, render
 2  convert    a file, a folder, or a glob
 3  volumes    a .fur file or a folder of them
 4  compare    a .fur against its source VGM
 5  render     .fur to WAV
 6  report     analysis, nothing written
 7  condense   write copies of tracks with 1x..4x lower BPM
```

`packs` is the place to start. It scans a folder (remembered next time), lists the packs with track counts and chips, and opens a track list with a dash next to tracks that have no `.fur` yet. `c` converts all tracks or a selection (`2-6`, `1,4`, or a single number), `e` changes a chip volume on the converted tracks, and `r` renders one to WAV so you can hear it. Conversion asks `optimise for pattern density? (Y/n)`, then a separate `detect and optimise for LFO loops? (Y/n)` (both default yes), then `experimental aggressive (lossy) sample clustering? (y/N)` (default no), then `normalize/verify through Furnace? (y/N)`; the default is no, so the menu does not launch Furnace unless you ask it to. The LFO answer is remembered for pasted-path conversions; the experimental lossy-clustering answer is only latched for the current menu session and is not persisted, so a new run defaults back to off. The menu stores its state in `~/.vgm2151fur.json`.

## Commands

The menu actions are also available as commands:

```
python -m vgm2151fur convert "01 Track.vgz" -o out
python -m vgm2151fur convert "My Gamepack" -o out --workers 6
python -m vgm2151fur report "01 Track.vgz"
python -m vgm2151fur edit out/01.Track.fur --list
python -m vgm2151fur edit out/01.Track.fur --chip C140 --factor 0.9
python -m vgm2151fur compare "01 Track.vgz" out/01.Track.fur --channel 4
python -m vgm2151fur render out/01.Track.fur --loops 2
```

`convert` writes one zlib-compressed `.fur` per input. `-o` sets the output folder (the default is a `fur/` folder beside the input). `--speed N` pins ticks per row when the automatic grid estimate is wrong, `--workers N` sets how many tracks run at once, and `--melody-only` / `--pcm-only` restrict the chips. `--no-normalize` skips the Furnace level fit and keeps the source mix without the fit. `--no-lfo-loop` skips the LFO/tail loop search for long pitch macros, which is faster but leaves those macros longer; exact first-cycle loops are still detected. `--lossy-sample-clustering` opts into the experimental lossy map reduction for YM2612 DAC, SSG PCM, NES raw PCM, uPD7759, 32X PWM, MSM5205 and Mikey bodies (about 32 samples, short fragments mapped to similar long/hot ones). A folder of packs writes each pack to its own `fur/` folder, or to `-o/<pack>`. Every track prints its size, grid speed, instrument and sample counts, and any warnings.

`report` prints the analysis without writing anything. Use it to see which chips a VGM uses, how the grid was estimated, and what was skipped.

`edit` lists or changes chip volumes. `--chip` takes a number, a chip name, or `all`; `--factor 1.1` raises the volume by 10%, and `--value 0.9` sets it outright.

`compare` exports the `.fur` through Furnace and reports stretches where its pitch stream differs from the source VGM by more than half a semitone for at least 100 ms. A run is worth listening to; no runs means the two register streams agree where it matters.

`render` writes a WAV of a `.fur` so you can hear the conversion.

`--variants` writes the 1x/2x/3x/4x row-condensing ladder side by side, and `--optimize lossless` marks the largest factor that adds no structural loss. Every variant reports `keys`/`miss` (source key-ons and the ones the pattern could not place); `--verify` exports each candidate, rejects sustained pitch divergence, and adds the precise key-on audit (`aud`, missed key-ons, effective operator field mismatches). `docs/operating.md` has the workflow and the `condense.tsv` columns.

## Tests

```
cd vgm2151fur
python -m unittest discover -s tests
```

The suite runs without game packs. Tests that need Furnace skip when it is missing. The current tree has three known unrelated failures in the condense and level-fit tests.

## Layout

```
vgm2151fur/     the package: analyze, furwrite, convert, edit, diag
tools/          standalone helpers, including fur2vgm and fur2wav
tests/          unittest suite
docs/           operating guide, format notes, chip notes
```

Project state and workflow are in `docs/portable-context.md` and `docs/implementation-notes.md`. Chip details are in `docs/opm-notes.md`, `docs/ym2203.md`, `docs/ym2612.md`, `docs/ym2610.md`, `docs/opna-opll.md`, `docs/opl.md`, `docs/k053260.md`, `docs/scc-ay8910.md`, `docs/segapcm-c140.md`, `docs/c219.md`, `docs/k054539.md`, `docs/ga20.md`, `docs/msm5205.md`, `docs/msm5232.md`, `docs/es5503.md`, `docs/ssg-pcm.md`, `docs/nes-vrc7.md`, `docs/fds.md`, `docs/gb.md`, `docs/huc6280.md`, `docs/c352.md`, `docs/ymz280b.md`, `docs/multipcm.md`, `docs/upd7759.md`, `docs/pwm32x.md`, `docs/pokey.md`, `docs/mikey.md`, `docs/x1-010.md`, `docs/wonderswan.md`, `docs/qsound.md`, `docs/vsu.md`, `docs/saa1099.md` and `docs/t6w28.md`. `docs/merged-logs.md` covers multi-family modules, and `docs/psg-open-items.md` tracks the remaining AY accuracy gaps.

Open follow-ups are tracked in [TODO.md](./TODO.md).

## Known limits

- The FM models cover the OPM/OPN/OPL register sets the drivers use, with details in the linked chip notes.
  - YM2608 channel-3 Ext, rhythm and delta-T ADPCM-B convert, but CSM timer key-ons are not synthesized.
  - YM2203 channel-3 3-slot chords reduce to one operator, CSM timer key-ons are not synthesized and LFO writes are skipped.
  - YM2610 uses the base 14-channel layout (16 on the B variant), does not split channel-2 special mode and does not synthesize CSM key-ons.
  - YM2413 keeps fast preset, volume and pitch motion as instrument macros while slower changes stay row effects.
  - An OPL log that mixes OPL2 with OPL3, or two crystals, converts on the family that plays most and warns.
  - An OPL register below about C-3 lands on the nearest cell, and a held waveform or envelope change applies at the next key-on.
  - A YM2203 note is labelled at its audible carrier's octave when a patch uses a power-of-two carrier MULT.
  - YM2203 SSG ornaments shorter than a pattern row are simplified.
- OPL4 ROM waves need the `VGM2151FUR_YRW801` image and RAM wave IDs need the VGM RAM dump.
  - The Y8950 ADPCM voice is silent without a delta-T data block.
- YMZ280B extracts distinct ROM bodies and keeps key-on start/stop/loop, levels, pan and mid-note pitch/level sweeps.
- YMW258-F (MultiPCM) converts 28 sample voices per chip from 8- or 12-bit ROM bodies, with source loop points, ADSR/LFO values, Sega 1 MB/512 KB banks and mid-note pitch/volume/pan writes.
  - Only triggered bodies are written, and the source sample index is remapped to a module instrument index.
- uPD7759 converts stand-alone phrase ROMs and slave port streams, rendered as 16-bit PCM and played on a Generic PCM DAC system (one column per chip).
  - Unique phrases use 120-slot sample maps, so a speech-heavy rip stays inside the instrument budget.
  - Identical phrases share one body, and rate changes inside a phrase are resampled to the module body's fixed rate.
- Sega 32X PWM converts streamed 12-bit stereo data to two Generic PCM DAC columns per chip, including n-Bit compressed `0x43` banks.
  - Candidate starts are detected at attack transients (rises out of the idle level), including inside a long passage; bodies shorter than a quarter chunk fold into the next body, silence-only pieces are dropped, and repeated bodies share one sample.
  - Long bodies are split at about 1 second without leaving a sub-minimum tail, and identical bodies are stored once, including when the left and right sides carry the same body.
  - The PWM data is a streamed duty sequence, so the module stores the decoded left/right PCM rather than the source register writes.
- Generic PCM DAC mix levels are limited to 2.0 per chip and the song master to 3.0; the source normalizer stops at that ceiling.
- SegaPCM, C140 and C219 follow mid-note volume and frequency writes, while start/end/loop/bank changes stay at the key-on value.
  - The C140 sign-flip and sign-magnitude mode bits are ignored, as libvgm ignores them.
- C219 converts sixteen voices per chip on Furnace's native `0xCF` system, including word addresses, bank groups, noise, invert/surround and C219 mu-law.
  - Mu-law bodies are decoded to 8-bit linear because Furnace's C219 depth-12 upload path only reaches four bytes per body.
  - Silent constant bodies are dropped, and source sample banks stay in separate Furnace banks by appending trailing silence to the previous one-shot body instead of adding visible padding samples.
  - The layout mirrors Furnace's even-length and bank-crossing rules, then replays the key-ons to warn when a group-bank switch would silence an active voice.
- K054539 converts eight voices per chip, each on its own Generic PCM DAC system, with 8-bit PCM, 16-bit PCM and 4-bit DPCM bodies.
  - Start/end markers, loop points, key-on/key-off, pitch, attenuation and panning are kept; reverb send is reported and omitted.
  - Each system carries one quarter of the chip's voice gain and the song master stays at 1.0.
- Irem GA20 converts four native sample voices per chip on Furnace's system `0xC7`.
  - ROM bodies stop at the first zero byte, as the chip does; the end register is not used to truncate playback.
  - Volume and mid-note frequency writes are kept, and the fixed C-4 reference matches Furnace's GA20 pitch table.
- OKI MSM5205 decodes its streamed 3/4-bit ADPCM into 16-bit PCM and splits it at attacks/silences for Generic PCM DAC banks.
  - The decoder mirrors libvgm's reset/FIFO/prescaler state, then reuses the YM2612-DAC/PWM chunking path.
  - `--lossy-sample-clustering` can reduce the bank further; it is off by default.
- OKI MSM5232 converts eight tone/noise voices per chip on Furnace's native `0xBC` system.
  - Pitch bytes map directly to note `72+pitch`; group control rides a one-value instrument duty macro, attack/decay become `12xx`/`13xx` (inverted through Furnace's tables), and noise becomes `11xx`.
  - TA7630 group volume (`0x1E/0x1F`) drives the channel volume columns, including mid-note changes.
- Ensoniq ES5503 converts up to 32 oscillators, each on its own Generic PCM DAC system.
  - Wave RAM (data block `0xE1`) is reconstructed and each 8-bit body stops at the chip's `0x00` terminator.
  - Playback rate follows `output_rate * freq / 2^(9 + resolution - size_class)`; the most common rate per body is the C-4 reference, then note/E5 carries each hit's exact pitch.
  - Volume stays the source's linear 0-255 value and the control-register output mask becomes `08xy` panning; each DAC system stays at 1.0 and the module master carries the VGM main-volume gain.
  - Sync/AM and swap modes are approximated as one-shot with a warning.
- RF5C68 and RF5C164 share one Furnace system selected by `chipType`, and a looping SSG PCM DAC stream plays once because the AY sample instrument is one-shot.
- SN76489 converts as four channels, and extreme periods or two notes on the same channel/row are approximated.
  - A clear attenuation jump back up becomes a note-on in the pattern; gradual swells stay in the volume column.
  - Held period motion becomes `E8xx`/`E9xx`/`E5xx` row effects when it fits the rows, with pitch macros reserved for sub-row motion.
  - The default system volume is 0.500, and an absolute VGM extra-header volume entry still overrides it.
  - A dual SN76489 log becomes two independent systems, while a T6W28 log is one four-channel stereo chip with `08xy` panning.
- YM2612 DAC keeps bank source samples: runs are grouped by bank offset and pitch cluster, and the longest read in each cluster is stored once.
  - Four/five-sample wait jitter at one nominal rate stays together, while a genuinely repitched use gets its own sample.
  - Shorter reads are prefix cuts or explicit note-offs, and silent or flat-line reads take no slot.
  - Consecutive one-shot runs with one pan and rate are joined, and near-identical bank bodies that differ only by a small start offset share one sample.
  - A silent run of 2,048 samples or more splits a bank run, and a body of at least 8,192 frames whose longest silent run exceeds 40% is split the same way.
  - The optional lossy clustering keeps at most 32 bodies and lets other fragments play the closest anchor, and SSG PCM and NES raw PCM use the same option.
  - Without bank offsets the fallback detects attacks, shares repeated hits and stores prefix bodies once, while a `$93` stream keeps its declared rate.
- NES APU converts pulses, triangle, noise and DPCM, with duty/envelope/linear-counter row effects and pitch/volume macros.
  - DPCM needs a type-`0x07` database plus `0x68` bank copies or a `0xC2` RAM dump, otherwise the notes are silent and the report warns.
  - `$4011` bit-bang PCM becomes 8-bit samples split at long silence and into budgeted chunks.
  - A dual-APU log becomes two NES systems with independent gates, `$C000` RAM, `0x68` windows and `$4011` streams.
  - Tone notes are halted, timers above ~16 kHz read as silence, envelope-mode decays ride the volume column, and noise/triangle notes end with their hardware counters.
- Famicom Disk System converts its single 64-step wavetable voice per chip, with either frequency-byte order, volume macros and the modulation table.
  - Hardware volume envelopes become software volume steps, only increasing modulation is available, and master volume `$4089` is folded into chip volume.
  - Volume-zero and silence writes produce note-offs; a log with both the NES APU and FDS becomes two systems.
- WonderSwan converts four wavetable/PCM voices per chip, including big-endian `0xC6` wave RAM, independent left/right volumes, sweep, noise and streamed PCM.
  - Streamed `0x89` writes become native 8-bit PCM on channel 2; a DMA-only stream is silent, and PCM timing is quantized to the detected rate and row grid.
  - Notes split on roughly semitone pitch jumps and volume attacks; global speaker volume `0x91` is ignored in stereo output.
- Capcom QSound converts sixteen 8-bit PCM voices and three 4-bit ADPCM voices per chip, with start/phase retriggers, loops, pan/surround and echo.
  - Furnace stores at most 64 KB per PCM body, ADPCM bodies are one-shot, and filter/DSP-mode registers are not exposed.
  - A body outside the supplied ROM blocks is emitted as silence; bodies are repacked, so original ROM addresses are not preserved.
- Game Boy converts two pulses, wave and noise, with per-note hardware/zombie envelopes and wave-RAM changes as a wave macro.
  - Clear pulse attacks and large held pitch jumps become note cells; pitch-only jumps carry `EAxx` legato so the hardware envelope keeps running. Slower bends become `E8xx`/`E9xx`/`E5xx` row effects, and only sub-row motion falls back to a pitch macro.
  - The noise note is a hardware table index, so the fastest NR43 entries fold onto C-5 and are not bit-exact on export.
- HuC6280 converts six wavetable voices, with periodic noise split into a transient and tone tail and DAC-stream bodies as 8-bit samples.
- Atari POKEY converts four voices per chip, including dual-chip logs, per-frame AUDC volume/waveform macros, AUDCTL macros/effects, joined 16-bit pairs, high-clock/15 kHz/filter modes and silent filter-control notes.
- Atari Lynx/Mikey converts four LFSR voices per chip with volume/pitch/duty/LFSR macros and panning, plus CPU-fed 8-bit DAC streams as native Lynx sample-map bodies.
  - LFSR notes use the source timer divider `(backup+1)<<shift`, so pitches follow the hardware except for Furnace's timer-divider clamp.
  - A streamed output register becomes a banked 0.25-second 8-bit sample at its inferred write rate; repeated chunks share one body.
  - Negative source volume is played at its magnitude because Furnace's Mikey volume column has no sign bit.
  - Volume-only mode (AUDC bit 4) is not converted because Furnace has no DC/DAC mode.
  - Two-tone mode (SKCTL bit 7) exists only on Furnace's ASAP core and is not converted.
  - AUDC distortion 7 has no separate Furnace wave tap and plays as distortion 6 with a warning.
- Seta X1-010 converts sixteen wavetable/PCM voices per chip using Furnace's native wave/envelope macros and 8-bit ROM sample maps.
- C352 needs a Furnace build with the ported core.
- K053260 loops the whole sample, and mid-note loop, KADPCM or direction changes use the key-on value.
  - A voice keyed with pan 0 is written silent and raised when its pan moves.
- Chip volumes above unity only exist in INF2 files.
- Patterns use a whole 8/4 or 16/4 phrase up to Furnace's 256-row limit, and very fine pitch motion is approximated with per-row ramps.
