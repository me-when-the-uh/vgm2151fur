# Konami YM2151 / YM3812 + K007232

Command names and the folder each one writes are in the README. This page is the three boards. Seconds and track names sit in each `games/<id>.json`.

| | Super Contra | Gradius II GOFER no Yabou | S.P.Y.: Special Project Y |
|---|---|---|---|
| MAME set | `scontra` (clone `scontraj`) | `gradius2` (parent `vulcan`) | `spy` (clone `spyu`) |
| Driver | `konami/thunderx.cpp` | `konami/twin16.cpp` | `konami/spy.cpp` |
| Chips | YM2151 + K007232 (mono cabinet) | YM2151 + K007232 + uPD7759 (stereo) | YM3812 + 2× K007232 (mono cabinet) |
| Latch | `maincpu` `0x1F84` then IRQ at `0x1F88` | `maincpu` `0xA0009` then bit 3 of `0xA0000` | `maincpu` `0x3FB0` then IRQ at `0x3FC0` |
| Isolate | NOP `STA $1F84` / `$1F88`, tap the latch, kick watchdog `0x1F8C` | Tap the latch. No ROM NOP | NOP `STA` extended to the latch and IRQ |
| Sound test | no | no | yes (service item 5) |

`vgm_mix.chips` is what `mix` writes into the extra header. The old keys (`ym2151_relative`, `k007232_relative`, `center_k007232`) still apply when `chips` is absent. Super Contra and S.P.Y. center the K007232. Gradius II keeps the logged hard pan.

Working listening mix for Super Contra is still `--ym2151 1.6 --k007232 0.25`. S.P.Y. is `--ym3812 1.0 --k007232 0.20` on both PCM chips.

The G2-with-G3-samples remix tooling stayed with the Gradius packs and is not part of this project.
