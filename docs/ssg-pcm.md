# SSG/PSG PCM playback (`SSGPCM`)

AY-3-8910, YM2149 and the SSG inside the YM2203/YM2608/YM2610 have no sample
hardware, but they can still play raw PCM: the driver disables the voice's
**tone and noise** bits in the mixer and writes the **volume register**
(`$08`/`$09`/`$0A`) at audio rate.  The volume register then acts as a 4-bit
DAC and the channel outputs the held level.

Furnace's AY platform supports the same idea.  An `INS2` instrument of type
`DIV_INS_AY` (6) with the `SM` feature's `useSample` flag set plays a raw
8-bit body on that channel.  `platform/ay.cpp` (`runDAC`) writes
`dacLogTableAY[data8 ^ 0x80]` to the volume register.  The reference module
`SSGPCM-test.fur` uses exactly this shape:

```
INS2 type 6, SM: initSample = N, flags = 0x02, waveLen = 31, no note map
PATN note = 108 (C-4)
SMP2: 8-bit, c4_rate = the source write rate
```

Note 108 is C-4 in Furnace's engine numbering (`dispatch.h`: note 60 = C-0),
which is exactly the sample's stored `c4_rate`; the body therefore plays 1:1.
`ssgpcm.py` writes the same shape.

## Detection

Only writes made while the voice is in DAC mode count:

```
tone_off  = mixer bit voice      set
noise_off = mixer bit voice + 3  set
envelope  = volume register bit 4 clear
```

A normal tone driver writes the volume at most once per frame (~735 samples).
A PCM stream writes every 2-16 samples.  `_bursts` keeps runs of at least
eight writes whose median spacing is at most 16 samples, so a slow volume
envelope or tremolo is not mistaken for PCM even while the mixer is off.

### DAC-stream commands (`0x90`-`0x95`)

Many PC-98 and arcade logs do not write the volume register directly.  They
set up a VGM DAC stream (`0x90 ss tt pp cc`) aimed at the SSG volume register
and start it with `0x93`.  `tt` is the header chip id (`0x06` YM2203, `0x07`
YM2608, `0x08` YM2610, `0x12` AY-8910); `cc` is `$08`/`$09`/`$0A`.  The data
bank is the type-`0x00` block, and the stream writes one byte per step at the
`0x92` rate.

`_stream_points` expands **every** play in full: mode 1 uses the whole byte
length, mode 2 converts the millisecond length at the stream rate, and mode 3
uses the rest of the bank.  A 2,000-byte slice is 2,000 register writes, not
one block; this is what "use the entirety of their lengths" means.  The
declared stream rate (typically 15.9 kHz) becomes the sample's `c4_rate`
instead of being inferred from the 44.1 kHz-rounded timestamps.  A looping
stream is played once (the AY sample instrument is one-shot) and reported.

The DAC-mode condition is what separates the two uses of one channel: in
`02 Blood Soon Castle [Intro]` the voice plays a normal tone with a slow
volume ramp for the first minute, then the mixer turns its tone off and the
same register carries drum bursts.

## Bodies

- A DAC stream supplies its own rate.  Direct writes use
  `round(44100 / median write spacing)`, shared by every body of one chip
  instance so simultaneous voices stay in sync.
- The waveform is sample-and-hold of the register values at that rate over
  the real timestamps, so its duration matches the source.
- Each 4-bit level is stored as the byte whose `dacLogTableAY` entry is that
  level (`_LEVEL_BYTE`), so `runDAC` writes the exact source register value
  back.  Level 1 is absent from Furnace's table; it maps to level 2.
- Bodies are one-shot and 8-bit (`SMP_DEPTH_8BIT`).
- A body shorter than **512 frames** is a click or a segmentation fragment:
  it is folded into the next body on the same voice (or the previous one),
  never written as a tiny sample.  The source audio is kept, including the
  pause, rather than dropped.

## Segmentation

A PCM stretch is split into notes at pauses of at least 46 ms (`2048`
samples).  A short hole (the 12-16 sample waits a driver leaves between
groups) stays inside one body.  Identical bodies share one instrument.  If a
chip would exceed the 160-instrument PCM budget, the split gap doubles
(92 ms, 184 ms, …) until it fits, so a dense drum track degrades to longer
bodies instead of running out of instrument slots.

DAC-stream plays are contiguous slices of one stream, so they normally join
into one body per continuous run (the OPNA example below is one 4.5 s body).

## Layout

The writer emits the `DIV_INS_AY` sample instruments immediately before the
SCC/AY/PSG tone instruments and the `SMP2` bodies after the OPL4 wave bodies.
The analyzer shifts the tone instrument bases by the PCM count and binds the
PCM notes after the FM list is final, so the instrument byte in each pattern
cell addresses the right body.

## Validation

`tests/test_vgm2151fur_ssgpcm.py` covers detection, the DAC-mode gate, slow
ramps, gap splitting, dedupe, the level round-trip and the three-voice case.
A rendered rate check (200 Hz square written at 8820 Hz) comes back at
200.5 Hz through the real Furnace build.  The reference rips:

| file | chip | source | bodies | notes |
| --- | --- | --- | --- | --- |
| `02 Blood Soon Castle [Intro]` | YM2203 SSG3 | DAC stream | 27 | 47 |
| `20 Eye Catch` | YM2203 SSG3 | DAC stream | 9 | 9 |
| `SELECT-ay` | AY-8910 PSG3 | direct writes | 32 | 32 |
| `19 Dance Dance [Clear]` | YM2608 SSG3 | DAC stream | 1 | 1 |
| `rforce` | YM2203 SSG1-3 | direct writes | 3 | 3 |

`rforce` keeps one 5.2 s body per voice (three simultaneous PCM voices);
`SELECT-ay` keeps one body per phrase; the OPNA stream is one 4.5 s body.
