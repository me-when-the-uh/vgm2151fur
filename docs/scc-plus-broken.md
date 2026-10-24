# SCC+ conversion is dropping most of the track

`VGM_collection/unsorted/Yie_Ar_Kung-Fu_II_-_Yie-Gah_Koutei_no_Gyakushuu_(MSX,_SCC+)`
converts to modules that are almost silent. Measured on
`03 Battle BGM 1`, counting register writes in the source and in a Furnace
`-vgmout` export of the module:

| file | SCC writes (0xD2) | AY writes |
| --- | --- | --- |
| source `03 Battle BGM 1.vgz` | 6748 | 0 |
| module `03 Battle BGM 1.fur` | 1252 | 0 |

For comparison, the same measurement on a correct conversion
(`Space_Manbow_(MSX2)/09 Dust-Dust-Dust`): source 15071 SCC + 6412 AY writes,
module 21134 + 5826. A healthy module writes *at least* as much as its source,
because pitch and volume macros add per-tick writes. This pack writes **18 %**
of its source, i.e. the SCC layer is mostly missing, hence "completely broken".

Both source and module have **zero AY writes**: this pack is SCC-only, so
nothing in the AY path is involved in the breakage.

## Root cause (measured)

The packs are logged with a **different bank convention** than Space Manbow, and
the collector assumed Space Manbow's.

Space Manbow (`plus False`) writes: bank 0 = 1952 wave, bank 1 = 2902 freq,
bank 2 = 10081 vol, bank 3 = 136 enable. The collector's bank-to-register map
(0 wave, 1 freq, 2 vol, 3 enable, 4 the SCC+ fifth-voice page) matches that
exactly, which is why it converts well.

The Yie Ar sources (`plus True`) write: **bank 0 = nothing at all**, bank 1 =
1913 (addresses 0x00-0x09 = five voices x two frequency bytes), bank 2 = 3986
(addresses 0x00-0x04 = five volumes), bank 3 = 112 (enable), bank 4 = 640 wave
+ 112 freq + 35 vol + 7 enable. So these rips put the waveform and the
registers in the *same* page addressed by the register's own address, i.e. the
standard K051649 map, and use the bank byte for the 8 KB page of the cartridge
window rather than for the register class.

Consequence with the bank map: every bank-1/2 write is read as *frequency* and
*volume* for a voice, **no bank-0 wave is ever written**, so all voices 0-3
have an all-zero waveform. `analyze.py` drops notes whose wave is all zero
(`SCC_MIN_PERIOD` / `not any(h.wave)`), so the module keeps only the fifth
voice's notes: 1295 SCC writes against the source's 6393, i.e. silence.

## Fixed

Both Yie Ar packs now convert with all five voices. What landed in `scc.py`:

1. **Bank 4 is the real wave RAM, addressed as-is.** The rips load the
   waveforms through bank 4 and never touch bank 0. On the K052539, voice 4's
   own 32 bytes are at 0x80-0x9F (a full image, written a handful of times per
   track). Addresses at 0xA0 and above repeat the register page, so they are
   skipped; frequency, volume and enable come from banks 1-3.
2. **Voices 3 and 4 share 0x60-0x7F only on the K051649.** SCC+ does not share
   that page: 0x60 is voice 3 and 0x80 is voice 4. Sharing it on these rips
   played voice 3's wave on voice 4. On Stage Start and Game Over the two
   images happen to match; on the battle tracks they do not.

Measured after the change on `01 Main BGM` (source 1795 samples vs module 1796,
sampled every 735 samples):

| quantity | before | after |
| --- | --- | --- |
| SCC writes | 1295 | 4782 (source 6393) |
| notes kept | 28 | 34 (collector finds 34) |
| gate match | 80.2 % | 99.9 % |
| volume match | 60.7 % | 75.6 % |
| frequency match | 59.6 % | 76.3 % |
| mean volume per voice | [4.68, 5.04, 5.72, 5.72, **14.99**] | [4.68, 5.04, 5.72, 5.72, **5.44**] |
| (source mean) | [4.67, 5.03, 5.7, 5.7, 5.43] | same |

`Yie Ar Kung-Fu II / 03 Battle BGM 1`: 1215 -> **6797** SCC writes against the
source's 6748. Space Manbow 09 is unchanged (244 notes, 10 waveforms, 21134
writes), and the full test suite passes.

## What the hand-made demo is actually doing

`furnace/demos/msx/Nemesis2AirBattle.fur` runs at speed 4 / hz 60, which
Furnace shows as 225 BPM, and keeps SCC volume in a volume macro (file code
0). A first cut of the converter wrote that macro as code 1, so the decay
played as an arpeggio: one pattern cell cycling through several notes. Code
0 is the volume macro. With the column at 15, `outVol = vol * macro / 15`
on the SCC and `macro + column - 15` on the AY both reproduce the register.

The other half of that symptom was the pitch. These drivers change notes by
rewriting the period and leaving the gate up, and the whole phrase was stored
as one note's pitch macro. Yie Ar Kung-Fu II / 03 Battle BGM 1 was 71 pattern
notes, median gate 0.6 s, one macro spanning 26 semitones, on the 900 BPM
frame grid. It is now 557 notes on the measured lattice (Furnace BPM 449; the
16th is two frames, and the written row is one frame). A held semitone
step is a new note; vibrato and slides stay macros; a constant detune is the
note's `E5`.

900 BPM was not a tempo. Furnace's BPM is 15 times the row rate at highlight
4, so one row per frame always reads as 900. SCC/AY rows start as the
measured 16th, one tick per frame. On the SCC a held semitone step is its own
cell. On the AY that hold is two frames, and a one-frame attack sweep stays
in the pitch macro.
When two cells would share that 16th, the row loses ticks and the highlight
moves with the ratio, so the quarter-note BPM stays put. The tick stays one
driver frame. Macros are built after condensing, at that rendition's tick
rate.
Condensing past the lattice stacks notes (`note_collision`) instead of
speeding the macros up. The AY's noise, mode and envelope period change
inside a note the same way, as duty / wave / ex2 / ex5 macros, with 20xx–24xx
only for the state at the note start.



