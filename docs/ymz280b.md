# Yamaha YMZ280B (PCMD8)

Eight sample voices, each able to play 4-bit ADPCM, 8-bit PCM or 16-bit PCM
from up to 16 MB of external ROM. VGM command `0x5D` writes a register/value
pair. A second chip uses `0xAD`, selected by bit 30 of header clock `0x68`.

The converter writes Furnace's `DIV_SYSTEM_YMZ280B` (chip id `0xB8`, eight
columns) and `DIV_INS_YMZ280B`.

## Register map

| register | meaning |
|---|---|
| `0x00 + ch*4` | frequency low 8 bits |
| `0x01 + ch*4` | control: bit 7 key-on, bit 4 loop, bit 0 frequency bit 8, bits 5-6 mode |
| `0x02 + ch*4` | total level, linear 0-255 |
| `0x03 + ch*4` | pan 0-15 |
| `0x20/0x40/0x60 + ch*4` | start address high/mid/low |
| `0x21/0x41/0x61 + ch*4` | loop-start address high/mid/low |
| `0x22/0x42/0x62 + ch*4` | loop-end address high/mid/low |
| `0x23/0x43/0x63 + ch*4` | stop address high/mid/low |
| `0x84-0x87` | RAM address latch and byte write |
| `0xFF` | bit 7 key-on enable; bit 6 external memory enable |

Mode bits: `1` = 4-bit ADPCM, `2` = 8-bit PCM, `3` = 16-bit PCM. A control
write with the mode bits zero keeps the previous mode.

Address registers are byte addresses into the sample ROM. They are latched at
key-on; rewriting start, stop or loop while the voice is held does not affect
the current note. Only a 0-to-1 transition of control bit 7 starts a note, so
repeated `0x80` writes while the voice is held are pitch/loop updates, not
retriggers. Control bit 7 0 is a key-off.

The VGM ROM is a type-`0x86` data block. Its size field carries the chip
instance in bit 31 for a dual-chip log. Each block has the usual ROM-size and
start-address header followed by the ROM bytes.

## Playback rate

```text
source frames/second = clock * (fnum + 1) / 98304
```

The four-bit ADPCM mode uses only the low eight frequency bits, so its maximum
is `clock / 384` (44.1 kHz at 16.9344 MHz). 8-bit and 16-bit PCM use nine bits
and reach `clock / 192` (88.2 kHz).

## Sample formats

- **8-bit PCM**: raw signed ROM bytes.
- **16-bit PCM**: ROM is big-endian; the converter swaps each pair because
  Furnace's `SMP2` depth 16 stores little-endian frames.
- **4-bit ADPCM**: raw packed bytes, high nibble first, stored as Furnace
  depth 3 (`YMZ/YMU ADPCM`). Loop points become nibble counts.

Each distinct `(chip, depth, start, stop, loop start/end)` body becomes one
sample. Repeated drum hits on the same ROM range share it.

## Loops and note ends

Loop start/end/stop are byte addresses. For 8-bit bodies the sample's loop
points are byte offsets from the body start; for 16-bit they are frames; for
ADPCM they are nibbles. A loop outside the body is disabled and reported.

A key-off ends a looping body. A one-shot body also ends at its natural ROM
stop, so the module releases the keyed voice there instead of holding silence.
A body that still loops without a key-off holds to the end of the log.

## Volume and pan

Register `0x02` is linear amplitude and goes straight to the Furnace volume
column: the YMZ platform reports a maximum volume of 255. Register `0x03`
uses the chip law:

- pan 8 = centre
- pan 1 = hard left (pan 0 is treated the same)
- pan 15 = hard right
- intermediate values attenuate the opposite side linearly

These become `08xy` split-pan effects. Mid-note level writes become
volume-column steps; mid-note frequency writes become pitch sweeps.

## Instrument and channel layout

The YMZ280B columns follow K053260 and precede SCC/AY/NES/PCE/Game Boy. A
YMZ-only log gets 8 (or 16 with two chips) real columns and no silent YM2151
placeholder. A log with an OPL or OPN family places the YMZ columns after
that FM block.

Each body gets one `DIV_INS_YMZ280B` instrument. This keeps patterns musical
and matches the per-sample instrument model used by SegaPCM and C140.

## Dual chip

`07 Stage4.vgm` is the reference: header `0x68 = 0x41026600` (dual bit plus
16.9344 MHz), chip 0 writes on `0x5D`, chip 1 on `0xAD`, and two type-`0x86`
ROM blocks carry chip ids 0 and 1 in the size high bit. The module gets two
`0xB8` systems and sixteen columns.

## Limitations

- The DSP registers (`0x80-0x82`) are ignored.
- Register RAM writes (`0x84-0x87`) are reported but not replayed; the
  converter slices the type-`0x86` ROM block.
- A key-on while `0xFF` bit 7 is clear cannot sound on the chip and is
  reported instead of being converted.
- Changing loop or mode while a voice is held keeps the key-on body; the
  source's live change is reported by the collector rather than split.
