"""Measure the musical row grid from key-on timing, or return None.

Drivers schedule notes on a 16th note whose length is not a whole number of
60 Hz frames. The measured unit is subdivided into rows and ticks, and EDxx
places every event within half a tick. The estimator is conservative, and
the caller falls back to the legacy 60 Hz heuristic when no lattice fits.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from vgm2151fur.vgm import VGM_RATE, VgmFile

# Target length of one tick: fine enough for frame-quantized note starts,
# coarse enough that ED values stay small.
TARGET_TICK_SAMPLES = 90.0

# Shortest row the grid will subdivide the measured 16th into. Shorter rows
# drive the row rate (and the tracker's BPM) up; longer rows trade
# slide-rate granularity for readability. See `estimate_grid(min_row=...)`.
MIN_ROW_SAMPLES = 280.0


@dataclass
class Grid:
    row: float  # samples per row (unit / subdiv)
    hz: float  # ticks per second (Furnace song hz)
    speed: int  # ticks per row (Furnace song speed)
    confidence: float
    onsets: int
    source: str
    unit: float = 0.0  # samples per 16th note (the measured unit)
    subdiv: int = 1  # rows per 16th note
    # The note value before the order-budget floor. A long rip with no single
    # tempo would otherwise report song_length/1920 as its 16th. SCC/AY rows
    # are measured from this pulse; `unit` still carries the floor for FM.
    pulse: float = 0.0

    @property
    def tick(self) -> float:
        """Samples per tick."""
        return self.row / max(1, self.speed)

    @property
    def bpm(self) -> float:
        """Quarter-note tempo implied by the measured 16th note."""
        return (VGM_RATE / self.unit) * 15.0 if self.unit else 0.0

    @property
    def row_note(self) -> str:
        names = {1: "16th", 2: "32nd", 4: "64th", 8: "128th"}
        return names.get(self.subdiv, f"1/{self.subdiv} of a 16th")


def _keyons_by_channel(vgm: VgmFile) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for w in vgm.writes:
        if w.chip == "ym2151" and w.reg == 0x08 and (w.val & 0x78):
            out.setdefault(w.val & 7, []).append(w.sample)
        elif w.chip == "ym2203" and w.reg == 0x28 and (w.val & 0xF0):
            # Per-operator key masks; any 0->1 start is an onset. Keep chip
            # instances apart without colliding with the YM2151's 0-7.
            out.setdefault(1000 + w.chip_id * 3 + (w.val & 3), []).append(w.sample)
        elif (
            w.chip == "ym2612" and not (w.reg & 0x100)
            and (w.reg & 0xFF) == 0x28 and (w.val & 0xF0)
        ):
            # Same rule for the YM2612's six channels.  Key-on encodes the
            # second group as 4-6 after the reserved 3.
            raw = w.val & 0x07
            if raw in (3, 7):
                continue
            ch = raw if raw < 3 else raw - 1
            out.setdefault(2000 + w.chip_id * 6 + ch, []).append(w.sample)
        elif (
            w.chip == "ym2610" and not (w.reg & 0x100)
            and (w.reg & 0xFF) == 0x28 and (w.val & 0xF0)
        ):
            # YM2610 uses 2+2 FM channels (B: 3+3); the 0x28 channel field
            # follows the same reserved-slot encoding.
            raw = w.val & 0x07
            if getattr(vgm, "ym2610_b", False):
                ch = {0: 0, 1: 1, 2: 2, 4: 3, 5: 4, 6: 5}.get(raw)
            else:
                ch = {1: 0, 2: 1, 5: 2, 6: 3}.get(raw)
            if ch is None:
                continue
            out.setdefault(3000 + w.chip_id * 6 + ch, []).append(w.sample)
    for v in out.values():
        v.sort()
    return out


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2]


def _bucketed_mode(vals: list[int], bucket: int = 4) -> float:
    """Most common delta, bucketed so jitter does not split the peak."""
    counts = Counter(int(round(v / bucket)) * bucket for v in vals)
    return float(counts.most_common(1)[0][0])


def _refine_unit(deltas: list[int], guess: float) -> float:
    """Least-squares refinement of the note unit around a candidate.

    Only intervals at least ~0.6x the guess take part. Ornament notes on a
    finer grid must not drag the fit away from the candidate's lattice.
    """
    usable = [d for d in deltas if d >= 0.6 * guess]
    if len(usable) < 8:
        usable = deltas
    # Repeated intervals dominate the sum. Folding them into weights keeps
    # the 600-point search affordable on dense percussive tracks.
    weights = tuple(Counter(usable).items())

    def cost(t: float) -> float:
        total = 0.0
        for d, n in weights:
            k = round(d / t)
            if k < 1:
                k = 1
            r = d - k * t
            total += n * r * r / k  # weight long deltas a little less
        return total

    lo, hi = guess * 0.9, guess * 1.1
    best_t = guess
    best_c = cost(guess)
    for i in range(400 + 1):
        t = lo + (hi - lo) * i / 400
        c = cost(t)
        if c < best_c:
            best_c, best_t = c, t
    lo2, hi2 = best_t * 0.995, best_t * 1.005
    for i in range(200 + 1):
        t = lo2 + (hi2 - lo2) * i / 200
        c = cost(t)
        if c < best_c:
            best_c, best_t = c, t
    return best_t


def _lattice_coverage(deltas: list[int], t: float) -> float:
    """Fraction of intervals that are near-integer multiples of the unit."""
    good = 0
    for d in deltas:
        k = max(1, round(d / t))
        if abs(d - k * t) <= 0.06 * k * t:
            good += 1
    return good / len(deltas) if deltas else 0.0


def hz_speed_for_row(row: float, tick_samples: float) -> tuple[float, int]:
    """Furnace hz/speed for a row of `row` samples at ~`tick_samples` per tick.

    EDxx must stay below the row's tick count (strict delay policy), and the
    tick rate has to stay a sane "song hz" for the tracker UI.
    """
    speed = max(1, min(255, int(round(row / tick_samples))))
    while speed > 1 and row / speed < 40.0:
        speed //= 2
    return VGM_RATE * speed / row, speed


def place_tick(
    sample: int,
    t0: int,
    row_samples: float,
    speed: int,
    tick_samples: float = TARGET_TICK_SAMPLES,
    *,
    floor: bool = False,
    stats: dict | None = None,
) -> tuple[int, int]:
    """Map a source sample to (row, ED delay in ticks).

    `floor=False` rounds to the nearest tick. `floor=True` uses the preceding
    tick. A caller can prepend the held byte and keep the exact source time.
    """
    if speed <= 1:
        row = max(0, int(math.floor(
            (sample - t0) / row_samples + (0.0 if floor else 0.5)
        )))
        return row, 0
    row = max(0, int((sample - t0) // row_samples))
    target = t0 + row * row_samples
    offset = (sample - target) / tick_samples
    d = int(math.floor(offset)) if floor else int(round(offset))
    if d >= speed:
        row += 1
        d = 0
        if stats is not None:
            stats["ed_rolled"] = stats.get("ed_rolled", 0) + 1
    return row, max(0, d)


def condense_row(
    hz: float, speed: int, subdiv: float, factor: float, tick_samples: float,
) -> tuple[float, int, float]:
    """Lengthen every row by `factor` (rows/s and so BPM drop by `factor`).

    `subdiv` (rows per measured 16th) is cosmetic - it only feeds Song.bpm - so
    a non-integer result (3x) is fine.  Returns the new (hz, speed, subdiv).
    """
    if factor <= 1:
        return hz, speed, subdiv
    row = (VGM_RATE / hz) * speed * factor
    nhz, nspeed = hz_speed_for_row(row, tick_samples)
    return nhz, nspeed, subdiv / factor


def estimate_grid(
    vgm: VgmFile, *, min_confidence: float = 0.75,
    tick_samples: float = TARGET_TICK_SAMPLES,
    min_row: float = MIN_ROW_SAMPLES,
    onsets: dict[int, list[int]] | None = None,
    label: str = "key-on lattice",
) -> Grid | None:
    """Return the measured grid, a subdivided 16th note, or None.

    Candidate units are scored by how many intervals they explain as integer
    multiples, and the coarsest near-best unit wins. `onsets` overrides the
    note times, keyed by channel.
    """
    by_ch = _keyons_by_channel(vgm) if onsets is None else onsets
    deltas: list[int] = []
    for ch, ons in by_ch.items():
        for a, b in zip(ons, ons[1:]):
            d = b - a
            if 0 < d <= 20000:
                deltas.append(d)
    if len(deltas) < 24:
        return None

    candidates: set[float] = set()
    for ons in by_ch.values():
        ds = [b - a for a, b in zip(ons, ons[1:]) if 0 < b - a <= 20000]
        if len(ds) < 8:
            continue
        candidates.add(_bucketed_mode(ds))
        candidates.add(_median([float(d) for d in ds]))
    candidates.add(_bucketed_mode(deltas))
    candidates = {c for c in candidates if c > 0}

    scored: list[tuple[float, float]] = []  # (unit, coverage)
    folded_scored: list[tuple[float, float]] = []
    for c in candidates:
        t = _refine_unit(deltas, c)
        if 800.0 <= t <= 8000.0:
            scored.append((t, _lattice_coverage(deltas, t)))
            continue
        if t < 800.0:
            continue
        # Out of band, too coarse.  A slow track's *fastest* note value can
        # still be longer than a 16th note: some drivers run 9.5k-19k samples
        # per melodic event.  Fold by halves back into the band so the rows
        # can subdivide it (a 15 rows/s fallback cannot carry the driver's
        # per-frame pitch writes).  Only used when no in-band candidate
        # explains the stream, and only when the folded unit really fits the
        # intervals, or noise would become a grid.
        folded = t
        while folded > 8000.0:
            folded /= 2.0
        recovered: list[tuple[float, float]] = []
        if folded >= 800.0:
            cov = _lattice_coverage(deltas, folded)
            if cov >= 0.8:
                recovered.append((folded, cov))
        if not recovered:
            # Half-folding fails when the mode/median sits on a *multiple* of
            # the row unit - a melody that moves in 8ths, quarters or whole
            # bars most of the time - so the unit is c/2, c/3, ... rather than
            # c/2^n.  Divide the raw candidate back down and demand a tight
            # fit *and* that the unit is not far finer than the shortest gap
            # seen: a divided noise unit fits the loose 0.8 bar (the
            # estimator's pseudorandom fixture did) but describes a grid no
            # note ever lands on.
            shortest = min(deltas)
            for div in (2, 3, 4, 5, 6, 7, 8):
                sub = c / div
                if sub < 800.0:
                    continue
                ts = _refine_unit(deltas, sub)
                if not (800.0 <= ts <= 8000.0):
                    continue
                if shortest > 2.0 * ts:
                    continue
                cov = _lattice_coverage(deltas, ts)
                if cov >= 0.9:
                    recovered.append((ts, cov))
        if recovered:
            folded_scored.append(max(recovered))
    if not scored:
        scored = folded_scored
    if not scored:
        return None
    low_note = ""
    ok = [(t, cov) for t, cov in scored if cov >= 0.8]
    if not ok:
        # Tracks that change tempo between sections have no single unit that
        # explains the whole song.  Use the smallest real candidate so the row
        # grid stays fine enough to place the fastest section without
        # collisions; EDxx still puts every event at its true write time.
        smalls = sorted(c for c in candidates if c >= 800.0)
        if not smalls:
            return None
        # `pulse` stays the fastest real note value. The floor below only
        # lengthens the FM row so a long rip still fits in about 240 orders
        # of 64-row patterns. Coverage is scored on the floored unit, which
        # is why a medley can report 0% here.
        pulse = smalls[0]
        unit = pulse
        if vgm.total_samples:
            unit = max(unit, vgm.total_samples / 1920.0)  # <= ~240 orders
        best_conf = _lattice_coverage(deltas, unit)
        low_note = ", sections change unit"
    else:
        # Prefer the coarsest unit among those with near-best coverage.
        best_cov = max(cov for _t, cov in ok)
        near = [(t, cov) for t, cov in ok if cov >= best_cov - 0.15]
        unit, best_conf = max(near)
        pulse = unit
    subdiv = 1
    for n in (8, 4, 2):
        if unit / n >= min_row:
            subdiv = n
            break
    row = unit / subdiv
    hz, speed = hz_speed_for_row(row, tick_samples)

    note = f"{label} ({len(deltas)} intervals)"
    if best_conf < min_confidence:
        note += ", low confidence"
    note += low_note
    return Grid(
        row=row,
        hz=hz,
        speed=speed,
        confidence=best_conf,
        onsets=sum(len(v) for v in by_ch.values()),
        source=note,
        unit=unit,
        subdiv=subdiv,
        pulse=pulse,
    )


# Writer uses row_of(last) + 2 and caps at 256 orders of 64 rows.
_ORDER_ROWS = 256 * 64 - 4


def refine_pcm_grid(
    hz: float,
    speed: int,
    subdiv: int,
    extent: int,
    gaps: list[int],
    tick_samples: float = TARGET_TICK_SAMPLES,
) -> tuple[float, int, int, str]:
    """Shrink the row until each playable same-voice restart has its own row.

    `gaps` are distances between restarts on one voice. A gap below the
    finest row that fits in 256 orders cannot have its own row. Returns the
    input grid unchanged when every gap already fits.
    """
    if extent <= 0 or speed <= 0 or hz <= 0 or not gaps:
        return hz, speed, subdiv, ""
    row = (VGM_RATE / hz) * speed
    finest = max(1.0, extent / _ORDER_ROWS)
    honor = [g for g in gaps if g >= finest]
    if not honor:
        return hz, speed, subdiv, ""
    need = float(min(honor))
    if need >= row - 0.5:
        return hz, speed, subdiv, ""
    target = max(finest, need - 1.0)
    new_speed = max(1, min(255, int(round(target / tick_samples))))
    while new_speed > 1 and target / new_speed < 40.0:
        new_speed //= 2
    new_hz = VGM_RATE * new_speed / target
    new_sub = max(1, int(round(subdiv * row / target))) if row > 0 else subdiv
    note = (
        f"PCM restarts down to {need:.0f} samples, "
        f"row {target:.0f} samples so each has its own row"
    )
    return new_hz, new_speed, new_sub, note
