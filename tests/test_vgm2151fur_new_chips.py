"""FDS, WonderSwan and QSound parsing, analysis and conversion."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.fds import collect_fds
from vgm2151fur.furio import decompress_fur, parse_fur
from vgm2151fur.furwrite import write_fur
from vgm2151fur.qsound import (
    QSOUND_ADPCM_NIBBLE_RATE,
    QSOUND_OUT_RATE,
    collect_qsound,
    qsound_pan,
)
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile, load_vgm
from vgm2151fur.wonderswan import collect_wonderswan


def _write(t: int, chip: str, reg: int, val: int, cid: int = 0) -> ChipWrite:
    return ChipWrite(t, chip, cid, reg, val)


def _vgm(writes, *, total: int = 44_100, roms=(), **kw) -> VgmFile:
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x171,
        total_samples=total, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        writes=list(writes), roms=list(roms), **kw,
    )


def _info_sample_count(data: bytes) -> int:
    raw = decompress_fur(data)
    info = struct.unpack_from("<I", raw, 20)[0]
    return struct.unpack_from("<H", raw, info + 8 + 18)[0]


def _raw_vgm(body: bytes, *, clock_off: int = 0, clock: int = 0,
             version: int = 0x171) -> bytes:
    buf = bytearray(0x120)
    buf[:4] = b"Vgm "
    struct.pack_into("<I", buf, 0x08, version)
    struct.pack_into("<I", buf, 0x18, 44100)
    struct.pack_into("<I", buf, 0x34, 0x100 - 0x34)
    if clock_off:
        struct.pack_into("<I", buf, clock_off, clock)
    buf[0x100:0x100 + len(body)] = body
    buf[0x100 + len(body)] = 0x66
    struct.pack_into("<I", buf, 0x04, len(buf) - 4)
    return bytes(buf)


class TestParsing(unittest.TestCase):
    def test_fds_write_split(self):
        body = bytes((
            0xB4, 0x20, 0x80,  # $4080 volume
            0xB4, 0x40, 0x12,  # wave RAM
            0xB4, 0x00, 0x30,  # NES APU pulse
            0xB4, 0x3F, 0x02,  # FDS I/O enable
        ))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "fds.vgm"
            p.write_bytes(_raw_vgm(body, clock_off=0x84, clock=1_789_773))
            vgm = load_vgm(p)
        self.assertEqual(vgm.nes_clock, 1_789_773)
        fds = [w for w in vgm.writes if w.chip == "fds"]
        nes = [w for w in vgm.writes if w.chip == "nes"]
        self.assertEqual([w.reg for w in fds], [0x80, 0x40, 0x23])
        self.assertEqual([w.reg for w in nes], [0x00])

    def test_wonderswan_commands_and_clock(self):
        body = bytes((
            0xBC, 0x01, 0x40,
            0xC6, 0x00, 0x80, 0xAB,
        ))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ws.vgm"
            p.write_bytes(_raw_vgm(body, clock_off=0xC0, clock=3_072_000))
            vgm = load_vgm(p)
        self.assertEqual(vgm.wonderswan_clock, 3_072_000)
        self.assertEqual(len(vgm.writes), 2)
        self.assertEqual(vgm.writes[0].reg, 0x81)
        self.assertEqual(vgm.writes[1].reg, 0x10080)

    def test_qsound_command_is_big_endian_and_rom(self):
        payload = (0x10000).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"\x01\x02"
        body = (
            bytes((0x67, 0x66, 0x8F))
            + len(payload).to_bytes(4, "little")
            + payload
            + bytes((0xC4, 0x12, 0x34, 0x56))
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "qs.vgm"
            p.write_bytes(_raw_vgm(body, clock_off=0xB4, clock=60_000_000))
            vgm = load_vgm(p)
        self.assertEqual(vgm.qsound_clock, 60_000_000)
        self.assertEqual(vgm.writes[-1].reg, 0x56)
        self.assertEqual(vgm.writes[-1].val, 0x1234)
        self.assertEqual(vgm.roms[0].kind, "qsound")


class TestFds(unittest.TestCase):
    def _song(self) -> VgmFile:
        writes = []
        for i in range(64):
            writes.append(_write(0, "fds", 0x40 + i, i & 0x3F))
        writes += [
            _write(10, "fds", 0x80, 0x80 | 20),
            _write(20, "fds", 0x82, 0x65),
            _write(21, "fds", 0x83, 0x02),
            _write(100, "fds", 0x80, 0x80),
            _write(200, "fds", 0x82, 0x80),
            _write(201, "fds", 0x83, 0x03),
            _write(400, "fds", 0x80, 0x80),
        ]
        return _vgm(writes, total=500, nes_clock=1_789_773)

    def test_collector(self):
        hits, _ = collect_fds(self._song())
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].freq, 0x265)
        self.assertEqual(hits[0].volume, 20)
        self.assertEqual(hits[0].off_sample, 100)
        self.assertEqual(hits[1].freq, 0x380)
        self.assertEqual(hits[1].off_sample, 400)

    def test_high_byte_before_low_byte(self):
        writes = []
        for i in range(64):
            writes.append(_write(0, "fds", 0x40 + i, i & 0x3F))
        writes += [
            _write(10, "fds", 0x80, 0x80 | 20),
            _write(20, "fds", 0x83, 0x04),
            _write(20, "fds", 0x82, 0x44),
        ]
        hits, _ = collect_fds(_vgm(writes, total=100, nes_clock=1_789_773))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].freq, 0x444)

    def test_fast_tick_lattice(self):
        writes = []
        for i in range(64):
            writes.append(_write(0, "fds", 0x40 + i, (i * 3) & 0x3F))
        writes.append(_write(0, "fds", 0x80, 0x80 | 20))
        for i in range(100):
            t = i * 110
            freq = 500 + (i % 16) * 37
            writes.append(_write(t, "fds", 0x82, freq & 0xFF))
            writes.append(_write(t, "fds", 0x83, (freq >> 8) & 0x0F))
        song = _vgm(
            writes, total=100 * 110 + 1000, nes_clock=1_789_773,
        )
        s = analyze(song)
        self.assertEqual(s.speed, 1)
        self.assertGreater(s.hz, 350)
        self.assertEqual(s.fds_count, 100)
        mod = parse_fur(write_fur(s))
        notes = sum(
            1 for rows in mod.patterns.values() for r in rows
            if r.note not in (-1, 180)
        )
        self.assertEqual(notes, 100)

    def test_conversion(self):
        mod = parse_fur(write_fur(analyze(self._song())))
        self.assertEqual(mod.chips, [0x8A])
        self.assertEqual(mod.n_ch, 1)
        self.assertTrue(any(r.note == 180 for rows in mod.patterns.values()
                            for r in rows))


class TestWonderSwan(unittest.TestCase):
    def _wave_song(self) -> VgmFile:
        writes = []
        for i in range(16):
            writes.append(_write(0, "wonderswan", 0x10000 + i, (i << 4) | i))
        writes += [
            _write(10, "wonderswan", 0x8F, 0),
            _write(20, "wonderswan", 0x80, 0x00),
            _write(21, "wonderswan", 0x81, 0x04),
            _write(30, "wonderswan", 0x88, 0xC0),
            _write(40, "wonderswan", 0x90, 0x01),
            _write(200, "wonderswan", 0x88, 0x00),
        ]
        return _vgm(writes, total=300, wonderswan_clock=3_072_000)

    def test_wave_note_and_cut(self):
        hits, pcm, _ = collect_wonderswan(self._wave_song())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].period, 2048 - 0x400)
        self.assertEqual((hits[0].vol_l, hits[0].vol_r), (12, 0))
        self.assertEqual(hits[0].off_sample, 200)
        self.assertEqual(pcm, [])

    def test_pcm_pan_decodes_low_right_bits(self):
        writes = [_write(0, "wonderswan", 0x90, 0x22)]
        writes.append(_write(1, "wonderswan", 0x94, 0x0F))
        for i in range(20):
            writes.append(_write(2 + i * 4, "wonderswan", 0x89, 128 + i))
        writes.append(_write(200, "wonderswan", 0x90, 0x00))
        _hits, pcm, _ = collect_wonderswan(
            _vgm(writes, total=300, wonderswan_clock=3_072_000)
        )
        self.assertEqual(len(pcm), 1)
        self.assertEqual(pcm[0].hits[0].pan_l, 15)
        self.assertEqual(pcm[0].hits[0].pan_r, 15)

    def test_pcm_stream(self):
        writes = [_write(0, "wonderswan", 0x90, 0x22)]
        for i in range(40):
            writes.append(_write(1 + i * 4, "wonderswan", 0x89, 128 + (i % 5)))
        writes.append(_write(200, "wonderswan", 0x90, 0x00))
        hits, pcm, _ = collect_wonderswan(
            _vgm(writes, total=300, wonderswan_clock=3_072_000)
        )
        self.assertEqual(len(pcm), 1)
        self.assertGreater(len(pcm[0].data), 10)
        self.assertAlmostEqual(pcm[0].rate, 44100 / 4, delta=2)

    def test_conversion(self):
        mod = parse_fur(write_fur(analyze(self._wave_song())))
        self.assertEqual(mod.chips, [0x96])
        self.assertEqual(mod.n_ch, 4)

    def test_pcm_conversion_loads_sample(self):
        writes = [_write(0, "wonderswan", 0x90, 0x22)]
        for i in range(80):
            writes.append(_write(1 + i * 4, "wonderswan", 0x89, 128 + (i % 7)))
        writes.append(_write(400, "wonderswan", 0x90, 0x00))
        song = _vgm(writes, total=500, wonderswan_clock=3_072_000)
        data = write_fur(analyze(song))
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0x96])
        self.assertEqual(len(mod.samples), 1)
        self.assertEqual(_info_sample_count(data), 1)
        self.assertEqual(mod.samples[0].depth, 8)


class TestQSound(unittest.TestCase):
    def _rom(self) -> bytes:
        return bytes((i * 3) & 0xFF for i in range(0x1000))

    def _pcm_song(self) -> VgmFile:
        roms = [RomSlice(0, 0x1000, 0, self._rom(), "qsound")]
        writes = [
            _write(0, "qsound", 0x78, 0x8000),  # bank for voice 0
            _write(1, "qsound", 0x01, 0x0100),
            _write(2, "qsound", 0x05, 0x0200),
            _write(3, "qsound", 0x04, 0x40),
            _write(5, "qsound", 0x03, 0x8000),
            _write(6, "qsound", 0x02, 4096),
            _write(7, "qsound", 0x06, 1200),
            _write(8, "qsound", 0x80, 0x14D),
            _write(9, "qsound", 0xBA, 896),
            _write(100, "qsound", 0x06, 0),
        ]
        return _vgm(writes, total=200, roms=roms, qsound_clock=60_000_000)

    def test_pcm_hit_and_linear_pan(self):
        pcm, adpcm, samples, _ = collect_qsound(self._pcm_song())
        self.assertEqual(len(pcm), 1)
        self.assertEqual(adpcm, [])
        hit = pcm[0]
        self.assertEqual(hit.freq, 4096)
        self.assertEqual(hit.start, 0x0100)
        self.assertEqual(hit.end, 0x0200)
        self.assertEqual(samples[0].loop_start, 0x100 - 0x40)
        linear, surround = qsound_pan(0x14D)
        self.assertEqual(linear, 13)
        self.assertEqual(surround, 0)
        self.assertEqual(hit.off_sample, 100)

    def test_start_address_retriggers_while_enabled(self):
        roms = [RomSlice(0, 0x1000, 0, self._rom(), "qsound")]
        writes = [
            _write(0, "qsound", 0x78, 0x8000),
            _write(1, "qsound", 0x01, 0x0100),
            _write(2, "qsound", 0x05, 0x0200),
            _write(3, "qsound", 0x04, 0x40),
            _write(4, "qsound", 0x06, 1200),
            _write(5, "qsound", 0x02, 4096),
            _write(6, "qsound", 0x03, 0x8000),
            # Phase stays enabled; a start rewrite is the next key-on.
            _write(1000, "qsound", 0x01, 0x0100),
            _write(1001, "qsound", 0x06, 900),
            _write(1002, "qsound", 0x02, 4096),
        ]
        pcm, _adpcm, _samples, _ = collect_qsound(
            _vgm(writes, total=1200, roms=roms, qsound_clock=60_000_000)
        )
        self.assertEqual(len(pcm), 2)
        self.assertEqual(pcm[0].sample_time, 6)
        self.assertEqual(pcm[1].sample_time, 1000)

    def test_adpcm_hit(self):
        roms = [RomSlice(0, 0x10000, 0, self._rom(), "qsound")]
        writes = [
            _write(0, "qsound", 0xCC, 0x8000),  # ADPCM0 bank
            _write(1, "qsound", 0xCA, 0x0010),
            _write(2, "qsound", 0xCB, 0x0020),
            _write(3, "qsound", 0xD6, 0),
            _write(4, "qsound", 0xD6, 1),
            _write(5, "qsound", 0xCD, 16383),
        ]
        pcm, adpcm, samples, _ = collect_qsound(
            _vgm(writes, total=300, roms=roms, qsound_clock=60_000_000)
        )
        self.assertEqual(pcm, [])
        self.assertEqual(len(adpcm), 1)
        self.assertEqual(samples[0].kind, "adpcm")
        self.assertEqual(len(samples[0].data), 16)
        self.assertGreater(adpcm[0].off_sample, 0)

    def test_conversion_sample_depth_and_masks(self):
        pcm = self._pcm_song()
        writes = list(pcm.writes)
        # Add one ADPCM voice to the same chip.
        roms = list(pcm.roms)
        roms[0] = RomSlice(0, 0x10000, 0, self._rom(), "qsound")
        writes += [
            _write(0, "qsound", 0xCC, 0x8000),
            _write(1, "qsound", 0xCA, 0x0010),
            _write(2, "qsound", 0xCB, 0x0020),
            _write(3, "qsound", 0xD6, 0),
            _write(4, "qsound", 0xD6, 1),
            _write(5, "qsound", 0xCD, 16383),
        ]
        song = _vgm(writes, total=200, roms=roms, qsound_clock=60_000_000)
        data = write_fur(analyze(song))
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0xE0])
        self.assertEqual(mod.n_ch, 19)
        self.assertEqual(_info_sample_count(data), len(mod.samples))
        depths = {s.depth for s in mod.samples}
        self.assertIn(8, depths)
        self.assertIn(4, depths)
        for smp in mod.samples:
            masks = [
                int.from_bytes(smp.presence[i:i + 4], "little")
                for i in range(0, 16, 4)
            ]
            if smp.depth == 4:
                self.assertNotEqual(masks[1], 0)
                self.assertEqual(masks[0], 0)
            else:
                self.assertNotEqual(masks[0], 0)


if __name__ == "__main__":
    unittest.main()
