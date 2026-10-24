# Konami SCC / SCC+ and AY-3-8910 support

The MSX packs in this collection are SCC music plus PSG percussion, so both
chips convert together. The register models are libvgm `k051649.c` (which
both the reference player and kss2vgm use) and the AY register set shared by
MAME and Furnace.

## Commands

- `0xD2 pp aa dd` — SCC, `pp` = register bank, `aa` = register:

  | pp | bank | `aa` range |
  | --- | --- | --- |
  | 0 | waveform RAM | 0x00–0x7F: voices 0–3; on the K051649 0x60–0x7F is shared by voices 3 and 4 |
  | 1 | frequency | 0x00–0x09, two bytes per voice (low, then high nibble) |
  | 2 | volume | 0x00–0x04, 4-bit |
  | 3 | voice enable | 0x00, one bit per voice |
  | 4 | waveform RAM | 0x80–0x9F: the K052539's independent voice 4 wave |
  | 5 | test register | 0x00–0x0F; bit 6 freezes wave RAM, bits 0/1 change the frequency-word width |

  Bit 7 of `pp` selects a second chip instance. The header's K051649 clock
  (0x9C) has bit 31 set for the K052539; that selects Furnace system `0xB4`
  (SCC+) instead of `0xA1` (SCC). A K052539 rip writes voice 4's wave at
  bank 4, a K051649 rip inside bank 0 at 0x60–0x7F, which reaches both voices
  3 and 4.
- `0xA0 rr dd` — AY-8910 register write. `rr` 0x00–0x0F, bit 7 selects the
  second instance. (The old `0xA0 → MultiPCM` note in `vgm.py` was wrong;
  MultiPCM is 0xB5.)

Header clocks: AY at 0x74 with type byte 0x78 and flags 0x79 (VGM 1.51+),
SCC at 0x9C (1.61+). The extra-header ids 0x12 (AY) and 0x19 (SCC) supply a
second instance's clock.

## Pitch

The SCC plays `clock / (16 * (period + 1))` and halts a voice at period ≤ 8.
The AY plays `clock / (16 * period)` (period 0 reads as 1). Both header
clocks are the value the reference player runs the chip at, and Furnace's
SCC/AY platforms build their pitch table from the same number and divider
(16 and 8), so a source period round-trips through the module.

`pitch.py` ports Furnace's period-mode `DivPitchTable` (including the
octave-shift rounding and the E5 fine-pitch interpolation) and inverts it.
The inverse picks the note/E5 pair whose register is closest to the source;
where the table's octave shift loses resolution the result is one register
step away (0.04 % at the bottom of the range, well under a cent), and
pitches above the note range's ceiling saturate.

Furnace's own clock defaults to COLOR_NTSC/2 = 1789772.727…; a rip whose
header clock is within a hertz of it is written without `customClock`, and
the analyzer computes the same clock, so the table matches the module.

Mid-note pitch changes do not become a note per step (that was faithful but
needlessly dense) and do not use `01xx`/`02xx` slides either (their rates are
FM/PCM-calibrated and drift on a 12-bit period). They become the instrument's
**pitch macro**: one value per tick, in the engine's pitch unit (1/128
semitone), which the platform adds straight to the period. One instrument per
distinct trajectory, so a driver's recurring bend or vibrato is written once
and any note can play it.

A 12-bit period is two register writes and drivers can be a frame apart about
the pair, so the intermediate combination is treated as a write-order
transient: writes inside a row are merged (last one wins, which is also what
makes a step *back* to the note's own pitch survive), and the first frame
after the gate belongs to the note's own pitch.

A macro holds at most 256 values. A series that fits is stored whole; a
repeating one is stored as one cycle that loops; a longer non-repeating one is
thinned by whole ticks (values, then a speed that says how many ticks each
value lasts). A non-looping macro drops its trailing run of one value, since
the engine holds the last value anyway - which is what lets two notes whose
bend differs only in how long it lasts share an instrument.

## Waveform motion

Konami's SCC drivers morph a held note by rewriting its 32 wave-RAM bytes
while it sounds (Space Manbow's Brilliance does it 52 times over one eight
second D-1 note, sweeping a sawtooth's slope). That is the note's *timbre*
moving, and it cannot be expressed as notes: it becomes the instrument's
**wave macro**, a per-tick list of wavetable indices, with one `WAVE` block
added to the module for every image the morph visits (deduplicated by
content). A whole-image rewrite is a burst of 32 byte writes; the collector
keeps only the state each burst settles on.

The result is that such a note is one pattern cell playing an instrument whose
wave macro walks the morph - no per-step cells, and the motion is back.

## Gates

The SCC has no key-on register, only the voice enable bits, and drivers use
either those, the 4-bit volume, or both as the gate. A note starts when the
enable bit is set and the volume is non-zero, and ends when either goes off.
Gates shorter than 100 samples are driver housekeeping (init sequences toggle
the enable mask for a few samples) and are dropped. A note whose period is
below 9, or whose wave RAM was never written, emits no note; its key-off is
still placed so the previous note stops.

The AY gate is the volume register (0 = off) or the mixer (both tone and
noise disabled = off). An envelope-enable volume write (bit 4) re-arms the
generator, which is how MSX drivers retrigger drums; that becomes a new note
even when the voice never went quiet. A write that clears the envelope bit
also emits a mode effect, or the register would keep using the generator.

A "mute" is not a note: drivers silence a channel by writing a tone period
below what the chip's music range can express (beyond B-9, so above roughly
16 kHz - a few register units) while leaving the volume up. Those writes read
as gate-offs, not as notes, which is what keeps the sequence free of
high-pitched blips that the source never played. `ay_min_period` computes the
cut-off from the pitch table, so it follows the rip's clock.

## Waves and instruments

Wave RAM images are deduplicated per file, so several voices, notes or morph
steps sharing one image share one wavetable entry. Each image becomes a
`WAVE` block and the instruments that use it are `INS2` type 18
(`DIV_INS_SCC`) whose `WS` feature points at the note's starting wave; the
writer converts each chip byte to the wavetable's unsigned center (the chip
reads signed, Furnace subtracts 128 when it loads the wave). A K051649 rip's
shared voice-3/4 wave is handled by the chip itself (the SCC platform maps
voice 4's wave writes to slot 3), so nothing special is needed there.

The AY uses instruments of type 6 (`DIV_INS_AY`) with a phase-reset macro
(file code 14). The chip restarts the envelope only when the shape register
is written, and Furnace's platform skips a write whose value did not change;
without the macro a drum hit reusing the previous shape kept the stale
envelope state, which cost about 20 % of the AY layer's energy and the
character of every drum attack.

Channel mode (tone/noise/envelope), noise frequency and the hardware envelope
(period and shape) are pattern effects, one per change:

| effect | meaning |
| --- | --- |
| `20xy` | channel mode; `x` = mode bits − 1 (bit 0 tone, bit 1 noise, bit 2 envelope) |
| `21xx` | noise frequency, value = 31 − register 6 |
| `22xy` | envelope shape `x`, enable this channel when `y` is non-zero |
| `23xx` / `24xx` | envelope period low / high |

A pure-noise AY voice has no audible pitch, so its period writes are not
re-articulated (they would only bloat the module); a tone or tone+noise voice
is.

## Volumes

Furnace system volumes come from the reference player's mix (both chips
default to unity) with `_FURNACE_VOLUME_SCALE` correcting the Furnace cores:
AY 1.46, SCC 1.04, measured on two-chip modules. A lone chip in a VGM is
scaled by libvgm's overall-volume normalizer, which Furnace does not do, so
single-chip test files are not a valid level reference.

## Row rate

These rips drive everything off a ~60 Hz tick, and their notes carry no
tie to a coarser musical unit the way an FM key-on lattice does, so the row
grid matters more here than for the arcade packs:

- When the estimator can measure a lattice from the notes, the row is that
  subdivision and the tempo is whatever it implies (100–225 BPM on the Space
  Manbow, Snatcher, Contra and SD Snatcher tracks).
- When it cannot (a sparse boss theme whose only regular pulse is the
  percussion tick), the fallback is one row per tick: the most faithful
  rendering, with a tracker tempo far above anything a human would write.
  `--speed N` sets the row explicitly (8 gives a 16th-note row and a ~112 BPM
  tempo for these rips).

The fallback is deliberately the faithful one: the AY's volume steps are a
pattern column, so a coarser row quantizes drum decays. Measured on
Space Manbow's "Hydra", one row per tick scores 0.742 against the source's
render and a 7-tick row 0.688 - the decay loss is real. Moving the per-tick
volume steps into a volume macro (the AY platform reads `std.vol`) is what
would make a musical row safe, and is the next step if that trade-off needs
to flip.

## Validation

The MSX2 packs were checked three ways per track: the register stream of a
Furnace `-vgmout` export against the source, the note registers at every gate
(`tools/fur_soundcheck.py`), and the rendered audio (chroma pitch alignment,
median over the track's windows). The note counts are the source's own gates:
one cell per gate, plus one instrument per distinct pitch trajectory or morph.

| track | SCC notes exact | AY notes exact | notes | chroma, .fur | chroma, export |
| --- | --- | --- | --- | --- | --- |
| Snatcher 09 Theme of Snatcher (P1) | 99.3 % | 99.7 % | 718 | 0 st (0.958) | 0 st (0.953) |
| Contra 02 Jungle Battle | 93.8 % | 99.4 % | 527 | 0 st (0.926) | 0 st (0.922) |
| SD Snatcher 01 Strange Overture | 100 % | 99.8 % | 619 | 0 st (0.939) | 0 st (0.922) |
| Snatcher 36 Goodbye Harry | 100 % | 100 % | 66 | 0 st (0.869) | 0 st (0.856) |
| Space Manbow 12 Brilliance | 94.0 % | 98.2 % | 286 | 0 st (0.778) | 0 st (0.783) |

The remaining SCC mismatches are the ±1 table-resolution cases. The AY's
Brilliance figure includes the inaudible-period cases the table cannot
express (they come out as silence, which is what they are).

A macro-based transcription has to be checked in the exported registers, not
just in the cells: `tools/fur_soundcheck.py` reads the period registers at
each gate, and a controlled bend (bend away, return) reproduces the source's
register trail exactly, including the intermediate values of a slow
low/high-byte pair.
