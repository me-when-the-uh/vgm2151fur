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
from vgm2151fur.furio import decompress_fur, iter_blocks  # noqa: E402
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

    def test_the_staircase_becomes_a_mult_macro(self):
        entries = {
            oi: (values, speed)
            for oi, values, speed in self.patch.op_mul_macro
        }
        self.assertIn(2, entries)
        values, _speed = entries[2]
        self.assertEqual(values[0], 0, "entry 0 is the attack multiplier")
        for value in range(4):
            self.assertIn(value, values)

    def test_the_file_stores_macro_code_three(self):
        data = write_fur(self.song, include_pcm=False, include_fm=True)
        feats = [f for f in _op_macros(data) if f[1] == 3]
        self.assertTrue(feats, "no MULT macro reached the instrument")
        tag, code, _speed, values = feats[0]
        self.assertEqual(tag, "O3")
        self.assertEqual(code, 3)
        self.assertIn(3, values)

    def test_a_key_on_burst_mult_is_the_instrument_not_a_macro(self):
        song = analyze(_song((KEY_ON, 0x50 | CH, 0)), pcm=False)
        ev = next(e for e in song.events if e.on and e.ch == CH)
        patch = song.fm_patches[ev.ins]
        self.assertEqual(ev.op_dtmul_pts, ())
        self.assertEqual(patch.ops[2].dt_mul, 0)
        self.assertEqual(patch.op_mul_macro, ())


if __name__ == "__main__":
    unittest.main()
