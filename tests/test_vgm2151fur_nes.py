"""NES APU tone/DPCM and Konami VRC7 detection/emission."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import NoteEvent, _absorb_detune, analyze
from vgm2151fur.furio import iter_blocks, parse_fur
from vgm2151fur.furwrite import (
    CHIP_NES, CHIP_VRC7, INS_NES, SMP_DEPTH_DPCM, write_fur,
)
from vgm2151fur.nes import (
    collect_nes_dpcm, collect_nes_pcm, collect_nes_tones, nes_note,
)
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile, load_vgm

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_nes"
TMP.mkdir(exist_ok=True)


def _vgm(writes, roms=(), **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=list(roms), writes=list(writes), **kw,
    )


def _raw_vgm(cmds: bytes, rom_blocks=(), nes_clock: int = 1789773,
             ym2413: int = 0) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x10:0x14] = (ym2413 & 0xFFFFFFFF).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    # The NES clock lives at 0x84; 0x80 is the Game Boy DMG clock.
    buf[0x84:0x88] = (nes_clock & 0xFFFFFFFF).to_bytes(4, "little")
    body = bytearray()
    for typ, start, data in rom_blocks:
        # Database blocks (< 0x40) carry no start address; ROM/RAM dumps do.
        payload = data if typ < 0x40 else (
            (start & 0xFFFF).to_bytes(2, "little") + data
        )
        body += (
            bytes([0x67, 0x66, typ])
            + len(payload).to_bytes(4, "little") + payload
        )
    body += cmds + b"\x66"
    return bytes(buf) + bytes(body)


def _nes(sample, reg, val, chip_id=0):
    return ChipWrite(sample, "nes", chip_id, reg, val)


class TestNesParsing(unittest.TestCase):
    def test_register_and_dpcm_ram(self):
        raw = _raw_vgm(
            bytes([0xB4, 0x00, 0x30, 0xB4, 0x15, 0x1F]),
            [(0xC2, 0xC000, bytes(range(64)))],
        )
        p = TMP / "parse_nes.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.nes_clock, 1789773)
        self.assertEqual(
            [(w.reg, w.val) for w in v.writes if w.chip == "nes"],
            [(0x00, 0x30), (0x15, 0x1F)],
        )
        self.assertEqual([r.kind for r in v.roms], ["nes_dpcm"])
        self.assertEqual(v.roms[0].start, 0xC000)
        self.assertEqual(len(v.roms[0].data), 64)

    def test_vrc7_bit(self):
        raw = _raw_vgm(B"", ym2413=0x80000000 | 3579545)
        p = TMP / "vrc7.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertTrue(v.ym2413_vrc7)
        self.assertEqual(v.ym2413_clock, 3579545)

        raw = _raw_vgm(B"", ym2413=3579545)
        p = TMP / "ym2413.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertFalse(v.ym2413_vrc7)


class TestDpcmBank(unittest.TestCase):
    def test_type07_bank_and_68_ram_write(self):
        # A 4 KB window copied from the type-0x07 database into $C000.
        raw = _raw_vgm(
            bytes([
                0x68, 0x00, 0x07, 0x00, 0x00, 0x00,  # type 7, src 0
                0x00, 0xC0, 0x00,                    # dest $C000
                0x04, 0x00, 0x00,                    # 4 bytes
                0xB4, 0x10, 0x0E, 0xB4, 0x12, 0x00,
                0xB4, 0x13, 0x00, 0xB4, 0x15, 0x1F,
            ]),
            [(0x07, 0x0000, bytes([0xAA, 0x55, 0x01, 0x80]))],
        )
        p = TMP / "dpcm_bank.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(bytes(v.nes_dpcm_bank[:4]), bytes([0xAA, 0x55, 0x01, 0x80]))
        self.assertEqual(len(v.pcm_ram_writes), 1)
        samples, hits, warn = collect_nes_dpcm(v)
        self.assertEqual(len(samples), 1)
        # $4013 = 0 is the minimum DMC length: one byte.
        self.assertEqual(samples[0].data, b"\xAA")
        self.assertEqual(len(hits), 1)
        self.assertEqual(warn, [])

    def test_dpcm_ram_reload_follows_stream_time(self):
        # Gimmick! re-dumps $C000/$D000 while the song plays. A hit must read
        # the window that was loaded at that point, not the last one in the
        # file (which made most DPCM notes play the wrong sample).
        def dump_block(data, addr=0xC000):
            payload = addr.to_bytes(2, "little") + data
            return (
                bytes([0x67, 0x66, 0xC2])
                + len(payload).to_bytes(4, "little") + payload
            )

        cmds = bytearray([
            0xB4, 0x10, 0x0E, 0xB4, 0x12, 0x00,
            0xB4, 0x13, 0x00, 0xB4, 0x15, 0x1F,   # hit 1 at t=0
            0x61, 0x10, 0x00,                     # wait 16 samples
        ])
        cmds += dump_block(bytes([0x55]) * 64)    # reload at t=16
        cmds += bytes([
            0xB4, 0x15, 0x0F,                     # stop
            0xB4, 0x13, 0x00, 0xB4, 0x15, 0x1F,   # hit 2 at t=16
        ])
        raw = _raw_vgm(
            bytes(cmds), [(0xC2, 0xC000, bytes([0xAA]) * 64)],
        )
        p = TMP / "dpcm_reload.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual([r.time for r in v.roms], [0, 16])
        samples, hits, warn = collect_nes_dpcm(v)
        self.assertEqual([h.sample_time for h in hits], [0, 16])
        self.assertEqual([s.data for s in samples], [b"\xAA", b"\x55"])
        self.assertEqual(warn, [])

    def test_trigger_without_data_warns(self):
        raw = _raw_vgm(bytes([
            0xB4, 0x10, 0x0E, 0xB4, 0x12, 0x00,
            0xB4, 0x13, 0x00, 0xB4, 0x15, 0x1F,
        ]))
        p = TMP / "dpcm_nodata.vgm"
        p.write_bytes(raw)
        samples, hits, warn = collect_nes_dpcm(load_vgm(p))
        self.assertEqual(len(hits), 1)
        self.assertTrue(any("no DPCM data block" in w for w in warn))


class TestSunsoft5b(unittest.TestCase):
    def _song(self, nes: bool):
        period = 0x100
        writes = [
            ChipWrite(0, "ay8910", 0, 7, 0x38),
            ChipWrite(0, "ay8910", 0, 0, period & 0xFF),
            ChipWrite(0, "ay8910", 0, 1, (period >> 8) & 0x0F),
            ChipWrite(0, "ay8910", 0, 8, 0x0F),
        ]
        if nes:
            writes.append(ChipWrite(0, "nes", 0, 0x15, 0x00))
        # No pin-26 flag: the Famicom pairing alone infers the 5B.
        return analyze(_vgm(
            writes, ay8910_clock=1789773, ay8910_type=0x10,
            ay8910_flags=0, nes_clock=1789773 if nes else 0,
        ))

    def test_famicom_ay_is_sunsoft5b_and_one_octave_lower(self):
        plain = self._song(False)
        fami = self._song(True)
        pn = [e for e in plain.events if e.on and e.tone_chip == 2][0]
        fn = [e for e in fami.events if e.on and e.tone_chip == 2][0]
        # The 5B halves the clock: its note is an octave below the same
        # register on a full-clock YM2149.
        self.assertEqual((pn.note * 64 + pn.e5) - (fn.note * 64 + fn.e5), 12 * 64)
        data = write_fur(fami, include_pcm=True)
        self.assertIn(b"chipType=2", data)
        self.assertIn(b"customClock=1789773", data)


class TestNesTones(unittest.TestCase):
    def test_pulse_duty_and_volume(self):
        # Key pulse 1: $4003 loads the length and frequency high byte, $4000
        # sets duty 2 and volume 9.
        writes = [
            _nes(1000, 0x15, 0x0F),
            _nes(1000, 0x02, 0x40),
            _nes(1000, 0x03, 0x09),
            _nes(1000, 0x00, 0xB9),  # duty 2, constant, halt, volume 9
            _nes(2000, 0x00, 0xB4),
            _nes(3000, 0x00, 0xB0),
        ]
        hits, offs, warn = collect_nes_tones(_vgm(writes, nes_clock=1789773))
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.voice, 0)
        self.assertEqual(hit.period, 0x140)
        self.assertEqual(hit.duty, 2)
        self.assertEqual(hit.vol, 9)
        self.assertEqual(hit.vol_pts, [(2000, 4)])
        self.assertEqual(offs[0].sample_time, 3000)
        self.assertEqual(warn, [])

    def test_triangle_linear_and_noise_mode(self):
        writes = [
            _nes(500, 0x15, 0x0F),
            _nes(500, 0x08, 0x81),
            _nes(500, 0x0A, 0x34),
            _nes(500, 0x0B, 0x01),
            _nes(500, 0x0C, 0x30),
            _nes(500, 0x0E, 0x87),  # short mode, period 7
            _nes(600, 0x0C, 0x3A),  # noise volume 10
        ]
        hits, offs, _warn = collect_nes_tones(_vgm(writes))
        tri = [h for h in hits if h.voice == 2][0]
        self.assertEqual(tri.period, 0x134)
        self.assertEqual(tri.linear, 0x81)
        noise = [h for h in hits if h.voice == 3][0]
        self.assertEqual(noise.duty, 1)
        self.assertEqual(noise.period, 7)
        self.assertEqual(noise.vol, 10)

    def test_period_jump_is_a_new_note(self):
        base = [
            _nes(1000, 0x15, 0x0F), _nes(1000, 0x00, 0xBF),
            _nes(1000, 0x02, 0x00), _nes(1000, 0x03, 0x01),
        ]
        writes = base + [
            _nes(5000, 0x02, 0x00), _nes(5000, 0x03, 0x00),  # octave up
        ]
        hits, _offs, _warn = collect_nes_tones(_vgm(writes))
        self.assertEqual(len(hits), 2)
        self.assertGreater(hits[0].period, hits[1].period)

    def test_high_byte_before_low_completes_the_pitch(self):
        # Gimmick! writes $400B (timer high) before $400A (timer low).  The
        # low write must re-read the period or the note keeps the stale low
        # byte from the previous note.
        writes = [
            _nes(1000, 0x15, 0x0F),
            _nes(1000, 0x08, 0xFF),
            _nes(1000, 0x0B, 0x01),
            _nes(1000, 0x0A, 0xC9),
        ]
        hits, _offs, _warn = collect_nes_tones(_vgm(writes))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].period, 0x1C9)

    def test_same_pitch_rewrites_stay_one_note(self):
        writes = [
            _nes(1000, 0x15, 0x0F), _nes(1000, 0x00, 0xBF),
            _nes(1000, 0x02, 0x40), _nes(1000, 0x03, 0x09),
        ]
        for t in range(2000, 8000, 735):
            writes += [
                _nes(t, 0x00, 0xB9), _nes(t, 0x02, 0x40),
                _nes(t, 0x03, 0x09),
            ]
        hits, _offs, _warn = collect_nes_tones(_vgm(writes))
        self.assertEqual(len(hits), 1)

    def test_note_mapping_round_trips(self):
        for period in (0, 1, 0x100, 0x400, 0x7FF):
            note, e5 = nes_note(1789773, period)
            self.assertGreaterEqual(note, 0)
            self.assertLessEqual(note, 179)
            self.assertGreaterEqual(e5, 0)
            self.assertLessEqual(e5, 0xFF)


class TestNesPitchMotion(unittest.TestCase):
    def _event(self, e5=119):
        return NoteEvent(
            sample=0, ch=13, on=True, note=112, ins=0, vol=15, e5=e5, pan=0,
        )

    def test_delayed_pitch_change_stays_a_bend(self):
        # Gimmick!'s triangle holds one period and then moves once near the
        # end of the note. Folding that into E5 retunes the whole note.
        ev = self._event()
        pitch = _absorb_detune(ev, (0, 0, 0, 60, 60), 1)
        self.assertEqual(pitch, (0, 0, 0, 60, 60))
        self.assertEqual((ev.note, ev.e5), (112, 119))

    def test_pitch_that_settles_on_the_first_tick_is_the_note(self):
        ev = self._event()
        pitch = _absorb_detune(ev, (0, 60, 60), 1)
        self.assertEqual(pitch, ())
        self.assertEqual(ev.e5, 119 + 60)

    def test_mid_row_channel_effect_does_not_delay_the_note(self):
        # Gimmick!'s intro opens pulse 2 on row 0 and changes duty later in
        # the same row. The row's ED must not push the note to that later
        # tick; the duty change moves to the next row instead.
        writes = [
            _nes(0, 0x15, 0x0F),
            _nes(44, 0x00, 0xB9),
            _nes(44, 0x02, 0x1D),
            _nes(44, 0x03, 0x09),
            _nes(753, 0x00, 0x39),    # duty 2 -> 0, still inside row 0
            _nes(2000, 0x00, 0x30),
        ]
        song = analyze(_vgm(writes, nes_clock=1789773))
        # Force the grid the source uses: 9 ticks of 91.7 samples per row.
        song.hz = 44100.0 / 91.7
        song.speed = 9
        song.t0 = 0
        mod = parse_fur(write_fur(song, include_pcm=True))
        # Pulse 1 is channel 0 in a NES-only module.
        row0 = mod.patterns[(0, 0)][0]
        row1 = mod.patterns[(0, 0)][1]
        self.assertGreaterEqual(row0.note, 0)
        self.assertNotIn(
            0xED, [c for c, _v in (row0.fx or ())],
            "a mid-row duty change must not delay the note",
        )
        self.assertIn((0x12, 2), row0.fx or ())
        self.assertIn((0x12, 0), row1.fx or ())

    def test_triangle_note_keeps_its_starting_pitch(self):
        writes = [
            _nes(1000, 0x15, 0x0F),
            _nes(1000, 0x08, 0xFF),
            _nes(1000, 0x0A, 169),
            _nes(1000, 0x0B, 0x00),
            _nes(40000, 0x0A, 160),   # one step down, long after the start
            _nes(80000, 0x08, 0x00),
        ]
        song = analyze(_vgm(writes, nes_clock=1789773))
        tri = next(
            e for e in song.events
            if e.on and e.tone_chip == 4 and e.note < 150
        )
        self.assertEqual((tri.note, tri.e5), nes_note(1789773, 169))
        self.assertEqual(tri.tone_pitch_pts, ((40000, 160),))
        self.assertTrue(song.nes_ins)
        self.assertTrue(song.nes_ins[0].pitch)


class TestNesDpcm(unittest.TestCase):
    def _rom(self, data: bytes) -> RomSlice:
        return RomSlice(0, len(data), 0xC000, data, "nes_dpcm")

    def test_dpcm_sample_body(self):
        data = bytearray(0x2000)
        body = bytes([0xAA, 0x55] * 8)
        data[0x440:0x440 + len(body)] = body
        writes = [
            _nes(1000, 0x10, 0x0E),
            _nes(1000, 0x12, 0x11),      # $C000 + 0x11*64 = $C440
            _nes(1000, 0x13, 0x00),      # 1 byte? length 16*0+1
            _nes(1000, 0x15, 0x1F),
            _nes(2000, 0x15, 0x0F),
        ]
        samples, hits, warn = collect_nes_dpcm(
            _vgm(writes, [self._rom(bytes(data))], nes_clock=1789773),
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].data, body[:1])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].rate_index, 0x0E)
        self.assertEqual(hits[0].end_time, 2000)
        self.assertEqual(warn, [])

    def test_dpcm_loop_bit(self):
        data = bytes(0x100)
        writes = [
            _nes(1000, 0x10, 0x4F),  # rate F, loop
            _nes(1000, 0x12, 0x00),
            _nes(1000, 0x13, 0x01),
            _nes(1000, 0x15, 0x1F),
        ]
        samples, _hits, _warn = collect_nes_dpcm(
            _vgm(writes, [self._rom(data)]),
        )
        self.assertTrue(samples[0].loop)

    def test_undumped_dpcm_warns(self):
        writes = [
            _nes(1000, 0x10, 0x0E),
            _nes(1000, 0x12, 0xF0),
            _nes(1000, 0x13, 0x01),
            _nes(1000, 0x15, 0x1F),
        ]
        _s, hits, warn = collect_nes_dpcm(
            _vgm(writes, [self._rom(bytes(0x100))]),
        )
        self.assertEqual(len(hits), 1)
        self.assertTrue(any("does not dump" in w for w in warn))

    def test_dpcm_stop_becomes_a_note_off(self):
        # The driver clears $4015 bit 4 to cut a drum short before its body
        # ends. That must become a PCM note-off, or the tracker plays the
        # whole sample.
        data = bytes(0x1000)
        writes = [
            _nes(1000, 0x10, 0x0E),
            _nes(1000, 0x12, 0x11),
            _nes(1000, 0x13, 0x01),
            _nes(1000, 0x15, 0x1F),
            _nes(5000, 0x15, 0x0F),
        ]
        song = analyze(_vgm(
            writes,
            [RomSlice(0, len(data), 0xC000, data, "nes_dpcm")],
            nes_clock=1789773,
        ))
        ons = [e for e in song.events if e.on and e.pcm]
        offs = [e for e in song.events if not e.on and e.pcm]
        self.assertEqual(len(ons), 1)
        self.assertEqual(ons[0].end, 5000)
        self.assertEqual([e.sample for e in offs], [5000])
        mod = parse_fur(write_fur(song, include_pcm=True))
        notes = [
            row.note
            for oi in mod.orders[4]
            for row in mod.patterns.get((4, oi), [])
            if row.note >= 0
        ]
        self.assertIn(180, notes)   # NOTE_OFF

    def test_4011_spam_becomes_pcm(self):
        # Two hundred 7-bit writes at 4-sample spacing = one 11025 Hz body.
        writes = [_nes(100 + i * 4, 0x11, (i * 3) & 0x7F) for i in range(200)]
        samples, hits, warn = collect_nes_pcm(_vgm(writes))
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].rate, 11025)
        self.assertEqual(len(hits), 1)
        self.assertGreater(len(samples[0].data), 180)
        self.assertEqual(warn, [])


class TestNesModule(unittest.TestCase):
    def _nes_song(self):
        writes = [
            _nes(1000, 0x15, 0x0F),
            _nes(1000, 0x00, 0xB9), _nes(1000, 0x02, 0x40),
            _nes(1000, 0x03, 0x09),
            _nes(2000, 0x04, 0xB4), _nes(2000, 0x06, 0x80),
            _nes(2000, 0x07, 0x05),
            _nes(2500, 0x08, 0x81), _nes(2500, 0x0A, 0x34),
            _nes(2500, 0x0B, 0x01),
            _nes(3000, 0x0C, 0x3A), _nes(3000, 0x0E, 0x03),
        ]
        return analyze(_vgm(writes, nes_clock=1789773))

    def test_nes_module_layout(self):
        song = self._nes_song()
        self.assertEqual(song.nes_ids, [0])
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        # An NES-only rip has no FM write, so the empty YM2151 placeholder is
        # dropped and the APU owns all five columns.
        self.assertEqual(mod.chips, [CHIP_NES])
        self.assertEqual(mod.n_ch, 5)
        types = [
            struct.unpack_from("<H", blk, 2)[0]
            for _off, mag, _size, blk in iter_blocks(data)
            if mag == b"INS2"
        ]
        self.assertIn(INS_NES, types)

    def test_vrc7_module_uses_six_channels(self):
        raw = _raw_vgm(
            bytes([
                0x51, 0x30, 0x10,  # ch 0: preset 1, volume 0
                0x51, 0x10, 0x40,  # fnum low
                0x51, 0x20, 0x10,  # key on, block 0
            ]),
            ym2413=0x80000000 | 3579545,
        )
        p = TMP / "vrc7_song.vgm"
        p.write_bytes(raw)
        song = analyze(load_vgm(p))
        self.assertTrue(song.opll_vrc7)
        self.assertEqual(song.fm_systems[0].fm_channels, 6)
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_VRC7])
        self.assertEqual(mod.n_ch, 6)


class TestNesDpcmWriter(unittest.TestCase):
    def test_dpcm_sample_depth(self):
        data = bytearray(0x1000)
        body = bytes([0xAA, 0x55, 0x01, 0x80] * 8)
        data[0x440:0x440 + len(body)] = body
        writes = [
            _nes(1000, 0x10, 0x0E),
            _nes(1000, 0x12, 0x11),
            _nes(1000, 0x13, 0x01),
            _nes(1000, 0x15, 0x1F),
        ]
        song = analyze(_vgm(
            writes,
            [RomSlice(0, len(data), 0xC000, bytes(data), "nes_dpcm")],
            nes_clock=1789773,
        ))
        self.assertEqual(len(song.nes_dpcm_samples), 1)
        fur = write_fur(song, include_pcm=True)
        mod = parse_fur(fur)
        self.assertEqual(len(mod.samples), 1)
        self.assertEqual(mod.samples[0].depth, SMP_DEPTH_DPCM)
        # $4013 = 0x01 is 17 bytes (16*1+1), i.e. 136 DPCM bits.
        self.assertEqual(mod.samples[0].n_frames, 17 * 8)


if __name__ == "__main__":
    unittest.main()
