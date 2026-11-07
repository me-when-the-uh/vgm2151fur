"""Philips SAA1099: parsing, collection, pitch inversion and .fur emission."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import (
    ToneIns, _loop_saa_pitch_macros, analyze,
)
from vgm2151fur.furio import CHIP_CHANNELS, iter_blocks, parse_fur
from vgm2151fur.furwrite import CHIP_SAA1099, INS_SAA, write_fur
from vgm2151fur.pitch import furnace_period_note
from vgm2151fur.saa1099 import (
    SAA_VOICES,
    collect_saa1099,
    saa_module_clock,
    saa_note,
    saa_period_note,
    saa_period_value,
    saa_rate,
)
from vgm2151fur.vgm import ChipWrite, VgmFile, chips_in_head, load_vgm

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_saa1099"
TMP.mkdir(exist_ok=True)


def _vgm(writes, **kw):
    total = kw.pop("total_samples", 400000)
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x171,
        total_samples=total, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=[], writes=list(writes), **kw,
    )


def _raw_vgm(body: bytes, clock: int = 8_000_000, dual: bool = False) -> bytes:
    buf = bytearray(0xD0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x171).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xD0 - 0x34).to_bytes(4, "little")
    raw = clock | (0x40000000 if dual else 0)
    buf[0xC8:0xCC] = raw.to_bytes(4, "little")
    payload = bytearray(bytes(buf) + body + b"\x66")
    payload[4:8] = (len(payload) - 4).to_bytes(4, "little")
    return bytes(payload)


def _platform_pair(value: int) -> tuple[int, int]:
    """Furnace SAA platform conversion: pitch-table value -> register pair."""
    if value <= 511:
        k = 0
    else:
        k = value.bit_length() - 9
    return (0xFF - (value >> k)) & 0xFF, 7 - k


class TestSaaParsing(unittest.TestCase):
    def test_bd_command_and_header(self):
        body = bytes([
            0xBD, 0x1C, 0x01,
            0xBD, 0x00, 0x88,
            0xBD, 0x14, 0x01,
            0xBD, 0x08, 0x40,
            0xBD, 0x10, 0x07,
            0xBD, 0x80, 0x88,
            0xBD, 0x94, 0x01,
        ])
        p = TMP / "saa_header.vgm"
        p.write_bytes(_raw_vgm(body, clock=8_000_000, dual=True))
        vgm = load_vgm(p)
        self.assertEqual(vgm.saa1099_clock, 8_000_000)
        self.assertTrue(vgm.saa1099_dual)
        writes = [(w.chip_id, w.reg, w.val) for w in vgm.writes]
        self.assertIn((0, 0x1C, 1), writes)
        self.assertIn((0, 0x00, 0x88), writes)
        self.assertIn((1, 0x00, 0x88), writes)
        self.assertIn((1, 0x14, 1), writes)
        self.assertIn("saa1099", chips_in_head(p.read_bytes()))

    def test_single_chip_header(self):
        p = TMP / "saa_single.vgm"
        p.write_bytes(_raw_vgm(b"\xBD\x00\x0F", clock=8_000_000))
        vgm = load_vgm(p)
        self.assertEqual(vgm.saa1099_clock, 8_000_000)
        self.assertFalse(vgm.saa1099_dual)


class TestSaaPitch(unittest.TestCase):
    def test_every_register_pair_round_trips(self):
        for octave in range(8):
            for offset in range(256):
                value = saa_period_value(offset, octave)
                note, e5 = saa_period_note(8_000_000, value)
                table_value = _value_for(note, e5)
                # The platform's truncation must land back on the source pair.
                self.assertEqual(
                    _platform_pair(table_value), (offset, octave),
                )

    def test_rate_formula(self):
        # offset 255, octave 7 at 8 MHz is the top half-cycle rate.
        self.assertAlmostEqual(saa_rate(8_000_000, 255, 7), 7812.5, places=3)
        self.assertAlmostEqual(
            saa_rate(8_000_000, 0, 0), 15625.0 / 511.0, places=3,
        )


def _value_for(note: int, e5: int) -> int:
    from vgm2151fur.pitch import pitch_table
    return pitch_table(saa_module_clock(8_000_000), 2, maximum=0xFFFF).value(
        note, e5 - 0x80,
    )


class TestSaaPitchLoop(unittest.TestCase):
    def test_periodic_macro_loops_with_other_macros(self):
        pattern = (0, 4, 8, 12, 8, 4)
        ins = ToneIns(
            pitch=pattern * 3,
            pitch_loop=255,
            panl=(1,),
            panr=(2,),
            mode=(3,),
            env=(0x8A,),
        )
        bucket = [ins]
        _loop_saa_pitch_macros(bucket)
        self.assertEqual(bucket[0].pitch, pattern)
        self.assertEqual(bucket[0].pitch_loop, 0)
        # The other macros are untouched.
        self.assertEqual(bucket[0].panl, (1,))
        self.assertEqual(bucket[0].mode, (3,))
        self.assertEqual(bucket[0].env, (0x8A,))

    def test_periodic_tail_keeps_its_lead_in(self):
        lead = (9,)
        pattern = (0, 4, 8, 12, 8, 4)
        ins = ToneIns(pitch=lead + pattern * 3, pitch_loop=255)
        bucket = [ins]
        _loop_saa_pitch_macros(bucket)
        self.assertEqual(bucket[0].pitch, lead + pattern)
        self.assertEqual(bucket[0].pitch_loop, 1)


class TestSaaCollect(unittest.TestCase):
    def _song(self, writes, dual=False):
        return _vgm(
            writes, saa1099_clock=8_000_000, saa1099_dual=dual,
        )

    def test_tone_note_and_pan_nibbles(self):
        writes = [
            ChipWrite(0, "saa1099", 0, 0x1C, 1),
            ChipWrite(0, "saa1099", 0, 0x14, 1),
            ChipWrite(0, "saa1099", 0, 0x00, 0xF0),
            ChipWrite(0, "saa1099", 0, 0x08, 0),
            ChipWrite(0, "saa1099", 0, 0x10, 7),
            ChipWrite(1000, "saa1099", 0, 0x00, 0),
        ]
        hits, offs, _warn = collect_saa1099(self._song(writes))
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual((h.left, h.right), (0, 15))
        self.assertTrue(h.tone)
        self.assertFalse(h.noise)

    def test_same_sample_writes_use_the_final_value(self):
        writes = [
            ChipWrite(0, "saa1099", 0, 0x1C, 1),
            ChipWrite(0, "saa1099", 0, 0x14, 1),
            ChipWrite(0, "saa1099", 0, 0x00, 0x88),
            ChipWrite(0, "saa1099", 0, 0x00, 0x44),
            ChipWrite(1000, "saa1099", 0, 0x00, 0),
        ]
        hits, _offs, _warn = collect_saa1099(self._song(writes))
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0].left, hits[0].right), (4, 4))

    def test_noise_generator_and_mode(self):
        writes = [
            ChipWrite(0, "saa1099", 0, 0x1C, 1),
            ChipWrite(0, "saa1099", 0, 0x15, 0x04),
            ChipWrite(0, "saa1099", 0, 0x16, 0x02),
            ChipWrite(0, "saa1099", 0, 0x02, 0x0F),
            ChipWrite(0, "saa1099", 0, 0x0A, 0x40),
            ChipWrite(0, "saa1099", 0, 0x11, 0x07),
            ChipWrite(1000, "saa1099", 0, 0x02, 0),
        ]
        hits, _offs, _warn = collect_saa1099(self._song(writes))
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertFalse(h.tone)
        self.assertTrue(h.noise)
        self.assertEqual(h.noise_freq, 2)  # low nibble of 0x02 (group 0)

    def test_amplitude_retrigger(self):
        writes = [
            ChipWrite(0, "saa1099", 0, 0x1C, 1),
            ChipWrite(0, "saa1099", 0, 0x14, 1),
            ChipWrite(0, "saa1099", 0, 0x00, 0x99),
            ChipWrite(1000, "saa1099", 0, 0x00, 0),
            ChipWrite(2000, "saa1099", 0, 0x00, 0x99),
            ChipWrite(3000, "saa1099", 0, 0x00, 0),
        ]
        hits, _offs, _warn = collect_saa1099(self._song(writes))
        self.assertEqual(len(hits), 2)

    def test_dual_chip_state_is_independent(self):
        writes = [
            ChipWrite(0, "saa1099", 0, 0x1C, 1),
            ChipWrite(0, "saa1099", 1, 0x1C, 1),
            ChipWrite(0, "saa1099", 0, 0x14, 1),
            ChipWrite(0, "saa1099", 1, 0x14, 1),
            ChipWrite(0, "saa1099", 0, 0x00, 0x0F),
            ChipWrite(0, "saa1099", 1, 0x00, 0xF0),
            ChipWrite(1000, "saa1099", 0, 0x00, 0),
            ChipWrite(1000, "saa1099", 1, 0x00, 0),
        ]
        hits, _offs, _warn = collect_saa1099(self._song(writes, dual=True))
        self.assertEqual({h.chip_id for h in hits}, {0, 1})
        self.assertEqual(
            {(h.chip_id, h.left, h.right) for h in hits},
            {(0, 15, 0), (1, 0, 15)},
        )

    def test_envelope_source_gets_a_silent_note(self):
        writes = [
            ChipWrite(0, "saa1099", 0, 0x1C, 1),
            # Group 0 source voice is 1; leave its amplitude at zero but give
            # it a frequency and enable the internal envelope.
            ChipWrite(0, "saa1099", 0, 0x09, 0x40),
            ChipWrite(0, "saa1099", 0, 0x10, 0x07),
            ChipWrite(0, "saa1099", 0, 0x18, 0x8A),
            # Output voice 2 is audible.
            ChipWrite(0, "saa1099", 0, 0x14, 0x04),
            ChipWrite(0, "saa1099", 0, 0x02, 0xFF),
            ChipWrite(0, "saa1099", 0, 0x0A, 0x40),
            ChipWrite(0, "saa1099", 0, 0x11, 0x07),
            ChipWrite(1000, "saa1099", 0, 0x02, 0),
        ]
        hits, _offs, _warn = collect_saa1099(self._song(writes))
        hidden = [h for h in hits if h.hidden]
        normal = [h for h in hits if not h.hidden]
        self.assertEqual(len(hidden), 1)
        self.assertEqual(hidden[0].voice, 1)
        self.assertEqual(len(normal), 1)
        self.assertEqual(normal[0].voice, 2)
        self.assertEqual(normal[0].env, 0x8A)


class TestSaaFurEmission(unittest.TestCase):
    def _song(self, dual=False):
        writes = [
            ChipWrite(0, "saa1099", 0, 0x1C, 1),
            ChipWrite(0, "saa1099", 0, 0x14, 1),
            ChipWrite(0, "saa1099", 0, 0x00, 0x88),
            ChipWrite(0, "saa1099", 0, 0x08, 0x40),
            ChipWrite(0, "saa1099", 0, 0x10, 0x07),
            ChipWrite(735, "saa1099", 0, 0x00, 0x44),
            ChipWrite(1470, "saa1099", 0, 0x00, 0),
        ]
        if dual:
            writes += [
                ChipWrite(0, "saa1099", 1, 0x1C, 1),
                ChipWrite(0, "saa1099", 1, 0x14, 1),
                ChipWrite(0, "saa1099", 1, 0x00, 0x0F),
                ChipWrite(0, "saa1099", 1, 0x08, 0x40),
                ChipWrite(0, "saa1099", 1, 0x10, 0x07),
                ChipWrite(1470, "saa1099", 1, 0x00, 0),
            ]
        return analyze(_vgm(
            writes, saa1099_clock=8_000_000, saa1099_dual=dual,
            total_samples=2205,
        ))

    def test_single_chip_module(self):
        song = self._song()
        self.assertEqual(song.saa_ids, [0])
        self.assertEqual(song.saa_count, 1)
        self.assertEqual(song.speed, 1)
        self.assertAlmostEqual(song.hz, 60.0)
        data = write_fur(song, include_fm=False, include_pcm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_SAA1099])
        self.assertEqual(CHIP_CHANNELS[CHIP_SAA1099], SAA_VOICES)
        self.assertEqual(mod.n_ch, SAA_VOICES)
        ins_types = [
            struct.unpack_from("<H", block, 2)[0]
            for _off, mag, _size, block in iter_blocks(data) if mag == b"INS2"
        ]
        self.assertIn(INS_SAA, ins_types)
        # The note's volume column is 15; the raw amplitude is in the pan
        # macros.
        notes = [e for e in song.events if e.on and e.ch >= 8]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].vol, 15)
        self.assertEqual(notes[0].pan, 0)
        self.assertEqual(notes[0].tone_amp_pts[0], (notes[0].sample, 8, 8))

    def test_dual_chip_module(self):
        song = self._song(dual=True)
        data = write_fur(song, include_fm=False, include_pcm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_SAA1099, CHIP_SAA1099])
        self.assertEqual(mod.n_ch, 12)

    @unittest.skipUnless(
        __import__("vgm2151fur.furnace", fromlist=["find_furnace"]).find_furnace(),
        "Furnace console build not available",
    )
    def test_furnace_round_trip(self):
        from vgm2151fur.furnace import export_vgm
        song = self._song(dual=True)
        fur = TMP / "saa_roundtrip.fur"
        out = TMP / "saa_roundtrip.vgm"
        fur.write_bytes(write_fur(song, include_fm=False, include_pcm=False))
        export_vgm(fur, out)
        vgm = load_vgm(out)
        self.assertEqual(vgm.saa1099_clock, 8_000_000)
        self.assertTrue(vgm.saa1099_dual)
        state = {}
        for w in vgm.writes:
            if w.sample <= 0:
                state[(w.chip_id, w.reg)] = w.val
        self.assertEqual(state.get((0, 0x00)), 0x88)
        self.assertEqual(state.get((1, 0x00)), 0x0F)


if __name__ == "__main__":
    unittest.main()
