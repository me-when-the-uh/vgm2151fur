"""Yamaha YMZ280B (PCMD8): parsing, samples, loops and .fur emission."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.edit import read_chips
from vgm2151fur.furio import iter_blocks, master_volume, parse_fur
from vgm2151fur.furwrite import (
    CHIP_OPL2, CHIP_YMZ280B, INS_YMZ280B, SMP_DEPTH_8BIT, SMP_DEPTH_16BIT,
    SMP_DEPTH_YMZ_ADPCM, write_fur, ymz280b_flag_text,
)
from vgm2151fur.vgm import (
    ChipWrite, RomSlice, VgmFile, chips_in_head, load_vgm,
)
from vgm2151fur.ymz280b import (
    YMZ_DEPTH_ADPCM, YMZ_DEPTH_PCM8, YMZ_DEPTH_PCM16,
    collect_ymz280b, ymz280b_rate, ymz_pan_fx, ymz_pan_ratio,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_ymz280b"
TMP.mkdir(exist_ok=True)
CLOCK = 16934400
KUK = Path(r"D:\downloads\timetraxvgm\kuk\djtBMX_vgmmergetest.vgm")
Z66 = Path(r"D:\Downloads\timetraxvgm\kuk\z66_title-test.vgm")
STAGE4 = Path(r"D:\Downloads\timetraxvgm\kuk\07 Stage4.vgm")


def _vgm(writes, roms=(), **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x154,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=list(roms), writes=list(writes),
        ymz280b_clock=CLOCK, **kw,
    )


def _w(t, reg, val, chip=0):
    return ChipWrite(t, "ymz280b", chip, reg, val)


def _addr(t, reg_hi, reg_mid, reg_lo, value, chip=0):
    return [
        _w(t, reg_hi, (value >> 16) & 0xFF, chip),
        _w(t, reg_mid, (value >> 8) & 0xFF, chip),
        _w(t, reg_lo, value & 0xFF, chip),
    ]


def _enable(t=0, chip=0):
    return _w(t, 0xFF, 0x80, chip)


def _note(
    t, start=0, stop=200, *, fnum=0x10, vol=200, pan=8, mode=0x40,
    loop=False, loop_start=0, loop_end=0, chip=0, off=None,
    channel=0,
):
    base = channel * 4
    out = _addr(t, 0x20 + base, 0x40 + base, 0x60 + base, start, chip)
    out += _addr(t, 0x23 + base, 0x43 + base, 0x63 + base, stop, chip)
    if loop:
        out += _addr(t, 0x21 + base, 0x41 + base, 0x61 + base, loop_start, chip)
        out += _addr(t, 0x22 + base, 0x42 + base, 0x62 + base, loop_end, chip)
    out += [
        _w(t, base + 0x02, vol, chip),
        _w(t, base + 0x03, pan, chip),
        _w(t, base + 0x00, fnum & 0xFF, chip),
        _w(t, base + 0x01,
           0x80 | (0x10 if loop else 0) | (mode & 0x60) |
           ((fnum >> 8) & 1), chip),
    ]
    if off is not None:
        out.append(_w(off, base + 0x01, 0, chip))
    return out


def _body(n=256):
    return bytes((i * 37 + 11) & 0xFF for i in range(n))


def _raw_vgm(cmds: bytes, rom_blocks=(), clock=CLOCK, dual=False):
    """A VGM 1.54 file with YMZ header clock and 0x86 ROM blocks.

    ``rom_blocks`` is (chip_id, rom_size, start, data).  The data block size
    carries the chip id in bit 31, exactly as real dual-chip logs do.
    """
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x154).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    raw = clock | (0x40000000 if dual else 0)
    buf[0x68:0x6C] = raw.to_bytes(4, "little")
    body = bytearray()
    for chip_id, rom_size, start, data in rom_blocks:
        payload = (
            rom_size.to_bytes(4, "little")
            + start.to_bytes(4, "little")
            + data
        )
        size = len(payload) | ((chip_id & 1) << 31)
        body += bytes([0x67, 0x66, 0x86]) + size.to_bytes(4, "little")
        body += payload
    body += cmds + b"\x66"
    return bytes(buf) + bytes(body)


class TestYmz280bParsing(unittest.TestCase):
    def test_header_dual_command_and_rom(self):
        raw = _raw_vgm(
            b"\x5d\xff\xd0\x5d\x23\x00\x5d\x43\x00\x5d\x63\x20"
            b"\x5d\x00\x10\x5d\x01\xc0"
            b"\xad\xff\xd0\xad\x23\x00\xad\x43\x01\xad\x63\x00"
            b"\xad\x00\x20\xad\x01\xc0",
            [
                (0, 0x200, 0, bytes(range(0x20))),
                (1, 0x100, 0, bytes(range(0x10))),
            ],
            dual=True,
        )
        p = TMP / "parse.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.ymz280b_clock, CLOCK)
        self.assertTrue(v.ymz280b_dual)
        self.assertEqual(v.ymz280b_clock_2, 0)
        writes = [w for w in v.writes if w.chip == "ymz280b"]
        self.assertTrue(any(w.chip_id == 0 for w in writes))
        self.assertTrue(any(w.chip_id == 1 for w in writes))
        roms = [r for r in v.roms if r.kind == "ymz280b"]
        self.assertEqual({r.chip for r in roms}, {0, 1})
        self.assertIn("ymz280b", chips_in_head(raw))
        self.assertIn("ymz280b_dual", chips_in_head(raw))

    def test_single_chip_data_block(self):
        raw = _raw_vgm(
            b"\x5d\x23\x00\x5d\x43\x00\x5d\x63\x20"
            b"\x5d\x00\x10\x5d\x01\xc0",
            [(0, 0x200, 0, bytes(range(0x20)))],
        )
        p = TMP / "single.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertFalse(v.ymz280b_dual)
        self.assertEqual(
            [(r.kind, r.chip) for r in v.roms], [("ymz280b", 0)],
        )


class TestYmz280bNotes(unittest.TestCase):
    def test_pcm8_note_rate_volume_and_pan(self):
        rom = _body(256)
        writes = [_enable()] + _note(
            1000, 0, 200, fnum=0x10, vol=200, pan=15, mode=0x40, off=5000,
        )
        samples, offs, warn = collect_ymz280b(_vgm(
            writes, [RomSlice(0, len(rom), 0, rom, "ymz280b")],
        ))
        self.assertFalse(warn)
        self.assertEqual(len(samples), 1)
        smp = samples[0]
        self.assertEqual(smp.depth, YMZ_DEPTH_PCM8)
        self.assertEqual(smp.data, rom[:200])
        self.assertEqual(smp.mode_fnum(), 0x10)
        self.assertEqual(smp.c4_rate(), round(
            ymz280b_rate(CLOCK, 0x10, YMZ_DEPTH_PCM8),
        ))
        hit = smp.hits[0]
        self.assertEqual(hit.vol, 200)
        self.assertEqual(hit.pan, 15)
        self.assertEqual(hit.off_sample, 5000)
        self.assertEqual([o.sample_time for o in offs], [5000])

        song = analyze(_vgm(
            writes, [RomSlice(0, len(rom), 0, rom, "ymz280b")],
        ))
        self.assertEqual(song.ymz280b_count, 1)
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.vol, 200)
        self.assertEqual(ev.pan, ymz_pan_fx(15))
        self.assertEqual(ev.note, 108)
        natural = 1000 + round(
            200 / ymz280b_rate(CLOCK, 0x10, YMZ_DEPTH_PCM8) * 44100
        )
        self.assertEqual(ev.end, min(5000, natural))

    def test_16bit_rom_is_byteswapped(self):
        # Two big-endian frames: 0x1234 and 0xF00F.
        rom = bytes((0x12, 0x34, 0xF0, 0x0F))
        writes = [_enable()] + _note(
            1000, 0, 4, fnum=0x20, mode=0x60, off=9000,
        )
        samples, _offs, warn = collect_ymz280b(_vgm(
            writes, [RomSlice(0, len(rom), 0, rom, "ymz280b")],
        ))
        self.assertFalse(warn)
        smp = samples[0]
        self.assertEqual(smp.depth, YMZ_DEPTH_PCM16)
        self.assertEqual(smp.data, bytes((0x34, 0x12, 0x0F, 0xF0)))
        self.assertEqual(smp.length, 2)
        self.assertEqual(smp.c4_rate(), round(
            ymz280b_rate(CLOCK, 0x20, YMZ_DEPTH_PCM16),
        ))

    def test_adpcm_body_and_loop_points(self):
        rom = bytes((0x12, 0x34, 0x56, 0x78))
        writes = [_enable()] + _note(
            1000, 0, 4, fnum=0x30, mode=0x20,
            loop=True, loop_start=2, loop_end=4, off=9000,
        )
        samples, _offs, warn = collect_ymz280b(_vgm(
            writes, [RomSlice(0, len(rom), 0, rom, "ymz280b")],
        ))
        self.assertFalse(warn)
        smp = samples[0]
        self.assertEqual(smp.depth, YMZ_DEPTH_ADPCM)
        self.assertEqual(smp.data, rom)
        self.assertEqual(smp.length, 8)       # nibbles
        self.assertEqual(smp.loop_start, 4)   # nibbles
        self.assertEqual(smp.loop_end, 8)
        self.assertEqual(smp.c4_rate(), round(
            ymz280b_rate(CLOCK, 0x30, YMZ_DEPTH_ADPCM),
        ))

    def test_pcm8_loop_points(self):
        rom = _body(256)
        writes = [_enable()] + _note(
            1000, 0, 100, fnum=0x40, mode=0x40,
            loop=True, loop_start=10, loop_end=60, off=9000,
        )
        samples, _offs, warn = collect_ymz280b(_vgm(
            writes, [RomSlice(0, len(rom), 0, rom, "ymz280b")],
        ))
        self.assertFalse(warn)
        self.assertEqual(samples[0].loop_start, 10)
        self.assertEqual(samples[0].loop_end, 60)

    def test_same_sample_setup_after_trigger_is_folded(self):
        rom = _body(256)
        writes = [
            _enable(),
            _w(1000, 0x23, 0), _w(1000, 0x43, 0), _w(1000, 0x63, 100),
            _w(1000, 0x00, 0x55),
            _w(1000, 0x01, 0xC0),   # trigger first, still old vol/pan
            _w(1000, 0x02, 0x99),   # volume written after the trigger
            _w(1000, 0x03, 0x02),   # pan written after the trigger
            _w(1000, 0x01, 0xC1),   # frequency high after the trigger
        ]
        samples, _offs, _warn = collect_ymz280b(_vgm(
            writes, [RomSlice(0, len(rom), 0, rom, "ymz280b")],
        ))
        hit = samples[0].hits[0]
        self.assertEqual(hit.vol, 0x99)
        self.assertEqual(hit.pan, 0x02)
        self.assertEqual(hit.fnum, 0x155)

    def test_repeated_keyon_does_not_retrigger(self):
        rom = _body(256)
        writes = [_enable()] + _note(1000, 0, 200, mode=0x40)
        writes += [_w(2000, 0x01, 0xC0), _w(3000, 0x01, 0xC0)]
        samples, offs, _warn = collect_ymz280b(_vgm(
            writes, [RomSlice(0, len(rom), 0, rom, "ymz280b")],
        ))
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(samples[0].hits), 1)
        self.assertFalse(offs)

    def test_keyon_before_enable_is_reported(self):
        rom = _body(256)
        writes = _note(1000, 0, 200, mode=0x40)
        samples, _offs, warn = collect_ymz280b(_vgm(
            writes, [RomSlice(0, len(rom), 0, rom, "ymz280b")],
        ))
        self.assertFalse(samples)
        self.assertTrue(any("0xFF bit 7" in w for w in warn))

    def test_keyon_enable_is_per_chip(self):
        rom0 = _body(128)
        rom1 = bytes((i * 13 + 7) & 0xFF for i in range(128))
        writes = [_enable(0, 1)]
        writes += _note(1000, 0, 100, mode=0x40, chip=0)
        writes += _note(1200, 0, 100, mode=0x40, chip=1)
        samples, _offs, warn = collect_ymz280b(_vgm(
            writes,
            [
                RomSlice(0, len(rom0), 0, rom0, "ymz280b"),
                RomSlice(1, len(rom1), 0, rom1, "ymz280b"),
            ],
        ))
        self.assertEqual({s.chip_id for s in samples}, {1})
        self.assertTrue(any("0xFF bit 7" in w for w in warn))

    def test_clock_flags(self):
        self.assertEqual(ymz280b_flag_text(16934400), "")
        self.assertEqual(ymz280b_flag_text(16000000), "clockSel=3")
        self.assertEqual(ymz280b_flag_text(14187580), "clockSel=2")
        self.assertEqual(ymz280b_flag_text(12345678), "customClock=12345678")

    def test_pan_law(self):
        self.assertEqual(ymz_pan_ratio(8), (15, 15))
        self.assertEqual(ymz_pan_ratio(1), (15, 0))
        self.assertEqual(ymz_pan_ratio(15), (0, 15))
        self.assertEqual(ymz_pan_fx(1), 0xF0)
        self.assertEqual(ymz_pan_fx(15), 0x0F)


class TestYmz280bModule(unittest.TestCase):
    def _song(self, roms, writes):
        return analyze(_vgm(writes, roms))

    def test_dual_chip_channels_and_fur(self):
        rom0 = _body(128)
        rom1 = bytes((i * 11 + 3) & 0xFF for i in range(128))
        writes = [_enable(0, 0), _enable(0, 1)]
        writes += _note(1000, 0, 100, fnum=0x10, mode=0x40, chip=0)
        writes += _note(1200, 0, 100, fnum=0x20, mode=0x40, chip=1)
        song = self._song(
            [
                RomSlice(0, len(rom0), 0, rom0, "ymz280b"),
                RomSlice(1, len(rom1), 0, rom1, "ymz280b"),
            ],
            writes,
        )
        self.assertEqual(song.ymz280b_ids, [0, 1])
        self.assertEqual(song.ymz280b_count, 2)
        chans = {e.ch for e in song.events if e.on and e.pcm}
        self.assertEqual(len(chans), 2)
        self.assertEqual(max(chans) - min(chans), 8)

        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_YMZ280B, CHIP_YMZ280B])
        self.assertEqual(mod.n_ch, 16)
        self.assertEqual(len(mod.samples), 2)
        types = [
            int.from_bytes(blk[2:4], "little")
            for _off, mag, _size, blk in iter_blocks(data) if mag == b"INS2"
        ]
        self.assertEqual(types, [INS_YMZ280B, INS_YMZ280B])

    def test_real_z66_opl2_plus_ymz(self):
        if not Z66.is_file():
            self.skipTest(f"{Z66} not present")
        song = analyze(load_vgm(Z66))
        self.assertEqual(song.ymz280b_ids, [0])
        self.assertEqual(song.ymz280b_count, 400)
        self.assertEqual(len(song.ymz280b_samples), 3)
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        self.assertIn(CHIP_OPL2, mod.chips)
        self.assertIn(CHIP_YMZ280B, mod.chips)
        self.assertEqual(mod.n_ch, 17)  # 9 OPL2 + 8 YMZ
        self.assertEqual(len(mod.samples), 3)
        # The OPL2 gains operator-level instrument copies after the first
        # tone base was computed. YMZ rows must point at the final three
        # sample instruments (0x0C-0x0E), not the stale 0x09-0x0B copies.
        used = set()
        for ch in mod.pcm_channels:
            for oi in mod.orders[ch]:
                for row in mod.patterns.get((ch, oi), []):
                    if 0 <= row.note < 180 and row.ins >= 0:
                        used.add(row.ins)
        self.assertEqual(used, {12, 13, 14})
        self.assertEqual(mod.ins_sample[12:15], [0, 1, 2])

    def test_real_stage4_two_ymz(self):
        if not STAGE4.is_file():
            self.skipTest(f"{STAGE4} not present")
        v = load_vgm(STAGE4)
        self.assertTrue(v.ymz280b_dual)
        self.assertEqual({r.chip for r in v.roms}, {0, 1})
        song = analyze(v)
        self.assertEqual(song.ymz280b_ids, [0, 1])
        self.assertEqual(song.ymz280b_count, 5007)
        self.assertEqual(len(song.ymz280b_samples), 17)
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        # A YMZ-only log must not gain a silent YM2151 placeholder.
        self.assertEqual(mod.chips, [CHIP_YMZ280B, CHIP_YMZ280B])
        self.assertEqual(mod.n_ch, 16)
        self.assertEqual(len(mod.samples), 17)
        self.assertLessEqual(len(song.fm_patches) + len(song.ymz280b_samples), 250)
        # The two Stage 4 instances run at full output. The measured core
        # difference is carried by the master, not by halving each chip.
        self.assertAlmostEqual(master_volume(data), 1.27, places=2)
        self.assertEqual([round(c.volume, 3) for c in read_chips(data)], [1.0, 1.0])

    def test_sample_depths_match_furnace(self):
        rom = _body(256)
        writes = [_enable()]
        writes += _note(1000, 0, 64, fnum=0x10, mode=0x40, off=3000)
        writes += _note(4000, 0, 32, fnum=0x20, mode=0x60, off=6000)
        writes += _note(7000, 0, 8, fnum=0x30, mode=0x20, off=9000)
        song = self._song(
            [RomSlice(0, len(rom), 0, rom, "ymz280b")], writes,
        )
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        self.assertEqual(
            sorted(s.depth for s in mod.samples),
            sorted([SMP_DEPTH_8BIT, SMP_DEPTH_16BIT, SMP_DEPTH_YMZ_ADPCM]),
        )


if __name__ == "__main__":
    unittest.main()
