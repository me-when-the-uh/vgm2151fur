"""Move a VGM loop marker onto the downbeat of the bar.

Rippers drop the loop sample wherever the phrase happened to be when they
hit the key. The note grid usually repeats every 4, 8, 12, or 16 pulses
(a 4/4 bar, an 8/8 bar, or the 12-pulse cell some of these themes use).
The marker should open that cell, not sit on the second or third pulse.

Furnace runs a row's notes and sample commands when it lands on the row,
and a `Bxx` on that same row jumps away at the end of it. The order cut
therefore opens a few rows before the downbeat commands (three): voices
keyed just before the main chord are inside the loop, and the downbeat
itself is not the jump row.

A grid with no clock, or a loop that is not a whole number of bars, stays
on the VGM marker. A clock with no repeating cell stays too, except when
the marker sits on the empty row after a chord: the cut moves to the rows
before that chord.

The marker is also often off by a whole phrase, not just a pulse: it can
sit inside the phrase, seconds after its first command. The loop keeps the
length the VGM gave it — a marker late at the start matches the end of the
log being late too — so the seam search looks for the phrase start whose
copy sits one loop length later, still inside the log. A copy closer than
that is an inner repetition, not the seam the marker is missing.
Candidates run a window on either side of the marker, and the earliest
long run of near-perfect matches wins, so a marker inside the phrase still
opens the loop on the phrase's first command without dropping the head.
The order opens a few rows before those commands, and the jump stops the
same distance before the returned copy.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

NOTE_OFF = 180
PERIODS = (4, 8, 12, 16)
MIN_REGULARITY = 0.75
# One late note must not hand the clock to a sparser, perfectly even voice.
REG_TOLERANCE = 0.05
# Weighted pitch-class agreement through the loop body. Exact slice equality
# stays near zero on a real bar whose voices are a row apart; 0.25 still
# leaves a through-composed cue (no repeating cell) on the VGM marker.
MIN_AUTOCORR = 0.25
MIN_AUTOCORR_GAP = 0.04
PERIOD_BAND = 0.02
MIN_VOTE_MARGIN = 0.08
# The winning phase has to be heavier than the phase the marker is on.
# A few hundredths is two phases that both carry the band.
MIN_PHASE_GAIN = 0.75
# The loop beat is an arrival when it brings in this many more voices than
# the candidate behind it. That is a pickup resolving, not a late marker.
TEXTURE_JUMP = 2
# The order cut opens this many rows before the first commands. One row is
# the row whose copy the seam search actually verified. Wider cuts (three
# rows were tried) land the seam on rows that are not exact copies of their
# log counterparts - the PCM and effect columns drift a row or two between
# the first and last iteration - and the loop audibly wobbles at the seam.
CUT_ROWS = 1


@dataclass(frozen=True)
class LoopFix:
    """A replacement loop, in pattern rows.

    `start_row` is the first row of the loop order. `end_row` is the row
    `Bxx` sits on (the last row that plays). `shift_pulses` is the musical
    move; the order cut additionally opens a few rows before the downbeat
    commands when the clock is wider than one row.
    """

    start_row: int
    end_row: int
    period: int
    shift_pulses: int
    step_rows: int


@dataclass(frozen=True)
class _Clock:
    step: int
    phase: int
    regularity: float
    channel: int


def _clock(notes: list[tuple[int, int, int]]) -> _Clock | None:
    by: dict[int, list[int]] = defaultdict(list)
    for row, ch, _pitch in notes:
        by[ch].append(row)
    best = None
    for ch, rows in by.items():
        ordered = sorted(set(rows))
        gaps = [b - a for a, b in zip(ordered, ordered[1:]) if 3 <= b - a <= 96]
        if len(gaps) < 8:
            continue
        top = Counter(gaps).most_common(1)[0][0]
        cluster = [gap for gap in gaps if abs(gap - top) <= 1]
        regularity = len(cluster) / len(gaps)
        step = max(1, int(round(sum(cluster) / len(cluster))))
        rec = (regularity, len(cluster), step, ch, ordered)
        if best is None or _better_clock(rec, best):
            best = rec
    if best is None or best[0] < MIN_REGULARITY:
        return None
    regularity, _count, step, ch, ordered = best
    phase, hits = 0, -1
    for candidate in range(step):
        count = sum(
            1 for row in ordered
            if min((row - candidate) % step, step - ((row - candidate) % step)) <= 1
        )
        if count > hits:
            hits, phase = count, candidate
    return _Clock(step=step, phase=phase, regularity=regularity, channel=ch)


def _better_clock(rec, best) -> bool:
    """Prefer the even voice. A regularity gap inside the tolerance loses to
    the voice with more on-grid gaps, so the pulse is the dense one."""
    reg, count = rec[0], rec[1]
    best_reg, best_count = best[0], best[1]
    if reg > best_reg + REG_TOLERANCE:
        return True
    return abs(reg - best_reg) <= REG_TOLERANCE and count > best_count


def _grid(notes: list[tuple[int, int, int]], clock: _Clock):
    """beat -> channel -> (pitch, row). Nearest pulse, within half a step."""
    grid: dict[int, dict[int, tuple[int, int]]] = defaultdict(dict)
    limit = clock.step / 2
    for row, ch, pitch in notes:
        rel = row - clock.phase
        beat = int(round(rel / clock.step))
        err = abs(rel - beat * clock.step)
        if err > limit:
            continue
        prev = grid[beat].get(ch)
        if prev is None or err < abs((prev[1] - clock.phase) - beat * clock.step):
            grid[beat][ch] = (pitch, row)
    return grid


def _echoes(grid: dict[int, dict[int, tuple[int, int]]], clock_ch: int) -> set[int]:
    """Channels that repeat another channel's pitch class one pulse later."""
    channels = sorted({ch for beat in grid.values() for ch in beat})
    echoes: set[int] = set()
    for src in channels:
        for dst in channels:
            if src == dst or dst == clock_ch or dst in echoes:
                continue
            total = hits = 0
            for beat, voices in grid.items():
                if src not in voices:
                    continue
                total += 1
                later = grid.get(beat + 1, {})
                if dst in later and later[dst][0] % 12 == voices[src][0] % 12:
                    hits += 1
            if total >= 12 and hits >= 8 and hits / total >= 0.55:
                echoes.add(dst)
    return echoes


def _roles(grid, echoes: set[int]) -> tuple[int | None, int | None]:
    stats = []
    for ch in {c for beat in grid.values() for c in beat}:
        if ch in echoes:
            continue
        pitches = [voices[ch][0] for beat, voices in grid.items() if ch in voices]
        if len(pitches) < 8:
            continue
        ordered = sorted(pitches)
        median = ordered[len(ordered) // 2]
        variety = len({pitch % 12 for pitch in pitches})
        stats.append((ch, median, variety, len(pitches)))
    if not stats:
        return None, None
    bass = min(stats, key=lambda item: (item[1], -item[3]))[0]
    leads = [item for item in stats if item[0] != bass]
    if not leads:
        return bass, None
    lead = max(leads, key=lambda item: (item[2], item[1], item[3]))[0]
    return bass, lead


def _beat_of(row: int, clock: _Clock) -> int:
    return int(round((row - clock.phase) / clock.step))


def _slice(grid, beat: int, echoes: set[int]) -> tuple:
    voices = grid.get(beat, {})
    return tuple(sorted(
        (ch, pitch % 12) for ch, (pitch, _row) in voices.items() if ch not in echoes
    ))


def _variety(grid) -> dict[int, int]:
    """Pitch classes each channel uses. A pedal weighs less than a melody."""
    seen: dict[int, set[int]] = defaultdict(set)
    for voices in grid.values():
        for ch, (pitch, _row) in voices.items():
            seen[ch].add(pitch % 12)
    return {ch: len(pcs) for ch, pcs in seen.items()}


def _agreement(left: dict[int, int], right: dict[int, int], weight: dict[int, int]) -> float | None:
    """Share of pitch classes that match, weighted by how much each voice moves.

    Empty against empty is skipped. A voice on only one side counts as a miss.
    """
    if not left and not right:
        return None
    keys = set(left) | set(right)
    if not keys:
        return None
    hit = total = 0.0
    for ch in keys:
        w = float(weight.get(ch, 1))
        total += w
        if ch in left and ch in right and left[ch] == right[ch]:
            hit += w
    return hit / total if total else None


def _period(grid, echoes, loop_beat: int, end_beat: int) -> tuple[int, float] | None:
    """The repeating cell length, in pulses, and its agreement."""
    weight = _variety(grid)
    span = end_beat - loop_beat
    scored: list[tuple[float, int]] = []
    for period in PERIODS:
        if span < period * 2:
            continue
        total = 0
        acc = 0.0
        for beat in range(loop_beat, end_beat - period):
            part = _agreement(
                dict(_slice(grid, beat, echoes)),
                dict(_slice(grid, beat + period, echoes)),
                weight,
            )
            if part is None:
                continue
            total += 1
            acc += part
        if total < 16:
            continue
        scored.append((acc / total, period))
    if not scored:
        return None
    # A tie keeps the shorter cell. Two identical bars are an 8, not a 16.
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    best_ac, best_period = scored[0]
    if best_ac < MIN_AUTOCORR:
        return None
    for ac, period in sorted(scored, key=lambda item: item[1]):
        if period <= best_period or ac < best_ac - PERIOD_BAND:
            continue
        # A multiple of a cell that already matches is the same cell again.
        if any(
            period % other == 0 and other_ac >= ac - PERIOD_BAND
            for other_ac, other in scored
            if 0 < other < period
        ):
            continue
        best_ac, best_period = ac, period
    rivals = [
        ac for ac, period in scored
        if period != best_period
        and period % best_period != 0
        and best_period % period != 0
    ]
    if rivals and best_ac - max(rivals) < MIN_AUTOCORR_GAP:
        return None
    return best_period, best_ac


def _whole_bars(length_rows: int, step: int, period: int) -> bool:
    """True when the loop length is within one pulse of a whole cell count."""
    pulses = length_rows / step
    bars = pulses / period
    return abs(bars - round(bars)) * period <= 1.0 + 1e-6


def _ensemble(grid, beat: int, echoes: set[int]) -> int:
    return sum(1 for ch in grid.get(beat, {}) if ch not in echoes)


def _phase_shift(
    grid, echoes, lead, loop_beat, end_beat, period: int,
) -> tuple[int, float, float, dict[int, float]]:
    """Pulses to add so the loop beat lands on the voted downbeat.

    The second value is the lead over the runner-up. The third is how far
    the winner sits above the phase the marker is already on. The dict is
    every phase's score.
    """
    scores = []
    for phase in range(period):
        ensemble = entries = count = 0
        for beat in range(loop_beat, end_beat + 1):
            if beat % period != phase:
                continue
            count += 1
            ensemble += _ensemble(grid, beat, echoes)
            if lead is not None and lead in grid.get(beat, {}) and lead not in grid.get(beat - 1, {}):
                entries += 1
        if count:
            scores.append((ensemble / count + 2.5 * (entries / count), phase))
    scores.sort(reverse=True)
    if not scores:
        return 0, 0.0, 0.0, {}
    by_phase = {phase: score for score, phase in scores}
    current = loop_beat % period
    current_score = by_phase.get(current, 0.0)
    margin = scores[0][0] - scores[1][0] if len(scores) > 1 else scores[0][0]
    delta = (scores[0][1] - current) % period
    if delta > period / 2:
        delta -= period
    return int(delta), margin, scores[0][0] - current_score, by_phase


def _entry_shift(grid, lead, loop_beat: int, period: int) -> int | None:
    """Pulses back to the lead's entry, when that entry is inside this cell.

    The entry is a lead note with the lead silent on the pulse before it.
    The nearest such note at or before the marker, and no more than half a
    cell back, is the start of the phrase the marker fell into.
    """
    if lead is None:
        return None
    found = None
    for beat in range(loop_beat - period // 2, loop_beat + 1):
        if lead not in grid.get(beat, {}):
            continue
        if lead in grid.get(beat - 1, {}):
            continue
        found = beat
    if found is None or found == loop_beat:
        return None
    return found - loop_beat


def _command_row(grid, beat: int, echoes: set[int]) -> int | None:
    """The row most of the beat's own notes sit on.

    A delayed double is ignored. A tie goes to the earlier row, which is
    where a sample keyed a row early already landed.
    """
    voices = grid.get(beat, {})
    rows = [row for ch, (_pitch, row) in voices.items() if ch not in echoes]
    if not rows:
        rows = [row for _pitch, row in voices.values()]
    if not rows:
        return None
    counts = Counter(rows)
    best = max(counts.values())
    return min(row for row, count in counts.items() if count == best)


def _snap_empty_marker(notes, loop_row: int, end_row: int, step: int) -> LoopFix | None:
    """The marker fell on the empty row after a chord. Land before that chord."""
    at_marker = sum(1 for row, _ch, _pitch in notes if row == loop_row)
    if at_marker:
        return None
    chord = loop_row - 1
    hits = sum(1 for row, _ch, _pitch in notes if row == chord)
    if hits < 2 or chord < 1:
        return None
    # A few execution rows before the commands, unless that row is a whole
    # pulse. The end follows the start by the same delta, so the played
    # span keeps its length and every row across the seam plays once.
    start = chord - CUT_ROWS if step > 1 else chord
    if start < 0:
        start = 0
    delta = start - loop_row
    end = end_row + delta
    if end < start:
        return None
    return LoopFix(
        start_row=start, end_row=end, period=0, shift_pulses=0, step_rows=step,
    )


def _phase_loop(
    notes: list[tuple[int, int, int]],
    *,
    loop_row: int,
    end_row: int,
) -> LoopFix | None:
    """Rotate a whole-bar loop onto its downbeat, or None to keep the marker.

    `notes` is `(row, channel, pitch)` for note-ons. `end_row` is the row
    the jump is on today.
    """
    if end_row < loop_row or not notes:
        return None
    clock = _clock(notes)
    if clock is None:
        return None
    grid = _grid(notes, clock)
    echoes = _echoes(grid, clock.channel)
    _bass, lead = _roles(grid, echoes)
    loop_beat = _beat_of(loop_row, clock)
    end_beat = _beat_of(end_row, clock)
    chosen = _period(grid, echoes, loop_beat, end_beat)
    length_rows = end_row - loop_row + 1
    if chosen is None or not _whole_bars(length_rows, clock.step, chosen[0]):
        return _snap_empty_marker(notes, loop_row, end_row, clock.step)

    period, _ac = chosen
    vote, margin, gain, phase_scores = _phase_shift(
        grid, echoes, lead, loop_beat, end_beat, period,
    )
    # A non-zero vote has to clear the runner-up and the phase we are on.
    # Confirming the current phase does not, so a lead entry can still move it.
    vote_ok = vote != 0 and margin >= MIN_VOTE_MARGIN and gain >= MIN_PHASE_GAIN
    shift = _entry_shift(grid, lead, loop_beat, period)
    if shift is not None and _ensemble(grid, loop_beat, echoes) >= _ensemble(grid, loop_beat + shift, echoes) + TEXTURE_JUMP:
        shift = None
    # One pulse back is the late-marker case: the beat the marker sits on can
    # be the heavier one when a double arrives there. A longer walk back has
    # to land on a phase that is actually heavier, and it loses to a vote
    # that already names a downbeat.
    if shift is not None and abs(shift) > 1:
        dest = phase_scores.get((loop_beat + shift) % period, 0.0)
        here = phase_scores.get(loop_beat % period, 0.0)
        if dest < here + MIN_PHASE_GAIN:
            shift = None
    if shift is not None and vote_ok and shift != vote:
        shift = None
    # Shift 0 is a confirmed downbeat, so the order still opens a few rows
    # early. A vote that did not clear the bar is not that confirmation: the
    # marker stays where the VGM put it.
    confirmed = vote_ok or (vote == 0 and margin >= MIN_VOTE_MARGIN)
    if shift is None:
        if not confirmed:
            return _snap_empty_marker(notes, loop_row, end_row, clock.step)
        shift = vote
    if abs(shift) > period // 2:
        return _snap_empty_marker(notes, loop_row, end_row, clock.step)

    command = _command_row(grid, loop_beat + shift, echoes)
    if command is None:
        return _snap_empty_marker(notes, loop_row, end_row, clock.step)
    if abs(command - loop_row) > (period * clock.step) // 2 + clock.step:
        return None
    # A few rows before the commands. A one-row clock would put the cut on
    # the previous pulse, which is the mid-bar cut this pass is trying to
    # leave. The end follows the start by the same delta.
    start = max(0, command - CUT_ROWS) if clock.step > 1 else command
    delta = start - loop_row
    end = end_row + delta
    # The VGM jump can already sit a few rows past the last note. Moving
    # both ends by the same amount stays inside that span.
    last_row = max(row for row, _ch, _pitch in notes)
    if end < start or end > max(end_row, last_row) or start > max(end_row, last_row):
        return None
    start, end = _seat_seam(_exact_rows(notes), start, end, 0, max(end_row, last_row), end_row)
    if start == loop_row and end == end_row:
        return None
    return LoopFix(
        start_row=start,
        end_row=end,
        period=period,
        shift_pulses=shift,
        step_rows=clock.step,
    )


# A phrase match has to be this close before the seam search will move the
# loop. A short coincidence (an inner repetition that agrees for under a
# second) does not.
_SEAM_SCORE = 0.95
_SEAM_TOL = 2


def _pedals(notes: list[tuple[int, int, int]]) -> set[int]:
    """Channels that sit on one pitch class. They agree on every bar."""
    by: dict[int, list[int]] = defaultdict(list)
    for _row, ch, pitch in notes:
        by[ch].append(pitch % 12)
    pedals = set()
    for ch, pcs in by.items():
        if len(pcs) < 12:
            continue
        top = Counter(pcs).most_common(1)[0][1]
        if top / len(pcs) >= 0.8:
            pedals.add(ch)
    return pedals


def _note_maps(notes: list[tuple[int, int, int]]):
    """`(row -> {(ch, pc)})`, `(row -> note bits)`, and notes per row.

    A row's bits are one bit per (channel, pitch class); the widened map ORs
    the rows within `_SEAM_TOL` so a seam match can sit a couple of rows off.
    """
    by: dict[int, set[tuple[int, int]]] = defaultdict(set)
    bits: dict[int, int] = defaultdict(int)
    weight: dict[int, int] = defaultdict(int)
    for row, ch, pitch in notes:
        pc = pitch % 12
        by[row].add((ch, pc))
        bits[row] |= 1 << (ch * 12 + pc)
        weight[row] += 1
    wide: dict[int, int] = {}
    if bits:
        lo, hi = min(bits), max(bits)
        for row in range(lo - _SEAM_TOL, hi + _SEAM_TOL + 1):
            acc = 0
            for delta in range(-_SEAM_TOL, _SEAM_TOL + 1):
                acc |= bits.get(row + delta, 0)
            wide[row] = acc
    return by, bits, wide, weight


def _seam_score(bits, wide, start: int, other: int, width: int) -> float:
    """How much of each side's notes show up on the other, a couple of rows off.

    Both directions count. A sparse slice that is merely contained in a
    busier one does not score as a match.
    """
    left = right = hit_left = hit_right = 0
    for offset in range(width):
        here = bits.get(start + offset, 0)
        there = bits.get(other + offset, 0)
        if here:
            left += here.bit_count()
            hit_left += (here & wide.get(other + offset, 0)).bit_count()
        if there:
            right += there.bit_count()
            hit_right += (there & wide.get(start + offset, 0)).bit_count()
    if left < 8 or right < 8:
        return 0.0
    return min(hit_left / left, hit_right / right)


def _best_return(bits, wide, start: int, end_row: int, lo: int, hi: int, width: int, step: int):
    """Best copy of `[start, start+width)` in `[lo, hi]`, or `(0.0, None)`.

    The comparison window has to fit completely inside the track: a copy
    clipped by the end of the log would be scored over fewer rows and can
    beat a real seam. A module already cut at its loop end has no complete
    copy inside the track, so its loop is left alone.
    """
    best_score = 0.0
    best_at = None
    for other in range(lo, hi + 1, step):
        if end_row - other < width:
            continue
        score = _seam_score(bits, wide, start, other, width)
        if score > best_score:
            best_score = score
            best_at = other
    return best_score, best_at


def _attack_agrees(by, command: int, ret: int, pedals: set[int]) -> int:
    """Non-pedal channels that play the same pitch class within a few rows."""
    left: dict[int, set[int]] = defaultdict(set)
    right: dict[int, set[int]] = defaultdict(set)
    for delta in range(-3, 4):
        for ch, pc in by.get(command + delta, ()):
            if ch not in pedals:
                left[ch].add(pc)
        for ch, pc in by.get(ret + delta, ()):
            if ch not in pedals:
                right[ch].add(pc)
    return sum(1 for ch, pcs in left.items() if pcs & right.get(ch, frozenset()))


# The cut may move this many rows either way when a different seat makes the
# rows across the seam agree exactly. The end moves with the start, so the
# played length never changes.
SEAT_SHIFT = 2
# Rows after the seam the seat compares. The attack is what the ear follows,
# and the window must fit inside the log: the tail past the jump is the copy
# the comparison needs, and comparing against the log's end proves nothing.
SEAT_SPAN = 16


def _exact_rows(notes: list[tuple[int, int, int]]) -> dict[int, set[tuple[int, int]]]:
    """`row -> {(channel, pitch)}` at full pitch, for the seat comparison."""
    by: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for row, ch, pitch in notes:
        by[row].add((ch, pitch))
    return by


def _seat_cost(exact, start: int, end: int) -> int:
    """Voices that play on one side of the seam but not the other.

    The jump sits on `end`, so the row after it must be the loop's first
    row. The rows have to agree at the row here, not a couple of rows off:
    this measures what the module plays, while the fuzzy match only finds
    the seam.
    """
    cost = 0
    for offset in range(SEAT_SPAN):
        left = exact.get(start + offset, frozenset())
        right = exact.get(end + 1 + offset, frozenset())
        cost += len(left ^ right)
    return cost


def _seat_seam(exact, start: int, end: int, lo: int, hi: int, content_end: int) -> tuple[int, int]:
    """The seat whose rows agree exactly across the seam, when one exists.

    Only a nearby seat that removes every disagreement wins, nearest first:
    a thin improvement means the tail itself is not a row-exact copy, and
    moving the cut on that evidence only trades one wobble for another. The
    window must fit inside the log, so a tail that runs out at the jump
    cannot pass on emptiness. A track whose rows already agree never moves.
    """
    if end + SEAT_SPAN > content_end:
        return start, end
    if _seat_cost(exact, start, end) == 0:
        return start, end
    for distance in range(1, SEAT_SHIFT + 1):
        for moved in (distance, -distance):
            if start + moved < lo or end + moved > hi:
                continue
            if end + moved + SEAT_SPAN > content_end:
                continue
            if _seat_cost(exact, start + moved, end + moved) == 0:
                return start + moved, end + moved
    return start, end


def _phrase_seam(
    notes: list[tuple[int, int, int]],
    loop_row: int,
    end_row: int,
    forward_rows: int,
) -> LoopFix | None:
    """Move the loop onto the phrase start the ending of the track repeats.

    The VGM loop length is the invariant: the marker can sit inside the
    phrase, but the loop keeps its length, so a candidate start's copy sits
    one loop length later, with the comparison window fitting completely
    inside the track. The marker being late at the start matches the end of
    the log being late too, and a shorter copy (an inner repetition) is not
    a seam at all. A module already cut at its loop end is left alone, so
    reconversion is stable. Candidates run a window on either side of the
    marker; the earliest long run of near-perfect matches wins, and its
    heaviest command row opens the loop.
    """
    span = end_row - loop_row
    window = min(int(forward_rows), span)
    if window < 16 or span < 16:
        return None
    by, bits, wide, weight = _note_maps(notes)
    if not bits:
        return None
    compare = max(16, int(forward_rows * 0.375))
    tail = max(12, int(forward_rows * 0.125))
    end_lo = end_row - window
    end_hi = end_row - tail
    # One loop length later is where a candidate's copy has to sit; the
    # slack covers the marker's own rounding and per-row alignment.
    loop_rows = span + 1
    slack = max(4, loop_rows // 200)
    first = max(0, loop_row - window)
    last = loop_row + window

    def scan(first: int, last: int) -> dict[int, tuple[float, int]]:
        found = {}
        for start in range(first, last + 1, 2):
            near = start + loop_rows
            lo = max(end_lo, start + tail, near - slack)
            hi = min(end_hi, near + slack, end_row - compare)
            if hi < lo:
                continue
            score, ret = _best_return(bits, wide, start, end_row, lo, hi, compare, 2)
            if ret is not None:
                found[start] = (score, ret)
        return found

    scores = scan(first, last)
    if not scores:
        return None
    peak = max(item[0] for item in scores.values())
    if peak < _SEAM_SCORE:
        return None
    high = sorted(row for row, (score, _ret) in scores.items() if score >= max(_SEAM_SCORE, peak - 0.03))
    if not high:
        return None
    need = int(forward_rows * 0.15)
    runs: list[list[int]] = []
    for row in high:
        if not runs or row - runs[-1][-1] > 4:
            runs.append([row])
        else:
            runs[-1].append(row)
    # The earliest run that reaches the peak band is the phrase start; a
    # later copy is the same phrase again and drops everything before it.
    band = next((run for run in runs if run[-1] - run[0] >= need), None)
    if band is None:
        return None
    band_start = band[0]
    lag = scores[band_start][1] - band_start
    pedals = _pedals(notes)
    clock = _clock(notes)
    step = clock.step if clock is not None else 2
    snap_end = band_start + max(8, forward_rows // 8)
    command = None
    command_n = -1
    ret = None
    for row in range(max(0, band_start - 4), snap_end + 1):
        if weight[row] < 3 or weight[row] < command_n:
            continue
        near = row + lag
        command_notes = {
            (ch, pc) for ch, pc in by.get(row, ()) if ch not in pedals
        }
        agreed = 0
        agreed_at = None
        agreed_share = -1
        agreed_n = -1
        for candidate in range(near - 4, near + 5):
            channels = _attack_agrees(by, row, candidate, pedals)
            share = len(command_notes & {
                (ch, pc) for ch, pc in by.get(candidate, ()) if ch not in pedals
            })
            if channels < 2 and share < 2:
                continue
            # Prefer the row that actually holds the same notes. A neighbour
            # can "agree" only because the chord sits a row or two away.
            if (share, weight[candidate], channels) > (agreed_share, agreed_n, agreed):
                agreed = channels
                agreed_share = share
                agreed_n = weight[candidate]
                agreed_at = candidate
        if agreed_at is None:
            continue
        if weight[row] > command_n or command is None:
            command_n = weight[row]
            command = row
            ret = agreed_at
    if command is None or ret is None or ret <= command:
        return None
    start = max(0, command - CUT_ROWS) if step > 1 else command
    # Played length equals the distance between the two copies of the chord,
    # so the jump lands on the early row and the chord is not struck twice.
    # The end follows the start: both ends move by the same amount, so the
    # seam stays continuous (the row after the end copies the loop's first
    # row) and the played span keeps its length.
    end = start + (ret - command) - 1
    if end <= start or end > end_row or start > end_row:
        return None
    start, end = _seat_seam(_exact_rows(notes), start, end, 0, end_row, end_row)
    if start == loop_row and end == end_row:
        return None
    return LoopFix(
        start_row=start,
        end_row=end,
        period=0,
        shift_pulses=0,
        step_rows=step,
    )


def find_loop(
    notes: list[tuple[int, int, int]],
    *,
    loop_row: int,
    end_row: int,
    forward_rows: int | None = None,
) -> LoopFix | None:
    """Return a new loop, or None to keep the VGM marker.

    `forward_rows` is the window around the marker a phrase start may sit
    in. The converter passes four seconds of rows, and a shorter loop passes
    its own length. Omitted, only the within-bar downbeat is considered.
    """
    fix = _phase_loop(notes, loop_row=loop_row, end_row=end_row)
    if fix is not None or not forward_rows or forward_rows < 8:
        return fix
    return _phrase_seam(notes, loop_row, end_row, int(forward_rows))


def describe_fix(fix: LoopFix, loop_row: int) -> str:
    """One short clause for the convert line."""
    delta = fix.start_row - loop_row
    if fix.period and fix.shift_pulses:
        return f"loop {fix.shift_pulses:+d}/{fix.period} ({delta:+d} rows)"
    if delta:
        ahead = "early" if delta < 0 else "late"
        n = abs(delta)
        unit = "row" if n == 1 else "rows"
        return f"loop {n} {unit} {ahead}"
    return "loop kept"


def _last_content_row(song) -> int:
    last = 0
    for ev in song.events:
        last = max(last, ev.sample)
        if ev.vol_pts:
            last = max(last, ev.vol_pts[-1][0])
    for sample, _ch, _cmd, _val in getattr(song, "channel_fx", ()) or ():
        last = max(last, sample)
    return song.row_of(last)


def row_to_sample(song, row: int) -> int:
    """A sample `Song.row_of` maps back to `row`."""
    return song.t0 + int(math.floor(row * song.samples_per_row + 1e-6))


# A seat for the wrap, checked against the recording's own copy of the loop
# head. The last pass drifts a row or two, and a whole figure can land late.
# A note's copy is searched within _COPY_DRIFT rows. _COPY_DRIFT_WIDE is the
# drift that still counts as the same music when a start is judged on its
# own: a pick that fails at this width is not a copy of anything.
_COPY_DRIFT = 3
_COPY_DRIFT_WIDE = 8
# The copy window in rows, the share of each side that has to agree, and the
# thin-window floor: a handful of notes is not evidence. The wide drift is
# used when a start is judged on its own, where a note or two of the drift
# can still go missing.
_COPY_SPAN = 96
_COPY_SHARE = 0.95
_COPY_SHARE_WIDE = 0.90
_COPY_MIN_EVENTS = 4
# A pick this far past the last recorded copy row has no copy of its own.
# A few rows of overshoot still reads the stretch behind it.
_COPY_EDGE = 3


def _copy_index(notes) -> dict[tuple[int, int], list[int]]:
    """(channel, pitch) -> rows, for the copy comparison."""
    by: dict[tuple[int, int], list[int]] = defaultdict(list)
    for row, ch, pitch in notes:
        by[(ch, pitch)].append(row)
    for rows in by.values():
        rows.sort()
    return by


def _copy_shares(by, start: int, length: int, end_row: int, drift: int):
    """Matched and total notes each way over the recorded copy.

    Forward counts the start's own notes that come back one loop length
    later. Backward counts the copy's notes that the start accounts for. The
    comparison stops where the recording does: past the declared end there is
    no copy to check against.
    """
    head_end = min(start + _COPY_SPAN, end_row - length + 1)
    copy_end = min(start + length + _COPY_SPAN, end_row + 1)
    fwd = total_fwd = 0
    for rows in by.values():
        i = bisect_left(rows, start)
        while i < len(rows) and rows[i] < head_end:
            total_fwd += 1
            target = rows[i] + length
            j = bisect_left(rows, target - drift)
            if j < len(rows) and rows[j] <= target + drift:
                fwd += 1
            i += 1
    back = total_back = 0
    for rows in by.values():
        i = bisect_left(rows, start + length)
        while i < len(rows) and rows[i] < copy_end:
            total_back += 1
            target = rows[i] - length
            j = bisect_left(rows, target - drift)
            if j < len(rows) and rows[j] <= target + drift:
                back += 1
            i += 1
    return fwd, total_fwd, back, total_back


def _copy_faithful(by, start: int, length: int, end_row: int, drift: int,
                   share: float = _COPY_SHARE) -> bool:
    fwd, total_fwd, back, total_back = _copy_shares(by, start, length, end_row, drift)
    if total_fwd < _COPY_MIN_EVENTS or total_back < _COPY_MIN_EVENTS:
        return False
    return fwd >= total_fwd * share and back >= total_back * share


def _copy_period(by, start: int, length: int, end_row: int,
                 drift: int) -> int | None:
    """The row displacement the recording's copy of the head measures.

    Every matched pair contributes its own displacement. The copy's period is
    the one most pairs agree on. None when there are too few pairs to tell
    """
    counts: dict[int, int] = defaultdict(int)
    head_end = min(start + _COPY_SPAN, end_row - length + 1)
    for rows in by.values():
        i = bisect_left(rows, start)
        while i < len(rows) and rows[i] < head_end:
            target = rows[i] + length
            j = bisect_left(rows, target - drift)
            if j < len(rows) and rows[j] <= target + drift:
                counts[rows[j] - rows[i]] += 1
            i += 1
    if not counts:
        return None
    period, pairs = max(sorted(counts.items()), key=lambda kv: kv[1])
    return period if pairs >= _COPY_MIN_EVENTS else None


def _copy_head_start(by, samples_at, chan_at, start: int, length: int,
                     drift: int, span: int = 8) -> tuple[int, int | None]:
    """The row in front of `start` the recording's copy replays, plus the
    first sample of that copy.

    Rows ahead of the VGM marker are intro material only while nothing brings
    them back. When the copy at the tail sounds a row again, one loop later,
    the loop opens on it, and the copy's own first sample pins the wrap.
    Empty rows do not end the walk (a pickup can be followed by rests); a row
    the copy does not answer does. Slides move their carriers' pitches while
    the copy runs, so a row also counts as answered when most of its channels
    sound inside the copy's window.
    """
    first: int | None = None
    moved = False
    chan_rows: dict[int, list[int]] = defaultdict(list)
    for (ch, _pitch), rows in by.items():
        chan_rows[ch].extend(rows)
    for rows in chan_rows.values():
        rows.sort()
    for row in range(start - 1, max(-1, start - 1 - span), -1):
        cells = [key for key, rows in by.items() if row in rows]
        if not cells:
            continue
        target = row + length
        hits = 0
        chans: set[int] = set()
        chan_hits = 0
        for key in cells:
            rows = by[key]
            i = bisect_left(rows, target - drift)
            ok = i < len(rows) and rows[i] <= target + drift
            ch = key[0]
            if ok:
                sample = samples_at.get((rows[i], ch, key[1]))
                if sample is None:
                    sample = chan_at.get((rows[i], ch))
                if sample is not None and (first is None or sample < first):
                    first = sample
            hits += int(ok)
            if ch not in chans:
                chans.add(ch)
                rowsi = chan_rows[ch]
                j = bisect_left(rowsi, target - drift)
                if j < len(rowsi) and rowsi[j] <= target + drift:
                    chan_hits += 1
        if hits * 3 >= len(cells) * 2 or (
            len(chans) >= 4 and chan_hits * 4 >= len(chans) * 3
        ):
            start = row
            moved = True
        else:
            break
    return start, (first if moved else None)


def _copy_aligned(notes, picked: int, end_row: int, length: int | None,
                  window: int) -> int:
    """The start the recording's own copy puts the wrap on.

    The rows past the declared loop end are the copy of the loop's first
    rows. The wrap belongs where that copy lines up. A pick below the
    verified stretch wraps into rows the copy does not answer, and the notes
    after the loop point are then not the ones the source plays there. A pick
    a few rows past the last recorded copy row still reads the stretch
    behind it; further out there is no evidence either way and the pick
    stands.
    """
    if length is None or length < 16:
        return picked
    by = _copy_index(notes)
    top = end_row - length
    stretch = None
    for start in range(top, max(-1, top - window), -1):
        if start < 0:
            break
        if _copy_faithful(by, start, length, end_row, _COPY_DRIFT):
            stretch = start
    if stretch is None:
        return picked
    if picked <= top:
        if picked < stretch and not _copy_faithful(
            by, picked, length, end_row, _COPY_DRIFT_WIDE, _COPY_SHARE_WIDE
        ):
            return stretch
        return picked
    if picked <= top + _COPY_EDGE:
        return stretch
    return picked


def _declared_length(song) -> int | None:
    """The VGM header's loop length in rows."""
    if not song.vgm.loop_samples or song.samples_per_row <= 0:
        return None
    length = int(round(song.vgm.loop_samples / song.samples_per_row))
    return length if length >= 2 else None


def _declared_end_row(song, start_row: int) -> int | None:
    """The jump row the declared loop length puts the wrap on.

    The VGM header's loop length is the one iteration length the rip
    vouches for, so the wrap sits exactly that many rows after the start
    the fix chose, whatever the recorded content does around it. The
    recording usually runs a little past it (the next iteration's head),
    and those rows must stay outside the loop: a jump row carrying the
    copy's notes plays them, and the seam strikes the head twice.
    """
    length = _declared_length(song)
    if length is None:
        return None
    return start_row + length - 1


def apply_loop(song) -> str | None:
    """Move `song`'s loop onto the downbeat. None when the VGM has no loop.

    `'loop kept'` when the marker already sits where it should, or the grid
    does not say. A description when the order cut moves; that case is also
    a warning on the song.
    """
    if song.loop_sample is None:
        return None
    notes = [
        (song.row_of(ev.sample), ev.ch, ev.note)
        for ev in song.events
        if ev.on and 0 <= ev.note < NOTE_OFF
    ]
    loop_row = song.row_of(song.loop_sample)
    end_row = _last_content_row(song)
    if not notes or end_row < loop_row:
        return "loop kept"
    # Four seconds either side of the marker, or the whole loop when it is
    # shorter. The seam search applies that cap itself.
    forward = 0
    if song.hz > 0 and song.speed > 0:
        forward = int(round(4.0 * song.hz / float(song.speed)))
    fix = find_loop(
        notes, loop_row=loop_row, end_row=end_row, forward_rows=forward,
    )
    picked = fix.start_row if fix is not None else loop_row
    start_row = _copy_aligned(
        notes, picked, end_row, _declared_length(song), forward,
    )
    head_sample: int | None = None
    if fix is None:
        # Rows ahead of the marker are intro material only while nothing
        # brings them back; the copy settles where the loop opens even when
        # no other pick was found. (A pick the copy already settled is past
        # this point and keeps its seat.)
        length = _declared_length(song)
        if length is not None:
            samples_at: dict[tuple[int, int, int], int] = {}
            chan_at: dict[tuple[int, int], int] = {}
            for ev in song.events:
                if ev.on and 0 <= ev.note < NOTE_OFF:
                    key = (song.row_of(ev.sample), ev.ch, ev.note)
                    prev = samples_at.get(key)
                    if prev is None or ev.sample < prev:
                        samples_at[key] = ev.sample
                    ckey = (key[0], ev.ch)
                    prev = chan_at.get(ckey)
                    if prev is None or ev.sample < prev:
                        chan_at[ckey] = ev.sample
            start_row, head_sample = _copy_head_start(
                _copy_index(notes), samples_at, chan_at, start_row, length,
                _COPY_DRIFT,
            )
        if start_row == loop_row:
            return "loop kept"
        fix = LoopFix(
            start_row=start_row, end_row=start_row, period=0, shift_pulses=0,
            step_rows=1,
        )
    elif start_row != fix.start_row:
        fix = replace(fix, start_row=start_row)
    declared = _declared_end_row(song, fix.start_row)
    if declared is not None:
        wrap_row = declared
        # The recording's own copy of the head measures the period. When it
        # puts the wrap earlier than the header does, the header runs past
        # the copy and the copy's first row would sound at the seam.
        length = _declared_length(song)
        by = _copy_index(notes)
        if _copy_faithful(by, fix.start_row, length, end_row, _COPY_DRIFT):
            period = _copy_period(by, fix.start_row, length, end_row, _COPY_DRIFT)
            if period is not None and period < length:
                wrap_row = fix.start_row + period - 1
        elif head_sample is not None:
            # The copy's own first sample says where the wrap belongs: the
            # row before it.
            wrap_row = max(fix.start_row, int(head_sample / song.samples_per_row) - 1)
        if wrap_row != fix.end_row:
            fix = replace(fix, end_row=wrap_row)
    # A tenth of a row inside the row: every row rate reads the point as the
    # row the loop opens on, not the row below.
    nudge = int(song.samples_per_row / 10)
    song.loop_sample = row_to_sample(song, fix.start_row) + nudge
    song.loop_end_sample = row_to_sample(song, fix.end_row) + nudge
    # The wrap sample: content at or past it is the copy of the head and
    # stays unwritten at every row rate. The tighter of the last row's end
    # and the header's own one-period point. A copy that drifts a hair early
    # still lands past this point, so the sliver of its first row that a
    # condensed grid would fold back stays out.
    cut = row_to_sample(song, fix.end_row + 1)
    if song.vgm.loop_samples:
        cut = min(cut, song.loop_sample + song.vgm.loop_samples)
    song.loop_cut_sample = cut
    text = describe_fix(fix, loop_row)
    song.warnings.append(text)
    return text
