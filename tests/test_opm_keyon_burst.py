#!/usr/bin/env python3
"""Key-on burst semantics on the OPM: an instrument loaded after its key-on.

A driver that parks every carrier at TL 7F, keys on, and writes the voice's
real levels a few samples later describes an instrument that cannot sound. The
snapshot is taken at the key-on, and the level write that follows reaches the
writer only as a mid-note fade -- the volume column can attenuate but never
raise the voice back. The levels arrive inside the same burst as the key-on, so
they belong to that note's instrument. This is what BQMUS03 does on every
channel at the start of the song and again when it changes voices mid-track.

The companion bug -- a slide on a row that carries an ED delay, which travels
fewer ticks than the row is long -- can only be measured against the player, so
it is guarded by `TestSweepModel.test_bend_on_a_delayed_key_on_lands_on_pitch`
in `test_furnace_roundtrip.py`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.analyze import analyze  # noqa: E402
from vgm2151fur.vgm import ChipWrite  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _synth import lattice, vgm  # noqa: E402

CH = 2
KEY_ON = 60000
KEY_OFF = 150000

# The burst as the driver writes it: voice, then pitch, then key-on, and only
# then the levels that make the carriers sound.
MUTED_VOICE = [
    (0x20 | CH, 0xC3),                       # RL both, fb 0, alg 3
    (0x40 | CH, 0x5B), (0x48 | CH, 0x10),    # op DT1/MULT
    (0x50 | CH, 0x32), (0x58 | CH, 0x70),
    (0x60 | CH, 0x7F), (0x68 | CH, 0x7F),    # every carrier parked
    (0x70 | CH, 0x7F), (0x78 | CH, 0x7F),
    (0x80 | CH, 0x1F), (0x88 | CH, 0x1F),    # KS / AR
    (0x90 | CH, 0x1F), (0x98 | CH, 0x1F),
    (0xA0 | CH, 0x0F), (0xA8 | CH, 0x16),    # AM / D1R
    (0xB0 | CH, 0x0C), (0xB8 | CH, 0x06),
    (0xC0 | CH, 0x42), (0xC8 | CH, 0x00),    # DT2 / D2R
    (0xD0 | CH, 0x00), (0xD8 | CH, 0x00),
    (0xE0 | CH, 0x61), (0xE8 | CH, 0x63),    # D1L / RR
    (0xF0 | CH, 0x64), (0xF8 | CH, 0x97),
]
LOADED_TL = {0x60 | CH: 0x2A, 0x68 | CH: 0x0C,
             0x70 | CH: 0x24, 0x78 | CH: 0x00}


def _burst_song(load_gap=2, *, pitch_writes=None, key_on=KEY_ON):
    """One note whose carrier levels arrive after its own key-on."""
    writes = list(lattice(60))
    for reg, val in MUTED_VOICE:
        writes.append(ChipWrite(key_on - 40, "ym2151", 0, reg, val))
    writes.append(ChipWrite(key_on - 20, "ym2151", 0, 0x28 | CH, 0x40))
    writes.append(ChipWrite(key_on - 20, "ym2151", 0, 0x30 | CH, 0x14))
    writes.append(ChipWrite(key_on, "ym2151", 0, 0x08, 0x78 | CH))
    # The same command's tail: the levels that make it audible.
    for i, (reg, val) in enumerate(sorted(LOADED_TL.items())):
        writes.append(ChipWrite(key_on + load_gap + i, "ym2151", 0, reg, val))
    for entry in pitch_writes or []:
        writes.append(ChipWrite(entry[0], "ym2151", 0, entry[1], entry[2]))
    writes.append(ChipWrite(KEY_OFF, "ym2151", 0, 0x08, CH))
    writes.sort(key=lambda w: w.sample)
    song = analyze(vgm(writes), pcm=False)
    return song, next(e for e in song.events if e.on and e.ch == CH)


class TestLateInstrumentLoad(unittest.TestCase):
    """A key-on whose carriers are parked picks up the burst's real levels."""

    def test_carrier_levels_written_after_the_key_on_are_the_instrument(self):
        song, ev = _burst_song()
        patch = song.fm_patches[ev.ins]
        self.assertLess(patch.carrier_tl(), 0x7F,
                        "the note was given an instrument that cannot sound")
        self.assertEqual([patch.ops[i].tl & 0x7F for i in range(4)],
                         [LOADED_TL[0x60 | CH], LOADED_TL[0x68 | CH],
                          LOADED_TL[0x70 | CH], LOADED_TL[0x78 | CH]])

    def test_the_loaded_levels_are_not_also_a_mid_note_fade(self):
        # Folding the burst into the instrument must not leave a volume step on
        # the note: the fade column cannot rise, and a redundant step would make
        # the same attack read twice.
        _song, ev = _burst_song()
        self.assertEqual(ev.tl_pts, ())

    def test_a_wider_gap_is_a_real_change_and_stays_out_of_the_snapshot(self):
        # Past the burst the driver means a mid-note change: the instrument keeps
        # the level it had at the key-on.
        song, ev = _burst_song(load_gap=60_000)
        patch = song.fm_patches[ev.ins]
        self.assertEqual(patch.carrier_tl(), 0x7F)

    def test_a_note_that_is_already_audible_keeps_its_own_snapshot(self):
        # The common case must not absorb a later voice's setup: the levels at
        # the key-on stay the instrument, whatever is written while it sounds.
        writes = list(lattice(60))
        for reg, val in MUTED_VOICE:
            writes.append(ChipWrite(KEY_ON - 40, "ym2151", 0, reg, val))
        for reg, val in LOADED_TL.items():
            writes.append(ChipWrite(KEY_ON - 30, "ym2151", 0, reg, val))
        writes.append(ChipWrite(KEY_ON - 20, "ym2151", 0, 0x28 | CH, 0x40))
        writes.append(ChipWrite(KEY_ON - 20, "ym2151", 0, 0x30 | CH, 0x14))
        writes.append(ChipWrite(KEY_ON, "ym2151", 0, 0x08, 0x78 | CH))
        # A different voice arrives while the note sounds.
        for reg, val in sorted(LOADED_TL.items()):
            writes.append(ChipWrite(KEY_ON + 20_000, "ym2151", 0, reg, val ^ 0x11))
        writes.append(ChipWrite(KEY_OFF, "ym2151", 0, 0x08, CH))
        writes.sort(key=lambda w: w.sample)
        song = analyze(vgm(writes), pcm=False)
        ev = next(e for e in song.events if e.on and e.ch == CH)
        patch = song.fm_patches[ev.ins]
        self.assertEqual([patch.ops[i].tl & 0x7F for i in range(4)],
                         [LOADED_TL[r] for r in sorted(LOADED_TL)])


if __name__ == "__main__":
    unittest.main()
