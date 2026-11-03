"""Yamaha Y8950: OPL2 FM plus one delta-T (ADPCM-B) voice.

The FM half is the shared OPL path under `KIND_Y8950`; the tests here cover the
`$5C` register stream, the ADPCM register map (`$07` control, `$09/$0A` start,
`$0B/$0C` end, `$10/$11` pitch, `$12` level), the 0x88 ROM block, the 32-byte
address units, and a real arcade rip through Furnace when the root is set.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import CHIP_CHANNELS, parse_fur
from vgm2151fur.furwrite import (
    CHIP_Y8950, CHIP_Y8950_DRUMS, INS_ADPCMB, write_fur,
)
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile, chips_in_head, load_vgm
from vgm2151fur.y8950 import collect_y8950_delta_t

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_y8950"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
CLOCK = 3579545


def _vgm(writes, roms=(), **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=CLOCK, k007232_clock=0, gd3={},
        roms=list(roms), writes=list(writes), **kw,
    )


def _lattice(n=60):
    return [ChipWrite(k * 5000, "ym2151", 0, 0x08, 0x78) for k in range(n)]


def _raw_vgm(cmds: bytes, blocks=(), clock: int = CLOCK) -> bytes:
    buf = bytearray(0x100)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x30:0x34] = CLOCK.to_bytes(4, "little")
    buf[0x34:0x38] = (len(buf) - 0x34).to_bytes(4, "little")
    buf[0x58:0x5C] = clock.to_bytes(4, "little")
    body = bytearray()
    for typ, size, start, data in blocks:
        payload = size.to_bytes(4, "little") + start.to_bytes(4, "little") + data
        body += bytes([0x67, 0x66, typ]) + len(payload).to_bytes(4, "little")
        body += payload
    body += cmds + b"\x66"
    return bytes(buf) + bytes(body)


def _reg(t, reg, val):
    return ChipWrite(t, "y8950", 0, reg, val & 0xFF)


def _adpcm_note(t, *, start=0x100, end=0x1FF, pitch=0x1000, level=0x80,
                loop=False):
    """One delta-T burst: start, end, pitch, level, then the control write."""
    return [
        _reg(t, 0x09, start & 0xFF),
        _reg(t, 0x0A, (start >> 8) & 0xFF),
        _reg(t, 0x0B, end & 0xFF),
        _reg(t, 0x0C, (end >> 8) & 0xFF),
        _reg(t, 0x10, pitch & 0xFF),
        _reg(t, 0x11, (pitch >> 8) & 0xFF),
        _reg(t, 0x12, level),
        _reg(t, 0x07, 0xB0 if loop else 0xA0),
    ]


def _rom(start: int, data: bytes) -> RomSlice:
    return RomSlice(0, 0, start, data, "y8950_deltat")


def _body(n=512):
    return bytes(0x80 | ((i * 7) % 126) for i in range(n))


class TestY8950Parsing(unittest.TestCase):
    def test_register_writes_clock_and_rom(self):
        cmds = b"\x5c\x07\xa0\x5c\x09\x34\x5c\x12\x80"
        raw = _raw_vgm(cmds, blocks=[(0x88, 0x1000, 0x200, bytes(range(32)))])
        p = TMP / "parse.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.y8950_clock, CLOCK)
        regs = [(w.reg, w.val) for w in v.writes if w.chip == "y8950"]
        self.assertEqual(regs, [(0x07, 0xA0), (0x09, 0x34), (0x12, 0x80)])
        self.assertEqual([(s.kind, s.start) for s in v.roms],
                         [("y8950_deltat", 0x200)])
        self.assertIn("y8950", chips_in_head(raw[:0x100]))

    def test_no_unconverted_warning(self):
        raw = _raw_vgm(b"\x5c\x07\xa0")
        p = TMP / "warn.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertFalse(any("does not convert" in w for w in v.warnings))


class TestY8950DeltaT(unittest.TestCase):
    def test_one_control_write_is_one_hit(self):
        rom = _rom(0x2000, _body())
        writes = _lattice() + _adpcm_note(1000, start=0x100, end=0x1FF,
                                          pitch=0x1000, level=0x80)
        samples, _warn = _collect(writes, rom)
        self.assertEqual(len(samples), 1)
        smp = samples[0]
        self.assertEqual((smp.start, smp.end), (0x100 << 5, ((0x1FF + 1) << 5) - 1))
        self.assertEqual(smp.rate, 0x1000)
        self.assertEqual(len(smp.hits), 1)
        self.assertEqual(smp.hits[0].vol, 0x80)

    def test_reset_stops_the_voice(self):
        writes = _lattice() + _adpcm_note(1000) + [_reg(5000, 0x07, 0x01)]
        samples, _warn = _collect(writes, _rom(0x2000, _body()))
        self.assertEqual(samples[0].hits[0].end_time, 5000)

    def test_a_second_start_closes_the_first(self):
        writes = (_lattice() + _adpcm_note(1000)
                  + _adpcm_note(2000, pitch=0x2000))
        samples, _warn = _collect(writes, _rom(0x2000, _body()))
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].hits[0].end_time, 2000)

    def test_loop_bit_is_kept(self):
        writes = _lattice() + _adpcm_note(1000, loop=True)
        samples, _warn = _collect(writes, _rom(0x2000, _body()))
        self.assertTrue(samples[0].hits[0].loop)

    def test_a_missing_rom_warns(self):
        rom = _rom(0x2000, _body())
        writes = _lattice() + _adpcm_note(1000)
        samples, warn = collect_y8950_delta_t(_vgm(writes), b"")
        self.assertTrue(any("delta-T" in w for w in warn), warn)

    def test_rate_and_body(self):
        rom = _rom(0x2000, _body())
        writes = _lattice() + _adpcm_note(1000, pitch=0x8000)
        samples, _warn = _collect(writes, rom)
        self.assertEqual(samples[0].rate, 0x8000)
        self.assertTrue(samples[0].data)


def _collect(writes, rom):
    # Leave the ROM to the collector so it stitches the 0x88 slice in.
    return collect_y8950_delta_t(_vgm(writes, roms=[rom]))


class TestY8950Writer(unittest.TestCase):
    def _song(self, drums=False):
        writes = _lattice()
        # A little FM so the OPL half has a key-on.
        writes += [
            ChipWrite(2000, "y8950", 0, 0xB0, 0x20),
            ChipWrite(3000, "y8950", 0, 0xB0, 0x00),
        ]
        if drums:
            writes += [
                ChipWrite(4000, "y8950", 0, 0xBD, 0x20),
                ChipWrite(4100, "y8950", 0, 0xBD, 0x30),
            ]
        writes += _adpcm_note(1000)
        return analyze(_vgm(writes, roms=[_rom(0x2000, _body())]))

    def test_chip_and_channel_count(self):
        song = self._song()
        m = parse_fur(write_fur(song))
        self.assertEqual(m.chips[-1], CHIP_Y8950)
        self.assertEqual(m.n_ch, 8 + 10)  # YM2151 lattice + 9 FM + 1 ADPCM
        self.assertEqual(sum(CHIP_CHANNELS[c] for c in m.chips), m.n_ch)

    def test_drums_selects_the_bigger_system(self):
        song = self._song(drums=True)
        m = parse_fur(write_fur(song))
        self.assertEqual(m.chips[-1], CHIP_Y8950_DRUMS)
        self.assertEqual(m.n_ch, 8 + 12)  # + 11 FM + 1 ADPCM

    def test_adpcm_instrument_type(self):
        song = self._song()
        types = [
            payload[2] | (payload[3] << 8)
            for _pos, magic, _size, payload in _iter_ins(write_fur(song))
        ]
        self.assertIn(INS_ADPCMB, types)


def _iter_ins(data: bytes):
    from vgm2151fur.furio import iter_blocks
    return [b for b in iter_blocks(data) if b[1] == b"INS2"]


def _sample_root() -> Path | None:
    raw = os.environ.get("VGM2151FUR_Y8950_RIPS")
    if raw and Path(raw).is_dir():
        return Path(raw)
    return None


@unittest.skipUnless(HAVE_FURNACE and _sample_root(), "Y8950 rips missing")
class TestY8950RealRip(unittest.TestCase):
    def test_ginga_ninkyouden(self):
        root = _sample_root()
        path = root / "03v - Credit Inserted (with voice).vgm"
        if not path.is_file():
            for cand in root.rglob("03v - Credit Inserted (with voice).vgm"):
                path = cand
                break
        if not path.is_file():
            self.skipTest("rip missing")
        src = load_vgm(path)
        song = analyze(src)
        self.assertTrue(song.y8950_b_samples)
        self.assertEqual([s.kind for s in song.fm_systems], ["y8950"])
        fur = TMP / "ginga.fur"
        fur.write_bytes(write_fur(song, compress=True))
        out = TMP / "ginga.export.vgm"
        export_vgm(fur, out)
        exp = load_vgm(out)
        self.assertEqual(exp.y8950_clock, src.y8950_clock)
        starts = [
            w.sample for w in exp.writes
            if w.chip == "y8950" and (w.reg & 0xFF) == 0x07
            and (w.val & 0x80) and not (w.val & 0x01)
        ]
        self.assertTrue(starts)


if __name__ == "__main__":
    unittest.main()
