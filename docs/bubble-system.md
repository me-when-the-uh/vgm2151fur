# Konami K005289 (Bubble System WSG)

The K005289 is the wavetable part of Konami's Bubble System board (Gradius,
TwinBee, Galactic Warriors). It has **two voices**, each reading a 32-byte
waveform from a 512-byte **4-bit PROM**. The chip itself is only an address
generator; the board's first AY-3-8910 drives its volume and waveform-select
pins through its I/O ports, while the second AY plays the PSG parts.

Furnace models it as **"Konami Bubble System WSG"** (system `0xAD`), two
columns with a 32x16 wavetable, using the **SCC instrument editor**
(`DIV_INS_SCC`). That is the target of this converter.

## VGM encoding

- **Clock**: header `0xEC` (VGM 1.71+), normally 3,579,545 Hz (COLOR_NTSC).
- **Register writes**: command `0x42`, three bytes. The high nibble of the
  first operand is a 4-bit register offset; the low nibble and the second
  byte form a 12-bit value.

  | offset | meaning |
  |--------|---------|
  | 0 / 1 | control channel A/B: volume in bits 0-3, waveform bank in bits 5-7 |
  | 2 / 3 | latch the 12-bit pitch (LD) |
  | 4 / 5 | trigger the latched pitch (TG) |

- **Waveform PROM**: data block `0xC3`, a 16-bit start address followed by
  the bytes. Voice 0 reads `0x000-0x0FF`, voice 1 `0x100-0x1FF`; bank `b`
  starts at `b * 32`. Each byte is one unsigned 4-bit sample (8 is center).

The MAME VGM mod used for the reference pack resolves the AY port writes
into the `0x42` control writes: the VGM already carries the WSG's volume and
waveform state, so this converter does **not** reinterpret AY writes. The
AY-8910s stay ordinary AY-8910s in the module.

## Conversion

- **Pitch.** The chip counts at the clock rate, so a voice sounds at
  `clock / (32 * (freq + 1))` with `freq = 0xFFF - register`. Furnace's
  platform runs the same table with a divider of 32 and writes
  `0x1000 - value`, which makes the source register round-trip exactly:
  the collector stores `period = 0xFFF - register`, the analyzer uses
  `furnace_period_note(..., divider=32, value=period, period_offset=1)`.
- **Gate.** There is no key-on bit. A note starts when the control volume
  leaves zero and ends when it returns. A TG that changes the pitch held for
  at least three quarters of a semitone (and a frame) splits the interval,
  exactly like the SCC's period writes; smaller movements become the
  instrument's pitch macro.
- **Volume.** The decay/re-attack contour becomes the instrument's 0-15
  volume macro; the note column is pinned so the macro is the register value.
- **Waveform.** Each bank image is deduplicated and written as a Furnace
  wavetable of height 15. A bank change during a note becomes the wave macro,
  the same mechanism the SCC uses.
- **Module layout.** The source's AY dual bit keeps both AY systems even when
  one is silent. For the reference pack that gives six AY columns followed by
  two WSG columns (module channels 6 and 7 for the WSG voices). Chip order is
  the writer's usual one: AY systems first, WSG last.
- **Mix.** Bubble System modules are written with a flat mixer: every system
  gets volume 1.0, so the AY pair and the WSG start at the same setting.
  The reference player's own mix differs (VGMPlay halves the dual AY pair
  and the cores have different output scales), so adjust the three sliders
  in Furnace if you want that balance instead.

## Verification

- `report` prints `WSG notes ... (1 chip, N waveforms, 3579545 Hz)`.
- `render` produces the WAV through Furnace's WSG core; muting the AY chips
  with `edit --chip AY8910 --value 0` leaves the WSG audible.
- Furnace's VGM **exporter has no case for `0xAD`**, so an export of a WSG
  module contains only the AY writes. For a WSG channel, `compare` therefore
  walks the `.fur`'s own pattern rows (honoring `ED` delays, `Dxx` breaks and
  the loop `Bxx`) plus each instrument's pitch macro, and compares that stream
  with the source's TG timeline. The AY channels still compare through the
  Furnace export. This is real evidence about the saved module, not a duplicate
  of the analyzer's events.

## Limits

- Two voices only, and the chip has no key-on or noise mode.
- The PROM is treated as a static 512-byte image; a rip that rewrites it
  mid-song is not modeled.
- Volume/waveform are read from the `0x42` control writes. A hand-made VGM
  that only drives the AY ports and never emits `0x42` control writes would
  need an AY-port fallback; the MAME rips already resolve that.
- Furnace builds older than the `0xAD` system will not play the module; current
  builds do.
