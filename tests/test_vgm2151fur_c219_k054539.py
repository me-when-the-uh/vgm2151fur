"""C219 and K054539 parsing, collection and .fur emission."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.c219 import (
    C219Hit,
    C219Sample,
    _c219_furnace_addresses,
    c219_layout_warnings,
    c219_rate,
    collect_c219,
    layout_c219_samples,
)
from vgm2151fur.furio import parse_fur
from vgm2151fur.furwrite import (
    CHIP_C219,
    CHIP_GA20,
    CHIP_MSM5232,
    CHIP_PCM_DAC,
    INS_C219,
    c219_flag_text,
    write_fur,
)
from vgm2151fur.ga20 import collect_ga20
from vgm2151fur.msm5205 import collect_msm5205
from vgm2151fur.msm5232 import collect_msm5232
from vgm2151fur.k054539 import (
    FORMAT_8BIT,
    FORMAT_16BIT,
    FORMAT_DPCM,
    K054539Sample,
    collect_k054539,
    k054539_pan_fx,
    k054539_volume_fx,
)
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile, load_vgm


def _write(t: int, chip: str, reg: int, val: int, cid: int = 0) -> ChipWrite:
    return ChipWrite(t, chip, cid, reg, val)


def _vgm(writes, *, total: int = 44_100, roms=(), **kw) -> VgmFile:
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x171,
        total_samples=total, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        writes=list(writes), roms=list(roms), **kw,
    )


def _raw_vgm(body: bytes, *, clock_off: int, clock: int,
             version: int = 0x171, extra: bytes = b"") -> bytes:
    buf = bytearray(0x100 + len(body) + 1)
    buf[:4] = b"Vgm "
    struct.pack_into("<I", buf, 0x08, version)
    struct.pack_into("<I", buf, 0x18, 44100)
    struct.pack_into("<I", buf, 0x34, 0x100 - 0x34)
    struct.pack_into("<I", buf, clock_off, clock)
    if extra:
        buf[0x95] = extra[0]
    buf[0x100:0x100 + len(body)] = body
    buf[0x100 + len(body)] = 0x66
    struct.pack_into("<I", buf, 0x04, len(buf) - 4)
    return bytes(buf)


def _pair_swap(data: bytes) -> bytes:
    out = bytearray(len(data) & ~1)
    for i in range(0, len(out), 2):
        out[i] = data[i + 1]
        out[i + 1] = data[i]
    return bytes(out)


def _c219_writes(*, mode: int = 0x90, compressed: bool = False,
                 noise: bool = False) -> list[ChipWrite]:
    body = bytes((1, 2, 3, 4, 5, 6, 7, 8))
    if compressed:
        body = bytes((0x00, 0x12, 0x34, 0x56))
    rom = [RomSlice(
        0, len(body), 0, _pair_swap(body), "c219",
    )]
    writes = [
        _write(0, "c219", 0x1F7, 0),      # bank 0 for voices 0-3
        _write(10, "c219", 0x06, 0x00),
        _write(10, "c219", 0x07, 0x00),   # start word 0
        _write(10, "c219", 0x08, 0x00),
        _write(10, "c219", 0x09, 0x02 if compressed else 0x04),
        _write(10, "c219", 0x0A, 0x00),
        _write(10, "c219", 0x0B, 0x01 if compressed else 0x02),
        _write(10, "c219", 0x02, 0xFF),
        _write(10, "c219", 0x03, 0xFF),   # freq 0xFFFF
        _write(10, "c219", 0x00, 0xC8),
        _write(10, "c219", 0x01, 0x64),   # R 200, L 100
        _write(20, "c219", 0x05, mode | (0x01 if compressed else 0)
               | (0x04 if noise else 0)),
        _write(900, "c219", 0x05, 0x10),  # key off
    ]
    # `rom` is returned separately because tests need the same payload.
    return writes, rom


class TestC219(unittest.TestCase):
    def test_c219_parse_and_rom(self):
        body = bytes((0xD4, 0x00, 0x06, 0x80, 0xD4, 0x00, 0x05, 0x90))
        payload = (0x100).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"\x01\x02"
        raw = _raw_vgm(
            bytes((0x67, 0x66, 0x8D)) + len(payload).to_bytes(4, "little")
            + payload + body,
            clock_off=0xA8, clock=25_401_600,
        )
        # Type byte 2 selects C219 on the shared 0xD4 command.
        raw = raw[:0x96] + b"\x02" + raw[0x97:]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c219.vgm"
            p.write_bytes(raw)
            v = load_vgm(p)
        self.assertEqual(v.c140_type, 2)
        self.assertEqual(v.c140_clock, 25_401_600)
        self.assertEqual([w.chip for w in v.writes], ["c219", "c219"])
        self.assertEqual([r.kind for r in v.roms], ["c219"])

    def test_c219_sample_and_note(self):
        writes, rom = _c219_writes()
        song = _vgm(writes, total=1000, roms=rom,
                    c140_clock=25_401_600, c140_type=2)
        samples, offs, _warn = collect_c219(song)
        self.assertEqual(len(samples), 1)
        smp = samples[0]
        self.assertEqual(smp.data, bytes((1, 2, 3, 4, 5, 6, 7, 8)))
        self.assertEqual(smp.loop_start, 4)
        self.assertEqual(smp.loop_end, 8)
        self.assertEqual(smp.source_bank, 0)
        self.assertEqual(len(offs), 1)
        self.assertAlmostEqual(
            smp.c4_rate(25_401_600),
            c219_rate(25_401_600, 0xFFFF) * 0.998051,
            delta=2,
        )

    def test_c219_layout_keeps_real_bodies_only(self):
        first = C219Sample(start=0, data=b"\x01\x02", source_bank=2, depth=8)
        second = C219Sample(start=10, data=b"\x03\x04", source_bank=1, depth=8)
        laid = layout_c219_samples([first, second])
        self.assertEqual([s.source_bank for s in laid], [1, 2])
        self.assertEqual(len(laid), 2)
        # The first real body carries the trailing silence that pushes the
        # second source bank to Furnace bank 1; no extra sample is added.
        self.assertGreater(len(laid[0].data), len(first.data))
        addresses, _mem = _c219_furnace_addresses(laid)
        self.assertEqual([a[0] for a in addresses], [0, 1])

    def test_c219_odd_gap_does_not_split_source_bank(self):
        # An odd extended donor used to end exactly on the 128 KB boundary,
        # so Furnace moved the donor itself to the next bank and split the
        # source group across two banks.
        donor = C219Sample(
            start=0, data=b"\x01\x02", source_bank=3, depth=8,
            source_length=2,
        )
        looping = C219Sample(
            start=10, data=b"\x03\x04" * 300, source_bank=3, depth=8,
            loop_start=2, loop_end=600, source_length=600,
        )
        other = C219Sample(
            start=20, data=b"\x05\x06", source_bank=2, depth=8,
            source_length=2,
        )
        laid = layout_c219_samples([donor, looping, other])
        addresses, _mem = _c219_furnace_addresses(laid)
        by_source: dict[int, set[int]] = {}
        for smp, address in zip(laid, addresses):
            by_source.setdefault(smp.source_bank, set()).add(address[0])
        self.assertEqual(by_source[3], {0})
        self.assertEqual(by_source[2], {1})
        self.assertFalse(any(
            "spans Furnace banks" in warning
            for warning in c219_layout_warnings(laid)
        ))

    def test_c219_group_bank_switch_is_reported(self):
        def hit(voice: int, when: int) -> C219Hit:
            return C219Hit(
                sample_time=when, chip_id=0, voice=voice,
                start=0, end=1, loop=0, loop_on=False, bank=0,
                freq=0x100, vol_l=255, vol_r=255, off_sample=when + 500,
            )

        low = C219Sample(
            start=0, data=b"\x01\x02", source_bank=1, depth=8,
            source_length=2,
        )
        high = C219Sample(
            start=10, data=b"\x03\x04", source_bank=2, depth=8,
            source_length=2,
        )
        low.hits.append(hit(0, 0))
        high.hits.append(hit(1, 100))
        warnings = c219_layout_warnings(layout_c219_samples([low, high]))
        self.assertTrue(any(
            "voice group 0 switches" in warning for warning in warnings
        ), warnings)

    def test_c219_compressed_and_noise(self):
        writes, rom = _c219_writes(mode=0x80, compressed=True, noise=True)
        song = _vgm(writes, total=1000, roms=rom,
                    c140_clock=25_401_600, c140_type=2)
        samples, _offs, _warn = collect_c219(song)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].depth, 8)  # noise ignores the ROM body
        self.assertTrue(samples[0].noise)
        writes, rom = _c219_writes(mode=0x80, compressed=True)
        song = _vgm(writes, total=1000, roms=rom,
                    c140_clock=25_401_600, c140_type=2)
        samples, _offs, _warn = collect_c219(song)
        self.assertEqual(samples[0].depth, 8)
        self.assertEqual(samples[0].data, bytes((0, 2, 20, 54)))

    def test_c219_fur(self):
        writes, rom = _c219_writes(mode=0x80 | 0x40 | 0x08)
        song = analyze(_vgm(writes, total=1000, roms=rom,
                           c140_clock=25_401_600, c140_type=2))
        self.assertEqual(len(song.c219_samples), 1)
        self.assertEqual(song.c219_count, 1)
        mod = parse_fur(write_fur(song))
        self.assertEqual(mod.chips, [CHIP_C219])
        self.assertEqual(mod.n_ch, 16)
        self.assertEqual(mod.master_vol, 1.0)
        self.assertEqual(len(mod.samples), 1)
        self.assertEqual(mod.samples[0].depth, 8)
        self.assertEqual(mod.ins_sample[0], 0)
        notes = [
            row for (_ch, _pat), rows in mod.patterns.items() for row in rows
            if row.note not in (-1, 180)
        ]
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            next(v for fx, v in notes[0].fx if fx == 0x12), 0x11,
        )

    def test_c219_flag_text(self):
        flags = c219_flag_text(25_401_600)
        self.assertIn("customClock=100000000", flags)


class TestK054539(unittest.TestCase):
    def _writes(self, fmt: int, *, loop: bool = False) -> tuple:
        if fmt == FORMAT_8BIT:
            body = bytes((0x12, 0x34, 0x56, 0x78, 0x80, 0xAA))
            start = 0
            end_marker = 4
        elif fmt == FORMAT_16BIT:
            body = struct.pack("<hhhh", 0x1234, -0x1234, 0x2345, -0x2345) + b"\x00\x80"
            start = 0
            end_marker = 8
        else:
            body = bytes((0x12, 0x34, 0x88, 0xAA))
            start = 0
            end_marker = 2
        rom = [RomSlice(0, len(body), 0, body, "k054539")]
        base = 0
        writes = [
            _write(0, "k054539", 0x22F, 0x01),  # PCM enable
            _write(0, "k054539", 0x200, (fmt << 2) | (0x01 if loop else 0)),
            _write(0, "k054539", 0x201, 0x01 if loop else 0),
            _write(0, "k054539", base + 0x00, 0x00),
            _write(0, "k054539", base + 0x01, 0x40),
            _write(0, "k054539", base + 0x02, 0x00),  # delta 0x4000
            _write(0, "k054539", base + 0x03, 0x00),
            _write(0, "k054539", base + 0x04, 0x00),
            _write(0, "k054539", base + 0x05, 0x18),
            _write(0, "k054539", base + 0x08, 0x00),
            _write(0, "k054539", base + 0x09, 0x00),
            _write(0, "k054539", base + 0x0A, 0x00),
            _write(0, "k054539", base + 0x0C, 0x00),
            _write(0, "k054539", base + 0x0D, 0x00),
            _write(0, "k054539", base + 0x0E, 0x00),
            _write(10, "k054539", 0x214, 0x01),
            _write(900, "k054539", 0x215, 0x01),
        ]
        return writes, rom

    def test_k054539_parse(self):
        payload = (0x1000).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"\x12\x34"
        body = (
            bytes((0x67, 0x66, 0x8C))
            + len(payload).to_bytes(4, "little")
            + payload
            + bytes((0xD3, 0x02, 0x14, 0x01))
        )
        raw = _raw_vgm(body, clock_off=0xA0, clock=18_432_000)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "k054539.vgm"
            p.write_bytes(raw)
            v = load_vgm(p)
        self.assertEqual(v.k054539_clock, 18_432_000)
        self.assertEqual(v.writes[0].chip, "k054539")
        self.assertEqual(v.writes[0].reg, 0x214)
        self.assertEqual(v.roms[0].kind, "k054539")

    def test_k054539_bodies(self):
        for fmt, depth, frames in (
            (FORMAT_8BIT, 8, 4),
            (FORMAT_16BIT, 16, 4),
            (FORMAT_DPCM, 16, 4),
        ):
            writes, rom = self._writes(fmt)
            song = _vgm(writes, total=1000, roms=rom,
                        k054539_clock=18_432_000)
            samples, offs, _warn = collect_k054539(song)
            self.assertEqual(len(samples), 1, fmt)
            smp = samples[0]
            self.assertEqual(smp.depth, depth, fmt)
            self.assertEqual(smp.length, frames, fmt)
            self.assertEqual(len(offs), 1)
            if fmt == FORMAT_DPCM:
                self.assertEqual(
                    smp.data,
                    struct.pack("<hhhh", 0x200, 0x300, 0xB00, 0xF00),
                )

    def test_k054539_immediate_keyoff_is_silent(self):
        # Some logs begin with a full key-on/key-off mask pair to reset every
        # voice. Both writes land on the same VGM sample, so no audio plays.
        writes, rom = self._writes(FORMAT_8BIT)
        writes[-1] = _write(10, "k054539", 0x215, 0x01)
        song = _vgm(writes, total=1000, roms=rom,
                    k054539_clock=18_432_000)
        samples, _offs, _warn = collect_k054539(song)
        self.assertEqual(samples, [])
        analysed = analyze(song)
        self.assertEqual(analysed.k054539_count, 0)
        self.assertEqual(analysed.k054539_samples, [])

    def test_k054539_loop_and_value_maps(self):
        writes, rom = self._writes(FORMAT_8BIT, loop=True)
        song = _vgm(writes, total=1000, roms=rom,
                    k054539_clock=18_432_000)
        samples, _offs, _warn = collect_k054539(song)
        smp = samples[0]
        self.assertEqual((smp.loop_start, smp.loop_end), (0, 4))
        self.assertEqual(k054539_volume_fx(0), 255)
        self.assertEqual(k054539_volume_fx(0x40), 4)
        self.assertEqual(k054539_pan_fx(0x18), 0xBB)
        self.assertEqual(k054539_pan_fx(0x11), 0x0F)
        self.assertEqual(k054539_pan_fx(0x1F), 0xF0)

    def test_k054539_fur(self):
        writes, rom = self._writes(FORMAT_8BIT)
        song = analyze(_vgm(writes, total=1000, roms=rom,
                           k054539_clock=18_432_000))
        self.assertEqual(song.k054539_count, 1)
        mod = parse_fur(write_fur(song))
        self.assertEqual(
            mod.chips.count(CHIP_PCM_DAC), 8,
        )
        self.assertEqual(mod.n_ch, 8)  # one DAC system per hardware voice
        self.assertEqual(mod.master_vol, 1.0)
        self.assertEqual(len(mod.samples), 1)
        self.assertEqual(mod.samples[0].depth, 8)
        notes = [
            row for (_ch, _pat), rows in mod.patterns.items() for row in rows
            if row.note not in (-1, 180)
        ]
        self.assertEqual(len(notes), 1)
        self.assertTrue(any(fx == 0x08 for fx, _v in notes[0].fx))


class TestGa20Msm5205Msm5232(unittest.TestCase):
    def test_ga20_body_and_fur(self):
        rom = [RomSlice(
            0, 4, 0, bytes((0x80, 0x90, 0xA0, 0x00)), "ga20",
        )]
        writes = [
            _write(0, "ga20", 0x06, 0x00),
            _write(0, "ga20", 0x07, 0x00),
            _write(0, "ga20", 0x02, 0x00),
            _write(0, "ga20", 0x03, 0x00),
            _write(0, "ga20", 0x04, 0x80),
            _write(0, "ga20", 0x05, 0xC0),
            _write(10, "ga20", 0x06, 0x02),   # key on
            _write(900, "ga20", 0x06, 0x00),  # key off
        ]
        song = _vgm(writes, total=1000, roms=rom, ga20_clock=3_579_545)
        samples, offs, _warn = collect_ga20(song)
        self.assertEqual(len(samples), 1)
        # ROM 0x80/0x90/0xA0 becomes signed 0x00/0x10/0x20; zero terminates.
        self.assertEqual(samples[0].data, bytes((0x00, 0x10, 0x20)))
        self.assertEqual(len(offs), 1)

        analysed = analyze(song)
        self.assertEqual(analysed.ga20_count, 1)
        mod = parse_fur(write_fur(analysed))
        self.assertIn(CHIP_GA20, mod.chips)
        self.assertEqual(len(mod.samples), 1)
        self.assertEqual(mod.samples[0].depth, 8)
        notes = [
            row for (_ch, _pat), rows in mod.patterns.items() for row in rows
            if row.note not in (-1, 180)
        ]
        self.assertEqual(len(notes), 1)

    def test_msm5232_note_and_group_fx(self):
        writes = [
            _write(0, "msm5232", 0x0C, 0x0F),
            _write(0, "msm5232", 0x08, 0x03),
            _write(0, "msm5232", 0x0A, 0x05),
            _write(10, "msm5232", 0x00, 0xA1),  # key on pitch 0x21
            _write(900, "msm5232", 0x00, 0x00),  # key off
        ]
        writes.append(_write(500, "msm5232", 0x1E, 0x08))
        song = _vgm(writes, total=1000, msm5232_clock=2_000_000)
        hits, offs, fx, vols, _warn = collect_msm5232(song)
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(vols), 1)
        self.assertEqual(vols[0].value, 8)
        self.assertEqual(hits[0].pitch, 0x21)
        self.assertFalse(hits[0].noise)
        self.assertEqual(len(offs), 1)
        self.assertTrue(any(e.code == 0x12 and e.value == 3 for e in fx))
        self.assertTrue(any(e.code == 0x13 and e.value == 5 for e in fx))

        analysed = analyze(song)
        self.assertEqual(analysed.msm5232_count, 1)
        mod = parse_fur(write_fur(analysed))
        self.assertIn(CHIP_MSM5232, mod.chips)
        self.assertEqual(mod.n_ch, 8)
        notes = [
            row for (_ch, _pat), rows in mod.patterns.items() for row in rows
            if row.note not in (-1, 180)
        ]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].note, 72 + 0x21)

    def test_msm5205_streams_to_pcm_dac(self):
        writes = [
            _write(0, "msm5205", 0, 1),   # reset
            _write(0, "msm5205", 4, 2),   # S2 prescaler
            _write(0, "msm5205", 5, 1),   # 4-bit
        ]
        for i in range(40):
            writes.append(_write(i + 1, "msm5205", 1, i & 0x0F))
        writes.append(_write(100, "msm5205", 0, 0))  # release
        song = _vgm(
            writes, total=2000, msm5205_clock=384_000,
            msm5205_flags=0x06,
        )
        samples, _warn = collect_msm5205(song)
        self.assertTrue(samples)
        self.assertTrue(all(s.rate == 44100 for s in samples))
        mod = parse_fur(write_fur(analyze(song)))
        self.assertIn(CHIP_PCM_DAC, mod.chips)
        self.assertEqual(mod.n_ch, 1)
        self.assertTrue(all(x.depth == 16 for x in mod.samples))


if __name__ == "__main__":
    unittest.main()
