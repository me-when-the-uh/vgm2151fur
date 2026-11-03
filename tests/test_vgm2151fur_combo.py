"""Merged logs: several chip families inside one module.

These rips pair engines the format rarely mixes - a Yamaha arcade FM chip
beside an OPL card, or a YM2612 that only drives its DAC under an AdLib
melody. The tests cover the `$90`-`$95` stream commands that carry the PCM,
the family choice that decides who gets a system, the channel bases that keep
two pitched families out of each other's columns, and - with the bundled
console build - what Furnace drives after a round trip.

Run:  python -m unittest tests.test_vgm2151fur_combo
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vgm2151fur.analyze import _family_sounds, analyze, format_report
from vgm2151fur.furwrite import (
    CHIP_OPL2, CHIP_OPL3, CHIP_OPLL_DRUMS, CHIP_SMS, CHIP_YM2151,
    CHIP_YM2612, CHIP_YM2612_EXT, _append_system_names, _channel_vol_max,
    write_fur,
)
from vgm2151fur.furio import CHIP_CHANNELS, iter_blocks, parse_fur
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.opl import N as OPL_N, OPLState, SLOT_ADDR
from vgm2151fur.vgm import ChipWrite, DacStreamPlay, VGM_RATE, VgmFile, load_vgm
from vgm2151fur.ym2612dac import DAC_RELEASE_SAMPLES, collect_dac

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_combo"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
# A key-on spacing wide enough that the grid estimator puts one row per note,
# so a test that counts notes on a channel means something.
PULSE = 20000


def _vgm(writes, **kw) -> VgmFile:
    """A synthetic log. Unnamed chip clocks stay zero, as an absent chip's."""
    clocks = {
        "ym2151_clock": 0, "ym2203_clock": 0, "ym2612_clock": 0,
        "ym2610_clock": 0, "ym2608_clock": 0, "opl_clock": 0,
        "opl2_clock": 0, "opl3_clock": 0, "ym2413_clock": 0,
        "sn76489_clock": 0, "k007232_clock": 0,
    }
    for key in list(kw):
        if key in clocks:
            clocks[key] = kw.pop(key)
    flat = [w for entry in writes for w in
            (entry if isinstance(entry, (list, tuple)) else [entry])]
    flat.sort(key=lambda w: (w.sample, w.chip, w.reg))
    return VgmFile(
        path=Path("merged.vgm"), buf=b"", version=0x161,
        total_samples=kw.pop("total_samples", 400000),
        loop_samples=0, loop_file_pos=None, loop_sample=None,
        gd3={}, roms=[], writes=flat, **clocks, **kw,
    )


def _stream(sample: int, offset: int, length: int, *, mode: int = 1,
            reg: int = 0x2A, rate: int = 11025, chip_id: int = 0,
            step: int = 1) -> DacStreamPlay:
    return DacStreamPlay(
        sample=sample, chip_type=0x02, chip_id=chip_id, stream=0, port=0,
        reg=reg, offset=offset, length=length, mode=mode, rate=rate,
        loop=bool(mode & 0x80), reverse=bool(mode & 0x10), step=step,
    )


# ------------------------------------------------------------- note builders

def opn_note(t: int, ch: int = 0, *, block: int = 4, fnum: int = 0x280,
             off: int | None = 9000) -> list[ChipWrite]:
    """One YM2612 voice, keyed with the encoding its $28 uses."""
    port, slot = divmod(ch, 3)
    base = 0x100 * port
    out: list[ChipWrite] = []
    for page in (0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
        for op in range(4):
            val = 0x01 if page == 0x30 else (0x1F if page == 0x70 else 0)
            out.append(ChipWrite(t, "ym2612", 0, base | page | (op << 2) | slot,
                                val))
    key = ch if ch < 3 else ch + 1
    out += [
        ChipWrite(t, "ym2612", 0, base | 0xB0 | slot, 0x07),
        ChipWrite(t, "ym2612", 0, base | 0xB4 | slot, 0xC0),
        ChipWrite(t + 1, "ym2612", 0, base | 0xA4 | slot,
                  (block << 3) | ((fnum >> 8) & 7)),
        ChipWrite(t + 1, "ym2612", 0, base | 0xA0 | slot, fnum & 0xFF),
        ChipWrite(t + 2, "ym2612", 0, 0x28, (0xF << 4) | key),
    ]
    if off is not None:
        out.append(ChipWrite(off, "ym2612", 0, 0x28, key))
    return out


def opl_note(t: int, ch: int = 0, *, kind: str = "opl2", block: int = 4,
             fnum: int = 580, off: int | None = 9000) -> list[ChipWrite]:
    """One OPL voice. An OPL3 channel past eight rides its second bank."""
    st = OPLState(kind, False)
    out: list[ChipWrite] = []
    for row in range(len(st.slots)):
        slot = st.slots[row][ch]
        if slot == OPL_N:
            continue
        addr = SLOT_ADDR[slot]
        out += [
            ChipWrite(t, kind, 0, addr + 0x20, 0x01),
            ChipWrite(t, kind, 0, addr + 0x40, 0x30 if row == 0 else 0x10),
            ChipWrite(t, kind, 0, addr + 0x60, 0xF5),
            ChipWrite(t, kind, 0, addr + 0x80, 0x94),
            ChipWrite(t, kind, 0, addr + 0xE0, row),
        ]
    cm = st.chan_map[ch]      # an OPL3's second bank is already in this table
    hi = (block << 2) | ((fnum >> 8) & 3)
    out += [
        ChipWrite(t + 1, kind, 0, cm + 0xA0, fnum & 0xFF),
        ChipWrite(t + 2, kind, 0, cm + 0xB0, hi | 0x20),
        ChipWrite(t + 2, kind, 0, cm + 0xC0, 0x30),
    ]
    if off is not None:
        out.append(ChipWrite(off, kind, 0, cm + 0xB0, hi))
    return out


def opm_note(t: int, ch: int = 0, *, kc: int = 0x4F, kf: int = 0x40,
             off: int | None = 9000) -> list[ChipWrite]:
    """One YM2151 voice: the operator pages, then $08 with its channel."""
    out: list[ChipWrite] = [ChipWrite(t, "ym2151", 0, 0x20 | ch, 0xC7)]
    for op in range(4):
        base = (op << 3) | ch
        out += [
            ChipWrite(t, "ym2151", 0, 0x40 | base, 0x11),
            ChipWrite(t, "ym2151", 0, 0x60 | base, 0x20),
            ChipWrite(t, "ym2151", 0, 0x80 | base, 0xF5),
            ChipWrite(t, "ym2151", 0, 0xA0 | base, 0xF4),
            ChipWrite(t, "ym2151", 0, 0xC0 | base, 0x75),
            ChipWrite(t, "ym2151", 0, 0xE0 | base, 0x04),
        ]
    out += [
        ChipWrite(t + 1, "ym2151", 0, 0x28 | ch, kc),
        ChipWrite(t + 1, "ym2151", 0, 0x30 | ch, kf),
        ChipWrite(t + 2, "ym2151", 0, 0x08, 0x78 | ch),
    ]
    if off is not None:
        out.append(ChipWrite(off, "ym2151", 0, 0x08, ch))
    return out


def opll_note(t: int, ch: int = 0, *, block: int = 1, fnum: int = 500,
              preset: int = 6, volume: int = 8,
              off: int | None = 9000) -> list[ChipWrite]:
    """One YM2413 voice on a ROM preset."""
    out = [
        ChipWrite(t, "opll", 0, 0x10 + ch, fnum & 0xFF),
        ChipWrite(t + 1, "opll", 0, 0x20 + ch,
                  (block << 1) | ((fnum >> 8) & 1) | 0x10),
        ChipWrite(t + 2, "opll", 0, 0x30 + ch, (preset << 4) | volume),
    ]
    if off is not None:
        out.append(ChipWrite(off, "opll", 0, 0x20 + ch,
                             (block << 1) | ((fnum >> 8) & 1)))
    return out


def psg_tone(t: int, voice: int = 0, *, period: int = 300, atten: int = 6,
             hold: int = 8000) -> list[ChipWrite]:
    """A Genesis PSG gate: latch the period, lift the attenuation, close it."""
    return [
        ChipWrite(t, "sn76489", 0, 0,
                  0x80 | (voice << 5) | ((period >> 8) & 0x0F)),
        ChipWrite(t, "sn76489", 0, 0, period & 0xFF),
        ChipWrite(t + 1, "sn76489", 0, 0,
                  0x80 | (voice << 5) | 0x10 | atten),
        ChipWrite(t + hold, "sn76489", 0, 0,
                  0x80 | (voice << 5) | 0x10 | 15),
    ]


# ------------------------------------------------------------ stream parsing

def _raw_pcm_log(body: bytes, banks: list[bytes],
                 *, clock: int = 7670453) -> bytes:
    """A loadable YM2612 log with type-0x00 banks and a command stream."""
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x161).to_bytes(4, "little")
    buf[0x18:0x1C] = (VGM_RATE).to_bytes(4, "little")
    buf[0x2C:0x30] = (clock & 0x3FFFFFFF).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    data = b"".join(
        bytes([0x67, 0x66, 0x00]) + len(b).to_bytes(4, "little") + b
        for b in banks
    )
    out = bytes(buf) + data + body + b"\x66"
    return out[:4] + (len(out) - 4).to_bytes(4, "little") + out[8:]


def _load(raw: bytes) -> VgmFile:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "stream.vgm"
        path.write_bytes(raw)
        return load_vgm(path)


class TestStreamCommands(unittest.TestCase):
    """`$90`-`$95` for a chip other than the OKIM6258."""

    BANK = bytes(((i * 31) % 251) + 4 for i in range(512))

    def _log(self, starts: list[tuple[int, int, int, int]], **kw) -> VgmFile:
        """Commands at real sample times, so a play can fall on the last one."""
        head = b"".join([
            bytes([0x90, 0x00, 0x02, 0x00, 0x2A]),
            bytes([0x91, 0x00, 0x00, 0x01, 0x00]),
            bytes([0x92, 0x00]) + (11025).to_bytes(4, "little"),
            bytes([0x52, 0x2B, 0x80]),
        ])
        body = bytearray(head)
        last = 0
        for sample, offset, length, mode in starts:
            if sample > last:
                body += bytes([0x61]) + (sample - last).to_bytes(2, "little")
                last = sample
            body += (bytes([0x93, 0x00]) + offset.to_bytes(4, "little")
                     + bytes([mode]) + length.to_bytes(4, "little"))
        return _load(_raw_pcm_log(bytes(body), [self.BANK], **kw))

    def test_a_stream_start_is_recorded(self):
        v = self._log([(100, 0, 64, 1)])
        self.assertEqual(len(v.dac_stream_plays), 1)
        play = v.dac_stream_plays[0]
        self.assertEqual((play.chip_type, play.reg, play.rate),
                         (0x02, 0x2A, 11025))
        self.assertEqual((play.sample, play.offset, play.length), (100, 0, 64))
        self.assertEqual(play.length_mode, 1)
        self.assertFalse(play.loop or play.reverse)

    def test_the_okim6258_path_is_untouched(self):
        body = (bytes([0x90, 0x00, 0x17, 0x00, 0x01])
                + bytes([0x92, 0x00]) + (7813).to_bytes(4, "little")
                + bytes([0x93, 0x00]) + (0).to_bytes(4, "little")
                + bytes([1]) + (8).to_bytes(4, "little"))
        v = _load(_raw_pcm_log(body, [self.BANK]))
        self.assertEqual(v.dac_stream_plays, [])
        self.assertEqual(len(v.msm6258_plays), 1)

    def test_mode_zero_moves_the_cursor_only(self):
        # A zero-length start parks the read cursor; the next play, whose own
        # offset says "ignore me", takes the song from there.
        v = self._log([(100, 256, 0, 0), (200, 0xFFFFFFFF, 32, 1)])
        self.assertEqual(len(v.dac_stream_plays), 1)
        play = v.dac_stream_plays[0]
        self.assertEqual(play.offset, 256)
        self.assertEqual((play.length, play.length_mode), (32, 1))

    def test_loop_and_reverse_flags(self):
        loop = self._log([(100, 0, 64, 0x81)]).dac_stream_plays[0]
        self.assertTrue(loop.loop)
        self.assertFalse(loop.reverse)
        self.assertEqual(loop.length_mode, 1)
        back = self._log([(100, 0, 64, 0x11)]).dac_stream_plays[0]
        self.assertTrue(back.reverse)
        self.assertFalse(back.loop)

    def test_a_stream_without_its_bank_is_reported(self):
        raw = _raw_pcm_log(
            bytes([0x90, 0x00, 0x02, 0x00, 0x2A])
            + bytes([0x93, 0x00]) + (0).to_bytes(4, "little")
            + bytes([1]) + (8).to_bytes(4, "little"), [],
        )
        v = _load(raw)
        self.assertTrue(any("no PCM data block" in w for w in v.warnings),
                        v.warnings)


class TestStreamCollection(unittest.TestCase):
    """`collect_dac` reading PCM out of those stream starts."""

    BANK = bytes(((i * 7) % 253) + 1 for i in range(4096))

    def _file(self, plays: list[DacStreamPlay], *, total: int = 20000):
        v = _vgm(
            [ChipWrite(0, "ym2612", 0, 0x2B, 0x80)],
            ym2612_clock=7670453, dac_data=bytearray(self.BANK),
            total_samples=total,
        )
        v.dac_stream_plays = plays
        return v

    def test_slices_take_the_exact_byte_range(self):
        bank = self.BANK
        v = self._file([_stream(0, 0, 128), _stream(9000, 256, 64)])
        samples, plays, warn, stats = collect_dac(v, slices=True)
        self.assertFalse(warn)
        self.assertEqual([s.data for s in samples], [bank[:128], bank[256:320]])
        self.assertEqual([p.onset for p in plays], [0, 9000])
        self.assertEqual(stats["dac_stream_plays"], 2)

    def test_chunks_are_contiguous_and_capped(self):
        v = self._file([_stream(0, 0, len(self.BANK))])
        samples, _plays, _warn, stats = collect_dac(v, slices=False)
        self.assertGreater(len(samples), 1)
        self.assertLessEqual(len(samples), DAC_RELEASE_SAMPLES)
        self.assertEqual(b"".join(s.data for s in samples), self.BANK)
        self.assertEqual(stats["dac_stream_chunks"], len(samples))

    def test_a_long_stream_spreads_the_budget_over_its_length(self):
        # One play of a whole bank: every chunk starts where the bytes say, at
        # the stream's own rate, and none of them overlaps the next.
        v = self._file([_stream(0, 0, len(self.BANK))])
        samples, plays, _warn, _stats = collect_dac(v, slices=False)
        done = 0
        for play, smp in zip(plays, samples):
            want = done * VGM_RATE / smp.c4_rate
            self.assertAlmostEqual(play.onset, want, delta=1)
            done += len(smp.data)
        self.assertEqual(done, len(self.BANK))

    def test_every_chunk_keeps_the_stream_rate(self):
        v = self._file([_stream(0, 0, len(self.BANK), rate=22050)])
        samples, _plays, _warn, _stats = collect_dac(v, slices=False)
        self.assertTrue(all(s.c4_rate == 22050 for s in samples))

    def test_the_end_of_file_flush_is_dropped(self):
        v = self._file([_stream(0, 0, 400), _stream(20000, 400, 400)],
                       total=20000)
        _samples, plays, _warn, stats = collect_dac(v, slices=False)
        self.assertEqual(stats["dac_stream_tail_plays"], 1)
        self.assertTrue(all(p.onset < 20000 for p in plays))

    def test_a_register_that_is_not_the_dac_is_refused(self):
        v = self._file([_stream(0, 0, 400, reg=0x28)])
        samples, plays, _warn, stats = collect_dac(v)
        self.assertEqual((samples, plays), ([], []))
        self.assertEqual(stats["dac_stream_other_reg"], 1)

    def test_a_second_chip_is_refused(self):
        v = self._file([_stream(0, 0, 400, chip_id=1)])
        _samples, _plays, _warn, stats = collect_dac(v)
        self.assertEqual(stats["dac_stream_second_chip"], 1)

    def test_milliseconds_count_the_rate(self):
        v = self._file([_stream(0, 0, 100, mode=2)])
        samples, _plays, _warn, _stats = collect_dac(v, slices=True)
        self.assertEqual(len(samples[0].data), round(100 * 11025 / 1000))

    def test_a_step_reads_every_nth_byte(self):
        v = self._file([_stream(0, 0, 64, step=2)])
        samples, _plays, _warn, _stats = collect_dac(v, slices=True)
        self.assertEqual(samples[0].data, self.BANK[:64:2])

    def test_reverse_walks_the_range_backwards(self):
        v = self._file([_stream(0, 0, 64, mode=0x11)])
        samples, _plays, _warn, _stats = collect_dac(v, slices=True)
        self.assertEqual(samples[0].data, self.BANK[:64][::-1])


# ------------------------------------------------- who gets a system at all

class TestFamilySound(unittest.TestCase):
    """A chip that only got its init block must not claim a system."""

    def test_init_only_setup_is_silent(self):
        writes = [ChipWrite(0, "ym2608", 0, reg, 0x00)
                  for reg in range(0x10, 0x20)]
        writes += [ChipWrite(0, "ym2612", 0, 0x27, 0x00)]
        v = _vgm(writes)
        self.assertFalse(_family_sounds(v, {"ym2608", "ym2608_ssg"}))
        self.assertFalse(_family_sounds(v, {"ym2612"}))

    def test_a_rhythm_latch_sounds(self):
        v = _vgm([ChipWrite(100, "ym2608", 0, 0x10, 0xA0 | 0x20)])
        self.assertTrue(_family_sounds(v, {"ym2608"}))

    def test_a_tuned_ssg_voice_sounds(self):
        v = _vgm([ChipWrite(100, "ym2608_ssg", 0, 1, 0x01)])
        self.assertTrue(_family_sounds(v, {"ym2608", "ym2608_ssg"}))

    def test_a_key_on_sounds(self):
        self.assertTrue(_family_sounds(_vgm(opn_note(100)), {"ym2612"}))
        self.assertTrue(_family_sounds(_vgm(opl_note(100)), {"opl2"}))
        self.assertTrue(_family_sounds(_vgm(opm_note(100)), {"ym2151"}))
        self.assertTrue(_family_sounds(_vgm(opll_note(100)), {"opll"}))

    def test_a_dac_write_is_not_an_fm_note(self):
        # The stream path decides whether a DAC-only chip earns a system, so
        # the register scan stays silent about it.
        v = _vgm([ChipWrite(0, "ym2612", 0, 0x2B, 0x80)],
                 dac_events=[(10, 1, 0)])
        self.assertFalse(_family_sounds(v, {"ym2612"}))


class TestComboLayout(unittest.TestCase):
    """The channel bases of a module that keeps more than one family."""

    def test_dac_only_opn2_keeps_both_systems(self):
        bank = bytes(((i * 13) % 251) + 2 for i in range(4096))
        writes: list = [ChipWrite(0, "ym2612", 0, 0x2B, 0x80)]
        for ch in range(3):
            writes += [opl_note(t, ch) for t in (1000 + ch * PULSE,
                                                 9000 + ch * PULSE)]
        v = _vgm(writes, ym2612_clock=7600489, opl2_clock=3579545,
                 dac_data=bytearray(bank), total_samples=80000)
        v.dac_stream_plays = [_stream(500, 0, len(bank))]
        song = analyze(v)
        self.assertEqual([s.kind for s in song.fm_systems], ["ym2612", "opl2"])
        ons = [e for e in song.events if e.on and not e.pcm]
        self.assertTrue(any(e.dac for e in ons), "the DAC note is missing")
        # The OPL melody starts after the OPN2's six columns.
        self.assertEqual(sorted({e.ch for e in ons if not e.dac}), [6, 7, 8])
        self.assertTrue(any("both systems were converted" in w
                            for w in song.warnings), song.warnings)

    def test_opm_beside_an_opl_keeps_its_own_eight_columns(self):
        writes: list = []
        for ch in range(8):
            writes += opm_note(1000 + ch * PULSE, ch)
        for ch in range(9):
            writes += opl_note(1500 + ch * PULSE, ch)
        v = _vgm(writes, ym2151_clock=4000000, opl2_clock=3579545,
                 total_samples=200000)
        song = analyze(v)
        self.assertEqual([(s.kind, s.fm_channels) for s in song.fm_systems],
                         [("ym2151", 8), ("opl2", 9)])
        self.assertEqual(song.fm_chip, "ym2151")
        opm = sorted({e.ch for e in song.events if e.on and not e.pcm
                      and e.ch < 8})
        opl = sorted({e.ch - 8 for e in song.events if e.on and not e.pcm
                      and e.ch >= 8})
        self.assertEqual(opm, list(range(8)))
        self.assertEqual(opl, list(range(9)))

    def test_opn2_with_notes_beside_an_opl3(self):
        bank = bytes(((i * 29) % 241) + 3 for i in range(2048))
        writes: list = [ChipWrite(0, "ym2612", 0, 0x2B, 0x80)]
        for ch in range(6):
            writes += opn_note(1000 + ch * PULSE, ch)
        for ch in range(4):
            writes += opl_note(1300 + ch * PULSE, ch, kind="opl3")
        v = _vgm(writes, ym2612_clock=7670453, opl3_clock=14318180,
                 dac_data=bytearray(bank), total_samples=200000)
        v.dac_stream_plays = [_stream(500, 0, len(bank))]
        song = analyze(v)
        self.assertEqual([(s.kind, s.fm_channels) for s in song.fm_systems],
                         [("ym2612", 6), ("opl3", 18)])
        chans = sorted({e.ch for e in song.events if e.on and not e.pcm})
        # The OPN2 keeps 0-5, its DAC column inside them; the OPL3 follows.
        self.assertEqual([c for c in chans if c < 6], [0, 1, 2, 3, 4, 5])
        self.assertTrue(any(c >= 6 for c in chans), chans)
        self.assertLess(max(chans), 24)
        dac = [e.ch for e in song.events if e.dac and e.on]
        self.assertEqual(set(dac), {5})

    def test_three_systems_stack_in_order(self):
        writes: list = []
        for ch in range(3):
            writes += opn_note(1000 + ch * PULSE, ch)
        for ch in range(3):
            writes += opll_note(1200 + ch * PULSE, ch)
        writes.append(ChipWrite(1300, "opll", 0, 0x0E, 0x20 | 0x1F))
        for ch in range(3):
            writes += psg_tone(1400 + ch * PULSE, ch)
        v = _vgm(writes, ym2612_clock=7670453, ym2413_clock=3579545,
                 sn76489_clock=3579545, total_samples=200000)
        song = analyze(v)
        self.assertEqual([s.kind for s in song.fm_systems], ["ym2612", "opll"])
        chans = sorted({e.ch for e in song.events if e.on and not e.pcm})
        self.assertTrue(all(0 <= c < 9 + 9 + 4 for c in chans), chans)
        # Every system sits in its own block: OPN2 0-5, OPLL 6-14, PSG 15-18.
        self.assertEqual([c for c in chans if c < 6], [0, 1, 2])
        self.assertTrue(any(6 <= c < 15 for c in chans), chans)
        self.assertTrue(any(c >= 15 for c in chans), chans)

    def test_init_only_arcade_chip_loses_its_system(self):
        init = [ChipWrite(0, "ym2608", 0, reg, 0x00) for reg in range(0x10, 0x20)]
        writes: list = init + opn_note(1000, 0) + psg_tone(2000, 0)
        v = _vgm(writes, ym2608_clock=7987200, ym2612_clock=7670453,
                 sn76489_clock=3579545, total_samples=200000)
        song = analyze(v)
        self.assertEqual([s.kind for s in song.fm_systems], ["ym2612"])
        self.assertTrue(any("init block" in w for w in song.warnings),
                        song.warnings)

    def test_a_silent_psg_is_dropped_with_a_warning(self):
        writes: list = opl_note(1000, 0) + [
            ChipWrite(1000, "sn76489", 0, 0, 0x80 | (v << 5) | 0x10 | 15)
            for v in range(4)
        ]
        v = _vgm(writes, opl2_clock=3579545, sn76489_clock=3579545,
                 total_samples=200000)
        song = analyze(v)
        self.assertEqual(song.psg_ids, [])


# -------------------------------------------------------- the module it emits

class TestComboWriter(unittest.TestCase):
    """What the module header and channel table say about a combined log."""

    @classmethod
    def setUpClass(cls):
        writes: list = []
        for ch in range(8):
            writes += opm_note(1000 + ch * PULSE, ch)
        for ch in range(9):
            writes += opl_note(1500 + ch * PULSE, ch)
        cls.song = analyze(_vgm(writes, ym2151_clock=4000000,
                               opl2_clock=3579545, total_samples=200000))

    def test_chip_list_and_channel_count(self):
        data = write_fur(self.song)
        m = parse_fur(data)
        self.assertEqual(m.chips[:2], [CHIP_YM2151, CHIP_OPL2])
        self.assertEqual(m.n_ch, 8 + 9)
        self.assertEqual(sum(CHIP_CHANNELS[c] for c in m.chips), m.n_ch)

    def test_clock_flags_travel_with_each_chip(self):
        data = write_fur(self.song)
        flags = _flag_texts(data)
        # The OPM clock is not Furnace's default, so it has to be named; the
        # OPL2 runs at COLOR_NTSC and needs no flag of its own.
        self.assertEqual(flags, ["customClock=4000000"])

    def test_channel_names_follow_the_system_order(self):
        longs: list[str] = []
        shorts: list[str] = []
        _append_system_names(self.song, self.song.fm_systems, longs, shorts)
        self.assertEqual(len(longs), 17)
        self.assertEqual(longs[:8], [f"FM {i + 1}" for i in range(8)])
        self.assertEqual(longs[8:], [f"FM {i + 1}" for i in range(9)])
        # The OPL block is numbered by module channel, so no tag repeats.
        self.assertEqual(shorts[:8], [f"F{i + 1}" for i in range(8)])
        self.assertEqual(shorts[8:], [f"F{i}" for i in range(9, 18)])
        self.assertEqual(len(set(shorts)), len(shorts))

    def test_each_column_uses_its_own_chip_volume_ceiling(self):
        table = _channel_vol_max(self.song, 17, 63, 0)
        self.assertEqual(table[:8], [127] * 8)
        self.assertEqual(table[8:17], [63] * 9)

    def test_a_single_family_keeps_one_ceiling(self):
        song = analyze(_vgm(opn_note(1000, 0) + opn_note(1000 + PULSE, 1),
                           ym2612_clock=7670453, total_samples=60000))
        self.assertEqual(_channel_vol_max(song, 6, 127, 0), [127] * 6)

    def test_report_names_every_system(self):
        text = format_report(self.song)
        self.assertIn("YM2151", text)
        self.assertIn("OPL2", text)


def _flag_texts(data: bytes) -> list[str]:
    return [payload.split(b"\x00")[0].decode("utf-8", "replace")
            for _pos, mag, _size, payload in iter_blocks(data)
            if mag == b"FLAG"]


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestComboRoundTrip(unittest.TestCase):
    """Furnace drives both systems of a combined module."""

    def test_opm_and_opl_play_their_own_channels(self):
        writes: list = []
        for ch in (0, 3, 7):
            writes += opm_note(1000 + ch * PULSE // 3, ch)
        for ch in (0, 4):
            writes += opl_note(1500 + ch * PULSE // 3, ch)
        v = _vgm(writes, ym2151_clock=4000000, opl2_clock=3579545,
                 total_samples=200000)
        song = analyze(v)
        self.assertEqual([(s.kind, s.fm_channels) for s in song.fm_systems],
                         [("ym2151", 8), ("opl2", 9)])
        fur = TMP / "opm_opl.fur"
        fur.write_bytes(write_fur(song, compress=True))
        out = TMP / "opm_opl.vgm"
        export_vgm(fur, out)
        exp = load_vgm(out)
        self.assertEqual(exp.ym2151_clock, 4000000)
        self.assertEqual(exp.opl2_clock, 3579545)
        opm = [w for w in exp.writes if w.chip == "ym2151" and w.reg == 0x08
               and (w.val & 0x78)]
        opl = [w for w in exp.writes if w.chip == "opl2"
               and 0xB0 <= (w.reg & 0xFF) <= 0xB8 and (w.val & 0x20)]
        # Each family drives its own registers: the OPM voices it keyed, and
        # the two OPL channels the melody was written on.
        self.assertEqual(sorted({w.val & 7 for w in opm}), [0, 3, 7])
        self.assertEqual(sorted({(w.reg & 0xFF) - 0xB0 for w in opl}), [0, 4])

    def test_dac_stream_and_melody_both_survive(self):
        bank = bytes(((i * 5) % 251) + 4 for i in range(8192))
        writes: list = [ChipWrite(0, "ym2612", 0, 0x2B, 0x80)]
        for ch in range(3):
            writes += [opl_note(t, ch) for t in (1000 + ch * PULSE,
                                                 9000 + ch * PULSE)]
        v = _vgm(writes, ym2612_clock=7600489, opl2_clock=3579545,
                 dac_data=bytearray(bank), total_samples=80000)
        v.dac_stream_plays = [_stream(200, 0, len(bank))]
        song = analyze(v)
        self.assertGreater(song.dac_count, 0)
        fur = TMP / "dac_opl.fur"
        fur.write_bytes(write_fur(song, compress=True))
        m = parse_fur(fur.read_bytes())
        self.assertEqual(m.chips[:2], [CHIP_YM2612, CHIP_OPL2])
        out = TMP / "dac_opl.vgm"
        export_vgm(fur, out)
        exp = load_vgm(out)
        starts = [t for t, _b, _r in exp.dac_block_plays] \
            + [p.sample for p in exp.dac_stream_plays]
        self.assertTrue(starts, "no DAC start survived the round trip")
        keys = [w for w in exp.writes if w.chip == "opl2"
                and 0xB0 <= (w.reg & 0xFF) <= 0xB8 and (w.val & 0x20)]
        self.assertTrue(keys, "the OPL melody vanished")


if __name__ == "__main__":
    unittest.main()
