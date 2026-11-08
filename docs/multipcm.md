# Yamaha YMW258-F (MultiPCM)

The YMW258-F, sold to Sega as the MultiPCM (315-5560), is a 28-voice sample
chip.  VGM addresses it through an indirect register port:

- `0xB5 0x01 dd` selects a channel (0-31; every eighth selector is invalid).
- `0xB5 0x02 dd` selects the channel register (0-10).
- `0xB5 0x00 dd` writes `dd` to the selected register.
- `0xC3 cc bbaa` selects a Sega ROM bank (register `0x10`/`0x11`/`0x12`).
- Bit 7 of a register byte selects the second chip instance.

The module uses Furnace's native MultiPCM system: 28 columns per instance.  A
`0x89` data block carries the ROM; the sample header at `(index * 12) & mask`
holds:

```
0-2   start address; bits 20-23 are the format (bit 2 = 12-bit)
3-4   loop start, relative to the body
5-6   0x10000 - body length
7     LFO rate and vibrato depth
8     attack rate and decay 1 rate
9     decay level and decay 2 rate
10    rate correction and release rate
11    amplitude LFO depth
```

Only the bodies a song triggers are written.  Both 8-bit and packed 12-bit
bodies are supported, and the source loop/end points are preserved.

## Pitch and level

The channel register pair 2/3 holds a signed 4-bit octave and a 10-bit f-num.
The playback rate is

```
chip_rate * (1024 + fnum) / 1024 * 2 ** (octave - 1)
```

where `chip_rate = VGM clock / 180`.  Furnace's pitch table and E5 fine tune
reproduce the register pair, quantised to the module's note grid.

Register 5 is total level: the module volume column is `127 - TL`.  Register 0
is the chip's panpot.  Its values are not a normal balance (0 is both channels
full, 8 is the hardware mute value, 1-7 attenuate left and 9-15 attenuate
right); the converter inverts Furnace's pan conversion so the source nibble is
restored.

Register 4 bit 7 is key-on and clear is key-off.  Mid-note pitch writes become
sweeps, volume/pan writes become volume points, and LFO writes become effects
`20xx`/`21xx`/`22xx` (PCM LFO rate, PM depth and AM depth).

## Limits

- Furnace's MultiPCM platform holds 512 samples and 2 MB of sample memory.
- A dual-instance header keeps both 28-column systems even when the second
  chip never sounds; the current corpus has no real dual MultiPCM example.
- Furnace's MultiPCM core runs quiet; the converter uses the chip mixer's
  2.0 ceiling and leaves the master at 1.0, so the normalizer can still fit
  the source without exceeding Furnace's 3.0 master cap.
- The module instrument index is what the chip's sample register selects, so
  source sample indices are remapped; the body and header still come from the
  source index.
- ADSR/LFO values come from the source sample header.  A driver that rewrites
  registers 7-10 after key-on is not given a per-note envelope copy.
- A `0xC3` bank uses VGMPlay's `/0x10` (1 MB) or `/0x08` (512 KB) scaling.
