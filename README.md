# vgm2151fur

Converts arcade and console VGM/VGZ rips into Furnace Tracker modules.
Supported chips:
- Yamaha YM2151 (OPM)
- Yamaha YM2203 (OPN)
- Yamaha YM2612 (OPN2), including channel-3 special/CSM frequency layouts
- Yamaha YM2610 / YM2610B (OPNB)
- Yamaha YM2608 (OPNA)
- Yamaha YM2413 (OPLL)
- Konami VRC7 (same OPLL core with its own preset ROM)
- Yamaha YM3526 (OPL), YM3812 (OPL2) and YMF262 (OPL3), including rhythm mode
  as its own system and OPL3 four-operator voices
- Yamaha Y8950 (OPL with delta-T ADPCM voice)
- Yamaha YMF278B (OPL4): OPL3 FM plus 24 wave channels. ROM wave samples are
  extracted into the module from a YRW801 image (`VGM2151FUR_YRW801`)
- Yamaha YM2149 / General Instrument AY-3-8910 (PSG), including volume-register
  PCM playback
- Texas Instruments SN76489 (PSG)
- Konami K007232 (experimental)
- Konami K053260
- Konami SCC / SCC+ (K051649/K052539)
- OKI MSM6295
- OKI MSM6258 (Sharp X68000 ADPCM)
- SegaPCM (315-5218)
- Namco C140 and C219
- Namco C352 (requires a development build of Furnace from a compatible fork. A build with C352 support (built from https://github.com/me-when-the-uh/furnace-ports) is located in `./furnace-c352` - use at your own risk, since it's always a good idea to build from source instead of relying on someone else's builds)
- Ricoh RF5C68 (FM Towns) and RF5C164 (Sega CD)
- NES APU (Ricoh 2A03, A Famicom AY is written as a Sunsoft 5B and its notes are pitched at the chip's half clock)
- Hudson HuC6280 (PC Engine / TurboGrafx-16) - six wavetable voices with per-wave instruments, periodic noise on voices 4/5 (noise-transient plus tone-tail percussion), and direct-D/A PCM through the DAC stream (8-bit sample bodies)

A log that mixes several chip families is written as one module when the
combination is supported. `docs/merged-logs.md` lists the rules.

It runs on Windows and Linux with Python 3.11 or newer.

## Requirements

- Python 3.11 or newer
- Furnace, for the volume fit and the `render`, `compare`, and `--verify` checks. Put a Furnace build in `furnace/` next to the project, or set `VGM2151FUR_FURNACE` to the executable. Any build works: the tools run it with `-console`. The normal release also includes a console build (`furnace-...-win64-console.zip`) if you want one, but you don't need it. Without Furnace, `convert --no-normalize` still writes modules, just without the level fit

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

`packs` is the place to start. It scans a folder (remembered next time), lists the packs with track counts and chips, and opens a track list with a dash next to tracks that have no `.fur` yet. `c` converts all tracks or a selection (`2-6`, `1,4`, or a single number), `e` changes a chip volume on the converted tracks, and `r` renders one to WAV so you can hear it. The menu stores its state in `~/.vgm2151fur.json`.

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

`convert` writes one zlib-compressed `.fur` per input. `-o` sets the output folder (the default is a `fur/` folder beside the input). `--speed N` pins ticks per row when the automatic grid estimate is wrong, `--workers N` sets how many tracks run at once, and `--melody-only` / `--pcm-only` restrict the chips. A folder of packs writes each pack to its own `fur/` folder, or to `-o/<pack>`. Every track prints its size, grid speed, instrument and sample counts, and any warnings.

`report` prints the analysis without writing anything. Use it to see which chips a VGM uses, how the grid was estimated, and what was skipped.

`edit` lists or changes chip volumes. `--chip` takes a number, a chip name, or `all`; `--factor 1.1` raises the volume by 10%, and `--value 0.9` sets it outright.

`compare` exports the `.fur` through Furnace and reports stretches where its pitch stream differs from the source VGM by more than half a semitone for at least 100 ms. A run is worth listening to; no runs means the two register streams agree where it matters.

`render` writes a WAV of a `.fur` so you can hear the conversion.

`--variants` writes the 1x/2x/3x/4x row-condensing ladder side by side, and `--optimize lossless` marks the largest factor that adds no structural loss. Every variant reports `keys`/`miss` (source key-ons and the ones the pattern could not place); `--verify` exports each candidate, rejects sustained pitch divergence, and adds the precise key-on audit (`aud`, missed key-ons, effective operator field mismatches). `docs/operating.md` has the workflow and the `condense.tsv` columns.

## Tests

```
cd vgm2151fur
python -m pytest tests -q
```

The suite runs without game packs. Tests that need Furnace skip when it is missing.

## Layout

```
vgm2151fur/     the package: analyze, furwrite, convert, edit, diag
tools/          standalone helpers, including fur2vgm and fur2wav
tests/          pytest suite
docs/           operating guide, format notes, chip notes
```

Project state and workflow are in `docs/portable-context.md` and `docs/implementation-notes.md`. Chip details are in `docs/opm-notes.md`, `docs/ym2203.md`, `docs/ym2612.md`, `docs/ym2610.md`, `docs/opna-opll.md`, `docs/opl.md`, `docs/k053260.md`, `docs/scc-ay8910.md`, `docs/segapcm-c140.md`, `docs/ssg-pcm.md`, `docs/nes-vrc7.md`, `docs/huc6280.md` and `docs/c352.md`. `docs/merged-logs.md` covers multi-family modules, and `docs/psg-open-items.md` tracks the remaining AY accuracy gaps.

Open follow-ups are tracked in [TODO.md](./TODO.md).

## Known limits

- The YM2151, YM2203, YM2612, YM2610 and YM2608 models cover the OPM/OPN register sets the arcade and console drivers use; the YM3526, YM3812, YMF262, Y8950 and YMF278B models cover the OPL family (`docs/opl.md`). The YM2608 converts channel-3 Ext, its rhythm kit and delta-T ADPCM-B, but CSM timer key-ons are not synthesized (`docs/opna-opll.md`).
- The YMF278B's ROM wave samples need the YRW801 image; point `VGM2151FUR_YRW801` at the `.rom` or `.zip`. Furnace's OPL4 core does not load the ROM itself, so the converter embeds the extracted bodies. RAM wave IDs need the RAM dump in the VGM. The Y8950's ADPCM voice is silent when the file carries no delta-T data block (`$88`).
- SegaPCM and C140 mid-note volume and frequency writes become volume-column steps and pitch sweeps; start/end/loop/bank changes mid-note stay at the key-on values. The C140 sign-flip and sign-magnitude mode bits are ignored, as libvgm ignores them (`docs/segapcm-c140.md`).
- The RF5C68 and RF5C164 share one Furnace system, selected by `chipType`. A looping SSG PCM DAC stream plays once because the AY sample instrument is one-shot (`docs/ssg-pcm.md`).
- An OPL track whose log mixes OPL2 with OPL3, or two different crystals, is converted on the family that plays most of it and the report says so. Two pitched families share a module only for the combinations in `docs/merged-logs.md`. Below about C-3 one f-num step is wider than a half tone, so a very deep register lands on the nearest cell; a waveform or envelope change written inside a held note is picked up at the next key-on instead, while a modulator level the log moves mid-note travels as an operator macro. `docs/opl.md` lists both with the measured numbers.
- YM2203 notes are labelled at the highest carrier's octave when a patch uses a power-of-two carrier MULT. A constant pitch macro on the instrument keeps the source f-num/block/audio unchanged, so the pattern note matches the audible carrier.
- The YM2203's channel-3 3-slot chord is reduced to one operator, CSM timer key-ons are not synthesized, and LFO writes are skipped; the report throws a warning when it sees them. Very fast SSG ornaments shorter than a pattern row are simplified (`docs/ym2203.md`).
- The Genesis SN76489 PSG is converted as four extra channels (three tone + noise). Extreme periods below the musical table range and two PSG notes on the same channel/row are approximated. The converter's default `.fur` SN76489 system volume is 0.500; an absolute VGM extra-header volume entry still overrides it.
- The YM2612 DAC is converted from the type-0 PCM bank, direct `0x2A` writes and modern `0x92`/`0x95` blocks. The default is release-capped PCM chunks: one FM6 note per chunk, split at every `0xB6` pan change and at most every 0.25 s, with a small sample budget so release Furnace loads the module. Silence-only chunks and leading silence get no note. The per-run slice representation remains available as `collect_dac(..., slices=True)` for editing, and `timeline=True` is the old 22.05 kHz comparison body. A source that writes `0x2B` without setting bit 7 has no DAC audio, so its FM6 key-ons stay FM notes. Channel-3 special and CSM per-operator frequencies are converted (the Ext system); CSM timer key-ons are not synthesized. LFO, PMS/AMS, pan and SSG-EG are converted (`docs/ym2612.md`).
- The YM2610 uses the base 14-channel layout (16 on the B variant). Channel-2 special/CSM mode is not split into per-operator FM3 columns, and timer key-ons are not synthesized. ADPCM-B uses the source's `0x83` block, a source with 8-bit delta-T mode would need a transcode. ADPCM-A/B gates carry explicit note-offs. A different sample whose trigger shares a row moves to the next free row, and a same-sample repeat within a row keeps the later start. The source tail past the loop finder's bar is kept, so post-bar A/B gates and their stops are not dropped (`docs/ym2610.md`).
- The NES APU converts its pulses, triangle, noise and DPCM channel; duty, envelope mode and the triangle linear counter are row effects while pitch and volume become per-note instrument macros. When the 256-instrument budget is full those notes fall back to the columns. DPCM needs the sample data (a type-`0x07` database plus `0x68` bank copies, or a `0xC2` RAM dump); without it the notes are written silent and the report warns. `$4011` bit-bang PCM becomes 8-bit samples. A Famicom's AY is inferred to be a Sunsoft 5B and pitched at its half clock (`docs/nes-vrc7.md`).
- The HuC6280 converts its six wavetable voices to `DIV_INS_PCE` instruments (one per distinct 32x5-bit wave), with volume on the column and the balance register on `08xy`. Voices 4/5 periodic noise is split into a noise transient plus a tone tail when the driver clears bit 7 mid-hit, and the noise-period sweep becomes a pitch macro. Direct-D/A DAC-stream bodies become 8-bit samples (`docs/huc6280.md`). A log with no FM write omits the empty YM2151.
- Furnace 0.6.8.3 has no C352 core. A build with the ported core can play those modules (`docs/c352.md`).
- K053260 loops the whole sample. A source loop maps to the full body. Mid-note loop, KADPCM or direction changes use the key-on value. A voice keyed with pan 0 is written silent and raised when its pan moves (`docs/k053260.md`).
- Chip volumes above unity only exist in INF2 files.
- Patterns use a whole 8/4 or 16/4 phrase, up to Furnace's 256-row limit. The row grid is estimated per track, and very fine pitch motion is approximated with per-row ramps (`docs/fur-writer.md`).
