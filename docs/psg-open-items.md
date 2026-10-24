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

1. **A drum's pitch glide is flattened.** The source writes the tone period in
   steps inside a note (1024 -> 1152 -> 1280 -> back), which is what makes an
   AY drum sound like a drum rather than a tone. The export holds 1024. The
   collector records those writes (`freq_pts`), so the loss is in the macro
   path: either the steps land inside a note the collector never opened, or
   the macro's values are not reaching the platform. Reading the register
   trail of one such note next to its `ToneIns` is the way to settle it.

2. **The period is also a gate.** Drivers mute a channel by writing a period
   the chip cannot sound (period 1) and bring it back by writing a real one,
   without touching the volume. `_audible` already treats the inaudible period
   as silence, but notes only open on volume/mixer writes, so the note that
   starts on the "un-mute" period write is never opened. Re-checking the gate
   on period writes was tried and made the volume timeline *worse* (98.6 % ->
   91.0 %), because the opened note writes its own volume over the source's
   envelope-armed register before the envelope effect lands. Doing it right
   means opening the note and emitting the envelope mode/period/shape effects
   in the same cell, then re-measuring.

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
