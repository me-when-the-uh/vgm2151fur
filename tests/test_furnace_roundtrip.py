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

from _synth import climb, inject_rows, lattice, note_song, staircase, vgm  # noqa: E402
from vgm2151fur.analyze import analyze, kc_kf_units  # noqa: E402
from vgm2151fur.diag import kc_kf_stream, register_writes, state_at  # noqa: E402
from vgm2151fur.furnace import find_furnace, export_vgm  # noqa: E402
from vgm2151fur.furwrite import write_fur  # noqa: E402
from vgm2151fur.vgm import ChipWrite, load_vgm  # noqa: E402

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


BEND_CH = 4
BASE_KC = 0x3E        # the key code the bend climbs from


def _bend_song(key_on: int, *, semitones: int = 8, step_samples: int = 130):
    """One note on channel 4 that bends up steeply from its own key-on.

    The steps run past the end of the first row, which is what makes that row's
    tick count matter: a bend that finishes inside one clipped row hides the
    error. The voice is written here rather than taken from `_synth.PATCH`, whose
    block carries its own KC/KF, and channel 0 belongs to the key-on heartbeat.
    """
    writes = list(lattice(80))
    regs = [(0x20 | BEND_CH, 0xC7)]                    # RL both, fb 7, alg 7
    for op in range(4):
        regs.append(((0x40 + 8 * op) | BEND_CH, 0x11))  # DT1 / MULT
        regs.append(((0x60 + 8 * op) | BEND_CH, 0x20))  # TL
        regs.append(((0x80 + 8 * op) | BEND_CH, 0x1F))  # KS / AR
        regs.append(((0xA0 + 8 * op) | BEND_CH, 0x05))  # AM / D1R
        regs.append(((0xC0 + 8 * op) | BEND_CH, 0x00))  # DT2 / D2R
        regs.append(((0xE0 + 8 * op) | BEND_CH, 0x5F))  # D1L / RR
    for reg, val in regs:
        writes.append(ChipWrite(key_on - 100, "ym2151", 0, reg, val))
    writes.append(ChipWrite(key_on - 50, "ym2151", 0, 0x28 | BEND_CH, BASE_KC))
    writes.append(ChipWrite(key_on - 50, "ym2151", 0, 0x30 | BEND_CH, 0x00))
    writes.append(ChipWrite(key_on, "ym2151", 0, 0x08, 0x78 | BEND_CH))
    bend = climb(key_on + 30, semitones=semitones, step_samples=step_samples,
                 kc=BASE_KC, kf=0x00, ch=BEND_CH)
    for sample, reg, val in bend:
        writes.append(ChipWrite(sample, "ym2151", 0, reg, val))
    writes.append(ChipWrite(bend[-1][0] + 40_000, "ym2151", 0, 0x08, BEND_CH))
    writes.sort(key=lambda w: w.sample)
    v = vgm(writes)
    return analyze(v, pcm=False), v, bend


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestSweepModel(unittest.TestCase):
    """The slide model has to track what the engine actually plays.

    A dense 0.25 st staircase climb makes the engine run a fraction of a unit
    per tick faster than the written parameter when a rate sits between two
    integers. The model accrues the written parameter, so the exported pitch
    must land on the source's final pitch and stay. A model that accrued the
    ideal rate instead would compound the fraction and hold the note sharp.
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

    def test_bend_on_a_delayed_key_on_lands_on_pitch(self):
        """A steep bend whose row starts late still reaches its pitch.

        A row runs its effects from the tick its own ED delay names. When a
        key-on lands between two ticks the delay rounds up, so the engine starts
        the slide later than the key-on sample and has fewer ticks to travel;
        rating the row from the key-on under-delivers the climb. Nothing notices
        afterwards, because every later row is measured against what the model
        believes it wrote, so the note holds flat until its next key-on. This is
        the shape "04 Blue Flight" plays on FM3 and FM4: a sweep of two and a
        half octaves from the key-on whose export sat about two semitones low.
        """
        # Learn the grid from the heartbeat, then land the key-on six-tenths of a
        # tick past a boundary four ticks into a row. ED rounds up to 4, so the
        # engine has three ticks to travel where the row looks like 3.4.
        probe = analyze(note_song(key_on=60_000, key_off=160_000), pcm=False)
        tick = probe.samples_per_row / probe.speed
        row0 = probe.place(60_000)[0]
        key_on = int(probe.t0 + ((row0 + 3) * probe.speed + 3.6) * tick)
        song, src_vgm, bend = _bend_song(key_on)
        row1, ed1 = song.place(key_on)
        ev = next(e for e in song.events if e.on and e.ch == BEND_CH)
        self.assertTrue(ev.sweep, "the bend should stay a ramp")
        span_model = ((song.t0 + (row1 + 1) * song.samples_per_row) - key_on) / tick
        self.assertGreater(span_model - (song.speed - ed1), 0.3,
                           f"no tick to lose: model {span_model:.2f}, "
                           f"engine {song.speed - ed1}")

        export = _export(song, "delayed_bend", key_on=key_on)
        src_stream = kc_kf_stream(src_vgm, BEND_CH)
        exp_stream = kc_kf_stream(export, BEND_CH)
        t_end = bend[-1][0] + 6000
        src_pitch = state_at(src_stream, t_end)
        exp_pitch = state_at(exp_stream, t_end)
        self.assertIsNotNone(src_pitch)
        self.assertIsNotNone(exp_pitch)
        delta = exp_pitch - src_pitch
        self.assertLessEqual(
            abs(delta), 8,
            f"a bend on a row delayed {ed1} ticks landed {delta} units "
            f"({delta / 64:.2f} st) off its held pitch",
        )


if __name__ == "__main__":
    unittest.main()
