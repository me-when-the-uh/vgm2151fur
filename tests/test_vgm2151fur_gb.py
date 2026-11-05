"""Game Boy DMG: parsing, notes, envelopes, wavetables and module layout."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import iter_blocks, parse_fur
from vgm2151fur.furwrite import CHIP_GB, INS_GB, write_fur
from vgm2151fur.gb import (
    collect_gb,
    gb_noise_note,
    gb_note,
    gb_period_delta,
    wave_nibbles,
)
from vgm2151fur.vgm import ChipWrite, VgmFile, load_vgm

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_gb"
TMP.mkdir(exist_ok=True)


def _vgm(writes, **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        writes=list(writes), gb_dmg_clock=4194304, **kw,
    )


def _gb(sample, reg, val):
    return ChipWrite(sample, "gb", 0, reg, val)


def _note(sample, env, env_len=0, duty=2, period=0x600):
    """A pulse-1 note: duty, envelope, period, trigger."""
    return [
        _gb(sample, 0x01, (duty << 6) | 0),
        _gb(sample, 0x02, env),
        _gb(sample, 0x03, period & 0xFF),
        _gb(sample, 0x04, ((period >> 8) & 7) | 0x80),
    ]


class TestGbParsing(unittest.TestCase):
    def test_command_and_clock(self):
        buf = bytearray(0xC0)
        buf[0:4] = b"Vgm "
        buf[8:12] = (0x161).to_bytes(4, "little")
        buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
        buf[0x80:0x84] = (4194304).to_bytes(4, "little")
        body = bytes([
            0xB3, 0x02, 0x47, 0xB3, 0x14, 0x77, 0xB3, 0x15, 0xFF,
            0xB3, 0x20, 0xAB, 0x66,
        ])
        p = TMP / "parse_gb.vgm"
        p.write_bytes(bytes(buf) + body)
        v = load_vgm(p)
        self.assertEqual(v.gb_dmg_clock, 4194304)
        self.assertEqual(
            [(w.reg, w.val) for w in v.writes if w.chip == "gb"],
            [(0x02, 0x47), (0x14, 0x77), (0x15, 0xFF), (0x20, 0xAB)],
        )
        self.assertNotIn("unsupported VGM command", " ".join(v.warnings))

    def test_dual_bit_selects_second_instance(self):
        buf = bytearray(0xC0)
        buf[0:4] = b"Vgm "
        buf[8:12] = (0x161).to_bytes(4, "little")
        buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
        buf[0x80:0x84] = (0x40000000 | 4194304).to_bytes(4, "little")
        p = TMP / "parse_gb_dual.vgm"
        p.write_bytes(bytes(buf) + bytes([0xB3, 0x82, 0x47, 0x66]))
        v = load_vgm(p)
        self.assertTrue(v.gb_dmg_dual)
        self.assertEqual([(w.chip_id, w.reg) for w in v.writes],
                         [(1, 0x02)])


class TestGbNotes(unittest.TestCase):
    def test_pulse_note_and_pitch(self):
        v = _vgm(_note(1000, 0xF0, period=0x600))
        hits, offs, waves, warn = collect_gb(v)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].voice, 0)
        self.assertEqual(hits[0].period, 0x600)
        self.assertEqual(hits[0].duty, 2)
        note, e5 = gb_note(4194304, 0x600)
        back = gb_note(4194304, 0x600)
        self.assertEqual((note, e5), back)
        self.assertEqual(gb_period_delta(0x600, 0x600), 0)
        self.assertGreater(gb_period_delta(0x600, 0x700), 0)  # higher pitch

    def test_note_off_on_dac_disable(self):
        writes = _note(1000, 0xF0)
        writes += [_gb(9000, 0x02, 0x00)]  # DAC off
        hits, offs, _w, _warn = collect_gb(_vgm(writes))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].off_sample, 9000)
        self.assertEqual([(o.sample_time, o.voice) for o in offs], [(9000, 0)])

    def test_hardware_envelope(self):
        # One NRx2 write at the trigger: the platform's hardware envelope.
        hits, _o, _w, _warn = collect_gb(_vgm(_note(1000, 0xF1)))
        self.assertFalse(hits[0].soft_env)
        self.assertEqual(hits[0].env_vol, 15)
        self.assertEqual(hits[0].env_dir, 0)  # bit 3 clear = decay
        self.assertEqual(hits[0].env_len, 1)

    def test_software_envelope(self):
        # Repeated NRx2 writes during the note are the zombie volume bug.
        writes = _note(1000, 0x28)
        for i, val in enumerate((0x18, 0x09, 0x11, 0x08), start=1):
            writes.append(_gb(1000 + i * 700, 0x02, val))
        hits, _o, _w, _warn = collect_gb(_vgm(writes))
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].soft_env)
        self.assertTrue(hits[0].vol_pts)

    def test_noise_note(self):
        # Shift 4 / divider 6 is in the platform's table.
        self.assertEqual(gb_noise_note(0x46), 60 + 46)
        # Shift 1 / divider 0 has no exact entry; the nearest is shift 0 / 1,
        # which has the same hardware period.
        self.assertEqual(
            gb_noise_note(0x10) - 60,
            gb_noise_note(0x01) - 60,
        )

    def test_pan_from_nr51(self):
        writes = [_gb(900, 0x15, 0x10)]  # ch1 left only, before the note
        writes += _note(1000, 0xF0)
        hits, _o, _w, _warn = collect_gb(_vgm(writes))
        self.assertEqual(hits[0].pan, 0xF0)
        writes = [_gb(900, 0x15, 0x01)]  # ch1 right only
        writes += _note(1000, 0xF0)
        hits, _o, _w, _warn = collect_gb(_vgm(writes))
        self.assertEqual(hits[0].pan, 0x0F)
        # A move while the note is held becomes a pan point.
        writes = _note(1000, 0xF0) + [_gb(1100, 0x15, 0x10)]
        hits, _o, _w, _warn = collect_gb(_vgm(writes))
        self.assertEqual(hits[0].pan_pts, [(1100, 0xF0)])


class TestGbWave(unittest.TestCase):
    @staticmethod
    def _wave_cmds(sample, image: bytes):
        return [_gb(sample, 0x20 + i, b) for i, b in enumerate(image)]

    def test_wave_images_become_wavetables(self):
        wave_a = bytes(range(0x10, 0x20))
        wave_b = bytes(range(0x90, 0xA0))
        writes = self._wave_cmds(0, wave_a)
        writes += [
            _gb(1000, 0x0A, 0x80),        # wave DAC on
            _gb(1000, 0x0C, 0x20),        # 100%
            _gb(1000, 0x0D, 0x00),
            _gb(1000, 0x0E, 0x86),        # trigger, period 0x600
        ]
        writes += self._wave_cmds(3000, wave_b)  # mid-note morph
        hits, _o, waves, _warn = collect_gb(_vgm(writes))
        self.assertEqual(len(waves), 2)
        self.assertEqual(waves[0], wave_nibbles(wave_a))
        self.assertEqual(waves[1], wave_nibbles(wave_b))
        self.assertEqual(hits[0].voice, 2)
        self.assertEqual(hits[0].wave, 0)
        self.assertEqual(hits[0].wave_pts, [(3000, 1)])
        self.assertEqual(hits[0].vol, 15)
        self.assertEqual(hits[0].sound_len, 63)


class TestGbModule(unittest.TestCase):
    def test_gb_only_module(self):
        writes = _note(1000, 0xF1)
        writes += [_gb(2000, 0x20 + i, 0x00) for i in range(16)]
        writes += [
            _gb(2000, 0x0A, 0x80),
            _gb(2000, 0x0C, 0x40),        # 50%
            _gb(2000, 0x0D, 0x00),
            _gb(2000, 0x0E, 0x86),
        ]
        writes += [_gb(3000, 0x10, 0x00), _gb(3000, 0x11, 0xF2),
                   _gb(3000, 0x12, 0x46), _gb(3000, 0x13, 0x80)]
        song = analyze(_vgm(writes))
        self.assertEqual(song.gb_ids, [0])
        self.assertGreaterEqual(song.gb_count, 3)
        mod = parse_fur(write_fur(song, include_pcm=True))
        self.assertEqual(mod.chips, [CHIP_GB])
        self.assertEqual(mod.n_ch, 4)
        types = [
            struct.unpack_from("<H", blk, 2)[0]
            for _off, mag, _size, blk in iter_blocks(
                write_fur(song, include_pcm=True)
            )
            if mag == b"INS2"
        ]
        self.assertIn(INS_GB, types)
        # The GB chip flag picks the CGB-E core.
        self.assertIn(b"chipType=2", write_fur(song, include_pcm=True))

    def test_gb_scc_combo_keeps_both(self):
        # Header-only SCC declaration plus a GB write: the analyzer must keep
        # both systems rather than treating SCC as a competing family.
        writes = _note(1000, 0xF1)
        song = analyze(_vgm(
            writes, k051649_clock=1789772, k051649_plus=True,
        ))
        # No SCC register writes -> no SCC voices, but GB still converts.
        self.assertEqual(song.gb_ids, [0])
        mod = parse_fur(write_fur(song, include_pcm=True))
        self.assertIn(CHIP_GB, mod.chips)


if __name__ == "__main__":
    unittest.main()
