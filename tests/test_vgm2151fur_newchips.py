"""SegaPCM, Namco C140 and Namco C352 support: parsing, collection, .fur emission."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.c140 import collect_c140
from vgm2151fur.c352 import collect_c352
from vgm2151fur.furio import iter_blocks, list_fur_samples, parse_fur
from vgm2151fur.furwrite import (
    CHIP_C140, CHIP_C352, CHIP_SEGAPCM, INS_C140, INS_C352, INS_SEGAPCM, write_fur,
)
from vgm2151fur.segapcm import _Voice, _advance_voice, collect_segapcm
from vgm2151fur.vgm import (
    ChipWrite, RomSlice, VgmFile, c352_effective_clock, chips_in_head, load_vgm,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_newchips"
TMP.mkdir(exist_ok=True)


def _lattice(n=60):
    return [
        ChipWrite(sample=k * 5000, chip="ym2151", chip_id=0, reg=0x08, val=0x78)
        for k in range(n)
    ]


def _vgm(writes, roms=(), **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=list(roms), writes=list(writes), **kw,
    )


def _raw_vgm(cmds: bytes, rom_blocks=(), shift: int = 0, mask: int = 0) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x30:0x34] = (3579545).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    buf[0x38:0x3C] = (4000000).to_bytes(4, "little")   # SegaPCM clock
    buf[0x3C] = shift & 0xFF                           # SegaPCM bank shift
    buf[0x3E] = mask & 0xFF                            # SegaPCM bank mask
    buf[0x96] = 1                                      # C140: System 21
    buf[0xA8:0xAC] = (12288000).to_bytes(4, "little")  # C140 clock
    body = bytearray()
    for typ, start, data in rom_blocks:
        payload = (len(data) + 8).to_bytes(4, "little") if False else data
        blk = (0x10000).to_bytes(4, "little") + start.to_bytes(4, "little") + payload
        body += bytes([0x67, 0x66, typ]) + len(blk).to_bytes(4, "little") + blk
    body += cmds + b"\x66"
    return bytes(buf) + bytes(body)


class TestVgmParsing(unittest.TestCase):
    def test_segapcm_write_and_rom(self):
        raw = _raw_vgm(
            b"\xc0\x84\x00\x10\xc0\x85\x00\x02\xc0\x86\x00\x00",
            [(0x80, 0x0400, bytes(range(64)))],
        )
        p = TMP / "spcm.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.segapcm_clock, 4000000)
        self.assertEqual(v.segapcm_bankshift, 0)
        self.assertEqual(v.segapcm_bankmask, 0)
        self.assertEqual([s.kind for s in v.roms], ["segapcm"])
        self.assertEqual(v.roms[0].start, 0x0400)
        w = [x for x in v.writes if x.chip == "segapcm"]
        self.assertEqual([(x.reg, x.val) for x in w],
                         [(0x84, 0x10), (0x85, 0x02), (0x86, 0x00)])

    def test_c140_write_and_rom(self):
        raw = _raw_vgm(b"\xd4\x00\x35\xd0", [(0x8D, 0x80000, bytes(32))])
        p = TMP / "c140.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.c140_clock, 12288000)
        self.assertEqual(v.c140_type, 1)
        self.assertEqual([s.kind for s in v.roms], ["c140"])
        self.assertEqual(v.roms[0].start, 0x80000)
        w = [x for x in v.writes if x.chip == "c140"]
        self.assertEqual([(x.reg, x.val) for x in w], [(0x035, 0xD0)])

    def test_legacy_clock_selects_system_21_mapping(self):
        # Starblade-style 1.51 file: no 1.61 type field, but the legacy
        # 8MHz/374 rate in the clock slot means System 21.
        raw = bytearray(_raw_vgm(b"", [(0x8D, 0x80000, bytes(32))]))
        raw[8:12] = (0x151).to_bytes(4, "little")
        raw[0xA8:0xAC] = (21390).to_bytes(4, "little")
        p = TMP / "c140_legacy.vgm"
        p.write_bytes(bytes(raw))
        v = load_vgm(p)
        self.assertEqual(v.c140_clock, 12288000)
        self.assertEqual(v.c140_type, 1)

    def test_unsupported_sample_chip_warns(self):
        raw = _raw_vgm(b"\xb6\x00\x00")  # uPD7759 write
        p = TMP / "upd.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertTrue(any("uPD7759" in w for w in v.warnings))

    def test_segapcm_interface_bytes(self):
        raw = _raw_vgm(b"", shift=12, mask=0x70)
        p = TMP / "spcm_intf.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.segapcm_bankshift, 12)
        self.assertEqual(v.segapcm_bankmask, 0x70)


class TestSegapcm(unittest.TestCase):
    def _song(self):
        rom = bytearray(0x1000)
        for i in range(0x400, 0x500):  # 256-byte region (end page 4 -> 0x500)
            rom[i] = 0x80 + (i & 0x3F)
        writes = _lattice()
        # voice 0: loop/start 0x0400 0x0400, end page 0x04 (end_byte 0x500)
        writes += [
            ChipWrite(60000, "segapcm", 0, 0x04, 0x00),
            ChipWrite(60000, "segapcm", 0, 0x05, 0x04),
            ChipWrite(60000, "segapcm", 0, 0x06, 0x04),
            ChipWrite(60000, "segapcm", 0, 0x07, 0x84),  # delta 132
            ChipWrite(60000, "segapcm", 0, 0x02, 0x20),  # vol L
            ChipWrite(60000, "segapcm", 0, 0x03, 0x10),  # vol R
            ChipWrite(60000, "segapcm", 0, 0x84, 0x00),
            ChipWrite(60000, "segapcm", 0, 0x85, 0x04),
            ChipWrite(60000, "segapcm", 0, 0x86, 0x00),  # enable (loop)
        ]
        roms = [RomSlice(0, 0x1000, 0, bytes(rom), "segapcm")]
        return analyze(_vgm(writes, roms, segapcm_clock=4000000), pcm=True)

    def test_hit_and_sample(self):
        song = self._song()
        self.assertEqual(len(song.segapcm_samples), 1)
        smp = song.segapcm_samples[0]
        self.assertEqual(smp.start, 0x400)
        self.assertEqual(smp.length, 256)
        self.assertEqual(smp.loop_start, 0)
        self.assertEqual(smp.loop_end, 256)
        self.assertEqual(smp.mode_freq(), 0x84)
        self.assertAlmostEqual(smp.c4_rate(4000000), 4000000 * 132 / 32768, delta=1)

    def test_fur_chip_and_instrument(self):
        song = self._song()
        events = [e for e in song.events if e.on and e.ch >= 8]
        self.assertTrue(events)
        ev = events[0]
        self.assertEqual(ev.ch, 8)
        self.assertEqual(ev.ins, len(song.fm_patches))  # first sample instrument
        self.assertEqual(ev.vol, 0x20)  # native 7-bit register value
        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0x82, CHIP_SEGAPCM])
        self.assertEqual(mod.n_ch, 8 + 16)
        self.assertEqual(min(mod.pcm_channels), 8)
        ins_types = []
        for _off, mag, _sz, block in iter_blocks(data):
            if mag == b"INS2":
                ins_types.append(struct.unpack_from("<H", block, 2)[0])
        self.assertIn(INS_SEGAPCM, ins_types)
        smps = list_fur_samples(data)
        self.assertEqual(len(smps), 1)
        self.assertEqual(smps[0].n_frames, 256)
        self.assertEqual(smps[0].loop_start, 0)

    def test_disabled_voice_is_not_a_hit(self):
        writes = _lattice() + [
            ChipWrite(60000, "segapcm", 0, 0x84, 0x00),
            ChipWrite(60000, "segapcm", 0, 0x85, 0x04),
            ChipWrite(60000, "segapcm", 0, 0x86, 0x01),  # disable
            ChipWrite(70000, "segapcm", 0, 0x86, 0x00),  # a bare re-enable
        ]
        roms = [RomSlice(0, 0x1000, 0, bytes(0x1000), "segapcm")]
        song = analyze(_vgm(writes, roms, segapcm_clock=4000000), pcm=True)
        self.assertEqual(song.segapcm_samples, [])

    def test_bank_offset_selects_rom_window(self):
        # shift 12, mask 0 → 0x70. ctrl 0xC0 → (0x40 << 12) | 0x90 = 0x40090.
        rom = bytearray(0x40100)
        raw = bytes(0xA0 + (i & 0x0F) for i in range(0x70))
        rom[0x40090:0x40100] = raw
        writes = _lattice() + [
            ChipWrite(60000, "segapcm", 0, 0x06, 0x00),  # end page 0 → 0x100
            ChipWrite(60000, "segapcm", 0, 0x07, 0x40),
            ChipWrite(60000, "segapcm", 0, 0x84, 0x90),
            ChipWrite(60000, "segapcm", 0, 0x85, 0x00),
            ChipWrite(60000, "segapcm", 0, 0x86, 0xC2),  # bank, one-shot
        ]
        roms = [RomSlice(0, len(rom), 0, bytes(rom), "segapcm")]
        song = analyze(_vgm(
            writes, roms, segapcm_clock=4000000,
            segapcm_bankshift=12, segapcm_bankmask=0,
        ), pcm=True)
        self.assertEqual(len(song.segapcm_samples), 1)
        smp = song.segapcm_samples[0]
        self.assertEqual(smp.start, 0x40090)
        self.assertEqual(smp.data, bytes((b ^ 0x80) & 0xFF for b in raw))
        self.assertIsNone(smp.loop_start)

    def _region(self):
        rom = bytearray(0x1000)
        for i in range(0x400, 0x500):
            rom[i] = 0xC0
        return rom

    def _key(self, t, *, end=0x04, delta=0x84, ctrl=0x02, start=0x0400, voice=0):
        base = voice << 3
        return [
            ChipWrite(t, "segapcm", 0, base + 0x04, start & 0xFF),
            ChipWrite(t, "segapcm", 0, base + 0x05, (start >> 8) & 0xFF),
            ChipWrite(t, "segapcm", 0, base + 0x84, start & 0xFF),
            ChipWrite(t, "segapcm", 0, base + 0x85, (start >> 8) & 0xFF),
            ChipWrite(t + 2, "segapcm", 0, base + 0x06, end),
            ChipWrite(t + 2, "segapcm", 0, base + 0x07, delta),
            ChipWrite(t + 2, "segapcm", 0, base + 0x02, 0x40),
            ChipWrite(t + 2, "segapcm", 0, base + 0x03, 0x40),
            ChipWrite(t + 2, "segapcm", 0, base + 0x86, ctrl),
        ]

    def test_register_dump_split_across_two_samples_is_one_note(self):
        # End and delta arrive two samples after the address. The note uses
        # those later values, and the dump is one hit.
        rom = self._region()
        writes = _lattice() + [
            ChipWrite(1000, "segapcm", 0, 0x06, 0x20),
            ChipWrite(1000, "segapcm", 0, 0x07, 0x01),
        ] + self._key(50000)
        roms = [RomSlice(0, 0x1000, 0, bytes(rom), "segapcm")]
        song = analyze(_vgm(writes, roms, segapcm_clock=4000000), pcm=True)
        self.assertEqual(len(song.segapcm_samples), 1)
        smp = song.segapcm_samples[0]
        self.assertEqual(smp.start, 0x400)
        self.assertEqual(smp.length, 256)
        self.assertEqual(smp.mode_freq(), 0x84)
        self.assertEqual(len(smp.hits), 1)

    def test_same_address_after_the_cursor_moves_is_a_second_note(self):
        # 13 samples at delta 0xB4 advances the cursor several bytes, so
        # rewriting the same start seeks. The two dumps stay separate.
        rom = self._region()
        writes = _lattice() + self._key(100000, delta=0xB4) + self._key(100013, delta=0xB4)
        roms = [RomSlice(0, 0x1000, 0, bytes(rom), "segapcm")]
        song = analyze(_vgm(writes, roms, segapcm_clock=4000000), pcm=True)
        self.assertEqual(len(song.segapcm_samples), 1)
        self.assertEqual(len(song.segapcm_samples[0].hits), 2)

    def test_rewrite_before_the_byte_advances_is_the_same_note(self):
        # delta 1 over 5 samples moves the fraction only. The byte address
        # is unchanged, so the rewrite is not a seek.
        rom = self._region()
        writes = _lattice() + self._key(100000, delta=0x01) + self._key(100005, delta=0x01)
        roms = [RomSlice(0, 0x1000, 0, bytes(rom), "segapcm")]
        song = analyze(_vgm(writes, roms, segapcm_clock=4000000), pcm=True)
        self.assertEqual(len(song.segapcm_samples), 1)
        self.assertEqual(len(song.segapcm_samples[0].hits), 1)

    def test_volume_and_delta_refresh_is_not_a_new_note(self):
        rom = self._region()
        writes = _lattice() + self._key(100000, delta=0x40) + [
            ChipWrite(101000, "segapcm", 0, 0x02, 0x10),
            ChipWrite(101000, "segapcm", 0, 0x07, 0x20),
        ]
        roms = [RomSlice(0, 0x1000, 0, bytes(rom), "segapcm")]
        song = analyze(_vgm(writes, roms, segapcm_clock=4000000), pcm=True)
        self.assertEqual(len(song.segapcm_samples[0].hits), 1)
        self.assertEqual(song.segapcm_samples[0].hits[0].freq, 0x40)

    def test_one_shot_replay_after_the_phrase_ends(self):
        # The chip sets the disable bit itself when the end page is reached.
        # That write is not in the log. Rewriting the same start later seeks
        # back and plays the phrase again. A far write on another voice must
        # not drop the first note.
        rom = self._region()
        writes = _lattice() + self._key(10000, delta=0xFF) + self._key(80000, delta=0xFF, voice=1)
        writes += self._key(80000, delta=0xFF)
        roms = [RomSlice(0, 0x1000, 0, bytes(rom), "segapcm")]
        song = analyze(_vgm(writes, roms, segapcm_clock=4000000), pcm=True)
        voice0 = [h for s in song.segapcm_samples for h in s.hits if h.voice == 0]
        self.assertEqual(len(voice0), 2)
        self.assertTrue(all(h.start == 0x0400 for h in voice0))
        self.assertEqual(song.segapcm_samples[0].length, 256)

    def test_restarts_get_their_own_rows_inside_the_order_budget(self):
        rom = self._region()
        writes = _lattice()
        t = 100000
        # One pair closer than any row the 256-order budget can give an
        # 8e6-sample song, then a stream of 800-sample restarts.
        writes += self._key(t, delta=0xB4)
        writes += self._key(t + 13, delta=0xB4)
        for k in range(1, 12):
            writes += self._key(t + 13 + 800 * k, delta=0xB4)
        roms = [RomSlice(0, 0x1000, 0, bytes(rom), "segapcm")]
        vgm = _vgm(writes, roms, segapcm_clock=4000000)
        vgm.total_samples = 8_000_000
        song = analyze(vgm, speed=8, pcm=True)
        hits = [h for s in song.segapcm_samples for h in s.hits if h.voice == 0]
        self.assertEqual(len(hits), 13)
        self.assertGreater(song.samples_per_row, 400)
        self.assertLess(song.samples_per_row, 900)
        rows = [song.place(h.sample_time)[0] for h in hits]
        # The 13-sample pair shares a row. Every 800-sample restart has its own.
        self.assertEqual(len(rows) - len(set(rows)), 1)

    def test_advance_matches_a_tick_loop(self):
        def tick(v: _Voice) -> None:
            if v.ctrl & 1:
                v.addr &= 0xFFFF00
                return
            stop = (v.end + 1) & 0xFF
            if ((v.addr >> 16) & 0xFF) == stop:
                if v.ctrl & 2:
                    v.ctrl |= 1
                    return
                v.addr = (v.loop & 0xFFFF) << 8
            v.addr = (v.addr + (v.freq & 0xFF)) & 0xFFFFFF

        cases = [
            _Voice(addr=0x040000, loop=0x0400, end=0x04, freq=0xB4, ctrl=0x00),
            _Voice(addr=0x040000, loop=0x0400, end=0x04, freq=0xFF, ctrl=0x02),
            _Voice(addr=0xFE8000, loop=0x0100, end=0x00, freq=0x7F, ctrl=0x00),
            _Voice(addr=0x040080, loop=0x0400, end=0x04, freq=0x00, ctrl=0x00),
            _Voice(addr=0x123456, loop=0xFFFF, end=0xFF, freq=0x10, ctrl=0x01),
        ]
        for start in cases:
            fast = _Voice(**{f: getattr(start, f) for f in ("addr", "loop", "end", "freq", "lvol", "rvol", "ctrl")})
            slow = _Voice(**{f: getattr(start, f) for f in ("addr", "loop", "end", "freq", "lvol", "rvol", "ctrl")})
            for n in (1, 2, 7, 64, 500, 4096):
                probe = _Voice(**{f: getattr(start, f) for f in ("addr", "loop", "end", "freq", "lvol", "rvol", "ctrl")})
                _advance_voice(probe, n)
                walked = _Voice(**{f: getattr(start, f) for f in ("addr", "loop", "end", "freq", "lvol", "rvol", "ctrl")})
                for _ in range(n):
                    tick(walked)
                self.assertEqual(probe.addr, walked.addr, (start.addr, start.freq, n))
                self.assertEqual(probe.ctrl, walked.ctrl, (start.addr, start.freq, n))
            for _ in range(3000):
                _advance_voice(fast, 1)
                tick(slow)
            self.assertEqual(fast.addr, slow.addr)
            self.assertEqual(fast.ctrl, slow.ctrl)


class TestC140(unittest.TestCase):
    def _body(self) -> bytes:
        # No zero bytes, so trailing-silence trim keeps all 200 frames.
        return bytes(((i * 3 + 1) & 0xFF) for i in range(200))

    def _song(self, compressed=False, loop=True, mix=None, tail=(), key_vol=(0xC8, 0x64)):
        body = self._body()
        rom = bytearray(0x200)
        rom[0x100:0x100 + len(body)] = body
        mode = 0x80 | (0x10 if loop else 0) | (0x08 if compressed else 0)
        writes = _lattice() + [
            ChipWrite(60000, "c140", 0, 0x04, 0),        # bank 0 → addr == start
            ChipWrite(60000, "c140", 0, 0x02, 0x64),     # freq MSB (0x6400)
            ChipWrite(60000, "c140", 0, 0x03, 0x00),
            ChipWrite(60000, "c140", 0, 0x01, key_vol[0]),  # vol L
            ChipWrite(60000, "c140", 0, 0x00, key_vol[1]),  # vol R
            ChipWrite(60000, "c140", 0, 0x06, 0x01),
            ChipWrite(60000, "c140", 0, 0x07, 0x00),
            ChipWrite(60000, "c140", 0, 0x08, 0x01),
            ChipWrite(60000, "c140", 0, 0x09, 0xC8),
            ChipWrite(60000, "c140", 0, 0x0A, 0x01),
            ChipWrite(60000, "c140", 0, 0x0B, 0x20),
            ChipWrite(60000, "c140", 0, 0x05, mode),
        ] + list(tail)
        roms = [RomSlice(0, len(rom), 0, bytes(rom), "c140")]
        extra = {0x1C: (0, mix)} if mix is not None else {}
        return analyze(_vgm(writes, roms, c140_clock=12288000, c140_type=1,
                            extra_volumes=extra), pcm=True)

    def test_hit_and_sample(self):
        song = self._song()
        self.assertEqual(len(song.c140_samples), 1)
        smp = song.c140_samples[0]
        self.assertEqual(smp.start, 0x100)
        self.assertEqual(smp.length, 200)
        self.assertEqual(smp.depth, 8)
        self.assertEqual(smp.data, self._body())
        self.assertEqual(smp.loop_start, 0x20)
        self.assertEqual(smp.loop_end, 200)
        self.assertEqual(smp.mode_freq(), 0x6400)
        r = smp.c4_rate(12288000)
        self.assertAlmostEqual(r, 12288000 * 0x6400 / 18874368, delta=1)

    def test_fur_chip_and_instrument(self):
        song = self._song()
        events = [e for e in song.events if e.on and e.ch >= 8]
        self.assertTrue(events)
        self.assertEqual(events[0].ch, 8)
        self.assertEqual(events[0].vol, 0xC8)
        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0x82, CHIP_C140])
        self.assertEqual(mod.n_ch, 8 + 24)
        ins_types = []
        for _off, mag, _sz, block in iter_blocks(data):
            if mag == b"INS2":
                ins_types.append(struct.unpack_from("<H", block, 2)[0])
        self.assertIn(INS_C140, ins_types)
        smps = list_fur_samples(data)
        self.assertEqual(smps[0].n_frames, 200)
        self.assertEqual((smps[0].loop_start, smps[0].loop_end), (0x20, 200))
        self.assertEqual(smps[0].loop_mode, 0)

    def test_compressed_mode_decodes(self):
        song = self._song(compressed=True, loop=False)
        smp = song.c140_samples[0]
        self.assertEqual(smp.depth, 16)
        self.assertEqual(smp.length, 200)
        self.assertEqual(len(smp.data), 400)
        # Full mulaw value of byte 0x01 is 256, not the old >>8 collapse to 1.
        self.assertEqual(struct.unpack_from("<h", smp.data, 0)[0], 256)
        self.assertIsNone(smp.loop_start)
        data = write_fur(song, include_pcm=True, include_fm=True)
        smps = list_fur_samples(data)
        self.assertEqual(smps[0].depth, 16)
        self.assertEqual(smps[0].n_frames, 200)

    def test_system2_bank_is_byte_address(self):
        # bank 21 + start 0x8A82 → 0x58A82. A decoy at the low address must lose.
        start = 0x8A82
        n = 64
        real = bytes(0x10 + i for i in range(n))
        rom = bytearray(0x58A82 + n)
        rom[start:start + n] = bytes([0x7F]) * n
        rom[0x58A82:0x58A82 + n] = real
        end = start + n
        writes = _lattice() + [
            ChipWrite(60000, "c140", 0, 0x04, 21),
            ChipWrite(60000, "c140", 0, 0x02, 0x10),
            ChipWrite(60000, "c140", 0, 0x03, 0x00),
            ChipWrite(60000, "c140", 0, 0x06, (start >> 8) & 0xFF),
            ChipWrite(60000, "c140", 0, 0x07, start & 0xFF),
            ChipWrite(60000, "c140", 0, 0x08, (end >> 8) & 0xFF),
            ChipWrite(60000, "c140", 0, 0x09, end & 0xFF),
            ChipWrite(60000, "c140", 0, 0x05, 0x80),
        ]
        roms = [RomSlice(0, len(rom), 0, bytes(rom), "c140")]
        song = analyze(_vgm(writes, roms, c140_clock=12288000, c140_type=0), pcm=True)
        self.assertEqual(len(song.c140_samples), 1)
        self.assertEqual(song.c140_samples[0].data, real)
        self.assertEqual(song.c140_samples[0].depth, 8)

    def test_undumped_rom_is_counted(self):
        writes = _lattice() + [
            ChipWrite(60000, "c140", 0, 0x04, 9),       # bank 9, not dumped
            ChipWrite(60000, "c140", 0, 0x06, 0x40),
            ChipWrite(60000, "c140", 0, 0x07, 0x00),
            ChipWrite(60000, "c140", 0, 0x08, 0x40),
            ChipWrite(60000, "c140", 0, 0x09, 0x10),
            ChipWrite(60000, "c140", 0, 0x05, 0x80),
        ]
        roms = [
            RomSlice(0, 0x40000, 0x80000, bytes(0x1000), "c140"),
            RomSlice(0, 0x40000, 0x90000, bytes(0x1000), "c140"),
        ]
        song = analyze(_vgm(writes, roms, c140_clock=12288000), pcm=True)
        self.assertTrue(any("not dump" in w for w in song.warnings))

    def test_key_off_emits_note_off(self):
        body = self._body()
        rom = bytearray(0x200)
        rom[0x100:0x100 + len(body)] = body
        writes = _lattice() + [
            ChipWrite(60000, "c140", 0, 0x06, 0x01),
            ChipWrite(60000, "c140", 0, 0x07, 0x00),
            ChipWrite(60000, "c140", 0, 0x08, 0x01),
            ChipWrite(60000, "c140", 0, 0x09, 0xC8),
            ChipWrite(60000, "c140", 0, 0x0A, 0x01),
            ChipWrite(60000, "c140", 0, 0x0B, 0x20),
            ChipWrite(60000, "c140", 0, 0x05, 0x90),  # key on, looping
            ChipWrite(90000, "c140", 0, 0x05, 0x10),  # key off (bit 7 clear)
        ]
        roms = [RomSlice(0, len(rom), 0, bytes(rom), "c140")]
        song = analyze(_vgm(writes, roms, c140_clock=12288000, c140_type=1), pcm=True)
        offs = [e for e in song.events if not e.on and e.ch == 8]
        self.assertEqual(len(offs), 1)
        self.assertEqual(offs[0].sample, 90000)
        data = write_fur(song, include_pcm=True, include_fm=True)
        self.assertEqual(parse_fur(data).chips, [0x82, CHIP_C140])


class TestChipVolumes(unittest.TestCase):
    def test_player_mix_defaults_and_entries(self):
        from vgm2151fur.furwrite import _player_chip_volume

        vgm = _vgm([], extra_volumes={0x1C: (0, 0x80)})
        self.assertAlmostEqual(_player_chip_volume(vgm, 0x03, 1), 1.0)
        self.assertAlmostEqual(_player_chip_volume(vgm, 0x04, 1), 1.5)
        self.assertAlmostEqual(_player_chip_volume(vgm, 0x1C, 1), 85 / 256)
        vgm = _vgm([], extra_volumes={0x18: (0, 0x60)})
        self.assertAlmostEqual(_player_chip_volume(vgm, 0x18, 1), 0.375)
        # Bit 15 makes an entry relative to the default; instances divide first.
        vgm = _vgm([], extra_volumes={0x04: (0, 0x8080)})
        self.assertAlmostEqual(_player_chip_volume(vgm, 0x04, 1), 0.75)
        vgm = _vgm([], extra_volumes={0x18: (0, 0x60)})
        self.assertAlmostEqual(_player_chip_volume(vgm, 0x18, 2), 0.375)

    def test_c140_fur_volume_follows_the_player_mix(self):
        # No entry: C140 default 2/3, Furnace scale 0.7. Entry 0x80: 85/256.
        for mix, expected in ((None, 0.7 * (0xAB / 0x100)), (0x80, 0.7 * (85 / 0x100))):
            song = TestC140()._song(mix=mix)
            data = write_fur(song, include_pcm=True, include_fm=True)
            self.assertIn(struct.pack("<fff", expected, 0.0, 0.0), data)

    def test_segapcm_fur_volume_is_the_player_default(self):
        song = TestSegapcm()._song()
        data = write_fur(song, include_pcm=True, include_fm=True)
        self.assertIn(struct.pack("<fff", 1.5, 0.0, 0.0), data)

    def test_dual_msm6295_instances_are_halved(self):
        from types import SimpleNamespace

        from vgm2151fur.furwrite import CHIP_MSM6295, _sample_chip_volume

        song = SimpleNamespace(vgm=_vgm([], msm6295_dual=True))
        self.assertAlmostEqual(_sample_chip_volume(song, CHIP_MSM6295, 1), 0.5)
        song = SimpleNamespace(vgm=_vgm([], msm6295_dual=False))
        self.assertAlmostEqual(_sample_chip_volume(song, CHIP_MSM6295, 1), 1.0)


class TestC140FadesAndSlides(unittest.TestCase):
    @staticmethod
    def _all_rows(mod, ch):
        rows = []
        for (c, _idx), pat in mod.patterns.items():
            if c == ch:
                rows.extend(pat)
        return rows

    def test_mid_note_volume_steps_and_pitch_slide(self):
        tail = [
            ChipWrite(80000, "c140", 0, 0x01, 0x90),   # L 200 -> 144
            ChipWrite(84000, "c140", 0, 0x01, 0x30),   # L 144 -> 48 (R holds 100)
            ChipWrite(85000, "c140", 0, 0x00, 0x30),   # R 100 -> 48
            ChipWrite(80000, "c140", 0, 0x02, 0x32),   # 0x6400 -> 0x3200: -1 octave
            ChipWrite(90000, "c140", 0, 0x03, 0x00),   # LSB write, freq unchanged
            ChipWrite(100000, "c140", 0, 0x05, 0x00),  # key off
        ]
        song = TestC140()._song(tail=tail)
        ons = [e for e in song.events if e.on and e.pcm]
        self.assertEqual(len(ons), 1)
        ev = ons[0]
        # Volume steps carry the L/R pair (the loud side is the level): the
        # intermediate (48, 100) state is real hardware, and a write that does
        # not change the pair (the LSB of an unchanged frequency) records
        # nothing.
        self.assertEqual([v for _t, v, _p in ev.vol_pts], [144, 100, 48])
        self.assertEqual([d for _t, d in ev.sweep], [-768])
        self.assertEqual(ev.end, 100000)

        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        rows = self._all_rows(mod, 8)
        self.assertIn(144, [r.vol for r in rows])
        self.assertIn(48, [r.vol for r in rows])
        self.assertTrue(any(c in (0x01, 0x02) for r in rows for c, _v in r.fx))
        # The note row keeps its own pitch: the pitch ramp must start on a
        # later row, or the key-on write inherits part of the slide
        # (measured 2 st attacks on Cyber Sled / Valkyrie).
        note_row = next(r for r in rows if 0 <= r.note < 180)
        self.assertFalse(any(c in (0x01, 0x02) for c, _v in note_row.fx))
        self.assertTrue(any(
            c in (0x01, 0x02)
            for r in rows if r is not note_row
            for c, _v in r.fx
        ))
        # PCM slide parameters are doubled: one parameter unit moves 1/128 st
        # per tick, so the octave drop asks for 1536/speed units, clamped to
        # the engine's 255.
        speed = song.speed or 1
        params = [v for r in rows for c, v in r.fx if c in (0x01, 0x02)]
        self.assertIn(round(min(255.0, 1536.0 / speed)), [abs(v) for v in params])

    def test_big_jump_lands_as_quick_legato(self):
        # A move bigger than the slide can deliver inside a row must land as an
        # instant E8xx/E9xx transpose instead of a rate-limited slur (measured
        # on 0.6.8.3 through the C140 and YM2151 platforms).
        body = TestC140()._body()
        rom = bytearray(0x200)
        rom[0x100:0x100 + len(body)] = body
        writes = _lattice() + [
            ChipWrite(60000, "c140", 0, 0x04, 0),
            ChipWrite(60000, "c140", 0, 0x02, 0x64),
            ChipWrite(60000, "c140", 0, 0x03, 0x00),
            ChipWrite(60000, "c140", 0, 0x01, 0xC8),
            ChipWrite(60000, "c140", 0, 0x00, 0x64),
            ChipWrite(60000, "c140", 0, 0x06, 0x01),
            ChipWrite(60000, "c140", 0, 0x07, 0x00),
            ChipWrite(60000, "c140", 0, 0x08, 0x01),
            ChipWrite(60000, "c140", 0, 0x09, 0xC0),
            ChipWrite(60000, "c140", 0, 0x0A, 0x01),
            ChipWrite(60000, "c140", 0, 0x0B, 0x20),
            ChipWrite(60000, "c140", 0, 0x05, 0x80 | 0x10),
            ChipWrite(61500, "c140", 0, 0x02, 0x32),   # one octave down, two rows later
            ChipWrite(61600, "c140", 0, 0x03, 0x00),
            ChipWrite(100000, "c140", 0, 0x05, 0x00),  # key off
        ]
        roms = [RomSlice(0, len(rom), 0, bytes(rom), "c140")]
        song = analyze(_vgm(writes, roms, c140_clock=12288000, c140_type=1), speed=1, pcm=True)
        data = write_fur(song, include_pcm=True, include_fm=True)
        rows = self._all_rows(parse_fur(data), 8)
        xpose = [(c, v) for r in rows for c, v in r.fx if c in (0xE8, 0xE9)]
        self.assertTrue(xpose, "expected an E8/E9 quick-legato transpose")
        down = [v for c, v in xpose if c == 0xE9]
        self.assertTrue(down and down[0] >= 8,
                        f"octave drop should transpose down hard, got {xpose}")

    def test_frequency_after_key_on_refits_note_and_reference(self):
        # A driver that keys the voice on with stale zero frequency registers
        # (the common "write the pitch right after the mode byte" pattern)
        # must not pin the sample's reference rate to 0: the played frequency
        # is the post-key-on write. Voice 0 gets 0xC800 that way, voice 1 keys
        # on normally at half that, so voice 1 is one octave under.
        body = TestC140()._body()
        rom = bytearray(0x200)
        rom[0x100:0x100 + len(body)] = body
        mode = 0x80 | 0x10
        common = lambda v: [
            ChipWrite(60000, "c140", 0, (v << 4) | 0x04, 0),
            ChipWrite(60000, "c140", 0, (v << 4) | 0x01, 0xC8),
            ChipWrite(60000, "c140", 0, (v << 4) | 0x00, 0x64),
            ChipWrite(60000, "c140", 0, (v << 4) | 0x06, 0x01),
            ChipWrite(60000, "c140", 0, (v << 4) | 0x07, 0x00),
            ChipWrite(60000, "c140", 0, (v << 4) | 0x08, 0x01),
            ChipWrite(60000, "c140", 0, (v << 4) | 0x09, 0xC8),
            ChipWrite(60000, "c140", 0, (v << 4) | 0x0A, 0x01),
            ChipWrite(60000, "c140", 0, (v << 4) | 0x0B, 0x20),
        ]
        writes = _lattice() + common(0) + [
            ChipWrite(60000, "c140", 0, 0x02, 0x00),   # freq still 0 at key-on
            ChipWrite(60000, "c140", 0, 0x03, 0x00),
            ChipWrite(60000, "c140", 0, 0x05, mode),
            ChipWrite(60030, "c140", 0, 0x02, 0xC8),   # real pitch right after
            ChipWrite(60035, "c140", 0, 0x03, 0x00),
        ] + common(1) + [
            ChipWrite(60000, "c140", 0, 0x12, 0x64),   # normal: pitch pre-key-on
            ChipWrite(60000, "c140", 0, 0x13, 0x00),
            ChipWrite(60100, "c140", 0, 0x15, mode),
        ]
        roms = [RomSlice(0, len(rom), 0, bytes(rom), "c140")]
        song = analyze(_vgm(writes, roms, c140_clock=12288000, c140_type=1), pcm=True)
        smp = song.c140_samples[0]
        self.assertEqual(smp.mode_freq(), 0xC800)
        ons = [e for e in song.events if e.on and e.pcm]
        self.assertEqual(sorted(e.note for e in ons), [96, 108])

    def test_note_rows_keep_their_volume_with_fade_points(self):
        # Fade points land between (and never on top of) note rows: a note's
        # own volume wins, and the stepped values show up as their own rows.
        tail = [
            ChipWrite(80000, "c140", 0, 0x01, 0x40),   # L 200 -> 64, R still 100
            ChipWrite(82000, "c140", 0, 0x00, 0x28),   # R 100 -> 40: max 64
            ChipWrite(83000, "c140", 0, 0x05, 0x80),   # retrigger voice 0
        ]
        song = TestC140()._song(tail=tail)
        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        rows = self._all_rows(mod, 8)
        notes = [row for row in rows if 0 <= row.note < 180]
        self.assertEqual([row.vol for row in notes], [200, 64])
        self.assertIn(100, [row.vol for row in rows])

    def test_fade_in_from_zero_volume_keeps_default_pan(self):
        # A hit keyed with both registers at zero must not write 08 00: the
        # fade steps only move the volume column, so a "both off" pan would
        # mute the voice forever. The note starts silent, pan untouched, and
        # the balance steps ride along with the volume points.
        tail = [
            ChipWrite(80000, "c140", 0, 0x01, 0x01),   # L 0 -> 1
            ChipWrite(81000, "c140", 0, 0x00, 0x01),   # R 0 -> 1
            ChipWrite(84000, "c140", 0, 0x01, 0x40),   # L -> 64
            ChipWrite(85000, "c140", 0, 0x00, 0x40),   # R -> 64
            ChipWrite(100000, "c140", 0, 0x05, 0x00),  # key off
        ]
        song = TestC140()._song(key_vol=(0, 0), tail=tail)
        ons = [e for e in song.events if e.on and e.pcm]
        ev = ons[0]
        self.assertEqual(ev.vol, 0)
        self.assertTrue(ev.no_pan)
        self.assertEqual([v for _t, v, _p in ev.vol_pts], [1, 1, 64, 64])
        self.assertEqual(ev.vol_pts[0][2], 0xF0)   # left-only step
        self.assertEqual(ev.vol_pts[-1][2], 0xFF)  # both full

        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        rows = self._all_rows(mod, 8)
        notes = [row for row in rows if 0 <= row.note < 180]
        self.assertEqual(len(notes), 1)
        self.assertFalse(any(c == 0x08 for c, _v in notes[0].fx))
        self.assertEqual(notes[0].vol, 0)
        self.assertIn(64, [row.vol for row in rows])
        self.assertIn(0xFF, [v for row in rows for c, v in row.fx if c == 0x08])


class TestCollectHelpers(unittest.TestCase):
    def test_rate_scales(self):
        from vgm2151fur.c140 import c140_rate
        from vgm2151fur.c352 import c352_rate
        from vgm2151fur.segapcm import segapcm_rate
        # Chip step-rate conventions: SegaPCM 32768, C140/C352 reference
        # divisor 18874368. 24192000 * 0x8000 / 18874368 is 42000 exactly.
        self.assertAlmostEqual(segapcm_rate(4000000, 132), 4000000 * 132 / 32768, delta=1)
        self.assertAlmostEqual(c140_rate(12288000, 25600), 12288000 * 25600 / 18874368, delta=1)
        self.assertEqual(c352_rate(24192000, 0x8000), 42000)


def _raw_c352(cmds: bytes, rom_blocks=(), clock=24192000, divider=72, mute_rear=False) -> bytes:
    """VGM 1.71 with a 0x100 header so the C352 clock at 0xDC is in the header."""
    buf = bytearray(0x100)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x171).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x30:0x34] = (3579545).to_bytes(4, "little")
    buf[0x34:0x38] = (0x100 - 0x34).to_bytes(4, "little")
    raw = clock & 0x3FFFFFFF
    if mute_rear:
        raw |= 0x80000000
    buf[0xDC:0xE0] = raw.to_bytes(4, "little")
    buf[0xD6] = divider & 0xFF
    body = bytearray()
    for typ, start, data in rom_blocks:
        blk = (0x10000).to_bytes(4, "little") + start.to_bytes(4, "little") + data
        body += bytes([0x67, 0x66, typ]) + len(blk).to_bytes(4, "little") + blk
    body += cmds + b"\x66"
    return bytes(buf) + bytes(body)


def _e1(addr: int, val: int) -> bytes:
    return bytes([0xE1]) + (addr & 0xFFFF).to_bytes(2, "big") + (val & 0xFFFF).to_bytes(2, "big")


class TestC352(unittest.TestCase):
    def _body(self, n=200) -> bytes:
        return bytes(((i * 3 + 1) & 0xFF) for i in range(n))

    def _regs(self, sample, voice=0, chip=0, **regs):
        names = ("vol_f", "vol_r", "freq", "flags", "bank", "start", "end", "loop")
        out = []
        for i, name in enumerate(names):
            if name in regs:
                out.append(ChipWrite(
                    sample, "c352", chip, voice * 8 + i, regs[name] & 0xFFFF,
                ))
        return out

    def _strobe(self, sample, chip=0, val=0):
        return ChipWrite(sample, "c352", chip, 0x202, val)

    def _song(self, writes, rom, *, chip=0, rom_size=None, **kw):
        roms = [RomSlice(chip, rom_size or len(rom), 0, bytes(rom), "c352")]
        return analyze(
            _vgm(_lattice() + list(writes), roms, c352_clock=24192000, c352_divider=72, **kw),
            pcm=True,
        )

    def _oneshot_writes(self, when=60000, **regs):
        body_end = 0x100 + 200
        base = dict(
            vol_f=0x4000, vol_r=0x0010, freq=0x8000, flags=0x4000,
            bank=0, start=0x100, end=body_end, loop=0,
        )
        base.update(regs)
        return self._regs(when, **base)

    def test_clock_formula_and_header(self):
        self.assertEqual(c352_effective_clock(24192000, 72), 24192000)
        self.assertEqual(c352_effective_clock(24192000, 0), 24192000)
        self.assertEqual(c352_effective_clock(336000, 1), 24192000)
        self.assertEqual(c352_effective_clock(336000, 0), 24192000)
        self.assertEqual(c352_effective_clock(0, 72), 0)

        raw = _raw_c352(_e1(2, 0x8000) + _e1(0x8002, 0x1000), [(0x92, 0, bytes(16))], mute_rear=True)
        self.assertIn("c352", chips_in_head(raw[:0x100]))
        self.assertNotIn("c352", chips_in_head(_raw_vgm(b"")))
        p = TMP / "c352_hdr.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.c352_clock, 24192000)
        self.assertEqual(v.c352_divider, 72)
        self.assertTrue(v.c352_mute_rear)
        self.assertEqual([s.kind for s in v.roms], ["c352"])
        writes = [w for w in v.writes if w.chip == "c352"]
        self.assertEqual(
            [(w.chip_id, w.reg, w.val) for w in writes],
            [(0, 2, 0x8000), (1, 2, 0x1000)],
        )
        self.assertFalse(any("does not convert" in w for w in v.warnings))

        p.write_bytes(_raw_c352(b"", clock=336000, divider=1))
        self.assertEqual(load_vgm(p).c352_clock, 24192000)
        p.write_bytes(_raw_c352(b"", clock=24192000, divider=0))
        self.assertEqual(load_vgm(p).c352_clock, 24192000)

        buf = bytearray(_raw_c352(b"", clock=0, divider=36))
        buf[0xBC:0xC0] = (4).to_bytes(4, "little")
        buf[0xC0:0xC4] = (12).to_bytes(4, "little")
        buf[0xC4:0xC8] = (8).to_bytes(4, "little")
        buf[0xCC] = 1
        buf[0xCD] = 0x27
        buf[0xCE:0xD2] = (12_000_000).to_bytes(4, "little")
        p.write_bytes(bytes(buf))
        v = load_vgm(p)
        self.assertEqual(v.c352_clock, 12_000_000)
        self.assertEqual(v.c352_divider, 36)
        self.assertIn("c352", chips_in_head(bytes(buf)))

    def test_oneshot_is_exclusive_and_needs_the_strobe(self):
        body = self._body()
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = body
        song = self._song(self._oneshot_writes(50000) + [self._strobe(60000)], rom)
        self.assertEqual(len(song.c352_samples), 1)
        smp = song.c352_samples[0]
        self.assertEqual(smp.data, body)
        self.assertEqual(smp.length, 200)
        self.assertEqual(smp.depth, 8)
        self.assertIsNone(smp.loop_start)
        self.assertEqual(smp.hits[0].sample_time, 60000)
        self.assertEqual(smp.mode_freq(), 0x8000)
        self.assertEqual(smp.c4_rate(24192000), 42000)
        self.assertEqual((smp.hits[0].vol_l, smp.hits[0].vol_r), (0x40, 0x10))
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.ch, 8)
        self.assertEqual(ev.vol, 0x40)
        self.assertEqual(ev.pan, 0xF4)
        self.assertEqual(ev.ins, len(song.fm_patches))

        silent = self._song(self._oneshot_writes(60000), rom)
        self.assertEqual(silent.c352_samples, [])

        again = self._song(
            self._oneshot_writes(50000) + [self._strobe(60000), self._strobe(70000, val=0xFFFF)],
            rom,
        )
        self.assertEqual(len(again.c352_samples[0].hits), 1)

    def test_loop_includes_the_end_byte(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        rom[0x1C7] = 0x5A
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4002,
            bank=0, start=0x100, end=0x1C7, loop=0x120,
        ) + [self._strobe(60000)]
        song = self._song(writes, rom)
        smp = song.c352_samples[0]
        self.assertEqual(smp.length, 200)
        self.assertEqual(smp.data[-1], 0x5A)
        self.assertEqual(smp.data, bytes(rom[0x100:0x1C8]))
        self.assertEqual((smp.loop_start, smp.loop_end, smp.loop_mode), (0x20, 200, 0))

    def test_key_off_write_stops_at_the_write(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._regs(
            50000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4002,
            bank=0, start=0x100, end=0x1C7, loop=0x120,
        ) + [
            self._strobe(60000),
            # the KEYOFF write clears BUSY: the chip is silent from the write
            *self._regs(80000, flags=0x2002),
            self._strobe(90000),
        ]
        song = self._song(writes, rom)
        offs = [e for e in song.events if not e.on and e.ch == 8]
        self.assertEqual([e.sample for e in offs], [80000])

    def test_mid_note_keyoff_write_stops_at_the_write(self):
        # The driver writes flags 0x2000 mid-note. The write clears BUSY, so
        # libvgm is silent from the write on (no run to wave_end); the later
        # strobe finds it already stopped. One note-off, at the write.
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._regs(
            50000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4002,
            bank=0, start=0x100, end=0x1C7, loop=0x120,
        ) + [
            self._strobe(60000),
            *self._regs(80000, flags=0x2000),
            self._strobe(90000),
        ]
        song = self._song(writes, rom)
        offs = [e for e in song.events if not e.on and e.ch == 8]
        self.assertEqual(len(offs), 1)
        self.assertLess(abs(offs[0].sample - 80000), 4)

    def test_retrigger_does_not_emit_a_note_off(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._oneshot_writes(50000) + [
            self._strobe(60000),
            *self._oneshot_writes(80000),
            self._strobe(90000),
        ]
        song = self._song(writes, rom)
        smp = song.c352_samples[0]
        self.assertEqual(len(smp.hits), 2)
        self.assertEqual(smp.hits[0].off_sample, 90000)
        self.assertEqual(smp.hits[1].off_sample, 0)
        self.assertEqual([e for e in song.events if not e.on and e.pcm], [])

    def test_mute_rear_drops_the_rear_pair(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        song = self._song(
            self._oneshot_writes(60000) + [self._strobe(60000)], rom,
            c352_mute_rear=True,
        )
        hit = song.c352_samples[0].hits[0]
        self.assertEqual((hit.vol_l, hit.vol_r), (0x40, 0))
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.vol, 0x40)
        self.assertEqual(ev.pan, 0xF0)

    def test_mulaw_is_the_c352_table(self):
        from vgm2151fur.c140 import _mulaw_i16 as c140_mulaw

        rom = bytearray(0x120)
        rom[0x100] = 0x01
        rom[0x101] = 0x81
        for i in range(0x102, 0x110):
            rom[i] = 0x02
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4008,
            bank=0, start=0x100, end=0x110, loop=0,
        ) + [self._strobe(60000)]
        song = self._song(writes, rom)
        smp = song.c352_samples[0]
        self.assertEqual(smp.depth, 16)
        self.assertEqual(smp.length, 16)
        self.assertEqual(struct.unpack_from("<h", smp.data, 0)[0], 32)
        self.assertEqual(struct.unpack_from("<h", smp.data, 2)[0], -64)
        self.assertEqual(c140_mulaw(1), 256)

    def test_reverse_plays_down_to_the_end(self):
        rom = bytearray(0x300)
        for i in range(0x101, 0x1C9):
            rom[i] = (i * 3 + 1) & 0xFF
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4001,
            bank=0, start=0x1C8, end=0x100, loop=0,
        ) + [self._strobe(60000)]
        song = self._song(writes, rom)
        smp = song.c352_samples[0]
        heard = bytes(rom[i] for i in range(0x1C8, 0x100, -1))
        self.assertEqual(smp.data, heard)
        self.assertEqual(smp.length, 200)

    def test_reverse_wrap_crosses_the_bank(self):
        n = 0x3D1C
        body = bytes((i * 7 + 5) & 0xFF for i in range(n))
        roms = [RomSlice(0, 0x1000000, 0x5C2E5, body, "c352")]
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4001,
            bank=0x0006, start=0x0000, end=0xC2E4, loop=0,
        ) + [self._strobe(60000)]
        song = analyze(_vgm(writes, roms, c352_clock=24192000), pcm=True)
        smp = song.c352_samples[0]
        self.assertEqual(smp.length, n)
        self.assertEqual(smp.data, body[::-1])
        self.assertFalse(any("wrap" in w for w in song.warnings))

    def test_reverse_wrap_at_bank_zero_is_silent(self):
        # the walk would leave the ROM window; the reference reads nothing there
        roms = [RomSlice(0, 0x1000000, 0xFFFFFF, bytes([0xAA]), "c352")]
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4001,
            bank=0x0000, start=0x0000, end=0xFFF8, loop=0,
        ) + [self._strobe(60000)]
        song = analyze(_vgm(writes, roms, c352_clock=24192000), pcm=True)
        self.assertEqual(song.c352_samples, [])
        self.assertTrue(any("wrap" in w for w in song.warnings))

    def test_loop_clear_stop_survives_other_strobes(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body(200)
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4002,
            bank=0, start=0x100, end=0x1C7, loop=0x120,
        ) + [self._strobe(60000)]
        writes += self._regs(70000, flags=0x0000)
        writes += [self._strobe(70001)]
        vgm = _vgm(writes, [RomSlice(0, 0x300, 0, bytes(rom), "c352")], c352_clock=24192000)
        _rom, _samples, offs, _warnings = collect_c352(vgm)
        self.assertTrue(offs, "the natural end must still fire")
        self.assertTrue(all(o.sample_time >= 70000 for o in offs))

    def test_flags_clear_of_busy_stops_the_voice(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body(200)
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4000,
            bank=0, start=0x100, end=0x1C7, loop=0,
        ) + [self._strobe(60000)]
        writes += self._regs(70000, flags=0x0000)
        vgm = _vgm(writes, [RomSlice(0, 0x300, 0, bytes(rom), "c352")], c352_clock=24192000)
        _rom, _samples, offs, _warnings = collect_c352(vgm)
        self.assertEqual([o.sample_time for o in offs], [70000])

    def test_busy_clear_before_retrigger_keeps_one_note(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body(200)
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4002,
            bank=0, start=0x100, end=0x1C7, loop=0x120,
        ) + [self._strobe(60000)]
        writes += self._regs(70000, flags=0x0000)
        writes += self._regs(70010, flags=0x4002)
        writes += [self._strobe(70020)]
        vgm = _vgm(writes, [RomSlice(0, 0x300, 0, bytes(rom), "c352")], c352_clock=24192000)
        _rom, samples, offs, _warnings = collect_c352(vgm)
        self.assertEqual(offs, [])
        self.assertEqual(len(samples[0].hits), 2)

    def test_geometry_rewrite_keeps_the_note(self):
        # a mid-note geometry rewrite (no strobe) does not start a new note:
        # the module keeps the body the key-on extracted
        rom0 = bytearray(0x200)
        rom0[0x110:0x191] = self._body(0x81)
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4022,
            bank=0x0000, start=0x0110, end=0x0190, loop=0x0300,
        ) + [self._strobe(60000)]
        writes += self._regs(70000, start=0x0120, end=0x01A0, loop=0x0310)
        writes += [self._strobe(70100)]
        vgm = _vgm(writes, [RomSlice(0, 0x1000000, 0, bytes(rom0), "c352")],
                   c352_clock=24192000)
        _rom, samples, offs, _warnings = collect_c352(vgm)
        hits = [h for s in samples for h in s.hits if h.voice == 0]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].sample_time, 60000)

    def test_ping_pong_loop_mode(self):
        rom = bytearray(0x300)
        rom[0x120:0x1C8] = self._body(168)
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4003,
            bank=0, start=0x120, end=0x1C7, loop=0x120,
        ) + [self._strobe(60000)]
        song = self._song(writes, rom)
        smp = song.c352_samples[0]
        self.assertEqual(smp.data, bytes(rom[0x120:0x1C8]))
        self.assertEqual((smp.loop_start, smp.loop_end, smp.loop_mode), (0, 168, 2))

    def test_ping_pong_start_below_the_region_keeps_the_attack(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body(200)
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4003,
            bank=0, start=0x100, end=0x1C7, loop=0x140,
        ) + [self._strobe(60000)]
        song = self._song(writes, rom)
        smp = song.c352_samples[0]
        self.assertEqual(smp.data, bytes(rom[0x100:0x1C8]))
        self.assertEqual((smp.loop_start, smp.loop_end, smp.loop_mode), (0x40, 200, 2))

    def test_link_outside_the_dump_keeps_the_intro(self):
        rom = bytearray(0x80)
        rom[0x20:0x31] = self._body(17)
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4022,
            bank=0, start=0x20, end=0x30, loop=0x10,
        ) + [self._strobe(60000)]
        song = self._song(writes, rom, rom_size=0x1000000)
        self.assertEqual(len(song.c352_samples), 1)
        smp = song.c352_samples[0]
        self.assertEqual(smp.data, bytes(rom[0x20:0x31]))
        self.assertIsNone(smp.loop_start)
        self.assertTrue(any("link" in w for w in song.warnings))

    def test_undumped_base_is_not_a_sample(self):
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4000,
            bank=0, start=0, end=0x20, loop=0,
        ) + [self._strobe(60000)]
        roms = [RomSlice(0, 0x20000, 0x10000, bytes(0x20), "c352")]
        song = analyze(_vgm(writes, roms, c352_clock=24192000), pcm=True)
        self.assertEqual(song.c352_samples, [])
        self.assertTrue(any("not dump" in w for w in song.warnings))

    def test_high_bank_bits_fold_through_the_rom_window(self):
        # libvgm reads wave[pos & pow2_mask(size)], so a driver address with
        # bits above the window aliases its low bits: 0x8001:0x0000 in a 16 MB
        # window is the byte at 0x10000. Tekken 2 keys its 0x80B1-style
        # voices exactly like that (1037 triggers on 17 Mid Boss).
        body = self._body()
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4000,
            bank=0x8001, start=0x0000, end=len(body), loop=0,
        ) + [self._strobe(60000)]
        roms = [RomSlice(0, 0x1000000, 0x10000, bytes(body), "c352")]
        song = analyze(_vgm(writes, roms, c352_clock=24192000), pcm=True)
        self.assertEqual(len(song.c352_samples), 1)
        self.assertEqual(song.c352_samples[0].data[:len(body)], bytes(body))
        self.assertFalse(any("not dump" in w for w in song.warnings))

    def test_high_bank_bits_that_fold_into_a_hole_stay_silent(self):
        # the folded offset must still land in a dumped slice: this one lands
        # past the body, where the reference reads the hole
        body = self._body()
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4000,
            bank=0x8002, start=0x0000, end=len(body), loop=0,
        ) + [self._strobe(60000)]
        roms = [RomSlice(0, 0x1000000, 0x10000, bytes(body), "c352")]
        song = analyze(_vgm(writes, roms, c352_clock=24192000), pcm=True)
        self.assertEqual(song.c352_samples, [])
        self.assertTrue(any("not dump" in w for w in song.warnings))

    def test_late_pitch_write_within_a_frame_is_the_note(self):
        # these drivers strobe first and write the note's own pitch inside the
        # same 60 Hz frame (Ending BGM: 232 hits, up to +18.9 st); the key-on
        # register is the previous note's value, not a slide start
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._oneshot_writes(60000, freq=0x4000) + [
            self._strobe(60000),
            ChipWrite(60400, "c352", 0, 2, 0x8000),
        ]
        song = self._song(writes, rom)
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.sweep, ())
        self.assertEqual(song.c352_samples[0].mode_freq(), 0x8000)

    def test_late_volume_write_within_a_frame_sets_the_attack(self):
        # Ridge Racer 2 zeroes the volume when it stops a voice, so the strobe
        # sees 0 and the real balance arrives 8 ms later; the note must start
        # at the written balance or the attack plays silent
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._oneshot_writes(60000, vol_f=0x0000) + [
            self._strobe(60000),
            ChipWrite(60400, "c352", 0, 0, 0x8000),
        ]
        song = self._song(writes, rom)
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.vol, 0x80)
        self.assertEqual(ev.pan, 0xF2)

    def test_loop_point_one_past_the_end_extends_the_body(self):
        # Tekken Tag 05 writes wave_end 0x...8d and wave_loop 0x...8e: the chip
        # jumps to the contiguous byte, plays a full bank there and repeats.
        # The body has to carry that region or the sample plays its intro once.
        rom = bytearray(0x20000)
        rom[0x100:0x1C8] = self._body()
        for i in range(len(rom)):
            rom[i] = (i * 7 + 3) & 0xFF
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4002,
            bank=0, start=0x0100, end=0x01C7, loop=0x01C8,
        ) + [self._strobe(60000)]
        song = self._song(writes, rom, rom_size=0x20000)
        self.assertEqual(len(song.c352_samples), 1)
        smp = song.c352_samples[0]
        self.assertEqual(smp.loop_start, 0xC8)
        self.assertEqual(len(smp.data), 0xC8 + 0x10000)
        self.assertEqual(smp.data[0xC8:0xC8 + 4], bytes((0x1C8 * 7 + 3 + 7 * k) & 0xFF
                                                         for k in range(4)))
        self.assertEqual(smp.loop_mode, 0)

    def test_geometry_rewrite_before_the_crossing_moves_the_loop(self):
        # Tekken Tag 05 pairs the key-on geometry (start=0x4cd4 / loop=0x009b)
        # with a rewrite 8 ms later (start=0x009b / loop=0x928e) that points
        # the LINK jump at the byte after the intro - the sample's real
        # continuation. The chip reads the live registers at the crossing,
        # so the body has to use the rewrite, not the key-on values.
        rom = bytearray(0x40000)
        for i in range(len(rom)):
            rom[i] = (i * 7 + 3) & 0xFF
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4022,
            bank=0, start=0x0100, end=0x01C7, loop=0x2000,
        ) + [
            self._strobe(60000),
            ChipWrite(60100, "c352", 0, 7, 0x01C8),  # loop one past the end
            ChipWrite(60100, "c352", 0, 5, 0x0000),  # start as the target's high half
        ]
        song = self._song(writes, rom, rom_size=0x40000)
        self.assertEqual(len(song.c352_samples), 1)
        smp = song.c352_samples[0]
        self.assertEqual(smp.loop_start, 0xC8)
        self.assertEqual(len(smp.data), 0xC8 + 0x10000)
        self.assertEqual(smp.data[0xC8:0xC8 + 4], bytes(
            (0x1C8 * 7 + 3 + 7 * k) & 0xFF for k in range(4)))

    def test_geometry_rewrite_after_the_crossing_is_ignored(self):
        # a write that lands after the voice already crossed wave_end is for
        # the next pass and must not move the body
        rom = bytearray(0x40000)
        for i in range(len(rom)):
            rom[i] = (i * 5 + 1) & 0xFF
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4002,
            bank=0, start=0x0100, end=0x01C7, loop=0x0101,
        ) + [
            self._strobe(60000),
            ChipWrite(60700, "c352", 0, 7, 0x01C8),  # well past the crossing
            ChipWrite(60700, "c352", 0, 5, 0x0000),
        ]
        song = self._song(writes, rom, rom_size=0x40000)
        self.assertEqual(len(song.c352_samples), 1)
        smp = song.c352_samples[0]
        self.assertEqual(smp.loop_start, 0x01)

    def test_stopped_note_does_not_carry_the_loop_region(self):
        # a note the driver stops before the running address reaches wave_end
        # never loops, so the payload stays the intro - the big loop bodies
        # are only built for notes that actually reach them
        rom = bytearray(0x20000)
        for i in range(len(rom)):
            rom[i] = (i * 7 + 3) & 0xFF
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4002,
            bank=0, start=0x0100, end=0x01C7, loop=0x01C8,
        ) + [
            self._strobe(60000),
            ChipWrite(60050, "c352", 0, 3, 0x0000),  # stopped 50 samples in
            self._strobe(60050),
        ]
        song = self._song(writes, rom, rom_size=0x20000)
        self.assertEqual(len(song.c352_samples), 1)
        smp = song.c352_samples[0]
        self.assertIsNone(smp.loop_start)
        self.assertEqual(smp.length, 0xC8)

    def test_freq_slide_and_post_key_refit(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._oneshot_writes(60000) + [
            self._strobe(60000),
            ChipWrite(80000, "c352", 0, 2, 0x4000),
        ]
        song = self._song(writes, rom)
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.sweep, ((80000, -768),))

        writes = _lattice() + self._oneshot_writes(60000, freq=0) + [
            self._strobe(60000),
            ChipWrite(60050, "c352", 0, 2, 0x8000),
        ]
        song = self._song(writes, rom)
        smp = song.c352_samples[0]
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(smp.mode_freq(), 0x8000)
        self.assertEqual((ev.note, ev.e5), (108, 0x80))
        self.assertEqual(ev.sweep, ())

    def test_fur_chip_instrument_and_volume(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        song = self._song(self._oneshot_writes(60000) + [self._strobe(60000)], rom)
        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0x82, CHIP_C352])
        self.assertEqual(mod.n_ch, 8 + 32)
        ins_types = []
        for _off, mag, _sz, block in iter_blocks(data):
            if mag == b"INS2":
                ins_types.append(struct.unpack_from("<H", block, 2)[0])
        self.assertIn(INS_C352, ins_types)
        smps = list_fur_samples(data)
        self.assertEqual(smps[0].n_frames, 200)
        self.assertEqual(smps[0].depth, 8)
        self.assertEqual(smps[0].c4_rate, 42000)
        self.assertEqual(smps[0].loop_mode, 0)
        self.assertIn(struct.pack("<fff", 0.25, 0.0, 0.0), data)
        self.assertIn(b"customClock=24192000", data)
        self.assertIn(b"Furnace 0.6.8.3", data)
        self.assertFalse(any("Furnace" in w for w in song.warnings))

        from vgm2151fur.furwrite import _player_chip_volume
        self.assertAlmostEqual(_player_chip_volume(_vgm([]), 0x27, 1), 0.25)
        self.assertAlmostEqual(_player_chip_volume(_vgm([]), 0x27, 2), 0.125)

    def test_dual_chip_channels(self):
        rom = bytearray(0x300)
        rom[0x100:0x1C8] = self._body()
        writes = _lattice() + self._regs(
            60000, chip=0, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4000,
            bank=0, start=0x100, end=0x1C8, loop=0,
        )
        writes += [self._strobe(60000, chip=0)]
        writes += self._regs(
            60000, chip=1, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4000,
            bank=0, start=0x100, end=0x1C8, loop=0,
        )
        writes += [self._strobe(60000, chip=1)]
        roms = [
            RomSlice(0, len(rom), 0, bytes(rom), "c352"),
            RomSlice(1, len(rom), 0, bytes(rom), "c352"),
        ]
        song = analyze(_vgm(writes, roms, c352_clock=24192000), pcm=True)
        ons = [e.ch for e in song.events if e.on and e.pcm]
        self.assertEqual(sorted(set(ons)), [8, 40])
        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0x82, CHIP_C352, CHIP_C352])
        self.assertEqual(mod.n_ch, 8 + 64)
        self.assertIn(struct.pack("<fff", 0.125, 0.0, 0.0), data)

    def test_ping_pong_fur_loop_mode(self):
        rom = bytearray(0x300)
        rom[0x120:0x1C8] = self._body(168)
        writes = _lattice() + self._regs(
            60000, vol_f=0x4000, vol_r=0, freq=0x8000, flags=0x4003,
            bank=0, start=0x120, end=0x1C7, loop=0x120,
        ) + [self._strobe(60000)]
        song = self._song(writes, rom)
        data = write_fur(song, include_pcm=True, include_fm=True)
        smp = list_fur_samples(data)[0]
        self.assertEqual(smp.loop_mode, 2)
        self.assertEqual((smp.loop_start, smp.loop_end), (0, 168))


if __name__ == "__main__":
    unittest.main()
