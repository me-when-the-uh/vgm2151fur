"""K051649/K052539 (SCC) and AY-8910 support: parsing, collection, .fur emission."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.ay8910 import ay_note, ay_rate, collect_ay8910
from vgm2151fur.furio import CHIP_CHANNELS, iter_blocks, parse_fur
from vgm2151fur.furwrite import (
    CHIP_AY8910, CHIP_K051649, CHIP_K052539, INS_AY, INS_SCC, write_fur,
)
from vgm2151fur.pitch import PeriodPitchTable, furnace_period_note
from vgm2151fur.scc import (
    SCC_VOICES, collect_scc, scc_note, scc_rate,
)
from vgm2151fur.vgm import ChipWrite, VgmFile, chips_in_head, load_vgm

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_scc_ay"
TMP.mkdir(exist_ok=True)


def _vgm(writes, **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=[], writes=list(writes), **kw,
    )


def _raw_vgm(body: bytes, scc_clock: int = 0, ay_clock: int = 0,
             plus: bool = False) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    if ay_clock:
        buf[0x74:0x78] = ay_clock.to_bytes(4, "little")
        buf[0x78] = 0
    if scc_clock:
        raw = scc_clock | (0x80000000 if plus else 0)
        buf[0x9C:0xA0] = raw.to_bytes(4, "little")
    payload = bytes(buf) + body + b"\x66"
    payload = bytearray(payload)
    payload[4:8] = (len(payload) - 4).to_bytes(4, "little")
    return bytes(payload)


def _scc_wave(offset: int, data: bytes) -> list[ChipWrite]:
    return [
        ChipWrite(sample=0, chip="k051649", chip_id=0, reg=offset + i, val=b)
        for i, b in enumerate(data)
    ]


def _sine() -> bytes:
    import math
    return bytes(int(round(127 * math.sin(2 * math.pi * i / 32))) & 0xFF for i in range(32))


SINE = _sine()


class TestSccParsing(unittest.TestCase):
    def test_d2_command_and_header(self):
        body = bytes([
            0xD2, 0, 0, 0x00, 0xD2, 0, 1, 0x40,      # wave voice 0
            0xD2, 1, 0, 0xFD, 0xD2, 1, 1, 0x00,      # period 253
            0xD2, 2, 0, 0x0F,                        # volume
            0xD2, 3, 0, 0x01,                        # enable voice 0
        ])
        p = TMP / "scc_header.vgm"
        p.write_bytes(_raw_vgm(body, scc_clock=1789773))
        vgm = load_vgm(p)
        self.assertEqual(vgm.k051649_clock, 1789773)
        self.assertFalse(vgm.k051649_plus)
        writes = [(w.reg, w.val) for w in vgm.writes if w.chip == "k051649"]
        self.assertIn((0x000, 0x00), writes)
        self.assertIn((0x100, 0xFD), writes)
        self.assertIn((0x200, 0x0F), writes)
        self.assertIn((0x300, 0x01), writes)

    def test_plus_flag_in_clock_field(self):
        p = TMP / "scc_plus.vgm"
        p.write_bytes(_raw_vgm(b"\x66", scc_clock=1789773, plus=True))
        vgm = load_vgm(p)
        self.assertTrue(vgm.k051649_plus)
        kinds = chips_in_head(p.read_bytes())
        self.assertIn("k051649", kinds)
        self.assertIn("scc_plus", kinds)

    def test_ay_command_and_header(self):
        body = bytes([0xA0, 7, 0xFE, 0xA0, 8, 0x0F, 0xA0, 0, 0xFE, 0xA0, 1, 0x00])
        p = TMP / "ay_header.vgm"
        p.write_bytes(_raw_vgm(body, ay_clock=1789773))
        vgm = load_vgm(p)
        self.assertEqual(vgm.ay8910_clock, 1789773)
        self.assertEqual(vgm.ay8910_type, 0)
        writes = [(w.reg, w.val) for w in vgm.writes if w.chip == "ay8910"]
        self.assertEqual(writes[0], (7, 0xFE))
        self.assertIn("ay8910", chips_in_head(p.read_bytes()))

    def test_multi_pcm_is_not_a0(self):
        # 0xA0 is the AY-8910 register write; MultiPCM is 0xB5.
        from vgm2151fur.vgm import UNCONVERTED_SAMPLE_CMDS
        self.assertNotIn(0xA0, UNCONVERTED_SAMPLE_CMDS)
        self.assertEqual(UNCONVERTED_SAMPLE_CMDS.get(0xB5), "MultiPCM")


class TestSccCollect(unittest.TestCase):
    def _song(self, period=253, volume=15, plus=False, wave=SINE):
        writes = _scc_wave(0, wave)
        writes += [
            ChipWrite(0, "k051649", 0, 0x100, period & 0xFF),
            ChipWrite(0, "k051649", 0, 0x101, period >> 8),
            ChipWrite(0, "k051649", 0, 0x200, volume),
            ChipWrite(100, "k051649", 0, 0x300, 0x01),
            ChipWrite(5000, "k051649", 0, 0x300, 0x00),
        ]
        return _vgm(writes, k051649_clock=1789772, k051649_plus=plus)

    def test_note_and_gate(self):
        notes, offs, waves, _warn = collect_scc(self._song())
        self.assertEqual(len(notes), 1)
        hit = notes[0].hit
        self.assertEqual(hit.voice, 0)
        self.assertEqual(hit.period, 253)
        self.assertEqual(hit.vol, 15)
        self.assertEqual(hit.sample_time, 100)
        self.assertEqual(len(offs), 1)
        self.assertEqual(offs[0].sample_time, 5000)
        self.assertEqual(scc_rate(1789772, 253), 1789772 / (16 * 254))
        note, e5 = scc_note(1789772, 253)
        self.assertGreaterEqual(note, 0)
        self.assertLessEqual(note, 179)
        # the exported register is the note's pitch value minus one
        table = PeriodPitchTable(440.0, 1789772.0, 16)
        self.assertEqual(table.value(note, e5 - 0x80) - 1, 253)

    def test_period_below_nine_is_silent(self):
        # The chip halts a voice at period <= 8; the collector still reports
        # the gate (the key-off matters), the analyzer drops the note.
        vgm = self._song(period=5)
        notes, _offs, _waves, _warn = collect_scc(vgm)
        self.assertEqual(len(notes), 1)
        song = analyze(vgm)
        self.assertEqual(song.scc_count, 0)
        self.assertFalse([e for e in song.events if e.on and e.ch >= 8])
        self.assertTrue([e for e in song.events if not e.on and e.ch >= 8])

    def test_shared_wave_on_k051649(self):
        # A write at 0x60 reaches voices 3 and 4 on the 5-voice chip.
        notes, _offs, waves, _warn = collect_scc(self._song(plus=False))
        self.assertEqual(len(waves), 1)
        # On the K052539 the same address is voice 3's own wave only.
        writes = _scc_wave(0x60, SINE) + [
            ChipWrite(0, "k051649", 0, 0x100, 253 & 0xFF),
            ChipWrite(0, "k051649", 0, 0x101, 253 >> 8),
            ChipWrite(0, "k051649", 0, 0x203, 15),
            ChipWrite(100, "k051649", 0, 0x300, 0x08),  # voice 3
        ]
        vgm = _vgm(writes, k051649_clock=1789772, k051649_plus=False)
        notes, _offs, _waves, _warn = collect_scc(vgm)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].hit.voice, 3)
        self.assertEqual(notes[0].hit.wave, SINE)

    def test_plus_voice4_wave_port(self):
        writes = _scc_wave(0x80, SINE)
        writes += [
            ChipWrite(0, "k051649", 0, 0x108, 253 & 0xFF),
            ChipWrite(0, "k051649", 0, 0x109, 253 >> 8),
            ChipWrite(0, "k051649", 0, 0x204, 15),
            ChipWrite(100, "k051649", 0, 0x300, 0x10),  # voice 4
        ]
        vgm = _vgm(writes, k051649_clock=1789772, k051649_plus=True)
        notes, _offs, _waves, _warn = collect_scc(vgm)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].hit.voice, 4)
        self.assertEqual(notes[0].hit.wave, SINE)


class TestAyCollect(unittest.TestCase):
    def _writes(self, *, mixer=0xFE, volume=0x0F, period=254, noise=0,
                env_shape=None, gate_at=100, off_at=5000):
        writes = [
            ChipWrite(0, "ay8910", 0, 7, mixer),
            ChipWrite(0, "ay8910", 0, 6, noise),
            ChipWrite(0, "ay8910", 0, 0, period & 0xFF),
            ChipWrite(0, "ay8910", 0, 1, period >> 8),
        ]
        if env_shape is not None:
            writes += [
                ChipWrite(0, "ay8910", 0, 11, 66),        # envelope period lo
                ChipWrite(0, "ay8910", 0, 12, 0),
                ChipWrite(0, "ay8910", 0, 13, env_shape),
            ]
        writes.append(ChipWrite(gate_at, "ay8910", 0, 8, volume))
        writes.append(ChipWrite(off_at, "ay8910", 0, 8, 0))
        return writes

    def test_tone_note(self):
        vgm = _vgm(self._writes(), ay8910_clock=1789773)
        hits, offs, _warn = collect_ay8910(vgm)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h.voice, 0)
        self.assertEqual(h.period, 254)
        self.assertEqual(h.vol, 15)
        self.assertEqual(h.tone, True)
        self.assertEqual(h.noise, False)
        self.assertEqual(len(offs), 1)
        self.assertAlmostEqual(ay_rate(1789773, 254), 1789773 / (16 * 254))
        note, e5 = ay_note(1789773, 254)
        table = PeriodPitchTable(440.0, 1789773.0, 8)
        self.assertEqual(table.value(note, e5 - 0x80), 254)

    def test_noise_note(self):
        vgm = _vgm(self._writes(mixer=0xF7, noise=8), ay8910_clock=1789773)
        hits, _offs, _warn = collect_ay8910(vgm)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].noise)
        self.assertFalse(hits[0].tone)
        fx = {cmd: val for _t, cmd, val in hits[0].fx_pts}
        self.assertEqual(fx[0x20], 1)   # mode: noise only (value = mode - 1)
        self.assertEqual(fx[0x21], 31 - 8)

    def test_envelope_note(self):
        vgm = _vgm(
            self._writes(volume=0x12, env_shape=9), ay8910_clock=1789773
        )
        hits, _offs, _warn = collect_ay8910(vgm)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertTrue(h.env)
        fx = [(cmd, val) for _t, cmd, val in h.fx_pts]
        self.assertIn((0x22, (9 << 4) | 1), fx)  # shape 9, enable this channel
        self.assertIn((0x23, 66), fx)
        self.assertIn((0x24, 0), fx)

    def test_volume_gate_retrigger(self):
        writes = [
            ChipWrite(0, "ay8910", 0, 7, 0xFE),
            ChipWrite(0, "ay8910", 0, 0, 254),
            ChipWrite(0, "ay8910", 0, 1, 0),
            ChipWrite(100, "ay8910", 0, 8, 15),
            ChipWrite(3000, "ay8910", 0, 8, 0),
            ChipWrite(4000, "ay8910", 0, 8, 12),
            ChipWrite(8000, "ay8910", 0, 8, 0),
        ]
        hits, _offs, _warn = collect_ay8910(_vgm(writes, ay8910_clock=1789773))
        self.assertEqual([h.sample_time for h in hits], [100, 4000])
        self.assertEqual([h.vol for h in hits], [15, 12])


class TestFurEmission(unittest.TestCase):
    def _scc_song(self, plus=True):
        writes = _scc_wave(0, SINE)
        writes += _scc_wave(0x20, bytes(reversed(SINE)))
        writes += [
            ChipWrite(0, "k051649", 0, 0x100, 253 & 0xFF),
            ChipWrite(0, "k051649", 0, 0x101, 253 >> 8),
            ChipWrite(0, "k051649", 0, 0x102, 672 & 0xFF),
            ChipWrite(0, "k051649", 0, 0x103, 672 >> 8),
            ChipWrite(0, "k051649", 0, 0x200, 15),
            ChipWrite(0, "k051649", 0, 0x201, 12),
            ChipWrite(100, "k051649", 0, 0x300, 0x01),
            ChipWrite(20000, "k051649", 0, 0x200, 8),
            ChipWrite(30000, "k051649", 0, 0x300, 0x00),
            ChipWrite(40000, "k051649", 0, 0x300, 0x02),
            ChipWrite(60000, "k051649", 0, 0x300, 0x00),
        ]
        return _vgm(writes, k051649_clock=1789772, k051649_plus=plus)

    def test_scc_module(self):
        song = analyze(self._scc_song(plus=True))
        self.assertEqual(song.scc_ids, [0])
        self.assertGreaterEqual(len(song.scc_waves), 2)
        self.assertEqual(song.scc_count, 2)
        notes = [e for e in song.events if e.on and e.ch >= 8]
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0].ch, 8)
        self.assertEqual(notes[0].vol, 15)
        data = write_fur(song, include_pcm=True, include_fm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_K052539])
        self.assertEqual(CHIP_CHANNELS[CHIP_K052539], SCC_VOICES)
        self.assertEqual(mod.n_ch, SCC_VOICES)
        ins_types = [
            struct.unpack_from("<H", block, 2)[0]
            for _off, mag, _size, block in iter_blocks(data) if mag == b"INS2"
        ]
        self.assertIn(INS_SCC, ins_types)
        self.assertEqual(ins_types.count(INS_SCC), len(song.scc_waves))
        # wave blocks carry the unsigned image (signed + 128)
        waves = [
            block for _off, mag, _size, block in iter_blocks(data) if mag == b"WAVE"
        ]
        self.assertEqual(len(waves), len(song.scc_waves))
        self.assertEqual(
            _wave_values(waves[0]), [(b + 128) & 0xFF for b in SINE]
        )

    def test_scc_nonplus_chip_byte(self):
        song = analyze(self._scc_song(plus=False))
        data = write_fur(song, include_pcm=True, include_fm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_K051649])

    def test_ay_module(self):
        writes = [
            ChipWrite(0, "ay8910", 0, 7, 0xFE),
            ChipWrite(0, "ay8910", 0, 0, 254),
            ChipWrite(0, "ay8910", 0, 1, 0),
            ChipWrite(0, "ay8910", 0, 11, 66),
            ChipWrite(0, "ay8910", 0, 12, 0),
            ChipWrite(0, "ay8910", 0, 13, 9),
            ChipWrite(100, "ay8910", 0, 8, 0x12),
            ChipWrite(10000, "ay8910", 0, 8, 0),
            ChipWrite(12000, "ay8910", 0, 8, 9),
            ChipWrite(20000, "ay8910", 0, 8, 0),
        ]
        song = analyze(_vgm(writes, ay8910_clock=1789773))
        self.assertEqual(song.ay_ids, [0])
        self.assertEqual(song.ay_count, 2)
        data = write_fur(song, include_pcm=True, include_fm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_AY8910])
        self.assertEqual(mod.n_ch, 3)
        ins_types = [
            struct.unpack_from("<H", block, 2)[0]
            for _off, mag, _size, block in iter_blocks(data) if mag == b"INS2"
        ]
        self.assertEqual(ins_types, [INS_AY])
        # the phase-reset macro must be in the instrument (file code 14)
        ins = [block for _off, mag, _size, block in iter_blocks(data) if mag == b"INS2"][0]
        self.assertIn(bytes([14, 2, 255, 255, 0, 1, 0, 1, 1, 0]), ins)
        # the envelope effects reach the pattern
        fx = [fx for rows in mod.patterns.values() for row in rows for fx in row.fx]
        cmds = {cmd for cmd, _val in fx}
        self.assertIn(0x22, cmds)
        self.assertIn(0x23, cmds)
        self.assertIn(0x24, cmds)


def _wave_values(block: bytes) -> list[int]:
    name_end = block.index(0)
    pos = name_end + 1
    length = struct.unpack_from("<I", block, pos)[0]
    pos += 4
    pos += 4  # min
    pos += 4  # max
    return [struct.unpack_from("<I", block, pos + 4 * i)[0] for i in range(length)]


class TestToneMacros(unittest.TestCase):
    """Mid-note pitch motion and waveform morphs become instrument macros."""

    def _bend_song(self):
        writes = _scc_wave(0, SINE)
        writes += [
            ChipWrite(0, "k051649", 0, 0x100, 3008 & 0xFF),
            ChipWrite(0, "k051649", 0, 0x101, 3008 >> 8),
            ChipWrite(0, "k051649", 0, 0x200, 12),
            ChipWrite(100, "k051649", 0, 0x300, 0x01),
        ]
        # bend away and come back: the pair (low, high) is one value
        for t, per in ((44100, 3400), (88200, 3008)):
            writes += [
                ChipWrite(t, "k051649", 0, 0x100, per & 0xFF),
                ChipWrite(t, "k051649", 0, 0x101, per >> 8),
            ]
        writes.append(ChipWrite(132300, "k051649", 0, 0x300, 0x00))
        return _vgm(writes, k051649_clock=1789772, k051649_plus=True)

    def test_pitch_bend_macro(self):
        song = analyze(self._bend_song())
        note = [e for e in song.events if e.on and e.tone_chip == 1][0]
        self.assertEqual(len(song.scc_ins), 1)
        spec = song.scc_ins[0]
        self.assertTrue(spec.pitch)
        self.assertEqual(spec.pitch[0], 0)  # starts on the note's own pitch
        self.assertEqual(spec.pitch[-1], 0)  # and comes back to it
        self.assertLess(min(spec.pitch), -100)  # the bend is in there
        # the collector keeps a step back to the note's own period
        self.assertEqual(note.tone_pitch_pts[-1][1], 3008)

    def test_wave_morph_macro(self):
        writes = _scc_wave(0, SINE)
        writes += [
            ChipWrite(0, "k051649", 0, 0x100, 252 & 0xFF),
            ChipWrite(0, "k051649", 0, 0x101, 252 >> 8),
            ChipWrite(0, "k051649", 0, 0x200, 12),
            ChipWrite(100, "k051649", 0, 0x300, 0x01),
        ]
        # the driver rewrites the whole image, one byte-burst per morph step
        for k, shift in enumerate((2, 4, 6)):
            t = 22050 + k * 22050
            ram = bytes(((b + shift) & 0xFF) for b in SINE)
            writes += _scc_wave_at(t, 0, ram)
        writes.append(ChipWrite(200000, "k051649", 0, 0x300, 0x00))
        song = analyze(_vgm(writes, k051649_clock=1789772, k051649_plus=True))
        self.assertGreaterEqual(len(song.scc_waves), 4)  # the morph images
        spec = [s for s in song.scc_ins if s.waves]
        self.assertTrue(spec, "no wave macro was written")
        macro = spec[0].waves
        self.assertGreaterEqual(len(set(macro)), 4)
        self.assertEqual(macro[0], 0)  # starts on the note's own image
        data = write_fur(song, include_pcm=True, include_fm=False)
        waves = [b for _o, m, _s, b in iter_blocks(data) if m == b"WAVE"]
        self.assertEqual(len(waves), len(song.scc_waves))
        # the instrument carries the wave macro (file code 3)
        ins = [b for _o, m, _s, b in iter_blocks(data) if m == b"INS2"]
        self.assertTrue(any(b"\x03" in block for block in ins))

    def test_ay_silence_gate_has_no_note(self):
        # A driver mutes a voice with an inaudible tone period; that is not a
        # note (and it must not become a high blip).
        from vgm2151fur.ay8910 import ay_min_period
        lo = ay_min_period(1789772)
        writes = [
            ChipWrite(0, "ay8910", 0, 7, 0xFE),   # both on
            ChipWrite(0, "ay8910", 0, 0, 254),
            ChipWrite(0, "ay8910", 0, 1, 0),
            ChipWrite(100, "ay8910", 0, 8, 12),   # audible note
            ChipWrite(2000, "ay8910", 0, 0, 1),   # mute: period below range
            ChipWrite(2000, "ay8910", 0, 1, 0),
            ChipWrite(2000, "ay8910", 0, 8, 2),
            ChipWrite(4000, "ay8910", 0, 8, 0),
        ]
        hits, offs, _warn = collect_ay8910(_vgm(writes, ay8910_clock=1789772))
        self.assertGreater(lo, 1)
        self.assertEqual([h.sample_time for h in hits], [100])
        self.assertEqual([o.sample_time for o in offs], [2000])


def _scc_wave_at(sample: int, offset: int, data: bytes) -> list[ChipWrite]:
    return [
        ChipWrite(sample, "k051649", 0, offset + i, b) for i, b in enumerate(data)
    ]


class TestEmissionHelpers(unittest.TestCase):
    def test_scc_wave_values_unsigned(self):
        block = b"\x00" + struct.pack("<I", 3) + struct.pack("<I", 0) + struct.pack("<I", 255)
        block += b"".join(struct.pack("<I", v) for v in (0, 128, 255))
        self.assertEqual(_wave_values(block), [0, 128, 255])


if __name__ == "__main__":
    unittest.main()
