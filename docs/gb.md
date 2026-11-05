# Game Boy DMG

The converter reads the Game Boy's LR35902 APU stream (VGM command `0xB3`,
register `0x00` = `FF10`) and writes Furnace's `DIV_SYSTEM_GB` (chip id
`0x04`). The system has four columns: Pulse 1, Pulse 2, Wavetable and Noise.

## Register map

| reg | GB reg | meaning |
|-----|--------|---------|
| 0x00 | NR10 | channel 1 sweep |
| 0x01-0x04 | NR11-14 | channel 1 duty/length, envelope, period, trigger |
| 0x06-0x09 | NR21-24 | channel 2 (no sweep) |
| 0x0A | NR30 | wave DAC on/off |
| 0x0B | NR31 | wave length |
| 0x0C | NR32 | wave output level |
| 0x0D/0x0E | NR33/34 | wave period, trigger |
| 0x10-0x13 | NR41-44 | noise length, envelope, divider, trigger |
| 0x14 | NR50 | master volume (the module mix is normalised separately) |
| 0x15 | NR51 | stereo L/R enables |
| 0x16 | NR52 | APU on/off |
| 0x20-0x2F | FF30-3F | wave RAM, 32 nibbles |

The header clock at `0x80` is the DMG CPU clock (`4194304` Hz). Bit 30 of the
clock field is a second APU and the instance bit is bit 7 of the register
(Dual Chip Support #2). Only the first instance is converted; a missing clock
falls back to `4194304`.

## Pitch

Furnace's GB core divides the clock by 16 and writes `2048 - freq` to
NRx3/NRx4. The analyzer inverts that (`gb_note`) so the exported register
matches the source exactly. Only an NRx4/NR44 trigger starts a new note; a
frequency write without a trigger keeps the hardware envelope running, so it
becomes a pitch macro (this is how the drivers build arpeggios). Splitting
those would restart the envelope and hold notes the source lets decay.

Noise has no period register: `gb_noise_note` inverts Furnace's `noiseTable`.
The table covers shift 0-15 with dividers 4-7 plus the shift-0 row; an NR43
outside it picks the nearest representable hardware period (usually a
different encoding of the same inaudible high frequency). The LFSR width bit
becomes the `12xx` duty effect.

## Envelopes and "zombie mode"

Pulse 1/2 and noise have a hardware envelope in NRx2 (initial volume,
direction, period). The wave voice has no envelope; NR32 selects 0/25/50/100 %
and maps onto the volume column as 0/5/10/15.

Two encodings appear in real logs, and the collector detects them per note:

- **Hardware envelope** - the driver writes NRx2 once at the trigger. The
  instrument is `DIV_INS_GB` with `softEnv = false` and carries `envVol`,
  `envDir`, `envLen` and the length field. Furnace runs the hardware envelope.
- **Software envelope (zombie mode)** - the driver keeps rewriting NRx2 while
  the note sounds, altering the volume through the hardware's NRx2 glitch.
  The writes are replayed through the same state machine as the emulator
  (`_nrx2_step`, the CGB-E single-pass rule) and the resulting volume
  trajectory becomes a volume macro on a `softEnv = true` instrument.

A mid-note NRx2 write that changes the decay period or direction (e.g.
`0xE7 -> 0x51`, used to speed a fade up) is a hardware-envelope change. The
collector splits the note there; the new segment starts at the new high
nibble (which is the level the source audibly continues from) and carries the
new period/direction. Keeping the original instrument would decay at the old
rate and leave the note too loud for too long.

The writer tags the chip `chipType=2` (CGB-E) so Furnace's emulator uses the
same zombie rule the replay follows. A hardware-envelope note keeps the volume
column at its initial level; a software-envelope note pins the column at 15
and lets the macro carry the absolute 0-15 volume.

## Wavetables

Wave RAM is accumulated into a 16-byte image; a complete write becomes a
deduplicated 32-nibble wavetable. A note starts on the most recent image. A
rewrite while the wave voice is sounding becomes a wave macro (`std.wave`),
so the driver's mid-note morph survives without splitting the note. Furnace's
GB core inverts every nibble when it loads a wavetable (the DAC is inverted),
so the writer stores the complement to make the exported wave RAM match the
source bytes.

## Stereo

NR51's per-channel L/R enables map to Furnace's `08xy` (binary left/right);
the note carries the value in force at its start and mid-note moves become
pan effects.

## Note-offs

A voice stops when its DAC is cleared (NRx2 = 0x00 for pulse/noise, NR30 bit 7
for wave) or when the driver re-triggers it with volume 0 (the common
`NRx2 = 0x08` then `NRx4 = 0x80` idiom). That second form used to become a
silent replacement note, which left the *previous* note open in the pattern;
the collector now treats a silent re-trigger as a real note-off and emits an
off event at that sample. The wave voice's NR30 on/off is its gate.

Noise length counting needs care: Furnace writes NR44's length-enable bit when
`soundLen < 64` and NR41 = `63 - soundLen`. A source note with length counting
*disabled* therefore has to carry `soundLen = 64` (not 63); otherwise every
noise note is cut after 64 ticks even though the source sustains it.

## Noise sweeps

NR43 is the noise timbre/frequency, not a new note. A drum body can rewrite it
several times while the hardware envelope decays, so mid-note changes become a
pitch macro (`tone_pitch_pts` in note-table steps) rather than note splits.
Splitting would restart the envelope on every step and chop the decay.

## Instrument budget

GB instruments share the module's 255-instrument limit with SCC/AY/PCE. GB
binds last, so when another wavetable family has already spent most of the
budget a reserve is held back for it and the surplus collapses to plain
per-wave / per-envelope instruments (the documented column fallback).

## Mixed logs

A GB log may also declare another chip (the `overworld_south_*` rips carry an
SCC+ as well). GB is a standalone wavetable/PSG system, so it converts beside
SCC/AY/PCE the same way those combine with each other. A GB-only log drops the
legacy YM2151 placeholder and the four GB columns start the module.
