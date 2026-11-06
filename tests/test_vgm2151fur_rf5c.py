"""Ricoh RF5C68 / RF5C164: VGM parsing, register model and .fur emission.

Both chips share one register file and one Furnace system (0x95); the
chipType flag picks the variant (0 RF5C68, 1 RF5C164). The tests cover the
$B0/$B1 register writes, $C1/$C2 memory writes, the 0xC0/0xC1 RAM blocks, the
sign-magnitude sample body, the "loop at the end is a hold" rule and the
one-shot note-off/retrigger split, then a synthetic FM Towns- and Sega
CD-style log through Furnace and back.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import CHIP_CHANNELS, parse_fur
from vgm2151fur.furwrite import (
    CHIP_RF5C68, CHIP_YM2151, INS_RF5C68, write_fur,
)
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.rf5c import (
    RF5C_VOICES,
    _sign_magnitude,
    build_rf5c_ram,
    collect_rf5c,
    rf5c_rate,
)
from vgm2151fur.vgm import (
    ChipWrite, RomSlice, VgmFile, chips_in_head, load_vgm,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_rf5c"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
LATTICE_CLOCK = 3579545
RF5C68_CLOCK = 8000000
RF5C164_CLOCK = 12500000


def _vgm(writes, roms=(), **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=LATTICE_CLOCK, k007232_clock=0,
        gd3={}, roms=list(roms), writes=list(writes), **kw,
    )


def _raw_vgm(cmds: bytes, blocks=(), *, clock: int = RF5C68_CLOCK,
             clock_off: int = 0x40) -> bytes:
    """A minimal 1.61 log with a chip clock, RAM blocks and a command body."""
    buf = bytearray(0x100)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x30:0x34] = LATTICE_CLOCK.to_bytes(4, "little")
    buf[0x34:0x38] = (len(buf) - 0x34).to_bytes(4, "little")
    buf[clock_off:clock_off + 4] = clock.to_bytes(4, "little")
    body = bytearray()
    for typ, start, data in blocks:
        payload = start.to_bytes(2, "little") + data
        body += bytes([0x67, 0x66, typ]) + len(payload).to_bytes(4, "little")
        body += payload
    body += cmds + b"\x66"
    return bytes(buf) + bytes(body)


def _lattice(n=60):
    return [
        ChipWrite(k * 5000, "ym2151", 0, 0x08, 0x78) for k in range(n)
    ]


def _select(t, ch, chip="rf5c68"):
    return ChipWrite(t, chip, 0, 0x07, 0x40 | ch)


def _reg(t, reg, val, chip="rf5c68"):
    return ChipWrite(t, chip, 0, reg, val & 0xFF)


def _enable(t, bits, chip="rf5c68"):
    return ChipWrite(t, chip, 0, 0x08, bits & 0xFF)


def _note(t, *, chip="rf5c68", ch=0, start=0x1000, loop=0, step=0x1000,
          env=0xCC, pan=0x77, enable=None):
    """One driver-style burst: select, take down, start/loop/step/env, key on.

    `start` and `loop` are byte offsets in sample RAM; the start register
    holds the 256-byte page. A driver retriggers by disabling the voice, then
    rewriting the page and enabling it again.
    """
    on = (0xFF & ~(1 << ch)) if enable is None else enable
    return [
        _select(t, ch, chip),
        _enable(t, 0xFF, chip),
        _reg(t, 0x06, start >> 8, chip),
        _reg(t, 0x04, loop & 0xFF, chip),
        _reg(t, 0x05, (loop >> 8) & 0xFF, chip),
        _reg(t, 0x02, step & 0xFF, chip),
        _reg(t, 0x03, (step >> 8) & 0xFF, chip),
        _reg(t, 0x00, env, chip),
        _reg(t, 0x01, pan, chip),
        _enable(t, on, chip),
    ]


def _body(n=200, seed=1):
    """A body of sign-magnitude bytes with no 0xFF end marker inside it.

    A magnitude of 127 would form 0xFF, the end marker, so the range stops at
    125 (0xFD).
    """
    return bytes(0x80 | ((i * 3 + seed) % 126) for i in range(n))


def _synthetic(chip="rf5c68", *, start=0x1000, data=None, loop=None,
               step=0x1000, clock=None):
    data = _body() if data is None else data
    clock_field = (
        {"rf5c68_clock": clock or RF5C68_CLOCK}
        if chip == "rf5c68" else
        {"rf5c164_clock": clock or RF5C164_CLOCK}
    )
    rom = RomSlice(0, 0, start, data + b"\xFF", chip)
    writes = _lattice() + _note(
        1000, chip=chip, start=start,
        loop=loop if loop is not None else 0, step=step,
    )
    return _vgm(writes, roms=[rom], **clock_field)


class TestRf5cParsing(unittest.TestCase):
    def test_register_and_memory_writes(self):
        # Bank 1 (0x1000) then a memory byte at offset 0x0020.
        cmds = (
            b"\xb0\x07\x01"          # control: bank 1, chip enable off
            b"\xb0\x07\xc2"          # select channel 2, chip enable on
            b"\xb0\x06\x34"          # start
            b"\xc1\x20\x00\xaa"      # memory write at 0x1000 | 0x0020
            b"\xb1\x07\x42"          # RF5C164 select channel 2
            b"\xc2\x10\x00\xbb"      # RF5C164 memory write at 0x0010
        )
        raw = _raw_vgm(cmds)
        p = TMP / "parse.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.rf5c68_clock, RF5C68_CLOCK)
        regs = [(w.chip, w.reg, w.val) for w in v.writes if w.chip == "rf5c68"]
        self.assertEqual(regs, [("rf5c68", 0x07, 0x01), ("rf5c68", 0x07, 0xC2),
                                ("rf5c68", 0x06, 0x34)])
        mem = [(w.chip, w.reg, w.val) for w in v.writes
               if w.chip.endswith("_mem")]
        self.assertEqual(mem, [("rf5c68_mem", 0x1020, 0xAA),
                               ("rf5c164_mem", 0x0010, 0xBB)])
        self.assertIn("rf5c68", chips_in_head(raw[:0x100]))

    def test_clock_fields_and_defaults(self):
        raw = _raw_vgm(b"", clock=RF5C164_CLOCK, clock_off=0x6C)
        p = TMP / "clock.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual(v.rf5c164_clock, RF5C164_CLOCK)
        self.assertIn("rf5c164", chips_in_head(raw[:0x100]))

    def test_ram_block_is_a_sixteen_bit_write(self):
        raw = _raw_vgm(
            b"", blocks=[(0xC0, 0x0120, bytes(range(16)))],
        )
        p = TMP / "block.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertEqual([(s.kind, s.start, len(s.data)) for s in v.roms],
                         [("rf5c68", 0x0120, 16)])

    def test_no_unconverted_warning(self):
        raw = _raw_vgm(b"\xb0\x08\xff")
        p = TMP / "warn.vgm"
        p.write_bytes(raw)
        v = load_vgm(p)
        self.assertFalse(any("does not convert" in w for w in v.warnings))


class TestRf5cCollector(unittest.TestCase):
    def test_one_burst_is_one_hit(self):
        _ram, samples, offs, warn = collect_rf5c(
            _synthetic(), "rf5c68", RF5C68_CLOCK,
        )
        self.assertFalse(warn)
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(samples[0].hits), 1)
        hit = samples[0].hits[0]
        self.assertEqual((hit.voice, hit.start, hit.step), (0, 0x1000, 0x1000))
        self.assertEqual((hit.env, hit.pan), (0xCC, 0x77))
        self.assertEqual(offs, [])

    def test_sample_body_is_sign_magnitude(self):
        data = bytes([0x80, 0x81, 0x81, 0x01, 0x7F, 0x7E]) + _body(10)
        # 0x80 -> 0, 0x81 -> +1, 0x01 -> -1, 0x7F -> -127, 0x7E -> -126
        body = bytes(_sign_magnitude(b) & 0xFF for b in data)
        self.assertEqual(body[:6], bytes([0, 1, 1, 255, 129, 130]))
        _ram, samples, _offs, _warn = collect_rf5c(
            _synthetic(data=data), "rf5c68", RF5C68_CLOCK,
        )
        self.assertEqual(samples[0].data, body)

    def test_end_marker_ends_the_body(self):
        _ram, samples, _offs, _warn = collect_rf5c(
            _synthetic(data=_body(64)), "rf5c68", RF5C68_CLOCK,
        )
        self.assertEqual(samples[0].length, 64)

    def test_a_mid_body_loop_is_kept(self):
        _ram, samples, _offs, _warn = collect_rf5c(
            _synthetic(data=_body(100), loop=0x1000 + 20),
            "rf5c68", RF5C68_CLOCK,
        )
        smp = samples[0]
        self.assertEqual(smp.loop_start, 20)
        self.assertEqual(smp.loop_end, 100)

    def test_a_loop_at_the_end_is_a_hold(self):
        # Drivers that want a one-shot point the loop at the last byte before
        # the end marker; that is not a musical loop.
        _ram, samples, _offs, _warn = collect_rf5c(
            _synthetic(data=_body(100), loop=0x1000 + 99),
            "rf5c68", RF5C68_CLOCK,
        )
        self.assertIsNone(samples[0].loop_start)

    def test_retrigger_does_not_emit_an_off(self):
        writes = _lattice() + _note(1000) + _note(1100)
        # Second note disables then re-enables channel 0 in the same burst.
        _ram, samples, offs, _warn = collect_rf5c(
            _vgm(writes, roms=[RomSlice(0, 0, 0x1000, _body() + b"\xFF",
                                        "rf5c68")]),
            "rf5c68", RF5C68_CLOCK,
        )
        self.assertEqual(len(samples[0].hits), 2)
        self.assertEqual(offs, [])

    def test_a_real_off_is_emitted(self):
        writes = _lattice() + _note(1000)
        writes.append(_enable(60000, 0xFF))  # disable everything
        _ram, samples, offs, _warn = collect_rf5c(
            _vgm(writes, roms=[RomSlice(0, 0, 0x1000, _body() + b"\xFF",
                                        "rf5c68")]),
            "rf5c68", RF5C68_CLOCK,
        )
        self.assertEqual([(o.sample_time, o.voice) for o in offs], [(60000, 0)])

    def test_rate_is_clock_times_step_over_786432(self):
        self.assertAlmostEqual(rf5c_rate(8000000, 0x1000), 8000000 * 4096 / 786432)
        self.assertAlmostEqual(rf5c_rate(12500000, 0x1000), 12500000 * 4096 / 786432)

    def test_banked_memory_write_lands_in_the_window(self):
        rom = RomSlice(0, 0, 0x200, b"\x80" * 8 + b"\xFF", "rf5c68")
        writes = _lattice() + [
            ChipWrite(0, "rf5c68", 0, 0x07, 0x01),      # bank 1
            ChipWrite(0, "rf5c68_mem", 0, 0x1000 | 0x200, 0x90),
        ]
        ram = build_rf5c_ram(_vgm(writes, roms=[rom]), "rf5c68")
        self.assertEqual(ram[0x200], 0x80)
        self.assertEqual(ram[0x1200], 0x90)


class TestRf5cWriter(unittest.TestCase):
    def test_rf5c68_is_one_system_with_eight_channels(self):
        song = analyze(_synthetic())
        data = write_fur(song)
        m = parse_fur(data)
        # The YM2151 lattice writes a heartbeat for the grid; the RF5C68 is
        # the second system.
        self.assertEqual(m.chips, [CHIP_YM2151, CHIP_RF5C68])
        self.assertEqual(m.n_ch, 8 + RF5C_VOICES)
        self.assertEqual(sum(CHIP_CHANNELS[c] for c in m.chips), m.n_ch)
        flags = _flag_texts(data)
        self.assertTrue(any("chipType=0" in f for f in flags), flags)
        self.assertTrue(any("clockSel=0" in f for f in flags), flags)

    def test_rf5c164_sets_the_chip_type_flag(self):
        song = analyze(_synthetic("rf5c164"))
        data = write_fur(song)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_YM2151, CHIP_RF5C68])
        flags = _flag_texts(data)
        self.assertTrue(any("chipType=1" in f for f in flags), flags)
        self.assertTrue(any("clockSel=2" in f for f in flags), flags)

    def test_instrument_type_is_rf5c68(self):
        song = analyze(_synthetic())
        data = write_fur(song)
        types = [
            payload[2] | (payload[3] << 8)
            for _pos, magic, _size, payload in _iter_ins(data)
        ]
        self.assertIn(INS_RF5C68, types)


def _flag_texts(data: bytes) -> list[str]:
    from vgm2151fur.furio import iter_blocks
    return [
        payload.split(b"\x00")[0].decode("utf-8", "replace")
        for _pos, magic, _size, payload in iter_blocks(data)
        if magic == b"FLAG"
    ]


def _iter_ins(data: bytes):
    from vgm2151fur.furio import iter_blocks
    return [b for b in iter_blocks(data) if b[1] == b"INS2"]


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestRf5cRoundTrip(unittest.TestCase):
    """A synthetic log goes through Furnace and comes back with its key-ons.

    The collector and writer tests prove the register model and the emitted
    instrument; these prove that a real module round trip keeps the chip
    variant, its clock and every voice gate.
    """

    def _round_trip(self, vgm):
        song = analyze(vgm)
        fur = TMP / "rf5c_round.fur"
        fur.write_bytes(write_fur(song, compress=True))
        out = TMP / "rf5c_round.export.vgm"
        export_vgm(fur, out)
        return vgm, song, load_vgm(out), fur

    def _keyons(self, v) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        prev = 0xFF
        for w in v.writes:
            if w.chip in ("rf5c68", "rf5c164") and (w.reg & 0xFF) == 0x08:
                for i in range(8):
                    if (prev >> i) & 1 and not (w.val >> i) & 1:
                        out.append((w.sample, i))
                prev = w.val
        return out

    def _assert_matched(self, a, b):
        used: set[int] = set()
        missed = 0
        for t, ch in a:
            best = None
            for j, (t2, ch2) in enumerate(b):
                if j in used or ch2 != ch:
                    continue
                d = abs(t2 - t)
                if d <= 2000 and (best is None or d < best[0]):
                    best = (d, j)
            if best is None:
                missed += 1
            else:
                used.add(best[1])
        self.assertEqual(missed, 0)

    def test_rf5c68_round_trips_its_keyons(self):
        src, song, exp, fur = self._round_trip(_synthetic())
        self.assertTrue(song.rf5c68_samples)
        self.assertTrue(all(s.data for s in song.rf5c68_samples))
        m = parse_fur(fur.read_bytes())
        self.assertEqual(m.chips, [CHIP_YM2151, CHIP_RF5C68])
        self.assertEqual(exp.rf5c68_clock, RF5C68_CLOCK)
        src_keys = self._keyons(src)
        exp_keys = [k for k in self._keyons(exp) if k[0] < src.total_samples]
        self.assertEqual(len(src_keys), len(exp_keys))
        self._assert_matched(src_keys, exp_keys)

    def test_rf5c164_round_trips_its_keyons(self):
        src, song, exp, fur = self._round_trip(
            _synthetic("rf5c164", start=0x2000, data=_body(seed=5)),
        )
        self.assertTrue(song.rf5c164_samples)
        self.assertTrue(all(s.data for s in song.rf5c164_samples))
        m = parse_fur(fur.read_bytes())
        self.assertEqual(m.chips, [CHIP_YM2151, CHIP_RF5C68])
        self.assertEqual(exp.rf5c164_clock, RF5C164_CLOCK)
        src_keys = self._keyons(src)
        exp_keys = [k for k in self._keyons(exp) if k[0] < src.total_samples]
        self.assertEqual(len(src_keys), len(exp_keys))
        self._assert_matched(src_keys, exp_keys)


if __name__ == "__main__":
    unittest.main()
