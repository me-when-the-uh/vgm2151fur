"""The driver-scripted vibrato ramp (PMS/AMS) becoming a Furnace macro.

A macro holds its last value and its step cannot exceed 255 ticks, so the
ramp has to carry the driver's reset to 0 or the vibrato never stops.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _synth  # noqa: E402

from vgm2151fur import furwrite as fw  # noqa: E402
from vgm2151fur.analyze import _flush_pms_ramp, analyze  # noqa: E402
from vgm2151fur.vgm import ChipWrite  # noqa: E402


class TestPmsRamp(unittest.TestCase):
    def test_reset_is_kept_so_the_macro_turns_off(self):
        acc: dict = {}
        _flush_pms_ramp(acc, 2, [(100, 0, 0), (500, 6, 0), (900, 0, 0)], 0)
        pms, _ams, deltas = acc[2][0]
        self.assertEqual(pms, [0, 6, 0])
        self.assertEqual(len(deltas), 3)

    def test_ramp_without_a_reset_is_unchanged(self):
        acc: dict = {}
        _flush_pms_ramp(acc, 1, [(100, 0, 0), (500, 6, 0)], 0)
        pms, _ams, _deltas = acc[1][0]
        self.assertEqual(pms, [0, 6])

    def test_lead_in_zero_is_inserted(self):
        acc: dict = {}
        _flush_pms_ramp(acc, 3, [(400, 6, 0)], 100)
        pms, _ams, deltas = acc[3][0]
        self.assertEqual(pms, [0, 6])
        self.assertEqual(deltas[0], 300)


class TestPerNotePmsMacro(unittest.TestCase):
    """A note without PMS writes must not inherit another note's ramp."""

    def _song(self):
        writes = []
        for reg, val in _synth.PATCH:
            writes.append(ChipWrite(0, "ym2151", 0, reg, val))
        writes += [
            ChipWrite(0, "ym2151", 0, 0x18, 0xD0),
            ChipWrite(0, "ym2151", 0, 0x19, 0x88),
            ChipWrite(0, "ym2151", 0, 0x1B, 0x02),
            ChipWrite(59000, "ym2151", 0, 0x28, 0x48),
            ChipWrite(59000, "ym2151", 0, 0x30, 0x00),
            ChipWrite(60000, "ym2151", 0, 0x08, 0x78),
            ChipWrite(100000, "ym2151", 0, 0x08, 0x00),
            ChipWrite(159000, "ym2151", 0, 0x28, 0x4A),
            ChipWrite(159000, "ym2151", 0, 0x30, 0x00),
            ChipWrite(160000, "ym2151", 0, 0x08, 0x78),
        ]
        for k, val in enumerate((0x10, 0x21, 0x31, 0x42, 0x52, 0x63, 0x73)):
            writes.append(ChipWrite(200000 + 200 * k, "ym2151", 0, 0x38, val))
        writes.append(ChipWrite(260000, "ym2151", 0, 0x08, 0x00))
        writes.sort(key=lambda w: w.sample)
        return analyze(_synth.vgm(writes))

    def test_constant_note_gets_its_own_zero_macro(self):
        song = self._song()
        notes = [e for e in song.events if e.on and e.ch == 0]
        self.assertEqual(len(notes), 2)
        first, second = notes
        self.assertEqual(song.fm_patches[first.ins].fms_macro, (0,))
        self.assertEqual(song.fm_patches[first.ins].ams_macro, (0,))
        self.assertNotEqual(first.ins, second.ins)

    def test_late_ramp_keeps_its_lead_in(self):
        song = self._song()
        notes = [e for e in song.events if e.on and e.ch == 0]
        patch = song.fm_patches[notes[1].ins]
        self.assertEqual(max(patch.fms_macro), 7)
        self.assertGreater(len(patch.fms_macro), 8)
        self.assertEqual(patch.fms_macro[0], 0)


class TestOpmTestPulse(unittest.TestCase):
    """The TEST-register pulse the driver uses to re-seed the OPM LFO."""

    def _song(self):
        key_on = 60000
        writes = _synth.lattice(40)
        for reg, val in _synth.PATCH:
            writes.append(ChipWrite(key_on - 1000, "ym2151", 0, reg, val))
        writes.append(ChipWrite(key_on - 3000, "ym2151", 0, 0x18, 0x0A))
        writes.append(ChipWrite(key_on, "ym2151", 0, 0x28, 0x48))
        writes.append(ChipWrite(key_on, "ym2151", 0, 0x30, 0x00))
        writes.append(ChipWrite(key_on, "ym2151", 0, 0x08, 0x78))
        # The driver pulses TEST bit 1 right after the key-on.
        writes.append(ChipWrite(key_on, "ym2151", 0, 0x01, 0x02))
        writes.append(ChipWrite(key_on, "ym2151", 0, 0x01, 0x00))
        writes.sort(key=lambda w: w.sample)
        return analyze(_synth.vgm(writes)), key_on

    def test_pulse_and_restore_are_both_emitted(self):
        song, key_on = self._song()
        fx = [e for e in song.chip_fx if e[1] == 0x17]
        self.assertTrue(any(len(e) == 3 and e[2] == 0 for e in fx),
                        "the 0x02 edge must become 17 00")
        self.assertTrue(any(len(e) == 4 and e[2] == 0x0A and e[3] == 1
                            for e in fx),
                        "the restore must carry the LFO speed one tick later")
        self.assertTrue(all(e[0] <= key_on + 2 for e in fx))

    def test_restore_lands_one_tick_after_the_pulse(self):
        song, _key_on = self._song()
        _orders, patterns, _loop, _pat_len = fw._build_patterns(
            song, 8, fw.PAT_LEN_MAX, 8)
        cells = []
        for ch, _idx, rows in patterns:
            for cell in rows:
                if cell is None:
                    continue
                for cmd, val in cell.fx or ():
                    if cmd == 0x17 and val in (0, 0x0A):
                        ed = next((v for c, v in cell.fx or () if c == 0xED), 0)
                        cells.append((ch, cmd, val, ed))
        self.assertIn((0, 0x17, 0x00, 0), cells)
        self.assertTrue(
            any(c[1] == 0x17 and c[2] == 0x0A and c[3] == 1 for c in cells),
            cells,
        )


if __name__ == "__main__":
    unittest.main()
