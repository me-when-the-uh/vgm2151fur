# PSG (AY-8910) accuracy: open items

The AY layer is the weakest part of an MSX conversion. This file records what
was measured so the next pass starts from facts, not guesses. Numbers are from
`Space_Manbow_(MSX2)/09 Dust-Dust-Dust (6st.BGM)`, comparing the source's
render with a Furnace `-vgmout` export of the module, sampling every 735
samples: exact register match per voice for the 12-bit tone period, the volume
register (level + envelope bit) and the mixer's audio bits.

| state | period exact | volume exact | mixer exact |
| --- | --- | --- | --- |
| 2026-10 (macros, silence gate, one cell per gate) | 58.2 % | 98.6 % | 98.9 % |
| ... with the period-gate experiment | 58.2 % | 91.0 % | 98.9 % |

## What the AY layer still gets wrong

1. **A drum's pitch glide is the pitch macro.** The source writes the tone
   period in steps inside one gate (1024 -> 1152 -> 1280, and the tom
   1104 -> 1024 -> 1536 -> 1792 -> 2048). The steps are about one frame
   apart. The old 735-sample merge collapsed a whole frame-to-frame chain
   into its last period, and a hit whose tone bit was off at the end never
   reached the segmenter, so the export held the first period. The AY
   timeline now merges a 16-sample byte pair. A step held for two frames is
   a cell. A shorter step stays in the pitch macro. The 58.2 % figure is the
   measurement from before that change.

2. **The period is also a gate.** Drivers mute a channel by writing a period
   the chip cannot sound (period 1) and bring it back by writing a real one,
   without touching the volume. `_audible` treats the inaudible period as
   silence, and a period write re-checks the gate, so the un-mute opens a
   note. An earlier experiment that opened on the period write also let the
   note's volume replace an envelope-armed register before the envelope
   effect landed (volume match 98.6 % -> 91.0 %). The gate cell now carries
   the envelope mode, period and shape with the note.

3. **Per-tick volume steps cost fidelity at any row rate.** The optimizer's
   own loss counters on this track: 116 dropped volume steps at 1x, 616 at 4x
   (and 1135 "ed_rolled" delays at 1x). Those steps are the AY's drum decays.
   Moving them into the instrument's **volume macro** (the AY platform reads
   `std.vol`, which multiplies the channel volume) is what would make both the
   optimizer and a musical row rate safe; then the condense factors stop
   losing decays.

## Measurement recipe

```
python scratch/ay_compare.py <source.vgz> <before.fur> <after.fur>
```

exports both modules with Furnace and prints the table above; the same script
plus `--mute AY8910#0.Disabled=True` renders gives the audio-side check
(`scratch/layer_check.py`).
