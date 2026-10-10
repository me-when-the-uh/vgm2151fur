"""Row condensing and the structural loss detector.

The condense factor lengthens every row, so Furnace's BPM (15 x rows/s) drops
by that factor.  `loss_total` counts content the writer could not place;
`dev_max` measures how far a row's straight-line pitch strays from the driver.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _synth import note_song  # noqa: E402

from vgm2151fur import convert, furwrite as fw  # noqa: E402
from vgm2151fur.analyze import analyze  # noqa: E402
from vgm2151fur.furio import parse_fur  # noqa: E402
from vgm2151fur.timing import condense_row, hz_speed_for_row  # noqa: E402


def _bpm(song) -> float:
    return 15.0 * song.hz / song.speed


class TestCondenseMath(unittest.TestCase):
    def test_hz_speed_for_row_targets_the_tick(self):
        hz, speed = hz_speed_for_row(900.0, 90.0)
        self.assertEqual(speed, 10)
        self.assertAlmostEqual(hz, 490.0, places=3)
        self.assertAlmostEqual(900.0 / speed, 90.0, places=6)

    def test_condense_halves_the_bpm(self):
        hz, speed, subdiv = condense_row(490.0, 10, 8, 2.0, 90.0)
        self.assertEqual(speed, 20)
        self.assertAlmostEqual(hz, 490.0, places=3)
        self.assertAlmostEqual(subdiv, 4.0, places=6)
        self.assertAlmostEqual(15.0 * hz / speed, 367.5, places=3)

    def test_factor_three_is_allowed(self):
        hz, speed, subdiv = condense_row(490.0, 10, 8, 3.0, 90.0)
        self.assertEqual(speed, 30)
        self.assertAlmostEqual(hz, 490.0, places=3)
        self.assertAlmostEqual(15.0 * hz / speed, 245.0, places=3)

    def test_factor_one_is_a_noop(self):
        self.assertEqual(condense_row(490.0, 10, 8, 1.0, 90.0), (490.0, 10, 8))


class TestLossCounters(unittest.TestCase):
    def test_add_fx_counts_a_dropped_effect(self):
        row = fw.Row()
        stats: dict = {}
        for cmd in (0x01, 0x02, 0x03):
            fw._add_fx(row, cmd, 1, 2, stats)
        self.assertEqual(stats.get("fx_dropped"), 1)

    def test_add_fx_is_lossless_with_room(self):
        row = fw.Row()
        stats: dict = {}
        for cmd in (0x01, 0x02):
            fw._add_fx(row, cmd, 1, 2, stats)
        self.assertNotIn("fx_dropped", stats)

    def test_loss_total_ignores_diagnostic_counters(self):
        self.assertEqual(
            fw.loss_total({"note_collision": 2, "ed_rolled": 9, "sweep_dev_bad": 7}),
            2,
        )

    def test_dev_max_reads_the_worst_deviation(self):
        self.assertEqual(fw.dev_max({"sweep_dev_max": 33}), 33)
        self.assertEqual(fw.dev_max({}), 0)


class TestAnalyzeCondense(unittest.TestCase):
    def test_condense_lengthens_rows_without_moving_hz(self):
        base = analyze(note_song(), pcm=False)
        twice = analyze(note_song(), pcm=False, condense=2.0)
        self.assertAlmostEqual(twice.hz, base.hz, delta=base.hz * 0.05)
        self.assertAlmostEqual(_bpm(twice), _bpm(base) / 2, delta=_bpm(base) * 0.03)
        self.assertGreater(twice.samples_per_row, base.samples_per_row * 1.8)

    def test_written_module_carries_the_condensed_grid(self):
        song = analyze(note_song(), pcm=False, condense=4.0)
        stats: dict = {}
        module = parse_fur(fw.write_fur(song, include_pcm=False, stats=stats))
        self.assertEqual(module.speed, song.speed)
        self.assertAlmostEqual(module.hz, song.hz, places=2)
        self.assertIsInstance(stats, dict)


class TestConvertVariants(unittest.TestCase):
    def _run(self, factors, mode, layout="flat"):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.out_dir = Path(tmp)
        with mock.patch.object(convert, "load_vgm", return_value=note_song()):
            return convert.convert_variants(
                Path("synthetic.vgz"), self.out_dir,
                factors=factors, mode=mode, pcm=False, loop_find=False,
                layout=layout,
            )

    def test_writes_the_set_without_touching_the_canonical_name(self):
        res = self._run((1, 2, 4), "none")
        names = {Path(r["dst"]).name for r in res}
        self.assertEqual(names, {"synthetic.fur", "synthetic x2.fur", "synthetic x4.fur"})

    def test_every_factor_halves_and_quarters_the_bpm(self):
        res = self._run((1, 2, 4), "all")
        by = {r["factor"]: r for r in res}
        self.assertAlmostEqual(by[2]["bpm"], by[1]["bpm"] / 2, delta=by[1]["bpm"] * 0.03)
        self.assertAlmostEqual(by[4]["bpm"], by[1]["bpm"] / 4, delta=by[1]["bpm"] * 0.03)

    def test_pick_follows_the_mode(self):
        self.assertEqual([r for r in self._run((1, 2, 4), "none") if r["pick"]][0]["factor"], 1)
        self.assertEqual([r for r in self._run((1, 2, 4), "all") if r["pick"]][0]["factor"], 4)

    def test_lossless_pick_never_adds_loss(self):
        res = self._run((1, 2, 3, 4), "lossless")
        base = next(r for r in res if r["factor"] == 1)
        pick = next(r for r in res if r["pick"])
        self.assertLessEqual(pick["loss_total"], base["loss_total"])
        self.assertLessEqual(pick["dev_max"], base["dev_max"] + 6)

    def test_every_variant_reports_loss_and_dev(self):
        for r in self._run((1, 2, 3, 4), "all"):
            self.assertIn("loss", r)
            self.assertIn("dev_max", r)
            self.assertIn("loss_total", r)

    def test_fractional_factor_names_the_file(self):
        res = self._run((1, 1.5), "all")
        names = {Path(r["dst"]).name for r in res}
        self.assertEqual(names, {"synthetic.fur", "synthetic x1.5.fur"})
        by = {r["factor"]: r for r in res}
        self.assertAlmostEqual(by[1.5]["bpm"], by[1]["bpm"] / 1.5, delta=by[1]["bpm"] * 0.05)

    def test_dirs_layout_files_default_and_optimised(self):
        res = self._run((1, 2, 3, 4), "all", layout="dirs")
        rel = {r["factor"]: Path(r["dst"]).relative_to(self.out_dir) for r in res}
        self.assertEqual(rel[1].parts[0], "default")
        self.assertEqual(rel[2].parts[0], "x2 optimised")
        self.assertEqual(rel[3].parts[0], "x3 optimised")
        for path in rel.values():
            self.assertTrue((self.out_dir / path).is_file())
        for r in res:
            self.assertIn("lossless", r)


class TestVariantsShareTheLoop(unittest.TestCase):

    @staticmethod
    def _fur_loop(module) -> tuple[int, int] | None:
        pat_len = module.pat_len

        def span(oi):
            widths = []
            for ch in range(module.n_ch):
                rows = module.patterns.get((ch, module.orders[ch][oi]), [])
                width = len(rows) if rows else pat_len
                for i, row in enumerate(rows):
                    if any(c == 0x0D for c, _v in row.fx):
                        width = min(width, i + 1)
                widths.append(width if rows else 0)
            return max(widths) if widths else pat_len

        bases, total, spans = [], 0, []
        for oi in range(module.n_ord):
            bases.append(total)
            w = span(oi)
            spans.append(w)
            total += w
        end = target = None
        for oi in range(module.n_ord):
            for ch in range(module.n_ch):
                rows = module.patterns.get((ch, module.orders[ch][oi]), [])
                for i, row in enumerate(rows[: spans[oi]]):
                    for cmd, val in row.fx:
                        if cmd == 0x0B:
                            end, target = bases[oi] + i, val
        if end is None:
            return None
        return bases[target], end

    def test_the_loop_is_found_once_on_the_1x_grid(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out_dir = Path(tmp)
        calls = []
        real_apply = convert.apply_loop

        def spy(song):
            calls.append(song)
            return real_apply(song)

        song = note_song()
        song.loop_sample = 120000
        song.loop_samples = 100000
        with mock.patch.object(convert, "load_vgm", return_value=song), \
                mock.patch.object(convert, "apply_loop", side_effect=spy):
            res = convert.convert_variants(
                Path("synthetic.vgz"), out_dir,
                factors=(1, 2, 4), mode="all", pcm=False, layout="dirs",
            )
        # One find for the whole set, on the 1x song
        self.assertEqual(len(calls), 1)
        loop_sample = calls[0].loop_sample
        self.assertIsNotNone(loop_sample)
        for r in res:
            module = parse_fur(Path(r["dst"]).read_bytes())
            loop = self._fur_loop(module)
            self.assertIsNotNone(loop, f"x{r['factor']:g}: no Bxx")
            row_samples = 44100.0 * module.speed / module.hz
            expected = round(loop_sample / row_samples)
            start, _end = loop
            self.assertLessEqual(
                abs(start - expected), 1,
                f"x{r['factor']:g}: loop starts at {start}, "
                f"the shared point sits at {expected}",
            )


if __name__ == "__main__":
    unittest.main()
