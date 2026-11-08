# Atari Lynx / Mikey

VGM 1.72 puts the Mikey clock at header `0xE4` (16 MHz on the Lynx) and sends
register writes with command `0x40` (`40 aa vv`).  Each of the four voices at
`0x20+8*n` can run as an LFSR tone or as a CPU-fed 8-bit DAC.

## LFSR voices

- Pitch comes from the timer divider `(backup + 1) << shift`; the converter
  inverts Furnace's Lynx period table to a note and `E5` pair.
- Volume is the signed `VOLCNTRL` magnitude, held in a volume macro at the
  source write rate.
- Feedback and control become the 10-bit Duty/Int macro (taps 0-5, 7, 10, 11
  and the integrate bit).
- Shift and other become the 12-bit Load LFSR macro; `3xxx` is available as a
  row effect but the converter uses the instrument macro.
- Attenuation and the stereo mute bits become `08xy` panning; a clear pan bit
  means full scale, matching the chip's attenuation bypass.

## DAC voices

A voice whose output register is written at audio rate is treated as a
software DAC.  The converter reconstructs signed 8-bit PCM at the most common
write period, banks fixed 0.25-second chunks, shares repeated bodies, and plays
them through a native Lynx sample-map instrument (`DIV_INS_AMIGA` with
`useSample` and `useNoteMap`).  The sample's C-4 rate is the inferred write
rate, so the module plays at the source speed.

## known limits

- Furnace's Lynx pitch divider is clamped to 36-16383, so very high timer rates
  (noise) and very low rates are approximated.
- Furnace's volume column has no sign, so a negative source volume plays with
  positive amplitude.
- A driver that updates the LFSR registers faster than the module tick rate is
  quantized to the tick grid.
- A second Mikey instance declared by the header has no per-instance VGM
  command, so its writes stay on the first chip.
- The DAC rate is inferred per voice; a mid-song rate change is split only when
  the write period changes enough to be detected.
