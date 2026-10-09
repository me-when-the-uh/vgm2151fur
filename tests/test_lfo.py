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

from vgm2151fur.analyze import _flush_pms_ramp  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
