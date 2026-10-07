# Taito B YM2610 (Nastar)

Nastar (World, 1988) is the parent set `nastar`. The same sound board is on `nastarw` and `rastsag2`. Main CPU is a 68000 at 12 MHz. Sound CPU is a Z80 at 4 MHz. The chip is a YM2610 at 8 MHz.

MAME sums YM2610 outputs 0, 1, and 2 onto one mono speaker. Z80 `$E400`–`$E403` are ignored writes marked pan. That external pan is not in a YM2610 VGM. A log from this board can be mono while the cabinet pans, the same kind of gap as the Darius II mixer.

## Command

The link is a TC0140SYT. Master port is `0x800000`. Master comm is `0x800002`. The 68000 send at `$2D2C` writes port 0, then the low nibble of the command, then the high nibble. The Z80 NMI vector is `RETN`. The main loop calls `$68`, which reads the two nibbles back into one byte.

`preview nastar` steps that byte with `[` `]`. `M` appends it to `output/nastar/codes.log`. Isolate replaces the game's port write with 0 and the two comm writes with the current nibbles.

```powershell
python arcade_rip.py preview nastar
python arcade_rip.py play nastar --code 0xNN --seconds 180
```

DIP SW1:3 is the service-mode switch in the driver. The driver comment says the manual calls SW1:1–4 unused, so a sound item may not be there. Tab, Service Mode On, F3 is the fallback when the poke stays silent. Key 9 is the service coin.

## ROMs

A headless boot (`mame nastar -video none -seconds_to_run 2`) exits 2: required files are missing. The two dumps are `ampal16l8-b81-05.21` and `ampal16l8-b81-06a.22`. The program and sample files are already in `nastar.zip`. This tree does not download the PAL dumps. Preview is ready once those two files are in the rom path.

`mix` is not wired for YM2610. Keep the raw VGM.
