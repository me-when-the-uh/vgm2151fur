# NES APU and Konami VRC7

The NES APU (Ricoh 2A03) converts to Furnace's `DIV_SYSTEM_NES` (`0x06`, five
channels). The VRC7 is the YM2413/OPLL core with six channels and its own
preset ROM, converted to `DIV_SYSTEM_VRC7` (`0x9D`).

## NES APU registers

VGM command `0xB4` (register, data). The register is `0x00-0x17` = `$4000-$4017`
and bit 7 selects a second APU instance.

| reg | channel | meaning |
| --- | --- | --- |
| `0x00/0x04` | pulse 1/2 | duty (bits 6-7), envelope/constant (bit 4), length halt (bit 5), level/period (bits 0-3) |
| `0x01/0x05` | pulse 1/2 | sweep |
| `0x02/0x06` | pulse 1/2 | timer low |
| `0x03/0x07` | pulse 1/2 | timer high (bits 0-2) + length counter |
| `0x08` | triangle | linear counter / control |
| `0x0A/0x0B` | triangle | timer low / high + length |
| `0x0C` | noise | envelope/constant (bit 4), halt (bit 5), level/period (bits 0-3) |
| `0x0E/0x0F` | noise | mode bit 7 + period / length |
| `0x10` | DPCM | rate (bits 0-3), loop (bit 6), IRQ (bit 7) |
| `0x11` | DPCM | 7-bit DAC level |
| `0x12/0x13` | DPCM | sample address / length |
| `0x15` | all | channel enable (bits 0-4) |

### Tone channels

The APU has no key-on register, so a voice sounds while it is enabled in
`$4015`, its length counter is loaded or halted, and the volume (or the
running envelope) is non-zero. The collector keeps one note across the
per-frame same-pitch rewrites common in NES drivers. A timer change of about a
semitone is a new note; smaller changes are a pitch trajectory.

- **Pitch**: `freq = clock / (16 * (timer + 1))`. Furnace's NES platform
  writes `period = pitch_table[n] - 1`, so the inverse asks the table for
  `timer + 1`.
- **Volume**: the low nibble of `$4000`/`$4004`/`$400C` is the pattern volume
  (0-15). In envelope mode it is the envelope period, which is what the
  register holds and what Furnace writes back.
- **Duty / noise mode**: `12xx` row effect at the note and on changes.
- **Envelope mode** (`$4000` bits 4-5): `15xx` row effect.
- **Triangle linear counter** (`$4008`): `19xx` row effect. The volume column
  only gates the triangle.
- Mid-note pitch and volume motion become per-note instrument macros
  (`std.pitch`, `std.vol`), so the changes survive a coarse row grid. If the
  instrument budget is full, `_take_tone` hands that note a plain instrument
  and the same motion stays on the columns (`sweep`/`vol_pts`).

## DPCM channel

### Sample playback

A DPCM playback is `$4010` (rate/loop) + `$4012` (address) + `$4013` (length)
followed by `$4015` bit 4. The sample address is `$C000 + ($4012 << 6)` and
the body length is `($4013 << 4) + 1` bytes of 1-bit DPCM.

The sample memory can arrive in two forms:

- **Type `0xC2` RAM dump**: a 16-bit start address followed by the bytes.
- **Type `0x07` database**: a flat bank. `0x68` commands then copy 4 KB
  windows of it into `$C000`/`$D000` while the song plays, so the collector
  replays those copies in time order before reading each sample. This is how
  the Gimmick! FME7 rip banks its DPCM data.

The bodies become `SMP2` samples with depth 1 (1-bit DPCM) and a
`DIV_INS_NES` instrument per body. The pattern note is `60 + rate index`: the
NES platform reads the DMC rate from the note, 0-15. A looping `$4010` stores
the whole body as the loop. A trigger with no dumped data still keeps its
timing but plays silence, and the report warns.

### Direct `$4011` PCM

Some drivers bypass DPCM and bit-bang the 7-bit DAC at audio rate. Runs of at
least eight `$4011` writes with a median spacing of 16 samples or less become
8-bit sample bodies (sample-and-hold, split at pauses of 46 ms, deduplicated,
minimum 128 frames). The body byte is `(level << 1) - 0x80`, the inverse of
the NES platform's `(data8 + 0x80) >> 1`.

A long capture is **not** stored as one sample. It is split first at long
contiguous zero runs (46 ms or more); the silent stretch is not sample data,
so the channel simply goes OFF there and the next sounding part gets its own
note at the source time. Each sounding part is then cut into chunks of at most
8192 frames, with the chunk size grown if needed to keep the whole capture
inside the same 240-sample budget the YM2612 DAC uses (Furnace sample indices
are one byte). The chunks use 120-slot sample-map instruments like the DAC:
note `60 + slot` selects the chunk and every slot plays at C-4 at the body's
stored rate, so a small tracker/sample-rate error can only drift inside one
chunk instead of across the song. `18xx = 00` selects PCM mode.

## Famicom AY = Sunsoft 5B

A Famicom expansion AY is a Sunsoft 5B with its SEL pin tied low, so the chip
runs at half the logged CPU clock. The VGM often does not carry that state
(the Gimmick! file sets the YM2149 pin-26 flag in header byte `0x79` bit 4;
files without it are inferred from the NES APU pairing). In both cases:

- the module writes the AY as `chipType=2` (Sunsoft 5B) with the logged clock;
- the analyzer labels the AY notes at half speed, so the written period is
  doubled and the chip sounds at the real /2 rate.

A stand-alone YM2149 with the pin-26 flag but no NES APU halves the notes the
same way and stays on the YM2149 type.

## VRC7

Bit 31 of the YM2413 clock field (header `0x10`) asks for a Konami VRC7. The
low 30 bits are the clock (3579545 for the usual part). The register stream is
the same `0x51` write the OPLL uses; the differences are six channels
(`$10-$15`, `$20-$25`, `$30-$35`), no `$0E` rhythm mode, and the VRC7 preset
ROM. Furnace's `DIV_SYSTEM_VRC7` already carries the VRC7 core and presets, so
the converter only:

- reads the flag and writes `chipType` as system `0x9D` instead of `0x89`;
- sizes the OPLL channel block to six and disables the drum layout;
- writes the same `DIV_INS_OPLL` instruments (preset = high nibble of `$3x`).

Pitch uses the OPLL frequency base and 9-bit f-number, unchanged.

## Limits

- Sub-frame gates (shorter than 100 samples) are dropped as driver
  housekeeping; the report counts them.
- The NES `$4017` frame-counter writes are not converted; the platform keeps
  its default frame mode.
- Sweep registers (`$4001`/`$4005`) are not converted.
- A DPCM trigger whose data is not in the file keeps its note timing but
  plays silence.
- AY note macros are capped at 250 total instruments; overflow notes fall
  back to row effects.
