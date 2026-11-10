"""YM2608 (OPNA) support: two address ports, the rhythm page, delta-T and layout.

The OPNA is where the YM2612's FM and the YM2610's sample engines meet, so this
module tests the joints rather than re-testing either: that `$56`/`$57` land in
one flat register number with the port in bit 8; that the pages under `$30` on
each port feed the rhythm and delta-T decoders instead of each other; that a
split channel 3 pushes the second block's columns along instead of colliding with
them; and that a delta-T body starts where the chip's own address unit puts it,
not at the head of the 256-byte block around it.

Run:  python -m unittest tests.test_vgm2151fur_ym2608
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze, format_report
from vgm2151fur.furwrite import (
    CHIP_YM2608, CHIP_YM2608_EXT, INS_ADPCMA, INS_ADPCMB, INS_FM,
    parse_fur_skeleton, write_fur,
)
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.furio import (
    CHIP_CHANNELS, iter_blocks, parse_fur, parse_smp2_payload,
)
from vgm2151fur.vgm import (
    ChipWrite, RomSlice, VgmFile, assemble_rom, chips_in_head, load_vgm,
)
from vgm2151fur.ym2608 import (
    ADPCM_B_ADDR_SHIFT,
    RHYTHM_VOICES,
    YM2608_DEFAULT_CLOCK,
    YM2608_SSG_DIVIDER,
    YM2608State,
    _deltat_slice,
    collect_ym2608_delta_t,
    collect_ym2608_rhythm,
    deltat_store_body,
    replay_deltat_memory,
)
from vgm2151fur.ym2612 import ym2612_note_for

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_ym2608"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
CLOCK = YM2608_DEFAULT_CLOCK
# A delta-T image with a recognisable body at 0x260, so a slice can be
# checked byte for byte against what the log asked the chip to read.
ROM = bytes((i * 5 + 1) & 0xFF for i in range(0x800))


def _raw_vgm(body: bytes, *, clock: int = CLOCK, dual: bool = False) -> bytes:
    """A minimal 1.61 log carrying a YM2608 clock at header 0x48."""
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    raw = clock & 0x3FFFFFFF
    if dual:
        raw |= 0x40000000
    buf[0x48:0x4C] = raw.to_bytes(4, "little")
    out = bytes(buf) + body + b"\x66"
    return out[:4] + (len(out) - 4).to_bytes(4, "little") + out[8:]


def _rom_block(data: bytes, *, typ: int = 0x81, start: int = 0) -> bytes:
    size = 8 + len(data)
    return (
        bytes([0x67, 0x66, typ])
        + size.to_bytes(4, "little")
        + len(data).to_bytes(4, "little")
        + start.to_bytes(4, "little")
        + data
    )


def _vgm(writes, rom: bytes | None = None, *, total_samples: int = 8000,
         dual: bool = False) -> VgmFile:
    roms = []
    if rom is not None:
        roms = [RomSlice(0, len(rom), 0, rom, "ym2608_deltat")]
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=total_samples, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        roms=roms, writes=sorted(writes, key=lambda w: w.sample),
        ym2608_clock=CLOCK, ym2608_dual=dual,
    )


def _fm_note(ch: int, *, block: int = 4, fnum: int = 0x400, t: int = 100,
             off: int = 7000, cid: int = 0) -> list[ChipWrite]:
    """A keyed FM voice on the chip's own port: 0-2 first block, 3-5 second."""
    port = ch // 3
    slot = ch % 3
    base = 0x100 if port else 0
    out: list[ChipWrite] = []
    for page in (0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
        for op in range(4):
            val = 0x01 if page == 0x30 else (0x1F if page == 0x50 else 0)
            out.append(ChipWrite(t, "ym2608", cid,
                                 base | page | (op << 2) | slot, val))
    out += [
        ChipWrite(t, "ym2608", cid, base | 0xB0 | slot, 0x07),
        ChipWrite(t, "ym2608", cid, base | 0xB4 | slot, 0xC0),
        ChipWrite(t, "ym2608", cid, base | 0xA4 | slot,
                  (block << 3) | ((fnum >> 8) & 7)),
        ChipWrite(t, "ym2608", cid, base | 0xA0 | slot, fnum & 0xFF),
        # One $28 on port 0 keys all six: the second block is 4-6, after the
        # slot the encoding leaves empty.
        ChipWrite(t, "ym2608", cid, 0x28,
                  (0xF << 4) | (ch if ch < 3 else ch + 1)),
    ]
    if off:
        out.append(ChipWrite(off, "ym2608", cid, 0x28,
                             ch if ch < 3 else ch + 1))
    return out


def _ssg_voice(voice: int, *, period: int = 0x1FF, level: int = 15,
               t: int = 300) -> list[ChipWrite]:
    """A tone gate on the SSG half: the register split lives in vgm.py."""
    return [
        ChipWrite(t, "ym2608_ssg", 0, voice * 2, period & 0xFF),
        ChipWrite(t, "ym2608_ssg", 0, voice * 2 + 1, (period >> 8) & 0x0F),
        ChipWrite(t, "ym2608_ssg", 0, 8 + voice, level),
        ChipWrite(t, "ym2608_ssg", 0, 7, 0x1E & ~(1 << voice)),
    ]


def _rhythm_key(t: int, bits: int, *, cid: int = 0) -> ChipWrite:
    return ChipWrite(t, "ym2608", cid, 0x10, bits)


def _rhythm_level(voice: int, level: int, pan: int = 3, *, t: int = 90) -> ChipWrite:
    return ChipWrite(t, "ym2608", 0, 0x18 + voice, (pan << 6) | level)


def _delta_t_play(t: int, *, start: int, end: int, rate: int, vol: int = 200,
                  pan: int = 3, loop: bool = False) -> list[ChipWrite]:
    """One delta-T play through the port-1 page. Addresses are over 32."""
    return [
        ChipWrite(t, "ym2608", 0, 0x102, (start >> ADPCM_B_ADDR_SHIFT) & 0xFF),
        ChipWrite(t, "ym2608", 0, 0x103, (start >> 13) & 0xFF),
        ChipWrite(t, "ym2608", 0, 0x104, (end >> ADPCM_B_ADDR_SHIFT) & 0xFF),
        ChipWrite(t, "ym2608", 0, 0x105, (end >> 13) & 0xFF),
        ChipWrite(t, "ym2608", 0, 0x109, rate & 0xFF),
        ChipWrite(t, "ym2608", 0, 0x10A, (rate >> 8) & 0xFF),
        ChipWrite(t, "ym2608", 0, 0x10B, vol),
        ChipWrite(t, "ym2608", 0, 0x101, (pan << 6)),
        ChipWrite(t + 1, "ym2608", 0, 0x100, 0x80 | (0x10 if loop else 0)),
    ]


class TestYm2608Parsing(unittest.TestCase):
    def test_header_clock_and_chip_presence(self):
        body = bytes([0x56, 0xB0, 0x07, 0x57, 0xA0, 0x40, 0x62])
        vgm = load_vgm_from(_raw_vgm(body))
        self.assertEqual(vgm.ym2608_clock, CLOCK)
        kinds = chips_in_head(_raw_vgm(body)[:0x200])
        self.assertIn("ym2608", kinds)

    def test_dual_bit_marks_the_second_chip(self):
        body = bytes([0xA6, 0xB0, 0x07])
        vgm = load_vgm_from(_raw_vgm(body, dual=True))
        self.assertTrue(vgm.ym2608_dual)
        self.assertEqual({w.chip_id for w in vgm.writes}, {1})

    def test_second_chip_without_the_bit_is_reported(self):
        vgm = load_vgm_from(_raw_vgm(bytes([0xA6, 0xB0, 0x07])))
        self.assertFalse(vgm.ym2608_dual)
        self.assertTrue(any("dual" in w or "header" in w for w in vgm.warnings),
                        vgm.warnings)

    def test_port_bit_and_ssg_split(self):
        body = bytes([
            0x56, 0x08, 0x0F,      # port 0, below $10: the SSG
            0x56, 0x18, 0xC8,      # port 0, rhythm level: stays with the chip
            0x56, 0xB0, 0x07,      # port 0, FM block 1
            0x57, 0xA0, 0x40,      # port 1 FM (a slot write on the second block)
            0x57, 0x01, 0xC0,      # port 1 below $11: delta-T pan
            0x62,                  # wait
        ])
        vgm = load_vgm_from(_raw_vgm(body))
        by_reg = {(w.chip, w.reg): w.val for w in vgm.writes}
        self.assertEqual(by_reg[("ym2608_ssg", 0x08)], 0x0F)
        self.assertEqual(by_reg[("ym2608", 0x18)], 0xC8)
        self.assertEqual(by_reg[("ym2608", 0xB0)], 0x07)
        self.assertEqual(by_reg[("ym2608", 0x1A0)], 0x40)
        self.assertEqual(by_reg[("ym2608", 0x101)], 0xC0)

    def test_deltat_block_assembles_at_its_offset(self):
        data = bytes((i * 7 + 3) & 0xFF for i in range(256))
        body = _rom_block(data, start=0x40)
        vgm = load_vgm_from(_raw_vgm(body))
        self.assertEqual(vgm.ym2608_clock, CLOCK)
        got = assemble_rom(vgm, "ym2608_deltat")
        self.assertGreaterEqual(len(got), 0x140)   # filled to the slice's end
        self.assertEqual(got[0x40:0x50], data[:16])


def load_vgm_from(raw: bytes) -> VgmFile:
    """load_vgm needs a file; the parser reads the header from disk."""
    path = TMP / "probe.vgm"
    path.write_bytes(raw)
    return load_vgm(path)


class TestYm2608State(unittest.TestCase):
    """The register map: two ports, each with a sample engine in its shadow."""

    def test_rhythm_page_lands_on_the_adpcm_a_decoder(self):
        st = YM2608State()
        st.write(0x11, 49)                    # global level
        st.write(0x18, (3 << 6) | 26)          # BD: pan both, level 26
        self.assertEqual(st.a_master, 49)
        self.assertEqual(st.a_vol[0], 26)
        self.assertEqual(st.a_pan[0], 3)

    def test_delta_t_page_lands_on_the_adpcm_b_decoder(self):
        st = YM2608State()
        for reg, val in ((0x101, 0xC0), (0x102, 0x60), (0x103, 0x01),
                         (0x104, 0xBF), (0x105, 0x01), (0x109, 0x64),
                         (0x10A, 0x62), (0x10B, 139)):
            st.write(reg, val)
        self.assertEqual(st.b_pan, 3)
        # `$102/$103` are a byte address over 32, low then high.
        self.assertEqual(st.b_start, 0x160)
        self.assertEqual(st.b_end, 0x1BF)
        self.assertEqual(st.b_freq, 0x6264)
        self.assertEqual(st.b_vol, 139)

    def test_irq_registers_reach_no_decoder(self):
        """Port 1 `$10` and port 0 `$29` are the OPNA's IRQ pair.

        Both sit where a decoder would otherwise read something: `$10` is inside
        the delta-T page's range one port over, and `$29/$2A` is where the
        YM2612 keeps its DAC byte. Neither may move a sample engine.
        """
        st = YM2608State()
        before = (st.b_control, st.b_pan, st.b_freq, st.a_master, st.dac_enabled)
        st.write(0x110, 0xFF)      # port 1 IRQ flag control
        st.write(0x29, 0xFF)       # port 0 IRQ mask
        st.write(0x2A, 0x80)       # YM2612 DAC data; nothing on the OPNA
        self.assertEqual(
            (st.b_control, st.b_pan, st.b_freq, st.a_master, st.dac_enabled),
            before,
        )

    def test_key_on_maps_the_second_block_past_a_reserved_slot(self):
        st = YM2608State()
        for raw, want in ((0, 0), (1, 1), (2, 2), (4, 3), (5, 4), (6, 5)):
            st.op_mask = [0] * 6
            st.write(0x28, (0xF << 4) | raw)
            self.assertEqual(st.op_mask[want], 0xF, f"$28 value {raw}")

    def test_no_dac_on_the_opna(self):
        st = YM2608State()
        self.assertFalse(st.dac_enabled)
        st.write(0x2B, 0x80)       # YM2612 DAC enable; OPNA IRQ counter byte
        self.assertFalse(st.dac_enabled)

    def test_special_mode_gives_channel_three_four_frequencies(self):
        st = YM2608State()
        st.write(0x27, 0x40)       # channel 3 special
        self.assertTrue(st.multi_freq)
        st.write(0xAC, (4 << 3) | 1)
        st.write(0xA8, 0x22)       # group 0 -> the column Furnace names O3
        self.assertEqual(st.frequency_op(2, 0), (4, 0x122))

    def test_pan_byte_alias_matches_the_ym2610_name(self):
        st = YM2608State()
        st.write(0xB4, (1 << 7) | (1 << 6))
        self.assertEqual(st.pan_byte(0), st.pan_byte_ch(0))

    def test_rhythm_level_keeps_the_global_separate(self):
        """$11 travels as an 1Fxx effect; the column is the voice's level."""
        st = YM2608State()
        st.write(0x18, (3 << 6) | 31)
        st.write(0x11, 0x3F)
        self.assertEqual(st.rhythm_level(0), 31)
        st.write(0x11, 49)
        self.assertEqual(st.rhythm_level(0), 31)
        # The ADPCM-A helper still folds the master, since that engine has no
        # per-chip effect in the module.
        self.assertLess(st.adpcm_a_level(0), 31)


class TestDeltaTCutting(unittest.TestCase):
    def test_body_starts_at_the_chip_address_not_its_block_head(self):
        """A range mid-block must not inherit the bytes before it.

        Rounding a `$102` address down to a 256-byte bank, which is what an
        ADPCM-A ROM wants, would hand the delta-T engine up to 255 bytes of
        somebody else's waveform in front of its own attack.
        """
        rom = bytearray(0x600)
        for i in range(0x200, 0x400):
            rom[i] = (i & 0x7F) | 0x80       # a recognisable sample body
        start = 0x260                        # $102 = 0x13: inside block 2
        body = _deltat_slice(bytes(rom), start, 0x3FF)
        self.assertEqual(body[:8], bytes(rom[start:start + 8]))
        self.assertFalse(len(body) % 32)      # whole address units, as the chip reads

    def test_store_body_pads_to_the_length_furnace_plays(self):
        self.assertEqual(len(deltat_store_body(b"\x01" * 832)), 1024)
        padded = deltat_store_body(b"\x01" * 832)
        self.assertEqual(padded[832:], b"\x80" * 192)
        self.assertEqual(deltat_store_body(b"\x01" * 256), b"\x01" * 256)


class TestRhythmCollector(unittest.TestCase):
    def test_key_bits_open_one_hit_per_voice(self):
        writes = [
            _rhythm_level(0, 26),                # BD level
            _rhythm_level(3, 20),                # HH level
            _rhythm_key(100, 0x09),              # BD + HH together
        ]
        hits, warn = collect_ym2608_rhythm(_vgm(writes))
        self.assertFalse(warn)
        self.assertEqual([h.voice for h in hits], [0, 3])
        self.assertEqual(hits[0].vol, 26)
        self.assertEqual(hits[1].vol, 20)

    def test_release_byte_closes_the_gate(self):
        writes = [
            _rhythm_level(1, 24),
            _rhythm_key(100, 0x02),               # SD on
            _rhythm_key(900, 0x82),               # SD off
        ]
        hits, _w = collect_ym2608_rhythm(_vgm(writes))
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0].sample_time, hits[0].end_time), (100, 900))

    def test_retrigger_ends_the_previous_hit(self):
        writes = [
            _rhythm_level(1, 24),
            _rhythm_key(100, 0x02),
            _rhythm_key(700, 0x02),               # the same voice again
        ]
        hits, _w = collect_ym2608_rhythm(_vgm(writes))
        self.assertEqual([(h.sample_time, h.end_time) for h in hits],
                         [(100, 700), (700, 8000)])

    def test_level_inside_the_gate_moves_the_note_not_a_step(self):
        """Drivers set $18 just before $10; that is the note's own level."""
        writes = [
            _rhythm_key(100, 0x02),               # SD keys at the old level
            _rhythm_level(1, 30, t=120),          # ... and is set microseconds on
        ]
        hits, _w = collect_ym2608_rhythm(_vgm(writes))
        self.assertEqual(hits[0].vol, 30)
        self.assertFalse(hits[0].vol_pts)

    def test_level_after_the_gate_is_a_step(self):
        writes = [
            _rhythm_level(1, 30),
            _rhythm_key(100, 0x02),
            _rhythm_level(1, 8, t=800),           # still inside the voice's body
        ]
        hits, _w = collect_ym2608_rhythm(_vgm(writes))
        self.assertEqual(hits[0].vol, 30)
        self.assertEqual(hits[0].vol_pts, [(800, 8)])

    def test_level_just_after_the_gate_belongs_to_the_hit(self):
        # The real level can follow the key by a few hundred samples; it is
        # still the hit's own level, not a step on the sounding voice.
        writes = [
            _rhythm_level(1, 0),
            _rhythm_key(100, 0x02),
            _rhythm_level(1, 24, t=410),
        ]
        hits, _w = collect_ym2608_rhythm(_vgm(writes))
        self.assertEqual(hits[0].vol, 24)
        self.assertFalse(hits[0].vol_pts)

    def test_global_level_is_not_folded_into_the_hits(self):
        writes = [
            _rhythm_level(0, 31), _rhythm_level(3, 31),
            ChipWrite(90, "ym2608", 0, 0x11, 0x3F),
            _rhythm_key(100, 0x09),
            ChipWrite(400, "ym2608", 0, 0x11, 0x30),
        ]
        hits, _w = collect_ym2608_rhythm(_vgm(writes))
        self.assertEqual(len(hits), 2)
        for h in hits:
            self.assertEqual(h.vol, 31)
            self.assertFalse(h.vol_pts)

    def test_pan_comes_from_the_level_byte(self):
        writes = [
            _rhythm_level(4, 20, pan=1),           # TM left
            _rhythm_key(100, 0x10),
        ]
        hits, _w = collect_ym2608_rhythm(_vgm(writes))
        self.assertEqual(hits[0].pan, 0x0F)


class TestDeltaTCollector(unittest.TestCase):
    ROM = bytes((i * 7 + 3) & 0xFF for i in range(0x1000))

    def _play(self, extra=(), **kw):
        writes = list(_delta_t_play(**kw))
        writes += list(extra)
        return _vgm(writes, self.ROM)

    def test_a_start_bit_opens_one_gate_per_play(self):
        vgm = self._play(t=100, start=0x200, end=0x3FF, rate=0x4000)
        smp, warn = collect_ym2608_delta_t(vgm, self.ROM)
        self.assertFalse(warn)
        self.assertEqual(len(smp), 1)
        h = smp[0].hits[0]
        # The range reads to the end of its last 32-byte block.
        self.assertEqual((h.start, h.end), (0x200, 0x3FF))
        self.assertEqual(h.rate, 0x4000)

    def test_reset_and_cleared_start_close_the_gate(self):
        vgm = self._play(
            t=100, start=0x200, end=0x3FF, rate=0x4000,
            extra=[ChipWrite(900, "ym2608", 0, 0x100, 0x01)],   # reset
        )
        smp, _w = collect_ym2608_delta_t(vgm, self.ROM)
        self.assertEqual(smp[0].hits[0].end_time, 900)

    def test_loop_flag_survives(self):
        vgm = self._play(t=100, start=0x200, end=0x3FF, rate=0x4000, loop=True)
        smp, _w = collect_ym2608_delta_t(vgm, self.ROM)
        self.assertTrue(smp[0].hits[0].loop)

    def test_body_is_the_exact_source_range(self):
        """The collector keeps the span the chip was told to read.

        Padding to Furnace's 256-byte sample quantum is the writer's job
        (`deltat_store_body`), so a range that stops mid-block stays short here:
        otherwise the analysis would claim the source played bytes it never did.
        """
        vgm = self._play(t=100, start=0x260, end=0x3FF, rate=0x4000)
        smp, _w = collect_ym2608_delta_t(vgm, self.ROM)
        self.assertEqual(smp[0].data, self.ROM[0x260:0x400])
        # What Furnace will actually hold, and what the export must give back.
        self.assertEqual(len(deltat_store_body(smp[0].data)), 512)

    def test_mid_play_rate_and_level_become_steps(self):
        vgm = self._play(
            t=100, start=0x200, end=0xFFF, rate=0x4000,
            extra=[
                ChipWrite(600, "ym2608", 0, 0x10B, 90),
                ChipWrite(700, "ym2608", 0, 0x109, 0x00),
                ChipWrite(701, "ym2608", 0, 0x10A, 0x80),
            ],
        )
        smp, _w = collect_ym2608_delta_t(vgm, self.ROM)
        h = smp[0].hits[0]
        self.assertIn((600, 90), h.vol_pts)
        self.assertIn((701, 0x8000), h.rate_pts)

    def test_no_data_block_is_reported(self):
        vgm = self._play(t=100, start=0x200, end=0x3FF, rate=0x4000)
        _smp, warn = collect_ym2608_delta_t(vgm, b"")
        self.assertTrue(any("delta-T" in w or "0x81" in w for w in warn), warn)


class TestYm2608Analyze(unittest.TestCase):
    """Routing, channel layout and the rhythm/delta-T events."""

    def _base(self):
        """One FM note, one SSG voice, one kit hit, one delta-T play."""
        writes = _fm_note(0) + _ssg_voice(0)
        writes += [ChipWrite(90, "ym2608", 0, 0x11, 0x3F),
                   _rhythm_level(0, 26), _rhythm_key(400, 0x01)]
        writes += _delta_t_play(500, start=0x200, end=0x3FF, rate=0x4000)
        return _vgm(writes, ROM)

    def test_chip_selection_and_flags(self):
        song = analyze(self._base())
        self.assertEqual(song.fm_chip, "ym2608")
        self.assertEqual(song.ym2610_ids, [])
        self.assertFalse(song.ym2608_ext)
        self.assertEqual(song.ym2608_ids, [0])

    def test_layout_matches_the_ym2610b_span(self):
        """FM, SSG, rhythm, delta-T - six voices where ADPCM-A sat."""
        song = analyze(self._base())

        def chs(pred):
            return sorted({e.ch for e in song.events if e.on and pred(e)})

        self.assertEqual(chs(lambda e: not e.pcm and e.ch < 6), [0])
        self.assertEqual(chs(lambda e: 6 <= e.ch < 9), [6])
        self.assertEqual(chs(lambda e: 9 <= e.ch < 15), [9])   # BD
        self.assertEqual(chs(lambda e: e.ch >= 15), [15])      # delta-T
        self.assertEqual(CHIP_CHANNELS[CHIP_YM2608], 16)

    def test_rhythm_note_carries_the_voice_level(self):
        song = analyze(self._base())
        hit = next(e for e in song.events if e.on and e.ch == 9)
        self.assertEqual(hit.vol, 26)          # the voice's own $18 field
        self.assertEqual(hit.pan, 0xFF)
        self.assertLessEqual(hit.vol, 31)      # the column's ceiling

    def test_global_level_is_emitted_as_an_1f_effect(self):
        writes = _fm_note(0) + [
            ChipWrite(80, "ym2608", 0, 0x11, 32),      # pull the kit down
            _rhythm_level(0, 26), _rhythm_key(400, 0x01),
        ]
        song = analyze(_vgm(writes))
        hit = next(e for e in song.events if e.on and e.ch == 9)
        self.assertEqual(hit.vol, 26)
        # The kit's global level stays a chip effect instead of being folded
        # into every hit (which clamped quiet kits to column 0).
        self.assertIn((80, 9, 0x1F, 32), song.channel_fx)

    def test_second_block_is_the_next_three_channels(self):
        song = analyze(_vgm(_fm_note(4) + _fm_note(0)))
        notes = sorted({e.ch for e in song.events if e.on})
        self.assertEqual(notes, [0, 4])

    def test_deltat_note_and_instrument(self):
        song = analyze(self._base())
        play = next(e for e in song.events if e.on and e.ch == 15)
        self.assertEqual(play.note, 60)                   # the map's first slot
        self.assertGreaterEqual(play.ins, len(song.fm_patches))
        ins = song.ym2608_b_samples
        self.assertEqual(len(ins), 1)

    def test_same_sample_global_levels_collapse_to_the_key_value(self):
        # The driver ramps $11 several times at the key-on sample; the chip
        # only holds the last byte. Placing the earlier values on following
        # rows attacked the drum too loud, then dropped it a row late.
        writes = [
            ChipWrite(400, "ym2608", 0, 0x11, 0x30),
            ChipWrite(400, "ym2608", 0, 0x11, 0x3C),
            ChipWrite(400, "ym2608", 0, 0x18, 0xCF),
            ChipWrite(400, "ym2608", 0, 0x10, 0x01),
        ]
        song = analyze(_vgm(writes))
        mod = parse_fur(write_fur(song))
        global_row = song.row_of(400)
        pat, row = divmod(global_row, mod.pat_len)
        cell = mod.patterns[(9, pat)][row]
        self.assertIn((0x1F, 0x3C), cell.fx or ())

    def test_deltat_register_upload_rebuilds_the_memory(self):
        # PMD-style logs upload the ADPCM-B image through $108 instead of
        # shipping a 0x81 data block; the block only declares the size.
        data = bytes((i * 11 + 5) & 0xFF for i in range(31))
        writes = [
            ChipWrite(0, "ym2608", 0, 0x100, 0x01),   # reset
            ChipWrite(0, "ym2608", 0, 0x101, 0x02),   # DRAM x8, shift 5
            ChipWrite(0, "ym2608", 0, 0x102, 0x04),   # start byte 0x80
            ChipWrite(0, "ym2608", 0, 0x103, 0x00),
            ChipWrite(0, "ym2608", 0, 0x104, 0x04),   # stop byte 0x9F
            ChipWrite(0, "ym2608", 0, 0x105, 0x00),
            ChipWrite(0, "ym2608", 0, 0x100, 0x60),   # external write mode
        ]
        writes += [
            ChipWrite(0, "ym2608", 0, 0x108, b) for b in data
        ]
        writes += [
            ChipWrite(100, "ym2608", 0, 0x109, 0x00),
            ChipWrite(100, "ym2608", 0, 0x10A, 0x40),
            ChipWrite(100, "ym2608", 0, 0x10B, 200),
            ChipWrite(100, "ym2608", 0, 0x101, 0xC0),
            ChipWrite(101, "ym2608", 0, 0x100, 0xA0),  # play external memory
        ]
        vgm = _vgm(writes)
        vgm.roms.append(RomSlice(0, 0x200, 0, b"", "ym2608_deltat"))
        mem = replay_deltat_memory(
            vgm, assemble_rom(vgm, "ym2608_deltat"),
        )
        self.assertEqual(mem[0x80:0x80 + len(data)], data)
        song = analyze(vgm)
        self.assertEqual(len(song.ym2608_b_samples), 1)
        self.assertEqual(
            song.ym2608_b_samples[0].data[:len(data)], data,
        )

    def test_mixed_log_warns_and_keeps_the_opna(self):
        writes = _fm_note(0) + [
            ChipWrite(100, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(150, "ym2612", 0, 0x2A, 0x40),
        ]
        song = analyze(_vgm(writes))
        self.assertEqual(song.fm_chip, "ym2608")
        self.assertTrue(any("YM2612" in w for w in song.warnings), song.warnings)

    def test_no_fm_keys_is_reported(self):
        writes = [ChipWrite(100, "ym2608", 0, 0x10, 0x01)]
        song = analyze(_vgm(writes))
        self.assertTrue(any("key-on" in w for w in song.warnings), song.warnings)

    def test_ssg_pitch_uses_the_opna_divider(self):
        """The OPNA's SSG divides by 32, where the YM2203 divides by 16.

        Get this wrong and every tone voice lands an octave off, because the
        period-to-note table is built from the divider.
        """
        from vgm2151fur.ay8910 import ay_note

        period = 0x1FF
        song = analyze(self._base())
        ev = next(e for e in song.events if e.on and 6 <= e.ch < 9)
        self.assertEqual((ev.note, ev.e5), ay_note(CLOCK, period, YM2608_SSG_DIVIDER))
        self.assertNotEqual(
            ay_note(CLOCK, period, YM2608_SSG_DIVIDER),
            ay_note(CLOCK, period, 16),
            "the test would not notice a YM2203 divider slipping in",
        )

    def test_rhythm_channels_get_their_releases(self):
        song = analyze(self._base())
        offs = {e.ch for e in song.events if not e.on and e.pcm}
        self.assertIn(9, offs)



class TestYm2608ExtChannel3(unittest.TestCase):
    """Special mode splits channel 3, and the second block moves along.

    Furnace's OPNA-Ext system names its columns FM1, FM2, four channel-3
    operators, then FM4-6 (sysDef.cpp:1383-1391) and hands `chan > 5` to the base
    platform three lower (ym2608ext.cpp:75-77). So a channel-3 key opens four
    columns at 2-5 and the block-B voices cannot stay at 3-5: that would put the
    bass on top of an operator.
    """

    def _writes(self):
        w = [ChipWrite(90, "ym2608", 0, 0x27, 0x40)]      # channel 3 special
        for op in range(4):                                 # four operators audible
            w.append(ChipWrite(95, "ym2608", 0, 0x30 | (op << 2) | 2, 0x01))
        for group in range(3):                              # the split frequencies
            w.append(ChipWrite(100, "ym2608", 0, 0xAC | group, (4 << 3) | 1))
            w.append(ChipWrite(100, "ym2608", 0, 0xA8 | group, 0x10 + group))
        w += [ChipWrite(100, "ym2608", 0, 0xA4 | 2, (5 << 3) | 2),
              ChipWrite(100, "ym2608", 0, 0xA0 | 2, 0x30),
              ChipWrite(100, "ym2608", 0, 0xB0 | 2, 0x07),
              ChipWrite(100, "ym2608", 0, 0xB4 | 2, 0xC0),
              ChipWrite(110, "ym2608", 0, 0x28, (0xF << 4) | 2)]
        w += _fm_note(3)                                    # a block-B voice
        return w

    def test_ext_is_detected(self):
        song = analyze(_vgm(self._writes()))
        self.assertTrue(song.ym2608_ext)

    def test_columns_and_second_block(self):
        song = analyze(_vgm(self._writes()))
        split = sorted(e.ch for e in song.events if e.on and 2 <= e.ch < 6)
        self.assertEqual(len(split), 4, "channel 3 must open four columns")
        b_notes = sorted(e.ch for e in song.events if e.on and e.ch >= 6)
        self.assertEqual(b_notes[:1], [6], "FM4 belongs to column 6, not 3")

    def test_pitch_matches_the_shared_opn_formula(self):
        """One formula for the family: same registers, same note as a YM2612."""
        song = analyze(_vgm(self._writes()))
        ev = next(e for e in song.events if e.on and e.ch == 6)
        want = ym2612_note_for(4, 0x400, CLOCK)
        self.assertEqual((ev.note, ev.e5), want)


class TestYm2608Timing(unittest.TestCase):
    def test_key_on_per_block_channel(self):
        from vgm2151fur.timing import _keyons_by_channel

        vgm = _vgm(_fm_note(0) + _fm_note(4))
        got = _keyons_by_channel(vgm, {"ym2608"})
        voices = sorted(k - 2000 for k in got)
        self.assertEqual(voices, [0, 4], "one onset lane per FM voice")

    def test_rhythm_keys_are_not_fm_onsets(self):
        from vgm2151fur.timing import _keyons_by_channel

        writes = _fm_note(0) + [ChipWrite(500, "ym2608", 0, 0x10, 0x3F)]
        got = _keyons_by_channel(_vgm(writes), {"ym2608"})
        self.assertEqual(sum(len(v) for v in got.values()), 1)


class TestYm2608Writer(unittest.TestCase):
    def _song(self, ext: bool = False):
        writes = self._notes(ext)
        return analyze(_vgm(writes, ROM))

    @staticmethod
    def _notes(ext: bool):
        writes = []
        if ext:
            writes.append(ChipWrite(90, "ym2608", 0, 0x27, 0x40))
            for op in range(4):
                writes.append(ChipWrite(95, "ym2608", 0,
                                        0x30 | (op << 2) | 2, 0x01))
            for group in range(3):
                writes.append(ChipWrite(100, "ym2608", 0, 0xAC | group,
                                        (4 << 3) | 1))
                writes.append(ChipWrite(100, "ym2608", 0, 0xA8 | group,
                                        0x10 + group))
            writes += [ChipWrite(100, "ym2608", 0, 0xA4 | 2, (5 << 3) | 2),
                       ChipWrite(100, "ym2608", 0, 0xA0 | 2, 0x30),
                       ChipWrite(100, "ym2608", 0, 0xB0 | 2, 0x07),
                       ChipWrite(100, "ym2608", 0, 0xB4 | 2, 0xC0),
                       ChipWrite(110, "ym2608", 0, 0x28, (0xF << 4) | 2)]
        writes += _fm_note(1 if ext else 0, off=7900)
        writes += _ssg_voice(0, t=300)
        writes += [ChipWrite(90, "ym2608", 0, 0x11, 0x3F),
                   _rhythm_level(0, 26), _rhythm_key(400, 0x11),
                   # Hi-hat again later, so two voices and two rows exist.
                   _rhythm_level(3, 12), _rhythm_key(900, 0x08)]
        writes += _delta_t_play(1200, start=0x260, end=0x3FF, rate=0x4000)
        return writes

    def test_chip_id_and_span(self):
        sk = parse_fur_skeleton(write_fur(self._song()))
        self.assertEqual(sk["chips"], [CHIP_YM2608])

    def test_ext_chip_id_and_span(self):
        sk = parse_fur_skeleton(write_fur(self._song(ext=True)))
        self.assertEqual(sk["chips"], [CHIP_YM2608_EXT])

    def test_clock_travels_as_furnaces_own_selection(self):
        """7987200 is one of Furnace's named OPNA clocks, not a custom one."""
        data = write_fur(self._song())
        self.assertIn(b"clockSel=1", data)
        vgm = _vgm(self._notes(False), bytes(0x800))
        vgm.ym2608_clock = 8_000_000
        odd = _vgm(self._notes(False), ROM)
        odd.ym2608_clock = 7_920_000
        self.assertIn(b"customClock=7920000", write_fur(analyze(odd)))
        self.assertNotIn(b"customClock", write_fur(analyze(vgm)))

    def test_instrument_types_and_the_empty_kit_map(self):
        data = write_fur(self._song())
        types = [int.from_bytes(payload[2:4], "little")
                 for _off, mag, _size, payload in iter_blocks(data)
                 if mag == b"INS2"]
        self.assertIn(INS_FM, types)
        self.assertIn(INS_ADPCMB, types)
        # One rhythm instrument, and it maps nothing: the column is the drum.
        self.assertEqual(types.count(INS_ADPCMA), 1)

    def test_deltat_sample_lands_in_the_opnas_one_bank(self):
        data = write_fur(self._song())
        blocks = [parse_smp2_payload(payload)
                  for _off, mag, _size, payload in iter_blocks(data)
                  if mag == b"SMP2"]
        self.assertTrue(blocks, "no delta-T sample written")
        for smp in blocks:
            self.assertEqual(smp["depth"], 6)          # DIV_SAMPLE_DEPTH_ADPCM_B
            masks = [int.from_bytes(smp["presence"][i * 4:(i + 1) * 4], "little")
                     for i in range(4)]
            self.assertTrue(masks[0] & 1, masks)       # bank 0, system 0
            self.assertFalse(any(masks[1:]), masks)

    def test_deltat_body_is_padded_to_the_stored_quantum(self):
        data = write_fur(self._song())
        smp = next(parse_smp2_payload(payload)
                   for _off, mag, _size, payload in iter_blocks(data)
                   if mag == b"SMP2")
        self.assertFalse((smp["n_frames"] + 1) // 2 % 0x100,
                         "a short body makes Furnace read the next sample")

    def test_channel_names_follow_furnace(self):
        data = write_fur(self._song())
        for label in (b"Kick", b"Snare", b"Top", b"HiHat", b"Tom", b"Rim"):
            self.assertIn(label, data)
        ext = write_fur(self._song(ext=True))
        self.assertIn(b"FM 3 OP1", ext)

    def test_report_names_the_chip_and_its_parts(self):
        text = format_report(self._song(ext=True))
        self.assertIn("YM2608 Ext x1 @ 7.9872 MHz", text)
        self.assertIn("SSG notes", text)


@unittest.skipUnless(HAVE_FURNACE, "Furnace console build missing")
class TestYm2608RoundTrip(unittest.TestCase):
    """What the emulator drives after a trip through Furnace, register by register."""

    def _round(self, name: str, ext: bool = False):
        writes = TestYm2608Writer._notes(ext)
        vgm = _vgm(writes, ROM)
        # Pin the grid: at the measured rate a synthetic log this short gets one
        # or two rows, and two kit hits would share a cell legitimately. speed=1
        # puts them far enough apart that an ordering claim means something.
        song = analyze(vgm, speed=1)
        fur = TMP / f"{name}.fur"
        fur.write_bytes(write_fur(song, compress=False))
        out = TMP / f"{name}.vgm"
        export_vgm(fur, out)
        return vgm, load_vgm(out)

    def test_rhythm_voices_keep_their_own_levels(self):
        """Each kit column comes back at the level the driver set.

        Two rows of hits can quantize onto one tick, so this compares what each
        voice was given rather than the order the key byte names them. The
        unwritten Tom is the interesting one: our state must default it the way
        ymfm's reset does - full level, panned both - and Furnace then writes
        $1C=$DF, which is what the test asserts came back.
        """

        def level_per_voice(vgm):
            levels: dict[str, set[int]] = {}
            held = [0x1F] * 6          # ymfm_adpcm.cpp:48-51
            for w in vgm.writes:
                if w.chip != "ym2608" or (w.reg & 0x100):
                    continue
                r = w.reg & 0xFF
                if 0x18 <= r <= 0x1D:
                    held[r - 0x18] = w.val & 0x1F
                if r == 0x10 and not (w.val & 0x80):
                    for i in range(6):
                        if w.val & (1 << i):
                            levels.setdefault(RHYTHM_VOICES[i][0], set()).add(
                                held[i])
            return levels

        src, exp = self._round("rt_rhythm")
        a, b = level_per_voice(src), level_per_voice(exp)
        self.assertEqual(set(a), {"BD", "HH", "TM"})
        self.assertTrue(b, "Furnace wrote no rhythm keys")
        for voice in a:
            self.assertEqual(a[voice], b.get(voice), voice)

    def test_deltat_registers_reach_the_same_span_and_step(self):
        src, exp = self._round("rt_delta")

        def plays(vgm):
            reg: dict[int, int] = {}
            out = []
            for w in vgm.writes:
                if w.chip != "ym2608" or not (w.reg & 0x100):
                    continue
                r = w.reg & 0xFF
                if r > 0x0B:
                    continue
                reg[r] = w.val
                if r != 0x00 or not (w.val & 0x80) or (w.val & 1):
                    continue
                start = ((reg.get(0x03, 0) << 8 | reg.get(0x02, 0)) << 5)
                end = ((reg.get(0x05, 0) << 8 | reg.get(0x04, 0)) << 5) | 31
                out.append((end - start + 1, reg.get(0x0A, 0) << 8 | reg.get(9, 0)))
            return out

        a, b = plays(src), plays(exp)
        self.assertTrue(b, "Furnace wrote no delta-T starts")
        # The source asked for 416 bytes; Furnace plays its own 256-byte quantum,
        # so the module must have declared exactly that much - not 416 (which
        # would run into the next sample) and not more.
        want = len(deltat_store_body(ROM[0x260:0x400]))
        self.assertEqual(b[0][0], want)
        self.assertLessEqual(abs(b[0][1] - a[0][1]), 8)

    def test_audit_fields_are_clean(self):
        from vgm2151fur.audit import compare_ym2608_samples

        src, exp = self._round("rt_audit", ext=True)
        rep = compare_ym2608_samples(src, exp)
        self.assertTrue(rep, "the audit saw no OPNA in the log")
        for kind in ("rhythm", "delta_t"):
            d = rep[kind]
            self.assertEqual(d["missed"], 0, (kind, d))
            for field in ("data", "length", "vol", "pan", "rate"):
                self.assertEqual(d[field], 0, (kind, field, d))

    def test_fm_pitch_survives_the_split(self):
        from vgm2151fur.diag import pitch_runs

        src, exp = self._round("rt_pitch", ext=True)
        for ch in (0, 1, 2, 3, 4, 5):
            runs = pitch_runs(src, exp, ch, threshold_st=0.25, min_run_ms=60)
            self.assertEqual(runs, [], f"ch{ch}: {runs[:2]}")




if __name__ == "__main__":
    unittest.main()
