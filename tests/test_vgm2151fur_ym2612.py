"""YM2612 (OPN2) support: parsing, pitch mapping, analysis and .fur emission."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vgm2151fur.analyze import (
    analyze, furnace_note_name,
)
from vgm2151fur.diag import (
    pitch_runs, ym2612_keyed_intervals, ym2612_pitch_stream,
)
from vgm2151fur.furio import (
    CHIP_CHANNELS, decompress_fur, iter_blocks, parse_fur,
)
from vgm2151fur.furwrite import (
    CHIP_YM2612,
    CHIP_YM2612_EXT,
    INS_FM,
    _compact_sample_map_notes,
    _shift_sample_map_instruments,
    write_fur,
)
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.vgm import (
    ChipWrite, VgmFile, _decompress_data_block, _parse_compression_table,
    chips_in_head, load_vgm,
)
from vgm2151fur.sn76489 import (
    collect_sn76489, psg_note, psg_pitch_table,
)
from vgm2151fur.ym2612 import (
    YM2612_FREQ_BASE,
    YM2612State,
    ym2612_note_for,
    ym2612_note_fnum,
    ym2612_pitch_units,
)
from vgm2151fur.ym2612dac import (
    DAC_RELEASE_SAMPLES,
    DAC_TARGET_SAMPLES,
    DacPlay,
    DacSample,
    _cluster_similar_samples,
    _collect_source,
    _merge_one_shot_samples,
    _split_long_silence,
    _split_silent_runs,
    collect_dac,
    group_dac_plays,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_ym2612"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
CLOCK = 7670453


def _vgm(writes, *, total_samples=400000, clock=CLOCK, **kw):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=total_samples, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        roms=[], writes=sorted(writes, key=lambda w: w.sample),
        ym2612_clock=clock, **kw,
    )


def _raw_vgm(body: bytes, clock: int = CLOCK, dual: bool = False,
             sn_clock: int = 3579545) -> bytes:
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0xC:0x10] = (sn_clock & 0x3FFFFFFF).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    raw = (clock & 0x3FFFFFFF) | (0x40000000 if dual else 0)
    buf[0x2C:0x30] = raw.to_bytes(4, "little")
    out = bytes(buf) + body + b"\x66"
    return out[:4] + (len(out) - 4).to_bytes(4, "little") + out[8:]


def _patch(t: int = 100, cid: int = 0, bank: int = 0, ch: int = 0,
           sr=(0, 0, 0, 0), ssg=(0, 0, 0, 0), dt=(0, 0, 0, 0)):
    out: list[ChipWrite] = []
    for page in (0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
        for op in range(4):
            if page == 0x30:
                val = 0x01 | ((dt[op] & 7) << 4)
            elif page == 0x50:
                val = 0x1F
            elif page == 0x70:
                val = sr[op]
            elif page == 0x90:
                val = ssg[op]
            else:
                val = 0x00
            out.append(ChipWrite(
                t, "ym2612", cid, (0x100 if bank else 0) | page + op * 4 + ch,
                val,
            ))
    out.append(ChipWrite(t, "ym2612", cid, (0x100 if bank else 0) | 0xB0 + ch, 0x07))
    out.append(ChipWrite(t, "ym2612", cid, (0x100 if bank else 0) | 0xB4 + ch, 0xC0))
    return out


def _fm_note(block: int, fnum: int, *, t: int = 100, off: int = 60000,
             cid: int = 0, ch: int = 0, patch_t: int | None = None,
             sr=(0, 0, 0, 0), ssg=(0, 0, 0, 0), dt=(0, 0, 0, 0)):
    bank = 1 if ch >= 3 else 0
    sub = ch - 3 if ch >= 3 else ch
    kon = ch if ch < 3 else ch + 1
    out = _patch(t if patch_t is None else patch_t, cid, bank, sub,
                 sr=sr, ssg=ssg, dt=dt)
    out += [
        ChipWrite(t, "ym2612", cid,
                  (0x100 if bank else 0) | 0xA4 + sub,
                  (block << 3) | ((fnum >> 8) & 7)),
        ChipWrite(t, "ym2612", cid,
                  (0x100 if bank else 0) | 0xA0 + sub, fnum & 0xFF),
        ChipWrite(t, "ym2612", cid, 0x28, 0xF0 | kon),
    ]
    if off:
        out.append(ChipWrite(off, "ym2612", cid, 0x28, kon))
    return out


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


class TestYm2612Parsing(unittest.TestCase):
    def test_header_and_banks(self):
        body = bytes([
            0x52, 0x30, 0x01,    # bank 0, channel 0 operator
            0x53, 0x30, 0x02,    # bank 1, channel 4 operator
            0x52, 0xA0, 0x03,    # channel 0 fnum low
            0xA2, 0x40, 0x04,    # second chip
            0xA3, 0x40, 0x05,    # second chip, bank 1
        ])
        p = TMP / "ym2612_dual.vgm"
        p.write_bytes(_raw_vgm(body, dual=True))
        vgm = load_vgm(p)
        self.assertEqual(vgm.ym2612_clock, CLOCK)
        self.assertTrue(vgm.ym2612_dual)
        got = [(w.chip, w.chip_id, w.reg, w.val) for w in vgm.writes]
        self.assertEqual(got, [
            ("ym2612", 0, 0x030, 0x01),
            ("ym2612", 0, 0x130, 0x02),
            ("ym2612", 0, 0x0A0, 0x03),
            ("ym2612", 1, 0x040, 0x04),
            ("ym2612", 1, 0x140, 0x05),
        ])

    def test_clock_is_masked(self):
        p = TMP / "ym2612_clock.vgm"
        p.write_bytes(_raw_vgm(b"\x52\x30\x01", dual=True))
        self.assertEqual(load_vgm(p).ym2612_clock, CLOCK)

    def test_chips_in_head(self):
        self.assertIn("ym2612", chips_in_head(_raw_vgm(b"\x66")))
        kinds = chips_in_head(_raw_vgm(b"\x66", dual=True))
        self.assertIn("ym2612", kinds)
        self.assertIn("ym2612_dual", kinds)
        # The YM2151 lives at 0x30; a YM2612-only header must not claim it.
        self.assertNotIn("ym2151", kinds)

    def test_compressed_dac_block_uses_the_7f_table(self):
        # Type 0x40 is the compressed form of the YM2612 PCM bank. Some
        # rips compress it with 4-bit Delta-PCM and a 0x7F table.
        table_payload = (
            bytes((0x01, 0x00, 0x08, 0x04))
            + (4).to_bytes(2, "little")
            + bytes((1, 2, 0xFF, 0))
        )
        table = _parse_compression_table(table_payload)
        self.assertEqual(table, (1, 0, 8, 4, [1, 2, 255, 0]))
        # four 8-bit outputs from 0x80, deltas +1, +2, -1, 0
        block = (
            bytes((0x01, 0x04, 0x00, 0x00, 0x00, 0x08, 0x04, 0x00,
                   0x80, 0x00))
            + bytes((0x01, 0x23))
        )
        self.assertEqual(
            _decompress_data_block(0x40, block, table),
            bytes((0x81, 0x83, 0x82, 0x82)),
        )

    def test_stray_second_chip_is_first_with_warning(self):
        p = TMP / "ym2612_stray.vgm"
        p.write_bytes(_raw_vgm(bytes([0xA2, 0x40, 0x02])))
        vgm = load_vgm(p)
        self.assertFalse(vgm.ym2612_dual)
        self.assertEqual([(w.chip, w.chip_id) for w in vgm.writes], [("ym2612", 0)])
        self.assertTrue(any("without the dual-instance" in w for w in vgm.warnings))

    def test_missing_clock_warning(self):
        p = TMP / "ym2612_noclock.vgm"
        p.write_bytes(_raw_vgm(bytes([0x52, 0x30, 0x01]), clock=0))
        vgm = load_vgm(p)
        self.assertEqual(vgm.ym2612_clock, 0)
        self.assertTrue(any("YM2612 clock" in w for w in vgm.warnings))

    def test_stream_end_wins_over_header_length(self):
        body = bytes([0x61, 0x10, 0x27, 0x52, 0x30, 0x01])  # wait 10000
        p = TMP / "ym2612_shortstream.vgm"
        p.write_bytes(_raw_vgm(body))
        vgm = load_vgm(p)
        self.assertEqual(vgm.total_samples, 10000)
        self.assertTrue(any("stream ends at 10000" in w for w in vgm.warnings))


class TestGenesisPsgDac(unittest.TestCase):
    def test_sn76489_and_dac_commands_parse(self):
        bank = bytes([0x80, 0x90, 0xA0, 0x80])
        body = bytes([
            0x50, 0x96,            # SN76489: ch0 volume 6
            0x67, 0x66, 0x00, 4, 0, 0, 0, *bank,
            0xE0, 0, 0, 0, 0,      # DAC pointer 0
            0x80,                  # play one byte
            0x50, 0x8F,            # SN76489: ch0 tone low
        ])
        p = TMP / "genesis_psg_dac.vgm"
        p.write_bytes(_raw_vgm(body))
        vgm = load_vgm(p)
        self.assertEqual(vgm.sn76489_clock, 3579545)
        self.assertIn("sn76489", chips_in_head(p.read_bytes()))
        self.assertEqual([w.val for w in vgm.writes if w.chip == "sn76489"],
                         [0x96, 0x8F])
        self.assertEqual(bytes(vgm.dac_data), bank)
        self.assertEqual(vgm.dac_events[0], (0, 0, 0))
        self.assertEqual([e for e in vgm.dac_events if e[1] == 1], [(0, 1, 0)])

    def test_game_gear_stereo_command_parses(self):
        body = bytes([0x4F, 0xBB, 0x50, 0x96])
        p = TMP / "gamegear_stereo.vgm"
        p.write_bytes(_raw_vgm(body))
        vgm = load_vgm(p)
        self.assertTrue(vgm.sn76489_stereo)
        self.assertEqual(
            [(w.reg, w.val) for w in vgm.writes if w.chip == "sn76489"],
            [(1, 0xBB), (0, 0x96)],
        )

    def test_dense_dac_is_signed_once_and_uses_split_pan(self):
        bank = bytes([0x80, 0x90]) + bytes(
            (0x80 + (i % 17) - 8) & 0xFF for i in range(298)
        )
        events = [(0, 0, 0)]
        events += [(i * 2, 1, 4) for i in range(300)]
        for raw, expected in ((0xC0, 0xFF), (0x80, 0xF0),
                              (0x40, 0x0F), (0x00, 0x00)):
            writes = [
                ChipWrite(0, "ym2612", 0, 0x22, 0),
                ChipWrite(0, "ym2612", 0, 0x1B6, raw),
            ]
            src = _vgm(writes, dac_data=bytearray(bank), dac_events=events)
            samples, plays, warn, _stats = collect_dac(src, slices=True)
            self.assertFalse(warn)
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0].data[0], 0x00)
            self.assertEqual(samples[0].data[1], 0x10)
            self.assertEqual(plays[0].pan, expected)

    def test_slow_glide_stays_one_note(self):
        # Each period step is below a semitone, but the glide as a whole
        # passes one. Comparing against the note's original period split it
        # into a new note every semitone; compare with the current pitch.
        ws = [ChipWrite(0, "sn76489", 0, 0, 0x96)]  # ch0 attenuation 6
        t = 100
        for period in (200, 202, 204, 206, 208, 210, 212, 214):
            ws.append(ChipWrite(t, "sn76489", 0, 0, 0x80 | (period & 0x0F)))
            ws.append(ChipWrite(t, "sn76489", 0, 0, (period >> 4) & 0x3F))
            t += 100
        ws.append(ChipWrite(t, "sn76489", 0, 0, 0x9F))
        hits, _offs, _warn = collect_sn76489(
            _vgm(ws, sn76489_clock=3579545),
        )
        self.assertEqual(len(hits), 1)
        self.assertGreaterEqual(len(hits[0].pitch_pts), 6)

    def test_collect_psg_tone_noise_and_pitch(self):
        ws = [
            ChipWrite(0, "ym2612", 0, 0x22, 0),
            # tone ch0: period 31, attenuation 6
            ChipWrite(100, "sn76489", 0, 0, 0x8F),
            ChipWrite(100, "sn76489", 0, 0, 0x01),
            ChipWrite(100, "sn76489", 0, 0, 0x96),
            ChipWrite(200, "sn76489", 0, 0, 0x80),
            ChipWrite(200, "sn76489", 0, 0, 0x02),
            ChipWrite(400, "sn76489", 0, 0, 0x9F),
            # noise: mode 7, attenuation 6
            ChipWrite(500, "sn76489", 0, 0, 0xE7),
            ChipWrite(500, "sn76489", 0, 0, 0xF6),
            ChipWrite(800, "sn76489", 0, 0, 0xFF),
        ]
        hits, offs, warn = collect_sn76489(_vgm(ws, sn76489_clock=3579545))
        self.assertFalse(warn)
        self.assertEqual(len(hits), 2)
        tone = next(h for h in hits if not h.noise)
        self.assertEqual((tone.period, tone.vol, tone.voice), (31, 9, 0))
        self.assertEqual(tone.pitch_pts, [(200, 32)])
        self.assertEqual(offs[0].sample_time, 400)
        noise = next(h for h in hits if h.noise)
        self.assertEqual((noise.vol, noise.noise_mode), (9, 7))

    def test_psg_period_zero_is_a_gate_off(self):
        # A driver silences a tone voice by writing period 0, then its volume
        # mute. The period write must end the note; otherwise the pattern has
        # no note-off and the low note sustains into the next one.
        ws = [
            ChipWrite(0, "ym2612", 0, 0x22, 0),
            ChipWrite(100, "sn76489", 0, 0, 0xCF),  # ch2 tone period 1023
            ChipWrite(100, "sn76489", 0, 0, 0x3F),
            ChipWrite(100, "sn76489", 0, 0, 0xD0),  # ch2 volume 0
            ChipWrite(500, "sn76489", 0, 0, 0xC0),  # period 0: gate off
            ChipWrite(500, "sn76489", 0, 0, 0x00),
            ChipWrite(500, "sn76489", 0, 0, 0xDF),
        ]
        hits, offs, _warn = collect_sn76489(_vgm(ws, sn76489_clock=3579545))
        tone = next(h for h in hits if h.voice == 2)
        self.assertEqual((tone.period, tone.off_sample), (1023, 500))
        self.assertIn((500, 2), [(o.sample_time, o.voice) for o in offs])

    def test_psg_noise_mode_3_tracks_tone_channel_3(self):
        # Noise mode 3 borrows tone generator 3's period. The noise note must
        # follow that register, or Furnace writes a fixed low period and the
        # channel goes effectively silent.
        ws = [
            ChipWrite(0, "ym2612", 0, 0x22, 0),
            ChipWrite(0, "sn76489", 0, 0, 0xCE),  # ch2 tone period 14
            ChipWrite(0, "sn76489", 0, 0, 0x00),
            ChipWrite(100, "sn76489", 0, 0, 0xE7),  # mode 3 + white
            ChipWrite(100, "sn76489", 0, 0, 0xF0),
            ChipWrite(400, "sn76489", 0, 0, 0xCE),  # ch2 period 30
            ChipWrite(400, "sn76489", 0, 0, 0x01),
            ChipWrite(800, "sn76489", 0, 0, 0xFF),
        ]
        hits, _offs, _warn = collect_sn76489(_vgm(ws, sn76489_clock=3579545))
        noise = next(h for h in hits if h.noise)
        self.assertEqual((noise.period, noise.noise_mode), (14, 7))
        self.assertEqual(noise.pitch_pts, [(400, 30)])

    def test_mid_note_noise_rate_change_is_a_new_note(self):
        # Modes 4 and 6 are both white, so 20xy does not change. Furnace
        # still takes the fixed rate from the note, so a rate write inside a
        # gate has to become a new note or the old rate keeps playing.
        ws = [
            ChipWrite(0, "sn76489", 0, 0, 0xE4),
            ChipWrite(0, "sn76489", 0, 0, 0xF0),
            ChipWrite(500, "sn76489", 0, 0, 0xE6),
            ChipWrite(900, "sn76489", 0, 0, 0xFF),
        ]
        hits, _offs, _warn = collect_sn76489(
            _vgm(ws, sn76489_clock=3579545),
        )
        noise = [h for h in hits if h.noise]
        self.assertEqual([(h.sample_time, h.noise_mode) for h in noise],
                         [(0, 4), (500, 6)])

    def test_same_period_rewrite_with_a_volume_write_retriggers(self):
        # Some GG drivers retrigger a held tone by writing the same period
        # again and then the new volume on one sample. Without the volume
        # pair a period refresh is housekeeping; with it the driver means a
        # new note, and the pattern must show the note-on.
        ws = [
            ChipWrite(0, "sn76489", 0, 0, 0x84),
            ChipWrite(0, "sn76489", 0, 0, 0x06),  # period 100
            ChipWrite(0, "sn76489", 0, 0, 0x90),
            ChipWrite(300, "sn76489", 0, 0, 0x84),  # refresh only
            ChipWrite(300, "sn76489", 0, 0, 0x06),
            ChipWrite(600, "sn76489", 0, 0, 0x84),  # retrigger
            ChipWrite(600, "sn76489", 0, 0, 0x06),
            ChipWrite(600, "sn76489", 0, 0, 0x97),
            ChipWrite(900, "sn76489", 0, 0, 0x9F),
        ]
        hits, _offs, _warn = collect_sn76489(
            _vgm(ws, sn76489_clock=3579540),
        )
        tone = [h for h in hits if h.voice == 0]
        self.assertEqual([(h.sample_time, h.off_sample, h.vol) for h in tone],
                         [(0, 600, 15), (600, 900, 8)])

    def test_rate_change_and_mute_on_one_sample_still_emits_off(self):
        # A Game Gear driver changes the noise rate and mutes the voice on the
        # same sample. The rate split opens a zero-length note, which is
        # discarded, but the note it replaced must still get its key-off or
        # the periodic noise rings until the next key-on.
        ws = [
            ChipWrite(0, "sn76489", 0, 0, 0xE0),
            ChipWrite(0, "sn76489", 0, 0, 0xF0),
            ChipWrite(500, "sn76489", 0, 0, 0xE1),
            ChipWrite(500, "sn76489", 0, 0, 0xFF),
        ]
        hits, offs, _warn = collect_sn76489(
            _vgm(ws, sn76489_clock=3579540),
        )
        noise = [h for h in hits if h.noise]
        self.assertEqual([(h.sample_time, h.off_sample) for h in noise],
                         [(0, 500)])
        self.assertIn((500, 3), [(o.sample_time, o.voice) for o in offs])

    def test_large_channel3_noise_jump_is_a_legato_note(self):
        # A period jump beyond what E8xx can carry on one row must land at
        # once. The source writes the register in one go; splitting the note
        # and marking it legato keeps the new period exact without
        # restarting the noise generator.
        ws = [
            ChipWrite(0, "sn76489", 0, 0, 0xCE),  # ch2 tone period 14
            ChipWrite(0, "sn76489", 0, 0, 0x00),
            ChipWrite(0, "sn76489", 0, 0, 0xE7),  # mode 3 + white
            ChipWrite(0, "sn76489", 0, 0, 0xF0),
            ChipWrite(500, "sn76489", 0, 0, 0xC0),  # ch2 period 0x200
            ChipWrite(500, "sn76489", 0, 0, 0x20),
            ChipWrite(900, "sn76489", 0, 0, 0xFF),
        ]
        hits, _offs, _warn = collect_sn76489(
            _vgm(ws, sn76489_clock=3579545),
        )
        noise = [h for h in hits if h.noise]
        self.assertEqual(len(noise), 2)
        self.assertEqual((noise[0].period, noise[0].legato), (14, False))
        self.assertEqual((noise[1].period, noise[1].legato), (0x200, True))

    def test_psg_system_disables_easy_noise(self):
        # Furnace's easy-noise shortcut clamps channel-3 noise periods below
        # 16 to 0. The converter writes real periods, so the flag must say so.
        ws = [
            ChipWrite(0, "sn76489", 0, 0, 0x8F),
            ChipWrite(0, "sn76489", 0, 0, 0x01),
            ChipWrite(0, "sn76489", 0, 0, 0x90),
        ]
        song = analyze(_vgm(ws, clock=0, sn76489_clock=3579545))
        self.assertIn(b"noEasyNoise=true", write_fur(song))

    def test_psg_rows_snap_to_the_write_lattice(self):
        # A PSG-only driver writes one frame at a time. The row grid follows
        # that interval, so a volume step and the next note never land on the
        # same row and a column step cannot be pulled back to the row start.
        step = 1470
        writes = []
        for i in range(64):
            t = i * step
            per = 200 + (i % 4) * 40
            writes += [
                ChipWrite(t, "sn76489", 0, 0, 0x80 | (per & 0x0F)),
                ChipWrite(t, "sn76489", 0, 0, per >> 4),
                ChipWrite(t, "sn76489", 0, 0, 0x90),
            ]
        writes.append(ChipWrite(64 * step, "sn76489", 0, 0, 0x9F))
        song = analyze(_vgm(
            writes, clock=0, total_samples=64 * step,
            sn76489_clock=3579545,
        ))
        per_write = step / song.samples_per_row
        self.assertAlmostEqual(per_write, round(per_write), delta=1e-6)
        self.assertGreaterEqual(round(per_write), 1)

    def test_psg_note_is_the_same_period(self):
        tbl = psg_pitch_table()
        for period in (2, 14, 31, 100, 511, 1023):
            note, e5 = psg_note(period)
            self.assertEqual(
                tbl.value(note, e5 - 0x80), period,
                (period, note, e5),
            )

    @unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
    def test_psg_module_round_trip(self):
        ws = [
            ChipWrite(0, "ym2612", 0, 0x22, 0),
            ChipWrite(100, "sn76489", 0, 0, 0x8F),
            ChipWrite(100, "sn76489", 0, 0, 0x01),
            ChipWrite(100, "sn76489", 0, 0, 0x96),
            ChipWrite(400, "sn76489", 0, 0, 0x9F),
        ]
        song = analyze(_vgm(ws, sn76489_clock=3579545))
        self.assertEqual(song.psg_count, 1)
        fur = TMP / "genesis_psg.fur"
        fur.write_bytes(write_fur(song, include_pcm=False))
        m = parse_fur(fur.read_bytes())
        self.assertEqual(m.n_ch, 10)
        self.assertIn("0x3", [hex(c) for c in m.chips])
        vgm = TMP / "genesis_psg.vgm"
        export_vgm(fur, vgm)
        exp = load_vgm(vgm)
        hits, _offs, _warn = collect_sn76489(exp)
        self.assertTrue(any(h.period == 31 and h.vol == 9 for h in hits))

    @unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
    def test_dac_stream_module_round_trip(self):
        bank = bytes((0x80 + (i % 31) - 15) & 0xFF for i in range(300))
        events = [(0, 0, 0)]
        events += [(i * 4, 1, 4) for i in range(300)]
        ws = [ChipWrite(0, "ym2612", 0, 0x22, 0)]
        src = _vgm(
            ws, dac_data=bytearray(bank), dac_events=events,
        )
        samples, plays, warn, _stats = collect_dac(src, slices=True)
        self.assertFalse(warn)
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 1)
        self.assertEqual(samples[0].c4_rate, 11025)
        song = analyze(src)
        self.assertEqual(song.dac_count, 1)
        fur = TMP / "genesis_dac.fur"
        fur.write_bytes(write_fur(song, include_pcm=True))
        m = parse_fur(fur.read_bytes())
        self.assertEqual(len(m.samples), 1)
        vgm = TMP / "genesis_dac.vgm"
        export_vgm(fur, vgm)
        exp = load_vgm(vgm)
        self.assertEqual(len(exp.dac_blocks), 1)
        self.assertTrue(exp.dac_block_plays)
        self.assertEqual(exp.dac_block_plays[0][2], 11025)
        self.assertEqual(
            bytes((b - 0x80) & 0xFF for b in exp.dac_blocks[0]),
            samples[0].data,
        )

    def test_psg_uses_the_sn_instrument_after_a_dac_and_pitch_copy(self):
        # The pitch-copy base recompute must keep the DAC maps.
        ws = _fm_note(4, 1204, off=60000)
        ws += [
            ChipWrite(30000, "ym2612", 0, 0xA4, (4 << 3) | ((1250 >> 8) & 7)),
            ChipWrite(30000, "ym2612", 0, 0xA0, 1250 & 0xFF),
        ]
        ws += [
            ChipWrite(100, "sn76489", 0, 0, 0xE7),  # white noise, mode 7
            ChipWrite(100, "sn76489", 0, 0, 0xF6),  # noise volume 6
        ]
        src = _vgm(
            ws,
            dac_events=[(0, 2, 0x80), (2, 2, 0x90), (4, 2, 0x70)],
            sn76489_clock=3579545,
            total_samples=60000,
        )
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 1)
        self.assertEqual(song.psg_count, 1)
        fur = TMP / "genesis_psg_instrument.fur"
        fur.write_bytes(write_fur(song, include_pcm=True))
        m = parse_fur(fur.read_bytes())
        psg_notes = [r for r in m.patterns.get((9, 0), []) if r.note >= 0]
        self.assertTrue(psg_notes)
        types = _ins_types(decompress_fur(fur.read_bytes()))
        self.assertEqual(types[psg_notes[0].ins], 0)  # DIV_INS_STD ("SN")


class TestYm2612DacSlices(unittest.TestCase):
    """The sliced FM6 DAC path: source bytes, holds, maps and timing."""

    @staticmethod
    def _runs(spec):
        """Build (bank, dac_events) for [(onset, bytes, wait), ...]."""
        bank = bytearray()
        events: list[tuple[int, int, int]] = []
        for t, data, wait in spec:
            off = len(bank)
            bank.extend(data)
            events.append((t, 0, off))
            for i in range(len(data)):
                events.append((t + i * wait, 1, wait))
        return bank, events

    @staticmethod
    def _signed(data: bytes) -> bytes:
        return bytes((x - 0x80) & 0xFF for x in data)

    @staticmethod
    def _marker():
        """One YM2612 write marks the chip present for `analyze`."""
        return [ChipWrite(0, "ym2612", 0, 0x22, 0)]

    def test_same_time_burst_keeps_the_final_value(self):
        events = [(0, 2, 0x90), (0, 2, 0xA0), (0, 2, 0xB0), (100, 2, 0x80)]
        src = _vgm(
            self._marker(), dac_events=events, total_samples=1000,
        )
        samples, _plays, warn, stats = collect_dac(src, slices=True)
        self.assertEqual(stats["dac_same_time_bytes"], 3)
        self.assertEqual(stats["dac_same_time_dropped"], 2)
        self.assertTrue(any("share one VGM sample" in w for w in warn))
        final = next(s for s in samples if s.data == b"\x30")
        self.assertEqual(final.c4_rate, 44100)
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 1)
        self.assertEqual(song.dac_samples[0].data[0], 0x30)
        self.assertEqual(song.dac_samples[0].data[-1], 0x00)

    def test_long_phrase_chunks_keep_the_source_rate(self):
        # A phrase past 0.25 s is split into several sample-map chunks. Each
        # chunk used to take the phrase's trailing zero as its own tail, so
        # every chunk stretched to the phrase end and the overlapping group
        # doubled its playback rate (44.1 kHz became 88.2 kHz).
        # Vary the bytes: a constant level is a flat DC line and the collector
        # now drops it before it reaches the sample map.
        events = [(i, 2, 0x40 + (i % 7)) for i in range(60000)]
        events += [(70000 + i, 2, 0x20 + (i % 5)) for i in range(10000)]
        src = _vgm(
            self._marker() + [ChipWrite(0, "ym2612", 0, 0x2B, 0x80)],
            dac_events=events, total_samples=90000,
        )
        samples, plays, _warn, _stats = collect_dac(src)
        self.assertEqual({s.c4_rate for s in samples}, {44100})
        ordered = sorted(plays, key=lambda p: p.onset)
        for a, b in zip(ordered, ordered[1:]):
            self.assertLessEqual(a.onset + a.span, b.onset + 1, (a, b))

    def test_timeline_fallback_is_explicit(self):
        bank = bytes((0x80 + (i % 31) - 15) & 0xFF for i in range(300))
        events = [(0, 0, 0)]
        events += [(i * 4, 1, 4) for i in range(300)]
        src = _vgm(
            self._marker(), dac_data=bytearray(bank), dac_events=events,
            total_samples=400000,
        )
        samples, plays, _warn, stats = collect_dac(src, timeline=True)
        self.assertEqual(stats.get("dac_timeline"), 1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].c4_rate, 22050)
        self.assertEqual(len(plays), 1)

    def test_direct_2a_stream_is_one_slice(self):
        values = [0x80, 0x90, 0x70, 0x60]
        events = [(i * 2, 2, v) for i, v in enumerate(values)]
        src = _vgm(self._marker(), dac_events=events, total_samples=1000)
        samples, plays, warn, _stats = collect_dac(src, slices=True)
        self.assertFalse(warn)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].data, self._signed(bytes(values)))
        self.assertEqual(plays[0].onset, 0)
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 1)
        self.assertEqual(song.dac_samples[0].data, samples[0].data)

    def test_long_gap_is_not_materialised(self):
        bank, events = self._runs([
            (0, bytes([0x81, 0x82, 0x83, 0x84]), 4),
            (4410, bytes([0x85, 0x86, 0x87, 0x88]), 4),
        ])
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=9000,
        )
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 2)
        # Each row owns its own run; the 4,410-sample pause stores no frames.
        self.assertEqual(sorted(len(s.data) for s in song.dac_samples), [4, 4])

    def test_held_byte_is_one_frame_plus_the_dac_hold(self):
        bank, events = self._runs([
            (0, bytes([0x80] * 32), 4),
            (4410, bytes([0x90, 0x91, 0x92, 0x93]), 4),
        ])
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=9000,
        )
        samples, _plays, _warn, stats = collect_dac(src, slices=True)
        self.assertEqual(len(samples), 2)
        held = next(s for s in samples if len(s.data) == 1)
        self.assertEqual(held.data, b"\x00")
        self.assertEqual(stats["dac_collapsed_bytes"], 32)
        song = analyze(src, speed=6, dac_slices=True)
        # A pure-silence run produces no note.
        self.assertEqual(song.dac_count, 1)
        self.assertEqual(len(song.dac_samples), 1)
        self.assertEqual(len(song.dac_samples[0].data), 4)
        self.assertEqual(song.dac_stats.get("dac_silent_groups"), 1)
        self.assertEqual(song.dac_stats.get("dac_silent_notes"), 1)

    def test_silent_rows_get_no_note_and_previous_sample_holds_silence(self):
        a = bytes([0x91, 0x92, 0x93, 0x94])
        b = bytes([0x95, 0x96, 0x97, 0x98])
        bank, events = self._runs([
            (0, a, 4),
            (4410, bytes([0x80] * 4), 4),
            (8820, b, 4),
        ])
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=14000,
        )
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 2)
        self.assertEqual(len(song.dac_samples), 2)
        self.assertEqual(song.dac_samples[0].data[-1], 0x00)
        self.assertEqual(song.dac_stats.get("dac_silent_groups"), 1)
        self.assertEqual(song.dac_stats.get("dac_silent_notes"), 1)
        fur = TMP / "dac_silence.fur"
        fur.write_bytes(write_fur(song, include_pcm=True))
        m = parse_fur(fur.read_bytes())
        notes = [r for r in m.patterns.get((5, 0), []) if r.note >= 0]
        self.assertEqual(len(notes), 2)

    def test_same_row_runs_merge_without_a_drop(self):
        a = bytes([0x01, 0x02, 0x03, 0x04])
        b = bytes([0x05, 0x06, 0x07, 0x08])
        bank, events = self._runs([(0, a, 4), (100, b, 4)])
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=1000,
        )
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 1)
        self.assertEqual(len(song.dac_samples), 1)
        data = song.dac_samples[0].data
        ia = data.find(self._signed(a))
        ib = data.find(self._signed(b), ia + len(a))
        self.assertGreaterEqual(ia, 0)
        self.assertGreater(ib, ia)
        stats: dict = {}
        write_fur(song, include_pcm=True, stats=stats)
        self.assertEqual(stats.get("note_collision", 0), 0)

    def test_more_than_120_slices_use_multiple_maps(self):
        spec = []
        for i in range(130):
            # Start at 0x81 so the first run is not a pure 0x80 rest.
            data = bytes([(0x81 + (i & 0x7F)) & 0xFF, 0x80 + (i >> 7)])
            spec.append((i * 4410, data, 4))
        bank, events = self._runs(spec)
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=580000,
        )
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 130)
        self.assertEqual(len(song.dac_samples), 130)
        self.assertEqual((len(song.dac_samples) + 119) // 120, 2)
        self.assertEqual({e.ins for e in song.events if e.dac}, {0, 1})
        # Sample 120 is the first of the second map: slot 0, instrument 1.
        second = sorted(song.dac_samples, key=lambda s: s.onset)[120]
        self.assertEqual(second.onset, 120 * 4410)
        cell = next(e for e in song.events if e.dac and e.sample == 120 * 4410)
        self.assertEqual((cell.note, cell.ins), (60, 1))
        fur = TMP / "dac_maps.fur"
        fur.write_bytes(write_fur(song, include_pcm=True))
        m = parse_fur(fur.read_bytes())
        self.assertEqual(len(m.samples), 130)
        # Every DAC map reports base 0 for release Furnace; the map entries
        # still point at samples 0..119 and 120..129.
        self.assertEqual(m.ins_sample[:2], [0, 0])

    def test_pan_change_between_rows_reaches_the_note(self):
        bank, events = self._runs([
            (0, bytes([0x91, 0x92, 0x93, 0x94]), 4),
            (4410, bytes([0x95, 0x96, 0x97, 0x98]), 4),
        ])
        writes = [ChipWrite(2000, "ym2612", 0, 0x1B6, 0x40)]
        src = _vgm(
            writes, dac_data=bank, dac_events=events, total_samples=9000,
        )
        song = analyze(src, speed=6, dac_slices=True)
        pans = [e.pan for e in song.events if e.dac]
        self.assertEqual(pans, [0xFF, 0x0F])

    def test_pan_change_inside_a_row_is_a_channel_effect(self):
        bank, events = self._runs([
            (0, bytes([0x91, 0x92, 0x93, 0x94]), 4),
            (100, bytes([0x95, 0x96, 0x97, 0x98]), 4),
        ])
        writes = [ChipWrite(50, "ym2612", 0, 0x1B6, 0x40)]
        src = _vgm(
            writes, dac_data=bank, dac_events=events, total_samples=1000,
        )
        song = analyze(src, speed=6, dac_slices=True)
        notes = [e for e in song.events if e.dac]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].pan, 0xFF)
        pans = [fx for fx in song.channel_fx if fx[1] == 5 and fx[2] == 0x08]
        self.assertTrue(pans)
        self.assertEqual(pans[0][3], 0x0F)

    def test_disabled_dac_leaves_fm6_alone(self):
        # Track 01 writes 0x2B=0 and never enables the DAC.  Its 0x2A
        # writes are inaudible and its FM6 key-on must stay an FM note.
        ws = [
            ChipWrite(0, "ym2612", 0, 0x2B, 0x00),
            *_fm_note(4, 1204, t=100, off=0, ch=5),
            ChipWrite(200, "ym2612", 0, 0x2A, 0x90),
        ]
        src = _vgm(ws, dac_events=[(200, 2, 0x90)], total_samples=1000)
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 0)
        self.assertTrue(any("disabled" in w for w in song.warnings))
        fm6 = [
            e for e in song.events
            if e.on and e.ch == 5 and not e.dac and not e.pcm
        ]
        self.assertTrue(fm6)
        self.assertTrue(all(e.ins >= 0 for e in fm6))

    def test_modern_block_play_is_a_slice(self):
        body = bytes([0x80, 0x90, 0x70, 0x60, 0x50])
        src = _vgm(
            self._marker(), dac_blocks=[body],
            dac_block_plays=[(0, 0, 11025), (4410, 0, 11025)],
            total_samples=9000,
        )
        samples, plays, warn, _stats = collect_dac(src, slices=True)
        self.assertFalse(warn)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].data, self._signed(body))
        self.assertEqual(samples[0].c4_rate, 11025)
        self.assertEqual(len(plays), 2)
        song = analyze(src, speed=6, dac_slices=True)
        self.assertEqual(song.dac_count, 2)

    def test_second_block_play_cuts_the_first(self):
        # Starting a 0x95 stream stops the previous block.  Expanding both
        # copies to their full length leaves two PCM streams interleaved, and
        # the later word wins at each frame (a garbled waveform).
        body = bytes(range(0x80, 0x80 + 100))
        src = _vgm(
            self._marker(), dac_blocks=[body],
            dac_block_plays=[(0, 0, 11025), (200, 0, 11025)],
            total_samples=9000,
        )
        samples, plays, warn, _stats = collect_dac(src, slices=True)
        self.assertFalse(warn)
        # 200 VGM samples at 11025 Hz is 50 source bytes.
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].data, self._signed(body[:50]))
        self.assertEqual(samples[1].data, self._signed(body))
        self.assertEqual(sorted(p.onset for p in plays), [0, 200])

        _s, _p, _w, stats = collect_dac(src)
        self.assertEqual(stats.get("dac_block_plays_cut"), 1)
        self.assertNotIn("dac_same_time_runs", stats)

    @unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
    def test_export_starts_and_bodies_match_the_slices(self):
        spec = [
            (0, bytes([0x91, 0x92, 0x93, 0x94]), 4),
            (4410, bytes([0x95, 0x96, 0x97, 0x98]), 4),
            (8820, bytes([0x99, 0x9A, 0x9B, 0x9C]), 4),
        ]
        bank, events = self._runs(spec)
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=14000,
        )
        song = analyze(src, speed=6, dac_slices=True)
        fur = TMP / "dac_slices.fur"
        fur.write_bytes(write_fur(song, include_pcm=True))
        vgm = TMP / "dac_slices.vgm"
        export_vgm(fur, vgm)
        exp = load_vgm(vgm)
        notes = sorted(e.sample for e in song.events if e.dac)
        starts = sorted(
            p[0] for p in exp.dac_block_plays
            if p[0] < song.vgm.total_samples
        )
        self.assertEqual(len(notes), 3)
        for note in notes:
            self.assertTrue(
                any(abs(start - note) <= 2 for start in starts),
                (note, starts),
            )
        bodies = {bytes(b) for b in exp.dac_blocks}
        ours = {
            bytes((x + 0x80) & 0xFF for x in s.data)
            for s in song.dac_samples
        }
        self.assertLessEqual(ours, bodies)

    def test_packed_dense_stream_is_one_real_sample_and_note(self):
        bank = bytes((0x80 + (i % 31) - 15) & 0xFF for i in range(300))
        bank, events = self._runs([(0, bank, 4)])
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=400000,
        )
        samples, plays, warn, stats = collect_dac(src)
        self.assertFalse(warn)
        self.assertEqual(stats["dac_mode"], "source")
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 1)
        self.assertEqual(samples[0].c4_rate, 11025)
        self.assertEqual(samples[0].data, self._signed(bank))
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_count, 1)
        self.assertEqual(len(song.dac_samples), 1)
        fur = TMP / "dac_packed.fur"
        fur.write_bytes(write_fur(song, include_pcm=True))
        m = parse_fur(fur.read_bytes())
        self.assertEqual(len(m.samples), 1)
        notes = [r for r in m.patterns.get((5, 0), []) if r.note >= 0]
        self.assertEqual(len(notes), 1)
        sm_bases = []
        for _o, mag, _s, blk in iter_blocks(decompress_fur(fur.read_bytes())):
            if mag != b"INS2":
                continue
            pos = 4
            name = ""
            sm = None
            while pos + 4 <= len(blk):
                feat = blk[pos:pos + 2]
                size = int.from_bytes(blk[pos + 2:pos + 4], "little")
                if feat == b"NA":
                    name = blk[pos + 4:pos + 4 + size].split(b"\0")[0].decode()
                if feat == b"SM":
                    sm = blk[pos + 4:pos + 4 + size]
                if feat == b"EN":
                    break
                pos += 4 + size
            if sm is not None and name.startswith("DAC"):
                sm_bases.append(int.from_bytes(sm[:2], "little"))
        self.assertEqual(sm_bases, [0])

    def test_packed_short_internal_silence_stays_one_phrase(self):
        bank, events = self._runs([
            (0, bytes([0x91, 0x92, 0x93, 0x94]), 4),
            (400, bytes([0x80] * 4), 4),
            (800, bytes([0x95, 0x96, 0x97, 0x98]), 4),
        ])
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=2000,
        )
        samples, plays, _warn, stats = collect_dac(src)
        self.assertEqual(stats.get("dac_silent_phrases", 0), 0)
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 1)
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_count, 1)

    def test_packed_long_gap_splits_audio_not_silence(self):
        bank, events = self._runs([
            (0, bytes([0x91, 0x92, 0x93, 0x94]), 4),
            (4410, bytes([0x80] * 4), 4),
            (8820, bytes([0x95, 0x96, 0x97, 0x98]), 4),
        ])
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=14000,
        )
        samples, plays, _warn, stats = collect_dac(src)
        self.assertEqual(stats.get("dac_source_silent_runs"), 1)
        # All three runs are played once and in sequence, so the one-shot
        # coalescer joins them into a single body. The gap is held inside it
        # (the DAC holds its last value, then the source writes 0x00).
        self.assertEqual(stats.get("dac_source_coalesced_chains"), 1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 1)
        self.assertTrue(any(samples[0].data))
        self.assertIn(b"\x00" * 4, samples[0].data)
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_count, 1)
        fur = TMP / "dac_packed_gap.fur"
        fur.write_bytes(write_fur(song, include_pcm=True))
        m = parse_fur(fur.read_bytes())
        notes = [r for r in m.patterns.get((5, 0), []) if r.note >= 0]
        self.assertEqual(len(notes), 1)

    def test_packed_silence_only_stream_has_no_notes(self):
        bank, events = self._runs([(0, bytes([0x80] * 32), 4)])
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=2000,
        )
        samples, plays, warn, stats = collect_dac(src)
        self.assertEqual(samples, [])
        self.assertEqual(plays, [])
        self.assertTrue(any("only silence" in w for w in warn))
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_count, 0)

    def test_packed_rate_is_the_smallest_source_interval(self):
        events = [
            (0, 2, 0x91),
            (3, 2, 0x92),
            (7, 2, 0x93),
            (12, 2, 0x94),
        ]
        src = _vgm(
            self._marker(), dac_events=events, total_samples=100,
        )
        samples, plays, _warn, _stats = collect_dac(src)
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 1)
        self.assertEqual(samples[0].c4_rate, 14700)
        # Waits 3, 4, 5 at 14,700 Hz: frames 0, 1, 2, 4; frame 3 holds 0x93.
        self.assertEqual(
            samples[0].data,
            self._signed(bytes([0x91, 0x92, 0x93, 0x93, 0x94])),
        )

    def test_long_pcm_is_one_source_sample_with_mid_note_pan(self):
        # 20 s at 4-wait with pan changes: one bank sample, one note, and the
        # pan moves on the channel while the sample keeps playing.
        bank = bytes((0x80 + (i % 31) - 15) & 0xFF for i in range(220500))
        bank, events = self._runs([(0, bank, 4)])
        writes = self._marker() + [
            ChipWrite(0, "ym2612", 0, 0x1B6, 0xC0),
            ChipWrite(5 * 44100, "ym2612", 0, 0x1B6, 0x80),
            ChipWrite(10 * 44100, "ym2612", 0, 0x1B6, 0x40),
        ]
        src = _vgm(
            writes, dac_data=bank, dac_events=events,
            total_samples=20 * 44100,
        )
        samples, plays, _warn, stats = collect_dac(src)
        self.assertEqual(stats["dac_mode"], "source")
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 1)
        self.assertEqual(samples[0].c4_rate, 11025)
        self.assertEqual(
            [pan for _t, pan in stats["dac_pan_points"]],
            [0xFF, 0xF0, 0x0F],
        )
        self.assertEqual(plays[0].pan, 0xFF)
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_count, 1)
        note_pans = {e.pan for e in song.events if e.dac}
        self.assertEqual(note_pans, {0xFF})
        pans = {
            fx[3] for fx in song.channel_fx
            if fx[2] == 0x08 and fx[3] in (0xF0, 0x0F)
        }
        self.assertEqual(pans, {0xF0, 0x0F})
        fur = TMP / "dac_chunks.fur"
        fur.write_bytes(write_fur(song, include_pcm=True))
        m = parse_fur(fur.read_bytes())
        self.assertEqual(len(m.samples), 1)
        notes = [r for r in m.patterns.get((5, 0), []) if r.note >= 0]
        self.assertEqual(len(notes), 1)
        pans = {
            val for rows in m.patterns.values() for r in rows
            for cmd, val in r.fx if cmd == 0x08
        }
        self.assertTrue({0xFF, 0xF0, 0x0F} <= pans, pans)

    def test_port0_b6_is_channel_three_not_the_dac_pan(self):
        # $B6 on port 0 panning channel 3 used to be read as the DAC's pan.
        # The DAC is FM6, whose pan lives on port 1 ($1B6).
        bank, events = self._runs([(0, bytes([0x80, 0x90, 0xA0, 0xB0]), 4)])
        writes = self._marker() + [
            ChipWrite(0, "ym2612", 0, 0xB6, 0x40),
        ]
        src = _vgm(
            writes, dac_data=bank, dac_events=events, total_samples=1000,
        )
        _samples, plays, _warn, _stats = collect_dac(src)
        self.assertEqual(plays[0].pan, -1)

        writes = self._marker() + [
            ChipWrite(0, "ym2612", 0, 0x1B6, 0x40),
        ]
        src = _vgm(
            writes, dac_data=bank, dac_events=events, total_samples=1000,
        )
        _samples, plays, _warn, _stats = collect_dac(src)
        self.assertEqual(plays[0].pan, 0x0F)


class TestYm2612Pitch(unittest.TestCase):
    def test_note_fnum_is_the_furnace_mapping(self):
        for note in (60, 72, 84, 96, 108, 115):
            block, fnum = ym2612_note_fnum(note, CLOCK)
            fbase = 440.0 * 2.0 ** ((note * 128 - 7296) / 1536.0)
            bf = round(fbase * (YM2612_FREQ_BASE / CLOCK))
            want_block = max(0, min(7, (note - 60) // 12))
            self.assertEqual((block, fnum), (want_block, bf >> want_block))

    def test_note_fnum_matches_exported_register(self):
        # The Genesis platform uses fmFreqBase 9437184, so note 96 is C-4
        # (261 Hz) at 7.67 MHz: block 3, f-num 643.
        self.assertEqual(ym2612_note_fnum(96, CLOCK), (3, 643))
        self.assertEqual(furnace_note_name(96), "C-3")

    def test_e5_is_one_128th_semitone(self):
        note, e5 = ym2612_note_for(3, 1646, CLOCK)
        b, f = ym2612_note_fnum(note, CLOCK, e5)
        self.assertLess(abs(ym2612_pitch_units(b, f)
                            - ym2612_pitch_units(3, 1646)), 0.2)

    def test_round_trip_through_e5(self):
        for block, fnum in ((4, 1100), (4, 1232), (3, 923), (3, 1466), (2, 2016)):
            note, e5 = ym2612_note_for(block, fnum, CLOCK)
            b, f = ym2612_note_fnum(note, CLOCK, e5)
            self.assertLess(
                abs(ym2612_pitch_units(b, f) - ym2612_pitch_units(block, fnum)),
                0.2,
            )

    def test_pitch_units_ignore_block_equivalents(self):
        self.assertAlmostEqual(
            ym2612_pitch_units(3, 1000), ym2612_pitch_units(4, 500), places=9
        )


class TestYm2612State(unittest.TestCase):
    def test_frequency_latch_needs_high_then_low(self):
        st = YM2612State()
        st.write(0xA0, 0xBE)          # low before any high: no commit
        self.assertEqual(st.frequency(0), (0, 0))
        st.write(0xA4, 0x12)          # high
        self.assertEqual(st.frequency(0), (0, 0))
        st.write(0xA0, 0xA5)          # low commits
        self.assertEqual(st.frequency(0), (2, 0x2A5))
        st.write(0xA4, 0x1A)          # high alone changes nothing
        self.assertEqual(st.frequency(0), (2, 0x2A5))
        st.write(0xA0, 0xD3)
        self.assertEqual(st.frequency(0), (3, 0x2D3))

    def test_bank_one_is_channels_four_to_six(self):
        st = YM2612State()
        st.write(0x130, 0x25)
        self.assertEqual(st.ops[3][0].dt_mul & 0x0F, 0x05)
        self.assertEqual(st.ops[0][0].dt_mul, 0)
        st.write(0x1A4, 0x1A)
        st.write(0x1A0, 0xD3)
        self.assertEqual(st.frequency(3), (3, 0x2D3))
        self.assertEqual(st.frequency(0), (0, 0))

    def test_special_mode_pairs(self):
        st = YM2612State()
        st.write(0x27, 0x40)          # special mode
        st.write(0xAC, 0x22)          # group 0 high
        st.write(0xA8, 0x05)          # group 0 low
        st.write(0xAD, 0x24)          # group 1
        st.write(0xA9, 0xB9)
        st.write(0xAE, 0x1A)          # group 2
        st.write(0xAA, 0xD3)
        self.assertEqual(st.frequency_op(2, 0), (4, 0x205))
        self.assertEqual(st.frequency_op(2, 1), (4, 0x4B9))
        self.assertEqual(st.frequency_op(2, 2), (3, 0x2D3))
        self.assertEqual(st.frequency_op(2, 3), (0, 0))


class TestYm2612Analyze(unittest.TestCase):
    def test_fm_note_and_patch(self):
        song = analyze(_vgm(_fm_note(4, 1232)))
        self.assertEqual(song.fm_chip, "ym2612")
        self.assertEqual(song.ym2612_ids, [0])
        self.assertFalse(song.ym2612_ext)
        self.assertEqual(len(song.fm_patches), 1)
        p = song.fm_patches[0]
        self.assertTrue(p.opn)
        self.assertEqual(p.alg, 7)
        ev = next(e for e in song.events if e.on)
        self.assertEqual(ev.ch, 0)
        self.assertEqual((ev.note, ev.e5), ym2612_note_for(4, 1232, CLOCK))
        self.assertEqual(ev.ins, 0)
        # The carrier TL moved into the pattern volume column (the test
        # patch's carrier is at TL 0, so the column is wide open).
        self.assertEqual(ev.vol, 127)

    def test_key_off_event_and_end(self):
        song = analyze(_vgm(_fm_note(4, 1232, off=60000)))
        on = next(e for e in song.events if e.on)
        self.assertEqual(on.end, 60000)
        off = [e for e in song.events if not e.on and e.ch == 0]
        self.assertEqual([e.sample for e in off], [60000])

    def test_retrigger_off_is_dropped(self):
        ws = _fm_note(4, 1232, off=0)
        ws += [
            ChipWrite(5000, "ym2612", 0, 0x28, 0x00),
            ChipWrite(5001, "ym2612", 0, 0x28, 0xF0),
        ]
        song = analyze(_vgm(ws))
        offs = [e for e in song.events if not e.on and e.ch == 0]
        self.assertEqual(offs, [])

    def test_channels_four_to_six_use_bank_one(self):
        ws = _fm_note(4, 1232, ch=0)
        ws += _fm_note(4, 1466, ch=3, t=200)
        ws += _fm_note(4, 1204, ch=5, t=300)
        song = analyze(_vgm(ws))
        on = [e for e in song.events if e.on]
        self.assertEqual(sorted(e.ch for e in on), [0, 3, 5])

    def test_mid_note_fnum_becomes_pitch_step(self):
        ws = _fm_note(4, 1204, off=60000)
        ws += [
            ChipWrite(30000, "ym2612", 0, 0xA4, (4 << 3) | ((1250 >> 8) & 7)),
            ChipWrite(30000, "ym2612", 0, 0xA0, 1250 & 0xFF),
        ]
        song = analyze(_vgm(ws))
        ev = next(e for e in song.events if e.on)
        self.assertGreaterEqual(len(ev.fm_pitch_pts), 2)
        # A single held step is cheaper as a row effect and must not spend an
        # instrument copy.
        self.assertTrue(ev.pitch_steps)
        self.assertEqual(ev.ins, 0)

    def test_mid_note_patch_change_splits_the_held_note(self):
        # A driver can load a new patch into a still-keyed voice (Time Trax
        # holds a drone through several patch swaps). The chip changes the
        # operators at once, but a tracker note keeps its instrument until the
        # next key-on, so the hold has to become a second note.
        ws = _fm_note(4, 1204, ch=4, off=60000)
        t = 30000
        ws += [
            ChipWrite(t, "ym2612", 0, 0x1B1, 0x3A),  # alg 2, feedback 7
            ChipWrite(t, "ym2612", 0, 0x131, 0x08),
            ChipWrite(t, "ym2612", 0, 0x135, 0x24),
        ]
        song = analyze(_vgm(ws))
        on = [e for e in song.events if e.on and e.ch == 4]
        self.assertEqual([e.sample for e in on], [100, t])
        self.assertEqual(on[0].end, t)
        self.assertNotEqual(on[0].ins, on[1].ins)
        self.assertEqual(song.fm_patches[on[1].ins].alg, 2)
        # The key stayed down: the patch swap is a legato split, not a new
        # attack. EA01 lets the new instrument's macros take over without
        # resetting the voice; the next real key-on writes EA00.
        self.assertTrue(on[1].legato)
        mod = parse_fur(write_fur(song, include_pcm=False, include_fm=True))
        self.assertTrue(any(
            row.note == on[1].note
            and row.ins == on[1].ins
            and any(cmd == 0xEA and val == 1 for cmd, val in row.fx)
            for (ch, _pat), rows in mod.patterns.items() if ch == 4
            for row in rows
        ), "legato split lost the instrument change")

    def test_patch_load_before_key_on_does_not_split(self):
        # The patch bytes just before a key transition belong to the next
        # note. Splitting the old note would put two notes on one row.
        ws = _fm_note(4, 1204, ch=4, off=0)
        ws += [
            ChipWrite(29900, "ym2612", 0, 0x1B1, 0x3A),
            ChipWrite(29900, "ym2612", 0, 0x131, 0x08),
            ChipWrite(30000, "ym2612", 0, 0x28, 0x05),
            ChipWrite(30001, "ym2612", 0, 0x28, 0xF5),
        ]
        song = analyze(_vgm(ws))
        on = [e for e in song.events if e.on and e.ch == 4]
        self.assertEqual([e.sample for e in on], [100, 30001])

    def test_long_pitch_sweep_becomes_pitch_macro(self):
        ws = _fm_note(4, 1204, off=60000)
        for i, fnum in enumerate((1210, 1260) * 6):
            t = 30000 + i * 1000
            ws += [
                ChipWrite(t, "ym2612", 0, 0xA4, (4 << 3) | ((fnum >> 8) & 7)),
                ChipWrite(t, "ym2612", 0, 0xA0, fnum & 0xFF),
            ]
        song = analyze(_vgm(ws))
        ev = next(e for e in song.events if e.on)
        self.assertGreater(len(ev.fm_pitch_pts), 4)
        self.assertNotEqual(ev.ins, 0)
        macro = song.fm_patches[ev.ins].pitch_macro
        self.assertTrue(macro)
        self.assertEqual(macro[0], 0)
        self.assertGreater(max(macro), 0)

    def test_lfo_register_becomes_chip_effect(self):
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2612", 0, 0x22, 0x0B),   # on, rate 3
            ChipWrite(20000, "ym2612", 0, 0x22, 0x02),  # off, rate 2
            ChipWrite(100, "ym2612", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2612", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x00),
        ]
        song = analyze(_vgm(ws))
        lfo = [(cmd, val) for _t, cmd, val in song.chip_fx if cmd == 0x10]
        self.assertEqual(lfo, [(0x10, 0x13), (0x10, 0x02)])

    def test_carrier_tl_moves_to_volume_and_shares_the_patch(self):
        # Algorithm 0: operator 4 is the only carrier.
        ws = _patch(100) + [
            ChipWrite(100, "ym2612", 0, 0xB0, 0x00),
            ChipWrite(100, "ym2612", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2612", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x00),
            # Same timbre, carrier TL 0x30 at the next key-on.
            ChipWrite(70000, "ym2612", 0, 0x4C, 0x30),
            ChipWrite(70000, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(80000, "ym2612", 0, 0x28, 0x00),
        ]
        song = analyze(_vgm(ws))
        on = [e for e in song.events if e.on]
        self.assertEqual(len(on), 2)
        self.assertEqual(on[0].ins, on[1].ins)
        self.assertEqual(on[0].vol, 127)
        self.assertEqual(on[1].vol, 127 - 0x30)
        self.assertEqual(len(song.fm_patches), 1)
        # The instrument keeps only the relative carrier level.
        self.assertEqual(song.fm_patches[0].ops[3].tl, 0)

    def test_alg4_carriers_use_register_order(self):
        # Algorithm 4: the real carriers are S2 and S4, i.e. registers 0x38
        # and 0x3C (op indices 2 and 3), not S3/S4 (1 and 3).
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2612", 0, 0xB0, 0x04),
            ChipWrite(100, "ym2612", 0, 0x40, 0x10),  # S1 TL (modulator)
            ChipWrite(100, "ym2612", 0, 0x44, 0x40),  # S3 TL (modulator)
            ChipWrite(100, "ym2612", 0, 0x48, 0x20),  # S2 TL (carrier)
            ChipWrite(100, "ym2612", 0, 0x4C, 0x30),  # S4 TL (carrier)
            ChipWrite(100, "ym2612", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2612", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x00),
        ]
        song = analyze(_vgm(ws))
        ev = next(e for e in song.events if e.on)
        patch = song.fm_patches[ev.ins]
        self.assertEqual([op.tl for op in patch.ops], [0x10, 0x40, 0x00, 0x10])
        self.assertEqual(patch.carrier_tl(), 0)
        self.assertEqual(ev.vol, 127 - 0x20)

    def test_partial_kon_mask_mutes_the_right_operators(self):
        # KON mask 0x5 enables datasheet operators S1 and S3, i.e. register
        # indices 0 (0x30) and 1 (0x34).  Register indices 2 (S2, 0x38) and
        # 3 (S4, 0x3C) must be muted.
        ws = _patch(100) + [
            ChipWrite(100, "ym2612", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2612", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2612", 0, 0x28, 0x50),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x00),
        ]
        song = analyze(_vgm(ws))
        ev = next(e for e in song.events if e.on)
        patch = song.fm_patches[ev.ins]
        self.assertNotEqual(patch.ops[0].tl, 0x7F)
        self.assertNotEqual(patch.ops[1].tl, 0x7F)
        self.assertEqual(patch.ops[2].tl, 0x7F)
        self.assertEqual(patch.ops[3].tl, 0x7F)

    def test_same_sample_writes_after_kon_are_in_the_patch(self):
        ws = _fm_note(4, 1232, off=60000)
        ws += [
            ChipWrite(100, "ym2612", 0, 0xB0, 0x00),  # alg 0 after the KON
            ChipWrite(100, "ym2612", 0, 0x4C, 0x30),  # carrier TL after it
            ChipWrite(100, "ym2612", 0, 0xB4, 0xC7),  # PMS 7 after it
        ]
        song = analyze(_vgm(ws))
        ev = next(e for e in song.events if e.on)
        patch = song.fm_patches[ev.ins]
        self.assertEqual(patch.alg, 0)
        self.assertEqual(patch.pms, 7)
        self.assertEqual(patch.ops[3].tl, 0)
        self.assertEqual(ev.vol, 127 - 0x30)

    def test_pms_ams_change_while_held_becomes_effects(self):
        ws = _fm_note(4, 1232, off=60000)
        ws += [ChipWrite(30000, "ym2612", 0, 0xB4, 0xD3)]  # AMS 1, PMS 3
        song = analyze(_vgm(ws))
        fx = {(cmd, val) for _t, _ch, cmd, val in song.channel_fx}
        self.assertIn((0x63, 1), fx)  # AMS
        self.assertIn((0x62, 3), fx)  # PMS (FMS)

    def test_pms_ams_are_part_of_the_patch(self):
        ws = _fm_note(4, 1232, off=60000)
        ws += [
            # A second note with the same timbre but different LFO depth must
            # not reuse the first instrument.
            ChipWrite(70000, "ym2612", 0, 0xB4, 0xD3),
            ChipWrite(70000, "ym2612", 0, 0x28, 0xF0),
        ]
        song = analyze(_vgm(ws))
        on = [e for e in song.events if e.on]
        self.assertEqual(len(on), 2)
        self.assertNotEqual(on[0].ins, on[1].ins)


class TestYm2612SpecialCases(unittest.TestCase):
    def _special(self):
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2612", 0, 0x27, 0x40),   # channel-3 special
            ChipWrite(100, "ym2612", 0, 0xAD, 0x22),   # group 1 (column 0)
            ChipWrite(100, "ym2612", 0, 0xA9, 0x05),
            ChipWrite(100, "ym2612", 0, 0xAC, 0x24),   # group 0 (column 2)
            ChipWrite(100, "ym2612", 0, 0xA8, 0xB9),
            ChipWrite(100, "ym2612", 0, 0xAE, 0x1A),   # group 2 (column 1)
            ChipWrite(100, "ym2612", 0, 0xAA, 0xD3),
            ChipWrite(100, "ym2612", 0, 0xA6, 0x01),   # normal, column 3
            ChipWrite(100, "ym2612", 0, 0xA2, 0x23),
            # Make the channel-3 operators audible in every column.
            ChipWrite(100, "ym2612", 0, 0x32, 0x01),
            ChipWrite(100, "ym2612", 0, 0x36, 0x01),
            ChipWrite(100, "ym2612", 0, 0x3A, 0x01),
            ChipWrite(100, "ym2612", 0, 0x3E, 0x01),
            ChipWrite(100, "ym2612", 0, 0xB2, 0x07),
            ChipWrite(100, "ym2612", 0, 0xB6, 0x40),   # channel 3: right only
            ChipWrite(100, "ym2612", 0, 0x28, 0xF2),
            ChipWrite(2000, "ym2612", 0, 0x28, 0xF2),  # same mask: no retrigger
            ChipWrite(60000, "ym2612", 0, 0x28, 0x02),
        ]
        return ws

    def test_special_mode_uses_ext_layout(self):
        song = analyze(_vgm(self._special()))
        self.assertTrue(song.ym2612_ext)
        evs = [e for e in song.events if e.on]
        self.assertEqual(len(evs), 4)
        self.assertEqual(sorted(e.ch for e in evs), [2, 3, 4, 5])
        by_ch = {e.ch: e for e in evs}
        self.assertEqual(
            (by_ch[2].note, by_ch[2].e5), ym2612_note_for(4, 0x205, CLOCK)
        )
        self.assertEqual(
            (by_ch[3].note, by_ch[3].e5), ym2612_note_for(3, 0x2D3, CLOCK)
        )
        self.assertEqual(
            (by_ch[4].note, by_ch[4].e5), ym2612_note_for(4, 0x4B9, CLOCK)
        )
        self.assertEqual(
            (by_ch[5].note, by_ch[5].e5), ym2612_note_for(0, 0x123, CLOCK)
        )
        # The four columns share the channel's pan register.
        self.assertEqual({by_ch[c].pan for c in range(2, 6)}, {0x0F})
        mod = parse_fur(write_fur(song))
        self.assertEqual(mod.chips, [CHIP_YM2612_EXT])
        self.assertEqual(mod.n_ch, 9)

    def test_partial_key_on_before_the_first_full_chord_is_kept(self):
        # FM Towns MIDI drivers open channel 3 with a two-operator mask before
        # the first full chord. Those are real notes, not operator setup.
        t = 20_000
        ws = _patch(100, ch=2)
        ws += [
            ChipWrite(100, "ym2612", 0, 0x27, 0x40),
            ChipWrite(100, "ym2612", 0, 0xAD, 0x22),   # group 1 (column 0)
            ChipWrite(100, "ym2612", 0, 0xA9, 0x6A),
            ChipWrite(100, "ym2612", 0, 0xAE, 0x22),   # group 2 (column 1)
            ChipWrite(100, "ym2612", 0, 0xAA, 0x6A),
            ChipWrite(t, "ym2612", 0, 0x28, 0x32),     # operators 1 and 2
        ]
        song = analyze(_vgm(ws))
        ons = sorted(
            e.ch for e in song.events if e.on and e.sample == t
        )
        self.assertEqual(ons, [2, 3])

    def test_normal_channel_notes_feed_all_four_columns(self):
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2612", 0, 0x27, 0x40),
            ChipWrite(100, "ym2612", 0, 0x32, 0x01),
            ChipWrite(100, "ym2612", 0, 0x36, 0x01),
            ChipWrite(100, "ym2612", 0, 0x3A, 0x01),
            ChipWrite(100, "ym2612", 0, 0x3E, 0x01),
            ChipWrite(100, "ym2612", 0, 0xB2, 0x07),
            ChipWrite(100, "ym2612", 0, 0xA6, 0x1A),
            ChipWrite(100, "ym2612", 0, 0xA2, 0xD3),
            ChipWrite(100, "ym2612", 0, 0x28, 0xF2),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x02),
        ]
        song = analyze(_vgm(ws))
        evs = [e for e in song.events if e.on]
        self.assertEqual(len(evs), 4)
        note, e5 = ym2612_note_for(3, 0x2D3, CLOCK)
        self.assertTrue(all((e.note, e.e5) == (note, e5) for e in evs))

    def test_csm_mode_warns_but_converts(self):
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2612", 0, 0x27, 0x80),
            ChipWrite(100, "ym2612", 0, 0x28, 0xF2),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x02),
        ]
        song = analyze(_vgm(ws))
        self.assertTrue(song.ym2612_ext)
        self.assertTrue(song.ym2612_csm)
        self.assertTrue(any("CSM" in w for w in song.warnings))

    def test_dac_key_ons_do_not_become_fm_notes(self):
        ws = [
            ChipWrite(100, "ym2612", 0, 0x2B, 0x80),   # DAC on
            ChipWrite(100, "ym2612", 0, 0x28, 0xF5),   # FM6 key-on
            ChipWrite(200, "ym2612", 0, 0x2A, 0x80),
            ChipWrite(300, "ym2612", 0, 0x28, 0x05),   # FM6 key-off
        ]
        song = analyze(_vgm(ws))
        self.assertEqual([e for e in song.events if e.on and e.ch == 5], [])
        self.assertTrue(any("DAC" in w for w in song.warnings))

    def test_pitch_stream_follows_bank_one(self):
        ws = _fm_note(4, 1232, ch=4)
        stream = ym2612_pitch_stream(_vgm(ws), 4)
        self.assertTrue(stream)
        self.assertAlmostEqual(stream[-1][1], ym2612_pitch_units(4, 1232), places=6)
        self.assertEqual(ym2612_keyed_intervals(_vgm(ws), 4), [(100, 60000)])
        # A channel outside the chip has no stream.
        self.assertEqual(ym2612_pitch_stream(_vgm(ws), 6), [])


class TestYm2612Writer(unittest.TestCase):
    def _song(self, ws):
        return analyze(_vgm(ws))

    def test_single_chip_layout_and_ids(self):
        data = write_fur(self._song(_fm_note(4, 1232)), include_pcm=False)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_YM2612])
        self.assertEqual(m.n_ch, 6)
        self.assertEqual(CHIP_CHANNELS[CHIP_YM2612], 6)
        self.assertIn(b"FM 1", data)
        self.assertIn(b"FM 6", data)
        self.assertIn(b"customClock=7670453", data)
        self.assertEqual(_ins_types(data)[0], INS_FM)

    def test_fm_towns_clock_uses_the_named_setting(self):
        # Furnace's Genesis clock list has a 8 MHz FM Towns entry; a real FM
        # Towns rip should select it instead of carrying a custom clock.
        song = self._song(_fm_note(4, 1232))
        song.vgm.ym2612_clock = 8_000_000
        data = write_fur(song, include_pcm=False)
        self.assertIn(b"clockSel=2", data)
        self.assertNotIn(b"customClock=8000000", data)

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

    def test_pitch_macro_rides_on_an_instrument_copy(self):
        ws = _fm_note(4, 1204, off=60000)
        for i, fnum in enumerate((1210, 1260) * 6):
            t = 30000 + i * 1000
            ws += [
                ChipWrite(t, "ym2612", 0, 0xA4, (4 << 3) | ((fnum >> 8) & 7)),
                ChipWrite(t, "ym2612", 0, 0xA0, fnum & 0xFF),
            ]
        song = self._song(ws)
        ev = next(e for e in song.events if e.on)
        self.assertTrue(song.fm_patches[ev.ins].pitch_macro)
        data = write_fur(song, include_pcm=False)
        self.assertGreaterEqual(data.count(b"INS2"), 2)

    def test_dual_chip_layout(self):
        ws = _fm_note(4, 1232, cid=0, ch=0)
        ws += _fm_note(4, 923, cid=1, ch=0, t=200)
        data = write_fur(self._song(ws), include_pcm=False)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_YM2612, CHIP_YM2612])
        self.assertEqual(m.n_ch, 12)
        self.assertIn(b"FM 1 (2)", data)
        self.assertEqual(_ins_types(data), [INS_FM])


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestYm2612Furnace(unittest.TestCase):
    def _export(self, song, name: str):
        fur = TMP / f"{name}.fur"
        out = TMP / f"{name}.vgm"
        fur.write_bytes(write_fur(song, include_pcm=False))
        export_vgm(fur, out)
        return load_vgm(out)

    def _pairs(self, vgm, ch=0):
        hi = None
        out = []
        bank = 1 if ch >= 3 else 0
        sub = ch - 3 if ch >= 3 else ch
        for w in vgm.writes:
            if w.chip != "ym2612" or w.chip_id != 0:
                continue
            r = w.reg & 0xFF
            if bool(w.reg & 0x100) != bool(bank):
                continue
            if r == 0xA4 + sub:
                hi = w.val
            elif r == 0xA0 + sub and hi is not None:
                out.append(((hi >> 3) & 7, w.val | ((hi & 7) << 8)))
                hi = None
        return out

    def test_note_pitch_round_trip(self):
        for block, fnum in ((2, 702), (4, 1232), (3, 923), (3, 1466)):
            vgm = self._export(
                analyze(_vgm(_fm_note(block, fnum))),
                f"ym2612_note_{block}_{fnum}",
            )
            self.assertEqual(vgm.ym2612_clock, CLOCK)
            pairs = self._pairs(vgm)
            want = ym2612_pitch_units(block, fnum)
            # The export may choose an equivalent block/f-num octave; the
            # played pitch is what has to match.
            self.assertTrue(
                any(abs(ym2612_pitch_units(b, f) - want) < 0.5 for b, f in pairs),
                pairs,
            )

    def test_bank_one_round_trip(self):
        vgm = self._export(
            analyze(_vgm(_fm_note(4, 1232, ch=4))), "ym2612_bank1",
        )
        want = ym2612_pitch_units(4, 1232)
        self.assertTrue(any(
            abs(ym2612_pitch_units(b, f) - want) < 0.5 for b, f in self._pairs(vgm, 4)
        ))

    def test_sr_and_ssg_eg_reach_the_chip(self):
        ws = _fm_note(
            4, 1232,
            sr=(0x12, 0x13, 0x14, 0x15),
            ssg=(0x05, 0x06, 0x07, 0x08),
        )
        vgm = self._export(analyze(_vgm(ws)), "ym2612_ops")
        reg: dict[int, set[int]] = {}
        for w in vgm.writes:
            if w.chip == "ym2612" and w.chip_id == 0 and not (w.reg & 0x100):
                reg.setdefault(w.reg & 0xFF, set()).add(w.val)
        for op in range(4):
            self.assertIn(0x12 + op, {v & 0x1F for v in reg[0x70 + op * 4]})
            self.assertIn(0x05 + op, {v & 0x0F for v in reg[0x90 + op * 4]})

    def test_dt_four_is_negative_zero(self):
        ws = _fm_note(4, 1232, dt=(4, 4, 4, 4))
        vgm = self._export(analyze(_vgm(ws)), "ym2612_dt4")
        for op in range(4):
            vals = {
                w.val for w in vgm.writes
                if w.chip == "ym2612" and w.chip_id == 0
                and not (w.reg & 0x100) and (w.reg & 0xFF) == 0x30 + op * 4
            }
            self.assertTrue(vals)
            self.assertEqual({v >> 4 for v in vals}, {0})

    def test_alg4_effective_carrier_levels_reach_the_chip(self):
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2612", 0, 0xB0, 0x04),
            ChipWrite(100, "ym2612", 0, 0x40, 0x10),  # S1 modulator
            ChipWrite(100, "ym2612", 0, 0x44, 0x40),  # S3 modulator
            ChipWrite(100, "ym2612", 0, 0x48, 0x20),  # S2 carrier
            ChipWrite(100, "ym2612", 0, 0x4C, 0x30),  # S4 carrier
            ChipWrite(100, "ym2612", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2612", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x00),
        ]
        vgm = self._export(analyze(_vgm(ws)), "ym2612_alg4")
        reg: dict[int, set[int]] = {}
        for w in vgm.writes:
            if w.chip == "ym2612" and w.chip_id == 0 and not (w.reg & 0x100):
                reg.setdefault(w.reg & 0xFF, set()).add(w.val)
        self.assertIn(0x10, reg[0x40])
        self.assertIn(0x40, reg[0x44])
        self.assertIn(0x20, reg[0x48])
        self.assertIn(0x30, reg[0x4C])

    def test_pms_ams_and_carrier_level_reach_the_chip(self):
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2612", 0, 0xB4, 0xD3),   # L/R, AMS 1, PMS 3
            ChipWrite(100, "ym2612", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2612", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x00),
            # A second note with carrier TL 0x30: the instrument stores the
            # relative level and the volume column carries the absolute one.
            ChipWrite(70000, "ym2612", 0, 0x4C, 0x30),
            ChipWrite(70000, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(80000, "ym2612", 0, 0x28, 0x00),
        ]
        vgm = self._export(analyze(_vgm(ws)), "ym2612_pms")
        reg: dict[int, set[int]] = {}
        for w in vgm.writes:
            if w.chip == "ym2612" and w.chip_id == 0 and not (w.reg & 0x100):
                reg.setdefault(w.reg & 0xFF, set()).add(w.val)
        self.assertIn(0xD3, reg[0xB4])
        # Carrier TL 0 at vol 127, then TL 0x30 at vol 127-0x30.
        self.assertIn(0x00, reg[0x4C])
        self.assertIn(0x30, reg[0x4C])

    def test_lfo_effect_sets_register_22(self):
        ws = _patch(100)
        ws += [
            ChipWrite(100, "ym2612", 0, 0x22, 0x0B),
            ChipWrite(100, "ym2612", 0, 0xA4, 0x25),
            ChipWrite(100, "ym2612", 0, 0xA0, 0xBA),
            ChipWrite(100, "ym2612", 0, 0x28, 0xF0),
            ChipWrite(60000, "ym2612", 0, 0x28, 0x00),
        ]
        vgm = self._export(analyze(_vgm(ws)), "ym2612_lfo")
        vals = {
            w.val for w in vgm.writes
            if w.chip == "ym2612" and not (w.reg & 0x100)
            and (w.reg & 0xFF) == 0x22
        }
        self.assertIn(0x0B, vals)


class TestYm2612DacSourceSamples(unittest.TestCase):
    """Bank reads reused by offset instead of one sample per time slice."""

    def test_short_read_is_a_prefix_of_the_longer_bank_run(self):
        body = bytes(0x80 + i for i in range(64))
        events = [(0, 0, 0)]
        events += [(i * 4, 1, 4) for i in range(64)]
        events += [(20000, 0, 0)]
        events += [(20000 + i * 4, 1, 4) for i in range(16)]
        events += [(40000, 0, 64)]
        events += [(40000 + i * 4, 1, 4) for i in range(8)]
        src = _vgm(
            [ChipWrite(0, "ym2612", 0, 0x22, 0)],
            dac_data=bytearray(body + bytes(0x80 + i for i in range(8))),
            dac_events=events, total_samples=60000,
        )
        samples, plays, warn, stats = collect_dac(src)
        self.assertFalse(warn)
        self.assertEqual(stats["dac_mode"], "source")
        self.assertEqual(len(samples), 2)
        self.assertEqual(len(plays), 3)
        self.assertEqual(len(samples[0].data), 64)
        song = analyze(src, speed=6)
        # The 16-byte read reuses the 64-byte body and is cut by the next
        # note.  With only two bodies there is no reason to cluster further,
        # so the distinct 8-byte one-shot keeps its own slot.
        self.assertEqual(len(song.dac_samples), 2)
        self.assertEqual(song.dac_count, 3)

    def test_similar_bodies_move_the_note_by_their_offset(self):
        # The same bank audio reached from a different row position has a
        # few frames of extra leading fill. One body serves both, and the
        # shorter play's note moves so the shared main waveform stays put.
        common = bytes((i * 7) % 251 for i in range(400))
        long = DacSample(
            0, 20 + len(common), b"\x00" * 20 + common, 44100, 0,
        )
        short = DacSample(
            0, 5 + len(common), b"\x09" * 5 + common, 44100, 0,
        )
        notes = [(1000, 0, 255), (5000, 1, 255)]
        samples, moved, offs = _cluster_similar_samples(
            [long, short], notes, [(1, 9000)], {},
        )
        self.assertEqual(samples, [long, short])
        self.assertEqual({idx for _t, idx, _p in moved}, {0})
        self.assertEqual(moved[0], (1000, 0, 255))
        # b[i] == a[i + 15], so the note starts 15 frames earlier at 44.1k.
        self.assertEqual(moved[1], (4985, 0, 255))
        self.assertEqual(offs, [(1, 8985)])

    def test_unique_contiguous_samples_become_one_body(self):
        first = DacSample(0, 4, b"\x01\x02\x03\x04", 1000, 0)
        second = DacSample(0, 4, b"\x05\x06\x07\x08", 1000, 0)
        notes = [(0, 0, 255), (176, 1, 255)]
        samples, merged, offs = _merge_one_shot_samples(
            [first, second], notes, [(0, 176), (1, 352)], {},
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][0], 0)
        self.assertEqual(merged[0][2], 255)
        self.assertEqual(samples[merged[0][1]].data,
                         b"\x01\x02\x03\x04\x05\x06\x07\x08")
        # The intermediate note's cut is gone; the chain's own end stays.
        self.assertEqual(offs, [(0, 176)])

    def test_long_held_gap_splits_the_bank_run(self):
        # Offset 0 is a kick; the driver then holds silence for 20,000
        # samples and continues at offset 4 with a snare. The post part must
        # keep its own offset and onset, not play 0.45 s late inside the kick.
        run = {
            "t": 0, "off": 0, "fresh": False, "pitch": 11025,
            "bytes": [1, 2, 3, 0, 9, 9, 9, 9],
            "times": [0, 4, 8, 12, 20012, 20016, 20020, 20024],
        }
        stats: dict = {}
        pieces = _split_silent_runs([run], stats)
        self.assertEqual(stats["dac_source_silence_splits"], 1)
        self.assertEqual(len(pieces), 2)
        pre, post = pieces
        self.assertEqual((pre["t"], pre["off"], pre["bytes"]),
                         (0, 0, [1, 2, 3, 0]))
        self.assertEqual((post["t"], post["off"], post["bytes"]),
                         (20012, 4, [9, 9, 9, 9]))

    def test_body_over_40_percent_silence_is_split(self):
        body = b"\x01" * 1000 + b"\x00" * 4500 + b"\x02" * 4500
        smp = DacSample(0, len(body), body, 44100, 0)
        notes = [(0, 0, 255)]
        samples, moved, offs = _split_long_silence(
            [smp], notes, [], {},
        )
        self.assertEqual(len(samples), 3)   # original plus the two parts
        self.assertEqual(len(moved), 2)
        self.assertEqual(moved[0][0], 0)
        self.assertEqual(moved[1][0], 5500)  # 1,000 + 4,500 frames at 44.1k
        self.assertEqual(samples[moved[0][1]].data, b"\x01" * 1000)
        self.assertEqual(samples[moved[1][1]].data, b"\x02" * 4500)
        self.assertEqual(offs, [])

    def test_short_body_keeps_its_silence(self):
        # The 8,192-frame floor is the whole body, not the silent run.
        body = b"\x01" * 2000 + b"\x00" * 3000
        smp = DacSample(0, len(body), body, 44100, 0)
        samples, moved, _offs = _split_long_silence(
            [smp], [(0, 0, 255)], [], {},
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(moved, [(0, 0, 255)])

    def test_over_budget_tail_is_one_final_sample(self):
        # When the chunk fallback still needs more than the budget, the whole
        # remaining sequence goes into the last sample instead of vanishing.
        from unittest.mock import patch
        import vgm2151fur.ym2612dac as dac

        events = []
        t = 0
        for k in range(10):
            # Vary the bytes inside each hit: a constant body is a flat DC
            # line and is now dropped before the sample map.
            events += [
                (t + i * 4, 2, 0x91 + ((k + i) % 7)) for i in range(20)
            ]
            t += 20 * 4 + 3000
        src = _vgm(
            [ChipWrite(0, "ym2612", 0, 0x2B, 0x80)],
            dac_events=events, total_samples=t + 1000,
        )
        with patch.object(dac, "DAC_RELEASE_SAMPLES", 4):
            samples, plays, warn, stats = dac.collect_dac(src)
        self.assertLessEqual(len(samples), 4)
        # The ten hits share seven distinct bodies (the body repeats every
        # seven hits); three stay exact and the four distinct tail bodies
        # join the final sample instead of being dropped.
        self.assertEqual(stats.get("dac_release_merged_tail"), 4)
        self.assertFalse(stats.get("dac_release_trimmed"))
        self.assertLessEqual(len(plays), 10)
        self.assertTrue(plays)
        self.assertTrue(any(
            "joined into the final sample" in w for w in warn
        ))

    def test_dac_disable_splits_the_pointer_run(self):
        # Gunstar Heroes holds the pointer while $2B turns the DAC off and on
        # again. The post-enable bytes resume further into the bank, so they
        # are a new source segment; a single held run would lose the snare.
        bank = bytes(0x80 + i for i in range(64))
        events = [(0, 0, 0)]
        events += [(i * 4, 1, 4) for i in range(32)]
        events += [(20000 + i * 4, 1, 4) for i in range(32)]
        writes = [
            ChipWrite(0, "ym2612", 0, 0x22, 0),
            ChipWrite(0, "ym2612", 0, 0x2B, 0x80),
            ChipWrite(128, "ym2612", 0, 0x2B, 0x00),
            ChipWrite(20000, "ym2612", 0, 0x2B, 0x80),
        ]
        src = _vgm(
            writes, dac_data=bytearray(bank), dac_events=events,
            total_samples=40000,
        )
        samples, plays, warn, stats = collect_dac(src)
        self.assertFalse(warn)
        self.assertEqual(stats["dac_mode"], "source")
        self.assertEqual(stats["dac_source_runs"], 2)
        self.assertEqual(len(samples), 2)
        self.assertEqual([s.start for s in samples], [0, 32])
        self.assertEqual(sorted(p.onset for p in plays), [0, 20000])
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_count, 2)
        self.assertEqual([e.note for e in song.events if getattr(e, "dac", False)],
                         [60, 61])


class TestYm2612DacBudgetReuse(unittest.TestCase):
    """Drivers that re-read one bank body stay inside the release budget."""

    def test_repeated_bank_reads_share_one_sample(self):
        # A drum driver points back at the same bank offset for every hit.
        # Each run must reuse the stored body by offset instead of adding a
        # module sample per hit, or a busy track needs hundreds of slots.
        body = bytes(0x80 + (i % 31) for i in range(64))
        events: list[tuple[int, int, int]] = []
        for hit in range(40):
            t = hit * 5000
            events.append((t, 0, 0))
            events += [(t + i * 4, 1, 4) for i in range(8)]
        src = _vgm(
            _fm_note(4, 1204, t=0, off=500),
            dac_data=bytearray(body), dac_events=events,
            total_samples=210000,
        )
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_stats.get("dac_mode"), "source")
        self.assertEqual(len(song.dac_samples), 1)
        self.assertGreater(song.dac_count, len(song.dac_samples))
        self.assertLessEqual(len(song.dac_samples), DAC_RELEASE_SAMPLES)
        self.assertFalse(song.dac_stats.get("dac_release_trimmed"))
        self.assertFalse(any("dropped to stay inside" in w for w in song.warnings))


class TestYm2612DacBanking(unittest.TestCase):
    """Bank bodies keep the longest read and drop unusable candidate slots."""

    @staticmethod
    def _bank_events(spec):
        """``dac_events`` for ``[(time, offset, count, wait)]``.

        Each entry points the bank cursor at ``offset`` and reads ``count``
        bytes from that fixed address, so the same bank body is deliberately
        read at two different cadences.
        """
        events: list[tuple[int, int, int]] = []
        for t, offset, count, wait in spec:
            events.append((t, 0, offset))
            events += [(t + i * wait, 1, wait) for i in range(count)]
        return events

    @staticmethod
    def _marker() -> list[ChipWrite]:
        return [ChipWrite(0, "ym2612", 0, 0x2B, 0x80)]

    def test_cadence_jitter_shares_the_longest_bank_body(self):
        # The same bank offset is read long and short, both at the same
        # nominal cadence with the usual 4/5-sample wait jitter.  The trimmed
        # mean keeps them one cluster; the longest read must serve both and
        # the short note is cut later.
        body = bytes(0x80 + (i % 37 + 1) for i in range(64))
        events: list[tuple[int, int, int]] = [(0, 0, 0)]
        t = 0
        for i in range(64):
            wait = 4 if i % 2 == 0 else 5
            events.append((t, 1, wait))
            t += wait
        events.append((20000, 0, 0))
        t = 20000
        for i in range(16):
            wait = 5 if i % 2 == 0 else 4
            events.append((t, 1, wait))
            t += wait
        src = _vgm(
            self._marker(), dac_data=bytearray(body),
            dac_events=events, total_samples=40000,
        )
        samples, plays, warn, stats = collect_dac(src)
        # Mixed 4/5 waits necessarily collide on a 4-sample frame grid; that
        # is the rate-exact layout warning, not a clustering failure.
        self.assertFalse([
            message for message in warn
            if "rounded to an already-written" not in message
        ])
        self.assertEqual(stats["dac_mode"], "source")
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 2)
        self.assertEqual({p.sample for p in plays}, {0})
        self.assertEqual(stats["dac_source_runs_reused"], 1)

    def test_flatline_and_near_silent_bodies_take_no_slot(self):
        bank = bytearray()
        bank += bytes(0x80 + (i % 29 + 1) for i in range(16))  # audible
        bank += bytes([0x90]) * 16                             # flat DC
        bank += bytes(0x80 + (i % 2) for i in range(16))       # peak 1
        events: list[tuple[int, int, int]] = []
        events += [(0, 0, 0)] + [(i * 4, 1, 4) for i in range(16)]
        events += [(10000, 0, 16)]
        events += [(10000 + i * 4, 1, 4) for i in range(16)]
        events += [(20000, 0, 32)]
        events += [(20000 + i * 4, 1, 4) for i in range(16)]
        src = _vgm(
            self._marker(), dac_data=bank, dac_events=events,
            total_samples=30000,
        )
        samples, plays, _warn, stats = collect_dac(src)
        self.assertEqual(stats["dac_mode"], "source")
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 1)
        self.assertGreater(stats.get("dac_source_quiet_runs", 0), 0)

    def test_attack_fallback_reuses_repeated_hits(self):
        # Direct 0x2A writes, so there is no bank offset to bank on.  The
        # fallback still has to split at the rests and store the repeated hit
        # once instead of cutting the whole stream into time chunks.
        hit = bytes(0x90 + (i % 7) for i in range(20))
        events: list[tuple[int, int, int]] = []
        t = 0
        for _ in range(6):
            events += [(t + i * 4, 2, hit[i]) for i in range(len(hit))]
            t += len(hit) * 4 + 4000
        src = _vgm(
            self._marker(), dac_events=events, total_samples=t + 1000,
        )
        samples, plays, warn, stats = collect_dac(src)
        self.assertFalse(warn)
        self.assertEqual(stats["dac_mode"], "attacks")
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 6)
        self.assertEqual(stats.get("dac_attack_segments"), 6)

    def test_attack_fallback_prefers_a_longer_prefix_body(self):
        # The first hit is a prefix of the second at the same rate; the short
        # play must point at the longer body and be cut by its own span.
        body = bytes(0x90 + (i % 7) for i in range(32))
        events: list[tuple[int, int, int]] = []
        t = 0
        for length in (16, 32):
            events += [(t + i * 4, 2, body[i]) for i in range(length)]
            t += length * 4 + 4000
        src = _vgm(
            self._marker(), dac_events=events, total_samples=t + 1000,
        )
        samples, plays, _warn, stats = collect_dac(src)
        self.assertEqual(stats["dac_mode"], "attacks")
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(plays), 2)
        self.assertEqual({p.sample for p in plays}, {0})
        self.assertEqual(stats.get("dac_attack_prefix_reused"), 1)


class TestYm2612DacFragmentClustering(unittest.TestCase):
    """A busy DAC map collapses to the hottest/longest sample anchors."""

    @staticmethod
    def _body(seed: int) -> bytes:
        value = seed * 1234567 + 1
        out = []
        for _ in range(64):
            value = (value * 1103515245 + 12345) & 0x7FFFFFFF
            out.append((value >> 16) & 0xFF)
        return bytes(out)

    @classmethod
    def _song(cls):
        samples = [
            DacSample(i, 64, cls._body(i), 14700, i * 4410)
            for i in range(40)
        ]
        plays = []
        # Sample 0 is the hot drum: ten uses.  The rest are one-shots.
        for hit in range(10):
            plays.append(DacPlay(
                hit * 4410, 0, (hit * 37) & 0xFF, 1000, False, True,
            ))
        for i in range(1, 40):
            plays.append(DacPlay(
                (10 + i) * 4410, i, (i * 53) & 0xFF, 1000, False, True,
            ))
        plays.sort(key=lambda p: p.onset)
        return samples, plays

    def test_sample_map_stays_within_the_target_when_enabled(self):
        samples, plays = self._song()
        out, notes, _offs, _pans, warn, stats = group_dac_plays(
            samples, plays, hz=60.0, speed=6, total_samples=300000,
            trim=False, cluster_fragments=True,
        )
        self.assertFalse(warn)
        self.assertLessEqual(len(out), DAC_TARGET_SAMPLES)
        self.assertEqual(len(notes), len(plays))
        self.assertTrue(all(0 <= idx < len(out) for _t, idx, _p in notes))
        # The ten-use body is one of the anchors.
        self.assertIn(
            bytes(samples[0].data),
            {bytes(smp.data) for smp in out},
        )
        self.assertGreaterEqual(stats.get("dac_fragment_clustered", 0), 1)

    def test_clustering_is_off_by_default(self):
        samples, plays = self._song()
        out, notes, _offs, _pans, warn, _stats = group_dac_plays(
            samples, plays, hz=60.0, speed=6, total_samples=300000,
            trim=False,
        )
        self.assertFalse(warn)
        self.assertGreater(len(out), DAC_TARGET_SAMPLES)
        self.assertEqual(len(notes), len(plays))


class TestYm2612DacPitchClusters(unittest.TestCase):
    """One bank offset used at several real rates gets several samples."""

    @staticmethod
    def _run(t: int, waits: list[int], seed: int) -> dict:
        times = [t]
        for wait in waits:
            times.append(times[-1] + wait)
        return {
            "t": t,
            "off": 1515,
            "bytes": [
                (0x80 + (seed * 17 + i * 7) % 61)
                for i in range(len(waits) + 1)
            ],
            "times": times,
            "fresh": False,
        }

    def test_jitter_merges_but_a_real_pitch_step_splits(self):
        runs = [
            self._run(0, [4, 5] * 8, 1),
            self._run(10_000, [5, 4] * 8, 2),
            self._run(20_000, [6, 7] * 8, 3),
            self._run(30_000, [7, 6] * 8, 4),
        ]
        samples, plays = _collect_source(runs, {}, [])
        self.assertEqual(len(samples), 2)
        self.assertEqual(
            [p.sample for p in sorted(plays, key=lambda p: p.onset)],
            [0, 0, 1, 1],
        )
        self.assertEqual(
            {samples[0].c4_rate, samples[1].c4_rate}, {11025, 7350},
        )


class TestYm2612DacLongHolds(unittest.TestCase):
    """A bank read held through a long rest must not carry it into a sample."""

    @staticmethod
    def _bad_silence(song):
        import vgm2151fur.ym2612dac as dac
        bad = []
        for i, smp in enumerate(song.dac_samples):
            if len(smp.data) < dac.DAC_SILENCE_MIN_LEN:
                continue
            run, _start = dac._longest_silence(smp.data)
            if run > len(smp.data) * dac.DAC_SILENCE_FRACTION:
                bad.append((i, len(smp.data), run))
        return bad

    @staticmethod
    def _bank_run() -> tuple[bytearray, list[tuple[int, int, int]]]:
        """One pointer run: a kick, a held silence, then a snare."""
        kick = bytes(0x80 + v for v in (9, 20, 30, 20))
        snare = bytes(0x80 + v for v in (40, 60, 40, 20))
        bank = bytearray(kick + b"\x80" + snare)
        events = [(0, 0, 0)]
        events += [(i * 4, 1, 4) for i in range(4)]
        events.append((1000, 1, 4))  # the silent byte the DAC holds
        events += [(5000 + i * 4, 1, 4) for i in range(4)]
        # The driver then reads the same bank range again later.
        events += [(20000, 0, 0)]
        events += [(20000 + i * 4, 1, 4) for i in range(4)]
        events.append((21000, 1, 4))
        events += [(25000 + i * 4, 1, 4) for i in range(4)]
        return bank, events

    def test_held_gap_splits_the_source_run(self):
        bank, events = self._bank_run()
        src = _vgm(
            [ChipWrite(0, "ym2612", 0, 0x22, 0)],
            dac_data=bank, dac_events=events, total_samples=50000,
        )
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_stats.get("dac_mode"), "source")
        self.assertGreaterEqual(
            song.dac_stats.get("dac_source_silence_splits", 0), 1,
        )
        self.assertEqual(self._bad_silence(song), [])
        notes = sorted(e.sample for e in song.events if e.dac and e.on)
        # The snare after the hold is its own note, not a late byte inside
        # the kick's body.
        self.assertTrue(any(t >= 4000 for t in notes), notes)
        self.assertFalse(song.dac_stats.get("dac_release_trimmed"))

    def test_dense_silent_stretch_leaves_the_stored_body(self):
        # The reads stay dense through the rest, so the source splitter does
        # not see a gap; the stored body itself has to give the silence back.
        pre = bytes(0x80 + (i % 30 + 1) for i in range(1000))
        mid = bytes([0x80] * 5000)
        post = bytes(0x80 + (i % 30 + 1) for i in range(4500))
        bank = bytearray(pre + mid + post)
        events = [(0, 0, 0)]
        events += [(i * 4, 1, 4) for i in range(len(bank))]
        src = _vgm(
            [ChipWrite(0, "ym2612", 0, 0x22, 0)],
            dac_data=bank, dac_events=events, total_samples=30000,
        )
        song = analyze(src, speed=6)
        self.assertGreaterEqual(
            song.dac_stats.get("dac_silence_split_samples", 0), 1,
        )
        self.assertEqual(self._bad_silence(song), [])
        self.assertLess(
            sum(len(s.data) for s in song.dac_samples), len(bank),
        )


class TestYm2612DacUnusedSamples(unittest.TestCase):
    def test_source_mode_drops_a_sample_no_row_selects(self):
        # A DAC note and an FM6 note can land on the same cell. The later FM6
        # key wins the pattern cell, so the body stored for the lost DAC note
        # is never played. The writer must remove that slot (and keep the
        # surviving one) instead of shipping an unreachable sample.
        writes = [
            ChipWrite(0, "ym2612", 0, 0x2B, 0x80),   # DAC on
        ] + _patch(0, 0, 1, 2) + [
            ChipWrite(0, "ym2612", 0, 0x1A4, (4 << 3)),
            ChipWrite(0, "ym2612", 0, 0x1A0, 0x00),
            ChipWrite(20, "ym2612", 0, 0x2B, 0x00),  # DAC off
            ChipWrite(21, "ym2612", 0, 0x28, 0xF6),  # FM6 takes the row
            ChipWrite(40000, "ym2612", 0, 0x28, 0x06),
            ChipWrite(50000, "ym2612", 0, 0x2B, 0x80),
        ]
        bank = bytearray(bytes(0x80 + v for v in (9, 20, 30, 20)))
        bank += b"\x80"
        bank += bytes(0x80 + v for v in (40, 60, 40, 20))
        events = [(2, 0, 0)]
        events += [(2 + i * 2, 1, 2) for i in range(4)]
        events.append((1000, 1, 2))
        events += [(50002 + i * 4, 1, 4) for i in range(4)]
        src = _vgm(
            writes, dac_data=bank, dac_events=events, total_samples=60000,
        )
        song = analyze(src, speed=6)
        self.assertEqual(song.dac_stats.get("dac_mode"), "source")
        self.assertEqual(len(song.dac_samples), 2)
        self.assertEqual(song.dac_count, 2)

        stats: dict = {}
        data = write_fur(song, include_pcm=True, stats=stats)
        mod = parse_fur(data)
        unused = stats.get("dac_samples_unused", 0)
        self.assertGreaterEqual(unused, 1)
        self.assertGreater(stats.get("note_collision", 0), 0)
        self.assertEqual(
            len(mod.samples), len(song.dac_samples) - unused,
        )
        base = len(song.fm_patches)
        n_ins = (len(song.dac_samples) + 119) // 120
        used = set()
        for (_ch, _pat), rows in mod.patterns.items():
            for row in rows:
                if 60 <= row.note < 180 and base <= row.ins < base + n_ins:
                    used.add((row.ins - base) * 120 + (row.note - 60))
        self.assertEqual(used, set(range(len(mod.samples))))


class TestSampleMapCleanup(unittest.TestCase):
    def test_unused_slots_are_compacted_and_later_instruments_shift(self):
        rows = [
            SimpleNamespace(note=60, ins=0),
            SimpleNamespace(note=62, ins=0),
            SimpleNamespace(note=60, ins=2),
        ]
        patterns = [(0, 0, rows)]
        samples = list(range(240))
        samples2, count = _compact_sample_map_notes(
            patterns, 0, 2, samples,
        )
        self.assertEqual(samples2, [0, 2])
        self.assertEqual(count, 1)
        self.assertEqual((rows[0].note, rows[0].ins), (60, 0))
        self.assertEqual((rows[1].note, rows[1].ins), (61, 0))
        self.assertEqual((rows[2].note, rows[2].ins), (60, 2))
        _shift_sample_map_instruments(patterns, 2, -1)
        self.assertEqual((rows[2].note, rows[2].ins), (60, 1))


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestYm2612PitchRoundTrip(unittest.TestCase):
    """Every OPN2 channel keeps its source pitch through Furnace."""

    def test_all_channels_survive_the_round_trip(self):
        notes = (
            (4, 1204, 0), (3, 923, 1), (2, 702, 2),
            (4, 1232, 3), (3, 1466, 4), (4, 1100, 5),
        )
        ws: list[ChipWrite] = []
        for block, fnum, ch in notes:
            t = 100 + ch * 12000
            ws += _fm_note(block, fnum, t=t, off=t + 6000, ch=ch)
        src = _vgm(ws, total_samples=80000)
        song = analyze(src)
        self.assertFalse(song.warnings, song.warnings)
        fur = TMP / "ym2612_pitch_round_trip.fur"
        fur.write_bytes(write_fur(song, include_pcm=False))
        out = TMP / "ym2612_pitch_round_trip.vgm"
        export_vgm(fur, out)
        back = load_vgm(out)
        for ch in range(6):
            self.assertEqual(
                pitch_runs(src, back, ch, threshold_st=0.25, min_run_ms=60),
                [], f"ch{ch}",
            )


if __name__ == "__main__":
    unittest.main()
