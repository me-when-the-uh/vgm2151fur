"""Furnace's period-mode pitch table, ported, plus its inverse.

Furnace turns a note plus the ``E5xx`` fine pitch into a chip register with
``DivPitchTable`` (``pitchTable.cpp``): for period chips the table holds
``round((clock / f) / divider)`` for one octave of notes, a note above it is
that value shifted right by whole octaves, and the adjacent-note difference
supplies the fine pitch.  Platforms whose register is "period - 1" (the SCC)
subtract one before writing.

Notes use Furnace's own numbering (0 = C-(-5), 108 = C-4); in period mode a
note stands for the frequency ``(tuning / 16) * 2 ** ((note - 57) / 12)``,
which is the convention the AY and SCC platforms share.

``furnace_period_note`` inverts the table, so a VGM register value can be
written back as the note/E5 pair that reproduces it exactly (up to the table's
own resolution) instead of through a closed form that ignores the integer
shifts.
"""

from __future__ import annotations

import math

FURNACE_TUNING = 440.0
NOTE_MAX = 179
E5_CENTER = 0x80
# Furnace's default clock for the AY-8910 and SCC systems: COLOR_NTSC / 2.
# The engine builds its pitch table from the double value, so writing
# ``customClock`` for a rip whose clock is within a hertz of this would change
# the table by a unit here and there for no audible reason.
FURNACE_CLOCK_DEFAULT = 315_000_000.0 / 88.0 / 2.0
CLOCK_DEFAULT_TOLERANCE = 1  # Hz


def module_clock(clock: float) -> float:
    """The clock the module will run at: Furnace's default when the rip's own
    clock is within a hertz of it, otherwise the rip's clock (which the writer
    then stores as ``customClock``)."""
    if not clock or abs(clock - FURNACE_CLOCK_DEFAULT) <= CLOCK_DEFAULT_TOLERANCE:
        return FURNACE_CLOCK_DEFAULT
    return float(clock)


class PeriodPitchTable:
    """DivPitchTable in period mode, for one (tuning, clock, divider)."""

    def __init__(self, tuning: float, clock: float, divider: float, maximum: int = 0xFFF):
        self.tuning = tuning
        self.clock = clock
        self.divider = divider
        self.max = maximum
        self.shift = 0
        while self.shift < 14:
            nbase = (self.shift - 4) * 12
            fbase = (tuning * 0.0625) * 2.0 ** ((nbase + 3) / 12.0)
            if round((clock / fbase) / divider) <= maximum:
                break
            self.shift += 1
        self.pitch = [0] * 13
        self.diff = [0] * 13
        for i in range(13):
            nbase = i + (self.shift - 5) * 12
            fbase = (tuning * 0.0625) * 2.0 ** ((nbase + 3) / 12.0)
            self.pitch[i] = round((clock / fbase) / divider)
        for i in range(12):
            self.diff[i] = self.pitch[i + 1] - self.pitch[i]
        self._offsets: dict[tuple[int, int], int] = {}

    @staticmethod
    def _shift_pair(root: int, diff: int, octave: int) -> tuple[int, int]:
        """Furnace shifts both values right, rounding the carry in."""
        if octave > 0:
            carry = bool(root & (1 << (octave - 1)))
            root >>= octave
            if carry:
                root += 1
            carry = bool(diff & (1 << (octave - 1)))
            diff >>= octave
            if carry:
                diff += 1
        return root, diff

    def value(self, note: int, fine: int = 0) -> int:
        """Register value Furnace computes for a note and fine pitch.

        ``fine`` is the engine's ``pitch`` value, i.e. ``E5 - 0x80``: a signed
        offset in 1/128 semitone, so ``fine=0`` is neutral and the pair
        ``(note, fine)`` can carry up to a semitone above or below the note.
        """
        return self._value_offset((note << 7) + fine)

    def note_fraction(self, value: int) -> float:
        """Note plus fraction a register value stands for, as a float note."""
        if value <= 0:
            return float(NOTE_MAX)
        freq = self.clock / (self.divider * value)
        return 57.0 + 12.0 * math.log2(freq / (self.tuning / 16.0))

    def offset_for(self, note: int, target: int) -> int:
        """Pitch offset (the engine's ``pitch`` units, 1/128 semitone) whose
        register is closest to `target`.

        This is what a pitch macro or an ``E5`` writes: the offset rides on top
        of the note, so a value beyond one semitone simply walks into the next
        note's entry of the table.
        """
        target = max(1, target)
        key = (note, target)
        hit = self._offsets.get(key)
        if hit is not None:
            return hit
        # A semitone of offset moves the register by the local table
        # difference, so a log2 estimate lands within a couple of hundred units
        # and the value falls monotonically from there.
        here = self.value(note, 0)
        span = 128.0 * 12.0 * math.log2(here / target) if here > 0 else 0.0
        est = max(-24000, min(24000, (note << 7) + int(round(span))))
        lo = max(-32768, est - 400)
        hi = min(32768, est + 400)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._value_offset(mid) > target:
                lo = mid + 1
            else:
                hi = mid
        best: tuple[int, int, int] | None = None  # (err, |off|, off)
        for off in range(max(-32768, lo - 4), min(32768, lo + 4) + 1):
            err = abs(self._value_offset(off) - target)
            cand = (err, abs(off), off)
            if best is None or cand < best:
                best = cand
        assert best is not None
        self._offsets[key] = best[2]
        return best[2]

    def _value_offset(self, offset: int) -> int:
        """`value` for a raw engine offset (note and pitch in one number)."""
        coarse = offset >> 7
        if coarse < 0:
            coarse = 0
        frac = offset & 127
        index = coarse % 12
        octave = (coarse // 12) - self.shift
        if octave < 0:
            return self.max
        root, diff = self._shift_pair(self.pitch[index], self.diff[index], octave)
        return root + ((diff * frac) >> 7)


_TABLES: dict[tuple, PeriodPitchTable] = {}
_NOTES: dict[tuple, tuple[int, int]] = {}


def pitch_table(clock: float, divider: float, maximum: int = 0xFFF) -> PeriodPitchTable:
    key = (round(clock), divider, maximum)
    tbl = _TABLES.get(key)
    if tbl is None:
        tbl = PeriodPitchTable(FURNACE_TUNING, clock, divider, maximum)
        _TABLES[key] = tbl
    return tbl


def _closest_fine(tbl: PeriodPitchTable, note: int, target: int) -> tuple[int, int]:
    """(fine, |value - target|) closest to target, by binary search + neighbours.

    ``fine`` is the signed E5-0x80 offset. The table's value falls as the fine
    pitch rises (shorter period), and the octave rounding can flatten the
    difference to zero for high notes; that case has no better fine than the
    center.
    """
    first = tbl.value(note, -128)
    last = tbl.value(note, 127)
    if first == last:
        return 0, abs(first - target)
    descending = last < first
    lo, hi = -128, 127
    while lo < hi:
        mid = (lo + hi) // 2
        if (tbl.value(note, mid) > target) == descending:
            lo = mid + 1
        else:
            hi = mid
    best: tuple[int, int] | None = None
    for fine in range(max(-128, lo - 3), min(127, lo + 3) + 1):
        err = abs(tbl.value(note, fine) - target)
        if best is None or err < best[1] or (err == best[1] and abs(fine) < abs(best[0])):
            best = (fine, err)
    assert best is not None
    return best


def furnace_period_note(
    clock: float,
    divider: float,
    value: int,
    *,
    period_offset: int = 0,
    maximum: int = 0xFFF,
) -> tuple[int, int]:
    """Inverse of the pitch table: register value -> (Furnace note, E5).

    ``period_offset`` is what the platform subtracts before writing (1 for the
    SCC, 0 for the AY), so the table must produce ``value + period_offset``.
    The returned pair is the one whose register is closest to the wanted
    value; an exact match wins, then the smaller fine pitch, then the note
    whose frequency is closest. The second element is the E5 effect byte
    (0x80 = no fine tune), which the engine turns back into ``E5 - 0x80``.
    """
    cache_key = (round(clock), divider, maximum, value, period_offset)
    hit = _NOTES.get(cache_key)
    if hit is not None:
        return hit
    tbl = pitch_table(clock, divider, maximum)
    target = value + period_offset
    base = tbl.note_fraction(target)
    best: tuple[int, int, int, int] | None = None  # (err, |fine|, note, fine)
    center = int(math.floor(base))
    lo = max(0, center - 2)
    hi = min(NOTE_MAX, center + 2)
    if lo > hi:
        # Out of the note range: the closest note the format can hold, with the
        # fine pitch picked to get as near as possible.
        lo = hi = max(0, min(NOTE_MAX, center))
    for note in range(lo, hi + 1):
        fine, err = _closest_fine(tbl, note, target)
        cand = (err, abs(fine), note, fine)
        if best is None or cand < best:
            best = cand
    assert best is not None
    result = (best[2], E5_CENTER + best[3])
    _NOTES[cache_key] = result
    return result
