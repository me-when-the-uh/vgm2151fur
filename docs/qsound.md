# Capcom QSound

VGM stores the QSound clock at header `0xB4`; old 4 MHz rips describe the same
chip that libvgm and Furnace run at 60 MHz. Register writes use command `0xC4`
with a 16-bit big-endian payload followed by the register byte. Data block
`0x8F` is the sample ROM.

## Conversion

- The 16 PCM voices keep their `[start, end)` ROM bodies and loop points.
- The three ADPCM voices keep their raw 4-bit bodies and play one-shot at the
  chip's fixed nibble rate.
- PCM frequency is written through Furnace's frequency-mode pitch table
  (divider 4096), and repeated bodies are shared while each note carries its
  own pitch.
- A phase write starts a note, and a start-address rewrite retriggers an
  already-enabled voice; a start/phase pair is kept as one note.
- Panning uses `08xy` plus `12xx` for the surround/linear algorithm; both
  register layouts are mapped.
- Per-voice echo send uses `11xx`; feedback and delay use `10xx`/`3xxx`.
- Every distinct sample keeps a base instrument before macro copies are
  allocated, so a note never falls back to another sample body.
- The chip output volume stays at unity.

## known limits

- Furnace's QSound PCM memory stores at most 64 KB per body.
- ADPCM bodies do not loop, matching the core.
- Filter tables and the DSP mode register are not exposed as effects.
- A body whose range is absent from the ROM blocks is emitted as silence.
- Sample bodies are repacked, so original ROM addresses are not preserved.
