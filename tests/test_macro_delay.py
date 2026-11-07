"""Leading macro repeats become the macro's delay field.

A tone pitch (or volume) macro that holds its first value for several ticks
can store one copy and let the delay hold it. The value list then has room
for the part that used to be cut off by the 255-entry limit.
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.analyze import _macro_series_delay  # noqa: E402
from vgm2151fur.furwrite import Buf, _write_macro  # noqa: E402


class TestMacroDelay(unittest.TestCase):
    def test_a_leading_run_is_held_by_the_delay(self):
        values, speed, loop, delay = _macro_series_delay(
            [5] * 10 + [6, 7, 8]
        )
        self.assertEqual(values, (5, 6, 7, 8))
        self.assertEqual((speed, loop), (1, 255))
        self.assertEqual(delay, 9, "ten ticks held: first tick + delay 9")

    def test_a_very_long_run_caps_the_delay_and_keeps_copies(self):
        values, _speed, _loop, delay = _macro_series_delay(
            [5] * 300 + [6]
        )
        self.assertEqual(delay, 255)
        self.assertEqual(values.count(5), 45)
        self.assertEqual(values[-1], 6)

    def test_a_cycle_is_not_shortened(self):
        # Shortening would turn the head into a cycle the original did not
        # have, so the full sequence stays and no delay is used.
        values, _speed, _loop, delay = _macro_series_delay(
            [1, 1, 2, 1, 2]
        )
        self.assertEqual(delay, 0)
        self.assertEqual(values, (1, 1, 2, 1, 2))

    def test_writer_emits_the_delay_byte(self):
        b = Buf()
        _write_macro(b, 4, (5, 6), speed=1, delay=9, width=2)
        code, length, _loop, _rel, _mode, word = struct.unpack_from(
            "<BBBBBB", b.b, 0
        )
        delay, speed = struct.unpack_from("<BB", b.b, 6)
        self.assertEqual((code, length), (4, 2))
        self.assertEqual(word, 0x81, "16-bit value encoding")
        self.assertEqual((delay, speed), (9, 1))
        self.assertEqual(struct.unpack_from("<hh", b.b, 8), (5, 6))


if __name__ == "__main__":
    unittest.main()
