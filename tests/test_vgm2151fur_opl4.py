"""Yamaha YMF278B (OPL4): OPL3-shaped FM plus 24 wave channels.

The FM half is the shared OPL path under the OPL3 tables; the tests here cover
the ``$D0`` port split, the 12-byte wave descriptor table, sample extraction
from the YRW801/ROM and the 0x87 RAM image, the 42-channel system, the rhythm
kit, and a synthetic wave round trip through Furnace.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import CHIP_CHANNELS, iter_blocks, parse_fur
from vgm2151fur.furwrite import (
    CHIP_OPL4, CHIP_OPL4_DRUMS, INS_MULTIPCM, INS_OPL, write_fur,
)
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.opl import KIND_OPL3, OPLState
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile, chips_in_head, load_vgm
from vgm2151fur.ymf278b import (
    OPL4_WAVE_SLOTS,
    collect_opl4_wave,
    opl4_pan_fx,
    opl4_rate,
    opl4_step,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_opl4"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
CLOCK = 33868800


def _descriptor(bits: int, start: int, loop: int, frames: int) -> bytes:
    # byte 3/4 = loop high/low, byte 5/6 = end high/low (ymf278.cpp).
    end = (0x10000 - frames) & 0xFFFF
    return bytes([
        (bits << 6) | ((start >> 16) & 0x3F),
        (start >> 8) & 0xFF, start & 0xFF,
        (loop >> 8) & 0xFF, loop & 0xFF,
        (end >> 8) & 0xFF, end & 0xFF,
        0, 0, 0, 0, 0,
    ])


def _rom(wave: int, sample: bytes, start: int = 0x1000) -> bytes:
    """A wave memory with one 8-bit descriptor for `wave`."""
    mem = bytearray(start + len(sample) + 16)
    off = wave * 12
    mem[off:off + 12] = _descriptor(0, start, 0x10, len(sample))
    mem[start:start + len(sample)] = sample
    return bytes(mem)


def _vgm(writes, roms=(), **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=kw.pop("total_samples", 400000),
        loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=list(roms), writes=list(writes), ymf278b_clock=CLOCK, **kw,
    )


def _wave_reg(slot: int, sub: int) -> int:
    return (2 << 8) | (0x08 + slot + sub * 24)


def _wave_note(t: int, *, wave: int = 16, slot: int = 0, fnum: int = 0x200,
               octave: int = 2, tl: int = 0x20, off: int = 8000):
    return [
        ChipWrite(t, "ymf278b", 0, _wave_reg(slot, 0), wave & 0xFF),
        ChipWrite(t, "ymf278b", 0, _wave_reg(slot, 1),
                  ((fnum & 0x7F) << 1) | ((wave >> 8) & 1)),
        ChipWrite(t, "ymf278b", 0, _wave_reg(slot, 2),
                  ((octave & 0xF) << 4) | ((fnum >> 7) & 7)),
        ChipWrite(t, "ymf278b", 0, _wave_reg(slot, 3), (tl << 1) & 0xFE),
        ChipWrite(t, "ymf278b", 0, _wave_reg(slot, 4), 0x8F),
        ChipWrite(off, "ymf278b", 0, _wave_reg(slot, 4), 0x0F),
    ]


def _fm_key(t: int, ch: int, *, block: int = 4, fnum: int = 580,
            off: int | None = None) -> list[ChipWrite]:
    """A keyed OPL3-shaped FM voice on the OPL4's port 0."""
    cm = OPLState(KIND_OPL3, False).chan_map[ch]
    out = [
        ChipWrite(t, "ymf278b", 0, cm + 0xA0, fnum & 0xFF),
        ChipWrite(t, "ymf278b", 0, cm + 0xB0,
                  (block << 2) | ((fnum >> 8) & 3) | 0x20),
    ]
    if off is not None:
        out.append(ChipWrite(off, "ymf278b", 0, cm + 0xB0,
                             (block << 2) | ((fnum >> 8) & 3)))
    return out


def _raw_vgm(cmds: bytes, blocks=(), clock: int = CLOCK) -> bytes:
    buf = bytearray(0x100)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x30:0x34] = (3579545).to_bytes(4, "little")
    buf[0x34:0x38] = (len(buf) - 0x34).to_bytes(4, "little")
    buf[0x60:0x64] = clock.to_bytes(4, "little")
    body = bytearray()
    for typ, start, data in blocks:
        payload = (0x200000).to_bytes(4, "little") + start.to_bytes(4, "little") + data
        body += bytes([0x67, 0x66, typ]) + len(payload).to_bytes(4, "little")
        body += payload
    body += cmds + b"\x66"
    return bytes(buf) + bytes(body)


class TestOpl4Parsing(unittest.TestCase):
    def test_port_split_clock_and_blocks(self):
        # Port 0 FM, port 2 wave; 0x84 ROM and 0x87 RAM blocks.
        cmds = (
            b"\xd0\x00\x20\x01"      # FM port 0: reg 0x20 = 1
            b"\xd0\x01\x20\x02"      # FM port 1
            b"\xd0\x02\x08\x10"      # wave slot 0 sample 16
        )
        raw = _raw_vgm(cmds, blocks=[
            (0x84, 0, bytes(range(16))),
            (0x87, 0, bytes(range(16))),
        ])
        p = TMP / "parse.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.ymf278b_clock, CLOCK)
        got = [(w.reg, w.val) for w in v.writes if w.chip == "ymf278b"]
        self.assertEqual(got, [(0x0020, 1), (0x0120, 2), (0x0208, 16)])
        self.assertEqual([s.kind for s in v.roms], ["ymf278b_rom", "ymf278b_ram"])
        self.assertIn("ymf278b", chips_in_head(raw[:0x100]))


class TestOpl4Rhythm(unittest.TestCase):
    def test_bank_zero_bd_latches_the_kit(self):
        # The OPL4's rhythm register is bank 0's $BD, exactly the OPL3's.
        # Its FM half must expose the same five kit columns after a latch.
        ws: list[ChipWrite] = []
        for k in range(8):
            t = 1000 + k * 5000
            ws += _fm_key(t, 15, block=4, fnum=580, off=t + 3000)
        ws += [
            ChipWrite(2000, "ymf278b", 0, 0xBD, 0x20),
            ChipWrite(4000, "ymf278b", 0, 0xBD, 0x3F),
            ChipWrite(28000, "ymf278b", 0, 0xBD, 0x20),
        ]
        song = analyze(_vgm(ws))
        self.assertTrue(song.opl_drums)
        used = {e.ch for e in song.events if e.on and e.ch >= 15}
        self.assertEqual(used, {15, 16, 17, 18, 19})
        self.assertGreater(song.drum_notes, 0)
        self.assertFalse(any("write no notes" in w for w in song.warnings))


class TestOpl4Wave(unittest.TestCase):
    def test_one_key_on_extracts_the_descriptor_sample(self):
        sample = bytes(range(1, 21))
        v = _vgm(_wave_note(100, wave=16, slot=0),
                 roms=[RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom")])
        samples, warn = collect_opl4_wave(v, rom=_rom(16, sample))
        self.assertFalse(warn)
        self.assertEqual(len(samples), 1)
        smp = samples[0]
        self.assertEqual(smp.wave, 16)
        self.assertEqual(smp.frames, 20)
        self.assertEqual((smp.loop_start, smp.loop_end), (0x10, 20))
        self.assertEqual(len(smp.hits), 1)

    def test_rate_matches_the_step_formula(self):
        step = opl4_step(2, 0x200)
        self.assertEqual(step, (0x200 + 1024) << 7)
        self.assertAlmostEqual(
            opl4_rate(CLOCK, step), CLOCK * step / (768.0 * 65536.0),
        )

    def test_zero_loop_address_loops_the_whole_body(self):
        # spc2vgm stores a single-cycle waveform with loop address 0. The
        # hardware wraps to the body start; treating 0 as "no loop" made the
        # sine play once and stop.
        sample = bytes(range(16))  # 8 frames of 16-bit data
        rom = bytearray(0x1000 + len(sample) + 16)
        rom[16 * 12:16 * 12 + 12] = _descriptor(2, 0x1000, 0, 8)
        rom[0x1000:0x1000 + len(sample)] = sample
        v = _vgm(_wave_note(100, wave=16),
                 roms=[RomSlice(0, 0, 0, bytes(rom), "ymf278b_rom")])
        samples, _warn = collect_opl4_wave(v, rom=bytes(rom))
        smp = samples[0]
        self.assertEqual((smp.loop_start, smp.loop_end), (0, 8))

    def test_negative_octave_shifts_right(self):
        # The wave part's 4-bit octave is signed; -6..-7 need a right shift.
        self.assertEqual(opl4_step(-4, 0x200), (0x200 + 1024) << 1)
        self.assertEqual(opl4_step(-6, 0x200), (0x200 + 1024) >> 1)
        self.assertEqual(opl4_step(-7, 0x200), (0x200 + 1024) >> 2)
        self.assertEqual(opl4_step(-8, 0x200), 0)

    def test_negative_octave_note_collects(self):
        sample = bytes(range(1, 21))
        v = _vgm(_wave_note(100, wave=16, slot=0, octave=-6),
                 roms=[RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom")])
        samples, _warn = collect_opl4_wave(v, rom=_rom(16, sample))
        self.assertEqual(samples[0].hits[0].step, opl4_step(-6, 0x200))

    def test_missing_rom_warns(self):
        v = _vgm(_wave_note(100))
        samples, warn = collect_opl4_wave(v, rom=b"")
        self.assertEqual(samples, [])
        self.assertTrue(any("YRW801" in w for w in warn), warn)

    def test_ram_samples_read_the_ram_table(self):
        # Sample id 384: descriptor at RAM offset 0, data at 0x200000+0x200.
        ram = bytearray(0x400)
        ram[0:12] = _descriptor(0, 0x200200, 0, 8)
        ram[0x200:0x208] = bytes(range(1, 9))
        v = _vgm(_wave_note(100, wave=384))
        samples, _warn = collect_opl4_wave(v, rom=b"", ram=bytes(ram))
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].wave, 384)
        self.assertEqual(samples[0].frames, 8)

    def test_full_64k_sample_is_one_shot(self):
        # A 0x10000-frame body wraps the slot's 16-bit position counter, so
        # the descriptor's loop address never takes effect (06 TIME'S UP!).
        # The reference module stores these as one-shot bodies.
        ram = bytearray(0x200 + 0x10000)
        ram[0:12] = _descriptor(0, 0x200200, 50000, 0x10000)
        v = _vgm(_wave_note(100, wave=384))
        samples, _warn = collect_opl4_wave(v, rom=b"", ram=bytes(ram))
        self.assertEqual(len(samples), 1)
        smp = samples[0]
        self.assertEqual(smp.frames, 0x10000)
        self.assertIsNone(smp.loop_start)
        self.assertIsNone(smp.loop_end)

    def test_key_off_ends_the_hit(self):
        sample = bytes(range(1, 21))
        v = _vgm(_wave_note(100, wave=16, off=900),
                 roms=[RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom")])
        samples, _warn = collect_opl4_wave(v, rom=_rom(16, sample))
        self.assertEqual(samples[0].hits[0].end_time, 900)

    def test_level_write_while_keyed_is_a_volume_point(self):
        sample = bytes(range(1, 21))
        writes = _wave_note(100, wave=16, off=9000)
        writes.insert(5, ChipWrite(500, "ymf278b", 0, _wave_reg(0, 3), 0x20))
        v = _vgm(writes, roms=[
            RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom"),
        ])
        samples, _warn = collect_opl4_wave(v, rom=_rom(16, sample))
        self.assertEqual(samples[0].hits[0].vol_pts, [(500, 16)])

    def test_attack_level_and_pan_after_key_on_are_folded(self):
        # spc2vgm keys a wave voice at TL=0x7F/pan 8, then writes the real
        # level and pan one tick later. At row resolution that is still the
        # attack; treating it as a step left the note silent.
        sample = bytes(range(1, 21))
        writes = [
            ChipWrite(100, "ymf278b", 0, _wave_reg(0, 0), 16),
            ChipWrite(100, "ymf278b", 0, _wave_reg(0, 1), 0x00),
            ChipWrite(100, "ymf278b", 0, _wave_reg(0, 2), 0x20),
            ChipWrite(100, "ymf278b", 0, _wave_reg(0, 3), 0xFE),  # TL 0x7F
            ChipWrite(100, "ymf278b", 0, _wave_reg(0, 4), 0x88),  # pan 8
            ChipWrite(140, "ymf278b", 0, _wave_reg(0, 3), 0x42),  # TL 0x21
            ChipWrite(140, "ymf278b", 0, _wave_reg(0, 4), 0x82),  # pan 2
            ChipWrite(9000, "ymf278b", 0, _wave_reg(0, 4), 0x02),
        ]
        v = _vgm(writes, roms=[
            RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom"),
        ])
        samples, _warn = collect_opl4_wave(v, rom=_rom(16, sample))
        hit = samples[0].hits[0]
        self.assertEqual((hit.level, hit.pan), (0x21, 2))
        self.assertEqual(hit.vol_pts, [])
        self.assertEqual(hit.pan_pts, [])
        song = analyze(v)
        ev = next(e for e in song.events if e.on and e.ch == 18)
        self.assertEqual(ev.vol, 0x7F - 0x21)
        self.assertEqual(ev.pan, opl4_pan_fx(2))

    def test_late_pan_write_is_a_pan_point(self):
        sample = bytes(range(1, 21))
        writes = _wave_note(100, wave=16, off=9000)
        writes.insert(5, ChipWrite(4000, "ymf278b", 0, _wave_reg(0, 4), 0x82))
        v = _vgm(writes, roms=[
            RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom"),
        ])
        samples, _warn = collect_opl4_wave(v, rom=_rom(16, sample))
        hit = samples[0].hits[0]
        self.assertEqual(hit.pan_pts, [(4000, 2)])
        song = analyze(v)
        ev = next(e for e in song.events if e.on and e.ch == 18)
        self.assertIn((4000, ev.vol, opl4_pan_fx(2)), ev.vol_pts)

    def test_held_fnum_write_is_a_pitch_step(self):
        sample = bytes(range(1, 21))
        writes = _wave_note(100, wave=16, off=9000)
        # Octave 2 -> 3 doubles the step: one octave, 768 1/64-semitone units.
        writes.insert(5, ChipWrite(
            4000, "ymf278b", 0, _wave_reg(0, 2),
            (3 << 4) | ((0x200 >> 7) & 7),
        ))
        v = _vgm(writes, roms=[
            RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom"),
        ])
        samples, _warn = collect_opl4_wave(v, rom=_rom(16, sample))
        hit = samples[0].hits[0]
        self.assertEqual(hit.pitch_pts, [(4000, opl4_step(3, 0x200))])
        song = analyze(v)
        ev = next(e for e in song.events if e.on and e.ch == 18)
        self.assertEqual(ev.pitch_steps, ((4000, 768),))

    def test_trailing_silence_step_is_not_a_volume_zero_row(self):
        # spc2vgm silences a slot with TL=0x7F just before its key-off. The
        # key-off stops the note, so the trailing zero is inaudible in the
        # source; writing it as a pattern step only adds volume-00 rows.
        sample = bytes(range(1, 21))
        writes = _wave_note(100, wave=16, off=9000)
        writes.insert(5, ChipWrite(4000, "ymf278b", 0, _wave_reg(0, 3), 0xFE))
        v = _vgm(writes, roms=[
            RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom"),
        ])
        song = analyze(v)
        ev = next(e for e in song.events if e.on and e.ch == 18)
        self.assertFalse(any(value == 0 for _t, value, _pan in ev.vol_pts))
        # A later rise must keep the silent step: it is real, not a tail.
        writes.insert(6, ChipWrite(6000, "ymf278b", 0, _wave_reg(0, 3), 0x42))
        v = _vgm(writes, roms=[
            RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom"),
        ])
        song = analyze(v)
        ev = next(e for e in song.events if e.on and e.ch == 18)
        self.assertTrue(any(value == 0 for _t, value, _pan in ev.vol_pts))

    def test_pan_inverts_furnace_conversion(self):
        # opl.cpp:2267 turns 08xy into the slot nibble as
        # ``8 ^ min(convertPanSplitToLinearLR(x, y, 15) + 1, 15)``.
        def furnace_pan(fx: int) -> int:
            left, right = (fx >> 4) & 15, fx & 15
            if left == right:
                linear = 7
            else:
                linear = int((1.0 + (right - left) / max(left, right)) * 0.5 * 15)
            return 8 ^ min(linear + 1, 15)

        self.assertEqual(opl4_pan_fx(0), 0xFF)  # centre, not hard left
        for pan in (0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15):
            self.assertEqual(furnace_pan(opl4_pan_fx(pan)), pan)
        self.assertEqual(opl4_pan_fx(8), 0)    # mute has no 08xy form


class TestOpl4Writer(unittest.TestCase):
    def _song(self, drums=False):
        writes = []
        # One FM voice on channel 0 (port 0).
        for page in (0x20, 0x23, 0x40, 0x43, 0x60, 0x63, 0x80, 0x83, 0xC0):
            writes.append(ChipWrite(100, "ymf278b", 0, page, 0x01))
        writes += [
            ChipWrite(200, "ymf278b", 0, 0xA0, 0x80),
            ChipWrite(200, "ymf278b", 0, 0xB0, 0x20),
            ChipWrite(3000, "ymf278b", 0, 0xB0, 0x00),
        ]
        if drums:
            # The OPL3/OPL4 rhythm register is bank 0's 0xBD (port 1's
            # 0x1BD is the second bank's own register).
            writes += [
                ChipWrite(4000, "ymf278b", 0, 0xBD, 0x20),
                ChipWrite(4100, "ymf278b", 0, 0xBD, 0x30),
            ]
        writes += _wave_note(1000)
        return analyze(_vgm(writes, roms=[
            RomSlice(0, 0, 0, _rom(16, bytes(range(1, 21))), "ymf278b_rom"),
        ]))

    def test_chip_and_channel_count(self):
        song = self._song()
        m = parse_fur(write_fur(song))
        self.assertEqual(m.chips, [CHIP_OPL4])
        self.assertEqual(m.n_ch, 42)
        self.assertEqual(sum(CHIP_CHANNELS[c] for c in m.chips), m.n_ch)

    def test_drums_selects_the_bigger_system(self):
        m = parse_fur(write_fur(self._song(drums=True)))
        self.assertEqual(m.chips, [CHIP_OPL4_DRUMS])
        self.assertEqual(m.n_ch, 44)

    def test_instrument_types(self):
        types = [
            payload[2] | (payload[3] << 8)
            for _pos, magic, _size, payload in iter_blocks(write_fur(self._song()))
            if magic == b"INS2"
        ]
        self.assertIn(INS_OPL, types)
        self.assertIn(INS_MULTIPCM, types)

    def test_wave_only_module_keeps_one_based_wave_instruments(self):
        # "06 TIME'S UP!" has no FM notes. The empty FM placeholder stays at
        # instrument 0 so the wave instruments are 1-based: 0x01 is wave 017,
        # 0x19 is wave 385. The sample index follows the placeholder (and the
        # old bug pointed every PCM channel at the previous sample).
        song = analyze(_vgm(_wave_note(100, wave=16), roms=[
            RomSlice(0, 0, 0, _rom(16, bytes(range(1, 21))), "ymf278b_rom"),
        ]))
        data = write_fur(song)
        m = parse_fur(data)
        self.assertEqual(m.ins_sample, [-1, 0])
        rows = m.patterns.get((18, m.orders[18][0]), [])
        notes = [(row.note, row.ins) for row in rows if row.note != -1]
        self.assertTrue(notes)
        self.assertEqual(notes[0][1], 1)


def _wave_keys(v):
    """Every rising wave-slot key bit in a register stream."""
    out = []; prev = {}
    for w in v.writes:
        if w.chip != "ymf278b" or (w.reg >> 8) != 2:
            continue
        r = w.reg & 0xFF
        if 0x08 <= r <= 0xF7 and (r - 8) // 24 == 4:
            slot = (r - 8) % 24
            on = bool(w.val & 0x80)
            was = prev.get(slot, False)
            prev[slot] = on
            if on and not was:
                out.append((w.sample, slot))
    return out


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestOpl4WaveRoundTrip(unittest.TestCase):
    def test_wave_keys_and_clock_survive_the_module(self):
        sample = bytes(range(1, 21))
        src = _vgm(_wave_note(100, wave=16, slot=0), total_samples=10000,
                   roms=[RomSlice(0, 0, 0, _rom(16, sample), "ymf278b_rom")])
        song = analyze(src)
        self.assertEqual([s.kind for s in song.fm_systems], ["opl4"])
        self.assertTrue(song.opl4_wave_samples)

        fur = TMP / "wave_roundtrip.fur"
        out = TMP / "wave_roundtrip.vgm"
        fur.write_bytes(write_fur(song, compress=True))
        export_vgm(fur, out)
        exp = load_vgm(out)

        self.assertEqual(exp.ymf278b_clock, src.ymf278b_clock)
        lim = src.total_samples
        self.assertEqual(len(_wave_keys(src)),
                         len([k for k in _wave_keys(exp) if k[0] < lim]))


if __name__ == "__main__":
    unittest.main()
