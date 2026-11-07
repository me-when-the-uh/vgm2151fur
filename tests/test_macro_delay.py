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

from vgm2151fur.analyze import (  # noqa: E402
    ToneIns, _compress_tone_delays, _lfo_tail_cycle, _macro_series_delay,
    _merge_tone_prefixes, _tone_macro_same_for,
)
from vgm2151fur.furwrite import Buf, _write_macro  # noqa: E402


class TestPitchLfoLoop(unittest.TestCase):
    def test_an_lfo_tail_loops_at_its_first_cycle(self):
        pattern = [10, 10, 10, 20, 20, 20, 20, 10, 10, 10, 10, 20, 20, 20]
        self.assertEqual(_lfo_tail_cycle([0] * 20 + pattern + pattern),
                         (20, len(pattern)))

    def test_the_delay_and_loop_are_kept_together(self):
        pattern = [10, 10, 10, 20, 20, 20, 20, 10, 10, 10, 10, 20, 20, 20]
        series = [0] * 20 + pattern + pattern
        values, _speed, loop, delay = _macro_series_delay(
            series, tail_loop=True,
        )
        self.assertEqual(delay, 19)
        self.assertEqual(loop, 1)
        self.assertEqual(values, (0,) + tuple(pattern))

    def test_a_monotonic_tail_is_not_an_lfo(self):
        self.assertIsNone(_lfo_tail_cycle([0] * 5 + list(range(20))))

    def test_step_length_compresses_a_long_hold(self):
        pattern = [10] * 8 + [20] * 8
        series = [0] * 300 + pattern + pattern
        values, speed, loop, delay = _macro_series_delay(
            series, tail_loop=True, step_compress=True,
        )
        self.assertEqual(delay, 255)
        self.assertGreater(speed, 1)
        self.assertLess(len(values), 20)
        self.assertEqual(loop, values.index(10))


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

    def test_a_cycle_is_not_turned_into_a_loop(self):
        # The shortened values happen to be cyclic, but the original did not
        # loop; the delay reproduces the held first tick and the macro ends
        # on its last value.
        values, _speed, loop, delay = _macro_series_delay(
            [1, 1, 2, 1, 2]
        )
        self.assertEqual(delay, 1)
        self.assertEqual(loop, 255)
        self.assertEqual(values, (1, 2, 1, 2))

    def test_recompress_tone_macros_before_grouping(self):
        bucket = [
            ToneIns(
                wave=1,
                pitch=(0, 0, 0, 0, 0, 0, 5, 6),
                vol=(3, 3, 3, 3, 2),
                vol_speed=2,
            ),
        ]
        _compress_tone_delays(bucket)
        spec = bucket[0]
        self.assertEqual(spec.pitch, (0, 5, 6))
        self.assertEqual(spec.pitch_delay, 5)
        self.assertEqual(spec.vol, (3, 2))
        self.assertEqual(spec.vol_delay, 7)

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


class TestToneMacroTiming(unittest.TestCase):
    """A fast looping tail must not look like a slow note's prefix."""

    @staticmethod
    def _pair() -> tuple[ToneIns, ToneIns]:
        simple = ToneIns(
            pitch=(0, 1), pitch_speed=28, pitch_loop=255, pitch_delay=22,
        )
        tail = ToneIns(
            pitch=(0, 1, 1, 1, 1, 1, 1, 1, 1, 121, 121, 121, 129, 129),
            pitch_speed=8, pitch_loop=10, pitch_delay=24,
        )
        return simple, tail

    def test_each_macro_uses_its_own_timing(self):
        simple, tail = self._pair()
        self.assertFalse(_tone_macro_same_for(tail, simple, 335))
        self.assertTrue(_tone_macro_same_for(tail, simple, 4))

    def test_a_long_note_does_not_merge_into_the_tail_instrument(self):
        simple, tail = self._pair()
        bucket = [simple, tail]
        mapping = _merge_tone_prefixes(
            bucket, {0: [(0, 335 * 100)]}, 100.0, 1,
        )
        self.assertEqual(mapping, {0: 0, 1: 1})


if __name__ == "__main__":
    unittest.main()
