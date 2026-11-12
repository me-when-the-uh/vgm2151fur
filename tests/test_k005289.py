"""Konami K005289 (Bubble System WSG): parsing, collection, .fur emission."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.diag import (
    _fur_wsg_stream, _runs_between, k005289_keyed_intervals,
    k005289_pitch_stream, tone_pitch_stream,
)
from vgm2151fur.edit import read_chips_file
from vgm2151fur.furio import CHIP_CHANNELS, iter_blocks, parse_fur
from vgm2151fur.furwrite import (
    CHIP_AY8910, CHIP_K005289, INS_SCC, write_fur,
)
from vgm2151fur.k005289 import (
    K005289_PROM_SIZE, collect_k005289, k005289_note, k005289_rate,
)
from vgm2151fur.pitch import PeriodPitchTable
from vgm2151fur.vgm import (
    ChipWrite, RomSlice, VgmFile, assemble_rom, chips_in_head, load_vgm,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_k005289"
TMP.mkdir(exist_ok=True)
PACK = Path(r"D:\Downloads\Gradius VGM Rip")

# A 4-bit ramp and its reverse; both are valid 32-sample PROM waveforms.
RAMP = bytes(i % 16 for i in range(32))
RAMP_DOWN = bytes(15 - (i % 16) for i in range(32))


def _prom(wave0: bytes = RAMP, wave1: bytes = RAMP_DOWN,
          bank1: bytes | None = None) -> bytes:
    prom = bytearray(K005289_PROM_SIZE)
    prom[0:32] = wave0
    prom[0x20:0x40] = RAMP_DOWN if bank1 is None else bank1
    prom[0x100:0x120] = wave1
    return bytes(prom)


def _prom_block(prom: bytes) -> bytes:
    """0xC3 RAM write: 16-bit address, then the 512 PROM bytes."""
    return (
        bytes([0x67, 0x66, 0xC3])
        + (2 + len(prom)).to_bytes(4, "little")
        + (0).to_bytes(2, "little")
        + prom
    )


def _k(off: int, val: int) -> bytes:
    return bytes([0x42, (off << 4) | ((val >> 8) & 0x0F), val & 0xFF])


def _delay(samples: int) -> bytes:
    return bytes([0x61]) + samples.to_bytes(2, "little")


def _raw_vgm(
    body: bytes, *, k005289_clock: int = 0, ay_clock: int = 0,
    ay_dual: bool = False,
) -> bytes:
    buf = bytearray(0x100)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x172).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0x100 - 0x34).to_bytes(4, "little")
    if k005289_clock:
        buf[0xEC:0xF0] = k005289_clock.to_bytes(4, "little")
    if ay_clock:
        raw = ay_clock | (0x40000000 if ay_dual else 0)
        buf[0x74:0x78] = raw.to_bytes(4, "little")
        buf[0x78] = 0
    payload = bytes(buf) + body + b"\x66"
    payload = bytearray(payload)
    payload[4:8] = (len(payload) - 4).to_bytes(4, "little")
    return bytes(payload)


def _vgm(writes, **kw) -> VgmFile:
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x172,
        total_samples=kw.pop("total_samples", 400000), loop_samples=0,
        loop_file_pos=None, loop_sample=None, ym2151_clock=3579545,
        k007232_clock=0, gd3={}, roms=kw.pop("roms", []),
        writes=list(writes), **kw,
    )


def _ramp_prom() -> list[RomSlice]:
    return [RomSlice(0, K005289_PROM_SIZE, 0, _prom(), "k005289")]


def _song_writes(
    *, start: int = 100, off: int = 20000, period: int = 0xF00,
    vol: int = 12,
) -> list[ChipWrite]:
    return [
        ChipWrite(0, "k005289", 0, 0, 0),
        ChipWrite(0, "k005289", 0, 1, 0),
        ChipWrite(start, "k005289", 0, 2, period),
        ChipWrite(start, "k005289", 0, 4, 0),
        ChipWrite(start, "k005289", 0, 0, (0 << 5) | vol),
        ChipWrite(off, "k005289", 0, 0, 0),
    ]


class TestK005289Parsing(unittest.TestCase):
    def test_command_clock_and_prom(self):
        prom = _prom()
        body = (
            _prom_block(prom)
            + _k(2, 0xF00) + _k(4, 0) + _k(0, 0x0C)
            + _delay(1000)
            + _k(0, 0)
        )
        p = TMP / "k005289_header.vgm"
        p.write_bytes(_raw_vgm(body, k005289_clock=3579545))
        vgm = load_vgm(p)
        self.assertEqual(vgm.k005289_clock, 3579545)
        writes = [
            (w.sample, w.reg, w.val) for w in vgm.writes
            if w.chip == "k005289"
        ]
        self.assertIn((0, 2, 0xF00), writes)
        self.assertIn((0, 4, 0x000), writes)
        self.assertIn((0, 0, 0x00C), writes)
        self.assertEqual(len(vgm.roms), 1)
        self.assertEqual(vgm.roms[0].kind, "k005289")
        self.assertEqual(vgm.roms[0].rom_size, K005289_PROM_SIZE)
        self.assertEqual(assemble_rom(vgm, "k005289"), prom)
        self.assertIn("k005289", chips_in_head(p.read_bytes()))
        self.assertFalse([
            w for w in vgm.warnings if "PROM" in w or "clock" in w
        ])

    def test_missing_clock_and_prom_warn(self):
        body = _k(2, 0xF00) + _k(4, 0) + _k(0, 0x0C) + _delay(2000)
        p = TMP / "k005289_incomplete.vgm"
        p.write_bytes(_raw_vgm(body))
        vgm = load_vgm(p)
        self.assertEqual(vgm.k005289_clock, 0)
        text = " ".join(vgm.warnings)
        self.assertIn("K005289 clock", text)
        self.assertIn("PROM", text)

    def test_chips_in_head_needs_uncompressed_bytes(self):
        p = TMP / "k005289_kinds.vgm"
        p.write_bytes(_raw_vgm(b"\x66", k005289_clock=3579545))
        self.assertIn("k005289", chips_in_head(p.read_bytes()))


class TestK005289Collect(unittest.TestCase):
    def test_note_and_gate(self):
        vgm = _vgm(_song_writes(), roms=_ramp_prom(),
                   k005289_clock=3579545)
        notes, offs, waves, warn = collect_k005289(vgm)
        self.assertEqual(len(notes), 1)
        self.assertEqual(warn, [])
        hit = notes[0].hit
        self.assertEqual(hit.voice, 0)
        self.assertEqual(hit.period, 0xFFF - 0xF00)
        self.assertEqual(hit.vol, 12)
        self.assertEqual(hit.wave, RAMP)
        self.assertEqual(notes[0].end_sample, 20000)
        self.assertEqual(offs[0].sample_time, 20000)
        self.assertEqual(waves, [RAMP])
        self.assertAlmostEqual(
            k005289_rate(3579545, 0xFF), 3579545 / (32 * 0x100),
        )
        note, e5 = k005289_note(vgm.k005289_clock, 0xFF)
        self.assertGreaterEqual(note, 0)
        self.assertLessEqual(note, 179)
        table = PeriodPitchTable(440.0, 3579545.0, 32)
        self.assertEqual(table.value(note, e5 - 0x80) - 1, 0xFF)

    def test_tg_pitch_change_is_recorded(self):
        writes = _song_writes(off=60000)
        writes.insert(5, ChipWrite(30000, "k005289", 0, 2, 0xE00))
        writes.insert(6, ChipWrite(30000, "k005289", 0, 4, 0))
        notes, _offs, _waves, _warn = collect_k005289(
            _vgm(writes, roms=_ramp_prom(), k005289_clock=3579545)
        )
        self.assertEqual(len(notes), 1)
        self.assertIn((30000, 0xFFF - 0xE00), notes[0].hit.freq_pts)

    def test_volume_steps(self):
        writes = _song_writes(off=20000)
        writes.insert(5, ChipWrite(5000, "k005289", 0, 0, 9))
        notes, _offs, _waves, _warn = collect_k005289(
            _vgm(writes, roms=_ramp_prom(), k005289_clock=3579545)
        )
        self.assertIn((5000, 9), notes[0].hit.vol_pts)

    def test_wave_bank_change(self):
        writes = _song_writes(off=20000)
        writes.insert(5, ChipWrite(10000, "k005289", 0, 0, (1 << 5) | 12))
        notes, _offs, _waves, _warn = collect_k005289(
            _vgm(writes, roms=_ramp_prom(),
                 k005289_clock=3579545)
        )
        self.assertEqual(notes[0].hit.wave_pts[-1][1], RAMP_DOWN)

    def test_short_gate_is_a_glitch(self):
        writes = _song_writes(off=150)
        notes, offs, _waves, warn = collect_k005289(
            _vgm(writes, roms=_ramp_prom(), k005289_clock=3579545)
        )
        self.assertEqual(notes, [])
        self.assertEqual(offs, [])
        self.assertTrue(any("shorter" in w for w in warn))

    def test_missing_prom_gives_no_notes(self):
        vgm = _vgm(_song_writes(), k005289_clock=3579545)
        notes, offs, waves, _warn = collect_k005289(vgm)
        self.assertEqual((notes, offs, waves), ([], [], []))


class TestK005289Analysis(unittest.TestCase):
    def _song(self):
        writes = _song_writes(off=60000)
        writes.insert(5, ChipWrite(30000, "k005289", 0, 2, 0xE00))
        writes.insert(6, ChipWrite(30000, "k005289", 0, 4, 0))
        vgm = _vgm(writes, roms=_ramp_prom(), k005289_clock=3579545,
                   total_samples=80000)
        return vgm, analyze(vgm)

    def test_note_split_and_emit(self):
        _vgm_obj, song = self._song()
        self.assertEqual(song.k005289_ids, [0])
        self.assertEqual(song.k005289_count, 2)
        notes = [e for e in song.events if e.on and e.tone_chip == 16]
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0].ch, 8)  # no FM/sample block in this log
        data = write_fur(song, include_pcm=False, include_fm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_K005289])
        self.assertEqual(mod.n_ch, 2)
        self.assertEqual(CHIP_CHANNELS[CHIP_K005289], 2)
        ins_types = [
            struct.unpack_from("<H", block, 2)[0]
            for _off, mag, _size, block in iter_blocks(data) if mag == b"INS2"
        ]
        self.assertIn(INS_SCC, ins_types)
        waves = [
            block for _off, mag, _size, block in iter_blocks(data)
            if mag == b"WAVE"
        ]
        self.assertEqual(len(waves), len(song.k005289_waves))
        self.assertEqual(_wave_values(waves[0]), list(RAMP))
        self.assertEqual(_wave_max(waves[0]), 15)

    def test_chip_name_for_edit(self):
        _vgm_obj, song = self._song()
        fur = TMP / "k005289_edit.fur"
        fur.write_bytes(write_fur(song, include_pcm=False, include_fm=False))
        chips = read_chips_file(fur)
        self.assertEqual([c.name for c in chips], ["Bubble System WSG"])

    def test_volume_decay_macro(self):
        writes = _song_writes(off=40000)
        writes.insert(5, ChipWrite(5000, "k005289", 0, 0, 9))
        writes.insert(6, ChipWrite(10000, "k005289", 0, 0, 6))
        song = analyze(_vgm(writes, roms=_ramp_prom(),
                            k005289_clock=3579545, total_samples=60000))
        self.assertTrue(song.k005289_ins)
        spec = song.k005289_ins[0]
        self.assertIn(9, spec.vol)
        self.assertIn(6, spec.vol)

    def test_wave_morph_macro(self):
        writes = _song_writes(off=40000)
        writes.insert(5, ChipWrite(10000, "k005289", 0, 0, (1 << 5) | 12))
        song = analyze(_vgm(writes, roms=_ramp_prom(),
                            k005289_clock=3579545, total_samples=60000))
        specs = [s for s in song.k005289_ins if s.waves]
        self.assertTrue(specs)
        self.assertGreaterEqual(len(song.k005289_waves), 2)
        data = write_fur(song, include_pcm=False, include_fm=False)
        ins = [
            block for _off, mag, _size, block in iter_blocks(data)
            if mag == b"INS2"
        ]
        self.assertTrue(any(b"\x03" in block for block in ins))

    def test_condensed_module_keeps_wsg(self):
        writes = _song_writes(off=60000)
        song = analyze(
            _vgm(writes, roms=_ramp_prom(), k005289_clock=3579545,
                 total_samples=80000),
            condense=2,
        )
        self.assertGreater(song.k005289_count, 0)
        data = write_fur(song, include_pcm=False, include_fm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_K005289])
        self.assertEqual(mod.n_ch, 2)


class TestK005289Compare(unittest.TestCase):
    def test_pitch_and_keyed_stream(self):
        vgm = _vgm(_song_writes(), roms=_ramp_prom(),
                   k005289_clock=3579545)
        self.assertEqual(
            k005289_pitch_stream(vgm, 0, 0)[0],
            (100, round(-768.0 * __import__("math").log2(0x100))),
        )
        intervals = k005289_keyed_intervals(vgm, 0, 0)
        self.assertEqual(intervals[0][0], 100)
        self.assertEqual(intervals[0][1], 20000)

    def test_module_stream_matches_source(self):
        writes = _song_writes(off=60000)
        writes.insert(5, ChipWrite(30000, "k005289", 0, 2, 0xE00))
        writes.insert(6, ChipWrite(30000, "k005289", 0, 4, 0))
        vgm = _vgm(writes, roms=_ramp_prom(), k005289_clock=3579545,
                   total_samples=80000)
        song = analyze(vgm)
        fur = TMP / "k005289_compare.fur"
        fur.write_bytes(write_fur(song, include_pcm=False, include_fm=False))
        mod_stream, mod_intervals = _fur_wsg_stream(fur, 0, 3579545)
        self.assertTrue(mod_stream)
        self.assertTrue(mod_intervals)
        runs = _runs_between(
            k005289_pitch_stream(vgm, 0, 0), mod_stream,
            k005289_keyed_intervals(vgm, 0, 0), None, 0.5, 100.0,
        )
        self.assertEqual(runs, [])

    def test_tone_layout_maps_ay_then_wsg(self):
        ay = 1789772
        writes = [
            ChipWrite(0, "ay8910", 0, 7, 0xFE),
            ChipWrite(0, "ay8910", 0, 0, 254),
            ChipWrite(0, "ay8910", 0, 1, 0),
            ChipWrite(100, "ay8910", 0, 8, 12),
        ] + _song_writes()
        vgm = _vgm(writes, roms=_ramp_prom(), k005289_clock=3579545,
                   ay8910_clock=ay, ay8910_dual=True)
        # AY chips 0 and 1 own channels 0-5; the WSG is 6-7.
        self.assertEqual(len(tone_pitch_stream(vgm, 0, [
            ("ay8910", 0, 3), ("ay8910", 1, 3), ("k005289", 0, 2),
        ])), 1)
        self.assertEqual(
            tone_pitch_stream(vgm, 6, [
                ("ay8910", 0, 3), ("ay8910", 1, 3), ("k005289", 0, 2),
            ])[0][0], 100,
        )
        self.assertEqual(
            tone_pitch_stream(vgm, 7, [
                ("ay8910", 0, 3), ("ay8910", 1, 3), ("k005289", 0, 2),
            ]), [],
        )


class TestGradiusPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PACK.is_dir():
            raise unittest.SkipTest("Gradius pack is not present")

    def test_every_track_parses_and_collects(self):
        for path in sorted(PACK.glob("*.vgz")):
            with self.subTest(track=path.name):
                vgm = load_vgm(path)
                self.assertEqual(vgm.k005289_clock, 3579545)
                notes, _offs, waves, warn = collect_k005289(vgm)
                self.assertEqual(warn, [])
                self.assertTrue(waves)
                self.assertTrue(notes)

    def test_convert_and_compare_wsg(self):
        from vgm2151fur.furnace import find_furnace
        from vgm2151fur.diag import compare_fur_to_source

        if not find_furnace():
            self.skipTest("needs the Furnace console build")
        src = PACK / "03 Challenger 1985 (Stage 1).vgz"
        song = analyze(load_vgm(src))
        fur = TMP / "gradius03.fur"
        fur.write_bytes(write_fur(song, include_pcm=True, include_fm=False))
        self.assertEqual(
            [c.name for c in read_chips_file(fur)],
            ["AY8910", "AY8910", "Bubble System WSG"],
        )
        # Bubble System modules use a flat mixer: every system at 1.0.
        self.assertEqual(
            [round(c.volume, 2) for c in read_chips_file(fur)],
            [1.0, 1.0, 1.0],
        )
        for ch in (6, 7):
            runs, _exported = compare_fur_to_source(
                src, fur, ch=ch, export_to=TMP / f"gradius03_ch{ch}.vgm",
            )
            self.assertEqual(runs, [])


def _wave_values(block: bytes) -> list[int]:
    name_end = block.index(0)
    pos = name_end + 1
    length = struct.unpack_from("<I", block, pos)[0]
    pos += 4 + 4 + 4
    return [struct.unpack_from("<I", block, pos + 4 * i)[0] for i in range(length)]


def _wave_max(block: bytes) -> int:
    name_end = block.index(0)
    pos = name_end + 1 + 4
    return struct.unpack_from("<I", block, pos + 4)[0]


if __name__ == "__main__":
    unittest.main()
