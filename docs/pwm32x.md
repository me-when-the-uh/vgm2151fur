# Sega 32X PWM

The 32X PWM chip is a stereo streamed DAC.  It has no phrase ROM: the driver
writes 12-bit left/right values through the DAC stream control commands.

- `0x90 stream, chip 0x11, port 2` (left) or `3` (right)
- `0x91 stream, bank 3, step size, step base`
- `0x92 stream, playback frequency` (usually 22020 Hz)
- `0x93`/`0x95` start a position or a data block
- `0xB2 ad dd` writes a control/cycle/level register

The `0x03` data block is the PCM bank.  It can be n-Bit compressed as type
`0x43`; the converter decodes subtype 0 (copy) and subtype 1 (shift left).
Items are two-byte little-endian words whose low 12 bits are the PWM value.

The converter follows the stream timeline, applies the cycle register's offset
and scale, and renders one 16-bit PCM body per side.  Because Furnace has no
32X PWM platform, each chip becomes **two Generic PCM DAC systems** (left and
right) with hard pan.  The module sample rate is 22020 Hz, matching the chip's
device rate.  Each side is cut into 0.25-second chunks and grouped through a
120-slot sample map, the same approach as the YM2612 DAC path, so the module
stays editable and does not hold one multi-megabyte sample.  When the VGM
loops, the pattern/order loop repeats the chunk sequence.

## Limits

- Table-compressed n-Bit blocks and DPCM-compressed blocks are not decoded.
- Direct level register writes are applied at their logged sample; a log that
  never uses the DAC stream has no continuous audio to reconstruct.
- The PWM output is stored as decoded PCM, so the module does not retain the
  source's per-sample register writes.
