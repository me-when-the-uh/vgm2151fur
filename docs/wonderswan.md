# Bandai WonderSwan

VGM 1.71 stores the WonderSwan clock at header `0xC0`. Register writes use
command `0xBC` (`0x80+port`, bit 7 selects a second chip) and internal-RAM
writes use `0xC6` (16-bit address, data). The sound chip sees the 16 KB RAM
through a `0x3FFF` mirror.

## Conversion

- Each voice's 32x4-bit wave is snapshotted from internal RAM through the wave
  base register `0x8F`; repeated images share one wavetable.
- The 11-bit period register maps to Furnace's period table with divider 32.
- Registers `0x08-0x0B` carry the left/right volume nibbles; the volume column
  stays at 15 and the two pan macros reproduce the register exactly.
- Channel 3 sweep writes use `12xx`/`13xx`; channel 4 noise uses `11xx`.
- Streamed PCM writes on port `0x89` become native 8-bit samples on channel 2,
  split at SNDMOD gates, long gaps and silence.

## known limits

- PCM is reconstructed from logged `0x89` writes; a DMA-only stream is silent.
- PCM timing is quantized to the detected write rate and the pattern grid.
- PCM volume register `0x94` is coarse, with two bits per side.
- Global speaker volume `0x91` is ignored in the stereo output.
- Wave RAM changed between uploads is followed at the next write burst.
