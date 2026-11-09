"""Seta/Allumer X1-010 VGM parsing, analysis and Furnace conversion."""

from __future__ import annotations

import math
import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import (
    decompress_fur,
    iter_blocks,
    master_volume,
    parse_fur,
)
from vgm2151fur.furwrite import write_fur
from vgm2151fur.pitch import frequency_pitch_table, furnace_frequency_note
from vgm2151fur.vgm import (
    ChipWrite,
    RomSlice,
    VgmFile,
    load_vgm,
)
from vgm2151fur.x1_010 import (
    X1_FREQ_DIV,
    collect_x1_010,
    x1_pcm_rate,
)


def _write(sample: int, reg: int, value: int) -> ChipWrite:
    return ChipWrite(sample, "x1_010", 0, reg, value & 0xFF)


def _vgm(
    writes: list[ChipWrite], *, total: int = 44_100,
    clock: int = 16_000_000, rom: bytes = b"",
) -> VgmFile:
    roms = (
        [RomSlice(0, max(0x100000, len(rom)), 0, rom, "x1_010")]
        if rom else []
    )
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x171,
        total_samples=total, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        writes=writes, x1_010_clock=clock, roms=roms,
    )


def _wave_setup(
    *, shape: int = 1, wave: int = 0, freq: int = 1845,
    clock: int = 16_000_000,
) -> list[ChipWrite]:
    writes: list[ChipWrite] = []
    # Envelope shape 1 lives at 0x80 on the chip; shape 0 aliases the channel
    # register block and is not used by the examples.
    for i in range(128):
        writes.append(_write(0, 0x80 + i, 0xFF))
    for i in range(128):
        writes.append(_write(
            0, 0x1000 + wave * 128 + i,
            int(96 * math.sin(2 * math.pi * i / 128)) & 0xFF,
        ))
    for reg, value in (
        (0, 0), (1, wave), (2, freq & 0xFF), (3, (freq >> 8) & 0xFF),
        (4, 10), (5, shape),
    ):
        writes.append(_write(0, reg, value))
    writes.append(_write(0, 0, 3))  # key on, wavetable mode
    writes.append(_write(9_000, 0, 0))
    return writes


class TestX1Header(unittest.TestCase):
    def test_header_clock_and_command(self):
        buf = bytearray(0x120)
        buf[:4] = b"Vgm "
        struct.pack_into("<I", buf, 0x04, 0x100 + 4 + 4)
        struct.pack_into("<I", buf, 0x08, 0x171)
        struct.pack_into("<I", buf, 0x18, 44100)
        struct.pack_into("<I", buf, 0x34, 0xCC)
        struct.pack_into("<I", buf, 0xD8, 16_666_666)
        buf[0x100:0x104] = bytes((0xC8, 0x00, 0x03, 0x5A))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x1.vgm"
            path.write_bytes(buf)
            vgm = load_vgm(path)
        self.assertEqual(vgm.x1_010_clock, 16_666_666)
        self.assertEqual(len(vgm.writes), 1)
        self.assertEqual(vgm.writes[0].chip, "x1_010")
        self.assertEqual(vgm.writes[0].reg, 3)
        self.assertEqual(vgm.writes[0].val, 0x5A)

    def test_rom_block(self):
        data = bytes(range(32))
        payload = (
            (0x100000).to_bytes(4, "little")
            + (0x1000).to_bytes(4, "little") + data
        )
        buf = bytearray(0x200)
        buf[:4] = b"Vgm "
        struct.pack_into("<I", buf, 0x04, 0x100 + 7 + len(payload) + 1)
        struct.pack_into("<I", buf, 0x08, 0x171)
        struct.pack_into("<I", buf, 0x18, 44100)
        struct.pack_into("<I", buf, 0x34, 0xCC)
        struct.pack_into("<I", buf, 0xD8, 16_000_000)
        buf[0x100:0x107] = (
            bytes((0x67, 0x66, 0x91))
            + len(payload).to_bytes(4, "little")
        )
        buf[0x107:0x107 + len(payload)] = payload
        buf[0x107 + len(payload)] = 0x66
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x1rom.vgm"
            path.write_bytes(buf)
            vgm = load_vgm(path)
        self.assertEqual(len(vgm.roms), 1)
        self.assertEqual(vgm.roms[0].kind, "x1_010")
        self.assertEqual(vgm.roms[0].start, 0x1000)
        self.assertEqual(vgm.roms[0].data, data)


class TestX1Pitch(unittest.TestCase):
    def test_frequency_inverse_round_trips(self):
        tbl = frequency_pitch_table(16_000_000, X1_FREQ_DIV)
        for value in (4, 16, 163, 1845, 2765, 0xFFFF):
            note, e5 = furnace_frequency_note(
                16_000_000, X1_FREQ_DIV, value, maximum=0xFFFF,
            )
            self.assertEqual(tbl.value(note, e5 - 0x80), value)


class TestX1Collector(unittest.TestCase):
    def test_wave_note(self):
        hits, pcm, offs, warnings = collect_x1_010(_vgm(_wave_setup()))
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(pcm), 0)
        self.assertEqual(warnings, [])
        self.assertEqual(hits[0].wave, 0)
        self.assertEqual(hits[0].env_shape, 1)
        self.assertEqual(hits[0].freq, 1845)
        self.assertEqual(len(hits[0].wave_data), 128)
        self.assertEqual(len(hits[0].env_data), 128)

    def test_pcm_note_body_and_rate(self):
        body = bytes((i * 7) & 0xFF for i in range(0x2000))
        writes = [
            _write(0, 1, 0xEE),
            _write(0, 2, 16),
            _write(0, 4, 0),
            _write(0, 5, 0xFE),
            _write(0, 0, 1),
            _write(8000, 0, 0),
        ]
        hits, pcm, offs, warnings = collect_x1_010(
            _vgm(writes, total=9000, rom=body),
        )
        self.assertEqual(hits, [])
        self.assertEqual(len(pcm), 1)
        self.assertEqual(pcm[0].start, 0)
        self.assertEqual(pcm[0].end, 0x2000)
        self.assertEqual(pcm[0].volume, 14)
        self.assertEqual(pcm[0].rate, x1_pcm_rate(16_000_000, 16))
        self.assertEqual(len(offs), 1)

    def test_pcm_key_on_after_natural_end_is_a_new_note(self):
        # The voice keys itself off when it reaches the ROM end, so a later
        # key-on write restarts the sample even though the status byte was
        # never rewritten to 0.
        body = bytes((i * 7) & 0xFF for i in range(0x1000))
        writes = [
            _write(0, 1, 0xEE),
            _write(0, 2, 16),
            _write(0, 4, 0),
            _write(0, 5, 0xFF),
            _write(0, 0, 1),
            _write(10_000, 0, 1),   # after the first natural end
            _write(10_001, 0, 1),   # duplicate start burst, same note
            _write(20_000, 0, 0),
        ]
        hits, pcm, _offs, _warnings = collect_x1_010(
            _vgm(writes, total=21_000, rom=body),
        )
        self.assertEqual(hits, [])
        self.assertEqual(len(pcm), 2)
        self.assertEqual(pcm[0].sample_time, 0)
        self.assertEqual(pcm[1].sample_time, 10_000)
        self.assertLess(pcm[0].natural_end, pcm[1].sample_time)
        self.assertEqual(pcm[1].natural_end, pcm[1].sample_time + 5_780)


class TestX1Module(unittest.TestCase):
    def test_wave_module_and_instrument(self):
        song = analyze(_vgm(_wave_setup()))
        self.assertEqual(song.x1_010_ids, [0])
        self.assertEqual(song.x1_010_count, 1)
        self.assertEqual(len(song.x1_waves), 1)
        self.assertEqual(len(song.x1_envs), 1)
        self.assertEqual(song.events[0].note, 105)
        data = write_fur(song, include_fm=False)
        self.assertEqual(master_volume(decompress_fur(data)), 1.0)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0xB0])
        self.assertEqual(mod.n_ch, 16)
        self.assertEqual(mod.samples, [])
        # The first INS2 is the X1 wavetable instrument, type 25.
        data = decompress_fur(write_fur(song, include_fm=False))
        types = [
            struct.unpack_from("<H", block, 2)[0]
            for _off, bmag, _sz, block in iter_blocks(data)
            if bmag == b"INS2"
        ]
        self.assertIn(25, types)

    def test_pcm_module_sample_and_frequency_effect(self):
        body = bytes((i * 5) & 0xFF for i in range(0x2000))
        writes = [
            _write(0, 1, 0xEE),
            _write(0, 2, 16),
            _write(0, 4, 0),
            _write(0, 5, 0xFE),
            _write(0, 0, 1),
            _write(8000, 0, 0),
        ]
        song = analyze(_vgm(writes, total=9000, rom=body))
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [0xB0])
        self.assertEqual(len(mod.samples), 1)
        self.assertEqual(mod.samples[0].depth, 8)
        self.assertEqual(mod.ins_sample, [0])
        rows = [
            (pat, i, row.note, row.fx)
            for (ch, pat), pat_rows in mod.patterns.items()
            if ch == 0
            for i, row in enumerate(pat_rows) if row.note >= 0
        ]
        self.assertTrue(rows)
        self.assertEqual(rows[0][2], 60)
        self.assertTrue(any(cmd == 0x20 and val == 16 for cmd, val in rows[0][3]))

    def test_repeated_wave_and_envelope_share_wavetables(self):
        writes = _wave_setup()
        # Second note reuses the same wave/envelope RAM.
        writes += [
            _write(12_000, 1, 0),
            _write(12_000, 5, 1),
            _write(12_000, 0, 3),
            _write(20_000, 0, 0),
        ]
        song = analyze(_vgm(writes, total=20_000))
        self.assertEqual(song.x1_010_count, 2)
        self.assertEqual(len(song.x1_waves), 1)
        self.assertEqual(len(song.x1_envs), 1)

    def test_pitch_point_becomes_a_row_step(self):
        writes = _wave_setup()
        # A mid-note retune. The collector and NoteEvent keep the raw 16-bit
        # register; when the write has its own row the builder emits an
        # E8/E9/E5 step instead of spending an instrument on the macro.
        writes[-1:] = [
            _write(3000, 2, 1900 & 0xFF),
            _write(3000, 3, (1900 >> 8) & 0xFF),
        ] + writes[-1:]
        song = analyze(_vgm(writes))
        ev = [e for e in song.events if e.on and e.tone_chip == 11][0]
        self.assertEqual(ev.tone_pitch_pts, ())
        self.assertEqual(ev.pitch_steps, ((3000, 32),))
        self.assertEqual(ev.sweep_div, 2.0)
        self.assertFalse(any(ins.pitch for ins in song.x1_010_ins))

    def test_row_slides_do_not_spend_the_instrument_budget(self):
        # Each note writes a unique pitch trajectory. Keeping those as
        # instrument macros fills the 250-slot budget and a later note on a
        # different waveform falls back to the first instrument (the wrong
        # wave). Row steps leave one instrument per waveform.
        writes: list[ChipWrite] = []
        for i in range(128):
            writes.append(_write(0, 0x80 + i, 0xFF))
        wave0 = bytes(
            int(96 * math.sin(2 * math.pi * i / 128)) & 0xFF
            for i in range(128)
        )
        wave1 = bytes((i * 2 - 128) & 0xFF for i in range(128))
        for wave, body in ((0, wave0), (1, wave1)):
            for i, value in enumerate(body):
                writes.append(_write(0, 0x1000 + wave * 128 + i, value))
        count = 260
        for i in range(count):
            start = 4000 + i * 4000
            wave = 1 if i == count - 1 else 0
            base = 1800
            a = (i % 7) + 1
            b = ((i // 7) % 7) + 1
            c = ((i // 49) % 7) + 1
            writes += [
                _write(start, 1, wave),
                _write(start, 2, base & 0xFF),
                _write(start, 3, (base >> 8) & 0xFF),
                _write(start, 4, 10),
                _write(start, 5, 1),
                _write(start, 0, 3),
            ]
            for j, offset in enumerate((a, a + b, a + b + c)):
                freq = base + offset * 5
                writes += [
                    _write(start + 800 * (j + 1), 2, freq & 0xFF),
                    _write(start + 800 * (j + 1), 3, (freq >> 8) & 0xFF),
                ]
            writes.append(_write(start + 3600, 0, 0))
        song = analyze(_vgm(writes, total=4000 + count * 4000 + 1000))
        notes = [e for e in song.events if e.on and e.tone_chip == 11]
        self.assertEqual(len(notes), count)
        self.assertLessEqual(len(song.x1_010_ins), 2)
        self.assertFalse(any(ins.pitch for ins in song.x1_010_ins))
        self.assertTrue(all(e.pitch_steps for e in notes))
        for e in notes:
            self.assertEqual(song.x1_010_ins[e.ins].wave, e.tone_wave)
        self.assertEqual(notes[-1].tone_wave, 1)

    def test_period_zero_envelope_shape_changes_become_volume_steps(self):
        # With a zero envelope period the hardware holds the first byte of the
        # selected shape. Drivers ramp the level by switching to another
        # constant shape; that is a volume column step, not an instrument
        # macro that would be stuck at the note's starting level.
        writes: list[ChipWrite] = []
        for i in range(128):
            writes.append(_write(0, 0x80 + i, 0xFF))   # shape 1: level 15
            writes.append(_write(0, 0x100 + i, 0x99))  # shape 2: level 9
        for i in range(128):
            writes.append(_write(
                0, 0x1000 + i, int(80 * math.sin(2 * math.pi * i / 128)) & 0xFF,
            ))
        writes += [
            _write(0, 1, 0),
            _write(0, 2, 1845 & 0xFF),
            _write(0, 3, (1845 >> 8) & 0xFF),
            _write(0, 4, 0),
            _write(0, 5, 1),
            _write(0, 0, 3),
            _write(3000, 5, 2),
            _write(9000, 0, 0),
        ]
        song = analyze(_vgm(writes))
        ev = [e for e in song.events if e.on and e.tone_chip == 11][0]
        self.assertEqual(ev.vol, 15)
        self.assertEqual(ev.vol_pts, ((3000, 9, None),))
        self.assertFalse(any(ins.vol for ins in song.x1_010_ins))

    def test_period_zero_envelope_rewrites_become_volume_steps(self):
        # Replace the constant envelope with a low starting level, then
        # rewrite envelope byte 0 while the note is sounding.
        writes = [
            _write(0, 0x80 + i, 0x11) for i in range(128)
        ] + _wave_setup()[128:]
        writes[-2:-2] = [_write(0, 4, 0)]
        writes.insert(-1, _write(3_000, 0x80, 0x99))
        song = analyze(_vgm(writes))
        ev = [e for e in song.events if e.on and e.tone_chip == 11][0]
        self.assertEqual(ev.vol, 1)
        self.assertEqual(ev.vol_pts, ((3000, 9, None),))
        self.assertFalse(any(ins.vol for ins in song.x1_010_ins))

    def test_pitch_macros_are_not_lfo_looped(self):
        writes = _wave_setup()
        extra = []
        for i in range(20):
            value = 1900 if i & 1 else 1800
            extra += [
                _write(2_500 + i * 300, 2, value & 0xFF),
                _write(2_500 + i * 300, 3, (value >> 8) & 0xFF),
            ]
        writes[-1:-1] = extra
        song = analyze(_vgm(writes))
        self.assertTrue(all(ins.pitch_loop == 255 for ins in song.x1_010_ins))

    def test_pcm_only_has_no_wave_assets(self):
        body = bytes((i * 3) & 0xFF for i in range(0x2000))
        writes = [
            _write(0, 1, 0xEE), _write(0, 2, 16), _write(0, 4, 0),
            _write(0, 5, 0xFE), _write(0, 0, 1), _write(7000, 0, 0),
        ]
        song = analyze(_vgm(writes, total=8000, rom=body))
        self.assertEqual(song.x1_010_ins, [])
        self.assertEqual(song.x1_waves, [])
        self.assertEqual(song.x1_envs, [])
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(len(mod.samples), 1)
        data = decompress_fur(write_fur(song, include_fm=False))
        self.assertNotIn(b"WAVE", data)

    def test_clock_select_flag(self):
        song = analyze(_vgm(
            _wave_setup(clock=16_666_666), clock=16_666_666,
        ))
        data = decompress_fur(write_fur(song, include_fm=False))
        self.assertIn(b"clockSel=1", data)


if __name__ == "__main__":
    unittest.main()
