# SegaPCM and Namco C140 sample support (0.9.20)

Both chips convert to their native Furnace systems, with no stand-in carrier. VGM
writes `0xC0` (SegaPCM) and `0xD4` (C140) and ROM blocks `0x80` / `0x8D` are
parsed, samples are extracted per voice, and loops are preserved through the
SMP2 loop points.

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

## C140 pitch and clock model (0.9.18)

Reference step rate (what MAME 0.288 and libvgm's c140.c produce): a voice
steps `freq * clock / 18874368` address words per second (`clock/288` sample
rate with a 16.16 fixed-point position; 18874368 = 288 * 65536). C-4 rates
now target exactly that.

Furnace shows the same registers 1.5x faster: its C140 core ticks at
`clock/192` with a plain 16-bit fraction, and its VGM export multiplies the
clock flag by 1.5. So the `.fur` clock flag carries **2/3 of the crystal**:
12288000 becomes 8192000, Furnace's default, and no custom clock is written.
All three faces then agree with the source:

- Furnace playback rate = the sample's C-4 rate = the reference rate;
- the exported clock = flag * 1.5 = 12288000, same as the source rip;
- exported frequency registers = `BASE * rate / clock` are scale-invariant,
  so they still equal the source registers exactly.

0.9.17 targeted the 12582912 base with the raw crystal as the clock:
playback was 1.5x (+7.02 semitones) high and every export declared clock
18432000 instead of 12288000. That is the same 1.5x the old (pre-0.51)
libvgm clock divider had — fixed upstream in the 2018 commit "C140/C219/
C352: fix chip clock/divider" (384 -> 288), and the reason the symptom was
recognisable.

Legacy clock fields resolve the way the players do: `21390` (the 8MHz/374
rate the System 21 rips carry, even in 1.51 files that predate the field)
maps to the 12288000 default, and other sub-1MHz values are multiplied by
576. A pre-1.61 file carrying `21390` is converted with the System 21
address mapping: its register stream is System 21 even though the 1.61
type field is absent (0.9.19 read those as System 2 - Starblade track 02
resolved 0 of its 1453 triggers). Assault, Metal Hawk, Mirai Ninja and
Valkyrie carry 12288000 in-file; Starblade and Cyber Sled carry 21390.

## What gets written

- One `.fur` instrument + SMP2 sample per distinct played region. SegaPCM
  bodies are the banked ROM bytes XOR `0x80`. C140 linear bodies are the raw
  signed bytes. C140 mulaw (mode bit 3) is stored as 16-bit linear
  (`depth` 16) from the full table, so Furnace does not decode it again.
  Loop points are in frames.
- Pitch per trigger as note + E5 fine tune (ratios in semitones); the volume
  column carries the chip's own register value (7-bit for SegaPCM, 8-bit for
  C140); pan comes from the L/R registers via `08xy`.
- A C140 voice key-off (mode bit 7 clear) emits a note-off on that channel, so
  a looping sample stops when the chip stops it instead of ringing until the
  next note on the voice.
- SegaPCM flags: `clockSel`/`customClock` (4 MHz X board, 32215900/8 Y board)
  and `memSize` from the dumped ROM size; C140 flags: `bankType` from the VGM
  type byte plus `customClock`.

## Where the bytes come from

SegaPCM ROM address is `((ctrl & mask) << shift) | reg16`. `shift` is header
byte `0x3C`, `mask` is header byte `0x3E` (0 means `0x70`), then masked with
`0x1fffff >> shift`. Out Run is shift 12; Galaxy Force II is shift 13. The
bank bits live in the same control write as key-on. 0.9.16 ignored them and
every Out Run body was the silence byte `0x80`.

C140 ROM address is libvgm `find_sample` (one byte per address, not a 16-bit
word). System 2: `((adrs & 0x200000) >> 2) | (adrs & 0x7ffff)` with
`adrs = (bank << 16) | start`. System 21: `((adrs & 0x300000) >> 1) + (adrs & 0x7ffff)`.
Assault bank 21 + start `0x8A82` is byte `0x58A82`. 0.9.16 read little-endian
word pairs and matched only the low 16 bits of a ROM slice, so the bodies were
scrambled even when the note sequence was right. A trigger whose address is
outside every dumped slice is skipped:
`N C140 triggers point at ROM that the VGM does not dump`.

## SegaPCM retrigger (0.9.19)

0x84 and 0x85 are the live cursor. libvgm writes them back every tick; MAME
`voice_addr_w` replaces those bits of the internal address and keeps the
fractional byte. Once the cursor has moved, a CPU write of the original
start seeks back to it, and that seek is a new note on the same sample.
Galaxy Force II rewrites the start every frame (~756 samples) with the
channel left enabled. 0.9.18 stored the first note and dropped the rest
(Beyond the Galaxy: 16384 enables, 1192 notes).

One Z80 dump writes the address, then the end page and delta, then control,
across at most 4 VGM samples. Those writes are one note, snapshotted after
the dump, so the end page and delta are the values that play. A later dump
is a new note when it changes the cursor byte, enables a stopped voice
(including a one-shot the chip itself has already stopped), or changes the
bank. A volume or delta rewrite that leaves the cursor on the same byte
stays on the current note.

The row is then tightened, inside the 256-order budget, until every restart
the budget can hold has its own row. Two writes closer than that share a
cell; the later write is the one the channel keeps playing. The tick stays
near 90 samples and the reported BPM is kept by scaling the row subdivision.

YM2151-muted vgm2wav of Galaxy Force II Scene Select, envelope correlation:
0.9997 when every address write seeks, 0.8524 when a same-address rewrite
leaves the cursor where it is.

0.9.19 hit counts on the logs (0.9.18 kept only the enables whose written
start or bank changed): Scene Select 1778 hits, every one on its own row.
Beyond the Galaxy 16292 hits (was 1192) at 486 samples/row; 51 pairs sit
closer than that budget and share a cell. Out Run Splash Wave 4710 hits,
one per enable.

## Verified

Register-level diff of Furnace's VGM export against the source (0.9.16, sequence
only (the sample bodies in that build were wrong):

- key-on count and timing: exact (Assault 2945/2945)
- frequency registers: within 0.1% (rounding)
- volume/pan registers: exact
- SegaPCM delta registers: exact

0.9.17 checks the bodies on real logs. Out Run (shift 12, mask default
`0x70`) yields 12 non-silent samples starting at `0x40090`, lag-1
autocorrelation median 0.75. Galaxy Force II (shift 13, mask `0xF8`) is the
same, median about 0.96. Assault bank 21 + start `0x8A82` is the raw bytes
at `0x58A82` (24575 bytes, one per address). Starblade mulaw lands as
16-bit linear with a varied table, not a flat line.

## Mid-note writes (0.9.22-0.9.23)

Drivers rewrite the volume and frequency registers while a voice is keyed; the
register mirror now keeps both step streams instead of only the key-on values:

- volume (0x00/0x01): a fade steps both registers; each pair change lands in
  the volume column (level = loud side) on rows without a note, with the L/R
  balance riding along as 08xx when the pair's ratio changes. Furnace feeds the
  column into the voice gain live (`chVol = outVol*pan/255`), so fades survive.
- fade-in keys (0.9.23): a hit whose volume registers are still zero at key-on
  (typical for the first trigger of a voice, and for every fade-in) used to
  write 08 00 from the zero balance - "both off", which no later volume step
  can undo, so the note stayed silent forever. Such hits now start silent on
  the default balance and the ramp steps carry the balance; hits whose registers
  stay zero (drivers that mute by zeroing both) keep the 08 00. Measured on the
  reconverted packs: Starblade 94 hits recovered, Cyber Sled 45, Valkyrie 16,
  Mirai Ninja 5 (its "Meeting Shiranui" intro pad is this case).
- frequency (0x02/0x03): MSB/LSB pairs within 16 samples merge to the final
  value (the intermediate is a write-order transient); a write within 120
  samples of key-on is the note's own pitch (same refit as the FM sweeps), the
  rest become 1/64-semitone deltas and go through the FM sweep encoder as
  01xx/02xx ramps with E5 pins.
- start/end/loop/bank changes mid-note are still not converted (the sample
  body is fixed at key-on).

## Played-frequency references (0.9.24)

Drivers often key a voice on before writing its frequency, so the key-on
snapshot holds zeros or the previous note's leftover. The sample's reference
rate (what its C-4 plays at) was the most common *raw* key-on value, and a
sample mostly keyed that way got 0: every hit of the sample then came out as
the `rate_to_note(0)` fallback (note 108, e5 80) with a 1000 Hz sample rate -
a ~5 octave error that chroma checks cannot see (chroma is octave blind, and
the full-mix soundcheck is FM dominated). Valkyrie "Prologue" has a 24-hit
sample keyed with seven zero-frequency triggers.

The reference now uses each hit's played frequency: the key-on value, unless
a frequency write lands within 120 samples of key-on (the same refit the FM
sweeps use). A *later* write still wins when the key-on value was zero,
because nothing was playing yet; zeros are ignored when picking a sample's
most common frequency. SegaPCM's reference ignores zero key-ons the same way.

Measured on Prologue: isolated C140-layer chroma agreement at zero shift rose
from 0.34 to 0.87, and 260 of 262 key-ons now land on the source's frequency
registers (+-0.0 st; was 229/248, including a -54..-59 semitone family).

## PCM slide scale and FM output cuts (0.9.26)

Two measured engine facts, both from Cyber Sled "Silent Fight":

- PCM `01xx`/`02xx` slides move 1/128 st per parameter unit per tick - half
  the FM rate (a 0x77 rate descended 60 units/row where the source did 120,
  and 0xFF note-row attack offsets read as +-2 st). PCM parameters are now
  doubled with the ideal rate capped at 127.5 units/tick, the engine's 255
  maximum, so glides run at the source's speed and reach their full range.
- A mid-note write that clears RL (regs 0x20-0x27, RL == 0) cuts the channel
  instantly on hardware. Silent Fight ends two keyed siren channels that way -
  voices still on, D1L at maximum - and without it those notes rang to the end
  of the track. Such writes now emit the note-off; the patches' RR (0xF here)
  releases them in milliseconds.

Known degenerate case: a voice keyed with an invalid sample window (end below
start, like Silent Fight's 0x7C00/0xD0 one-shot) is stored as a 1-frame loop,
which Furnace plays as a static buzz; the chip plays past `end` instead, so
the body differs. Rare, and only when the driver leaves a voice's window
registers unset at key-on.

Engine facts measured while chasing Last Game's "restless" C140 voices 6/7
(a driver vibrato that swings +-1.6 st every two rows):

- Pitch jumps are capped at 127.5 units/tick (2 st). A 12 st register jump
  chases in about six ticks - 13 ms at that track's 95-sample ticks.
- F1xx/F2xx (single tick pitch) act as a one-shot *offset* of xx/2 units and
  override a slide on the same row, so they cannot beat the cap. EAxx
  (legato) does not suppress the C140 key-on - a note change still retriggers.
- One slide rate per row traces a two-rows-per-cycle vibrato to within about
  +-0.5 st with brief transients; finer rows (subdivision) would be the only
  lever, at the cost of pattern budget.

## Instant jumps via quick legato (0.9.27)

Furnace's `E8xx`/`E9xx` ("quick legato up/down") transposes the playing note
by y semitones in a single write, persists until the next note and resets at
its key-on. Measured on the bundled 0.6.8.3 build through both the C140 and
YM2151 platforms (`EAxx` toggle legato does nothing on either: C140 ignores
it, a YM2151 note change still emits key-off + key-on). Sweeps now land any
overshoot beyond what the row's slide can deliver (127.5 units/tick PCM, 255
FM) with an `E8`/`E9`, letting the slide cover the sub-semitone remainder -
register jumps, dive attacks and attack bends step instantly instead of
slurring at the rate cap. Sub-tick sweeps (a register change within one row
of the key-on) also land via a transpose on the following row instead of
being dropped.

The slide scale was re-measured on 0.9.26's doubling: a parameter unit moves
1/128 st per tick linearly to 255 on PCM (per-row travel = 3.5 units x
param), and 1/64 st on FM. No saturation below the parameter cap; earlier
"clamp" readings came from registers hitting their floor mid-measurement.

E5 scales differ per platform: one byte step is 1/64 st on FM but 1/128 st
on PCM (a static E5 0x40 lands exactly -0.5 st below 0x80 on the C140).
0.9.28 converts sweep pin offsets per platform - before that every PCM
fine-tune pin acted at half strength, which is exactly the percent-level
"99%/101%" family of pitch offsets that no rate or amp correction could
reach. E8/E9 transposes measure +1.0001 st per semitone on both platforms.

0.9.29: the sweep model accrues the movement the *engine* makes, not the
ideal rate. A glide that wants 2.66 units/tick is written as `01 03` (the
parameter is a whole number), so the engine runs ~13% fast while the model
credited 2.66; the error compounded over long climbs and left held notes on
the wrong pitch. Hyper Duel's Stage 1 climbs (+5 st each, three of them)
ended +31/+65/+111 units sharp - up to 1.7 st held for seconds, the exact
"hits wrong notes" family. With `slide` adding param-per-tick, the plateaus
now land within +-7 units (0.11 st) and the pins only clean up sub-unit
residue. (An experiment to also land discrete register jumps instantly with
E8/E9 was measured to run away on Metal Hawk's alternating chords and is
not shipped; those jumps still ramp within one row.)

Scale of what that recovers (first six tracks per pack, from the logs):

| pack | mid-note volume writes | mid-note frequency writes |
|---|---|---|
| Starblade | 346k | 69k |
| Valkyrie | 235k | 39k |
| Cyber Sled | 109k | 72k |
| Metal Hawk | 29k | 10k |
| Assault | 6k | 39k |
| Mirai Ninja | 10k | 21k |

## Limits

- SegaPCM mid-note volume/frequency writes are not converted yet (C140's are,
  since 0.9.22); each note keeps its trigger-time values.
- C140 sign-flip (mode bit6) and sign-magnitude (bit0) are ignored. libvgm,
  which is what these VGMs were logged against, does not apply them either.
- A `.fur` from before 0.9.18 has the C140 pitch bug (1.5x high, clock
  18432000 on export); reconvert the C140 packs after that version.
- A `.fur` from before 0.9.19 is missing SegaPCM same-address retriggers; the
  SegaPCM packs under `output/**/fur` were batch-reconverted with 0.9.19.
- A `.fur` from before 0.9.20 has no C140 note-offs (looping voices ring) and
  maps Starblade as System 2 (most of its C140 layer silent or scrambled);
  reconvert the C140 packs after that version.
- A `.fur` from before 0.9.21 carries flat chip output levels; the chip volumes
  now follow the player's mix - see docs/mix-levels.md. The SegaPCM, C140 and
  MSM6295 packs under `output/` were reconverted with 0.9.21.
- A `.fur` from before 0.9.22 drops C140 fades and pitch slides; before 0.9.23
  it also mutes the fade-in keys, before 0.9.24 the sample references of
  drivers that key before writing the frequency are broken, before 0.9.25
  every PCM sweep's attack inherits part of its note row's slide travel, and
  before 0.9.26 PCM glides run at half speed and FM channels cut by an RL
  write keep ringing. The C140 and SegaPCM packs were reconverted with 0.9.26.
- PCM slide *trajectories* are row quantized: a dive faster than the slide
  engine can move (127.5 units/tick for PCM after 0.9.26, 255 for FM, one tick
  per row at speed 1) lands short until its rows catch up. PCM ramps start on
  the row after their note (0.9.25) so a row's 01xx/02xx travel cannot leak
  into the key-on write. FM sweeps keep their measured behavior. Key-on
  comparisons verify registers; per-window layer chroma and per-write
  trajectory scans cover the rest.
- Tracks longer than 256 orders are truncated by the existing pattern limit.
  Restarts closer than that budget share a row; the later write is kept.

## Auditing a conversion

    python fur_soundcheck.py "<source.vgm|vgz>" "output/<pack>/fur/<track>.fur"

Renders the source, the `.fur` (through Furnace) and an exported VGM; prints
the pitch-shift table for both comparisons plus the VGM `Dev` lines, so chip
clocks can be compared. Healthy: best shift 0 in both pairs and equal clocks;
the renders are kept under `<fur folder>/soundcheck/`.

- `pitch_shift.py A.wav B.wav`: the measurement alone. Octave-blind by
  design: the bug read as `-5` (= `+7.02` mod 12). Read the whole table.
- `fur2wav.py <fur>`: render any `.fur` through Furnace to WAV.
- `fur2vgm.py <fur>` + `vgm2wav-mute`: portable export + render.
- `vgm_scan.py`: list every SegaPCM/C140/C219 pack in the tree.

Legacy-file caveat: Starblade and Cyber Sled carry the old `21390` clock
(resolved to 12288000 like the players do). Starblade's "sparse C140 dump"
of 0.9.18/0.9.19 was the System 2/21 mapping bug: with the type resolved,
all six tracks place every C140 trigger (track 02 went from 0 samples to
6 samples / 1453 triggers).

### Measured (0.9.18-0.9.20, on the converted packs)

`fur_soundcheck.py`, best shift / median; clocks are source -> export:

| track | source vs .fur | source vs export | C140 clock |
|---|---|---|---|
| Assault 04 BGM 1 | 0 / 0.988 | 0 / 0.989 | 12288000 -> 12287808 |
| Starblade 02 Engage in Single Combat (0.9.20) | 0 / 0.996 | 0 / 0.995 | 12288000 -> 12287808 |
| Starblade 03 The Theme | 0 / 0.991 | 0 / 0.990 | 12288000 -> 12287808 |
| Out Run 03 Splash Wave (SegaPCM) | 0 / 0.964 | 0 / 0.964 | 4000000 -> 4000000 |
| Metal Hawk 04 Round Clear (0.9.22) | 0 / 0.992 | 0 / 0.993 | 12288000 -> 12287808 |

Per-chip dynamics after 0.9.23 (isolated C140 layer: source render minus a
C140-muted render vs .fur render minus a volume-zeroed render), envelope
correlation over the whole track: Metal Hawk 03 BGM 1 0.88, Starblade 04 Blue
Flight 0.88, Valkyrie 06 Zulu 0.78, Mirai Ninja 02 Meeting Shiranui 0.76,
Cyber Sled 02 Enter for Battle 0.72 (these include every slide ramp and fade
step, quantized to rows).

The 192 Hz clock difference is Furnace deriving the export clock from an
integer tick rate (0.03 cents). Before the fix, Assault measured -5 (=
+7.02 mod 12) / 0.897 and exported 18432000; after, 0 / 0.988. The earlier
"Assault mix drift" (chroma 0.695 in 0.9.17) was this same pitch bug.

Starblade's earlier "FM-dominated" reading was the same mapping bug. The
dump resolves every C140 trigger now (track 01: 14 samples / 1679 triggers,
track 02: 6 / 1453, track 03: 22 / 1466, no undumped warnings); track 03
alone gained 1333 playable triggers over its 5-sample 0.9.18 build. Assault's
C140 layer is complete (13 samples, 2945 triggers).
