#!/usr/bin/env python3
"""Smoke tests for the YM2151+K007232 VGM → Furnace helper."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _synth import inject_rows, note_song
from vgm2151fur import furwrite as fw
from vgm2151fur.analyze import (
    Song, kc_to_furnace_note, furnace_note_name, analyze, vol15, kf_to_e5, pcm_pan_fx,
    FMPatch, NoteEvent, OpRegs,
)
from vgm2151fur.convert import (
    CONVERT_WORKERS, convert_folder, convert_vgm, select_tracks, track_number,
)
from vgm2151fur.furwrite import (
    CHIP_K007232, CHIP_MSM6258, CHIP_MSM6295, CHIP_YM2151,
    FUR_MAGIC, FUR_VERSION, parse_fur_skeleton, NOTE_OFF, ym_dt_to_furnace, pack_dt_mul,
    write_fur,
)
from vgm2151fur.pcm import (
    PcmHit, PcmSample, dump_pcm as dump_pcm_files, k007232_rate, pcm7_to_s8,
    scale_pcm7,
)
from vgm2151fur.furio import fx_pan_amps, list_fur_samples, load_fur_bytes, parse_fur
from vgm2151fur.oki import msm6258_div_fx, oki_flag_text, oki_furnace_vol, oki_rate
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile, load_vgm

# The VGM packs live outside the project. Tests that need one skip when it is
# absent; point VGM2151FUR_FIXTURES at the folder that holds the packs.
REPO = ROOT.parent
FIXTURES = Path(os.environ.get("VGM2151FUR_FIXTURES", REPO))


def _pack(name: str) -> Path:
    spaced, underscored = FIXTURES / name, FIXTURES / name.replace(" ", "_")
    return spaced if spaced.exists() else underscored


G2 = _pack("Gradius II AC Rip")
G3 = _pack("Gradius_III_(Arcade)")
COLLECTION = _pack("VGM collection")
G2_TITLE = G2 / "01 Title Demo.vgm"
G2_TABIDACHI = G2 / "04 Tabidachi (Air Battle 1).vgm"
G2_BURNING = G2 / "05 Burning Heat (Stage 1 BGM).vgm"
G2_STONE = G2 / "10 The Old Stone Age 1 ~ 2 (Stage 5 BGM).vgm"
G3_SAND = G3 / "06 Sand Storm (Stage 1 BGM).vgz"
BG_STAGE1 = (
    COLLECTION / "old" / "Battle_Garegga_(Toaplan_2)"
    / "06 Fly to the Leaden Sky [Stage 1 Valley].vgz"
)


class TestKC(unittest.TestCase):
    def test_a4(self):
        # Official table: octave 4, nibble 10 (A) = A-4 = furnace 117
        self.assertEqual(kc_to_furnace_note(0x4A), 117)
        self.assertEqual(furnace_note_name(117), "A-4")
        # nibble 8 is G, not A
        self.assertEqual(kc_to_furnace_note(0x48), 115)
        self.assertEqual(furnace_note_name(115), "G-4")

    def test_c5_from_note14(self):
        # Konami / Yamaha: nibble 14 = C of the next octave. 0x4E = C-5.
        self.assertEqual(kc_to_furnace_note(0x4E), 120)
        self.assertEqual(furnace_note_name(120), "C-5")
        self.assertEqual(kc_to_furnace_note(0x3E), 108)  # C-4
        self.assertEqual(furnace_note_name(108), "C-4")

    def test_cs4(self):
        self.assertEqual(kc_to_furnace_note(0x40), 109)
        self.assertEqual(furnace_note_name(109), "C#4")


class TestK007232Rate(unittest.TestCase):
    def test_typical_drum_pitch(self):
        hz = k007232_rate(3579545, 4051)
        self.assertGreater(hz, 15000)
        self.assertLess(hz, 25000)

    def test_s8_center(self):
        self.assertEqual(pcm7_to_s8(b"\x40")[0], 0)

    def test_scale_pcm7_preserves_center(self):
        self.assertEqual(scale_pcm7(b"\x40\x7f", 1.0), b"\x40\x7f")
        scaled = scale_pcm7(b"\x40\x7f", 0.72)
        self.assertEqual(scaled[0], 0x40)
        self.assertLess(scaled[1], 0x7F)
        self.assertGreater(scaled[1], 0x40)


class TestDTRemap(unittest.TestCase):
    def test_furnace_dt_table_roundtrip(self):
        # arcade.cpp: rWrite(DT, dtTable[op.dt]<<4) with dtTable = 7,6,5,0,1,2,3,4
        dt_table = (7, 6, 5, 0, 1, 2, 3, 4)
        for ym in range(8):
            fur = ym_dt_to_furnace(ym)
            self.assertEqual(dt_table[fur], ym, msg=f"YM DT {ym}")

    def test_pack_keeps_mult(self):
        self.assertEqual(pack_dt_mul(0x01) & 0x0F, 1)
        self.assertEqual(pack_dt_mul(0x73) & 0x0F, 3)


class TestVolAndKF(unittest.TestCase):
    def test_k007232_volume_is_linear_amplitude(self):
        # MAME mixes a voice as sample * (vol * 2) / 32768, so 255 is full
        # scale and the column has to be linear in the register.
        self.assertEqual(vol15(0), 0)
        self.assertEqual(vol15(255), 15)
        self.assertEqual(vol15(136), 8)  # 0x88, the common Twin16 step
        self.assertEqual(vol15(34), 2)  # 0x22
        self.assertEqual(vol15(17), 1)  # 0x11
        self.assertEqual(vol15(153), 9)  # 0x99 is not full scale
        self.assertLess(vol15(34), vol15(136))
        self.assertLess(vol15(136), vol15(255))

    def test_k007232_volume_honours_a_0_15_preset_scale(self):
        # Some drivers write a 0-15 preset instead of the register; the file
        # is scaled by its loudest write (see analyze).
        self.assertEqual(vol15(15, 15.0), 15)
        self.assertEqual(vol15(7, 15.0), 7)
        self.assertEqual(vol15(0, 15.0), 0)
        self.assertEqual(vol15(255, 255.0), vol15(255))

    def test_pcm_pan_is_furnace_08xy_nibbles(self):
        # 08xy: x = left, y = right. Loud side stays at F so the volume
        # column is not halved. 0x80 is left-at-8 / right-off, not center.
        self.assertEqual(pcm_pan_fx(15, 15), 0xFF)
        self.assertEqual(pcm_pan_fx(15, 0), 0xF0)
        self.assertEqual(pcm_pan_fx(0, 15), 0x0F)
        self.assertEqual(pcm_pan_fx(0, 0), 0x00)
        self.assertEqual(pcm_pan_fx(10, 5), 0xF8)
        self.assertNotEqual(pcm_pan_fx(15, 15), 0x80)

    def test_kf_fraction_is_top_six_bits(self):
        # ymfm: block_freq = (KC << 6) | (KF >> 2): the low 2 bits are ignored.
        # Furnace 0.6.8.3 maps E5 0x80+f to KF fraction f (one unit per step).
        self.assertEqual(kf_to_e5(0x00), 0x80)
        self.assertEqual(kf_to_e5(0x03), 0x80)  # low two bits do not count
        self.assertEqual(kf_to_e5(0x04), 0x81)
        self.assertEqual(kf_to_e5(0x80), 0xA0)  # 32/64 semitone
        self.assertEqual(kf_to_e5(0xFC), 0xBF)  # 63/64 semitone


class TestK007232FurRows(unittest.TestCase):
    """Regression guards for the Gradius II/III PCM rows.

    A symmetric volume pair used to be written as 08 80 (left 8/15, right
    off), which plays on the left ear only and about 6 dB quiet.
    """

    def test_chip_volume_follows_the_sources_mix(self):
        hits = [PcmHit(0, 0, 0x10000, 4000, 0, 0x88, 0x88)]
        smp = PcmSample(start=0x10000, pcm7=bytes([0x40] * 16), hits=hits)
        song = _fake_song("04 Tabidachi (Air Battle 1).vgm", [smp])
        song.k007232_volume = 0.5
        data = write_fur(song, include_pcm=True, include_fm=True)
        # chip output triple (vol, panL, panR) in the .fur
        self.assertIn(struct.pack("<fff", 0.5, 0.0, 0.0), data)

    def test_chip_volume_reads_the_extra_header(self):
        vgm = _fake_song("x.vgz", []).vgm
        vgm.extra_volumes = {0x2A: (0, 0x004D), 0x03: (1, 0x0100)}
        self.assertAlmostEqual(vgm.chip_volume(0x2A), 0x4D / 256)
        self.assertIsNone(vgm.chip_volume(0x03))  # flags set: keep the default
        self.assertIsNone(vgm.chip_volume(0x66))  # absent

    def test_extra_header_volume_is_not_read_as_a_clock(self):
        # The G2 rips carry a volume list that a naive clock parse turns into
        # a 700 MHz K007232. The clock must come from the header instead.
        if not G2_TABIDACHI.is_file():
            self.skipTest("Gradius II rip missing")
        vgm = load_vgm(G2_TABIDACHI)
        self.assertEqual(vgm.k007232_clock, 3579545)
        self.assertAlmostEqual(vgm.chip_volume(0x2A), 0x4D / 256)


@unittest.skipUnless(G2_TABIDACHI.is_file(), "Gradius II rip missing")
class TestK007232RowsOnRealTrack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.song = analyze(load_vgm(G2_TABIDACHI), pcm=True)
        cls.mod = parse_fur(write_fur(cls.song, include_pcm=True, include_fm=True))

    def test_symmetric_hits_pan_both_sides(self):
        pcm = [e for e in self.song.events if e.pcm and e.on]
        self.assertTrue(pcm)
        self.assertTrue(all(e.pan == 0xFF for e in pcm))
        for e in pcm:
            self.assertNotEqual(e.pan, 0x80)

    def test_fur_rows_carry_the_same_pan(self):
        pans = [
            v for (_ch, _idx), rows in self.mod.patterns.items()
            if _ch in self.mod.pcm_channels
            for r in rows for c, v in r.fx if c == 0x08
        ]
        self.assertTrue(pans)
        self.assertEqual(set(pans), {0xFF})

    def test_volume_column_is_linear_in_the_register(self):
        columns = sorted({
            e.vol for e in self.song.events if e.pcm and e.on and e.vol > 0
        })
        self.assertEqual(columns, [2, 4, 6, 7, 8])  # 0x22, 0x44, 0x66, 0x77, 0x88


@unittest.skipUnless(G2_BURNING.is_file(), "Gradius II rip missing")
class TestBurningHeat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vgm = load_vgm(G2_BURNING)

    def test_rom_block(self):
        self.assertTrue(any(s.rom_size == 131072 for s in self.vgm.roms))
        self.assertGreater(len(self.vgm.roms[0].data), 1000)

    def test_analyze_has_melody_and_pcm(self):
        song = analyze(self.vgm, pcm=True)
        self.assertGreaterEqual(len(song.fm_patches), 5)
        self.assertLessEqual(len(song.fm_patches), 24)
        ons = [e for e in song.events if e.on and not e.pcm]
        self.assertGreater(len(ons), 200)
        pcm_ons = [e for e in song.events if e.on and e.pcm]
        self.assertGreater(len(pcm_ons), 50)
        self.assertGreaterEqual(len(song.pcm_samples), 8)

    def test_pcm_dump(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            idx = dump_pcm_files(self.vgm, Path(td))
            self.assertGreaterEqual(len(idx["samples"]), 8)
            wav = Path(td) / idx["samples"][0]["file"]
            self.assertTrue(wav.is_file())
            self.assertGreater(wav.stat().st_size, 44)

    def test_fur_header(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "burning.fur"
            info = convert_vgm(G2_BURNING, dest, pcm=True)
            data = dest.read_bytes()
            self.assertTrue(data.startswith(FUR_MAGIC))
            ver = struct.unpack_from("<H", data, 16)[0]
            self.assertEqual(ver, FUR_VERSION)
            self.assertIn(b"INFO", data)
            self.assertIn(b"INS2", data)
            self.assertIn(b"PATN", data)
            self.assertIn(b"SMP2", data)
            self.assertIn(b"ADIR", data)
            self.assertGreater(info["fm_patches"], 0)
            self.assertGreater(info["pcm_samples"], 0)
            self.assertGreater(info["bytes"], 10_000)
            skel = parse_fur_skeleton(data)
            self.assertEqual(skel["version"], FUR_VERSION)
            self.assertEqual(skel["first_block"], b"ADIR")
            self.assertIn("INS2", skel["magics"])
            self.assertIn("PATN", skel["magics"])
            self.assertIn("SMP2", skel["magics"])
            self.assertEqual(skel["chips"], [0x82, 0xC6])
            self.assertGreater(skel["notes"], 200)
            self.assertGreater(skel["bxx"], 0)
            self.assertEqual(NOTE_OFF, 180)
            self.assertIn(b"FLAG", data)
            self.assertIn(b"stereo=true", data)
            # Sample presence bitfields must not be zero or K007232 stays silent.
            smp = data.find(b"SMP2")
            self.assertGreater(smp, 0)
            body = data[smp + 8:]
            z = body.find(b"\x00")
            pres = body[z + 1 + 24:z + 1 + 40]
            self.assertEqual(len(pres), 16)
            self.assertNotEqual(pres, b"\x00" * 16)

    def test_long_intro_keeps_notes(self):
        """Loop at ~68s must not collapse the intro into one 64-row order."""
        if not G2_STONE.is_file():
            self.skipTest("missing Old Stone Age rip")
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "stone.fur"
            info = convert_vgm(G2_STONE, dest, pcm=True)
            skel = parse_fur_skeleton(dest.read_bytes())
            self.assertGreater(skel["notes"], int(info["events"] * 0.85))
            self.assertGreater(skel["bxx"], 0)


@unittest.skipUnless(G2_TITLE.is_file(), "Gradius II Title Demo missing")
class TestTitleDemoOPM(unittest.TestCase):
    def test_delayed_vibrato_and_carrier_tl(self):
        song = analyze(load_vgm(G2_TITLE), pcm=True)
        self.assertTrue(any(c == 0x17 for _s, c, _v in song.chip_fx))
        self.assertTrue(any(c == 0x1F and v > 0 for _s, c, v in song.chip_fx))
        leads = [p for p in song.fm_patches if p.fms_macro and max(p.fms_macro) >= 4]
        self.assertTrue(leads, "Title Demo FM 1/2 must get a delayed PMS macro")
        self.assertEqual(leads[0].fms_macro[0], 0)
        self.assertGreaterEqual(leads[0].macro_speed, 2)
        # Brass / choir-lead carriers must keep their logged TL, not 0.
        self.assertGreater(leads[0].carrier_tl(), 8)

    def test_fur_has_lfo_effects_and_nonzero_tl(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "title.fur"
            convert_vgm(G2_TITLE, dest, pcm=True)
            data = dest.read_bytes()
            skel = parse_fur_skeleton(data)
            self.assertGreater(skel["fx_cmds"].get(0x17, 0), 0)
            self.assertGreater(skel["fx_cmds"].get(0x1F, 0), 0)
            self.assertIn(b"FM", data)
            # First OPM instrument: FM feature ops in file order M1,M2,C1,C2.
            fm = data.find(b"FM", data.find(b"INS2"))
            self.assertGreater(fm, 0)
            body = data[fm + 4:]
            # flags, alg/fb, ams/fms, 4-op byte, then 4×8 op bytes. TL is byte 1.
            tls = [body[4 + i * 8 + 1] for i in range(4)]
            self.assertGreater(max(tls), 8)
            self.assertIn(b"\x0a", data[data.find(b"MA"): data.find(b"MA") + 80])  # FMS macro code 10


@unittest.skipUnless(G3_SAND.is_file(), "Gradius III pack missing")
class TestGradius3Vgz(unittest.TestCase):
    def test_sliced_rom(self):
        vgm = load_vgm(G3_SAND)
        types = {s.rom_size for s in vgm.roms}
        self.assertTrue(vgm.roms)
        song = analyze(vgm, pcm=True)
        self.assertGreater(len(song.fm_patches), 3)
        self.assertGreater(len(song.pcm_samples), 3)

    def test_k007232_clock_is_chip_clock_not_volume(self):
        # Extra header 0x4000 is K007232 mix (64/256), not 16384 Hz.
        vgm = load_vgm(G3_SAND)
        self.assertGreaterEqual(vgm.k007232_clock, 100_000)
        self.assertEqual(vgm.k007232_clock, 3579545)
        song = analyze(vgm, pcm=True)
        rates = [s.c4_rate(vgm.k007232_clock) for s in song.pcm_samples if s.length >= 8]
        self.assertTrue(rates)
        self.assertGreater(min(rates), 4000)



class TestTrackSelect(unittest.TestCase):
    def test_number_and_substring(self):
        files = [
            Path("01 Title Demo.vgm"),
            Path("15 Fire Dragon (Stage 7 BGM 3).vgm"),
            Path("04 Tabidachi (Air Battle 1).vgm"),
        ]
        self.assertEqual(track_number(files[1]), 15)
        hit = select_tracks(files, 15)
        self.assertEqual(hit[0].name, files[1].name)
        hit = select_tracks(files, "tabidachi")
        self.assertEqual(len(hit), 1)
        self.assertIn("Tabidachi", hit[0].name)
        with self.assertRaises(FileNotFoundError):
            select_tracks(files, 99)

def _fake_song(stem: str, samples: list[PcmSample]) -> Song:
    vgm = VgmFile(
        path=Path(stem),
        buf=b"",
        version=0x170,
        total_samples=1000,
        loop_samples=0,
        loop_file_pos=None,
        loop_sample=None,
        ym2151_clock=0,
        k007232_clock=3579545,
        gd3={},
    )
    return Song(
        vgm=vgm,
        t0=0,
        loop_sample=None,
        speed=6,
        hz=60.0,
        fm_patches=[],
        pcm_samples=samples,
        events=[],
    )

def _pcm_song(events, *, loop=None, speed=6, hz=60.0):
    vgm = VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x151, total_samples=1,
        loop_samples=0, loop_file_pos=None, loop_sample=loop,
        ym2151_clock=3579545, k007232_clock=3579545, gd3={}, roms=[], writes=[],
    )
    return Song(
        vgm=vgm, t0=0, loop_sample=loop, speed=speed, hz=hz,
        fm_patches=[], pcm_samples=[], events=events,
    )


def _pcm_notes(song, ch=8):
    mod = parse_fur(write_fur(song, include_pcm=True, include_fm=True))
    found = []
    for oi, pat in enumerate(mod.orders[ch]):
        for i, row in enumerate(mod.patterns[(ch, pat)]):
            if 0 <= row.note < 180:
                found.append((oi * mod.pat_len + i, row))
    return found


class TestPcmRowCollisions(unittest.TestCase):
    def test_distinct_times_on_one_row_both_play(self):
        # 4410 samples/row. 100 and 400 share row 0 and used to overwrite.
        song = _pcm_song([
            NoteEvent(sample=100, ch=8, on=True, note=100, ins=0, vol=12, e5=0x80, pan=0xFF, pcm=True),
            NoteEvent(sample=400, ch=8, on=True, note=110, ins=0, vol=12, e5=0x80, pan=0xFF, pcm=True),
            NoteEvent(sample=20000, ch=8, on=True, note=120, ins=0, vol=12, e5=0x80, pan=0xFF, pcm=True),
        ])
        notes = [n for _r, row in _pcm_notes(song) for n in (row.note,)]
        self.assertIn(100, notes)
        self.assertIn(110, notes)
        self.assertEqual(len(notes), 3)

    def test_tight_pair_does_not_steal_the_next_row(self):
        # 4410 samples/row. 100 and 200 share row 0; 4500 is row 1.
        # Pushing 200 onto row 1 would shift every later note on the voice.
        song = _pcm_song([
            NoteEvent(sample=100, ch=8, on=True, note=100, ins=0, vol=12, e5=0x80, pan=0xFF, pcm=True),
            NoteEvent(sample=200, ch=8, on=True, note=110, ins=0, vol=12, e5=0x80, pan=0xFF, pcm=True),
            NoteEvent(sample=4500, ch=8, on=True, note=120, ins=0, vol=12, e5=0x80, pan=0xFF, pcm=True),
        ])
        found = [(row_i, row.note) for row_i, row in _pcm_notes(song)]
        self.assertEqual(found, [(0, 110), (1, 120)])

    def test_same_timestamp_keeps_the_later_trigger(self):
        song = _pcm_song([
            NoteEvent(sample=100, ch=8, on=True, note=100, ins=0, vol=12, e5=0x80, pan=0xFF, pcm=True),
            NoteEvent(sample=100, ch=8, on=True, note=110, ins=0, vol=8, e5=0x80, pan=0xF0, pcm=True),
            NoteEvent(sample=20000, ch=8, on=True, note=120, ins=0, vol=12, e5=0x80, pan=0xFF, pcm=True),
        ])
        early = [row for row_i, row in _pcm_notes(song) if row_i == 0]
        self.assertEqual(len(early), 1)
        self.assertEqual(early[0].note, 110)
        self.assertEqual(early[0].vol, 8)
        self.assertIn((0x08, 0xF0), early[0].fx)

    def test_equal_volumes_write_both_speakers(self):
        song = _pcm_song([
            NoteEvent(sample=0, ch=8, on=True, note=108, ins=0, vol=13, e5=0x80, pan=0xFF, pcm=True),
        ])
        rows = _pcm_notes(song)
        self.assertEqual(len(rows), 1)
        self.assertIn((0x08, 0xFF), rows[0][1].fx)


class TestFurPanAmps(unittest.TestCase):
    def test_08xy_nibbles(self):
        self.assertEqual(fx_pan_amps(0xFF), (1.0, 1.0))
        self.assertEqual(fx_pan_amps(0xF0), (1.0, 0.0))
        self.assertEqual(fx_pan_amps(0x0F), (0.0, 1.0))
        l, r = fx_pan_amps(0x80)
        self.assertAlmostEqual(l, 8 / 15, places=5)
        self.assertEqual(r, 0.0)
        l, r = fx_pan_amps(0x88)
        self.assertAlmostEqual(l, 8 / 15, places=5)
        self.assertAlmostEqual(r, 8 / 15, places=5)

class TestFMOperatorOrder(unittest.TestCase):
    """INS2 FM ops are stored in YM2151 register order (0x40, 0x48, 0x50, 0x58).

    Furnace reads the four operators with a straight loop and arcade.cpp maps
    op[j] to ADDR+opOffs[j] (opOffs = {0,8,0x10,0x18}). File op j must be
    the operator of register 0x40+8j. A permutation here swaps the contents of
    the 0x48 and 0x50 operators in every instrument.
    """

    def test_ops_in_register_order(self):
        vgm = VgmFile(
            path=Path("synthetic.vgm"), buf=b"", version=0x151, total_samples=1,
            loop_samples=0, loop_file_pos=None, loop_sample=None,
            ym2151_clock=3579545, k007232_clock=3579545, gd3={}, roms=[], writes=[],
        )
        ops = tuple(
            OpRegs(
                dt_mul=(((i + 1) & 7) << 4) | (i & 0x0F),
                tl=0x11 + i * 0x11,
                ks_ar=((i & 3) << 6) | (0x1F - i),
                am_d1r=(0x80 if i & 1 else 0) | (i + 3),
                dt2_d2r=((i & 3) << 6) | (i + 5),
                d1l_rr=((i + 1) << 4) | i,
            )
            for i in range(4)
        )
        patch = FMPatch(alg=4, fb=5, ams=2, pms=3, ops=ops)
        song = Song(
            vgm=vgm, t0=0, loop_sample=None, speed=1, hz=60.0,
            fm_patches=[patch], pcm_samples=[],
            events=[NoteEvent(sample=0, ch=0, on=True, note=108, ins=0, vol=-1, e5=0x80, pan=0x80)],
        )
        data = write_fur(song, include_pcm=False, include_fm=True)
        fm = data.find(b"FM", data.find(b"INS2"))
        self.assertGreater(fm, 0)
        body = data[fm + 4:]
        self.assertEqual(body[0] & 0xF0, 0xF0)  # all four operators enabled
        self.assertEqual(body[1], (4 << 4) | 5)  # alg/fb
        self.assertEqual(body[2], (2 << 3) | 3)  # ams/fms
        for i in range(4):
            op = body[4 + i * 8:4 + i * 8 + 8]
            self.assertEqual(
                op[0],
                (ym_dt_to_furnace((ops[i].dt_mul >> 4) & 7) << 4) | (ops[i].dt_mul & 0x0F),
                f"op{i} dt/mul",
            )
            self.assertEqual(op[1], ops[i].tl & 0x7F, f"op{i} tl")
            self.assertEqual(op[2], ops[i].ks_ar & 0xDF, f"op{i} ks/ar")
            self.assertEqual(op[3], ops[i].am_d1r, f"op{i} am/d1r")
            self.assertEqual(op[7], ((ops[i].dt2_d2r >> 6) & 3) << 3, f"op{i} dt2")
        # A swap regression would land op2's TL in slot 1.
        self.assertEqual(body[4 + 1 * 8 + 1], ops[1].tl)
        self.assertEqual(body[4 + 2 * 8 + 1], ops[2].tl)


def _chip_mix(data: bytes) -> tuple[list[int], list[float], list[int]]:
    """Chip ids, output gains, and legacy volume bytes from an INFO block."""
    info = data.find(b"INFO")
    size = struct.unpack_from("<I", data, info + 4)[0]
    payload = data[info + 8:info + 8 + size]
    chips = [c for c in payload[24:56] if c]
    legacy = list(payload[56:56 + len(chips)])
    tail = 43  # patchbay, compat, speed pattern, groove, three ADIR pointers
    raw = payload[-(tail + 12 * len(chips)):-tail or None]
    gains = [struct.unpack_from("<f", raw, i * 12)[0] for i in range(len(chips))]
    return chips, gains, legacy


def _ins_types(data: bytes) -> list[int]:
    types = []
    pos = 0
    while True:
        i = data.find(b"INS2", pos)
        if i < 0:
            break
        types.append(struct.unpack_from("<H", data, i + 10)[0])
        pos = i + 4
    return types


def _rows(mod, ch: int):
    out = []
    for oi, pat in enumerate(mod.orders[ch]):
        for i, row in enumerate(mod.patterns[(ch, pat)]):
            if row.note >= 0 or row.fx:
                out.append((oi * mod.pat_len + i, row))
    return out


def _oki_vgm() -> VgmFile:
    """Banked phrase 0 (8 bytes, voice 0) and phrase 1 (400 bytes, voice 3)."""
    rom = bytearray(0x20800)
    rom[0x20000:0x20008] = bytes([0x00, 0x05, 0x00, 0x00, 0x05, 0x07, 0x00, 0x00])
    rom[0x20008:0x20010] = bytes([0x00, 0x06, 0x00, 0x00, 0x07, 0x8F, 0x00, 0x00])
    rom[0x20500:0x20508] = b"\x11" * 8
    rom[0x20600:0x20790] = b"\x22" * 400
    writes = [
        ChipWrite(0, "msm6295", 0, 0x0E, 0x81),
        ChipWrite(10, "msm6295", 0, 0x10, 0x02),
        ChipWrite(100, "msm6295", 0, 0x00, 0x80),
        ChipWrite(110, "msm6295", 0, 0x00, 0x10),
        ChipWrite(200, "msm6295", 0, 0x00, 0x81),
        ChipWrite(210, "msm6295", 0, 0x00, 0x80),
        ChipWrite(2000, "msm6295", 0, 0x00, 0x40),
    ]
    return VgmFile(
        path=Path("oki.vgm"),
        buf=b"",
        version=0x161,
        total_samples=3000,
        loop_samples=0,
        loop_file_pos=None,
        loop_sample=None,
        ym2151_clock=4_000_000,
        k007232_clock=4_000_000,
        gd3={"track": "oki"},
        roms=[RomSlice(0, len(rom), 0, bytes(rom), "msm6295")],
        writes=writes,
        msm6295_clock=2_000_000,
        msm6295_pin7=True,
    )


def _msm6258_vgm() -> VgmFile:
    writes = [
        ChipWrite(0, "msm6258", 0, 0, 0x02),
        ChipWrite(10, "msm6258", 0, 1, 0x11),
        ChipWrite(20, "msm6258", 0, 1, 0x22),
        ChipWrite(30, "msm6258", 0, 1, 0x33),
        ChipWrite(40, "msm6258", 0, 1, 0x44),
        ChipWrite(800, "msm6258", 0, 0, 0x01),
    ]
    return VgmFile(
        path=Path("adpcm.vgm"),
        buf=b"",
        version=0x161,
        total_samples=1000,
        loop_samples=0,
        loop_file_pos=None,
        loop_sample=None,
        ym2151_clock=4_000_000,
        k007232_clock=4_000_000,
        gd3={"track": "adpcm"},
        roms=[],
        writes=writes,
        msm6258_clock=4_000_000,
        msm6258_flags=0,
    )


class TestOkiVolumeAndFlags(unittest.TestCase):
    def test_nibble_and_pin7(self):
        self.assertEqual(oki_furnace_vol(0), 8)
        self.assertEqual(oki_furnace_vol(2), 6)
        self.assertEqual(oki_furnace_vol(8), 0)
        self.assertIsNone(oki_furnace_vol(9))
        self.assertEqual(oki_rate(2_000_000, True), 15152)
        self.assertEqual(msm6258_div_fx(1024), 2)
        self.assertEqual(msm6258_div_fx(768), 1)
        self.assertIsNone(msm6258_div_fx(512))
        high = oki_flag_text(2_000_000, True, banked=False)
        self.assertIn("clockSel=8", high)
        self.assertNotIn("rateSel", high)
        low = oki_flag_text(2_000_000, False, banked=True)
        self.assertIn("rateSel=true", low)
        self.assertIn("isBanked=true", low)


class TestOkiPhrase(unittest.TestCase):
    def test_bank_voice3_and_early_stop(self):
        song = analyze(_oki_vgm(), speed=1, pcm=True)
        self.assertEqual([s.length for s in song.oki_samples], [8, 400])
        self.assertEqual(song.oki_samples[0].phrase, 0)
        self.assertEqual(song.oki_samples[1].phrase, 1)
        self.assertEqual(song.oki_samples[0].rate, 15152)
        short = song.oki_samples[0].hits
        long = song.oki_samples[1].hits
        self.assertEqual([(h.voice, h.vol) for h in short], [(0, 8)])
        self.assertEqual([(h.voice, h.vol, h.end) for h in long], [(3, 8, 2000)])
        ons = [e for e in song.events if e.on and e.pcm]
        self.assertEqual(sorted((e.ch, e.note, e.vol) for e in ons), [(8, 108, 8), (11, 108, 8)])
        self.assertTrue(all(e.no_pan for e in ons))
        offs = [e for e in song.events if not e.on and e.pcm]
        self.assertEqual([(e.ch, e.sample) for e in offs], [(11, 2000)])

        data = write_fur(song, include_pcm=True, include_fm=True)
        chips, gains, legacy = _chip_mix(data)
        self.assertEqual(chips, [CHIP_YM2151, CHIP_MSM6295])
        self.assertEqual(legacy, [64, 64])
        self.assertAlmostEqual(gains[0], 1.0, places=5)
        self.assertAlmostEqual(gains[1], 1.0, places=5)
        self.assertNotIn(CHIP_K007232, chips)
        self.assertIn(b"clockSel=8", data)
        self.assertNotIn(b"rateSel=true", data)
        self.assertEqual(_ins_types(data), [36, 36])
        samples = list_fur_samples(data)
        self.assertEqual([s.depth for s in samples], [10, 10])
        self.assertEqual([s.n_frames for s in samples], [8, 400])
        self.assertEqual(samples[0].c4_rate, 15152)
        self.assertEqual(samples[0].data, b"\x11" * 8)
        self.assertNotEqual(samples[0].presence, b"\x00" * 16)

        mod = parse_fur(data)
        self.assertEqual(mod.n_ch, 12)
        played = []
        for ch in range(8, 12):
            for _row_i, row in _rows(mod, ch):
                if 0 <= row.note < 180:
                    played.append((ch, row.note, row.vol, row.fx))
                self.assertNotIn(0x08, [c for c, _v in row.fx])
        self.assertEqual([(c, n, v) for c, n, v, _fx in played], [(8, 108, 8), (11, 108, 8)])
        offs_ch = [
            row.note
            for _i, row in _rows(mod, 11)
            if row.note == NOTE_OFF
        ]
        self.assertEqual(offs_ch, [NOTE_OFF])


class TestMsm6258Burst(unittest.TestCase):
    def test_one_burst_and_divider_effect(self):
        song = analyze(_msm6258_vgm(), speed=1, pcm=True)
        self.assertEqual(len(song.msm6258_samples), 1)
        self.assertEqual(song.msm6258_samples[0].data, bytes([0x11, 0x22, 0x33, 0x44]))
        self.assertEqual(song.channel_fx, [(0, 8, 0x20, 2)])
        ons = [e for e in song.events if e.on and e.pcm]
        self.assertEqual([(e.ch, e.note, e.vol) for e in ons], [(8, 108, 8)])
        data = write_fur(song, include_pcm=True, include_fm=True)
        chips, gains, legacy = _chip_mix(data)
        self.assertEqual(chips, [CHIP_YM2151, CHIP_MSM6258])
        self.assertEqual(legacy[1], 64)
        self.assertAlmostEqual(gains[1], 1.0, places=5)
        self.assertEqual(_ins_types(data), [35])
        samples = list_fur_samples(data)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].depth, 10)
        self.assertEqual(samples[0].n_frames, 4)
        mod = parse_fur(data)
        self.assertEqual(mod.n_ch, 9)
        rows = _rows(mod, 8)
        notes = [row for _i, row in rows if 0 <= row.note < 180]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].note, 108)
        self.assertEqual(notes[0].vol, 8)
        self.assertIn((0x20, 2), notes[0].fx)
        self.assertNotIn(0x08, [c for c, _v in notes[0].fx])
        self.assertTrue(any(row.note == NOTE_OFF for _i, row in rows))

@unittest.skipUnless(BG_STAGE1.is_file(), "Battle Garegga pack missing")
class TestBattleGareggaStage1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vgm = load_vgm(BG_STAGE1)
        cls.song = analyze(cls.vgm, pcm=True)
        cls.data = write_fur(cls.song, include_pcm=True, include_fm=True)

    def test_header_clock_is_not_the_command_stream(self):
        self.assertEqual(self.vgm.version, 0x161)
        self.assertEqual(self.vgm.ym2151_clock, 4_000_000)
        self.assertEqual(self.vgm.msm6295_clock, 2_000_000)
        self.assertTrue(self.vgm.msm6295_pin7)
        self.assertEqual(self.vgm.msm6258_clock, 0)
        self.assertEqual(self.vgm.k007232_clock, 4_000_000)
        self.assertNotEqual(self.vgm.k007232_clock, 1728090438)

    def test_phrases_and_module(self):
        ons = [e for e in self.song.events if e.on and e.pcm]
        self.assertEqual(len(ons), 201)
        self.assertTrue(all(8 <= e.ch <= 11 for e in ons))
        self.assertTrue(all(e.note == 108 and e.vol in (6, 8) and e.no_pan for e in ons))
        self.assertGreaterEqual(len(self.song.fm_patches), 1)
        self.assertNotIn("no YM2151 key-ons found", self.song.warnings)
        self.assertEqual(self.song.msm6258_samples, [])
        lengths = {s.length for s in self.song.oki_samples}
        self.assertIn(8313, lengths)
        self.assertIn(2272, lengths)

        chips, gains, legacy = _chip_mix(self.data)
        self.assertEqual(chips, [CHIP_YM2151, CHIP_MSM6295])
        self.assertEqual(legacy[1], 64)
        self.assertAlmostEqual(gains[1], 1.0, places=5)
        self.assertIn(b"clockSel=8", self.data)
        self.assertNotIn(b"rateSel=true", self.data)
        self.assertNotIn(CHIP_K007232, chips)
        self.assertNotIn(CHIP_MSM6258, chips)
        types = _ins_types(self.data)
        self.assertIn(36, types)
        self.assertNotIn(45, types)
        self.assertTrue(any(t == 33 for t in types))
        samples = list_fur_samples(self.data)
        self.assertTrue(samples)
        self.assertTrue(all(s.depth == 10 for s in samples))
        frames = {s.n_frames for s in samples}
        self.assertIn(8313, frames)
        self.assertIn(2272, frames)
        mod = parse_fur(self.data)
        self.assertEqual(mod.n_ch, 12)
        fur_ons = []
        for ch in range(8, 12):
            for _i, row in _rows(mod, ch):
                if row.note < 0:
                    continue
                self.assertIn(row.note, (108, NOTE_OFF))
                if 0 <= row.note < 180:
                    self.assertIn(row.vol, (6, 8))
                    fur_ons.append(row)
                self.assertNotIn(0x08, [c for c, _v in row.fx])
        self.assertEqual(len(fur_ons), 201)


@unittest.skipUnless(G2_BURNING.is_file() and G2_TITLE.is_file(), "Gradius II rips missing")
class TestConvertFolder(unittest.TestCase):
    def test_parallel_output_matches_serial(self):
        self.assertGreaterEqual(CONVERT_WORKERS, 1)
        files = [G2_TITLE, G2_BURNING]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            serial = list(convert_folder(files, root / "serial", pcm=True, workers=1))
            self.assertEqual([f for f, _i, _e in serial], files)
            self.assertTrue(all(e is None for _f, _i, e in serial))
            parallel = list(convert_folder(
                files, root / "parallel", pcm=True, workers=2, log=lambda _m: None,
            ))
            self.assertEqual(
                sorted(f.name for f, _i, _e in parallel),
                sorted(f.name for f in files),
            )
            self.assertTrue(all(e is None for _f, _i, e in parallel))
            for f in files:
                a = (root / "serial" / (f.stem + ".fur")).read_bytes()
                b = (root / "parallel" / (f.stem + ".fur")).read_bytes()
                self.assertEqual(a, b)


class TestColumnFit(unittest.TestCase):
    """Effect columns per channel follow the channel's densest row.

    Furnace sizes a pattern column from its effect column count; the writer
    used to hand every channel the 8-column build capacity, so a converted
    rip opened with full-width columns. The count must never drop below the
    busiest row, or a load-and-re-save in Furnace would drop effects.
    """

    def test_fit_fx_cols_floor_and_ceiling(self):
        rows = [fw._blank_row(8) for _ in range(4)]
        rows[0].fx = [(0x08, 0x80), (0xE5, 0x90), (0xED, 0x10)]
        full = fw._blank_row(8)
        for cmd in range(1, 9):
            fw._add_fx(full, cmd, 0x11, 8)
        cols = fw._fit_fx_cols([(0, 0, rows), (1, 0, [full]), (2, 0, [fw._blank_row(8)])], 4)
        self.assertEqual(cols, [3, 8, 1, 1])

    def test_effect_columns_match_content(self):
        song = analyze(note_song(key_on=60000, key_off=160000), pcm=False)
        plan = [(2, 0x08, 0x80), (2, 0xE5, 0x90), (2, 0xED, 0x10)]
        with inject_rows(song, plan, 60000):
            data = write_fur(song, include_pcm=False, include_fm=True)
        mod = parse_fur(data)
        max_fx = [0] * mod.n_ch
        for (ch, _idx), rows in mod.patterns.items():
            for row in rows:
                max_fx[ch] = max(max_fx[ch], len(row.fx))
        self.assertEqual(mod.effect_cols, [max(1, n) for n in max_fx])
        self.assertGreaterEqual(mod.effect_cols[0], 3)
        self.assertTrue(all(1 <= c <= 8 for c in mod.effect_cols))


if __name__ == "__main__":
    unittest.main()

