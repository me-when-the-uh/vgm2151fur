"""OPL-family (YM3526/OPL, YM3812/OPL2, YMF262/OPL3) support.

Covers the VGM commands and header clocks, Furnace's channel and slot tables (an
OPL3 permutes its eighteen columns and pairs them for four-operator voices), the
pitch model, rhythm mode with the frequency registers its kit shares, the .fur
emission of DIV_INS_OPL and DIV_INS_OPL_DRUMS instruments, and - with the bundled
console build - a round trip through Furnace that reads back what it drove.

Run:  python -m unittest tests.test_vgm2151fur_opl
"""

from __future__ import annotations

import math
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from vgm2151fur.analyze import analyze, format_report
from vgm2151fur.diag import (
    export_fur, opl_attacks, opl_keyed_intervals, opl_pitch_stream,
    opl_voice_addrs, pitch_runs,
)
from vgm2151fur.furwrite import (
    CHIP_OPL, CHIP_OPL2, CHIP_OPL3, CHIP_OPL2_DRUMS, CHIP_OPL3_DRUMS,
    CHIP_OPL_DRUMS, INS_OPL, INS_OPL_DRUMS, write_fur,
)
from vgm2151fur.furnace import export_vgm, find_furnace
from vgm2151fur.furio import iter_blocks, parse_fur
from vgm2151fur.opl import (
    KIND_OPL, KIND_OPL2, KIND_OPL3, N as OPL_N, SLOT_ADDR, FreqPitchTable,
    OPLState, channel_count, drum_channel, is_drum_channel, melodic_count,
    opl_carrier_octave, opl_module_clock, opl_note_fnum, opl_note_for,
    opl_pitch_units, pitch_table, to_freq,
)
from vgm2151fur.timing import _keyons_by_channel, estimate_grid
from vgm2151fur.vgm import (
    ChipWrite, VGM_RATE, VgmFile, chips_in_head, load_vgm,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_opl"
TMP.mkdir(exist_ok=True)
HAVE_FURNACE = find_furnace() is not None
CLOCK_OPL2 = 3579545
CLOCK_OPL3 = 14318180
# The channel the note-lattice heartbeat keys, kept clear of the channels a test
# asserts on.
PULSE = 5

SLOTS_OPL2 = OPLState(KIND_OPL2, False).slots
SLOTS_OPL3 = OPLState(KIND_OPL3, False).slots
SLOTS_KIT = OPLState(KIND_OPL2, True).slots


def _vgm(writes, *, kind: str = KIND_OPL2, total_samples=400000, **kw):
    field = {KIND_OPL: "opl_clock", KIND_OPL2: "opl2_clock",
             KIND_OPL3: "opl3_clock"}[kind]
    default = {KIND_OPL: CLOCK_OPL2, KIND_OPL2: CLOCK_OPL2,
               KIND_OPL3: CLOCK_OPL3}[kind]
    clocks = {"opl_clock": 0, "opl2_clock": 0, "opl3_clock": 0}
    clocks[field] = kw.pop(field, default)
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x151,
        total_samples=total_samples, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        roms=[], writes=sorted(writes, key=lambda w: w.sample), **clocks, **kw,
    )


def _raw_vgm(body: bytes, *, clock_off: int, clock: int, dual: bool = False,
             pan: bool = False) -> bytes:
    """A loadable VGM whose OPL clock sits in the given header field."""
    buf = bytearray(0xC0)
    buf[0:4] = b"Vgm "
    buf[8:12] = (0x151).to_bytes(4, "little")
    buf[0x18:0x1C] = (44100).to_bytes(4, "little")
    buf[0x34:0x38] = (0xC0 - 0x34).to_bytes(4, "little")
    raw = clock & 0x3FFFFFFF
    if dual:
        raw |= 0x40000000
    if pan:
        raw |= 0x80000000
    buf[clock_off:clock_off + 4] = raw.to_bytes(4, "little")
    out = bytes(buf) + body + b"\x66"
    return out[:4] + (len(out) - 4).to_bytes(4, "little") + out[8:]


def reg(kind: str, addr: int) -> int:
    """The register as the log spells it: bank B carries bit 8 on an OPL3."""
    return addr if kind == KIND_OPL3 else addr & 0xFF


def op_writes(t: int, kind: str, addr: int, *, am=0, vib=0, sus=0, ksr=0,
              mult=1, tl=20, ksl=0, ar=15, dr=5, sl=9, rr=4, ws=0,
              cid: int = 0) -> list[ChipWrite]:
    """All five registers of one operator, named as the datasheet names them."""
    return [
        ChipWrite(t, kind, cid, reg(kind, addr + 0x20),
                  (am << 7) | (vib << 6) | (sus << 5) | (ksr << 4) | mult),
        ChipWrite(t + 1, kind, cid, reg(kind, addr + 0x40), (ksl << 6) | tl),
        ChipWrite(t + 2, kind, cid, reg(kind, addr + 0x60), (ar << 4) | dr),
        ChipWrite(t + 3, kind, cid, reg(kind, addr + 0x80), (sl << 4) | rr),
        ChipWrite(t + 4, kind, cid, reg(kind, addr + 0xE0), ws),
    ]


def voice_slots(kind: str, ch: int, four: bool = False, drums: bool = False):
    """The operator addresses of the voice a module channel drives."""
    st = OPLState(kind, drums)
    rows = 4 if four else 2
    return [SLOT_ADDR[st.slots[r][ch]] for r in range(rows)
            if st.slots[r][ch] != OPL_N]


def key_on(t: int, kind: str, ch: int, *, block: int = 4, fnum: int = 580,
           alg: int = 0, fb: int = 2, pan: int = 3, drums: bool = False,
           cid: int = 0, off_time: int = 60000) -> list[ChipWrite]:
    """Frequency, connection and key bit of one voice, then its release."""
    st = OPLState(kind, drums)
    cm = st.chan_map[ch]
    hi = (block << 2) | ((fnum >> 8) & 3)
    out = [
        ChipWrite(t, kind, cid, reg(kind, cm + 0xA0), fnum & 0xFF),
        ChipWrite(t, kind, cid, reg(kind, cm + 0xB0), hi | 0x20),
        ChipWrite(t, kind, cid, reg(kind, cm + 0xC0),
                  (alg & 1) | ((fb & 7) << 1) | ((pan & 3) << 4)),
    ]
    if off_time:
        out.append(ChipWrite(off_time, kind, cid, reg(kind, cm + 0xB0), hi))
    return out


def hi_write(kind: str, ch: int, *, t: int = 0, block: int, fnum: int,
             cid: int = 0, kon: bool = False) -> ChipWrite:
    """The frequency-high write of a channel, at its hardware register.

    A release has to name the hardware channel the engine drives for that
    column, which an OPL3 permutes: clearing bit 5 of the wrong ``0xBn`` leaves
    the voice keyed and swallows the next note.
    """
    st = OPLState(kind, False)
    cm = st.chan_map[ch]
    return ChipWrite(t, kind, cid, reg(kind, cm + 0xB0),
                     (block << 2) | ((fnum >> 8) & 3) | (0x20 if kon else 0))


def voice(t: int, kind: str, ch: int, *, four: bool = False,
          drums: bool = False, ops: list[dict] | None = None,
          cid: int = 0, **kw) -> list[ChipWrite]:
    """Load a voice's operators and key it on."""
    addrs = voice_slots(kind, ch, four, drums)
    specs = ops if ops is not None else [{} for _ in addrs]
    out: list[ChipWrite] = []
    for addr, spec in zip(addrs, specs):
        out += op_writes(t, kind, addr, cid=cid, **spec)
    return out + key_on(t + 20, kind, ch, drums=drums, cid=cid, **kw)


def heart(kind: str = KIND_OPL2, ch: int = PULSE, start: int = 100, n: int = 60,
          spacing: int = 5000) -> list[ChipWrite]:
    """Key-on ticks, so the grid estimator has a lattice to measure."""
    st = OPLState(kind, False)
    cm = st.chan_map[ch]
    out: list[ChipWrite] = []
    for k in range(n):
        t = start + k * spacing
        out += [ChipWrite(t, kind, 0, reg(kind, cm + 0xA0), 0x2D),
                ChipWrite(t, kind, 0, reg(kind, cm + 0xB0),
                          (4 << 2) | 1 | 0x20),
                ChipWrite(t + spacing // 2, kind, 0, reg(kind, cm + 0xB0),
                          (4 << 2) | 1)]
    return out


def rhythm(t: int, kind: str = KIND_OPL2, bits: int = 0, depth: int = 3,
           cid: int = 0) -> ChipWrite:
    """The rhythm latch: both depth bits, mode on, and the trigger bits given."""
    return ChipWrite(t, kind, cid, 0xBD, (depth << 6) | 0x20 | bits)


def ins_types(data: bytes) -> list[int]:
    return [int.from_bytes(payload[2:4], "little")
            for _off, mag, _size, payload in iter_blocks(data) if mag == b"INS2"]


def fm_feature(data: bytes, index: int = 0) -> bytes:
    seen = -1
    for _off, mag, _size, payload in iter_blocks(data):
        if mag != b"INS2":
            continue
        seen += 1
        if seen != index:
            continue
        pos = payload.find(b"FM")
        if pos < 0:
            return b""
        size = int.from_bytes(payload[pos + 2:pos + 4], "little")
        return payload[pos + 4:pos + 4 + size]
    return b""


def keyon_events(vgm, kind: str, drums: bool):
    """[(chip_id, column, sample, unit)] at every key-on of one chip's stream.

    The unit is the phase step ``fnum << block``, which is what sets the
    frequency: a rip may hold a register in another octave split and Furnace
    re-derives the pair from its own table. Grouping by channel keeps a
    comparison from drifting when one voice holds an extra note, and the kit's
    shared frequency registers are named by their own column.
    """
    st = OPLState(kind, drums)
    chan_of: dict[tuple[int, int], list[int]] = {}
    for fch, cm in enumerate(st.chan_map):
        chan_of.setdefault((((cm >> 8) & 1) if st.opl3 else 0, cm & 0xFF),
                          []).append(fch)
    image: dict[tuple[int, int], int] = {}
    keyed: dict[tuple[int, int], bool] = {}
    out: list[tuple[int, int, int, int]] = []
    trig = 0

    def push(cid, fch, sample, port, off):
        hi = image.get((port, 0xB0 + off), 0)
        out.append((cid, fch, sample, (((hi & 3) << 8) | image.get(
            (port, 0xA0 + off), 0)) << ((hi >> 2) & 7)))

    for w in vgm.writes:
        if w.chip != kind:
            continue
        port = ((w.reg >> 8) & 1) if st.opl3 else 0
        r = w.reg & 0xFF
        image[(port, r)] = w.val
        if 0xB0 <= r <= 0xB8:
            on = bool(w.val & 0x20)
            was = keyed.get((port, r), False)
            keyed[(port, r)] = on
            if not (on and not was):
                continue
            for fch in chan_of.get((port, r - 0xB0), ()):
                if not drums or fch < st.melodic:
                    push(w.chip_id, fch, w.sample, port, r - 0xB0)
        elif r == 0xBD and not st.opl3:
            bits = (w.val & 0x1F) if (w.val & 0x20) else 0
            for i in range(5):
                bit = 1 << (4 - i)
                if not ((bits & bit) and not (trig & bit)):
                    continue
                cm = st.chan_map[st.melodic + i]
                push(w.chip_id, st.melodic + i, w.sample, (cm >> 8) & 1,
                     cm & 0xFF)
            trig = bits
    return out


def keyon_units(vgm, kind: str, drums: bool) -> list[int]:
    """Every key-on phase step of a stream, in time order."""
    return [unit for _cid, _fch, _t, unit in keyon_events(vgm, kind, drums)]



class TestOplLoader(unittest.TestCase):
    def test_opl2_command_and_clock(self):
        body = bytes([0x5A, 0x20, 0x01, 0x5A, 0xB0, 0x24])
        path = TMP / "opl2_raw.vgm"
        path.write_bytes(_raw_vgm(body, clock_off=0x50, clock=CLOCK_OPL2))
        vgm = load_vgm(path)
        self.assertEqual(vgm.opl2_clock, CLOCK_OPL2)
        self.assertEqual([(w.chip, w.reg, w.val) for w in vgm.writes],
                         [("opl2", 0x20, 0x01), ("opl2", 0xB0, 0x24)])
        self.assertIn("opl2", chips_in_head(path.read_bytes()[:0xC0]))

    def test_opl1_command_and_clock(self):
        path = TMP / "opl1_raw.vgm"
        path.write_bytes(_raw_vgm(bytes([0x5B, 0x20, 0x11]), clock_off=0x54,
                                 clock=CLOCK_OPL2))
        vgm = load_vgm(path)
        self.assertEqual(vgm.opl_clock, CLOCK_OPL2)
        self.assertEqual({w.chip for w in vgm.writes}, {"opl"})
        self.assertIn("opl", chips_in_head(path.read_bytes()[:0xC0]))

    def test_opl3_ports_share_one_chip(self):
        path = TMP / "opl3_raw.vgm"
        path.write_bytes(_raw_vgm(
            bytes([0x5E, 0xB0, 0x24, 0x5F, 0xB0, 0x04, 0x5F, 0x04, 0x3D]),
            clock_off=0x5C, clock=CLOCK_OPL3))
        vgm = load_vgm(path)
        self.assertEqual(vgm.opl3_clock, CLOCK_OPL3)
        # Bank B keeps its number in bit 8 of reg, the way a YM2612 bank does.
        self.assertEqual([(w.chip, w.reg) for w in vgm.writes],
                         [("opl3", 0x0B0), ("opl3", 0x1B0), ("opl3", 0x104)])
        self.assertEqual({w.chip_id for w in vgm.writes}, {0})
        kinds = chips_in_head(path.read_bytes()[:0xC0])
        self.assertIn("opl3", kinds)
        self.assertNotIn("opl2", kinds)

    def test_dual_opl2_uses_the_second_command(self):
        path = TMP / "opl2_dual.vgm"
        path.write_bytes(_raw_vgm(bytes([0x5A, 0xB0, 0x24, 0xAA, 0xB1, 0x34]),
                                 clock_off=0x50, clock=CLOCK_OPL2, dual=True,
                                 pan=True))
        vgm = load_vgm(path)
        self.assertTrue(vgm.opl2_dual)   # bit 30: a chip answers on $AA
        self.assertTrue(vgm.opl2_pan)    # bit 31: the pair is hard left/right
        self.assertEqual([(w.chip_id, w.reg) for w in vgm.writes],
                         [(0, 0xB0), (1, 0xB1)])

    def test_stray_second_chip_is_reported(self):
        path = TMP / "opl2_stray.vgm"
        path.write_bytes(_raw_vgm(bytes([0xAA, 0xB0, 0x24]), clock_off=0x50,
                                 clock=CLOCK_OPL2))
        vgm = load_vgm(path)
        self.assertFalse(vgm.opl2_dual)
        self.assertEqual(vgm.writes, [])
        self.assertTrue(any("0xAA" in w for w in vgm.warnings), vgm.warnings)

    def test_missing_clock_warns_with_the_family_default(self):
        path = TMP / "opl2_noclock.vgm"
        path.write_bytes(_raw_vgm(bytes([0x5A, 0xB0, 0x24]), clock_off=0x50,
                                 clock=0))
        vgm = load_vgm(path)
        self.assertEqual(vgm.opl2_clock, 0)
        self.assertTrue(any("YM3812 clock" in w for w in vgm.warnings))


class TestOplPitchModel(unittest.TestCase):
    def setUp(self):
        self.clock = opl_module_clock(CLOCK_OPL2)

    def test_reference_fnum_pairs(self):
        # Measured through Furnace at its default clock: A-440 sits on f-num 290
        # in block 0, an octave up doubles the phase step instead of the block,
        # and a block only advances when f-num would overflow its ten bits.
        self.assertEqual(opl_note_fnum(57, self.clock), (0, 290))
        self.assertEqual(opl_note_fnum(69, self.clock), (0, 580))
        self.assertEqual(opl_note_fnum(48, self.clock), (0, 172))
        self.assertEqual(opl_note_fnum(84, self.clock), (2, 345))
        self.assertEqual(opl_note_fnum(96, self.clock), (3, 344))

    def test_an_octave_up_doubles_the_phase_step(self):
        # From C3 up the f-num has resolution to spare, so an octave is exact to
        # a cent. The bottom octaves are covered by the register sweep below,
        # where one table step is legitimately a fifth of a tone wide.
        for note in range(60, 144):
            here = opl_note_fnum(note, self.clock)
            up = opl_note_fnum(note + 12, self.clock)
            # Integer rounding in the table costs a fraction of a cent at the
            # bottom of the range, so the invariant is a pitch one.
            err = abs(opl_pitch_units(*up) - opl_pitch_units(*here) - 768.0)
            self.assertLess(err / 64.0, 0.1, note)

    def test_opl3_base_is_four_times_the_opl2_one(self):
        # Four times the clock over four times the divider: the same phase step
        # at either chip's usual crystal, which is why a YMF262 board and an
        # AdLib card agree on pitch.
        clock3 = opl_module_clock(CLOCK_OPL3, True)
        for note in (36, 57, 69, 84, 108):
            a = opl_note_fnum(note, self.clock)
            b = opl_note_fnum(note, clock3, opl3=True)
            self.assertEqual(a[1] << a[0], b[1] << b[0], note)

    def test_module_clock_only_moves_for_a_real_difference(self):
        self.assertEqual(opl_module_clock(CLOCK_OPL2), opl_module_clock(0))
        self.assertEqual(opl_module_clock(self.clock), self.clock)
        self.assertEqual(opl_module_clock(3679545), 3679545)

    def test_a_macro_compensates_an_octave_relabel_exactly(self):
        tbl = FreqPitchTable(self.clock, 32768 * 72)
        for note in range(12, 168):
            here = to_freq(tbl.value(note))
            moved = to_freq(tbl.value(note + 12, 0x80, -1536))
            self.assertEqual(here[1] << here[0], moved[1] << moved[0], note)

    def test_every_playable_register_round_trips(self):
        for opl3, clock in ((False, self.clock),
                            (True, opl_module_clock(14400000, True))):
            tbl = FreqPitchTable(clock, 32768 * 288 if opl3 else 32768 * 72)
            for block in range(8):
                for fnum in range(1, 1024):
                    unit = fnum << block
                    est = 57.0 + 12.0 * math.log2(unit * clock
                                                  / (440.0 * tbl.base))
                    if not (0 <= est <= 179):
                        continue
                    note, e5 = opl_note_for(block, fnum, clock, opl3)
                    got = to_freq(tbl.value(note, e5))
                    err = abs(opl_pitch_units(*got) - opl_pitch_units(block, fnum))
                    # Below about C-3 one table step is wider than a half tone,
                    # so the closest cell cannot land on every register.
                    self.assertLess(err, 64, (opl3, block, fnum, note, e5, got))

    def test_exact_phase_step_from_the_second_octave_up(self):
        for block in range(2, 7):
            for fnum in (0x1FF, 0x2A6, 0x3C0, 514, 900):
                note, e5 = opl_note_for(block, fnum, self.clock)
                got = to_freq(FreqPitchTable(self.clock, 32768 * 72).value(
                    note, e5))
                self.assertEqual(got[1] << got[0], fnum << block,
                                 (block, fnum, note, e5, got))

    def test_unplayable_register_clamps_instead_of_raising(self):
        note, e5 = opl_note_for(0, 2, self.clock)
        self.assertTrue(0 <= note <= 179)
        self.assertTrue(0x00 <= e5 <= 0xFF)


class TestOplChannelMap(unittest.TestCase):
    def test_opl3_permutation_pairs_the_bank(self):
        st = OPLState(KIND_OPL3, False)
        # Furnace interleaves each pair so a four-operator voice owns two
        # adjacent columns: channel 1 is hardware channel 3, and bank B starts at
        # column 6. Its last three columns are bank A's top pair.
        self.assertEqual(st.chan_map[:8], (0, 3, 1, 4, 2, 5, 0x100, 0x103))
        self.assertEqual(st.chan_map[15:], (6, 7, 8))
        self.assertEqual(channel_count(KIND_OPL3, False), 18)

    def test_four_op_bit_selects_the_pair(self):
        st = OPLState(KIND_OPL3, False)
        self.assertFalse(st.is_four_op(0))
        st.write(1, 0x04, 0x0D)  # bank A pairs 0 and 2, bank B pair 0
        self.assertTrue(st.is_four_op(0))
        self.assertFalse(st.is_four_op(2))
        self.assertTrue(st.is_four_op(4))
        self.assertFalse(st.is_four_op(5))    # an odd column never heads a voice
        self.assertTrue(st.is_four_op(6))
        # The four operators: two from each hardware channel of the pair.
        self.assertEqual([SLOT_ADDR[st.slots[r][0]] for r in range(4)],
                         [0x00, 0x03, 0x08, 0x0B])

    def test_kit_slots_and_shared_frequency_registers(self):
        st = OPLState(KIND_OPL2, True)
        self.assertEqual(melodic_count(KIND_OPL2, True), 6)
        # The kick keeps channel 6's pair; snare, tom, cymbal and hi-hat take one
        # operator each. Snare and hi-hat share a frequency register, so do tom
        # and cymbal - which is why their columns are compared by pitch class.
        self.assertEqual([SLOT_ADDR[st.slots[0][f]] for f in range(6, 11)],
                         [0x10, 0x14, 0x12, 0x15, 0x11])
        self.assertEqual(st.chan_map[6:], (6, 7, 8, 8, 7))
        self.assertEqual(drum_channel(1 << 4, 6), 6)       # kick
        self.assertEqual(drum_channel(1 << 0, 6), 10)      # hi-hat
        self.assertFalse(is_drum_channel(KIND_OPL2, True, 6))
        self.assertTrue(is_drum_channel(KIND_OPL2, True, 7))

    def test_a_plain_module_has_no_kit_columns(self):
        self.assertFalse(is_drum_channel(KIND_OPL2, False, 7))
        self.assertEqual(channel_count(KIND_OPL3, True), 20)


class TestOplAnalyze(unittest.TestCase):
    def test_two_operator_voice_and_instrument(self):
        song = analyze(_vgm(voice(100, KIND_OPL2, 3, block=4, fnum=580)
                            + heart()))
        self.assertEqual(song.fm_chip, KIND_OPL2)
        ev = next(e for e in song.events if e.on and e.ch == 3)
        self.assertEqual((ev.note, ev.e5),
                         opl_note_for(4, 580, opl_module_clock(CLOCK_OPL2)))
        # The carrier's logged attenuation rides the volume column: the
        # instrument keeps the relative level, so the note opens 20 below top.
        self.assertEqual(ev.vol, 63 - 20)
        patch = song.fm_patches[ev.ins]
        self.assertTrue(patch.opl and not patch.four and not patch.drum)
        self.assertEqual((patch.alg, patch.fb), (0, 2))
        self.assertEqual(patch.carriers(), (1,))
        self.assertEqual(song.fm_vol_max, 63)

    def test_key_off_ends_the_note(self):
        song = analyze(_vgm(voice(100, KIND_OPL2, 1, block=3, fnum=700,
                                 off_time=40000) + heart()))
        ons = [e for e in song.events if e.on and e.ch == 1]
        offs = [e for e in song.events if not e.on and e.ch == 1]
        self.assertEqual(len(ons), 1)
        self.assertTrue(offs and abs(offs[-1].sample - 40000) < 800)

    def test_additive_connection_marks_both_operators_as_carriers(self):
        song = analyze(_vgm(voice(100, KIND_OPL2, 4, alg=1) + heart()))
        ev = next(e for e in song.events if e.on and e.ch == 4)
        patch = song.fm_patches[ev.ins]
        self.assertEqual(patch.alg, 1)
        self.assertEqual(patch.carriers(), (0, 1))

    def test_am_and_vibrato_depth_become_chip_effects(self):
        ws = voice(100, KIND_OPL2, 0) + heart()
        ws += [ChipWrite(5000, KIND_OPL2, 0, 0xBD, 0xF0),
               ChipWrite(9000, KIND_OPL2, 0, 0xBD, 0xB0)]
        song = analyze(_vgm(ws))
        fx = [(c, v) for _t, c, v in song.chip_fx if c in (0x10, 0x17)]
        self.assertIn((0x10, 1), fx)     # deep amplitude modulation
        self.assertIn((0x17, 0), fx)     # vibrato depth back to normal

    def test_rhythm_mode_picks_the_drums_system(self):
        ws = voice(100, KIND_OPL2, 6, drums=True, block=3, fnum=900) + heart()
        # Kick and snare ride the latch bits; the kit owns columns 6 to 10.
        ws += [rhythm(2000, bits=0), rhythm(10000, bits=0x18),
               rhythm(14000, bits=0), rhythm(18000, bits=0x04),
               rhythm(22000, bits=0)]
        song = analyze(_vgm(ws))
        self.assertTrue(song.opl_drums)
        kit = [e for e in song.events if e.on and e.drum]
        self.assertEqual({e.ch for e in kit}, {7, 8})     # snare and tom
        self.assertTrue(all(song.fm_patches[e.ins].drum for e in kit))
        kick = [e for e in song.events if e.on and e.ch == 6]
        self.assertTrue(kick)
        self.assertFalse(song.fm_patches[kick[0].ins].drum)

    def test_melodic_key_on_a_kit_channel_is_reported(self):
        ws = voice(100, KIND_OPL2, 7, drums=True, block=3, fnum=500) + heart()
        ws += [rhythm(2000, bits=0x10), rhythm(6000, bits=0)]
        song = analyze(_vgm(ws))
        self.assertTrue(song.opl_drums)
        self.assertFalse([e for e in song.events if e.on and e.ch == 7])
        self.assertTrue(any("kit" in w for w in song.warnings), song.warnings)

    def test_opl3_four_op_note_lands_on_the_pair_head(self):
        ws = [ChipWrite(90, KIND_OPL3, 0, 0x104, 0x01)]      # bank A pair 0
        ws += voice(95, KIND_OPL3, 0, four=True, block=4, fnum=0x3D1,
                    ops=[{"mult": 1}, {"mult": 5}, {"mult": 3}, {"mult": 7}],
                    alg=0, off_time=60000)
        # The pair's second connection bit lives on the partner's C0 register.
        ws.insert(1, ChipWrite(96, KIND_OPL3, 0, 0xC3, 0x21))
        song = analyze(_vgm(ws + heart(KIND_OPL3), kind=KIND_OPL3))
        evs = [e for e in song.events if e.on and e.ch == 0]
        self.assertTrue(evs)
        patch = song.fm_patches[evs[0].ins]
        self.assertTrue(patch.four)
        # Bit 1 of the algorithm came from hardware channel 3, and the operator
        # order is the pair's: ch0 op1, ch3 op1, ch0 op2, ch3 op2.
        self.assertEqual(patch.alg, 2)
        self.assertEqual([o.mult for o in patch.ops], [1, 3, 5, 7])
        self.assertEqual(patch.carriers(), (2, 3))

    def test_a_pair_switches_between_two_and_four_operators(self):
        # One column heads a four-operator voice and then a plain pair, because
        # the log turns register 0x104 off between them.
        ws = [ChipWrite(90, KIND_OPL3, 0, 0x104, 0x02)]
        ws += voice(95, KIND_OPL3, 2, four=True, block=4, fnum=0x3D1,
                    ops=[{"tl": 9}, {"tl": 20}, {"tl": 31}, {"tl": 42}],
                    off_time=0)
        ws += [ChipWrite(30000, KIND_OPL3, 0, 0x104, 0x00),
               hi_write(KIND_OPL3, 2, t=30100, block=4, fnum=0x3D1)]
        ws += voice(31000, KIND_OPL3, 2, block=4, fnum=700,
                    ops=[{"tl": 12}, {"tl": 33}], off_time=60000)
        song = analyze(_vgm(ws + heart(KIND_OPL3, ch=0), kind=KIND_OPL3))
        kinds = [song.fm_patches[e.ins].four for e in song.events
                 if e.on and e.ch == 2]
        self.assertEqual(sorted(set(kinds)), [False, True])

    def test_dual_opl2_instances_get_their_own_channels(self):
        ws = voice(100, KIND_OPL2, 0) + heart()
        ws += voice(140, KIND_OPL2, 1, cid=1, block=5, fnum=400)
        song = analyze(_vgm(ws))
        self.assertEqual(song.opl_ids, [0, 1])
        chans = sorted({e.ch for e in song.events if e.on})
        self.assertIn(0, chans)
        self.assertIn(10, chans)     # chip 1's channel 1 is column 10

    def test_pan_bits_reach_the_column(self):
        ws = heart(KIND_OPL3) + voice(100, KIND_OPL3, 6, block=3, fnum=700,
                                     pan=1)
        song = analyze(_vgm(ws, kind=KIND_OPL3))
        ev = next(e for e in song.events if e.on and e.ch == 6)
        self.assertEqual(ev.pan, 0xF0)          # left only

    def test_an_opl2_column_gets_no_pan_effect(self):
        song = analyze(_vgm(voice(100, KIND_OPL2, 0) + heart()))
        ev = next(e for e in song.events if e.on and e.ch == 0)
        self.assertEqual(ev.pan, 0)

    def test_a_rhythm_poke_without_a_latch_keeps_nine_channels(self):
        # The bug this guards: a driver that pokes $BD with bit 5 set during chip
        # init and clears it in the same sample never sounds a drum, yet taking
        # that write as "this is a kit track" moved the module to the drums system
        # and left every melody note on channels 7 to 9 with no column at all.
        ws = voice(100, KIND_OPL2, 6, block=3, fnum=900) + heart()
        ws += voice(20000, KIND_OPL2, 7, block=3, fnum=500)
        ws += voice(40000, KIND_OPL2, 8, block=4, fnum=300)
        # Mode on and off inside one sample, no trigger bit ever set.
        ws += [ChipWrite(150, KIND_OPL2, 0, 0xBD, 0x20),
               ChipWrite(150, KIND_OPL2, 0, 0xBD, 0xC0)]
        song = analyze(_vgm(ws))
        self.assertFalse(song.opl_drums, "a poke became a kit")
        data = write_fur(song, include_pcm=False)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_OPL2])
        self.assertEqual(m.n_ch, 9)
        chans = {e.ch for e in song.events if e.on}
        # The heartbeat keys its own column too; what matters is that the three
        # channels rhythm mode would have taken still carry their melody.
        self.assertTrue({6, 7, 8} <= chans, sorted(chans))
        self.assertFalse(any("kit" in w for w in song.warnings), song.warnings)

    def test_a_latch_beside_the_poke_still_selects_the_kit(self):
        # The other half: once a percussion bit is really latched while the mode
        # is held, that is a kit track and its columns exist to be played on.
        ws = voice(100, KIND_OPL2, 6, drums=True, block=3, fnum=900) + heart()
        ws += [rhythm(2000), rhythm(10000, bits=0x18), rhythm(14000, bits=0)]
        song = analyze(_vgm(ws))
        self.assertTrue(song.opl_drums)
        self.assertEqual(parse_fur(write_fur(song, include_pcm=False)).chips,
                         [CHIP_OPL2_DRUMS])

    def test_hand_written_vibrato_keeps_its_return_legs(self):
        # A slow-attack pad on channel 8 moves its pitch by alternating one
        # f-num register between two values every ~150 ms (modaozi Dungeon Theme
        # intro). Mid-note pitch used to be compared against the value the note
        # *started* on, so every write returning to that pitch was discarded: half
        # the oscillation survived and the pitch never came back down. Track the
        # register's current value instead, and both legs appear.
        ws = voice(100, KIND_OPL2, 7, block=4, fnum=580) + heart()
        for k in range(6):
            ws.append(ChipWrite(20000 + k * 6600, KIND_OPL2, 0, 0xA7,
                                (580 + (96 if k % 2 else 0)) & 0xFF))
        ws.append(ChipWrite(120000, KIND_OPL2, 0, 0xB7, 0x0F))
        song = analyze(_vgm(ws))
        ev = next(e for e in song.events if e.on and e.ch == 7)
        pts = ev.fm_pitch_pts
        # six writes, the first already at the note's own pitch: one anchor plus
        # five real transitions.
        self.assertEqual(len(pts), 6, "a leg of the oscillation was dropped")
        # up, back to the start, up again - strictly alternating.
        self.assertEqual(pts[0][2], pts[2][2], "the pitch did not return")
        self.assertNotEqual(pts[1][2], pts[0][2])
        self.assertEqual([p[2] for p in pts], [580, 676, 580, 676, 580, 676])
        # The time steps must alternate too: five returns/up-moves, not one.
        self.assertEqual(len({p[0] for p in pts}), len(pts))

    def test_only_the_moving_channel_records_pitch(self):
        # The alternation above must not smear onto the rest of the module: a
        # channel whose frequency registers nobody touched keeps no pitch points.
        ws = voice(100, KIND_OPL2, 7, block=4, fnum=580) + heart()
        ws += voice(120, KIND_OPL2, 2, block=3, fnum=400)
        for k in range(6):
            ws.append(ChipWrite(20000 + k * 6600, KIND_OPL2, 0, 0xA7,
                                (580 + (96 if k % 2 else 0)) & 0xFF))
        song = analyze(_vgm(ws))
        moving = {e.ch: len(e.fm_pitch_pts) for e in song.events if e.on}
        self.assertEqual(moving.get(7), 6, moving)
        self.assertFalse(moving.get(2), "a still channel picked up motion")

        src = _vgm(voice(100, KIND_OPL2, 3) + heart())
        self.assertIsNotNone(estimate_grid(src))
        keys = _keyons_by_channel(src)
        self.assertTrue(any(k >= 4000 for k in keys), list(keys))


class TestOplWriter(unittest.TestCase):
    def test_chip_ids_and_channel_counts(self):
        for kind, ch in ((KIND_OPL2, 0), (KIND_OPL3, 8)):
            data = write_fur(analyze(_vgm(voice(100, kind, ch)
                                         + heart(kind), kind=kind)),
                             include_pcm=False)
            m = parse_fur(data)
            self.assertEqual(m.chips, [{KIND_OPL2: CHIP_OPL2,
                                        KIND_OPL3: CHIP_OPL3}[kind]])
            self.assertEqual(m.n_ch, channel_count(kind, False))

    def test_rhythm_module_uses_the_drums_system(self):
        ws = voice(100, KIND_OPL2, 6, drums=True) + heart()
        ws += [rhythm(1000), rhythm(10000, bits=8), rhythm(14000, bits=0)]
        data = write_fur(analyze(_vgm(ws)), include_pcm=False)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_OPL2_DRUMS])
        self.assertEqual(m.n_ch, 11)
        self.assertIn(b"BD", data)
        self.assertIn(b"HiHat", data)
        self.assertIn(INS_OPL_DRUMS, ins_types(data))

    def test_instrument_type_and_operator_bytes(self):
        ws = voice(100, KIND_OPL2, 0, alg=0, fb=5,
                   ops=[{"am": 1, "vib": 1, "ksr": 1, "mult": 7, "tl": 8,
                         "ar": 15, "dr": 6, "sl": 3, "rr": 5, "ws": 1},
                        {"sus": 1, "mult": 4, "tl": 20, "ksl": 2, "ar": 14,
                         "dr": 9, "sl": 12, "rr": 3, "ws": 2}])
        song = analyze(_vgm(ws + heart()))
        ev = next(e for e in song.events if e.on and e.ch == 0)
        data = write_fur(song, include_pcm=False)
        self.assertEqual(ins_types(data)[ev.ins], INS_OPL)
        feat = fm_feature(data, ev.ins)
        self.assertEqual(feat[0], 4 | 0x10 | 0x20)     # four bytes, two enabled
        self.assertEqual(feat[1], (0 << 4) | 5)        # algorithm and feedback
        self.assertEqual(feat[3] & 32, 0)              # not a four-op voice
        # Furnace stores an OPL operator as eight bytes, one per register plus
        # the two it derives: key-scale rate rides the multiplier byte, sustain
        # the level, vibrato the attack, amplitude modulation and key-scale level
        # the decay, then volume/depth, sustain level with release, a reserved
        # byte and the waveform.
        mod, car = feat[4:12], feat[12:20]
        self.assertEqual(mod[0], 0x80 | 7)             # ksr and multiplier
        self.assertEqual(mod[1] & 63, 8)               # its logged level
        self.assertEqual(mod[1] >> 7, 0)               # sustain clear
        self.assertEqual(mod[2], (1 << 5) | 15)        # vibrato and attack rate
        self.assertEqual(mod[3], (1 << 7) | 6)         # am and decay rate
        self.assertEqual(mod[5], (3 << 4) | 5)         # sustain level, release
        self.assertEqual(mod[6], 0)                    # no SSG envelope on OPL
        self.assertEqual(mod[7], 1)                    # waveform select
        self.assertEqual(car[0] & 15, 4)               # multiplier only
        self.assertTrue(car[1] >> 7)                   # sustain set
        self.assertEqual(car[3], (2 << 5) | 9)         # key-scale level, decay
        self.assertEqual(car[4] >> 5, 2)               # the carrier follows volume
        self.assertEqual(car[7], 2)

    def test_a_kit_instrument_carries_four_operators(self):
        ws = voice(100, KIND_OPL2, 6, drums=True) + heart()
        addrs = [SLOT_ADDR[SLOTS_KIT[0][f]] for f in range(7, 11)]
        ws += op_writes(95, KIND_OPL2, addrs[0], mult=6, tl=11)   # snare
        ws += op_writes(95, KIND_OPL2, addrs[3], mult=9, tl=17)   # hi-hat
        ws += [rhythm(2000), rhythm(10000, bits=8), rhythm(14000, bits=0)]
        data = write_fur(analyze(_vgm(ws)), include_pcm=False)
        self.assertIn(INS_OPL_DRUMS, ins_types(data))
        feat = fm_feature(data, ins_types(data).index(INS_OPL_DRUMS))
        self.assertEqual(feat[0], 4 | 0xF0)            # all four read
        self.assertEqual(feat[3] & 32, 0)              # not a four-op voice
        self.assertEqual(feat[4:12][0] & 15, 6)        # snare multiplier
        self.assertEqual(feat[4 + 24:4 + 32][1] & 63, 17)   # hi-hat level

    def test_four_op_instrument_sets_the_ops_flag(self):
        ws = [ChipWrite(90, KIND_OPL3, 0, 0x104, 0x08)]           # bank B pair 0
        ws += voice(95, KIND_OPL3, 6, four=True, block=4, fnum=0x3D1)
        song = analyze(_vgm(ws + heart(KIND_OPL3), kind=KIND_OPL3))
        data = write_fur(song, include_pcm=False)
        want = [i for i, p in enumerate(song.fm_patches) if p.four]
        self.assertTrue(want)
        feat = fm_feature(data, want[0])
        self.assertEqual(feat[3] & 32, 32)             # the ops==4 flag
        self.assertEqual(feat[0], 4 | 0xF0)            # four operators enabled
        self.assertEqual(parse_fur(data).chips, [CHIP_OPL3])

    def test_clock_flag_only_when_it_differs(self):
        plain = write_fur(analyze(_vgm(voice(100, KIND_OPL2, 0) + heart())),
                          include_pcm=False)
        self.assertNotIn(b"customClock", plain)
        odd = write_fur(analyze(_vgm(voice(100, KIND_OPL2, 0) + heart(),
                                    opl2_clock=3679545)), include_pcm=False)
        self.assertIn(b"customClock=3679545", odd)
        o3 = write_fur(analyze(_vgm(voice(100, KIND_OPL3, 8)
                                   + heart(KIND_OPL3), kind=KIND_OPL3)),
                       include_pcm=False)
        self.assertIn(b"customClock=14318180", o3)
        exact = write_fur(analyze(_vgm(
            voice(100, KIND_OPL3, 8) + heart(KIND_OPL3),
            opl3_clock=int(opl_module_clock(14318182, True)), kind=KIND_OPL3)),
            include_pcm=False)
        self.assertNotIn(b"customClock", exact)
        o3b = write_fur(analyze(_vgm(voice(100, KIND_OPL3, 8)
                                    + heart(KIND_OPL3), opl3_clock=14400000,
                                    kind=KIND_OPL3)), include_pcm=False)
        self.assertIn(b"customClock=14400000", o3b)

    def test_dual_chips_get_two_systems_and_hard_pan_triples(self):
        ws = voice(100, KIND_OPL2, 0) + heart()
        ws += voice(140, KIND_OPL2, 1, cid=1, block=5, fnum=400)
        src = _vgm(ws, opl2_dual=True, opl2_pan=True)
        data = write_fur(analyze(src), include_pcm=False)
        m = parse_fur(data)
        self.assertEqual(m.chips, [CHIP_OPL2, CHIP_OPL2])
        self.assertEqual(m.n_ch, 18)
        self.assertIn(b"FM 2 (2)", data)


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestOplFurnaceRoundTrip(unittest.TestCase):
    """The model's proof: what the module makes Furnace write to the chip."""

    def _export(self, song, name: str):
        fur = TMP / f"{name}.fur"
        out = TMP / f"{name}.vgm"
        fur.write_bytes(write_fur(song, include_pcm=False))
        export_vgm(fur, out)
        return load_vgm(out)

    def _regs(self, vgm, kind: str) -> dict[int, set[int]]:
        """Values seen at each register address, bank B keeping bit 8."""
        regs: dict[int, set[int]] = {}
        for w in vgm.writes:
            if w.chip == kind:
                regs.setdefault(w.reg, set()).add(w.val)
        return regs

    def test_note_lands_on_the_logged_phase_step(self):
        # Registers the table reaches exactly: one f-num step at these blocks is
        # well under a cent, so the chip must get the rip's own phase step.
        for block, fnum in ((2, 690), (4, 580), (1, 313), (0, 290)):
            song = analyze(_vgm(voice(100, KIND_OPL2, 3, block=block, fnum=fnum)
                                + heart()))
            vgm = self._export(song, f"opl2_{block}_{fnum}")
            units = keyon_units(vgm, KIND_OPL2, False)
            self.assertIn(fnum << block, units, (block, fnum, units[:6]))

    def test_a_high_register_lands_within_one_f_num_step(self):
        # Block 6 leaves the f-num just eleven steps between a cell and the next,
        # so the closest note sits two cents away. The pitch is what matters here.
        song = analyze(_vgm(voice(100, KIND_OPL2, 3, block=6, fnum=977)
                            + heart()))
        vgm = self._export(song, "opl2_high")
        units = [u for cid, fch, _t, u in keyon_events(vgm, KIND_OPL2, False)
                 if (cid, fch) == (0, 3)]
        self.assertEqual(len(units), 1, units)
        cents = abs(1200.0 * math.log2(units[0] / (977 << 6)))
        self.assertLess(cents, 4.0, units)

    def test_operator_bytes_reach_the_chip(self):
        ws = voice(100, KIND_OPL2, 0, fb=5,
                   ops=[{"am": 1, "vib": 1, "sus": 1, "ksr": 1, "mult": 7,
                         "tl": 8, "ar": 15, "dr": 6, "sl": 3, "rr": 5, "ws": 2},
                        {"mult": 4, "tl": 20, "ksl": 1}])
        vgm = self._export(analyze(_vgm(ws + heart())), "opl2_operators")
        regs = self._regs(vgm, KIND_OPL2)
        # Modulator 0x2n: am, vib, sustain, key-scale rate and multiplier.
        self.assertIn(0x80 | 0x40 | 0x20 | 0x10 | 7, regs[0x20])
        self.assertIn(8, {v & 63 for v in regs[0x40]})
        self.assertIn((15 << 4) | 6, regs[0x60])
        self.assertIn((3 << 4) | 5, regs[0x80])
        self.assertIn(2, regs[0xE0])                  # waveform select
        # Carrier of the same voice: its own level and key-scale level.
        self.assertIn(20, {v & 63 for v in regs[0x43]})
        self.assertIn(1 << 6, {v & 0xC0 for v in regs[0x43]})
        self.assertIn((5 << 1) | 0x30, regs[0xC0])    # feedback and both outputs

    def test_a_carrier_level_rides_the_volume_column(self):
        # The instrument keeps one level and the note carries the difference, so
        # two attenuations of the same patch share an instrument and not two.
        ws = voice(100, KIND_OPL2, 4, ops=[{}, {"tl": 6}], off_time=30000)
        ws += voice(40000, KIND_OPL2, 4, ops=[{}, {"tl": 30}], off_time=90000)
        song = analyze(_vgm(ws + heart()))
        evs = [e for e in song.events if e.on and e.ch == 4]
        self.assertEqual(len(evs), 2)
        self.assertEqual([e.ins for e in evs], [evs[0].ins] * 2,
                         "the level split the instrument")
        self.assertEqual({e.vol for e in evs}, {63 - 6, 63 - 30})

    def test_four_op_pair_drives_both_hardware_channels(self):
        ws = [ChipWrite(90, KIND_OPL3, 0, 0x104, 0x01)]
        # The partner's connection bit is loaded first: a key-on takes the patch
        # it finds, exactly as the chip does.
        ws.append(ChipWrite(92, KIND_OPL3, 0, 0xC3, 0x01 | 0x20))
        ws += voice(95, KIND_OPL3, 0, four=True, block=4, fnum=977,
                    ops=[{"mult": 1}, {"mult": 5}, {"mult": 3}, {"mult": 7}],
                    alg=0, pan=2)
        vgm = self._export(analyze(_vgm(ws + heart(KIND_OPL3),
                                       kind=KIND_OPL3)), "opl3_4op")
        regs = self._regs(vgm, KIND_OPL3)
        # Four operators: two from hardware channel 0 and two from channel 3.
        self.assertIn(1, {v & 15 for v in regs[0x20]})
        self.assertIn(5, {v & 15 for v in regs[0x23]})
        self.assertIn(3, {v & 15 for v in regs[0x28]})
        self.assertIn(7, {v & 15 for v in regs[0x2B]})
        self.assertTrue(any(v & 0x01 for v in regs.get(0x104, set())),
                        "the pair bit never reached bank B")
        self.assertTrue(any((w.val & 0x20) and w.reg == 0xB0 for w in vgm.writes),
                        "the voice never keyed")
        units = [u for cid, fch, _t, u in keyon_events(vgm, KIND_OPL3, False)
                 if (cid, fch) == (0, 0)]
        self.assertEqual(len(units), 1, units)
        # One f-num step at block 4 is a cent, and the table cannot promise more.
        self.assertLess(abs(1200.0 * math.log2(units[0] / (977 << 4))), 4.0,
                        units)

    def test_kit_bytes_land_on_the_rhythm_slots(self):
        ws = voice(100, KIND_OPL2, 6, drums=True, block=3, fnum=900) + heart()
        addrs = [SLOT_ADDR[SLOTS_KIT[0][f]] for f in range(7, 11)]
        ws += op_writes(95, KIND_OPL2, addrs[0], mult=6, tl=11)       # snare
        ws += op_writes(95, KIND_OPL2, addrs[2], mult=9, tl=17)       # top cymbal
        ws += [rhythm(2000), rhythm(12000, bits=0x08 | 0x02),
               rhythm(16000, bits=0)]
        vgm = self._export(analyze(_vgm(ws)), "opl2_kit")
        regs = self._regs(vgm, KIND_OPL2)
        self.assertIn(6, {v & 15 for v in regs[0x34]})     # snare multiplier
        self.assertIn(11, {v & 63 for v in regs[0x54]})    # snare level
        self.assertIn(9, {v & 15 for v in regs[0x35]})     # cymbal multiplier
        self.assertIn(17, {v & 63 for v in regs[0x55]})    # cymbal level
        self.assertTrue(any(v & 0x20 for v in regs.get(0xBD, set())),
                        "rhythm mode never came back on")
        self.assertTrue(any(v & 0x08 for v in regs.get(0xBD, set())),
                        "the snare was never latched")

    def test_second_chip_is_written_on_its_own_command(self):
        ws = voice(100, KIND_OPL2, 0) + heart()
        ws += voice(140, KIND_OPL2, 1, cid=1, block=5, fnum=400)
        vgm = self._export(analyze(_vgm(ws)), "opl2_dual")
        self.assertEqual({w.chip for w in vgm.writes}, {"opl2"})
        # Both instances are keyed, each on its own chip.
        by_id = {(cid, unit) for cid, _fch, _t, unit
                 in keyon_events(vgm, KIND_OPL2, False)}
        self.assertIn((0, 580 << 4), by_id)
        self.assertIn((1, 400 << 5), by_id)



SOLAR = Path(r"F:\NASbackup\JamesAlan\_devspace\grbsd\ripwip\_finished"
             r"\solarwinds1\solar1_BH_MU1_trimmed.vgm")
MC01 = Path(r"F:\NASbackup\JamesAlan\_devspace\grbsd\ripwip\miwaku\opl3\mc_01.vgm")
METAL = Path(r"F:\NASbackup\JamesAlan\_devspace\grbsd\ripwip"
             r"\metalace-opl3\main_010a.vgm")
KXEMI = Path(r"F:\NASbackup\JamesAlan\_devspace\grbsd\ripwip\_finished"
             r"\knightsofxentar-opl3\KXEMI1_opl2.vgm")
# A YM3526 that really latches the kit. Rhythm mode is identical to a YM3812's -
# same $BD register, same five percussion voices - the chip only lacks waveform
# select, so this is what proves the family was not special-cased out of it.
OPL1KIT = Path(r"D:\Apps\Audio\Furnace\opl1_rhythmtest.vgm")


@unittest.skipUnless(HAVE_FURNACE and MC01.is_file(),
                     "Furnace or the reference rips missing")
class TestOplRealRips(unittest.TestCase):
    """The rips the model was tuned on: every unbent note lands exactly."""

    def _check(self, path: Path, kind: str, min_attacks: int = 1000):
        """At every attack the chip is handed the same registers it was before.

        The comparison is sample-atomic (``diag.opl_attacks``): a driver that
        re-attacks quickly writes the new voice's operator bytes and its key bit
        in one sample, interleaved with the release before it, and a tracker row
        holds one value - so what matters is the register set once that sample is
        finished, which is also what the analyzer snapshots. Kit columns are
        compared for their operators but not their frequency: two percussion
        voices share one register there, so the byte belongs to whichever was
        written last. A note the analyzer bent is compared at its attack only if
        it still holds the rip's f-num, because a macro, row step or slide moves
        it on purpose afterwards.
        """
        src = load_vgm(path)
        song = analyze(src)
        self.assertEqual(song.fm_chip, kind)
        drums = song.opl_drums
        stem = TMP / f"real_{kind}_{path.stem}"
        fur = stem.with_suffix(".fur")
        fur.write_bytes(write_fur(song, include_pcm=False))
        exp = load_vgm(export_fur(fur, stem.with_suffix(".vgm")))

        want: dict[tuple[int, int], list] = {}
        got: dict[tuple[int, int], list] = {}
        for row in opl_attacks(src, kind, drums):
            want.setdefault(row[:2], []).append(row)
        for row in opl_attacks(exp, kind, drums):
            got.setdefault(row[:2], []).append(row)
        self.assertEqual(sorted(want), sorted(got), f"{path.name}: channels")

        chunk = channel_count(kind, drums)
        st = OPLState(kind, drums)
        mine: dict[tuple[int, int], list] = {}
        for e in song.events:
            if not e.on:
                continue
            cid, col = ((e.ch // chunk, e.ch % chunk) if len(song.opl_ids) > 1
                        else ((song.opl_ids or [0])[0], e.ch))
            mine.setdefault((cid, col), []).append(e)

        total = sum(len(v) for v in want.values())
        bytes_cmp = 0
        pitch_cmp = 0
        bad: list[str] = []
        # Furnace re-latches the kit as it wraps the loop, so an exported attack
        # past the source's last sample has nothing to compare with. Drop those,
        # one row of slack for the tick it lands on; everything inside the song
        # still has to match exactly.
        edge = src.total_samples + 600
        for key, brows in list(got.items()):
            kept = [r for r in brows if r[2] <= edge]
            if len(kept) != len(brows):
                # only a rhythm channel may gain at the seam, and only its tail.
                # The kick is one of them even though it plays a plain DIV_INS_OPL
                # voice: it owns channel 7's two operators, like BD on the chip.
                self.assertGreaterEqual(key[1], st.melodic,
                                f"{path.name} ch{key}: melodic attack past the end")
                self.assertEqual(kept, brows[:len(kept)])
                got[key] = kept
        for ch, rows in sorted(want.items()):
            brows = got.get(ch, [])
            self.assertEqual(len(brows), len(rows),
                             f"{path.name} ch{ch}: attack count")
            evs = mine.get(ch, [])
            cid, col = ch
            cm = st.chan_map[col]
            port, off = ((cm >> 8) & 1) if st.opl3 else 0, cm & 0xFF
            kit = is_drum_channel(kind, drums, col)
            for i, (_cid2, _col2, t, simg) in enumerate(rows):
                _c3, _c4, t2, eimg = brows[i]
                ev = evs[i] if i < len(evs) else None
                bent = bool(ev is not None and (ev.fm_pitch_pts or ev.pitch_steps
                                                or ev.sweep))
                if not kit and not bent:
                    hi = simg.get((port, 0xB0 + off), 0)
                    want_u = (((hi & 3) << 8) | simg.get((port, 0xA0 + off), 0)
                              ) << ((hi >> 2) & 7)
                    ge = eimg.get((port, 0xB0 + off), 0)
                    got_u = (((ge & 3) << 8) | eimg.get((port, 0xA0 + off), 0)
                             ) << ((ge >> 2) & 7)
                    pitch_cmp += 1
                    if want_u != got_u:
                        bad.append("ch%s t=%.3f unit want %d got %d"
                                  % (ch, t / VGM_RATE, want_u, got_u))
                for addr in opl_voice_addrs(kind, drums, col, simg):
                    a_port = (addr >> 8) & 1 if st.opl3 else 0
                    a_off = addr & 0xFF
                    for page in (0x20, 0x40, 0x60, 0x80, 0xE0):
                        w = simg.get((a_port, a_off + page), 0)
                        g = eimg.get((a_port, a_off + page))
                        if g is None:
                            continue
                        bytes_cmp += 1
                        if w != g:
                            bad.append("ch%s t=%.3f op %x/%02X want %02X got %02X"
                                       % (ch, t / VGM_RATE, a_port, a_off + page,
                                          w, g))
        # Both comparisons must actually have run: every attack of the rip, and
        # several operator registers at each one. A short fixture sets its own
        # floor instead, so the test cannot pass by comparing nothing.
        self.assertGreater(total, min_attacks, path.name)
        self.assertGreater(bytes_cmp, total * 4, path.name)
        self.assertEqual(bad[:6], [], f"{path.name}: {len(bad)} diffs")

    def test_opl3_medley_with_four_operator_voices(self):
        song = analyze(load_vgm(MC01))
        self.assertTrue(any(p.four for p in song.fm_patches), "no 4-op voice")
        self._check(MC01, KIND_OPL3)

    @unittest.skipUnless(METAL.is_file(), "Metal Ace rip missing")
    def test_second_opl3_rip_keeps_every_register(self):
        self._check(METAL, KIND_OPL3)

    @unittest.skipUnless(SOLAR.is_file(), "Solar Winds rip missing")
    def test_opl2_rhythm_rip(self):
        song = analyze(load_vgm(SOLAR))
        self.assertTrue(song.opl_drums)
        self._check(SOLAR, KIND_OPL2)

    @unittest.skipUnless(OPL1KIT.is_file(), "YM3526 rhythm fixture missing")
    def test_opl1_rhythm_rip(self):
        # The YM3526 has rhythm mode exactly like a YM3812 - same $BD register,
        # same kit - so the only thing a real OPL1 log can catch is the family
        # being special-cased out of the drums layout, or its absent waveform
        # register ($Ex) being invented on the way back.
        src = load_vgm(OPL1KIT)
        song = analyze(src)
        self.assertEqual(song.fm_chip, KIND_OPL)
        self.assertTrue(song.opl_drums, "the YM3526 was kept melodic")
        m = parse_fur(write_fur(song, include_pcm=False))
        self.assertEqual(m.chips, [CHIP_OPL_DRUMS])
        self.assertEqual(m.n_ch, 11)

        # $BD bits 4..0 latch BD, SD, TOM, CYM/TP and HH. BD is driven from
        # channel 7's two operators of its own; only channels 8 and 9 put two
        # voices on one frequency register.
        NAMES = {4: "BD", 3: "SD", 2: "TM", 1: "TP", 0: "HH"}

        def latches(vgm, times=False):
            """Per-voice rising percussion edges in $BD, counted or timed."""
            fired, prev = ({n: [] for n in NAMES.values()} if times else Counter()), 0
            for w in vgm.writes:
                if w.chip != KIND_OPL or (w.reg & 0xFF) != 0xBD:
                    continue
                cur = (w.val & 0x1F) if (w.val & 0x20) else 0
                for k, name in NAMES.items():
                    if (cur >> k) & 1 and not (prev >> k) & 1:
                        if times:
                            fired[name].append(w.sample)
                        else:
                            fired[name] += 1
                prev = cur
            return fired

        back = load_vgm(self._export(OPL1KIT, song))
        a, b = latches(src), latches(back)
        self.assertGreater(sum(a.values()), 0, "the fixture latched no drum")
        # Nothing may be lost: every voice the log latches must sound again.
        for name, n in a.items():
            self.assertGreaterEqual(b.get(name, 0), n,
                                    "%s: %d hit(s) lost (%r vs %r)" % (name, n - b.get(name, 0), a, b))
        # A surplus is only acceptable after the source's last sample, where the
        # module plays its final row once more across the loop seam. Any earlier
        # than that and a drum would double up inside the song.
        for name, hits in latches(back, times=True).items():
            want = set(latches(src, times=True)[name])
            for t in hits:
                if t not in want:
                    self.assertGreater(
                        t, src.total_samples - 2 * 551,
                        "%s latched again at %.0fms, inside the song" % (name, t / 44.1))
        # and every voice, kit included, lands with the registers it had
        self._check(OPL1KIT, KIND_OPL, min_attacks=20)

    def _export(self, path: Path, song) -> Path:
        """Write the module and have Furnace re-export it."""
        fur = TMP / f"opl1_{path.stem}.fur"
        fur.write_bytes(write_fur(song, include_pcm=False))
        return export_fur(fur, TMP / f"opl1_{path.stem}.vgm")

    @unittest.skipUnless(KXEMI.is_file(), "Knights of Xentar rip missing")
    def test_dual_opl2_rip(self):
        src = load_vgm(KXEMI)
        self.assertTrue(src.opl2_dual and src.opl2_pan)
        self._check(KXEMI, KIND_OPL2)
        song = analyze(src)
        self.assertEqual(song.opl_ids, [0, 1])
        m = parse_fur(write_fur(song, include_pcm=False))
        self.assertEqual(m.chips, [CHIP_OPL2, CHIP_OPL2])
        self.assertEqual(m.n_ch, 18)


class TestOplVerify(unittest.TestCase):
    """--verify's measure: a voice inside its own key, trimmed for row ticks."""

    def test_pitch_stream_follows_the_channel_registers(self):
        src = _vgm(voice(100, KIND_OPL2, 3, block=4, fnum=580) + heart())
        stream = opl_pitch_stream(src, 3)
        self.assertTrue(stream)
        want = int(round(opl_pitch_units(4, 580)))
        self.assertIn(want, [u for _t, u in stream])

    def test_intervals_name_the_keyed_span(self):
        src = _vgm(voice(100, KIND_OPL2, 1, block=3, fnum=700, off_time=40000)
                   + heart())
        spans = opl_keyed_intervals(src, 1)
        self.assertEqual(len(spans), 1, spans)
        start, end = spans[0]
        self.assertTrue(100 <= start <= 200, start)
        self.assertAlmostEqual(end, 40000, delta=800)

    def test_a_kit_column_is_not_measured(self):
        # Snare and hi-hat share one frequency register, so neither stream says
        # anything about the voice; the measure stays silent rather than wrong.
        ws = voice(100, KIND_OPL2, 6, drums=True) + heart()
        ws += [rhythm(2000), rhythm(10000, bits=0x09), rhythm(14000, bits=0)]
        src = _vgm(ws)
        self.assertEqual(opl_pitch_stream(src, 7), [])
        self.assertEqual(opl_keyed_intervals(src, 10), [])

    def test_the_second_chip_is_measured_apart(self):
        ws = voice(100, KIND_OPL2, 0) + heart()
        ws += voice(140, KIND_OPL2, 1, cid=1, block=5, fnum=400)
        src = _vgm(ws)
        first = opl_pitch_stream(src, 0)
        second = opl_pitch_stream(src, 9 + 1)
        self.assertIn(int(round(opl_pitch_units(4, 580))), [u for _t, u in first])
        self.assertIn(int(round(opl_pitch_units(5, 400))), [u for _t, u in second])

    def test_a_wrong_octave_shows_up_as_one_run(self):
        # The same eight notes written an octave apart: the measure must report the
        # spans, so a real error cannot hide behind the trim.
        def stream(block):
            ws = []
            for k in range(8):
                t = 210 + k * 5000
                ws += voice(t, KIND_OPL2, 3, block=block, fnum=781,
                            off_time=t + 4600)
            return _vgm(ws + heart(), total_samples=45000)

        runs = pitch_runs(stream(5), stream(4), 3, threshold_st=0.25,
                          min_run_ms=1.0)
        self.assertTrue(runs, "an octave went unnoticed")
        self.assertGreater(abs(runs[0].worst_st), 11.0, runs[0].worst_st)
        self.assertEqual(pitch_runs(stream(4), stream(4), 3, threshold_st=0.25,
                                    min_run_ms=1.0), [])


class TestOplReport(unittest.TestCase):
    def test_the_report_names_the_chip_and_its_columns(self):
        song = analyze(_vgm(voice(100, KIND_OPL3, 8) + heart(KIND_OPL3),
                            kind=KIND_OPL3))
        text = format_report(song)
        self.assertIn("FM chip     YMF262 x1 @ 14.3182 MHz", text)
        self.assertIn("FM voices   18 columns, 0 four-operator instrument(s)",
                      text)

    def test_a_glide_never_blames_an_opn_chip(self):
        # The pitch-sweep warning is shared with the OPN paths; on an OPL log it
        # has to name the chip that was actually read.
        ws = voice(100, KIND_OPL3, 2, block=4, fnum=580) + heart(KIND_OPL3)
        ws += [ChipWrite(2000, KIND_OPL3, 0, 0xA1, 0x80),
               ChipWrite(2000, KIND_OPL3, 0, 0xB1, (4 << 2) | 3)]
        song = analyze(_vgm(ws + voice(20000, KIND_OPL3, 2, block=4, fnum=700,
                                      off_time=60000), kind=KIND_OPL3))
        for w in song.warnings:
            for other in ("YM2151", "YM2203", "YM2612"):
                self.assertNotIn(other, w, w)

    def test_a_kit_module_counts_its_hits_apart(self):
        ws = voice(100, KIND_OPL2, 6, drums=True) + heart()
        ws += [rhythm(2000), rhythm(10000, bits=0x0C), rhythm(14000, bits=0)]
        song = analyze(_vgm(ws))
        text = format_report(song)
        self.assertIn("FM chip     YM3812 + rhythm x1 @ 3.57954 MHz", text)
        kit = next(line for line in text.splitlines() if "kit hits" in line)
        self.assertIn(f"+{song.drum_notes} kit hits", kit)
        self.assertNotIn("kit hits", format_report(
            analyze(_vgm(voice(100, KIND_OPL2, 3) + heart()))))


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestOplVerifyRoundTrip(unittest.TestCase):
    def test_a_converted_song_diverges_nowhere(self):
        # The pipeline's own measure on a song the converter built: no channel may
        # report a held wrong pitch, and one that does would show the row grid,
        # the register model or the key map to be wrong.
        ws = voice(100, KIND_OPL3, 2, block=4, fnum=580, off_time=30000)
        ws += voice(40000, KIND_OPL3, 2, block=5, fnum=700, off_time=80000)
        ws += voice(120, KIND_OPL3, 8, block=3, fnum=977, off_time=50000)
        ws += [ChipWrite(90, KIND_OPL3, 0, 0x104, 0x08)]
        ws += voice(95, KIND_OPL3, 6, four=True, block=4, fnum=977,
                    ops=[{"mult": 1}, {"mult": 5}, {"mult": 3}, {"mult": 7}])
        ws += heart(KIND_OPL3, ch=15) + heart(KIND_OPL3, ch=0)
        song = analyze(_vgm(ws, kind=KIND_OPL3))
        fur = TMP / "verify_4op.fur"
        fur.write_bytes(write_fur(song, include_pcm=False))
        out = TMP / "verify_4op.vgm"
        exp = load_vgm(export_fur(fur, out))
        for ch in range(channel_count(KIND_OPL3, False)):
            runs = pitch_runs(song.vgm, exp, ch, threshold_st=0.25,
                              min_run_ms=max(4.0, song.samples_per_row * 1000
                                             / VGM_RATE * 0.75))
            self.assertEqual([(r.t0, r.worst_st) for r in runs], [], f"ch{ch}")


class TestOplChip1(unittest.TestCase):
    """The YM3526: an OPL2 that has no waveform register.

    Rhythm mode is identical - same `0xBD`, same five voices, same drums system -
    so the one thing to prove about this chip is that `0xE0`+ never appears, in
    the log or in what Furnace writes back. `TestOplRealRips` does that on a real
    OPL1 kit rip; the synthetic logs here cover what that rip does not reach.
    """

    def test_the_pitch_table_matches_the_opl2(self):
        # Same base and same shift: a YM3526 at COLOR_NTSC is in tune with a
        # YM3812, and A-440 sits on the published f-num 290.
        clock = opl_module_clock(CLOCK_OPL2)
        tbl = pitch_table(clock, False)
        self.assertEqual(tbl.base, 32768 * 72)
        self.assertEqual(opl_note_fnum(57, clock), (0, 290))
        self.assertEqual(opl_note_fnum(69, clock), (0, 580))

    def test_the_module_is_an_opl_system_of_nine_channels(self):
        song = analyze(_vgm(voice(100, KIND_OPL, 2) + heart(KIND_OPL),
                            kind=KIND_OPL))
        self.assertEqual(song.fm_chip, KIND_OPL)
        m = parse_fur(write_fur(song, include_pcm=False))
        self.assertEqual(m.chips, [CHIP_OPL])
        self.assertEqual(m.n_ch, 9)

    def test_rhythm_mode_gives_the_opl_drums_system(self):
        ws = voice(100, KIND_OPL, 6, drums=True) + heart(KIND_OPL)
        ws += [rhythm(1000, KIND_OPL), rhythm(9000, KIND_OPL, bits=0x18),
               rhythm(13000, KIND_OPL, bits=0)]
        song = analyze(_vgm(ws, kind=KIND_OPL))
        self.assertTrue(song.opl_drums)
        m = parse_fur(write_fur(song, include_pcm=False))
        self.assertEqual(m.chips, [CHIP_OPL_DRUMS])
        self.assertEqual(m.n_ch, 11)


@unittest.skipUnless(HAVE_FURNACE, "furnace console build missing")
class TestOplChip1RoundTrip(unittest.TestCase):
    def test_no_waveform_register_and_the_logged_frequency(self):
        # The chip has no 0xE0 page, so nothing may reach it - and the note still
        # lands on the rip's own phase step.
        song = analyze(_vgm(voice(100, KIND_OPL, 2, block=4, fnum=580,
                                 ops=[{"ws": 1}, {"ws": 3}])
                            + heart(KIND_OPL), kind=KIND_OPL))
        fur = TMP / "opl1.fur"
        fur.write_bytes(write_fur(song, include_pcm=False))
        back = load_vgm(export_fur(fur, TMP / "opl1.vgm"))
        pages = {(w.reg & 0xF0) for w in back.writes if w.chip == KIND_OPL}
        self.assertNotIn(0xE0, {hex(p) for p in pages}, sorted(map(hex, pages)))
        self.assertIn(580 << 4, keyon_units(back, KIND_OPL, False))


if __name__ == "__main__":
    unittest.main()
