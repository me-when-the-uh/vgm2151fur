"""YM2610/YM2610B support: parsing, pitch, ADPCM collection and .fur output."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.audit import compare_ym2610_samples
from vgm2151fur.diag import (
    ym2610_keyed_intervals,
    ym2610_pitch_stream,
)
from vgm2151fur.furio import (
    CHIP_CHANNELS,
    iter_blocks,
    parse_fur,
    parse_smp2_payload,
)
from vgm2151fur.furwrite import (
    CHIP_YM2610,
    CHIP_YM2610B,
    INS_ADPCMA,
    INS_ADPCMB,
    INS_FM,
    NOTE_OFF,
    parse_fur_skeleton,
    write_fur,
)
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile, chips_in_head, load_vgm
from vgm2151fur.ym2610 import (
    CHAN_OFF_4,
    CHAN_OFF_6,
    KON_4,
    KON_6,
    YM2610_DEFAULT_CLOCK,
    YM2610State,
    collect_ym2610_adpcm,
    ym2610_note_fnum,
    ym2610_note_for,
)
from vgm2151fur.ym2612 import ym2612_pitch_units

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_ym2610"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
CLOCK = YM2610_DEFAULT_CLOCK


def _raw_vgm(body: bytes, *, clock: int = CLOCK, b: bool = False,
             dual: bool = False) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    raw = (clock & 0x3FFFFFFF)
    if b:
        raw |= 0x80000000
    if dual:
        raw |= 0x40000000
    buf[0x4C:0x50] = raw.to_bytes(4, "little")
    out = bytes(buf) + body + b"\x66"
    return out[:4] + (len(out) - 4).to_bytes(4, "little") + out[8:]


def _rom_block(data: bytes, typ: int = 0x82, start: int = 0) -> bytes:
    size = 8 + len(data)
    return (
        bytes([0x67, 0x66, typ])
        + size.to_bytes(4, "little")
        + len(data).to_bytes(4, "little")
        + start.to_bytes(4, "little")
        + data
    )


def _vgm(writes, rom=None, *, b: bool = False, total_samples=8000) -> VgmFile:
    roms = []
    if rom is not None:
        roms = [RomSlice(0, len(rom), 0, rom, "ym2610_adpcm")]
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=total_samples, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        roms=roms, writes=sorted(writes, key=lambda w: w.sample),
        ym2610_clock=CLOCK, ym2610_b=b,
    )


def _fm_note(ch: int, *, b: bool = False, block: int = 4, fnum: int = 0x400,
             t: int = 100, off: int = 7000, cid: int = 0) -> list[ChipWrite]:
    offsets = CHAN_OFF_6 if b else CHAN_OFF_4
    kon_map = KON_6 if b else KON_4
    bank, off = offsets[ch]
    base = 0x100 if bank else 0
    out: list[ChipWrite] = []
    for page in (0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
        for op in range(4):
            val = 0x01 if page == 0x30 else (0x1F if page == 0x50 else 0)
            out.append(ChipWrite(
                t, "ym2610", cid, base | page | (op << 2) | off, val,
            ))
    out += [
        ChipWrite(t, "ym2610", cid, base | 0xB0 | off, 0x07),
        ChipWrite(t, "ym2610", cid, base | 0xB4 | off, 0xC0),
        ChipWrite(t, "ym2610", cid, base | 0xA4 | off,
                 (block << 3) | ((fnum >> 8) & 7)),
        ChipWrite(t, "ym2610", cid, base | 0xA0 | off, fnum & 0xFF),
        ChipWrite(t, "ym2610", cid, 0x28,
                 0xF0 | next(k for k, v in kon_map.items() if v == ch)),
    ]
    if off:
        out.append(ChipWrite(off, "ym2610", cid, 0x28,
                             next(k for k, v in kon_map.items() if v == ch)))
    return out


def _adpcm_writes(rom_len: int = 1024) -> list[ChipWrite]:
    return [
        # ADPCM-A channel 1: region 0x000..0x1ff, gate level 0 then 8.
        ChipWrite(0, "ym2610", 0, 0x101, 0x3F),
        ChipWrite(100, "ym2610", 0, 0x110, 0x00),
        ChipWrite(100, "ym2610", 0, 0x118, 0x00),
        ChipWrite(100, "ym2610", 0, 0x120, 0x01),
        ChipWrite(100, "ym2610", 0, 0x128, 0x00),
        ChipWrite(100, "ym2610", 0, 0x108, 0xC0),
        ChipWrite(100, "ym2610", 0, 0x100, 0x01),
        ChipWrite(108, "ym2610", 0, 0x108, 0xC8),
        ChipWrite(1100, "ym2610", 0, 0x100, 0x81),
        # ADPCM-B: region 0x200..0x3ff, 50% rate, pan set after start.
        ChipWrite(2000, "ym2610", 0, 0x12, 0x02),
        ChipWrite(2000, "ym2610", 0, 0x13, 0x00),
        ChipWrite(2000, "ym2610", 0, 0x14, 0x03),
        ChipWrite(2000, "ym2610", 0, 0x15, 0x00),
        ChipWrite(2000, "ym2610", 0, 0x11, 0x00),
        ChipWrite(2000, "ym2610", 0, 0x1B, 0x00),
        ChipWrite(2000, "ym2610", 0, 0x10, 0x80),
        ChipWrite(2002, "ym2610", 0, 0x11, 0xC0),
        ChipWrite(2002, "ym2610", 0, 0x1B, 0x80),
        ChipWrite(2013, "ym2610", 0, 0x19, 0x00),
        ChipWrite(2015, "ym2610", 0, 0x1A, 0x80),
        ChipWrite(3000, "ym2610", 0, 0x10, 0x00),
    ]


class TestYm2610Parsing(unittest.TestCase):
    def test_header_commands_and_ssg_split(self):
        body = bytes([
            0x58, 0x30, 0x01,    # port 0 FM operator
            0x58, 0x08, 0x0F,    # port 0 SSG volume
            0x59, 0x10, 0x02,    # port 1 ADPCM-A start low
            0xA8, 0x40, 0x03,    # second chip, port 0
            0xA9, 0x11, 0x04,    # second chip, port 1
        ])
        p = TMP / "ym2610_dual.vgm"
        p.write_bytes(_raw_vgm(body, b=True, dual=True))
        vgm = load_vgm(p)
        self.assertEqual(vgm.ym2610_clock, CLOCK)
        self.assertTrue(vgm.ym2610_b)
        self.assertTrue(vgm.ym2610_dual)
        got = [(w.chip, w.chip_id, w.reg, w.val) for w in vgm.writes]
        self.assertEqual(got, [
            ("ym2610", 0, 0x030, 0x01),
            ("ym2610_ssg", 0, 0x008, 0x0F),
            ("ym2610", 0, 0x110, 0x02),
            ("ym2610", 1, 0x040, 0x03),
            ("ym2610", 1, 0x111, 0x04),
        ])

    def test_chips_in_head(self):
        self.assertIn("ym2610", chips_in_head(_raw_vgm(b"\x66")))
        kinds = chips_in_head(_raw_vgm(b"\x66", b=True))
        self.assertIn("ym2610", kinds)
        self.assertIn("ym2610_b", kinds)
        self.assertNotIn("ym2151", kinds)

    def test_adpcm_rom_block(self):
        rom = bytes(range(256)) * 2
        p = TMP / "ym2610_rom.vgm"
        p.write_bytes(_raw_vgm(_rom_block(rom)))
        vgm = load_vgm(p)
        slices = [r for r in vgm.roms if r.kind == "ym2610_adpcm"]
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].data, rom)

    def test_missing_clock_warning(self):
        p = TMP / "ym2610_noclock.vgm"
        p.write_bytes(_raw_vgm(bytes([0x58, 0x30, 0x01]), clock=0))
        vgm = load_vgm(p)
        self.assertEqual(vgm.ym2610_clock, 0)
        self.assertTrue(any("YM2610 clock" in w for w in vgm.warnings))


class TestYm2610Pitch(unittest.TestCase):
    def test_inverse_round_trip(self):
        for note in (36, 48, 60, 72, 96, 120):
            block, fnum = ym2610_note_fnum(note, CLOCK)
            note2, e5 = ym2610_note_for(block, fnum, CLOCK)
            block2, fnum2 = ym2610_note_fnum(note2, CLOCK, e5)
            self.assertLessEqual(
                abs(ym2612_pitch_units(block2, fnum2)
                    - ym2612_pitch_units(block, fnum)),
                1.0,
            )

    def test_channel_offsets(self):
        st4 = YM2610State(False)
        self.assertEqual(st4.channel_of(0x031), 0)
        self.assertEqual(st4.channel_of(0x032), 1)
        self.assertEqual(st4.channel_of(0x131), 2)
        self.assertEqual(st4.channel_of(0x132), 3)
        self.assertIsNone(st4.channel_of(0x030))
        self.assertIsNone(st4.channel_of(0x133))
        st6 = YM2610State(True)
        self.assertEqual(st6.channel_of(0x030), 0)
        self.assertEqual(st6.channel_of(0x032), 2)
        self.assertEqual(st6.channel_of(0x130), 3)
        self.assertEqual(st6.channel_of(0x132), 5)


class TestYm2610Adpcm(unittest.TestCase):
    def test_collect_a_and_b(self):
        rom = bytes(i & 0xFF for i in range(1024))
        vgm = _vgm(_adpcm_writes(), rom)
        a, b, warnings = collect_ym2610_adpcm(vgm, rom)
        self.assertFalse(warnings)
        self.assertEqual(len(a), 1)
        self.assertEqual((a[0].start, a[0].end), (0x000, 0x1FF))
        self.assertEqual(a[0].data, rom[0:0x200])
        hit = a[0].hits[0]
        self.assertEqual(hit.vol, 8)  # 0xC8 written after the gate
        self.assertEqual(hit.pan, 0xFF)
        self.assertEqual(hit.end_time, 1100)
        self.assertEqual(len(b), 1)
        self.assertEqual((b[0].start, b[0].end, b[0].rate),
                         (0x200, 0x3FF, 0x8000))
        self.assertEqual(b[0].data, rom[0x200:0x400])
        bhit = b[0].hits[0]
        self.assertEqual(bhit.vol, 0x80)
        self.assertEqual(bhit.pan, 0xFF)
        self.assertEqual(bhit.end_time, 3000)

    def test_bank_b_falls_back_to_83(self):
        rom_a = bytes(1024)
        rom_b = bytes(i & 0xFF for i in range(1024))
        vgm = _vgm(_adpcm_writes())
        _a, b, _w = collect_ym2610_adpcm(vgm, rom_a, rom_b)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0].data, rom_b[0x200:0x400])
        self.assertEqual(b[0].rate, 0x8000)

    def test_sample_audit_reads_b_from_83(self):
        rom_a = bytes(1024)
        rom_b = bytes((i * 7) & 0xFF for i in range(1024))
        src = _vgm(_adpcm_writes(), rom_a)
        src.roms.append(
            RomSlice(0, len(rom_b), 0, rom_b, "ym2610_deltat")
        )
        exp = _vgm(_adpcm_writes())
        exp.roms = [RomSlice(0, len(rom_b), 0, rom_b, "ym2610_deltat")]
        samples = compare_ym2610_samples(src, exp)
        self.assertEqual(samples["b"]["matched"], 1)
        self.assertEqual(samples["b"]["data"], 0)


class TestYm2610Writer(unittest.TestCase):
    def _song(self, b: bool = True):
        writes = _fm_note(1, b=b)
        writes += [
            ChipWrite(200, "ym2610_ssg", 0, 0x00, 0x00),
            ChipWrite(200, "ym2610_ssg", 0, 0x01, 0x02),
            ChipWrite(200, "ym2610_ssg", 0, 0x08, 0x0F),
            ChipWrite(200, "ym2610_ssg", 0, 0x07, 0x3E),
        ]
        writes += _adpcm_writes()
        rom = bytes(i & 0xFF for i in range(1024))
        return analyze(_vgm(writes, rom, b=b))

    def test_layout_and_instruments(self):
        song = self._song(b=True)
        data = write_fur(song)
        sk = parse_fur_skeleton(data)
        self.assertEqual(sk["chips"], [CHIP_YM2610B])
        self.assertGreaterEqual(sk["notes"], 3)
        self.assertGreaterEqual(sk["offs"], 2)
        self.assertEqual(CHIP_CHANNELS[CHIP_YM2610B], 16)
        types = []
        depths = []
        presence = []
        for _off, mag, _size, payload in iter_blocks(data):
            if mag == b"INS2" and len(payload) >= 4:
                types.append(int.from_bytes(payload[2:4], "little"))
            elif mag == b"SMP2":
                smp = parse_smp2_payload(payload)
                depths.append(smp["depth"])
                presence.append(smp["presence"])
        self.assertIn(INS_FM, types)
        self.assertIn(INS_ADPCMA, types)
        self.assertIn(INS_ADPCMB, types)
        self.assertEqual(sorted(depths), [5, 6])
        masks = [
            [int.from_bytes(p[i * 4:(i + 1) * 4], "little") for i in range(4)]
            for p in presence
        ]
        self.assertTrue(any(m[0] & 1 and not m[1] for m in masks))
        self.assertTrue(any(m[1] & 1 and not m[0] for m in masks))

    def test_plain_variant_layout(self):
        song = self._song(b=False)
        sk = parse_fur_skeleton(write_fur(song))
        self.assertEqual(sk["chips"], [CHIP_YM2610])
        self.assertEqual(CHIP_CHANNELS[CHIP_YM2610], 14)

    def test_adpcm_offs_written(self):
        song = analyze(_vgm(_adpcm_writes(), bytes(1024), total_samples=3000))
        m = parse_fur(write_fur(song))
        offs = {
            ch for (ch, _idx), rows in m.patterns.items()
            for row in rows if row.note == NOTE_OFF
        }
        self.assertIn(7, offs)   # ADPCM-A channel 0
        self.assertIn(13, offs)  # ADPCM-B

    def test_adpcm_loop_end_off(self):
        song = analyze(_vgm(_adpcm_writes(), bytes(1024), total_samples=3000))
        song.loop_sample = 2000
        song.loop_end_sample = 2900
        song.loop_cut_sample = 2950
        m = parse_fur(write_fur(song))
        offs = [
            row for (ch, _idx), rows in m.patterns.items() if ch == 13
            for row in rows if row.note == NOTE_OFF
        ]
        self.assertTrue(offs)

    def test_adjusted_loop_keeps_source_tail(self):
        song = analyze(_vgm(_adpcm_writes(), bytes(1024), total_samples=3000))
        song.loop_sample = 1000
        song.loop_end_sample = 1500
        song.loop_cut_sample = 1800
        m = parse_fur(write_fur(song))
        notes = [
            row.note for (ch, _idx), rows in m.patterns.items() if ch == 13
            for row in rows if 0 <= row.note < 180
        ]
        self.assertIn(60, notes)  # B trigger at 2000, after the bar trim

    @unittest.skipUnless(HAVE_FURNACE, "Furnace is not installed")
    def test_furnace_round_trip(self):
        song = self._song(b=True)
        fur = TMP / "ym2610_roundtrip.fur"
        fur.write_bytes(write_fur(song, compress=True))
        out = TMP / "ym2610_roundtrip.vgm"
        export_vgm(fur, out)
        vgm = load_vgm(out)
        self.assertTrue(vgm.ym2610_b)
        self.assertEqual(vgm.ym2610_clock, CLOCK)
        # The FM voice and the SSG voice survive the export.
        self.assertTrue(ym2610_pitch_stream(vgm, 1))
        self.assertTrue(ym2610_keyed_intervals(vgm, 1))
        self.assertTrue(ym2610_pitch_stream(vgm, 6))
        samples = compare_ym2610_samples(song.vgm, vgm)
        self.assertEqual(samples["a"]["matched"], 1)
        self.assertEqual(samples["b"]["matched"], 1)
        self.assertEqual(
            samples["a"]["data"] + samples["b"]["data"]
            + samples["a"]["vol"] + samples["b"]["vol"]
            + samples["b"]["rate"],
            0,
        )
        self.assertTrue(any(
            w.chip == "ym2610" and not (w.reg & 0x100)
            and w.reg == 0x10 and (w.val & 1) for w in vgm.writes
        ))
        self.assertTrue(any(
            w.chip == "ym2610" and (w.reg & 0x1FF) == 0x100
            and (w.val & 0x80) for w in vgm.writes
        ))


if __name__ == "__main__":
    unittest.main()
