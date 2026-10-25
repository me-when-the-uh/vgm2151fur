"""YM2203 (OPN) support: parsing, pitch mapping, analysis and .fur emission."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import (
    _SSG_VOLUME_TRIM, analyze, furnace_note_name,
)
from vgm2151fur.ay8910 import ay_rate, collect_ay8910
from vgm2151fur.diag import opn_pitch_stream, pitch_runs
from vgm2151fur.furio import CHIP_CHANNELS, iter_blocks, parse_fur
from vgm2151fur.furwrite import (
    CHIP_YM2203, CHIP_YM2203_EXT, INS_AY, INS_FM, write_fur,
)
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.vgm import ChipWrite, VgmFile, chips_in_head, load_vgm
from vgm2151fur.ym2203 import (
    OPN_FREQ_BASE, opn_note_for, opn_note_fnum, opn_pitch_units,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_ym2203"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
CLOCK = 3072000


def _vgm(writes, **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=400000, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        # a real VGM is in stream order; sorting also keeps same-sample
        # register write order stable
        roms=[], writes=sorted(writes, key=lambda w: w.sample), **kw,
    )


def _raw_vgm(body: bytes, clock: int = CLOCK, dual: bool = False) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    raw = (clock & 0x3FFFFFFF) | (0x40000000 if dual else 0)
    buf[0x44:0x48] = raw.to_bytes(4, "little")
    out = bytes(buf) + body + b"\x66"
    return out[:4] + (len(out) - 4).to_bytes(4, "little") + out[8:]


def _patch(
    t: int = 100, cid: int = 0,
    sr: tuple[int, int, int, int] = (0, 0, 0, 0),
    ssg: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> list[ChipWrite]:
    per_op = {0x70: sr, 0x90: ssg}
    out: list[ChipWrite] = []
    for page in (0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
        for op in range(4):
            if page == 0x30:
                val = 0x01
            elif page == 0x50:
                val = 0x1F
            elif page in per_op:
                val = per_op[page][op]
            else:
                val = 0x00
            out.append(ChipWrite(t, "ym2203", cid, page + op * 4, val))
    out.append(ChipWrite(t, "ym2203", cid, 0xB0, 0x07))  # alg 7: all carriers
    return out


def _fm_note(
    block: int, fnum: int, *, t: int = 100, off: int = 60000,
    cid: int = 0, ch: int = 0, patch_t: int | None = None,
    sr: tuple[int, int, int, int] = (0, 0, 0, 0),
    ssg: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> list[ChipWrite]:
    out = _patch(t if patch_t is None else patch_t, cid, sr=sr, ssg=ssg)
    out += [
        ChipWrite(t, "ym2203", cid, 0xA4 + ch, (block << 3) | ((fnum >> 8) & 7)),
        ChipWrite(t, "ym2203", cid, 0xA0 + ch, fnum & 0xFF),
        ChipWrite(t, "ym2203", cid, 0x28, 0xF0 | ch),
    ]
    if off:
        out.append(ChipWrite(off, "ym2203", cid, 0x28, ch))
    return out


def _ssg_note(*, t: int = 100, off: int = 60000, cid: int = 0) -> list[ChipWrite]:
    return [
        ChipWrite(t, "ym2203_ssg", cid, 0, 0x1C),
        ChipWrite(t, "ym2203_ssg", cid, 1, 0x01),   # period 284
        ChipWrite(t, "ym2203_ssg", cid, 7, 0xFE),   # tone A only
        ChipWrite(t, "ym2203_ssg", cid, 8, 0x0F),   # volume A max
        ChipWrite(off, "ym2203_ssg", cid, 8, 0x00),
    ]


def _ins_types(data: bytes) -> list[int]:
    out = []
    for _off, mag, _size, payload in iter_blocks(data):
        if mag == b"INS2" and len(payload) >= 4:
            out.append(int.from_bytes(payload[2:4], "little"))
    return out


def _fm_feature(data: bytes) -> bytes:
    for _off, mag, _size, payload in iter_blocks(data):
        if mag != b"INS2":
            continue
        pos = payload.find(b"FM")
        if pos >= 0 and pos + 4 <= len(payload):
            size = int.from_bytes(payload[pos + 2:pos + 4], "little")
            return payload[pos + 4:pos + 4 + size]
    return b""


class TestYm2203Parsing(unittest.TestCase):
    def test_header_and_register_split(self):
        body = bytes([
            0x55, 0x30, 0x01,    # FM ch0 op1
            0x55, 0x08, 0x0F,    # SSG channel A volume
            0xA5, 0x40, 0x02,    # second chip FM
            0xA5, 0x09, 0x0F,    # second chip SSG
        ])
        p = TMP / "ym2203_dual.vgm"
        p.write_bytes(_raw_vgm(body, dual=True))
        vgm = load_vgm(p)
        self.assertEqual(vgm.ym2203_clock, CLOCK)
        self.assertTrue(vgm.ym2203_dual)
        got = [(w.chip, w.chip_id, w.reg, w.val) for w in vgm.writes]
        self.assertEqual(got, [
            ("ym2203", 0, 0x30, 0x01),
            ("ym2203_ssg", 0, 0x08, 0x0F),
            ("ym2203", 1, 0x40, 0x02),
            ("ym2203_ssg", 1, 0x09, 0x0F),
        ])

    def test_raw_clock_is_masked(self):
        p = TMP / "ym2203_clock.vgm"
        p.write_bytes(_raw_vgm(b"\x55\x30\x01", dual=True))
        self.assertEqual(load_vgm(p).ym2203_clock, CLOCK)

    def test_chips_in_head(self):
        self.assertIn("ym2203", chips_in_head(_raw_vgm(b"\x66")))
        kinds = chips_in_head(_raw_vgm(b"\x66", dual=True))
        self.assertIn("ym2203", kinds)
        self.assertIn("ym2203_dual", kinds)

    def test_stray_a5_is_first_chip_with_warning(self):
        p = TMP / "ym2203_stray.vgm"
        p.write_bytes(_raw_vgm(bytes([0xA5, 0x40, 0x02])))
        vgm = load_vgm(p)
        self.assertFalse(vgm.ym2203_dual)
        self.assertEqual([(w.chip, w.chip_id) for w in vgm.writes], [("ym2203", 0)])
        self.assertTrue(any("without the dual-instance" in w for w in vgm.warnings))

    def test_missing_clock_warning(self):
        p = TMP / "ym2203_noclock.vgm"
        p.write_bytes(_raw_vgm(bytes([0x55, 0x30, 0x01]), clock=0))
        vgm = load_vgm(p)
        self.assertEqual(vgm.ym2203_clock, 0)
        self.assertTrue(any("YM2203 clock" in w for w in vgm.warnings))


class TestYm2203Pitch(unittest.TestCase):
    def test_note_fnum_is_the_forward_mapping(self):
        # Furnace's linear-mode note conversion (calcBaseFreqFNumBlock).
        for note in (60, 72, 84, 96, 108, 115):
            block, fnum = opn_note_fnum(note, CLOCK)
            nbase = note << 7
            fbase = 440.0 * 2.0 ** ((nbase - 7296) / 1536.0)
            bf = round(fbase * (OPN_FREQ_BASE / CLOCK))
            want_block = max(0, min(7, (note - 60) // 12))
            self.assertEqual((block, fnum), (want_block, bf >> want_block))

    def test_e5_is_one_128th_semitone(self):
        # fnum 1232 is note 115 (f-num 1204) plus 0.398 semitone; that is
        # 51 units of E5 in this module's default linear pitch mode.
        self.assertEqual(opn_note_for(4, 1232, CLOCK), (115, 0x80 + 51))
        self.assertEqual(opn_note_for(4, 1204, CLOCK), (115, 0x80))

    def test_round_trip_through_e5(self):
        for block, fnum in ((4, 1100), (4, 1232), (3, 923), (3, 1466), (2, 2016)):
            note, e5 = opn_note_for(block, fnum, CLOCK)
            b, base = opn_note_fnum(note, CLOCK)
            got = base * 2.0 ** ((e5 - 0x80) / 1536.0)
            # the chosen note may be in another octave; compare pitch, not the
            # raw f-num
            self.assertLess(
                abs(opn_pitch_units(b, got) - opn_pitch_units(block, fnum)),
                0.2,
            )

    def test_pitch_units_ignore_block_equivalents(self):
        self.assertAlmostEqual(
            opn_pitch_units(3, 1000), opn_pitch_units(4, 500), places=9
        )


class TestYm2203Analyze(unittest.TestCase):
    def test_fm_note_and_patch(self):
        song = analyze(_vgm(_fm_note(4, 1232), ym2203_clock=CLOCK))
        self.assertEqual(song.fm_chip, "ym2203")
        self.assertEqual(song.ym2203_ids, [0])
        self.assertEqual(len(song.fm_patches), 1)
        p = song.fm_patches[0]
        self.assertTrue(p.opn)
        self.assertEqual(p.alg, 7)
        self.assertEqual(p.ops[0].dt_mul, 0x01)
        ev = next(e for e in song.events if e.on)
        self.assertEqual((ev.ch, ev.note, ev.e5), (0, 115, 0x80 + 51))
        self.assertEqual(ev.ins, 0)
        self.assertEqual(ev.vol, -1)

    def test_key_off_event_and_end(self):
        song = analyze(_vgm(_fm_note(4, 1232, off=60000), ym2203_clock=CLOCK))
        on = next(e for e in song.events if e.on)
        self.assertEqual(on.end, 60000)
        off = [e for e in song.events if not e.on and e.ch == 0]
        self.assertEqual([e.sample for e in off], [60000])

    def test_retrigger_off_is_dropped(self):
        ws = _fm_note(4, 1232, off=0)
        ws += [
            ChipWrite(5000, "ym2203", 0, 0x28, 0x00),
            ChipWrite(5001, "ym2203", 0, 0x28, 0xF0),
        ]
        song = analyze(_vgm(ws, ym2203_clock=CLOCK))
        offs = [e for e in song.events if not e.on and e.ch == 0]
        self.assertEqual(offs, [])

    def test_mid_note_fnum_becomes_pitch_macro(self):
        ws = _fm_note(4, 1204, off=60000)
        ws += [
            ChipWrite(30000, "ym2203", 0, 0xA4, (4 << 3) | ((1250 >> 8) & 7)),
            ChipWrite(30000, "ym2203", 0, 0xA0, 1250 & 0xFF),
        ]
        song = analyze(_vgm(ws, ym2203_clock=CLOCK))
        ev = next(e for e in song.events if e.on)
        self.assertGreaterEqual(len(ev.fm_pitch_pts), 2)
        self.assertNotEqual(ev.ins, 0)
        macro = song.fm_patches[ev.ins].pitch_macro
        self.assertTrue(macro)
        self.assertEqual(macro[0], 0)
        self.assertGreater(max(macro), 0)

    def test_long_pitch_sweep_becomes_pitch_macro(self):
        ws = _fm_note(4, 1204, off=60000)
        for i, fnum in enumerate((1210, 1260) * 6):
            t = 30000 + i * 1000
            ws += [
                ChipWrite(t, "ym2203", 0, 0xA4, (4 << 3) | ((fnum >> 8) & 7)),
                ChipWrite(t, "ym2203", 0, 0xA0, fnum & 0xFF),
            ]
        song = analyze(_vgm(ws, ym2203_clock=CLOCK))
        ev = next(e for e in song.events if e.on)
        self.assertGreater(len(ev.fm_pitch_pts), 4)
        self.assertNotEqual(ev.ins, 0)
        macro = song.fm_patches[ev.ins].pitch_macro
        self.assertTrue(macro)
        self.assertEqual(macro[0], 0)
        self.assertGreater(max(macro), 0)

    def test_ssg_note_lands_on_voice3(self):
        song = analyze(_vgm(_ssg_note(), ym2203_clock=CLOCK))
        self.assertEqual(song.ym2203_ssg_count, 1)
        self.assertEqual(song.ay_count, 1)
        ev = next(e for e in song.events if e.on)
        self.assertEqual(ev.ch, 3)
        self.assertEqual(ev.tone_chip, 2)
        self.assertEqual(ev.tone_clock, CLOCK)
        self.assertEqual(ev.tone_div, 16)
        # 3.072 MHz / (16 * 284) = 676.06 Hz.
        self.assertAlmostEqual(ay_rate(CLOCK, 284), CLOCK / (16 * 284))
        self.assertEqual(song.ay_ins[0].vol, ())

    def test_dual_chip_channels(self):
        ws = _fm_note(4, 1232, cid=0, ch=0)
        ws += _fm_note(4, 923, cid=1, ch=0, t=200, off=60000)
        ws += _ssg_note(cid=1, t=300, off=60000)
        song = analyze(_vgm(ws, ym2203_clock=CLOCK))
        self.assertEqual(song.ym2203_ids, [0, 1])
        on = [e for e in song.events if e.on]
        self.assertEqual(sorted(e.ch for e in on), [0, 6, 9])
        self.assertEqual(song.ym2203_ssg_count, 1)

    def test_ssg_uses_the_same_ay_effects(self):
        ws = _ssg_note()
        # envelope mode + shape write become 20xx/22xy on the PSG channel
        ws.append(ChipWrite(20000, "ym2203_ssg", 0, 8, 0x1F))
        ws.append(ChipWrite(20000, "ym2203_ssg", 0, 13, 0x0D))
        song = analyze(_vgm(ws, ym2203_clock=CLOCK))
        self.assertTrue(any(
            cmd == 0x22
            for e in song.events if e.on
            for _t, cmd, _v in e.tone_fx_pts
        ))


class TestYm2203Writer(unittest.TestCase):
    def _song(self, ws):
        return analyze(_vgm(ws, ym2203_clock=CLOCK))

    def test_single_chip_layout_and_ids(self):
        data = write_fur(self._song(_fm_note(4, 1232)), include_pcm=False)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_YM2203])
        self.assertEqual(m.n_ch, 6)
        self.assertEqual(CHIP_CHANNELS[CHIP_YM2203], 6)
        self.assertIn(b"FM 1-1", data)
        self.assertIn(b"PSG 1-1", data)
        self.assertIn(b"customClock=3072000", data)
        self.assertEqual(_ins_types(data)[0], INS_FM)

    def test_opn_operator_fields(self):
        sr = (0x12, 0x13, 0x14, 0x15)
        ssg = (0x05, 0x06, 0x07, 0x08)
        ws = _fm_note(4, 1232, sr=sr, ssg=ssg)
        data = write_fur(self._song(ws), include_pcm=False)
        feat = _fm_feature(data)
        self.assertTrue(feat)
        self.assertEqual(feat[3] & 0x3F, 0x20)  # 4-op bit
        for op in range(4):
            base = 4 + op * 8
            self.assertEqual(feat[base + 4] & 0x1F, 0x12 + op)
            self.assertEqual(feat[base + 6] & 0x0F, 0x05 + op)

    def test_ssg_note_gets_an_ay_instrument(self):
        data = write_fur(self._song(_ssg_note()), include_pcm=False)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_YM2203])
        row = next(r for r in m.patterns[(3, 0)] if r.note >= 0)
        self.assertEqual(row.ins, 0)
        self.assertEqual(_ins_types(data), [INS_AY])

    def test_dual_chip_layout(self):
        ws = _fm_note(4, 1232, cid=0, ch=0)
        ws += _fm_note(4, 923, cid=1, ch=0, t=200)
        data = write_fur(self._song(ws), include_pcm=False)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_YM2203, CHIP_YM2203])
        self.assertEqual(m.n_ch, 12)
        self.assertIn(b"FM 2-1", data)
        self.assertIn(b"PSG 2-1", data)
        # both notes share one OPN FM instrument
        self.assertEqual(_ins_types(data), [INS_FM])

    def test_pitch_macro_rides_on_an_instrument_copy(self):
        ws = _fm_note(4, 1204, off=60000)
        for i, fnum in enumerate((1210, 1260) * 6):
            t = 30000 + i * 1000
            ws += [
                ChipWrite(t, "ym2203", 0, 0xA4, (4 << 3) | ((fnum >> 8) & 7)),
                ChipWrite(t, "ym2203", 0, 0xA0, fnum & 0xFF),
            ]
        song = self._song(ws)
        ev = next(e for e in song.events if e.on)
        self.assertTrue(song.fm_patches[ev.ins].pitch_macro)
        data = write_fur(song, include_pcm=False)
        self.assertGreaterEqual(data.count(b"INS2"), 2)


class TestYm2203SpecialCases(unittest.TestCase):
    def test_3slot_pitch_uses_per_operator_registers(self):
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2203", 0, 0x27, 0x40),   # 3-slot / CSM mode
            ChipWrite(100, "ym2203", 0, 0xAC, 0x22),   # op2 block 4, fnum high 2
            ChipWrite(100, "ym2203", 0, 0xA8, 0x05),   # op2 fnum 0x205
            ChipWrite(100, "ym2203", 0, 0xA6, 0x01),   # normal fnum, deliberately
            ChipWrite(100, "ym2203", 0, 0xA2, 0x05),   # a different pitch
            # configure channel 3's own operators (MULT=1, alg 7) so the
            # carrier-octave normaliser does not see the all-zero reset patch
            ChipWrite(100, "ym2203", 0, 0x32, 0x01),
            ChipWrite(100, "ym2203", 0, 0x36, 0x01),
            ChipWrite(100, "ym2203", 0, 0x3A, 0x01),
            ChipWrite(100, "ym2203", 0, 0x3E, 0x01),
            ChipWrite(100, "ym2203", 0, 0xB2, 0x07),
            ChipWrite(100, "ym2203", 0, 0x28, 0xF2),
            ChipWrite(2000, "ym2203", 0, 0x28, 0xF2),  # same mask: no retrigger
            ChipWrite(60000, "ym2203", 0, 0x28, 0x02),
        ]
        song = analyze(_vgm(ws, ym2203_clock=CLOCK))
        evs = [e for e in song.events if e.on]
        self.assertEqual(len(evs), 4)
        # The ext column that carries operator 0's frequency is module
        # channel 4 (column 2; Furnace's opChanOffs order is 1,2,0,3).
        ev = next(e for e in evs if e.ch == 4)
        self.assertEqual((ev.note, ev.e5), opn_note_for(4, 0x205, CLOCK))
        self.assertTrue(song.ym2203_ext)
        self.assertNotIn("special-mode", " ".join(song.warnings))
        self.assertTrue(all(
            e.ch in (2, 3, 4, 5) for e in evs
        ))
        mod = parse_fur(write_fur(song))
        self.assertEqual(mod.chips, [CHIP_YM2203_EXT])
        self.assertEqual(mod.n_ch, 9)

    def test_mixed_carrier_labels_without_moving_the_audio(self):
        # alg 5, carrier op2 MULT=2, other carriers MULT=1: mixed.  The
        # loudest carrier sets the note label (A#4), while the compensating
        # pitch macro keeps the source channel f-num/audio exactly.  A mixed
        # patch must not transpose the whole channel: that made the 1x
        # carriers (and, on other patches, the loudest carrier) an octave
        # high.
        ws = [w for w in _patch(100) if w.reg not in (0xB0, 0x34)]
        ws += [
            ChipWrite(100, "ym2203", 0, 0xB0, 0x05),   # alg 5
            ChipWrite(100, "ym2203", 0, 0x34, 0x02),   # op2 MULT=2
            ChipWrite(100, "ym2203", 0, 0xA4, 0x25),   # block 4, fnum 0x5BA
            ChipWrite(100, "ym2203", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2203", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2203", 0, 0x28, 0x00),
        ]
        clock = 1_500_000
        song = analyze(_vgm(ws, ym2203_clock=clock))
        note, e5 = opn_note_for(4, 1466, clock)
        ev = next(e for e in song.events if e.on)
        self.assertEqual((ev.note, ev.e5), (note + 12, e5))
        patch = song.fm_patches[ev.ins]
        self.assertEqual(sorted(patch.ops[i].dt_mul & 0x0F for i in (1, 2, 3)), [1, 1, 2])
        self.assertEqual(patch.pitch_macro, (-1536,))
        self.assertEqual(furnace_note_name(ev.note), "A#4")

    def test_loudest_carrier_sets_the_label(self):
        # alg 4, carriers S2 (0x38) MULT=4 and S4 (0x3C) MULT=2.  The 2x
        # carrier is 3 dB louder (lower TL), so it is the audible lead: the
        # label moves one octave, not two, and the macro keeps the source
        # f-num.
        ws = [
            w for w in _patch(100)
            if w.reg not in (0xB0, 0x34, 0x38, 0x3C, 0x44, 0x48, 0x4C)
        ]
        ws += [
            ChipWrite(100, "ym2203", 0, 0xB0, 0x04),   # alg 4
            ChipWrite(100, "ym2203", 0, 0x38, 0x04),   # S2 MULT=4
            ChipWrite(100, "ym2203", 0, 0x3C, 0x02),   # S4 MULT=2
            ChipWrite(100, "ym2203", 0, 0x48, 0x14),   # S2 TL 0x14 (quieter)
            ChipWrite(100, "ym2203", 0, 0x4C, 0x11),   # S4 TL 0x11 (louder)
            ChipWrite(100, "ym2203", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2203", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2203", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2203", 0, 0x28, 0x00),
        ]
        clock = 1_500_000
        song = analyze(_vgm(ws, ym2203_clock=clock))
        note, e5 = opn_note_for(4, 1466, clock)
        ev = next(e for e in song.events if e.on)
        self.assertEqual((ev.note, ev.e5), (note + 12, e5))
        self.assertEqual(song.fm_patches[ev.ins].pitch_macro, (-1536,))

    def test_non_octave_loudest_carrier_is_not_relabelled(self):
        # The loudest carrier is MULT=3; there is no whole-octave label, so
        # the note stays at the channel f-num and no macro is added.
        ws = [
            w for w in _patch(100)
            if w.reg not in (0xB0, 0x34, 0x44, 0x48, 0x4C)
        ]
        ws += [
            ChipWrite(100, "ym2203", 0, 0xB0, 0x05),   # alg 5
            ChipWrite(100, "ym2203", 0, 0x34, 0x03),   # op2 MULT=3 (loudest)
            ChipWrite(100, "ym2203", 0, 0x44, 0x00),   # op2 TL 0
            ChipWrite(100, "ym2203", 0, 0x48, 0x20),   # other carriers quieter
            ChipWrite(100, "ym2203", 0, 0x4C, 0x20),
            ChipWrite(100, "ym2203", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2203", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2203", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2203", 0, 0x28, 0x00),
        ]
        clock = 1_500_000
        song = analyze(_vgm(ws, ym2203_clock=clock))
        note, e5 = opn_note_for(4, 1466, clock)
        ev = next(e for e in song.events if e.on)
        self.assertEqual((ev.note, ev.e5), (note, e5))
        self.assertEqual(song.fm_patches[ev.ins].pitch_macro, ())

    def test_uniform_carrier_keeps_the_channel_octave(self):
        # All three alg-5 carriers are MULT=2, so the channel already sounds
        # an octave up.  The pattern note moves to C-4/A#4 etc., while a
        # constant -1536 pitch macro preserves the source f-num/audio.
        ws = [
            w for w in _patch(100)
            if w.reg not in (0xB0, 0x34, 0x38, 0x3C)
        ]
        ws += [
            ChipWrite(100, "ym2203", 0, 0xB0, 0x05),   # alg 5
            ChipWrite(100, "ym2203", 0, 0x34, 0x02),   # op2 MULT=2
            ChipWrite(100, "ym2203", 0, 0x38, 0x02),   # op3 MULT=2
            ChipWrite(100, "ym2203", 0, 0x3C, 0x02),   # op4 MULT=2
            ChipWrite(100, "ym2203", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2203", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2203", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2203", 0, 0x28, 0x00),
        ]
        clock = 1_500_000
        song = analyze(_vgm(ws, ym2203_clock=clock))
        note, e5 = opn_note_for(4, 1466, clock)
        ev = next(e for e in song.events if e.on)
        self.assertEqual((ev.note, ev.e5), (note + 12, e5))
        patch = song.fm_patches[ev.ins]
        self.assertEqual(patch.pitch_macro, (-1536,))

    def test_fm_fallback_pitch_tick_scale(self):
        ws = [
            ChipWrite(100 + i * 300, "ym2203", 0, 0xA0, i)
            for i in range(13)
        ]
        song = analyze(_vgm(ws, ym2203_clock=CLOCK))
        self.assertEqual(song.hz, 180.0)
        self.assertEqual(song.speed, 3)
        self.assertAlmostEqual(44100 * song.speed / song.hz, 735, delta=5)

    def test_ssg_fast_period_tick_scale(self):
        ws = _ssg_note(off=60000)
        ws += [
            ChipWrite(150 + i * 50, "ym2203_ssg", 0, 0, 0x1C - i)
            for i in range(15)
        ]
        song = analyze(_vgm(ws, ym2203_clock=CLOCK))
        self.assertEqual(song.hz, 900.0)
        self.assertEqual(song.speed, 15)
        self.assertAlmostEqual(44100 * song.speed / song.hz, 735, delta=5)


class TestYm2203Diag(unittest.TestCase):
    def test_ssg_stream_skips_inaudible_period(self):
        ws = _ssg_note(off=60000)
        ws += [
            ChipWrite(20000, "ym2203_ssg", 0, 1, 0x00),
            ChipWrite(20001, "ym2203_ssg", 0, 0, 0x00),  # period 0
        ]
        stream = opn_pitch_stream(_vgm(ws, ym2203_clock=CLOCK), 3)
        self.assertTrue(stream)
        # period 0 would map to the maximum pitch unit (0); the converter
        # treats it as silence and never emits it
        self.assertFalse(any(u == 0 for _t, u in stream))

    def test_pitch_stream_maps_channels(self):
        src = _vgm(_fm_note(4, 1232, cid=0) + _fm_note(4, 923, cid=1, t=200),
                   ym2203_clock=CLOCK)
        self.assertEqual(len(opn_pitch_stream(src, 6)), 1)
        self.assertEqual(opn_pitch_stream(src, 3), [])  # SSG voice
        self.assertAlmostEqual(
            opn_pitch_stream(src, 0)[0][1], opn_pitch_units(4, 1232)
        )

    def test_pitch_runs_use_opn_units(self):
        def with_hold(block, fnum, ws):
            # a later unchanged f-num write gives pitch_runs an end time
            return ws + [
                ChipWrite(50000, "ym2203", 0, 0xA4,
                          (block << 3) | ((fnum >> 8) & 7)),
                ChipWrite(50000, "ym2203", 0, 0xA0, fnum & 0xFF),
            ]

        a = _vgm(with_hold(4, 1204, _fm_note(4, 1204, off=60000)),
                 ym2203_clock=CLOCK)
        b = _vgm(with_hold(4, 1232, _fm_note(4, 1232, off=60000)),
                 ym2203_clock=CLOCK)
        runs = pitch_runs(a, b, 0, threshold_st=0.2, min_run_ms=10)
        self.assertTrue(runs)
        self.assertGreater(abs(runs[0].worst_st), 0.3)


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestYm2203Furnace(unittest.TestCase):
    def _export(self, song, name: str):
        fur = TMP / f"{name}.fur"
        out = TMP / f"{name}.vgm"
        fur.write_bytes(write_fur(song, include_pcm=False))
        export_vgm(fur, out)
        return load_vgm(out)

    def test_note_pitch_round_trip(self):
        vgm = self._export(analyze(_vgm(_fm_note(4, 1232), ym2203_clock=CLOCK)),
                           "ym2203_note")
        self.assertEqual(vgm.ym2203_clock, CLOCK)
        # Furnace writes the high byte then the low one; pair on the low byte.
        pairs = []
        lo = hi = 0
        for w in vgm.writes:
            if w.chip != "ym2203" or w.chip_id != 0:
                continue
            if 0xA0 <= w.reg <= 0xA2:
                lo = w.val
                pairs.append(((hi >> 3) & 7, ((hi & 7) << 8) | lo))
            elif 0xA4 <= w.reg <= 0xA6:
                hi = w.val
        self.assertIn((4, 1232), pairs)

    def test_sr_and_ssg_eg_reach_the_chip(self):
        ws = _fm_note(
            4, 1232,
            sr=(0x12, 0x13, 0x14, 0x15),
            ssg=(0x05, 0x06, 0x07, 0x08),
        )
        vgm = self._export(analyze(_vgm(ws, ym2203_clock=CLOCK)), "ym2203_ops")
        reg: dict[int, set[int]] = {}
        for w in vgm.writes:
            if w.chip == "ym2203" and w.chip_id == 0:
                reg.setdefault(w.reg, set()).add(w.val)
        for op in range(4):
            self.assertIn(0x12 + op, {v & 0x1F for v in reg[0x70 + op * 4]})
            self.assertIn(0x05 + op, {v & 0x0F for v in reg[0x90 + op * 4]})

    def test_ssg_period_round_trip(self):
        vgm = self._export(analyze(_vgm(_ssg_note(), ym2203_clock=CLOCK)),
                           "ym2203_ssg")
        reg: dict[int, set[int]] = {}
        for w in vgm.writes:
            if w.chip == "ym2203_ssg" and w.chip_id == 0:
                reg.setdefault(w.reg, set()).add(w.val)
        self.assertIn(0x1C, reg.get(0, set()))
        self.assertIn(0x01, reg.get(1, set()))
        self.assertIn(0x0F - _SSG_VOLUME_TRIM, reg.get(8, set()))


class TestYm2203OctaveConvention(unittest.TestCase):
    """The base note is the source channel f-num/block.

    A source note at block 3 / f-num 1646 at 1.5 MHz is C-3 (130.81 Hz).
    The analyzer then moves the pattern note to the highest power-of-two
    carrier octave.  If the carriers disagree, the channel itself is
    transposed by the spread (lowest to highest) to match the reference's
    per-channel carrier; if they agree, a constant pitch macro preserves the
    source f-num.  ``test_mixed_carrier_transposes_the_channel_octave`` and
    ``test_uniform_carrier_keeps_the_channel_octave`` cover both cases.
    """

    def test_source_c3_maps_to_furnace_c3(self):
        self.assertEqual(opn_note_fnum(96, 1_500_000), (3, 1646))
        self.assertEqual(opn_note_for(3, 1646, 1_500_000), (96, 0x80))
        self.assertEqual(furnace_note_name(96), "C-3")

    def test_carrier_mult_does_not_transpose_the_channel_note(self):
        # Legendary Wings Area 1 (OPN 00) has alg 5 and carrier op2 MULT=2.
        # The source channel is still block 3 / f-num 2933: A#3, with an
        # octave-above carrier component in the patch.
        self.assertEqual(opn_note_fnum(106, 1_500_000), (3, 2933))
        self.assertEqual(furnace_note_name(106), "A#3")


if __name__ == "__main__":
    unittest.main()
