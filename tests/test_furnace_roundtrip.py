"""Furnace round trips: read back what the engine does with our effects.

Each test builds a synthetic song, exports the .fur to VGM with the bundled
Furnace build, and reads the register stream back. This is how the legato
semantics, the LFO mapping and the slide accrual model were measured. They
are kept as regression tests because a converter change that breaks them
sounds wrong in ways unit tests on the writer cannot see.

Skipped when the Furnace console build is missing.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _synth import inject_rows, note_song, staircase  # noqa: E402
from vgm2151fur.analyze import analyze, kc_kf_units  # noqa: E402
from vgm2151fur.diag import kc_kf_stream, register_writes, state_at  # noqa: E402
from vgm2151fur.furnace import find_furnace, export_vgm  # noqa: E402
from vgm2151fur.furwrite import write_fur  # noqa: E402
from vgm2151fur.vgm import load_vgm  # noqa: E402

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_roundtrip"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None


def _export(song, name: str, plan=None, key_on: int = 60000):
    if plan:
        with inject_rows(song, plan, key_on):
            data = write_fur(song, include_pcm=False, include_fm=True)
    else:
        data = write_fur(song, include_pcm=False, include_fm=True)
    fur = TMP / f"{name}.fur"
    fur.write_bytes(data)
    out = TMP / f"{name}.vgm"
    export_vgm(fur, out)
    return load_vgm(out)


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestLegatoSemantics(unittest.TestCase):
    """E8xx / E9xx add to the note and stay until the next note (measured)."""

    def test_transposes_accumulate(self):
        song = analyze(note_song(key_on=60000, key_off=160000), pcm=False)
        row0 = 0
        with inject_rows(song, [(2, 0xE8, 0x02), (5, 0xE8, 0x03), (8, 0xE9, 0x01)],
                         60000) as row0:
            fur = TMP / "legato.fur"
            fur.write_bytes(write_fur(song, include_pcm=False, include_fm=True))
        out = TMP / "legato.vgm"
        export_vgm(fur, out)
        export = load_vgm(out)
        stream = kc_kf_stream(export, 0)
        base = state_at(stream, 60500)
        sprow = song.samples_per_row

        def at(row: int) -> int:
            return state_at(stream, int(song.t0 + (row + 2) * sprow))

        self.assertEqual(at(row0 + 2) - base, 2 * 64)
        self.assertEqual(at(row0 + 5) - base, 5 * 64)
        self.assertEqual(at(row0 + 8) - base, 4 * 64)


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestLfoEffects(unittest.TestCase):
    """1Fxx PMD and 1Exx AMD share register 0x19, bit 7 selects (measured)."""

    def test_roundtrip_to_registers(self):
        song = analyze(note_song(key_on=60000, key_off=160000), pcm=False)
        song.chip_fx = [
            (60500, 0x17, 0xCA),
            (61500, 0x18, 0x02),
            (62500, 0x1F, 0x2D),
            (64000, 0x1E, 0x11),
        ]
        export = _export(song, "lfo")
        rows = register_writes(export, "ym2151", 0, [0x18, 0x19, 0x1B])
        values = [(reg, val) for _t, reg, val in rows]
        self.assertIn((0x18, 0xCA), values)
        self.assertIn((0x1B, 0x02), values)
        self.assertIn((0x19, 0xAD), values)  # PMD 0x2D with the select bit
        self.assertIn((0x19, 0x11), values)  # AMD plain


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestSweepModel(unittest.TestCase):
    """The slide model has to track what the engine actually plays.

    A dense 0.25 st staircase climb makes the engine run a fraction of a unit
    per tick faster than the written parameter when a rate sits between two
    integers. The model accrues the written parameter, so the exported pitch
    must land on the source's final pitch and stay. 0.9.28 and earlier
    compounded the fraction and held the note up to 1.7 st sharp.
    """

    def test_staircase_climb_lands_on_pitch(self):
        writes = staircase(61000, steps=20, step_samples=500)
        src_vgm = note_song(key_on=60000, key_off=160000, pitch_writes=writes)
        song = analyze(src_vgm, pcm=False)
        self.assertTrue(any(ev.sweep for ev in song.events if ev.on))
        export = _export(song, "climb")

        src_stream = kc_kf_stream(src_vgm, 0)
        exp_stream = kc_kf_stream(export, 0)
        t_end = writes[-1][0] + 2000
        src_pitch = state_at(src_stream, t_end)
        exp_pitch = state_at(exp_stream, t_end)
        self.assertIsNotNone(src_pitch)
        self.assertIsNotNone(exp_pitch)
        delta = exp_pitch - src_pitch
        self.assertLessEqual(
            abs(delta), 8,
            f"pitch drifted {delta} units ({delta / 64:.2f} st) after the climb",
        )


if __name__ == "__main__":
    unittest.main()
