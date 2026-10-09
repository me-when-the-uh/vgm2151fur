# SegaPCM and Namco C140 sample support

Both chips convert to their native Furnace systems, with no stand-in
carrier. VGM writes `0xC0` (SegaPCM) and `0xD4` (C140) and ROM blocks `0x80`
/ `0x8D` are parsed, samples are extracted per voice, and loops are
preserved through the SMP2 loop points.

## Chips

| | SegaPCM | Namco C140 |
|---|---|---|
| `.fur` system | 0x9B (16 voices) | 0xCE (24 voices) |
| instrument type | 39 | 53 |
| VGM writes | `0xC0` u16-LE address + data | `0xD4` u16-**BE** 9-bit address + data |
| ROM block | `0x80` | `0x8D` |
| clock | header 0x38 (VGM ≥1.51) | header 0xA8 + type byte 0x96 (0 = System 2, 1 = System 21) |
| registers | voice = `(off>>3)&0xF`, reg = `off&7` | `voice = addr>>4`, reg = `addr&0xF` |
| | vol L/R 0x02/0x03 (7-bit), loop 0x04/0x05, end 0x06, delta 0x07; start 0x84/0x85, ctrl 0x86 | vol R/L 0x00/0x01, freq 0x02/0x03, bank 0x04, mode 0x05, start 0x06/0x07, end 0x08/0x09, loop 0x0A/0x0B |
| active | ctrl bit0 clear | mode bit7 (bit6 retriggers when keyed) |
| loop | ctrl bit1 clear | mode bit4 |
| end | 256-byte page: `end_byte = ((end+1)&0xFF) << 8` | address count; one ROM byte per step |

Furnace converts a sample's C-4 rate into a chip frequency with
`freq = CHIP_FREQBASE * rate / clock` (SegaPCM base 32768, C140 base
12582912), so each sample's C-4 rate here is `freq * clock / base`.

## C140 pitch and clock model

Reference step rate (MAME and libvgm's c140.c): a voice steps
`freq * clock / 18874368` address words per second (`clock/288` sample rate
with a 16.16 fixed-point position; 18874368 = 288 * 65536). C-4 rates target
exactly that.

Furnace's C140 core ticks at `clock/192` with a plain 16-bit fraction, 1.5x
faster, and its VGM export multiplies the clock flag by 1.5. So the `.fur`
clock flag carries 2/3 of the crystal: 12288000 becomes 8192000, Furnace's
default, and no custom clock is written. All three faces then agree:

- Furnace playback rate = the sample's C-4 rate = the reference rate;
- the exported clock = flag * 1.5 = the source crystal;
- exported frequency registers = `BASE * rate / clock` are scale-invariant,
  so they equal the source registers exactly.

Legacy clock fields resolve the way the players do: `21390` (the 8MHz/374
rate some System 21 rips carry, even in files that predate the type field)
maps to the 12288000 default, and other sub-1MHz values are multiplied by
576. A pre-1.61 file carrying `21390` is converted with the System 21
address mapping: its register stream is System 21 even though the 1.61 type
field is absent.

## What gets written

- One `.fur` instrument + SMP2 sample per distinct played region. SegaPCM
  bodies are the banked ROM bytes XOR `0x80`. C140 linear bodies are the raw
  signed bytes. C140 mulaw (mode bit 3) is stored as 16-bit linear
  (`depth` 16) from the full table, so Furnace does not decode it again.
  Loop points are in frames.
- Pitch per trigger as note + E5 fine tune (ratios in semitones); the volume
  column carries the chip's own register value (7-bit for SegaPCM, 8-bit for
  C140); pan comes from the L/R registers via `08xy`.
- A C140 voice key-off (mode bit 7 clear) emits a note-off on that channel,
  so a looping sample stops when the chip stops it instead of ringing until
  the next note on the voice.
- SegaPCM flags: `clockSel`/`customClock` (X and Y board clocks) and
  `memSize` from the dumped ROM size; C140 flags: `bankType` from the VGM
  type byte plus `customClock`.

## Where the bytes come from

SegaPCM ROM address is `((ctrl & mask) << shift) | reg16` where `shift` is
header byte `0x3C`, `mask` is header byte `0x3E` (0 means `0x70`), then
masked with `0x1fffff >> shift`. The bank bits live in the same control
write as key-on. Ignoring them would leave every body as the silence byte
`0x80` on drivers whose bank bits are set.

C140 ROM address is libvgm `find_sample` (one byte per address, not a 16-bit
word). System 2: `((adrs & 0x200000) >> 2) | (adrs & 0x7ffff)` with
`adrs = (bank << 16) | start`. System 21:
`((adrs & 0x300000) >> 1) + (adrs & 0x7ffff)`. A trigger whose address is
outside every dumped slice is skipped:
`N C140 triggers point at ROM that the VGM does not dump`.

## SegaPCM retrigger

`0x84`/`0x85` are the live cursor. libvgm writes them back every tick; MAME
`voice_addr_w` replaces those bits of the internal address and keeps the
fractional byte. Once the cursor has moved, a CPU write of the original
start seeks back to it, and that seek is a new note on the same sample.
Some drivers rewrite the start every frame with the channel left enabled;
those restarts must all be kept.

One Z80 dump writes the address, then the end page and delta, then control,
across at most 4 VGM samples. Those writes are one note, snapshotted after
the dump, so the end page and delta are the values that play. A later dump
is a new note when it changes the cursor byte, enables a stopped voice
(including a one-shot the chip itself has already stopped), or changes the
bank. A volume or delta rewrite that leaves the cursor on the same byte
stays on the current note.

The row grid is then tightened, inside the 256-order budget, until every
restart the budget can hold has its own row. Two writes closer than that
share a cell; the later write is the one the channel keeps playing. The
tick stays near 90 samples and the reported BPM is kept by scaling the row
subdivision.

## Mid-note writes

Drivers rewrite the volume and frequency registers while a voice is keyed;
the register mirror keeps both step streams instead of only the key-on
values:

- volume (0x00/0x01): a fade steps both registers; each pair change lands
  in the volume column (level = loud side) on rows without a note, with the
  L/R balance riding along as 08xx when the pair's ratio changes. Furnace
  feeds the column into the voice gain live (`chVol = outVol*pan/255`), so
  fades survive.
- fade-in keys: a hit whose volume registers are still zero at key-on
  (typical for the first trigger of a voice, and for every fade-in) used to
  write 08 00 from the zero balance, "both off", which no later volume step
  can undo, so the note stayed silent forever. Such hits now start silent
  on the default balance and the ramp steps carry the balance; hits whose
  registers stay zero (drivers that mute by zeroing both) keep the 08 00.
- frequency (0x02/0x03): MSB/LSB pairs within 16 samples merge to the final
  value (the intermediate is a write-order transient); a write within 120
  samples of key-on is the note's own pitch (the same refit the FM sweeps
  use), the rest become 1/64-semitone deltas and go through the FM sweep
  encoder as 01xx/02xx ramps with E5 pins.
- start/end/loop/bank changes mid-note are still not converted (the sample
  body is fixed at key-on).

## Played-frequency references

Drivers often key a voice on before writing its frequency, so the key-on
snapshot holds zeros or the previous note's leftover. The sample's
reference rate (what its C-4 plays at) must use each hit's played frequency:
the key-on value, unless a frequency write lands within 120 samples of
key-on. A later write still wins when the key-on value was zero, because
nothing was playing yet; zeros are ignored when picking a sample's most
common frequency. SegaPCM's reference ignores zero key-ons the same way.
Taking the raw key-on mode instead pins a sample mostly keyed that way at
rate 0, about five octaves off and invisible to octave-blind checks.

## PCM slides and transpose

PCM `01xx`/`02xx` slides move 1/128 st per parameter unit per tick, half the
FM rate. PCM parameters are doubled with the ideal rate capped at 127.5
units/tick, the engine's 255 maximum, so glides run at the source's speed
and reach their full range.

A mid-note write that clears RL (regs 0x20-0x27, RL == 0) cuts the channel
instantly on hardware, voices still keyed; such writes emit the note-off.
The patches' RR releases them in milliseconds.

Furnace's `E8xx`/`E9xx` quick legato transposes the playing note by y
semitones in one write, persists until the next note and resets at its
key-on. On the sample chips that is an absolute pitch set in the note's
domain, not the relative transpose the FM platform performs. Sweeps land
any overshoot beyond what a row's slide can deliver (127.5 units/tick PCM,
255 FM) with an `E8`/`E9`, letting the slide cover the sub-semitone
remainder; register jumps, dive attacks and attack bends then step
instantly instead of slurring at the rate cap. Sub-tick sweeps (a register
change within one row of the key-on) also land via a transpose on the
following row instead of being dropped.

E5 scales differ per platform: one byte step is 1/64 st on FM but 1/128 st
on PCM (a static E5 0x40 lands exactly -0.5 st below 0x80 on the C140), so
sweep pin offsets convert per platform. The sweep model accrues the
movement the *engine* makes, not the ideal rate: a glide that wants 2.66
units/tick is written as `01 03` (the parameter is a whole number), and
crediting the ideal rate compounds a fraction-of-a-unit error into
semitones over long climbs.

## Limits

- SegaPCM mid-note volume/frequency writes are not converted; each note
  keeps its trigger-time values. C140's are.
- C140 sign-flip (mode bit6) and sign-magnitude (bit0) are ignored. libvgm,
  which these VGMs are logged against, does not apply them either.
- A C140 voice keyed with an invalid sample window (end below start) is
  stored as a 1-frame loop, which Furnace plays as a static buzz; the chip
  plays past `end` instead, so the body differs. Rare, and only when the
  driver leaves a voice's window registers unset at key-on.
- PCM slide trajectories are row quantized: a dive faster than the slide
  engine can move (127.5 units/tick for PCM, 255 for FM, one tick per row
  at speed 1) lands short until its rows catch up. PCM ramps start on the
  row after their note so a row's 01xx/02xx travel cannot leak into the
  key-on write.
- Tracks longer than 256 orders are truncated by the pattern limit.
  Restarts closer than that budget share a row; the later write is kept.

## Auditing a conversion

    python fur_soundcheck.py "<source.vgm|vgz>" "output/<pack>/fur/<track>.fur"

Renders the source, the `.fur` (through Furnace) and an exported VGM; prints
the pitch-shift table for both comparisons plus the VGM `Dev` lines, so chip
clocks can be compared. Healthy: best shift 0 in both pairs and equal
clocks; the renders are kept under `<fur folder>/soundcheck/`.

- `pitch_shift.py A.wav B.wav`: the measurement alone. Octave-blind by
  design: a 1.5x clock error reads as `-5` (= `+7.02` mod 12). Read the
  whole table.
- `fur2wav.py <fur>`: render any `.fur` through Furnace to WAV.
- `fur2vgm.py <fur>` + `vgm2wav-mute`: portable export + render.
- `vgm_scan.py`: list every SegaPCM/C140/C219 pack in the tree.
