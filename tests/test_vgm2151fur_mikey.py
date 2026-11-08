"""Atari Lynx Mikey VGM parsing and Furnace conversion."""

from __future__ import annotations

import math
import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import parse_fur
from vgm2151fur.furwrite import write_fur
from vgm2151fur.mikey import (
    MIKEY_DEFAULT_CLOCK,
    collect_mikey,
    mikey_duty,
    mikey_lfsr,
    mikey_period,
)
from vgm2151fur.vgm import ChipWrite, VgmFile, load_vgm


def _write(sample: int, reg: int, value: int) -> ChipWrite:
    return ChipWrite(sample, "mikey", 0, reg, value & 0xFF)


def _vgm(writes: list[ChipWrite], total: int = 44_100) -> VgmFile:
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x172,
        total_samples=total, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        writes=writes, mikey_clock=MIKEY_DEFAULT_CLOCK,
    )


def _tone_writes(
    volume: int = 90, backup: int = 230, control: int = 0x39,
    feedback: int = 0x48, *, off: int | None = 8820,
) -> list[ChipWrite]:
    return [
        _write(0, 0x20, volume),
        _write(0, 0x21, feedback),
        _write(0, 0x23, 0),
        _write(0, 0x24, backup),
        _write(0, 0x25, control),
        _write(0, 0x27, 0),
        *([_write(off, 0x20, 0)] if off is not None else []),
    ]


class TestMikeyHelpers(unittest.TestCase):
    def test_duty_mask(self):
        self.assertEqual(mikey_duty(0x48, 0x39), 0x288)
        self.assertEqual(mikey_duty(0, 0), 0)

    def test_lfsr_seed(self):
        self.assertEqual(mikey_lfsr(0x34, 0xA0), 0xA34)

    def test_period(self):
        self.assertEqual(mikey_period(230, 0x39), 462)


class TestMikeyVgm(unittest.TestCase):
    def test_header_clock_and_command(self):
        buf = bytearray(0x100)
        buf[:4] = b"Vgm "
        struct.pack_into("<I", buf, 0x04, 0x100 + 4 + 5)
        struct.pack_into("<I", buf, 0x08, 0x172)
        struct.pack_into("<I", buf, 0x18, 44100)
        struct.pack_into("<I", buf, 0x34, 0xCC)
        struct.pack_into("<I", buf, 0xE4, MIKEY_DEFAULT_CLOCK)
        buf[0x100:0x105] = bytes((0x40, 0x20, 0x5A, 0x66, 0x00))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mikey.vgm"
            path.write_bytes(buf)
            vgm = load_vgm(path)
        self.assertEqual(vgm.mikey_clock, MIKEY_DEFAULT_CLOCK)
        self.assertEqual(vgm.writes[0].chip, "mikey")
        self.assertEqual(vgm.writes[0].reg, 0x20)
        self.assertEqual(vgm.writes[0].val, 0x5A)


class TestMikeyCollector(unittest.TestCase):
    def test_lfsr_note(self):
        hits, offs, samples, warnings = collect_mikey(_vgm(_tone_writes()))
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(samples), 0)
        self.assertEqual(warnings, [])
        hit = hits[0]
        self.assertEqual(hit.value, 462)
        self.assertEqual(hit.vol, 90)
        self.assertEqual(hit.duty, 0x288)
        self.assertEqual(hit.pan, 0xFF)
        self.assertEqual(hit.off_sample, 8820)

    def test_volume_and_pitch_motion(self):
        writes = _tone_writes(off=None)
        writes += [
            _write(882, 0x20, 78),
            _write(1764, 0x24, 162),
        ]
        hits, _offs, _samples, _warnings = collect_mikey(_vgm(writes))
        hit = hits[0]
        self.assertEqual(hit.vol_pts[0], (882, 78))
        self.assertEqual(hit.freq_pts[0], (1764, 326))

    def test_full_setup_before_volume_splits_note(self):
        writes = [
            _write(0, 0x21, 0x84),
            _write(0, 0x24, 230),
            _write(0, 0x25, 0x38),
            _write(5, 0x20, 40),
            _write(5000, 0x21, 7),
            _write(5000, 0x24, 244),
            _write(5000, 0x25, 0x19),
            _write(5005, 0x20, 48),
            _write(10000, 0x20, 0),
        ]
        hits, _offs, _samples, _warnings = collect_mikey(
            _vgm(writes, total=12000),
        )
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].off_sample, 5005)
        self.assertEqual(hits[1].sample_time, 5005)
        self.assertEqual(hits[0].duty, 0x304)
        self.assertEqual(hits[1].duty, 7)

    def test_attack_settle_updates_initial_duty(self):
        writes = [
            _write(0, 0x20, 40),
            _write(5, 0x21, 7),
            _write(5, 0x25, 0x19),
            _write(9000, 0x20, 0),
        ]
        hits, _offs, _samples, _warnings = collect_mikey(
            _vgm(writes, total=10000),
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].duty, 7)
        self.assertEqual(hits[0].duty_pts, [])

    def test_pan_register(self):
        writes = _tone_writes(off=None)
        writes += [
            _write(0, 0x40, 0x3C),  # left 3, right 12
            _write(0, 0x44, 0x00),  # pan bits clear: full scale
            _write(0, 0x50, 0x00),
        ]
        hits, _offs, _samples, _warnings = collect_mikey(_vgm(writes))
        self.assertEqual(hits[0].pan, 0xFF)

    def test_dac_stream(self):
        writes = []
        for i in range(1900):
            value = int(64 + 60 * math.sin(i / 20.0))
            writes.append(_write(i * 6, 0x22, value & 0xFF))
        _hits, _offs, samples, warnings = collect_mikey(
            _vgm(writes, total=44100),
        )
        self.assertTrue(samples)
        self.assertEqual(warnings, [])
        self.assertEqual(samples[0].rate, 7350)
        self.assertEqual(len(samples[0].data), 1837)


class TestMikeyModule(unittest.TestCase):
    def test_tone_module(self):
        song = analyze(_vgm(_tone_writes()))
        self.assertEqual(song.mikey_ids, [0])
        self.assertEqual(song.mikey_count, 1)
        self.assertTrue(song.mikey_ins)
        ins = song.mikey_ins[0]
        self.assertEqual(ins.duty, (0x288,))
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [0xA8])
        self.assertEqual(mod.n_ch, 4)
        notes = [
            row.note
            for (ch, _pat), rows in mod.patterns.items()
            if ch == 0
            for row in rows if row.note >= 0
        ]
        self.assertEqual(notes[0], 109)

    def test_dac_module(self):
        writes = [
            _write(i * 6, 0x22, (i * 17) & 0x7F)
            for i in range(240)
        ]
        song = analyze(_vgm(writes, total=8820))
        self.assertTrue(song.mikey_samples)
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [0xA8])
        self.assertEqual(mod.n_ch, 4)
        self.assertTrue(mod.samples)
        self.assertEqual(mod.samples[0].depth, 8)
        self.assertEqual(mod.samples[0].c4_rate, 7350)


if __name__ == "__main__":
    unittest.main()
