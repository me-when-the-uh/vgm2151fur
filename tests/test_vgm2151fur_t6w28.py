"""T6W28 (Neo Geo Pocket Color) and dual-chip VGM coverage.

The T6W28 is one stereo chip built from the two SN76489 register halves, so
the VGM header sets both the dual bit (30) and the T6W28 bit (31).  Command
0x50 feeds the left half (tone periods and left attenuation) and 0x30 the
right half (right attenuation, noise select and noise extra period).  A log
therefore never carries a normal SN76489 and a T6W28 at the same time; this
file pins that down together with the ordinary dual-instance commands for the
other supported chips.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import NoteEvent, Song, analyze
from vgm2151fur.furio import iter_blocks, parse_fur
from vgm2151fur.furwrite import (
    CHIP_K007232, CHIP_RF5C68, CHIP_SEGAPCM, CHIP_SMS, CHIP_T6W28,
    CHIP_YM2151, INS_T6W28, NOTE_OFF, _build_patterns, write_fur,
)
from vgm2151fur.sn76489 import (
    collect_sn76489, t6w28_note, t6w28_noise_note,
)
from vgm2151fur.vgm import (
    ChipWrite, RomSlice, VgmFile, chips_in_head, load_vgm,
)

TMP = Path(tempfile.mkdtemp(prefix="vgm2151fur_t6w28_"))
T6_CLOCK = 3_072_000


def _raw_vgm(
    body: bytes, *,
    clocks: dict[int, int] | None = None,
    version: int = 0x171,
    total: int = 100_000,
) -> bytes:
    """A small VGM with a 0x100-byte header and the supplied clock fields."""
    base = 0x100
    buf = bytearray(base)
    buf[0:4] = b"Vgm "
    buf[8:12] = version.to_bytes(4, "little")
    buf[0x18:0x1C] = total.to_bytes(4, "little")
    buf[0x34:0x38] = (base - 0x34).to_bytes(4, "little")
    for off, value in (clocks or {}).items():
        buf[off:off + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")
    out = bytes(buf) + body + b"\x66"
    return out[:4] + (len(out) - 4).to_bytes(4, "little") + out[8:]


def _vgm(writes: list[ChipWrite], **kw) -> VgmFile:
    kw.setdefault("version", 0x171)
    kw.setdefault("total_samples", 200_000)
    kw.setdefault("ym2151_clock", 0)
    kw.setdefault("k007232_clock", 0)
    kw.setdefault("roms", [])
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", loop_samples=0,
        loop_file_pos=None, loop_sample=None, gd3={}, writes=writes, **kw,
    )


def _left(t: int, voice: int, period: int, atten: int) -> list[ChipWrite]:
    return [
        ChipWrite(t, "sn76489", 0, 0,
                  0x80 | (voice << 5) | (period & 0x0F)),
        ChipWrite(t, "sn76489", 0, 0, (period >> 4) & 0x3F),
        ChipWrite(t, "sn76489", 0, 0, 0x90 | (voice << 5) | atten),
    ]


def _right(t: int, voice: int, atten: int) -> list[ChipWrite]:
    return [
        ChipWrite(t, "sn76489", 1, 0, 0x90 | (voice << 5) | atten),
    ]


def _close(t: int, voice: int) -> list[ChipWrite]:
    return [
        ChipWrite(t, "sn76489", 0, 0, 0x90 | (voice << 5) | 15),
        ChipWrite(t, "sn76489", 1, 0, 0x90 | (voice << 5) | 15),
    ]


def _instrument_types(data: bytes) -> list[int]:
    out = []
    for _off, mag, _size, payload in iter_blocks(data):
        if mag == b"INS2" and len(payload) >= 4:
            out.append(int.from_bytes(payload[2:4], "little"))
    return out


class TestT6W28Loader(unittest.TestCase):
    def test_header_and_both_halves_parse(self):
        body = bytes([
            0x50, 0x8F, 0x50, 0x01, 0x50, 0x96,   # left tone + left volume
            0x30, 0x9A,                            # right volume
            0x30, 0xD0, 0x30, 0x02,                # noise extra period
            0x30, 0xE7,                            # noise select 3, white
        ])
        p = TMP / "t6_header.vgm"
        p.write_bytes(_raw_vgm(
            body, clocks={0x0C: T6_CLOCK | 0xC0000000},
        ))
        vgm = load_vgm(p)
        self.assertTrue(vgm.sn76489_dual)
        self.assertTrue(vgm.sn76489_t6w28)
        self.assertEqual(vgm.sn76489_clock, T6_CLOCK)
        self.assertEqual(
            [(w.chip_id, w.reg, w.val) for w in vgm.writes],
            [(0, 0, 0x8F), (0, 0, 0x01), (0, 0, 0x96),
             (1, 0, 0x9A), (1, 0, 0xD0), (1, 0, 0x02), (1, 0, 0xE7)],
        )
        kinds = chips_in_head(p.read_bytes())
        self.assertIn("t6w28", kinds)
        self.assertNotIn("sn76489", kinds)
        self.assertNotIn("sn76489_dual", kinds)

    def test_second_stream_without_the_dual_bit_is_reported(self):
        p = TMP / "t6_stray.vgm"
        p.write_bytes(_raw_vgm(bytes([0x30, 0x9A]),
                               clocks={0x0C: T6_CLOCK}))
        vgm = load_vgm(p)
        self.assertFalse(vgm.sn76489_dual)
        self.assertFalse(vgm.sn76489_t6w28)
        self.assertEqual(
            [(w.chip_id, w.val) for w in vgm.writes], [(1, 0x9A)],
        )
        self.assertTrue(any("without the dual-instance" in w
                            for w in vgm.warnings))

    def test_t6w28_bit_without_dual_bit_is_mono_with_warning(self):
        p = TMP / "t6_half.vgm"
        p.write_bytes(_raw_vgm(
            bytes([0x50, 0x9F]), clocks={0x0C: T6_CLOCK | 0x80000000},
        ))
        vgm = load_vgm(p)
        self.assertFalse(vgm.sn76489_dual)
        self.assertFalse(vgm.sn76489_t6w28)
        self.assertTrue(any("T6W28 flag" in w for w in vgm.warnings))


class TestT6W28Collector(unittest.TestCase):
    def test_independent_side_levels_become_volume_and_pan(self):
        writes = _left(0, 0, 31, 6) + _right(0, 0, 10)
        writes += _close(1000, 0)
        hits, offs, warn = collect_sn76489(
            _vgm(writes, sn76489_clock=T6_CLOCK, sn76489_t6w28=True,
                 sn76489_dual=True),
        )
        self.assertFalse(warn)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual((hit.voice, hit.period, hit.vol), (0, 31, 9))
        # min attenuation 6: left full, right 11/15 (value 0xFB).
        self.assertEqual(hit.pan, 0xFB)
        self.assertTrue(hit.t6w28)
        self.assertEqual((offs[0].sample_time, offs[0].voice), (1000, 0))

    def test_side_volume_change_while_held_is_a_mix_point(self):
        writes = _left(0, 0, 31, 6) + _right(0, 0, 10)
        writes += [ChipWrite(500, "sn76489", 1, 0, 0x9F)]  # right off
        writes += _close(1000, 0)
        hits, _offs, _warn = collect_sn76489(
            _vgm(writes, sn76489_clock=T6_CLOCK, sn76489_t6w28=True,
                 sn76489_dual=True),
        )
        hit = hits[0]
        # The loud side is 9/15, so its split is 15; the now-silent right side
        # needs register attenuation 15, which is pan nibble 6 against that
        # 9-level column.
        self.assertEqual(hit.mix_pts, [(500, 9, 0xF6)])

    def test_noise_select_and_extra_period_come_from_the_right_half(self):
        writes = _left(0, 0, 31, 6)      # tone engine 1
        writes += [ChipWrite(0, "sn76489", 0, 0, 0xF6)]   # left noise
        writes += [ChipWrite(0, "sn76489", 1, 0, 0xFA)]   # right noise
        writes += [ChipWrite(0, "sn76489", 1, 0, 0xE0)]   # fixed rate 0
        writes += [ChipWrite(300, "sn76489", 1, 0, 0xC0)]  # extra low
        writes += [ChipWrite(300, "sn76489", 1, 0, 0x20)]  # extra high
        writes += [ChipWrite(300, "sn76489", 1, 0, 0xE7)]  # select 3 white
        writes += [
            ChipWrite(900, "sn76489", 0, 0, 0xFF),
            ChipWrite(900, "sn76489", 1, 0, 0xFF),
        ]
        hits, _offs, _warn = collect_sn76489(
            _vgm(writes, sn76489_clock=T6_CLOCK, sn76489_t6w28=True,
                 sn76489_dual=True),
        )
        noise = next(h for h in hits if h.noise)
        self.assertEqual(noise.period, 0x100)
        self.assertEqual(noise.noise_mode, 0)
        self.assertEqual(noise.vol, 9)
        self.assertEqual(noise.pan, 0xFB)
        self.assertIn((300, 0x200), noise.pitch_pts)
        self.assertIn((300, 4), noise.mode_pts)

    def test_note_round_trips_through_the_t6w28_pitch_tables(self):
        from vgm2151fur.pitch import pitch_table
        tone = pitch_table(T6_CLOCK, 16, maximum=0x3FF)
        for period in (8, 14, 31, 100, 511, 1023):
            note, e5 = t6w28_note(period, T6_CLOCK)
            self.assertEqual(tone.value(note, e5 - 0x80), period)
        # Periods below the table's shortest note clamp like the plain PSG.
        note, e5 = t6w28_note(2, T6_CLOCK)
        self.assertEqual(tone.value(note, e5 - 0x80),
                         tone.value(179, 127))
        for period in (0x100, 0x200, 0x3FF):
            note, e5 = t6w28_noise_note(period, T6_CLOCK)
            from vgm2151fur.pitch import pitch_table
            tbl = pitch_table(T6_CLOCK, 15, maximum=0x3FF)
            self.assertEqual(tbl.value(note, e5 - 0x80), period)


class TestT6W28Module(unittest.TestCase):
    def _song(self):
        writes = _left(0, 1, 200, 4) + _right(0, 1, 7)
        writes += _left(1000, 2, 300, 2) + _right(1000, 2, 2)
        writes += _close(5000, 1) + _close(5000, 2)
        return analyze(_vgm(
            writes, sn76489_clock=T6_CLOCK, sn76489_t6w28=True,
            sn76489_dual=True, total_samples=8000,
        ))

    def test_one_t6w28_system_with_four_channels(self):
        song = self._song()
        self.assertTrue(song.psg_t6w28)
        self.assertEqual(song.psg_ids, [0])
        notes = [e for e in song.events if e.on and e.tone_chip == 3]
        self.assertEqual(len(notes), 2)
        self.assertEqual({e.tone_div for e in notes}, {16})
        self.assertTrue(all(e.tone_clock == T6_CLOCK for e in notes))
        self.assertTrue(all(e.pan for e in notes))
        data = write_fur(song, include_pcm=False)
        mod = parse_fur(data)
        self.assertEqual(mod.chips, [CHIP_T6W28])
        self.assertEqual(mod.n_ch, 4)
        self.assertEqual(set(_instrument_types(data)), {INS_T6W28})

    def test_panning_reaches_a_pattern_cell(self):
        writes = _left(0, 0, 31, 0) + _right(0, 0, 15)
        writes += _close(5000, 0)
        song = analyze(_vgm(
            writes, sn76489_clock=T6_CLOCK, sn76489_t6w28=True,
            sn76489_dual=True, total_samples=8000,
        ))
        data = write_fur(song, include_pcm=False)
        mod = parse_fur(data)
        rows = mod.patterns[(0, 0)]
        effects = [fx for row in rows for fx in row.fx]
        self.assertIn((0x08, 0xF0), effects)

    def test_dual_sn76489_writes_two_sms_systems(self):
        writes = _left(0, 0, 31, 6) + [
            ChipWrite(0, "sn76489", 1, 0, 0x80 | (200 & 0x0F)),
            ChipWrite(0, "sn76489", 1, 0, (200 >> 4) & 0x3F),
            ChipWrite(0, "sn76489", 1, 0, 0x90 | 6),
        ]
        writes += [
            ChipWrite(5000, "sn76489", 0, 0, 0x9F),
            ChipWrite(5000, "sn76489", 1, 0, 0x9F),
        ]
        song = analyze(_vgm(
            writes, sn76489_clock=3579545, sn76489_dual=True,
            total_samples=8000,
        ))
        self.assertFalse(song.psg_t6w28)
        self.assertEqual(song.psg_ids, [0, 1])
        self.assertEqual(song.psg_count, 2)
        chans = {e.ch for e in song.events if e.on and e.tone_chip == 3}
        self.assertEqual(len(chans), 2)
        self.assertEqual(max(chans) - min(chans), 4)
        mod = parse_fur(write_fur(song, include_pcm=False))
        self.assertEqual(mod.chips, [CHIP_SMS, CHIP_SMS])
        self.assertEqual(mod.n_ch, 8)


class TestDualCommands(unittest.TestCase):
    def _load(self, body: bytes, clock_off: int, clock: int):
        p = TMP / f"dual_{clock_off:02x}.vgm"
        p.write_bytes(_raw_vgm(body, clocks={clock_off: clock | 0x40000000}))
        return load_vgm(p)

    def test_ym2151_second_command(self):
        vgm = self._load(bytes([0xA4, 0x20, 0x07]), 0x30, 3579545)
        self.assertTrue(vgm.ym2151_dual)
        self.assertEqual([(w.chip_id, w.reg, w.val) for w in vgm.writes],
                         [(1, 0x20, 0x07)])

    def test_opl_family_second_commands(self):
        body = bytes([
            0xAB, 0x20, 0x01,   # YM3526 chip 1
            0xAC, 0x21, 0x02,   # Y8950 chip 1
            0xAE, 0x22, 0x03,   # YMF262 chip 1 port 0
            0xAF, 0x23, 0x04,   # YMF262 chip 1 port 1
        ])
        p = TMP / "opl_dual.vgm"
        p.write_bytes(_raw_vgm(body, clocks={
            0x54: 3579545 | 0x40000000,
            0x58: 3579545 | 0x40000000,
            0x5C: 14318180 | 0x40000000,
        }))
        vgm = load_vgm(p)
        self.assertTrue(vgm.opl_dual)
        self.assertTrue(vgm.y8950_dual)
        self.assertTrue(vgm.opl3_dual)
        self.assertEqual(
            [(w.chip, w.chip_id, w.reg, w.val) for w in vgm.writes],
            [("opl", 1, 0x20, 0x01), ("y8950", 1, 0x21, 0x02),
             ("opl3", 1, 0x22, 0x03), ("opl3", 1, 0x123, 0x04)],
        )

    def test_c140_chip_select_is_address_bit_15(self):
        p = TMP / "c140_dual.vgm"
        p.write_bytes(_raw_vgm(bytes([0xD4, 0x80, 0x12, 0x34]),
                               clocks={0xA8: 12288000 | 0x40000000}))
        vgm = load_vgm(p)
        self.assertTrue(vgm.c140_dual)
        self.assertEqual([(w.chip_id, w.reg, w.val) for w in vgm.writes],
                         [(1, 0x12, 0x34)])

    def test_opl4_port_bit_7_selects_the_second_chip(self):
        p = TMP / "opl4_dual.vgm"
        p.write_bytes(_raw_vgm(bytes([0xD0, 0x82, 0x20, 0x45]),
                               clocks={0x60: 33868800 | 0x40000000}))
        vgm = load_vgm(p)
        self.assertTrue(vgm.ymf278b_dual)
        self.assertEqual([(w.chip_id, w.reg, w.val) for w in vgm.writes],
                         [(1, 0x220, 0x45)])

    def test_rf5c_register_bit_7_selects_the_second_chip(self):
        p = TMP / "rf5c_dual.vgm"
        p.write_bytes(_raw_vgm(bytes([0xB0, 0x88, 0x01]),
                               clocks={0x40: 8000000 | 0x40000000}))
        vgm = load_vgm(p)
        self.assertTrue(vgm.rf5c68_dual)
        self.assertEqual([(w.chip_id, w.reg, w.val) for w in vgm.writes],
                         [(1, 0x08, 0x01)])
        self.assertFalse(any("only handles one RF5C" in w
                             for w in vgm.warnings))

    def test_segapcm_address_bit_15_selects_the_second_chip(self):
        p = TMP / "segapcm_dual.vgm"
        p.write_bytes(_raw_vgm(bytes([0xC0, 0x00, 0x80, 0x11]),
                               clocks={0x38: 4000000 | 0x40000000}))
        vgm = load_vgm(p)
        self.assertTrue(vgm.segapcm_dual)
        self.assertEqual([(w.chip_id, w.reg, w.val) for w in vgm.writes],
                         [(1, 0x00, 0x11)])
        self.assertFalse(any("only handles one SegaPCM" in w
                             for w in vgm.warnings))

    def test_second_game_gear_mask_command(self):
        p = TMP / "gg_dual.vgm"
        p.write_bytes(_raw_vgm(bytes([0x3F, 0xF0]),
                               clocks={0x0C: 3579545 | 0x40000000}))
        vgm = load_vgm(p)
        self.assertTrue(vgm.sn76489_dual)
        self.assertEqual([(w.chip_id, w.reg, w.val) for w in vgm.writes],
                         [(1, 1, 0xF0)])


def _opm_note(cid: int, t: int, ch: int = 0, off: int = 5000):
    out: list[ChipWrite] = []
    for page, vals in (
        (0x20, (0xC7, 0x00)),
        (0x28, (0x4A, 0x00)),
        (0x30, (0x00, 0x00)),
        (0x40, (0x01, 0x00)),
        (0x50, (0x1F, 0x00)),
        (0x60, (0x00, 0x00)),
        (0x70, (0x00, 0x00)),
        (0x80, (0x0F, 0x00)),
        (0x90, (0x00, 0x00)),
    ):
        for i, val in enumerate(vals):
            out.append(ChipWrite(
                t, "ym2151", cid, page + ch + i * 8, val,
            ))
    out.append(ChipWrite(t, "ym2151", cid, 0x08, 0x78 | ch))
    out.append(ChipWrite(off, "ym2151", cid, 0x08, ch))
    return out


class TestT6W28CutAndVolumeRows(unittest.TestCase):
    def test_note_off_does_not_pair_ec_with_ed(self):
        # The note and its off round onto one row. ECxx on the same line as
        # EDxx can cut the following note, so the writer must not pair them.
        writes = _left(100, 0, 200, 10) + _right(100, 0, 10)
        writes += _close(300, 0)
        song = analyze(_vgm(
            writes, sn76489_clock=T6_CLOCK, sn76489_t6w28=True,
            sn76489_dual=True, total_samples=8000,
        ))
        mod = parse_fur(write_fur(song, include_pcm=True))
        for (ch, _idx), rows in mod.patterns.items():
            if ch >= 4:
                continue
            for row in rows:
                cmds = {c for c, _v in (row.fx or ())}
                self.assertFalse(0xEC in cmds and 0xED in cmds, row)

    def test_louder_same_row_step_keeps_the_note_attack(self):
        writes = _left(0, 0, 200, 10) + _right(0, 0, 10)
        writes += _left(50, 0, 200, 2) + _right(50, 0, 2)
        writes += _close(4000, 0)
        song = analyze(_vgm(
            writes, sn76489_clock=T6_CLOCK, sn76489_t6w28=True,
            sn76489_dual=True, total_samples=8000,
        ))
        mod = parse_fur(write_fur(song, include_pcm=True))
        rows = mod.patterns[(0, mod.orders[0][0])]
        note_rows = [(i, r) for i, r in enumerate(rows) if r.note not in (-1, 180)]
        self.assertEqual(note_rows[0][1].vol, 5)
        later = [
            i for i, r in enumerate(rows)
            if i > note_rows[0][0] and r.note == -1 and r.vol == 13
        ]
        self.assertTrue(later)


class TestPsgShortGateCut(unittest.TestCase):
    def test_short_noise_gate_keeps_a_safe_cut(self):
        # A one-frame noise hit whose off rounds onto its own row used to
        # lose both its ED delay and the off, so the gate ran until the
        # channel's next key-on much later. Keep the cut on the note row, but
        # only when ECxx lands after the delayed note starts (ECxx > EDxx).
        # ECxx <= EDxx makes Furnace cut before the note sounds, the T6W28
        # premature-cut bug. 1102.5 samples/row is the measured Fantasy Zone
        # shape: the hit starts two ticks into row 56 and ends ten ticks in,
        # so both round onto row 56.
        v = VgmFile(
            path=Path("synthetic.vgm"), buf=b"", version=0x161,
            total_samples=100000, loop_samples=0, loop_file_pos=None,
            loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
            roms=[], writes=[], sn76489_clock=3579540,
        )
        song = Song(v, 0, None, 12, 480.0, [], [], [
            NoteEvent(
                sample=61920, ch=11, on=True, note=62, ins=0, vol=12,
                e5=0x80, pan=0, end=62655, tone_chip=3,
                tone_clock=3579540, tone_div=16,
            ),
            NoteEvent(
                sample=62655, ch=11, on=False, note=180, ins=-1, vol=-1,
                e5=0x80, pan=0,
            ),
        ])
        _orders, patterns, _loop, _len = _build_patterns(
            song, 4, 256, 3, ch_offset=-8, stats={},
        )
        rows = next(
            rows for ch, _pat, rows in patterns
            if ch == 3 and any(
                r is not None and r.note not in (-1, NOTE_OFF) for r in rows
            )
        )
        at = next(
            i for i, r in enumerate(rows)
            if r is not None and r.note not in (-1, NOTE_OFF)
        )
        fx = dict(rows[at].fx)
        self.assertIn(0xED, fx)
        self.assertIn(0xEC, fx)
        self.assertGreater(fx[0xEC], fx[0xED])

    def test_cut_at_the_next_key_on_sample_is_not_written(self):
        # A one-sample blip (the PCE wave case) has its key-off on the same
        # sample as the next key-on. The next key-on already ends it; an ECxx
        # here would land on the new note's tick.
        v = VgmFile(
            path=Path("synthetic.vgm"), buf=b"", version=0x161,
            total_samples=100000, loop_samples=0, loop_file_pos=None,
            loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
            roms=[], writes=[], sn76489_clock=3579540,
        )
        song = Song(v, 0, None, 12, 480.0, [], [], [
            NoteEvent(
                sample=61920, ch=11, on=True, note=62, ins=0, vol=12,
                e5=0x80, pan=0, end=62655, tone_chip=3,
                tone_clock=3579540, tone_div=16,
            ),
            NoteEvent(
                sample=62655, ch=11, on=False, note=180, ins=-1, vol=-1,
                e5=0x80, pan=0,
            ),
            NoteEvent(
                sample=62655, ch=11, on=True, note=60, ins=0, vol=12,
                e5=0x80, pan=0, end=64000, tone_chip=3,
                tone_clock=3579540, tone_div=16,
            ),
        ])
        _orders, patterns, _loop, _len = _build_patterns(
            song, 4, 256, 3, ch_offset=-8, stats={},
        )
        rows = next(
            rows for ch, _pat, rows in patterns
            if ch == 3 and any(
                r is not None and r.note not in (-1, NOTE_OFF) for r in rows
            )
        )
        at = next(
            i for i, r in enumerate(rows)
            if r is not None and r.note not in (-1, NOTE_OFF)
        )
        self.assertNotIn(0xEC, dict(rows[at].fx))
        self.assertIn(0xED, dict(rows[at].fx))

    def test_cut_reaching_the_next_key_on_is_not_written(self):
        # Neo South Island Act 2: the cut (ED03 EC05) lands on the same tick
        # as the next key-on. Writing ECxx there cuts the new note instead of
        # the old one, so the next key-on must be allowed to end the note.
        v = VgmFile(
            path=Path("synthetic.vgm"), buf=b"", version=0x161,
            total_samples=100000, loop_samples=0, loop_file_pos=None,
            loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
            roms=[], writes=[], sn76489_clock=3579540,
        )
        song = Song(v, 0, None, 12, 480.0, [], [], [
            NoteEvent(
                sample=61920, ch=11, on=True, note=62, ins=0, vol=12,
                e5=0x80, pan=0, end=62655, tone_chip=3,
                tone_clock=3579540, tone_div=16,
            ),
            NoteEvent(
                sample=62655, ch=11, on=False, note=180, ins=-1, vol=-1,
                e5=0x80, pan=0,
            ),
            # Same row tick as the would-be ED+EC cut.
            NoteEvent(
                sample=62843, ch=11, on=True, note=62, ins=0, vol=12,
                e5=0x80, pan=0, end=64000, tone_chip=3,
                tone_clock=3579540, tone_div=16,
            ),
        ])
        _orders, patterns, _loop, _len = _build_patterns(
            song, 4, 256, 3, ch_offset=-8, stats={},
        )
        rows = next(
            rows for ch, _pat, rows in patterns
            if ch == 3 and any(
                r is not None and r.note not in (-1, NOTE_OFF) for r in rows
            )
        )
        at = next(
            i for i, r in enumerate(rows)
            if r is not None and r.note not in (-1, NOTE_OFF)
        )
        self.assertNotIn(0xEC, dict(rows[at].fx))
        self.assertIn(0xED, dict(rows[at].fx))
        self.assertTrue(
            rows[at + 1] is not None
            and rows[at + 1].note not in (-1, NOTE_OFF)
        )


class TestDualYm2151(unittest.TestCase):
    def test_both_instances_get_their_own_columns(self):
        writes = _opm_note(0, 100, 0) + _opm_note(1, 100, 0)
        song = analyze(_vgm(
            writes, ym2151_clock=3579545, ym2151_dual=True,
            total_samples=8000,
        ))
        self.assertEqual(song.ym2151_ids, [0, 1])
        self.assertEqual(
            [(s.kind, s.chip_id, s.fm_channels) for s in song.fm_systems],
            [("ym2151", 0, 8), ("ym2151", 1, 8)],
        )
        chans = {e.ch for e in song.events if e.on and not e.pcm}
        self.assertEqual(chans, {0, 8})
        mod = parse_fur(write_fur(song, include_pcm=False))
        self.assertEqual(mod.chips, [CHIP_YM2151, CHIP_YM2151])
        self.assertEqual(mod.n_ch, 16)


class TestDualSegaPcm(unittest.TestCase):
    @staticmethod
    def _key(cid: int, t: int):
        return [
            ChipWrite(t, "segapcm", cid, 0x04, 0x00),
            ChipWrite(t, "segapcm", cid, 0x05, 0x04),
            ChipWrite(t, "segapcm", cid, 0x84, 0x00),
            ChipWrite(t, "segapcm", cid, 0x85, 0x04),
            ChipWrite(t + 2, "segapcm", cid, 0x06, 0x04),
            ChipWrite(t + 2, "segapcm", cid, 0x07, 0x84),
            ChipWrite(t + 2, "segapcm", cid, 0x02, 0x40),
            ChipWrite(t + 2, "segapcm", cid, 0x03, 0x40),
            ChipWrite(t + 2, "segapcm", cid, 0x86, 0x02),
        ]

    def test_second_rom_and_voice_are_converted(self):
        rom = bytearray(0x1000)
        for i in range(0x400, 0x500):
            rom[i] = 0xC0
        roms = [
            RomSlice(0, 0x1000, 0, bytes(rom), "segapcm"),
            RomSlice(1, 0x1000, 0, bytes(rom), "segapcm"),
        ]
        writes = self._key(0, 100) + self._key(1, 200)
        song = analyze(_vgm(
            writes, roms=roms, segapcm_clock=4000000,
            segapcm_dual=True, total_samples=8000,
        ))
        self.assertEqual(song.segapcm_ids, [0, 1])
        self.assertEqual(len(song.segapcm_samples), 2)
        chans = sorted(e.ch for e in song.events if e.on and e.pcm)
        self.assertEqual(len(chans), 2)
        self.assertEqual(chans[1] - chans[0], 16)
        mod = parse_fur(write_fur(song, include_pcm=True))
        self.assertEqual(
            mod.chips, [CHIP_YM2151, CHIP_SEGAPCM, CHIP_SEGAPCM],
        )
        self.assertEqual(mod.n_ch, 8 + 32)


class TestDualRf5c(unittest.TestCase):
    @staticmethod
    def _key(cid: int, t: int):
        return [
            ChipWrite(t, "rf5c68", cid, 0x07, 0x40),
            ChipWrite(t, "rf5c68", cid, 0x00, 0x7F),
            ChipWrite(t, "rf5c68", cid, 0x01, 0xFF),
            ChipWrite(t, "rf5c68", cid, 0x02, 0x00),
            ChipWrite(t, "rf5c68", cid, 0x03, 0x10),
            ChipWrite(t, "rf5c68", cid, 0x04, 0x00),
            ChipWrite(t, "rf5c68", cid, 0x05, 0x00),
            ChipWrite(t, "rf5c68", cid, 0x06, 0x01),
            ChipWrite(t, "rf5c68", cid, 0x08, 0xFE),
        ]

    def test_both_rf5c_instances_are_converted(self):
        body = bytes(range(0x40, 0x80)) + b"\xFF"
        roms = [
            RomSlice(0, 0x1000, 0x100, body, "rf5c68"),
            RomSlice(1, 0x1000, 0x100, body, "rf5c68"),
        ]
        writes = self._key(0, 100) + self._key(1, 200)
        song = analyze(_vgm(
            writes, roms=roms, rf5c68_clock=8000000,
            rf5c68_dual=True, total_samples=8000,
        ))
        self.assertEqual(len(song.rf5c68_samples), 2)
        chans = sorted(e.ch for e in song.events if e.on and e.pcm)
        self.assertEqual(len(chans), 2)
        self.assertEqual(chans[1] - chans[0], 8)
        mod = parse_fur(write_fur(song, include_pcm=True))
        self.assertEqual(
            mod.chips, [CHIP_YM2151, CHIP_RF5C68, CHIP_RF5C68],
        )
        self.assertEqual(mod.n_ch, 8 + 16)


class TestDualK007232(unittest.TestCase):
    @staticmethod
    def _key(cid: int, t: int):
        return [
            ChipWrite(t, "k007232", cid, 0x00, 0xF0),
            ChipWrite(t, "k007232", cid, 0x01, 0x0F),
            ChipWrite(t, "k007232", cid, 0x02, 0x00),
            ChipWrite(t, "k007232", cid, 0x03, 0x01),
            ChipWrite(t, "k007232", cid, 0x04, 0x00),
            ChipWrite(t, "k007232", cid, 0x14, 0x00),
            ChipWrite(t, "k007232", cid, 0x10, 0x88),
            ChipWrite(t, "k007232", cid, 0x11, 0x88),
            ChipWrite(t, "k007232", cid, 0x1F, 0x00),
        ]

    def test_second_bank_and_voice_are_converted(self):
        body = bytes(range(0x40, 0x80)) + b"\x80"
        roms = [
            RomSlice(0, 0x200, 0, body, "k007232"),
            RomSlice(1, 0x200, 0, body, "k007232"),
        ]
        writes = self._key(0, 100) + self._key(1, 200)
        song = analyze(_vgm(
            writes, roms=roms, k007232_clock=3579545,
            k007232_dual=True, total_samples=8000,
        ))
        self.assertEqual(len(song.pcm_samples), 2)
        chans = sorted(e.ch for e in song.events if e.on and e.pcm)
        self.assertEqual(len(chans), 2)
        self.assertEqual(chans[1] - chans[0], 2)
        mod = parse_fur(write_fur(song, include_pcm=True))
        self.assertEqual(
            mod.chips, [CHIP_YM2151, CHIP_K007232, CHIP_K007232],
        )
        self.assertEqual(mod.n_ch, 8 + 4)


if __name__ == "__main__":
    unittest.main()
