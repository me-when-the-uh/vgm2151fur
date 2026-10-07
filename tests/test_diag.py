"""The diagnostic helpers, on synthetic VGMs. No Furnace needed."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _synth import note_song, vgm  # noqa: E402
from vgm2151fur.diag import kc_kf_stream, pitch_runs, register_writes, state_at  # noqa: E402
from vgm2151fur.vgm import ChipWrite  # noqa: E402


class TestPitchRuns(unittest.TestCase):
    def test_identical_streams_have_no_runs(self):
        song = note_song(key_on=60000, key_off=160000)
        self.assertEqual(pitch_runs(song, song, 0, min_run_ms=50), [])

    def test_held_divergence_is_reported_with_span(self):
        a = note_song(key_on=60000, key_off=160000)
        writes = [
            (100000, 0x28, 0x02),  # +2 st inside the held note
            (120000, 0x28, 0x00),  # back
        ]
        extra = [
            ChipWrite(sample, "ym2151", 0, reg, val)
            for sample, reg, val in writes
        ]
        b = vgm(sorted(list(a.writes) + extra, key=lambda w: w.sample))
        runs = pitch_runs(a, b, 0, min_run_ms=100)
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0].worst_st, 2.0, places=2)
        self.assertGreater(runs[0].t1 - runs[0].t0, 19000)

    def test_short_glitch_is_ignored(self):
        a = note_song(key_on=60000, key_off=160000)
        extra = [ChipWrite(100000, "ym2151", 0, 0x28, 0x02)]
        b = vgm(sorted(list(a.writes) + extra, key=lambda w: w.sample))
        self.assertEqual(pitch_runs(a, b, 0, min_run_ms=500), [])


class TestStreams(unittest.TestCase):
    def test_kc_kf_stream_tracks_each_write(self):
        song = note_song(key_on=60000, key_off=160000, kc=0x42, kf=0x14)
        stream = kc_kf_stream(song, 0)
        first = state_at(stream, 60000)
        second = state_at(stream, 100000)
        self.assertEqual(first, second)  # nothing moves in a plain note

    def test_register_writes_filter_changes(self):
        song = note_song(key_on=60000, key_off=160000)
        rows = register_writes(song, "ym2151", 0, [0x28, 0x30])
        self.assertTrue(rows)
        self.assertTrue(all(reg in (0x28, 0x30) for _t, reg, _v in rows))


if __name__ == "__main__":
    unittest.main()
