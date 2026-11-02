#!/usr/bin/env python3
"""OPL operator macros: a scripted modulator level must reach the chip.

Furnace rebuilds an OPL voice from its instrument at key-on and scales only an
operator marked KVS with the channel volume. A driver that scripts a
modulator's `$4n` therefore cannot be followed by the volume column at all:
the voice used to play one colour where the log moved it, and the conversion
looked perfect at every key-on because only the sustained part differed.

The route is a per-operator macro: feature `O1`..`O4`, code 6
(DIV_MACRO_OP_TL). Measured end to end through the bundled Furnace build, the
chip receives `$4n = 63 - value`. Furnace stores a *level* (up is louder)
while the log holds attenuation, and the writer inverts. A macro runs from
the key-on and has no delay of its own. Entry zero is the attack level, and a
move that starts several ticks in costs that many repeated entries, which is
why the curve is quantized to a `speed` rather than being sampled per tick.
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.analyze import VGM_RATE, analyze  # noqa: E402
from vgm2151fur.furwrite import write_fur  # noqa: E402
from vgm2151fur.furio import decompress_fur, iter_blocks  # noqa: E402
from vgm2151fur.vgm import ChipWrite, VgmFile  # noqa: E402

CH = 2
MOD, CAR = 0x02, 0x05          # ch2's op1 (modulator) and op2 (carrier) slots
KEY_ON = 60_000


def _vgm(writes):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x151,
        total_samples=400_000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={}, roms=[],
        writes=sorted(writes, key=lambda w: w.sample),
        opl_clock=0, opl2_clock=3_579_545, opl3_clock=0,
    )


def _wr(sample, reg, val):
    return ChipWrite(sample, "opl2", 0, reg, val)


def _voice(mod_tl=25):
    """A heartbeat plus a held ch2 voice whose modulator level walks a ramp."""
    w = []
    for k in range(1, 40):                      # key-on lattice on channel 5
        w += [_wr(k * 5000, 0xA0 | 5, 0x57), _wr(k * 5000, 0xB0 | 5, 0x35),
              _wr(k * 5000 + 20, 0xB0 | 5, 0x15)]
    for base in (MOD, CAR):
        w += [_wr(3000, 0x20 | base, 0x01),
              _wr(3000, 0x40 | base, mod_tl if base == MOD else 21),
              _wr(3000, 0x60 | base, 0xF8), _wr(3000, 0x80 | base, 0xF4),
              _wr(3000, 0xE0 | base, 0x00)]
    w.append(_wr(3000, 0xC0 | CH, 0x0A))        # algorithm 0: op2 is the carrier
    w += [_wr(KEY_ON, 0xA0 | CH, 0x57), _wr(KEY_ON + 2, 0xB0 | CH, 0x31)]
    return w


RAMP = [3, 7, 11, 15, 19, 23, 27, 31, 35, 39]


def _song(script_mod=True):
    w = _voice()
    if script_mod:
        for i, v in enumerate(RAMP):            # one step every ~5 ticks
            w.append(_wr(KEY_ON + 500 + i * 460, 0x40 | MOD, v))
    else:                                        # same script on the carrier
        for i, v in enumerate(RAMP):
            w.append(_wr(KEY_ON + 500 + i * 460, 0x40 | CAR, v))
    w.append(_wr(KEY_ON + 90_000, 0xB0 | CH, 0x11))
    return _vgm(w)


def _macro_features(data):
    """-> {feature tag: (code, len, delay, speed, values)} from a raw .fur."""
    out = {}
    for _off, mag, _size, blk in iter_blocks(decompress_fur(data)):
        if mag != b"INS2":
            continue
        pos = 4
        while pos + 4 <= len(blk):
            tag = blk[pos:pos + 2]
            if tag == b"EN":
                break
            sz = struct.unpack_from("<H", blk, pos + 2)[0]
            body = blk[pos + 4:pos + 4 + sz]
            if tag.startswith(b"O") and len(body) > 10:
                code, ln = body[2], body[3]
                delay, speed = body[8], body[9]
                out[tag.decode()] = (code, ln, delay, speed,
                                     list(body[10:10 + ln]))
            pos += 4 + sz
    return out


class TestOperatorMacroCapture(unittest.TestCase):
    """A scripted modulator becomes that operator's macro, in `$4n` terms."""

    def setUp(self):
        self.song = analyze(_song(), pcm=False)
        self.ev = next(e for e in self.song.events if e.on and e.ch == CH)
        self.patch = self.song.fm_patches[self.ev.ins]

    def test_modulator_curve_is_captured_per_operator(self):
        got = dict(self.ev.opl_op_tl)
        self.assertIn(0, got, "the modulator's levels were never recorded")
        levels = [v for _s, v in got[0]]
        self.assertEqual(levels, RAMP)

    def test_macro_targets_the_modulator_with_absolute_levels(self):
        entries = {oi: (values, speed)
                   for oi, values, speed in self.patch.op_tl_macro}
        self.assertIn(0, entries)
        self.assertNotIn(1, entries, "the carrier rides the volume column")
        values, speed = entries[0]
        # A macro starts with the note and cannot wait: entry 0 is the level the
        # voice attacks on, and the first logged move follows it.
        self.assertEqual(values[0], 25, "entry zero must be the attack level")
        seen = list(values)
        self.assertEqual([v for v in seen if v in RAMP], RAMP,
                         "the logged steps must arrive in order, unturned")

    def test_file_stores_levels_in_furnaces_domain_and_no_delay(self):
        data = write_fur(self.song, include_pcm=False, include_fm=True)
        feats = _macro_features(data)
        self.assertIn("O1", feats, sorted(feats))
        self.assertNotIn("O2", feats)
        code, ln, delay, speed, values = feats["O1"]
        self.assertEqual(code, 6, "DIV_MACRO_OP_TL")
        self.assertEqual(delay, 0, "an operator macro has no delay concept")
        self.assertGreaterEqual(speed, 1)
        self.assertEqual(ln, len(values))
        # byte = 127 - $4n, so the editor reads back exactly the logged level.
        self.assertEqual(values[0], 127 - 25, "entry zero is the attack level")
        self.assertEqual([v for v in values if 127 - v in RAMP],
                         [127 - t for t in RAMP])

    def test_editor_visible_level_stays_in_the_opl_six_bit_range(self):
        """What the UI shows for an OPL operator TL macro must be a $4n.

        The file byte is Furnace's level domain (`$4n = 63 - (byte & 63)`, editor
        shows `127 - byte`), so the test checks the number a person reads in the
        instrument editor, not the raw byte.
        """
        data = write_fur(self.song, include_pcm=False, include_fm=True)
        bad = []
        for tag, (_code, _ln, _delay, _speed, values) in _macro_features(
                data).items():
            for v in values:
                shown = 127 - v
                if not (0 <= shown <= 63):
                    bad.append((tag, v, shown))
        self.assertEqual(bad, [], f"editor levels outside $4n range: {bad[:6]}")

    def test_carrier_script_stays_on_the_volume_column(self):
        song = analyze(_song(script_mod=False), pcm=False)
        for p in song.fm_patches:
            for oi, _vals, _s in getattr(p, "op_tl_macro", ()):
                self.assertNotEqual(
                    oi, 1,
                    "a carrier level rides the volume column, not a macro")

    def test_macro_lives_on_a_copy_so_plain_notes_keep_their_instrument(self):
        self.assertIn("+lvl", self.patch.name)
        plain = [p for p in self.song.fm_patches if not p.op_tl_macro]
        self.assertTrue(plain, "the instrument without a macro must survive")


@unittest.skipUnless(
    os.environ.get("VGM2151FUR_FURNACE")
    or (ROOT / "furnace" / "furnace.exe").is_file(),
    "furnace console build missing")
class TestOperatorMacroPlayback(unittest.TestCase):
    """The real player must put the logged `$4n` back on the chip."""

    def test_played_modulator_follows_the_ramp(self):
        import tempfile

        from vgm2151fur.furnace import export_vgm
        from vgm2151fur.vgm import load_vgm

        song = analyze(_song(), pcm=False)
        with tempfile.TemporaryDirectory() as td:
            fur = Path(td) / "m.fur"
            fur.write_bytes(write_fur(song, include_pcm=False,
                                      include_fm=True))
            out = Path(td) / "m.vgm"
            export_vgm(fur, out)
            got = [w.val & 0x3F for w in load_vgm(out).writes
                   if w.chip == "opl2" and (w.reg & 0xFF) == (0x40 | MOD)]
        seen = [v for v in got if v in RAMP]
        missing = [v for v in RAMP if v not in got]
        self.assertFalse(missing,
                         f"player never reached $4n levels {missing}; "
                         f"wrote {got[:16]}")
        # The module loops, so the ramp plays more than once: compare one pass.
        self.assertEqual(seen[:len(RAMP)], RAMP,
                         "the ramp must arrive in order and unturned")


if __name__ == "__main__":
    unittest.main()
