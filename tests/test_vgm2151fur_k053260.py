"""Konami K053260: VGM parsing, register model and .fur emission."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import iter_blocks, list_fur_samples, parse_fur
from vgm2151fur.furwrite import (
    CHIP_K053260, INS_K053260, SMP_DEPTH_ADPCM_K, write_fur,
)
from vgm2151fur.k053260 import (
    collect_k053260, k053260_pan_fx, k053260_rate,
)
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile, chips_in_head, load_vgm

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_k053260"
TMP.mkdir(exist_ok=True)
CLOCK = 3579545


def _lattice(n=40):
    return [
        ChipWrite(sample=k * 5000, chip="ym2151", chip_id=0, reg=0x08, val=0x78)
        for k in range(n)
    ]


def _vgm(writes, roms=(), **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=CLOCK, k007232_clock=0, gd3={},
        roms=list(roms), writes=list(writes), **kw,
    )


def _raw_vgm(cmds: bytes, rom_blocks=(), clock: int = CLOCK) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x30:0x34] = CLOCK.to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    buf[0xAC:0xB0] = clock.to_bytes(4, "little")
    body = bytearray()
    for typ, start, data in rom_blocks:
        blk = (0x10000).to_bytes(4, "little") + start.to_bytes(4, "little") + data
        body += bytes([0x67, 0x66, typ]) + len(blk).to_bytes(4, "little") + blk
    body += cmds + b"\x66"
    return bytes(buf) + bytes(body)


def _body(n=200):
    # No zero bytes, so the trailing-silence trim does not shorten it.
    return bytes(((i * 3 + 1) & 0xFF) for i in range(n))


def _regs(t, voice=0, chip=0, **regs):
    names = ("pitch_lo", "pitch_hi", "len_lo", "len_hi", "start_lo",
             "start_mid", "start_hi", "volume")
    out = []
    for i, name in enumerate(names):
        if name in regs:
            out.append(ChipWrite(
                t, "k053260", chip, 8 + voice * 8 + i, regs[name] & 0xFF,
            ))
    return out


def _key(t, value, chip=0):
    return ChipWrite(t, "k053260", chip, 0x28, value)


def _loop(t, value, chip=0):
    return ChipWrite(t, "k053260", chip, 0x2A, value)


def _pan(t, value, chip=0, second=False):
    return [ChipWrite(t, "k053260", chip, 0x2D if second else 0x2C, value)]


def _start_regs(start):
    return (
        start & 0xFF,
        (start >> 8) & 0xFF,
        (start >> 16) & 0x1F,
    )


class TestK053260Parsing(unittest.TestCase):
    def test_write_clock_and_rom(self):
        raw = _raw_vgm(
            b"\xba\x08\x01\xba\x89\x02",
            [(0x8E, 0, bytes(range(32)))],
        )
        p = TMP / "parse.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.k053260_clock, CLOCK)
        self.assertEqual([s.kind for s in v.roms], ["k053260"])
        self.assertEqual(v.roms[0].start, 0)
        w = [x for x in v.writes if x.chip == "k053260"]
        self.assertEqual(
            [(x.chip_id, x.reg, x.val) for x in w],
            [(0, 0x08, 0x01), (1, 0x09, 0x02)],
        )
        self.assertIn("k053260", chips_in_head(raw[:0xC0]))
        self.assertFalse(any("does not convert" in x for x in v.warnings))

    def test_missing_clock_uses_default(self):
        raw = bytearray(_raw_vgm(b"\xba\x08\x01"))
        raw[0xAC:0xB0] = (0).to_bytes(4, "little")
        p = TMP / "noclk.vgm"
        p.write_bytes(bytes(raw))
        v = load_vgm(p)
        self.assertEqual(v.k053260_clock, 0)
        self.assertTrue(any("K053260 clock" in x for x in v.warnings))

    def test_extra_header_clock(self):
        raw = _raw_vgm(b"")
        header = bytearray(raw[:0xC0])
        header[0xAC:0xB0] = (0).to_bytes(4, "little")
        header[0xBC:0xC0] = (4).to_bytes(4, "little")
        header[0x34:0x38] = (0xD0 - 0x34).to_bytes(4, "little")
        extra = bytearray(16)
        extra[0:4] = (12).to_bytes(4, "little")
        extra[4:8] = (4).to_bytes(4, "little")
        extra[8] = 1
        extra[9] = 0x1D
        extra[10:14] = (8_000_000).to_bytes(4, "little")
        buf = bytes(header) + bytes(extra) + raw[0xC0:]
        p = TMP / "extra.vgm"
        p.write_bytes(buf)
        v = load_vgm(p)
        self.assertEqual(v.k053260_clock, 8_000_000)
        self.assertIn("k053260", chips_in_head(buf))


class TestK053260RegisterModel(unittest.TestCase):
    def _song(self, writes, *, rom=None, clock=CLOCK):
        if rom is None:
            body = _body()
            rom = bytearray(0x300)
            rom[0x101:0x101 + len(body)] = body
        roms = [RomSlice(0, len(rom), 0, bytes(rom), "k053260")]
        return analyze(
            _vgm(_lattice() + list(writes), roms, k053260_clock=clock),
            pcm=True,
        )

    def _key_writes(self, start=0x100, length=200, pitch=0xFC0, volume=100,
                    voice=0, when=60000, loop=False, adpcm=False, reverse=False,
                    chip=0):
        lo, mid, hi = _start_regs(start)
        writes = _regs(
            when, voice, chip=chip,
            pitch_lo=pitch & 0xFF, pitch_hi=(pitch >> 8) & 0x0F,
            len_lo=length & 0xFF, len_hi=(length >> 8) & 0xFF,
            start_lo=lo, start_mid=mid, start_hi=hi, volume=volume,
        )
        if loop or adpcm:
            writes.append(_loop(when, (1 << voice) if loop else 0, chip=chip))
            if adpcm:
                writes.append(_loop(when, (0x10 << voice) | (
                    (1 << voice) if loop else 0), chip=chip))
        return writes + [_key(
            when, (1 << voice) | ((0x10 << voice) if reverse else 0), chip=chip,
        )]

    def test_pcm_body_skips_the_start_byte(self):
        body = _body()
        rom = bytearray(0x300)
        rom[0x100] = 0x7F          # never played
        rom[0x101:0x101 + len(body)] = body
        song = self._song(self._key_writes(), rom=rom)
        self.assertEqual(len(song.k053260_samples), 1)
        smp = song.k053260_samples[0]
        self.assertEqual(smp.start, 0x100)
        self.assertEqual(smp.depth, 8)
        self.assertEqual(smp.data, body)
        self.assertEqual(smp.length, 200)
        self.assertEqual(smp.mode_pitch(), 0xFC0)
        self.assertEqual(smp.c4_rate(CLOCK), 55930)
        self.assertIsNone(smp.loop_start)

        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual((ev.note, ev.e5), (108, 0x80))
        self.assertEqual(ev.vol, 100)
        self.assertEqual(ev.pan, 0xFF)
        self.assertEqual(ev.ch, 8)

        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0x82, CHIP_K053260])
        self.assertEqual(mod.n_ch, 8 + 4)
        ins_types = [
            struct.unpack_from("<H", block, 2)[0]
            for _off, mag, _sz, block in iter_blocks(data) if mag == b"INS2"
        ]
        self.assertIn(INS_K053260, ins_types)
        smps = list_fur_samples(data)
        self.assertEqual(smps[0].depth, 8)
        self.assertEqual(smps[0].n_frames, 200)
        self.assertEqual(smps[0].data, body)
        self.assertEqual(smps[0].c4_rate, 55930)
        # The Furnace core is half the reference core. The raised master
        # carries the boost; the chip float is its master-relative share.
        from vgm2151fur.furio import master_volume
        self.assertEqual(master_volume(data), 2.0)
        self.assertIn(struct.pack("<fff", 0.5, 0.0, 0.0), data)
        self.assertIn(struct.pack("<fff", 1.0, 0.0, 0.0), data)

    def test_adpcm_body_is_stored_as_depth_7(self):
        body = bytes((i * 7 + 3) & 0xFF for i in range(120))
        rom = bytearray(0x200)
        rom[0x101:0x101 + len(body)] = body
        song = self._song(self._key_writes(length=120, adpcm=True), rom=rom)
        smp = song.k053260_samples[0]
        self.assertEqual(smp.depth, 7)
        self.assertEqual(smp.data, body)
        self.assertEqual(smp.length, 240)

        data = write_fur(song, include_pcm=True, include_fm=True)
        fur = list_fur_samples(data)[0]
        self.assertEqual(fur.depth, SMP_DEPTH_ADPCM_K)
        self.assertEqual(fur.n_frames, 240)   # nibbles
        self.assertEqual(fur.data, body)      # raw nibble bytes
        self.assertEqual(fur.loop_start, -1)

    def test_adpcm_trailing_zero_bytes_are_kept(self):
        body = bytes([0x01, 0x02, 0x00, 0x00])
        rom = bytearray(0x200)
        rom[0x101:0x101 + len(body)] = body
        song = self._song(self._key_writes(length=len(body), adpcm=True), rom=rom)
        self.assertEqual(song.k053260_samples[0].data, body)

    def test_loop_marks_the_whole_body(self):
        body = bytes((i * 5 + 1) & 0xFF for i in range(64))
        rom = bytearray(0x200)
        rom[0x101:0x101 + len(body)] = body
        song = self._song(self._key_writes(length=64, loop=True), rom=rom)
        smp = song.k053260_samples[0]
        self.assertEqual((smp.loop_start, smp.loop_end), (0, 64))
        data = write_fur(song, include_pcm=True, include_fm=True)
        fur = list_fur_samples(data)[0]
        self.assertEqual((fur.loop_start, fur.loop_end), (0, 64))
        self.assertEqual(fur.loop_mode, 0)

    def test_adpcm_loop_uses_nibble_loop_points(self):
        body = bytes((i * 9 + 5) & 0xFF for i in range(64))
        rom = bytearray(0x200)
        rom[0x101:0x101 + len(body)] = body
        song = self._song(
            self._key_writes(length=64, adpcm=True, loop=True), rom=rom,
        )
        smp = song.k053260_samples[0]
        self.assertEqual((smp.loop_start, smp.loop_end), (0, 128))
        data = write_fur(song, include_pcm=True, include_fm=True)
        fur = list_fur_samples(data)[0]
        self.assertEqual(fur.depth, 7)
        self.assertEqual(fur.n_frames, 128)
        self.assertEqual((fur.loop_start, fur.loop_end), (0, 128))

    def test_reverse_pcm_plays_the_previous_bytes(self):
        body = bytes((i * 3 + 1) & 0xFF for i in range(50))
        rom = bytearray(0x300)
        rom[0x100 - len(body):0x100] = body
        song = self._song(
            self._key_writes(start=0x100, length=len(body), reverse=True),
            rom=rom,
        )
        smp = song.k053260_samples[0]
        self.assertTrue(smp.reverse)
        self.assertEqual(smp.data, body[::-1])
        self.assertEqual(smp.c4_rate(CLOCK), 55930)

    def test_reverse_adpcm_swaps_nibbles(self):
        body = bytes([0x12, 0x34, 0x56, 0x78])
        rom = bytearray(0x100)
        rom[0x100 - len(body):0x100] = body
        song = self._song(
            self._key_writes(
                start=0x100, length=len(body), adpcm=True, reverse=True,
            ),
            rom=rom,
        )
        smp = song.k053260_samples[0]
        self.assertEqual(smp.depth, 7)
        self.assertEqual(smp.data, bytes([0x87, 0x65, 0x43, 0x21]))

    def test_key_off_emits_note_off(self):
        song = self._song(self._key_writes(when=60000) + [_key(90000, 0)])
        offs = [e for e in song.events if not e.on and e.ch == 8]
        self.assertEqual(len(offs), 1)
        self.assertEqual(offs[0].sample, 90000)
        data = write_fur(song, include_pcm=True, include_fm=True)
        self.assertEqual(parse_fur(data).chips, [0x82, CHIP_K053260])

    def test_volume_and_pan_changes_become_points(self):
        writes = self._key_writes(volume=100)
        writes += _regs(70000, volume=40)
        writes += _pan(71000, 1)  # voice 0 hard left
        writes += _pan(72000, 2)  # voice 0 left-of-centre
        song = self._song(writes)
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.vol, 100)
        self.assertEqual(ev.pan, 0xFF)
        self.assertEqual(
            [(v, p) for _t, v, p in ev.vol_pts],
            [(40, None), (40, 0xF0), (40, 0xF7)],
        )

    def test_pitch_change_becomes_a_sweep(self):
        writes = self._key_writes(pitch=0xFC0)
        writes += _regs(80000, pitch_lo=0x80, pitch_hi=0x0F)  # 0xF80, half rate
        song = self._song(writes)
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.sweep, ((80000, -768),))
        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        rows = [
            row for (ch, _idx), pat in mod.patterns.items() if ch == 8
            for row in pat
        ]
        speed = song.speed or 1
        params = [v for r in rows for c, v in r.fx if c == 0x02]
        self.assertIn(round(min(255.0, 1536.0 / speed)), params)
        note_row = next(r for r in rows if 0 <= r.note < 180)
        self.assertFalse(any(c in (0x01, 0x02) for c, _v in note_row.fx))

    def test_second_instance_uses_its_own_channels(self):
        body = _body()
        arr = bytearray(0x300)
        arr[0x101:0x101 + len(body)] = body
        roms = [
            RomSlice(0, 0x300, 0, bytes(arr), "k053260"),
            RomSlice(1, 0x300, 0, bytes(arr), "k053260"),
        ]
        writes = self._key_writes(voice=0, when=60000)
        writes += [_key(90000, 0)]
        writes += self._key_writes(voice=0, when=100000, chip=1)
        song = analyze(_vgm(_lattice() + writes, roms, k053260_clock=CLOCK), pcm=True)
        ons = [e.ch for e in song.events if e.on and e.pcm]
        self.assertEqual(ons, [8, 12])
        data = write_fur(song, include_pcm=True, include_fm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [0x82, CHIP_K053260, CHIP_K053260])
        self.assertEqual(mod.n_ch, 8 + 8)

    def test_attack_refit_uses_the_first_frame_write(self):
        writes = self._key_writes(pitch=0, volume=0)
        writes += _regs(60200, pitch_lo=0xC0, pitch_hi=0x0F, volume=90)
        song = self._song(writes)
        smp = song.k053260_samples[0]
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(smp.mode_pitch(), 0xFC0)
        self.assertEqual((ev.note, ev.e5), (108, 0x80))
        self.assertEqual(ev.vol, 90)
        self.assertEqual(ev.sweep, ())

    def test_pan_zero_key_on_is_silent_until_it_moves(self):
        writes = self._key_writes(volume=100) + _pan(59000, 0)
        writes += _pan(80000, 4)
        song = self._song(writes)
        ev = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(ev.vol, 0)
        self.assertTrue(ev.no_pan)
        self.assertEqual(
            [(v, p) for _t, v, p in ev.vol_pts],
            [(100, 0xFF)],
        )

    def test_same_body_shared_and_different_loop_split(self):
        writes = self._key_writes(when=60000)
        writes += [_key(70000, 0)]
        writes += self._key_writes(when=80000, loop=True)
        song = self._song(writes)
        self.assertEqual(len(song.k053260_samples), 2)
        self.assertEqual(
            [s.loop_start for s in song.k053260_samples], [None, 0]
        )
        self.assertTrue(all(len(s.hits) == 1 for s in song.k053260_samples))

    def test_undumped_rom_warns(self):
        song = self._song(self._key_writes(start=0x400))
        self.assertEqual(song.k053260_samples, [])
        self.assertTrue(any("not dump" in w for w in song.warnings))

    def test_mode_change_at_a_key_off_is_not_a_warning(self):
        writes = self._key_writes(when=60000)
        writes += [_loop(70000, 0x10), _key(70000, 0)]
        song = self._song(writes)
        self.assertFalse(any("KADPCM mode changes" in w for w in song.warnings))

    def test_no_rom_block_warns(self):
        song = analyze(
            _vgm(_lattice() + self._key_writes(), k053260_clock=CLOCK),
            pcm=True,
        )
        self.assertEqual(song.k053260_samples, [])
        self.assertTrue(any("no K053260 ROM block" in w for w in song.warnings))

    def test_clock_rate_and_pan_helpers(self):
        self.assertAlmostEqual(k053260_rate(CLOCK, 0xFC0), 55930.39, delta=0.1)
        self.assertAlmostEqual(k053260_rate(CLOCK, 0xF80), 27965.2, delta=0.1)
        self.assertEqual(
            [k053260_pan_fx(p) for p in range(8)],
            [0x00, 0xF0, 0xF7, 0xFB, 0xFF, 0xBF, 0x7F, 0x0F],
        )
        from vgm2151fur.furwrite import k053260_flag_text
        self.assertEqual(k053260_flag_text(CLOCK), "clockSel=0")
        self.assertEqual(k053260_flag_text(4_000_000), "clockSel=1")
        self.assertEqual(k053260_flag_text(8_000_000), "customClock=8000000")


if __name__ == "__main__":
    unittest.main()
