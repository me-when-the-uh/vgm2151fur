# Atari POKEY

POKEY is the four-voice sound chip in Atari's 8-bit computers. It is a
square/noise PSG with no key-on register: a voice sounds while its `AUDC`
volume nibble is non-zero. Drivers "play" it by rewriting `AUDF`/`AUDC` every
frame, so the per-frame volume ramp is the chip's envelope.

VGM writes arrive on `0xBB` as `register, value`. The register is four bits;
bit 7 of the register byte selects a second chip when the header's clock field
has bit 30 set.

## Registers

| Register | Meaning |
| --- | --- |
| `0/2/4/6` | `AUDF1-4`, 8-bit divider |
| `1/3/5/7` | `AUDC1-4`: low nibble volume, bit 4 volume-only, bits 5-7 distortion |
| `8` | `AUDCTL` |
| `15` | `SKCTL` (serial/keyboard; not converted) |

`AUDCTL`:

- bit 7: 9-bit poly (shortens the noise LFSR)
- bit 6: high clock for voice 1 (`~1.79 MHz`)
- bit 5: high clock for voice 3
- bit 4: join voices 1+2 (16-bit)
- bit 3: join voices 3+4 (16-bit)
- bit 2: high-pass filter 1+3
- bit 1: high-pass filter 2+4
- bit 0: 15 kHz clock for the non-high voices

## Conversion

- **Voices**: four columns per chip; a dual-chip log becomes two POKEY systems
  (eight columns).
- **Gate**: volume > 0 opens a note, volume 0 closes it. A large volume rise
  while the gate is open is a new hit (a drum roll).
- **Volume envelope**: the `AUDC` volume steps become a Furnace volume macro,
  one step per driver tick. The pattern column carries the opening level.
- **Waveform**: `AUDC` distortion becomes a `10xx` waveform effect (0-7).
  A change while a voice sounds is a row effect at that sample.
- **AUDCTL**: a `11xx` effect on the chip's first column. The source's
  high-clock, 15 kHz, filter and join bits are passed through.
- **Joined pairs**: one voice on the low column. `AUDF_low` is the low byte,
  `AUDF_high` the high byte, and the high channel's `AUDC` supplies volume and
  waveform. This is how VGMPlay and Furnace both read the joined register, so
  the source value is preserved. Turning the join bit off reopens the two
  independent voices.
- **Pitch**: Furnace's POKEY platform turns a pitch-table value into the
  written `AUDF` with a transform that depends on `AUDCTL` and the waveform
  (the bass/buzz waveforms divide and snap their periods). The converter
  inverts that transform, so the module writes the source's `AUDF` rather than
  an approximate closed-form value.
- **Timing**: a POKEY-only log uses the driver's register-write lattice as the
  row grid, so each frame's volume/envelope step lands on a row.

## Limitations

- **Volume-only mode** (`AUDC` bit 4) outputs a DC level, not a tone; drivers
  use it for DAC playback. Furnace's POKEY effects have no equivalent. The
  converter counts those writes and warns; the six test rips do not use it.
- **Two-tone mode** (`SKCTL` bit 7) exists only on Furnace's ASAP core. It is
  reported when seen, not converted.
- Serial, keyboard and potentiometer registers are ignored.
- POKEY has no hardware envelope like the AY; the per-frame volume writes are
  reproduced as macros/columns, which is what the source actually does.
