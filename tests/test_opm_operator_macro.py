"""YM2151 per-operator DT1/MULT macros inside very short notes.

A driver can retune one operator every frame while a staccato note sounds
(Magical Chase walks channel 1's third operator MULT through 0..4). The
change is faster than a pattern row and belongs to one operator, so neither a
row pitch slide nor a whole-voice pitch macro reproduces it. Furnace's
O1..O4 features carry per-operator DT1 (code 9) and MULT (code 3) macros.
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.analyze import analyze  # noqa: E402
from vgm2151fur.furio import (  # noqa: E402
    decompress_fur, iter_blocks, parse_fur,
)
from vgm2151fur.furwrite import write_fur  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _synth import note_song  # noqa: E402

CH = 2
KEY_ON = 60_000
KEY_OFF = 63_000


def _song(*writes):
    return note_song(
        key_on=KEY_ON, key_off=KEY_OFF, ch=CH,
        pitch_writes=list(writes),
    )


def _op_macros(data):
    """-> [(operator tag, macro code, speed, values)] from a raw .fur."""
    out = []
    for _off, mag, _size, blk in iter_blocks(decompress_fur(data)):
        if mag != b"INS2":
            continue
        pos = 4
        while pos + 4 <= len(blk):
            tag = blk[pos:pos + 2]
            if tag == b"EN":
                break
            size = struct.unpack_from("<H", blk, pos + 2)[0]
            body = blk[pos + 4:pos + 4 + size]
            if tag.startswith(b"O") and len(body) > 10:
                code = body[2]
                count = body[3]
                speed = body[9]
                out.append((
                    tag.decode(), code, speed,
                    list(body[10:10 + count]),
                ))
            pos += 4 + size
    return out


class TestShortNoteOperatorTracking(unittest.TestCase):
    def setUp(self):
        self.song = analyze(_song(
            (KEY_ON + 100, 0x50 | CH, 0),
            (KEY_ON + 900, 0x50 | CH, 1),
            (KEY_ON + 1700, 0x50 | CH, 2),
            (KEY_ON + 2500, 0x50 | CH, 3),
        ), pcm=False)
        self.ev = next(e for e in self.song.events if e.on and e.ch == CH)
        self.patch = self.song.fm_patches[self.ev.ins]

    def test_the_note_records_the_operator_staircase(self):
        self.assertTrue(self.ev.op_dtmul_pts)
        self.assertEqual(
            [(op, value) for _s, op, value in self.ev.op_dtmul_pts],
            [(2, 0), (2, 1), (2, 2), (2, 3)],
        )

    def test_the_staircase_becomes_row_effects(self):
        # The steps land on separate rows, so the pattern carries them.
        self.assertTrue(self.ev.op_mul_fx)
        self.assertEqual(
            [value for _s, op, value in self.ev.op_mul_fx], [0, 1, 2, 3]
        )
        self.assertEqual(self.patch.op_mul_macro, ())

    def test_the_file_stores_16xy_rows(self):
        data = write_fur(self.song, include_pcm=False, include_fm=True)
        m = parse_fur(data)
        fx = [
            (code, value)
            for (_ch, _pat), rows in m.patterns.items()
            for row in rows for code, value in row.fx
        ]
        # Operator index 2 is effect x=2 on the OPM (orderedOps 0,2,1,3).
        self.assertEqual(
            [value for code, value in fx if code == 0x16],
            [0x20, 0x21, 0x22, 0x23],
        )
        self.assertFalse(
            [f for f in _op_macros(data) if f[1] == 3],
            "row-safe steps must not create an operator macro",
        )

    def test_two_steps_inside_one_row_stay_a_macro(self):
        # Both changes are shorter than a row, so a row effect would lose one.
        song = analyze(_song(
            (KEY_ON + 100, 0x50 | CH, 0),
            (KEY_ON + 200, 0x50 | CH, 1),
            (KEY_ON + 300, 0x50 | CH, 2),
            (KEY_ON + 400, 0x50 | CH, 3),
        ), pcm=False)
        ev = next(e for e in song.events if e.on and e.ch == CH)
        patch = song.fm_patches[ev.ins]
        self.assertFalse(ev.op_mul_fx)
        self.assertTrue(patch.op_mul_macro)

    def test_detune_steps_use_53xy_on_rows(self):
        song = analyze(_song(
            (KEY_ON + 100, 0x50 | CH, 0x31),   # DT1 raw 3 (engine 6)
            (KEY_ON + 900, 0x50 | CH, 0x71),   # DT1 raw 7 (engine 0)
        ), pcm=False)
        ev = next(e for e in song.events if e.on and e.ch == CH)
        self.assertEqual(
            [value for _s, op, value in ev.op_dt_fx], [6, 0]
        )
        data = write_fur(song, include_pcm=False, include_fm=True)
        m = parse_fur(data)
        fx = [
            value for (_ch, _pat), rows in m.patterns.items()
            for row in rows for code, value in row.fx if code == 0x53
        ]
        self.assertEqual(fx, [0x26, 0x20])

    def test_detune2_steps_use_55xy_on_rows(self):
        reg = 0xD0 | CH                     # operator 3's DT2/D2R
        song = analyze(_song(
            (KEY_ON + 100, reg, 0x40),
            (KEY_ON + 900, reg, 0x80),
        ), pcm=False)
        ev = next(e for e in song.events if e.on and e.ch == CH)
        self.assertEqual(
            [value for _s, op, value in ev.op_dt2_fx], [1, 2]
        )
        data = write_fur(song, include_pcm=False, include_fm=True)
        m = parse_fur(data)
        fx = [
            value for (_ch, _pat), rows in m.patterns.items()
            for row in rows for code, value in row.fx if code == 0x55
        ]
        self.assertEqual(fx, [0x21, 0x22])

    def test_a_key_on_burst_mult_is_the_instrument_not_a_macro(self):
        song = analyze(_song((KEY_ON, 0x50 | CH, 0)), pcm=False)
        ev = next(e for e in song.events if e.on and e.ch == CH)
        patch = song.fm_patches[ev.ins]
        self.assertEqual(ev.op_dtmul_pts, ())
        self.assertEqual(patch.ops[2].dt_mul, 0)
        self.assertEqual(patch.op_mul_macro, ())


if __name__ == "__main__":
    unittest.main()
