"""Nintendo Virtual Boy VSU: parsing, envelopes, waves and .fur emission."""

from __future__ import annotations

import collections
import struct
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import _vsu_sw_effect, analyze
from vgm2151fur.edit import read_chips
from vgm2151fur.furio import iter_blocks, master_volume, parse_fur
from vgm2151fur.furwrite import (
    CHIP_VBOY, INS_VBOY, write_fur,
)
from vgm2151fur.vgm import (
    ChipWrite, VgmFile, chips_in_head, load_vgm,
)
from vgm2151fur.vsu import (
    VSU_DEFAULT_CLOCK,
    collect_vsu,
    vsu_note,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_vsu"
TMP.mkdir(exist_ok=True)
CLOCK = VSU_DEFAULT_CLOCK
JACK = Path(r"D:\Downloads\Jack_Bros._(Nintendo_Virtual_Boy)")
GAL = Path(r"D:\Downloads\GalacticPinballVGM")
A64 = Path(r"D:\Downloads\a64_redshift.vgm")


def _vgm(writes, **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x171,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=[], writes=list(writes), vsu_clock=CLOCK, **kw,
    )


def _w(t, reg, val, chip=0):
    return ChipWrite(t, "vsu", chip, reg & 0x7FFF, val & 0xFF)


def _wave(table=0, vals=None):
    vals = vals or [(i * 2 + 1) & 0x3F for i in range(32)]
    return [_w(0, table * 0x20 + i, v) for i, v in enumerate(vals)]


def _note(
    t, ch=0, *, fnum=0x100, vol=15, pan=0x88, off=None, intl=0x80,
    ev1=0, ram=0, chip=0,
):
    base = 0x100 + ch * 0x10
    out = [
        _w(t, base + 6, ram, chip),
        _w(t, base + 1, pan, chip),
        _w(t, base + 2, fnum & 0xFF, chip),
        _w(t, base + 3, (fnum >> 8) & 0x07, chip),
        _w(t, base + 4, (vol & 0x0F) << 4, chip),
        _w(t, base + 5, ev1, chip),
        _w(t, base + 0, intl, chip),
    ]
    if off is not None:
        out.append(_w(off, base + 0, 0, chip))
    return out


def _raw_vgm(cmds: bytes, clock: int = CLOCK, dual: bool = False) -> bytes:
    buf = bytearray(0x100)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x171).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0x100 - 0x34).to_bytes(4, "little")
    raw = clock | (0x40000000 if dual else 0)
    buf[0xC4:0xC8] = raw.to_bytes(4, "little")
    return bytes(buf) + cmds + b"\x66"


def _c7(reg: int, val: int) -> bytes:
    return bytes((0xC7, (reg >> 8) & 0xFF, reg & 0xFF, val & 0xFF))


class TestVsuParsing(unittest.TestCase):
    def test_header_command_and_chip_kind(self):
        raw = _raw_vgm(_c7(0x100, 0x80) + _c7(0x000, 0x3F))
        p = TMP / "parse.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.vsu_clock, CLOCK)
        self.assertFalse(v.vsu_dual)
        writes = [w for w in v.writes if w.chip == "vsu"]
        self.assertEqual([(w.reg, w.val) for w in writes],
                         [(0x100, 0x80), (0x000, 0x3F)])
        self.assertIn("vsu", chips_in_head(raw))

    def test_second_chip_bit(self):
        raw = _raw_vgm(_c7(0x8100, 0x80), dual=True)
        p = TMP / "dual.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertTrue(v.vsu_dual)
        self.assertIn("vsu_dual", chips_in_head(raw))
        w = [w for w in v.writes if w.chip == "vsu"][0]
        self.assertEqual(w.chip_id, 1)
        self.assertEqual(w.reg, 0x100)


class TestVsuCollector(unittest.TestCase):
    def test_key_note_pan_and_wave(self):
        writes = _wave(0) + _note(1000, 0, fnum=0x123, vol=12, pan=0x13,
                                  off=5000)
        hits, offs, waves, _mods, _dyn, warn = collect_vsu(_vgm(writes))
        self.assertFalse(warn)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h.voice, 0)
        self.assertEqual(h.period, 2048 - 0x123)
        self.assertEqual(h.vol, 12)
        self.assertEqual(h.pan, 0x13)
        self.assertEqual(h.off_sample, 5000)
        self.assertEqual(h.wave, 1)  # zero image is 0, the written wave is 1
        self.assertEqual(waves[h.wave],
                         bytes((i * 2 + 1) & 0x3F for i in range(32)))
        self.assertEqual(vsu_note(CLOCK, h.period)[0] > 0, True)

    def test_volume_rise_starts_new_note(self):
        writes = _wave(0) + _note(1000, 0, vol=15, off=None)
        # A held driver: drop to 3, then re-attack at 15.
        writes += [_w(3000, 0x104, 0x30), _w(5000, 0x104, 0xF0)]
        writes += [_w(9000, 0x100, 0x00)]
        hits, offs, _waves, _mods, _dyn, _warn = collect_vsu(_vgm(writes))
        self.assertEqual([h.sample_time for h in hits], [1000, 5000])
        self.assertEqual(hits[0].off_sample, 5000)
        self.assertEqual(hits[1].off_sample, 9000)
        self.assertEqual(hits[1].vol, 15)
        self.assertEqual(len(offs), 1)

    def test_hardware_envelope_steps(self):
        writes = _wave(0) + _note(
            1000, 0, vol=2, ev1=0x01, off=4000,
        )
        # EV0=0x21: volume 2, speed 1, down.
        writes = [
            _w(w.sample, w.reg, 0x21) if w.reg == 0x104 else w
            for w in writes
        ]
        hits, _offs, _waves, _mods, _dyn, _warn = collect_vsu(_vgm(writes))
        h = hits[0]
        self.assertEqual(h.vol, 2)
        # 76,800 clocks/tick at 5 MHz = 677.376 samples; speed 1 = 2 ticks.
        self.assertEqual([v for _t, v in h.vol_pts], [1, 0])
        self.assertAlmostEqual(h.vol_pts[0][0], 2355, delta=3)
        self.assertAlmostEqual(h.vol_pts[1][0], 3709, delta=3)

    def test_interval_auto_stop(self):
        # 0xAC = key-on + interval enable, length 12 -> 13 interval ticks.
        writes = _wave(0) + _note(100, 0, intl=0xAC)
        hits, offs, _waves, _mods, _dyn, _warn = collect_vsu(_vgm(writes))
        self.assertEqual(len(hits), 1)
        # 19,200 clocks / 5 MHz * 44100 = 169.344 samples per tick.
        self.assertAlmostEqual(hits[0].off_sample, 2301, delta=3)
        self.assertEqual(offs[0].sample_time, hits[0].off_sample)

    def test_noise_tap_and_mid_change(self):
        writes = _wave(0) + _note(
            1000, 5, fnum=0x700, vol=10, pan=0x08, ev1=0x10,
        )
        writes += [_w(3000, 0x155, 0x70), _w(5000, 0x150, 0x00)]
        hits, _offs, _waves, _mods, _dyn, _warn = collect_vsu(_vgm(writes))
        h = hits[0]
        self.assertEqual(h.voice, 5)
        self.assertEqual(h.tap, 1)
        self.assertEqual(h.tap_pts, [(3000, 7)])

    def test_sweep_and_mod_effect_mapping(self):
        self.assertEqual(_vsu_sw_effect(0x45, 0x41), (0x13, 0x45))
        self.assertEqual(_vsu_sw_effect(0x74, 0x70), (0x14, 0x37))
        self.assertEqual(_vsu_sw_effect(0x10, 0x40), (0x13, 0x10))
        self.assertEqual(_vsu_sw_effect(0x45, 0x00), (0x13, 0x00))

    def test_dynamic_wave_change(self):
        writes = _wave(0) + _note(1000, 0, vol=15)
        # Rewrite table 0 while the voice is playing.
        writes += [
            _w(2000, i, 0x3F) for i in range(32)
        ]
        writes += [_w(4000, 0x100, 0x00)]
        hits, _offs, waves, _mods, dyn, _warn = collect_vsu(_vgm(writes))
        self.assertTrue(dyn)
        self.assertGreaterEqual(len(waves), 2)
        self.assertTrue(hits[0].wave_pts)


class TestVsuModule(unittest.TestCase):
    def _module(self, writes):
        song = analyze(_vgm(writes))
        data = write_fur(song, include_pcm=True)
        return song, data, parse_fur(data)

    def test_vsu_only_layout_and_effects(self):
        writes = _wave(0) + _note(1000, 0, vol=15, pan=0x13, off=5000)
        writes += _note(6000, 5, fnum=0x700, vol=10, pan=0x08, ev1=0x10,
                        off=9000)
        song, data, mod = self._module(writes)
        self.assertEqual(mod.chips, [CHIP_VBOY])
        self.assertEqual(mod.n_ch, 6)
        types = [
            int.from_bytes(blk[2:4], "little")
            for _off, mag, _size, blk in iter_blocks(data) if mag == b"INS2"
        ]
        self.assertTrue(types)
        self.assertTrue(all(t == INS_VBOY for t in types))
        wave_blocks = [blk for _o, mag, _s, blk in iter_blocks(data)
                       if mag == b"WAVE"]
        # zero image + the written wave
        self.assertEqual(len(wave_blocks), 2)
        fx = collections.Counter()
        for ch in range(mod.n_ch):
            for oi in mod.orders[ch]:
                for row in mod.patterns.get((ch, oi), []):
                    for cmd, _val in (row.fx or []):
                        fx[cmd] += 1
        self.assertGreater(fx[0x11], 0)  # noise tap
        self.assertGreater(fx[0x08], 0)  # pan

    def test_real_temple_static(self):
        p = JACK / "16 Temple of Nightmare [Level 3].vgz"
        if not p.is_file():
            self.skipTest(f"{p} not present")
        song = analyze(load_vgm(p))
        self.assertEqual(song.vsu_ids, [0])
        self.assertEqual(song.vsu_count, 2401)
        self.assertFalse(song.vsu_dynamic)
        self.assertEqual(len(song.vsu_waves), 6)
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_VBOY])
        self.assertEqual(mod.n_ch, 6)
        self.assertLessEqual(len(song.vsu_ins), 250)
        self.assertAlmostEqual(master_volume(data), 2.2, places=2)
        self.assertEqual([round(c.volume, 3) for c in read_chips(data)], [1.0])

    def test_real_opening_noise(self):
        p = JACK / "08 Opening 2.vgz"
        if not p.is_file():
            self.skipTest(f"{p} not present")
        song = analyze(load_vgm(p))
        self.assertTrue(any(h.on and h.tone_chip == 7 and h.ch % 6 == 5
                            for h in song.events))
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        tap = 0
        for ch in range(mod.n_ch):
            for oi in mod.orders[ch]:
                for row in mod.patterns.get((ch, oi), []):
                    tap += sum(1 for cmd, _v in (row.fx or []) if cmd == 0x11)
        self.assertGreater(tap, 0)

    def test_real_stage_sweep_and_envelope(self):
        p = GAL / "04 Stage Select.vgz"
        if not p.is_file():
            self.skipTest(f"{p} not present")
        song = analyze(load_vgm(p))
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        fx = collections.Counter()
        for ch in range(mod.n_ch):
            for oi in mod.orders[ch]:
                for row in mod.patterns.get((ch, oi), []):
                    for cmd, _val in (row.fx or []):
                        fx[cmd] += 1
        self.assertGreater(fx[0x13], 0)  # channel-4 sweep

    def test_real_important_modulation_table(self):
        p = GAL / "01 Important.vgz"
        if not p.is_file():
            self.skipTest(f"{p} not present")
        song = analyze(load_vgm(p))
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        fx = collections.Counter()
        for ch in range(mod.n_ch):
            for oi in mod.orders[ch]:
                for row in mod.patterns.get((ch, oi), []):
                    for cmd, _val in (row.fx or []):
                        fx[cmd] += 1
        self.assertGreater(fx[0x14], 0)  # modulation
        # The FD feature carries the source's 32-byte modulation table.
        tables = []
        for _o, mag, _s, blk in iter_blocks(data):
            if mag != b"INS2":
                continue
            p = 4
            while p + 4 <= len(blk):
                code = blk[p:p + 2]
                p += 2
                if code == b"EN":
                    break
                size = struct.unpack_from("<H", blk, p)[0]
                p += 2
                if code == b"FD" and size >= 41:
                    tables.append(blk[p + 9:p + 41])
                p += size
        self.assertTrue(any(t[:3] == bytes((0, 0xA7, 0x1D)) for t in tables))

    def test_real_a64_dynamic_waves(self):
        if not A64.is_file():
            self.skipTest(f"{A64} not present")
        song = analyze(load_vgm(A64))
        self.assertTrue(song.vsu_dynamic)
        self.assertLessEqual(len(song.vsu_ins), 250)
        self.assertLessEqual(len(song.vsu_waves), 256)
        data = write_fur(song, include_pcm=True)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_VBOY])
        self.assertEqual(mod.n_ch, 6)
        wave_fx = 0
        for ch in range(mod.n_ch):
            for oi in mod.orders[ch]:
                for row in mod.patterns.get((ch, oi), []):
                    wave_fx += sum(1 for cmd, _v in (row.fx or [])
                                   if cmd == 0x10)
        self.assertGreater(wave_fx, 0)
        self.assertIn("screwThis=true", data.decode("latin1", "ignore"))


if __name__ == "__main__":
    unittest.main()
