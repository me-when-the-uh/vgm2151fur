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
A drum track can have far more bodies than the module has instrument slots, so
`ssgpcm.py` groups 120 bodies into the same `DIV_INS_AY` instrument through
`useNoteMap` (flags `0x03`): notes 60-179 select a slot and every slot stores
C-4.  See "Instruments" below.

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

Plays that start at the **same bank offset** read the same source from the
start, so a short play is a prefix of the longest one.  The collector stores
the longest body once and gives each play its own hit; the hit's `end_time`
becomes the note-off that cuts the longer body at the shorter play's stop.
This is the same rule the banked YM2612 DAC path uses.  On the Rusty title
rip it cuts 104 bodies to 57 without changing the 758 hits.

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
- A direct-write body shorter than **128 frames** is a click or a
  segmentation fragment: it is folded into the next body on the same voice
  (or the previous one), never written as a tiny sample.  A DAC-stream play
  under 128 frames is dropped (it is a real short click, not a sample), but
  its ``0x93`` start still defines the other notes.

## Segmentation

Direct writes are split into notes at pauses of at least 46 ms (`2048`
samples).  A short hole (the 12-16 sample waits a driver leaves between
groups) stays inside one body.  Identical bodies share one instrument.  If a
chip would exceed the 160-instrument candidate budget, the split gap doubles
(92 ms, 184 ms, …) until it fits.

A DAC stream is different: **each `0x93` start is one note**, and its
`0x94` stop (or the next `0x93` start) is the end.  The declared length is
truncated to the stop when the driver stops early.  Stream plays are never
merged with each other or with direct writes: the gap between a stop and the
next start is only a few samples, and joining them would rebuild the one
giant body this avoids.  `0x92` sets the rate for the play that follows.

## Instruments

A drum track can have hundreds of distinct bodies (`03 Rusty [Title]` has
148).  One instrument per body would blow through Furnace's 255-instrument
module limit, especially beside a large FM patch list.  Each `DIV_INS_AY`
instrument therefore carries a **sample map**: 120 bodies per instrument,
selected by notes 60-179.  Every slot stores C-4, so a body still plays at its
own `c4_rate`; the note is only a slot id.  `03 Rusty` fits its 148 bodies in
2 instruments (250 total instruments with 225 FM and 22 tone AY), and
`29 Shooting Star` fits 275 in 3.

The writer emits the `DIV_INS_AY` sample-map instruments immediately before
the SCC/AY/PSG tone instruments and the `SMP2` bodies after the OPL4 wave
bodies.  The analyzer shifts the tone instrument bases by the PCM instrument
count and binds the PCM notes after the FM list is final.

## Validation

`tests/test_vgm2151fur_ssgpcm.py` covers detection, the DAC-mode gate, slow
ramps, gap splitting, dedupe, the level round-trip and the three-voice case.
A rendered rate check (200 Hz square written at 8820 Hz) comes back at
200.5 Hz through the real Furnace build.  The reference rips:

| file | chip | source | bodies | notes | instr |
| --- | --- | --- | --- | --- | --- |
| `02 Blood Soon Castle [Intro]` | YM2203 SSG3 | stream + direct | 73 | 124 | 1 |
| `20 Eye Catch` | YM2203 SSG3 | stream + direct | 70 | 78 | 1 |
| `SELECT-ay` | AY-8910 PSG3 | direct writes | 32 | 32 | 1 |
| `19 Dance Dance [Clear]` | YM2608 SSG3 | DAC stream | 56 | 57 | 1 |
| `rforce` | YM2203 SSG1-3 | direct writes | 3 | 3 | 1 |
| `03 Rusty [Title]` | YM2608 SSG3 | DAC stream | 148 | 780 | 2 |
| `29 Shooting Star [Visual 4]` | YM2608 SSG3 | DAC stream | 275 | 567 | 3 |

`rforce` keeps one 5.2 s body per voice (three simultaneous PCM voices);
`SELECT-ay` keeps one body per phrase; each OPNA `0x93` start is its own note
(`19 Dance` is 57 notes, not one 4.5 s body).
