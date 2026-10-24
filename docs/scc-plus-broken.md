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

## The real cause: these rips stream wave RAM, they do not gate notes

Both Yie Ar sources are VGM v1.61 and classify like this (counted with the
correct 4-byte `0xD2 pp aa dd` walk):

| source | wave RAM writes | freq | vol | enable |
| --- | --- | --- | --- | --- |
| Yie Ar Kung-Fu 1, `01 Main BGM` | 6168 | 112 | 35 | 7 |
| Yie Ar Kung-Fu II, `03 Battle BGM 1` | 6651 | 48 | 15 | 3 |
| Space Manbow 09 (a healthy conversion) | 15071 | - | - | - |

96-97 % of the writes are the 32-byte waveform itself, and the frequency
registers change roughly a hundred times in a whole track. The music in these
rips is **not** carried by enable/volume gates: the driver keeps a voice
enabled and *streams* its waveform, setting the pitch when the tune's note
changes. The `pp` byte takes values 0-4 in both packs, which fits a bank index
rather than the chip instance a single SCC would use.

My collector's model - a note starts at an enable or volume gate and the wave
image is whatever is in RAM at that instant - finds only the handful of gates
(7 and 3), so the module writes 1252 registers where the source writes 6748.
The module is not mis-converting notes; it is built on the wrong note model
for this driver.

## The fix direction

Gate these voices on the **frequency** register instead: when the period
changes and the voice is enabled and loud, that is a note, and the waveform
playing at that moment is its instrument. The per-tick wave RAM burst becomes
the note's **wave macro** (the same machinery Space Manbow's morph already
uses), so a streamed waveform reproduces as a wavetable sequence rather than
being sampled once at a gate.

That turns these two packs into exactly the target practice the user wants:
~100 notes per track, one wavetable per note, and no reliance on the gate
model that is failing here.
