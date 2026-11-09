# Nintendo Famicom Disk System

VGM carries the FDS through the NES command `0xB4`. libvgm remaps offsets
`0x20-0x3F` to `$4080-$408A` (and `0x3F` to the `$4023` I/O enable), while
`0x40-0x7F` is the 64-byte wave table. The FDS runs from the NES CPU clock in
header `0x84`; the FDS bit in that field marks the mapper.

## Conversion

- The 64x6-bit wave table becomes a Furnace wavetable; repeated images share
  one block.
- The 12-bit frequency register is written through Furnace's frequency-mode
  pitch table (`CHIP_FREQBASE` 262144).
- `$4080` volume becomes instrument volume macros or column steps, with a
  note-off when the level reaches zero.
- The 32-byte modulation table and its depth/speed/position start state ride
  the FDS instrument feature; live changes use `11xx-14xx`.
- NTSC, PAL and Dendy clocks map to `clockSel`; other clocks use
  `customClock`.

## known limits

- Hardware volume envelopes are replayed as software volume steps.
- Furnace's FDS platform always uses an increasing modulation envelope.
- The master volume register `$4089` is folded into the chip volume, so live
  changes are not followed.
- Wave RAM is snapshotted at note starts and at upload bursts.
- A log with both the NES APU and FDS is written as two systems.
