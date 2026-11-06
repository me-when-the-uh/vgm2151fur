"""OPL-family (YM3526/YM3812/YMF262) support: pitch mapping and chip state.

The OPL chips have two operators per channel (an OPL3 pair can be joined into
one four-operator voice), a 6-bit TL and one connection bit. Their register
image cannot share the OPM/OPN byte fields.  ``OPLState`` keeps the flat
register image a VGM log writes and reads it back through the slot and channel
tables of Furnace's own OPL platform (``src/engine/platform/opl.cpp``). A
converted channel, operator or drum is the one Furnace will drive.

Furnace numbers OPL3 channels by pair, not by the hardware channel order:
``CHAN_MAP`` carries that permutation and puts the port in bit 8, exactly as
the platform's ``chanMapOPL3`` does.  A note is labelled with the channel's own
f-number. A four-operator voice reads the registers of its pair's first
channel, which is what the chip keys.

Pitch works through ``DivPitchTable`` in frequency mode: the engine turns a
note plus ``E5xx`` (1/128 semitone in linear mode) into one number, and the
platform splits that into block/f-num by taking the highest power of two not
above it (``toFreq``).  ``opl_note_for`` inverts the pair of steps. A log
register therefore comes back as the note/E5 combination that writes it again.
"""

from __future__ import annotations

import math

from vgm2151fur.fm import FMPatch, OpRegs

OPL_DEFAULT_CLOCK = 3579545   # YM3526; COLOR_NTSC, Furnace's system default
OPL2_DEFAULT_CLOCK = 3579545  # YM3812; the AdLib/Sound Blaster clock
OPL3_DEFAULT_CLOCK = 14318180  # YMF262; COLOR_NTSC * 4

KIND_OPL = "opl"
KIND_OPL2 = "opl2"
KIND_OPL3 = "opl3"
# The YMF278B/OPL4's FM half is OPL3-shaped (two banks, four-operator pairs,
# waveform select) but runs off a different divider: Furnace's DivPlatformOPL
# sets chipFreqBase to 32768*684 and its clock to 33.8688 MHz (opl.cpp:3390).
KIND_OPL4 = "opl4"
# The Y8950's FM half is an OPL2 (waveform select and rhythm included); its
# ADPCM voice is handled outside this state.
KIND_Y8950 = "y8950"

# DivPlatformOPL::chipFreqBase, keyed by whether the chip has two banks. The
# OPL3 runs eighteen channels off four times the prescaler. At its usual
# 14.318 MHz clock a note lands on the same unit value an OPL2 finds at 3.58.
FREQ_BASE = {False: 32768 * 72, True: 32768 * 288}
# DivPlatformOPL::setOPLType for chipType 4 overrides the OPL3 base with the
# OPL4's divider (32768*684). Using the OPL3 base here puts every FM note more
# than an octave flat, so the pitch path carries it separately.
FREQ_BASE_OPL4 = 32768 * 684


def opl3_like(kind: str) -> bool:
    """True for the two-bank OPL chips: YMF262 and the OPL4's FM half."""
    return kind in (KIND_OPL3, KIND_OPL4)
# pitchTable.init's maximum, and the f-num ceiling of toFreq.
MAX_UNIT = 0x1FFFF
MAX_FNUM = 0x3FF
# toFreq starts a block at this unit value (OPLL_C_NUM in opl.cpp).
BLOCK_MIN = 686

N = 255  # an unused slot, as Furnace spells it

# slotMap: operator slot index -> register address (bit 8 selects port 1).
SLOT_ADDR = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05,
    0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D,
    0x10, 0x11, 0x12, 0x13, 0x14, 0x15,
    0x100, 0x101, 0x102, 0x103, 0x104, 0x105,
    0x108, 0x109, 0x10A, 0x10B, 0x10C, 0x10D,
    0x110, 0x111, 0x112, 0x113, 0x114, 0x115,
)

# slots[operator row][Furnace channel] -> slot index.  The OPL2 rows are the
# two operators of one channel; the OPL3 rows add the second channel of a
# four-operator pair.
SLOTS_OPL2 = (
    (0, 1, 2, 6, 7, 8, 12, 13, 14),
    (3, 4, 5, 9, 10, 11, 15, 16, 17),
)
SLOTS_OPL2_DRUMS = (
    # BD keeps both of channel 6; then snare, tom, cymbal and hi-hat take the
    # single rhythm operator of channels 7 and 8.
    (0, 1, 2, 6, 7, 8, 12, 16, 14, 17, 13),
    (3, 4, 5, 9, 10, 11, 15, N, N, N, N),
)
SLOTS_OPL3 = (
    (0, 6, 1, 7, 2, 8, 18, 24, 19, 25, 20, 26, 30, 31, 32, 12, 13, 14),
    (3, 9, 4, 10, 5, 11, 21, 27, 22, 28, 23, 29, 33, 34, 35, 15, 16, 17),
    (6, N, 7, N, 8, N, 24, N, 25, N, 26, N, N, N, N, N, N, N),
    (9, N, 10, N, 11, N, 27, N, 28, N, 29, N, N, N, N, N, N, N),
)
SLOTS_OPL3_DRUMS = (
    (0, 6, 1, 7, 2, 8, 18, 24, 19, 25, 20, 26, 30, 31, 32, 12, 16, 14, 17, 13),
    (3, 9, 4, 10, 5, 11, 21, 27, 22, 28, 23, 29, 33, 34, 35, 15, N, N, N, N),
    (6, N, 7, N, 8, N, 24, N, 25, N, 26, N, N, N, N, N, N, N, N),
    (9, N, 10, N, 11, N, 27, N, 28, N, 29, N, N, N, N, N, N, N, N),
)

# chanMap[Furnace channel] + 0xA0/0xB0/0xC0 -> the channel's register block.
CHAN_MAP_OPL2 = (0, 1, 2, 3, 4, 5, 6, 7, 8)
CHAN_MAP_OPL2_DRUMS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 8, 7)
CHAN_MAP_OPL3 = (0, 3, 1, 4, 2, 5,
                 0x100, 0x103, 0x101, 0x104, 0x102, 0x105,
                 0x106, 0x107, 0x108, 6, 7, 8)
CHAN_MAP_OPL3_DRUMS = CHAN_MAP_OPL3 + (8, 7)

# orderedOpsL: slot row -> instrument operator in four-operator mode.
ORDERED_OPS_L = (0, 2, 1, 3)

# Channel layout per chip and mode: (channel count, melodic channel count).
LAYOUT = {
    (KIND_OPL, False): (9, 9),
    (KIND_OPL2, False): (9, 9),
    (KIND_OPL3, False): (18, 18),
    (KIND_OPL, True): (11, 6),
    (KIND_OPL2, True): (11, 6),
    (KIND_OPL3, True): (20, 15),
    # The OPL4's FM half, before the caller appends the 24 wave columns.
    (KIND_OPL4, False): (18, 18),
    (KIND_OPL4, True): (20, 15),
    # The Y8950's FM half is OPL2-shaped. Its ADPCM column is appended by the
    # caller, not part of this state.
    (KIND_Y8950, False): (9, 9),
    (KIND_Y8950, True): (11, 6),
}


def slots_for(kind: str, drums: bool) -> tuple[tuple[int, ...], ...]:
    if opl3_like(kind):
        return SLOTS_OPL3_DRUMS if drums else SLOTS_OPL3
    return SLOTS_OPL2_DRUMS if drums else SLOTS_OPL2


def chan_map_for(kind: str, drums: bool) -> tuple[int, ...]:
    if opl3_like(kind):
        return CHAN_MAP_OPL3_DRUMS if drums else CHAN_MAP_OPL3
    return CHAN_MAP_OPL2_DRUMS if drums else CHAN_MAP_OPL2


def channel_count(kind: str, drums: bool) -> int:
    return LAYOUT[(kind, drums)][0]


def melodic_count(kind: str, drums: bool) -> int:
    return LAYOUT[(kind, drums)][1]


# Rhythm key-on bits of register 0xBD, in the order Furnace's channels
# follow: bit 4 is the bass drum on channel `melodic`, then each step down
# names the next channel (snare, tom, cymbal, hi-hat).
DRUM_BITS = ((1 << 4, "BD"), (1 << 3, "SD"), (1 << 2, "TM"),
             (1 << 1, "TP"), (1 << 0, "HH"))


def drum_channel(bit: int, melodic: int) -> int:
    """Furnace channel a 0xBD trigger bit drives."""
    for i, (mask, _name) in enumerate(DRUM_BITS):
        if mask == bit:
            return melodic + i
    raise ValueError(f"not a rhythm bit: {bit}")


def is_drum_channel(kind: str, drums: bool, fch: int) -> bool:
    """True when Furnace drives the channel from a DIV_INS_OPL_DRUMS voice.

    The kick keeps two operators and its own channel. It takes a normal OPL
    instrument, and only the four single-operator columns use the kit voice.
    """
    return drums and fch > LAYOUT[(kind, drums)][1]


def opl_carrier_octave(patch) -> int:
    """Octave of the loudest carrier relative to the channel f-num.

    The OPL multiplier table is OPN's without the halved values: 0 is a half,
    1 unison and then multiples up. Only an exact power of two can be cancelled
    by a whole-octave note move. A multiplier of 3/5/6/... leaves the note
    where it is.

    The YM2413 shares that table except at zero: Nuked-OPLL's ``pg_multi``
    starts ``1, 2, 4, 6...``, so a multiplier of 0 is unison and never an
    octave down. An OPLL preset has no readable operators at all, so only the
    user patch can move.
    """
    if not getattr(patch, "opl", False) or getattr(patch, "drum", False):
        return 0
    opll = bool(getattr(patch, "opll", False))
    best_tl = 0x40 if not opll else 0x10
    best_octave: int | None = None
    for i in patch.carriers():
        op = patch.ops[i]
        mask = 0x0F if opll else 0x3F
        tl = op.tl & mask
        if tl >= mask:
            continue
        mult = op.mult & 15
        if opll and mult == 0:
            ratio = 1.0
        else:
            ratio = 0.5 if mult == 0 else float(mult)
        octave = (
            int(round(math.log2(ratio)))
            if ratio in (0.5, 1.0, 2.0, 4.0, 8.0)
            else None
        )
        if tl < best_tl:
            best_tl = tl
            best_octave = octave
        elif tl == best_tl and octave is not None:
            if best_octave is None or octave > best_octave:
                best_octave = octave
    return best_octave or 0


# --- pitch ------------------------------------------------------------------

# In linear pitch mode ``calcFreq`` folds the E5 byte into the table offset
# unchanged. One unit is 1/128 semitone, and the instrument pitch macro
# (``pitch2``) is added to the same offset. The ``mul`` argument the OPL platform
# passes (2) only scales the legacy non-linear path, which this project never
# writes - its modules keep Furnace's default linear pitch.


class FreqPitchTable:
    """DivPitchTable in frequency mode, for one (clock, base) pair.

    ``value`` returns the engine's frequency unit, which ``toFreq`` splits into
    block and f-num. The table holds one octave and the note's octave shifts
    it, rounding the carry in. A closed form alone misses by a unit here and
    there.
    """

    def __init__(self, clock: float, base: int, tuning: float = 440.0,
                 maximum: int = MAX_UNIT):
        self.clock = float(clock)
        self.base = float(base)
        self.tuning = tuning
        self.max = maximum
        self.shift = 14
        while self.shift > 0:
            nbase = (self.shift - 5) * 12
            fbase = tuning * 2.0 ** ((nbase + 3) / 12.0)
            if round(fbase * (self.base / self.clock)) <= maximum:
                break
            self.shift -= 1
        self.pitch = [0] * 13
        for i in range(13):
            nbase = i + (self.shift - 5) * 12
            fbase = tuning * 2.0 ** ((nbase + 3) / 12.0)
            self.pitch[i] = round(fbase * (self.base / self.clock))
        self.diff = [self.pitch[i + 1] - self.pitch[i] for i in range(12)]

    def offset(self, note: int, e5: int = 0x80, pitch2: int = 0) -> int:
        """The raw table offset of a note, its E5 byte and a pitch macro value."""
        return (note << 7) + (int(e5) - 0x80) + int(pitch2)

    def value(self, note: int, e5: int = 0x80, pitch2: int = 0) -> int:
        """The unit Furnace writes for a note, its E5 byte and pitch macro."""
        return self._value_at(self.offset(note, e5, pitch2))

    def _value_at(self, offset: int) -> int:
        coarse = offset >> 7
        if coarse < 0:
            coarse = 0
        fine = offset & 127
        index = coarse % 12
        octave = self.shift - (coarse // 12)
        if octave < 0:
            return self.max
        root, diff = self.pitch[index], self.diff[index]
        if octave > 0:
            carry = bool(root & (1 << (octave - 1)))
            root >>= octave
            if carry:
                root += 1
            carry = bool(diff & (1 << (octave - 1)))
            diff >>= octave
            if carry:
                diff += 1
        return root + ((diff * fine) >> 7)


_TABLES: dict[tuple[float, int], FreqPitchTable] = {}

# The clocks Furnace gives its OPL systems when no customClock is written:
# COLOR_NTSC and COLOR_NTSC * 4. A rip at the same nominal integer clock still
# has to build its pitch table from these floats, or the last unit of a f-num
# can disagree with what the module computes.
FURNACE_CLOCK = 315_000_000.0 / 88.0
FURNACE_CLOCK_OPL3 = FURNACE_CLOCK * 4.0
CLOCK_DEFAULT_TOLERANCE = 1  # Hz; the writer omits customClock inside it

# The YM2413 platform's own clock presets (``DivPlatformOPLL::setFlags``).
# ``clockSel`` is how Furnace names them in the UI, and matching one exactly
# keeps the module on a built-in selection instead of a custom clock.
OPLL_CLOCK_SEL = (
    (0, FURNACE_CLOCK),                             # COLOR_NTSC
    (1, (283.75 * 15625.0 + 25.0) * 4.0 / 5.0),     # COLOR_PAL x4/5
    (2, 4_000_000.0),
    (3, FURNACE_CLOCK / 2.0),
)


def opl_module_clock(clock: int | float, opl3: bool = False) -> float:
    """The clock the module will run at: Furnace's default or the rip's own."""
    default = FURNACE_CLOCK_OPL3 if opl3 else FURNACE_CLOCK
    if not clock or abs(float(clock) - default) <= CLOCK_DEFAULT_TOLERANCE:
        return default
    return float(clock)


def pitch_table(
    clock: float, opl3: bool = False, freq_base: int | None = None,
) -> FreqPitchTable:
    """The module's table for one chip and clock.

    ``opl3`` selects the two-bank layout, and ``freq_base`` overrides the
    divider when the variant is not an OPL3 (the OPL4 uses ``32768*684``).
    Neither is derivable from the clock, which a rip may set to anything.
    """
    base = FREQ_BASE[bool(opl3)] if freq_base is None else int(freq_base)
    key = (round(opl_module_clock(clock, opl3), 4), base)
    tbl = _TABLES.get(key)
    if tbl is None:
        tbl = FreqPitchTable(key[0], base)
        _TABLES[key] = tbl
    return tbl


def to_freq(unit: int) -> tuple[int, int]:
    """DivPlatformOPL::toFreq for an instrument with no fixed block.

    ``bsr`` in Furnace counts the bits of ``unit / OPLL_C_NUM`` rather than
    returning its highest set bit. The block is therefore one higher than an
    MSB index suggests, and the f-num keeps the next lower half-octave span.
    Verified against a round trip through Furnace's own export: every note of
    three and a half octaves lands on the same block and f-num this function
    predicts.
    """
    block = 0
    if unit > 0:
        index = unit // BLOCK_MIN
        if index > 0:
            block = index.bit_length()
    block = max(0, min(7, block))
    fnum = unit >> block
    if fnum > MAX_FNUM:
        fnum = MAX_FNUM
    return block, fnum


def opl_pitch_units(block: int, fnum: int) -> float:
    """Pitch in 1/64 semitones; only differences are meaningful."""
    return 768.0 * (math.log2(max(1, fnum)) + block)


def opl_unit_for(
    note: int, clock: float, e5: int = 0x80, opl3: bool = False,
    pitch2: int = 0, freq_base: int | None = None,
) -> int:
    """The engine frequency unit a note, its E5 byte and a macro value produce.

    ``pitch2`` is an instrument pitch macro's offset in 1/128 semitone.
    ``get()`` folds it into the same offset as the fine tune. A note whose
    instrument carries an octave compensation therefore still lands on the
    rip's register. ``freq_base`` is the variant's divider (OPL4).
    """
    return pitch_table(clock, opl3, freq_base).value(note, e5, pitch2)


def opl_note_fnum(
    note: int, clock: float, e5: int = 0x80, opl3: bool = False,
    pitch2: int = 0, freq_base: int | None = None,
) -> tuple[int, int]:
    """(block, fnum) Furnace writes for a pattern note on this clock."""
    return to_freq(opl_unit_for(note, clock, e5, opl3, pitch2, freq_base))


_NOTES: dict[tuple[int, int, float, bool, int], tuple[int, int]] = {}


def opl_note_for(
    block: int, fnum: int, clock: float, opl3: bool = False,
    freq_base: int | None = None,
) -> tuple[int, int]:
    """Source (block, fnum) -> (pattern note, E5 byte).

    The pair is searched in the domain the engine actually reaches: a note plus
    its E5 byte, scaled by ``E5_STEP`` inside the table. Only reachable values
    are scored, and what comes back is what Furnace will write. When the target
    sits between two of them the closer one wins, then the smaller fine tune. A
    register below note 0 or above B-9 has no pattern cell. The window is
    clamped into the range, and the closest playable note wins.
    """
    block = max(0, min(7, int(block)))
    fnum = max(1, min(MAX_FNUM, int(fnum)))
    tbl = pitch_table(clock, opl3, freq_base)
    key = (block, fnum, round(float(clock), 4), bool(opl3), int(tbl.base))
    hit = _NOTES.get(key)
    if hit is not None:
        return hit
    want = opl_pitch_units(block, fnum)
    unit = max(1, fnum << block)
    est = 57.0 + 12.0 * math.log2(unit * tbl.clock / (tbl.tuning * tbl.base))
    center = max(0, min(179, int(math.floor(est))))
    lo_note = max(0, min(179, center - 3))
    hi_note = max(lo_note, min(179, center + 4))
    best: tuple[float, int, int, int] | None = None  # (pitch err, |e5|, note, e5)
    for note in range(lo_note, hi_note + 1):
        # Pitch rises with the fine tune, and bisection finds the crossing. The
        # search is in E5 units, one of which moves the table by two steps.
        lo, hi = -128, 127
        while lo < hi:
            mid = (lo + hi) // 2
            if opl_pitch_units(*to_freq(tbl.value(note, 0x80 + mid))) < want:
                lo = mid + 1
            else:
                hi = mid
        for off in range(max(-128, lo - 2), min(127, lo + 2) + 1):
            got = to_freq(tbl.value(note, 0x80 + off))
            err = abs(opl_pitch_units(*got) - want)
            cand = (err, abs(off), note, 0x80 + off)
            if best is None or cand < best:
                best = cand
    assert best is not None
    out = (best[2], best[3])
    _NOTES[key] = out
    return out


# --- OPLL (YM2413) pitch ----------------------------------------------------
#
# The YM2413 platform is a separate core: DivPlatformOPLL has its own
# CHIP_FREQBASE (1180068), a maximum unit of 0xffff and OPLL_C_NUM 343, and its
# f-num is nine bits wide. The note/E5 search is the same, only the table and
# the split differ.

OPLL_DEFAULT_CLOCK = 3579545
OPLL_FREQ_BASE = 1180068
OPLL_MAX_UNIT = 0xFFFF
OPLL_C_NUM = 343
OPLL_MAX_FNUM = 0x1FF

_OPLL_TABLES: dict[float, FreqPitchTable] = {}
_OPLL_NOTES: dict[tuple[int, int, float], tuple[int, int]] = {}


def opll_module_clock(clock: int | float) -> float:
    """The clock the OPLL module will run at: Furnace's default or the rip's."""
    if not clock or abs(float(clock) - FURNACE_CLOCK) <= CLOCK_DEFAULT_TOLERANCE:
        return FURNACE_CLOCK
    for _sel, hz in OPLL_CLOCK_SEL:
        if abs(float(clock) - hz) <= CLOCK_DEFAULT_TOLERANCE:
            return hz
    return float(clock)


def opll_clock_flag(clock: int | float) -> str:
    """The chip flag that puts the OPLL platform at `clock`.

    Furnace's four named selections are exact, so a rip whose header matches
    one of them travels as ``clockSel`` (the way the tracker itself stores it)
    and anything else as a custom clock.
    """
    if not clock:
        return ""
    hz = opll_module_clock(clock)
    for sel, target in OPLL_CLOCK_SEL:
        if abs(hz - target) <= 0.5:
            return "" if sel == 0 else f"clockSel={sel}"
    return f"customClock={int(round(hz))}"


def opll_pitch_table(clock: float) -> FreqPitchTable:
    key = round(opll_module_clock(clock), 4)
    tbl = _OPLL_TABLES.get(key)
    if tbl is None:
        tbl = FreqPitchTable(key, OPLL_FREQ_BASE, maximum=OPLL_MAX_UNIT)
        _OPLL_TABLES[key] = tbl
    return tbl


def opll_to_freq(unit: int) -> tuple[int, int]:
    """DivPlatformOPLL::toFreq: the block/f-num a frequency unit becomes."""
    block = 0
    if unit > 0:
        index = unit // OPLL_C_NUM
        if index > 0:
            block = index.bit_length()
    block = max(0, min(7, block))
    fnum = unit >> block
    if fnum > OPLL_MAX_FNUM:
        fnum = OPLL_MAX_FNUM
    return block, fnum


def opll_unit_for(note: int, clock: float, e5: int = 0x80) -> int:
    return opll_pitch_table(clock).value(note, e5)


def opll_note_fnum(note: int, clock: float, e5: int = 0x80) -> tuple[int, int]:
    """(block, fnum) Furnace writes for a pattern note on this clock."""
    return opll_to_freq(opll_unit_for(note, clock, e5))


def opll_note_for(
    block: int, fnum: int, clock: float,
) -> tuple[int, int]:
    """Source (block, fnum) -> (pattern note, E5 byte), OPLL layout."""
    block = max(0, min(7, int(block)))
    fnum = max(1, min(OPLL_MAX_FNUM, int(fnum)))
    key = (block, fnum, round(float(clock), 4))
    hit = _OPLL_NOTES.get(key)
    if hit is not None:
        return hit
    tbl = opll_pitch_table(clock)
    want = opl_pitch_units(block, fnum)
    unit = max(1, fnum << block)
    est = 57.0 + 12.0 * math.log2(unit * tbl.clock / (tbl.tuning * tbl.base))
    center = max(0, min(179, int(math.floor(est))))
    lo_note = max(0, min(179, center - 3))
    hi_note = max(lo_note, min(179, center + 4))
    best: tuple[float, int, int, int] | None = None
    for note in range(lo_note, hi_note + 1):
        lo, hi = -128, 127
        while lo < hi:
            mid = (lo + hi) // 2
            if opl_pitch_units(*opll_to_freq(tbl.value(note, 0x80 + mid))) < want:
                lo = mid + 1
            else:
                hi = mid
        for off in range(max(-128, lo - 2), min(127, lo + 2) + 1):
            got = opll_to_freq(tbl.value(note, 0x80 + off))
            err = abs(opl_pitch_units(*got) - want)
            cand = (err, abs(off), note, 0x80 + off)
            if best is None or cand < best:
                best = cand
    assert best is not None
    out = (best[2], best[3])
    _OPLL_NOTES[key] = out
    return out


# --- register image ---------------------------------------------------------

def _idle_op() -> OpRegs:
    """An operator the chip never reaches: it stays silent."""
    return OpRegs(tl=0x3F, mult=1)


class OPLState:
    """Register image of one OPL chip, read through Furnace's channel tables.

    ``kind`` picks the platform variant (waveform depth, banks and rhythm
    bits), and ``drums`` switches to the channel tables of Furnace's drums
    system. The rhythm flag follows register 0xBD. The log decides when the
    percussion channels start to mean drums.
    """

    def __init__(self, kind: str = KIND_OPL2, drums: bool = False) -> None:
        self.kind = kind
        self.opl3 = opl3_like(kind)
        self.drums = drums
        self.regs = [0] * 512
        total, melodic = LAYOUT[(kind, drums)]
        self.channels = total
        self.melodic = melodic
        self.slots = slots_for(kind, drums)
        self.chan_map = chan_map_for(kind, drums)

    # -- image ---------------------------------------------------------------

    def write(self, port: int, reg: int, val: int) -> None:
        self.regs[((port & 1) << 8 if self.opl3 else 0) | (reg & 0xFF)] = val & 0xFF

    @property
    def rhythm(self) -> bool:
        """Rhythm mode as the chip has it (bit 5 of 0xBD)."""
        return bool(self.regs[0xBD] & 0x20)

    @property
    def triggers(self) -> int:
        """The latch-on-write key bits of 0xBD."""
        return self.regs[0xBD] & 0x1F

    @property
    def four_op_mask(self) -> int:
        """The six pair bits of the OPL3 "new" register 0x104."""
        return self.regs[0x104] & 0x3F if self.opl3 else 0

    def is_four_op(self, fch: int) -> bool:
        """True when this channel heads a four-operator pair."""
        if not self.opl3 or fch >= 12 or (fch & 1):
            return False
        return bool((self.four_op_mask >> (fch // 2)) & 1)

    def op_count(self, fch: int) -> int:
        return 4 if self.is_four_op(fch) else 2

    # -- channel reads -------------------------------------------------------

    def _block_fnum_raw(self, fch: int) -> tuple[int, int]:
        cm = self.chan_map[fch]
        hi = self.regs[cm + 0xB0]
        return (hi >> 2) & 7, ((hi & 3) << 8) | self.regs[cm + 0xA0]

    def frequency(self, fch: int) -> tuple[int, int]:
        """(block, fnum) the channel's own registers hold.

        In drums mode the four percussion channels share two frequency
        registers, exactly as the chip does: snare and hi-hat follow channel 7
        and tom and cymbal follow channel 8.
        """
        return self._block_fnum_raw(fch)

    def keyed(self, fch: int) -> bool:
        return bool(self.regs[self.chan_map[fch] + 0xB0] & 0x20)

    def algorithm(self, fch: int) -> int:
        """The connection field as the platform stores it.

        A two-operator voice takes one bit; a four-operator pair splits its
        two bits over the two channels' 0xCn registers.
        """
        cm = self.chan_map[fch]
        alg = self.regs[cm + 0xC0] & 1
        if self.is_four_op(fch):
            alg |= (self.regs[self.chan_map[fch + 1] + 0xC0] & 1) << 1
        return alg

    def feedback(self, fch: int) -> int:
        return (self.regs[self.chan_map[fch] + 0xC0] >> 1) & 7

    def pan_bits(self, fch: int) -> int:
        """The hardware L/R enable bits (0 = both off, 3 = both)."""
        if not self.opl3:
            return 3
        return (self.regs[self.chan_map[fch] + 0xC0] >> 4) & 3

    def pan_byte(self, fch: int) -> int:
        """Furnace 08xy for the channel; 0 means "write nothing".

        The OPL3 gate is one bit per side, and a full nibble carries the level
        Furnace scales by.  Both sides on must be written explicitly as 0xFF:
        a channel that was hard-panned earlier otherwise keeps that pan when
        the source returns to centre.  Both sides off cannot be written
        without Furnace's compatibility rule turning 0x00 back into centre,
        so it stays an omitted effect.
        """
        if not self.opl3:
            return 0
        bits = self.pan_bits(fch)
        if bits == 3:
            return 0xFF
        if not bits:
            return 0
        out = 0
        if bits & 1:
            out |= 0xF0
        if bits & 2:
            out |= 0x0F
        return out

    def operator(self, addr: int) -> OpRegs:
        """One operator decoded from its five registers at ``addr``."""
        regs = self.regs
        amvib = regs[addr + 0x20]
        tl = regs[addr + 0x40]
        ardr = regs[addr + 0x60]
        slrr = regs[addr + 0x80]
        ws = regs[addr + 0xE0] if self.kind != KIND_OPL else 0
        return OpRegs(
            tl=tl & 0x3F,
            ksl=(tl >> 6) & 3,
            ar=(ardr >> 4) & 15,
            dr=ardr & 15,
            sl=(slrr >> 4) & 15,
            rr=slrr & 15,
            am=(amvib >> 7) & 1,
            vib=(amvib >> 6) & 1,
            sus=(amvib >> 5) & 1,
            ksr=(amvib >> 4) & 1,
            mult=amvib & 15,
            ws=ws & (7 if self.opl3 else 3),
        )

    def _op_addr(self, fch: int, row: int) -> int | None:
        slot = self.slots[row][fch]
        return None if slot == N else SLOT_ADDR[slot]

    def snapshot(self, fch: int) -> FMPatch:
        """The voice a channel holds right now, in Furnace's operator order."""
        four = self.is_four_op(fch)
        ops: list[OpRegs] = [_idle_op() for _ in range(4)]
        for row in range(4 if four else 2):
            addr = self._op_addr(fch, row)
            if addr is None:
                continue
            ops[ORDERED_OPS_L[row] if four else row] = self.operator(addr)
        return FMPatch(
            alg=self.algorithm(fch),
            fb=self.feedback(fch),
            ams=0,
            pms=0,
            ops=tuple(ops),  # type: ignore[arg-type]
            opl=True,
            four=four,
        )

    def drum_snapshot(self) -> FMPatch:
        """The four percussion operators as one DIV_INS_OPL_DRUMS instrument.

        Row 0 of channels ``melodic+1`` to ``melodic+4`` is the snare, tom,
        cymbal and hi-hat operator, which is the order commitState copies them
        from an instrument's operators.
        """
        ops = []
        for row in range(1, 5):
            addr = self._op_addr(self.melodic + row, 0)
            ops.append(self.operator(addr) if addr is not None else _idle_op())
        while len(ops) < 4:
            ops.append(_idle_op())
        return FMPatch(
            alg=0, fb=0, ams=0, pms=0,
            ops=tuple(ops[:4]),  # type: ignore[arg-type]
            opl=True, drum=True,
        )

    def carrier_tl(self, fch: int) -> int:
        return self.snapshot(fch).carrier_tl()

    def channel_tl(self, fch: int) -> int:
        """Level a volume-column step has to reproduce on this channel.

        The platform scales only the operators that reach the output. A
        percussion column is one operator of its own and tracks that operator.
        """
        if self.drums and fch > self.melodic:
            addr = self._op_addr(fch, 0)
            return 63 if addr is None else self.operator(addr).tl
        return self.carrier_tl(fch)
