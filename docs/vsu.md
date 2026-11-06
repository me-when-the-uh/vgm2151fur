# Nintendo Virtual Boy VSU (VSU-VUE)

Six voices: five 32-entry, 6-bit wavetable channels plus one noise channel.
The five wavetables are shared; a voice chooses one through its `RAM`
register.  The converter writes Furnace's `DIV_SYSTEM_VBOY` (chip id `0x9C`,
six columns) and `DIV_INS_VBOY` (type `16`).

## VGM commands and header

- `0xC7 mmll dd`: write `dd` to register `mmll`.  The register is a **16-bit
  big-endian word index** (hardware byte address / 4), matching Furnace's
  `writeS_BE(addr >> 2)`.  Bit 15 selects a second chip.
- Header `0xC4` (VGM 1.71+): VSU clock, normally `5,000,000`.  Bit 30 marks
  a second chip.
- No sample ROM block is involved; the wavetables arrive through `0xC7`.

## Register map

| register index | meaning |
|---|---|
| `0x000-0x09F` | wave RAM: five tables of 32 6-bit samples |
| `0x0A0-0x0BF` | modulation table: 32 signed bytes |
| `0x100 + ch*0x10 + 0` | `INTL`: bit 7 key-on, bit 5 interval enable, bits 0-4 length |
| `0x100 + ch*0x10 + 1` | `LRV`: left level (high nibble), right level (low) |
| `0x100 + ch*0x10 + 2` | frequency low |
| `0x100 + ch*0x10 + 3` | frequency high (3 bits) |
| `0x100 + ch*0x10 + 4` | `EV0`: envelope value (high nibble), speed/direction (low) |
| `0x100 + ch*0x10 + 5` | `EV1`: bit 0 enable, bit 1 loop, bits 4-6 tap/sweep/mod |
| `0x100 + ch*0x10 + 6` | `RAM`: wavetable index 0-4; 5-15 reads zero |
| `0x100 + ch*0x10 + 7` | channel 4 `SweepControl` |
| `0x160` | reset; bit 0 stops every voice |

## Pitch

The hardware advances one wave step every `2048 - Frequency` VSU clocks.
Furnace's VBOY platform writes `2048 - pitchTableValue`, so the converter
stores the source's `2048 - Frequency` value and inverts Furnace's 16x period
table with the same `0x7FF` ceiling.  The label may sit about five octaves
above the sounding pitch (a 32-step wave divides the step rate by 32), but
the register and audio match.

The noise channel uses the same register mapping; the VSU core applies its
own 10x noise divisor, exactly as Furnace's core does.

## Envelopes

`EV1` bit 0 enables the hardware envelope.  Its value starts at `EV0`'s high
nibble, steps every `(EV0 & 7) + 1` envelope ticks (one tick is 76,800 VSU
clocks), and follows `EV0` bit 3 for direction.  Bit 1 loops back to the
reload value.  The converter replays this counter into a volume timeline;
Furnace drives `EV0` from a volume macro with `EV1` bit 0 clear.  Writing
`EV0` while a voice is held resets the value immediately, so a driver that
keeps writing `EV0` (Jack Bros) and one that leans on the hardware envelope
(Galactic Pinball) both work.

The `INTL` length counter (bit 5) is modelled as an automatic key-off after
`(INTL & 0x1F) + 1` interval ticks, each 19,200 VSU clocks.

## Volume and pan

`EV0`'s high nibble is the 0-15 volume and goes to the Furnace column.
`LRV` is **not** a pan position: it is a 4-bit level for each side.  `0x80`
is hard left, `0x11` is quiet on both sides, `0xFF` is full on both.  The
converter maps it directly to `08xy`, so Furnace's own `DIV_CMD_PANNING`
writes the same nibbles.

The VBOY core is about 2.2x quieter than VGMPlay at unity.  The writer carries
that factor in the song master volume (the chip's own volume stays 1.0).

## Noise

Channel 5 is the LFSR noise voice.  `EV1` bits 4-6 are the tap length (the
same field channel 4 uses for sweep/modulation).  Each note writes the tap as
Furnace `11xx`; the default tap 0 is not left behind when the source uses
tap 1 or 7.

## Channel 4 sweep and modulation

Channel 4 is the fifth wavetable voice and also owns `SweepControl` and the
modulation table.  The converter emits Furnace's native VBOY effects:

- `13xy` with the raw `SweepControl` byte for sweep.
- `14xy` for modulation: `x` is 1 (enable) or 3 (enable + phase reset), `y`
  is `SweepControl`'s high nibble (period and 8x divider).
- The instrument's `FD` feature carries the 32-byte modulation table, and
  `initModTableWithFirstWave` loads it when channel 4 starts a note.

This reuses Furnace's own VSU core, so the register-to-pitch timing is the
same as the source.

## Wavetables

Distinct settled wave images become `WAVE` blocks with height 63.  The
starting image is selected by the instrument's wave-synth entry.  A driver
that rewrites wave RAM while voices are keyed (the `a64` reference does this
about every 439 samples) gets a `10xx` pattern effect at each settled image.
Bursts of byte writes are collapsed to one image, and a module flag
`screwThis=true` keeps Furnace from inserting its channel-disable anti-click
pass that the source did not use.

`10xx` can only name 256 wavetables.  If a source settles more than that, the
converter keeps the most used images and maps the others to the nearest
retained waveform (mean squared difference over the 32 samples).

## Instrument and channel layout

The six VSU columns follow Game Boy and precede nothing else.  A VSU-only log
gets six real columns and no silent YM2151 placeholder.  Instruments are
deduplicated by their volume/pitch macros, starting wave and modulation
table; the 250-instrument limit falls back to a per-wave instrument with
row-step volume/pitch.

## Limitations

- Live modulation-table swaps are kept at the key-on image; the next note
  picks up the new table.
- The 256-entry wave palette is a best-effort reduction for per-tick
  wavetable drivers.
- Driver writes that select `RAM` 5-15 are silent on hardware and are not
  emitted as notes.
- Furnace's VSU core treats the 6-bit wave data as unipolar, while VGMPlay
  subtracts 32.  Symmetric waveforms are unaffected; a strongly asymmetric
  per-tick waveform (the `a64` stress case) can differ in DC offset and
  level.  Channel-4 modulation also uses Furnace's core, whose accumulator
  differs slightly from VGMPlay's.
