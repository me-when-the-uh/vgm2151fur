"""Yamaha YMW258-F (MultiPCM) parsing, collector and module emission."""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import parse_fur
from vgm2151fur.furwrite import CHIP_MULTIPCM, write_fur
from vgm2151fur.multipcm import (
    MULTIPCM_VOICES,
    collect_multipcm,
    played_rate,
)
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile


def _rom(depth: int = 8) -> bytes:
    """One sample header at index 0 and a 256-frame body at 0x100."""
    rom = bytearray(0x1000)
    start = 0x100
    if depth == 12:
        start |= 1 << 22  # format bit 2 -> 12-bit
    rom[0:3] = start.to_bytes(3, "big")
    rom[3:5] = (0x20).to_bytes(2, "big")
    rom[5:7] = (0x10000 - 256).to_bytes(2, "big")
    rom[7] = 0x00  # LFO / VIB
    rom[8] = 0xF0  # AR 15, D1R 0
    rom[9] = 0x00  # DL 0, D2R 0
    rom[10] = 0x0F  # RC 0, RR 15
    rom[11] = 0x00  # AM 0
    body = bytes((i & 0xFF) for i in range(256))
    if depth == 12:
        # Pack 256 12-bit frames; the exact values do not matter here.
        packed = bytearray()
        for i in range(0, 256, 2):
            a = (i * 13) & 0xFFF
            b = ((i + 1) * 13) & 0xFFF
            packed += bytes((a >> 4, ((a & 0xF) << 4) | (b >> 8), b & 0xFF))
        body = bytes(packed)
    rom[0x100:0x100 + len(body)] = body
    return bytes(rom)


def _vgm(writes, **kw) -> VgmFile:
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=44100, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        writes=list(writes), multipcm_clock=8_000_000, **kw,
    )


def _write(sample: int, reg: int, val: int) -> ChipWrite:
    return ChipWrite(sample, "multipcm", 0, reg, val)


def _note(reg5: int = 0x00, pan: int = 0x00) -> list[ChipWrite]:
    # Select channel 0, then set registers 3,2,1,5,0,4.
    return [
        _write(0, 1, 0),
        _write(0, 2, 3), _write(0, 0, 0x10),  # octave 1, fn high 0
        _write(0, 2, 2), _write(0, 0, 0x00),  # fn low 0, sample high bit
        _write(0, 2, 1), _write(0, 0, 0x00),  # sample index 0
        _write(0, 2, 5), _write(0, 0, reg5),  # TL
        _write(0, 2, 0), _write(0, 0, pan),   # pan
        _write(0, 2, 4), _write(0, 0, 0x80),  # key on
    ]


class TestMultipcmCollector(unittest.TestCase):
    def test_eight_bit_sample_and_note(self):
        rom = _rom()
        v = _vgm(_note() + [
            _write(1000, 2, 4), _write(1000, 0, 0x00),  # key off
        ], roms=[RomSlice(0, len(rom), 0, rom, "multipcm")])
        samples, warn = collect_multipcm(v)
        self.assertEqual(warn, [])
        self.assertEqual(len(samples), 1)
        s = samples[0]
        self.assertEqual((s.index, s.depth, s.frames), (0, 8, 256))
        self.assertEqual(s.data, rom[0x100:0x200])
        self.assertEqual((s.loop_start, s.loop_end), (0x20, 256))
        self.assertEqual(s.ar, 15)
        self.assertEqual(s.rr, 15)
        self.assertEqual(len(s.hits), 1)
        hit = s.hits[0]
        self.assertEqual((hit.octave, hit.fnum, hit.pan), (1, 0, 0))
        self.assertEqual(hit.off_sample, 1000)

    def test_twelve_bit_sample(self):
        rom = _rom(12)
        v = _vgm(_note(), roms=[RomSlice(0, len(rom), 0, rom, "multipcm")])
        samples, _warn = collect_multipcm(v)
        self.assertEqual(samples[0].depth, 12)
        self.assertEqual(samples[0].frames, 256)
        self.assertEqual(len(samples[0].data), (256 * 3 + 1) // 2)

    def test_sega_bank_mapping(self):
        rom = _rom()
        # Sample start 0x100000 selects the high bank; a 1 MB bank 1 maps it
        # back to 0x100000, which is inside a 4 MB image.
        hdr = bytearray(0x101000)
        hdr[0:len(rom)] = rom
        hdr[0:3] = (0x100100).to_bytes(3, "big")
        body = rom[0x100:0x200]
        hdr[0x100100:0x100200] = body
        v = _vgm(
            [ChipWrite(0, "multipcm_bank", 0, 3, 16)]
            + _note(),
            roms=[RomSlice(0, len(hdr), 0, bytes(hdr), "multipcm")],
        )
        samples, _warn = collect_multipcm(v)
        self.assertEqual(samples[0].start, 0x100100)

    def test_rate_pitch(self):
        # octave 1, fnum 0 is the chip's output rate (clock / 180).
        self.assertAlmostEqual(played_rate(8_000_000, 1, 0), 8_000_000 / 180)
        self.assertAlmostEqual(
            played_rate(8_000_000, 2, 0),
            2 * 8_000_000 / 180,
        )


class TestMultipcmModule(unittest.TestCase):
    def test_layout_and_instrument_type(self):
        rom = _rom()
        v = _vgm(_note(), roms=[RomSlice(0, len(rom), 0, rom, "multipcm")])
        song = analyze(v)
        self.assertEqual(song.multipcm_ids, [0])
        self.assertEqual(song.multipcm_count, 1)
        self.assertTrue(any(e.ch >= 0 for e in song.events))
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [CHIP_MULTIPCM])
        self.assertEqual(mod.n_ch, MULTIPCM_VOICES)

    def test_dual_header_keeps_silent_second_chip(self):
        rom = _rom()
        v = _vgm(
            _note(),
            roms=[RomSlice(0, len(rom), 0, rom, "multipcm")],
            multipcm_dual=True,
        )
        song = analyze(v)
        self.assertEqual(song.multipcm_ids, [0, 1])
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [CHIP_MULTIPCM, CHIP_MULTIPCM])
        self.assertEqual(mod.n_ch, 2 * MULTIPCM_VOICES)

    def test_key_off_stops_early(self):
        rom = _rom()
        writes = _note() + [
            _write(1000, 2, 4), _write(1000, 0, 0x00),
        ]
        v = _vgm(writes, roms=[RomSlice(0, len(rom), 0, rom, "multipcm")])
        song = analyze(v)
        offs = [e for e in song.events if not e.on and e.pcm]
        self.assertEqual([e.sample for e in offs], [1000])
        data = write_fur(song, include_fm=False)
        mod = parse_fur(data)
        notes = [
            row.note for rows in mod.patterns.values() for row in rows
        ]
        self.assertIn(180, notes)

    def test_pan_volume_and_instrument_headers(self):
        rom = _rom()
        # TL 0 -> volume 127; pan 0 -> both channels full.
        v = _vgm(_note(reg5=0x00, pan=0x00),
                 roms=[RomSlice(0, len(rom), 0, rom, "multipcm")])
        song = analyze(v)
        ev = next(e for e in song.events if e.on)
        self.assertEqual(ev.vol, 127)
        self.assertEqual(ev.pan, 0xFF)
        data = write_fur(song, include_fm=False)
        # The MultiPCM instrument carries its ADSR in an MP feature.
        self.assertIn(b"MP", data)
        from vgm2151fur.edit import read_chips_file
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.fur"
            p.write_bytes(data)
            chips = read_chips_file(p)
        self.assertTrue(all(c.volume <= 2.0 for c in chips))
        self.assertLessEqual(parse_fur(data).master_vol, 3.0)


if __name__ == "__main__":
    unittest.main()
