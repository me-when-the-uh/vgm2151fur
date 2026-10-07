#!/usr/bin/env python3
"""Tests for the measured key-on lattice grid (vgm2151fur.timing + placement)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.analyze import (
    Song,
    VGM_RATE,
    _shift_note_e5,
    analyze,
    FMPatch,
    NoteEvent,
    OpRegs,
    opm_pitch_offset,
    transpose_opm_events,
)
from vgm2151fur.furio import load_fur_bytes, parse_fur
from vgm2151fur.furwrite import write_fur
from vgm2151fur.timing import estimate_grid
from vgm2151fur.vgm import ChipWrite, VgmFile, load_vgm

SF = ROOT / "05 Starfield (Stage 4 BGM).vgz"
DTA = ROOT / "07 Destroy Them All (Stage 6 BGM).vgz"
ATTACK = ROOT / "04 Attack the Enemy (Stage 1 BGM).vgz"


def _vgm_with_keyons(onsets):
    """Synthetic VgmFile carrying only 0x08 key-on writes."""
    writes = [ChipWrite(sample=s, chip="ym2151", chip_id=0, reg=0x08, val=0x78 | ch) for s, ch in onsets]
    writes.sort(key=lambda w: w.sample)
    return VgmFile(
        path=Path("synthetic.vgm"),
        buf=b"",
        version=0x151,
        total_samples=writes[-1].sample + 1 if writes else 0,
        loop_samples=0,
        loop_file_pos=None,
        loop_sample=None,
        ym2151_clock=3579545,
        k007232_clock=3579545,
        gd3={},
        roms=[],
        writes=writes,
    )


def _vgm_with_writes(writes):
    writes = sorted(writes, key=lambda w: w.sample)
    return VgmFile(
        path=Path("synthetic.vgm"),
        buf=b"",
        version=0x151,
        total_samples=writes[-1].sample + 1 if writes else 0,
        loop_samples=0,
        loop_file_pos=None,
        loop_sample=None,
        ym2151_clock=3579545,
        k007232_clock=3579545,
        gd3={},
        roms=[],
        writes=writes,
    )


def _song(hz, speed, subdiv, vgm):
    return Song(
        vgm=vgm,
        t0=0,
        loop_sample=None,
        speed=speed,
        hz=hz,
        fm_patches=[],
        pcm_samples=[],
        events=[],
        row_subdiv=subdiv,
    )


class TestEstimateGrid(unittest.TestCase):
    def test_lattice_sixteenths(self):
        # 2500-sample subdivision on ch1, 5000-sample units on ch0/ch2.
        # The estimator must lock onto the *finest* playing unit.
        onsets = []
        t = 0
        for k in range(80):
            onsets.append((t, 0))
            onsets.append((t, 1))
            onsets.append((t + 2500, 1))
            if k % 2 == 0:
                onsets.append((t, 2))
            t += 5000
        grid = estimate_grid(_vgm_with_keyons(onsets))
        self.assertIsNotNone(grid)
        self.assertAlmostEqual(grid.unit, 2500.0, delta=60.0)
        self.assertGreaterEqual(grid.subdiv, 4)
        self.assertAlmostEqual(grid.row, grid.unit / grid.subdiv, places=6)
        self.assertGreaterEqual(grid.speed, 1)
        self.assertAlmostEqual(grid.hz, 44100.0 * grid.speed / grid.row, places=6)
        self.assertAlmostEqual(grid.tick, grid.row / grid.speed, places=6)
        self.assertGreaterEqual(grid.confidence, 0.9)

    def test_irregular_returns_none(self):
        # Pseudo-random spacing must not be accepted as a lattice.
        onsets = []
        t = 0
        for k in range(90):
            t += 3000 + (k * 977) % 2500
            onsets.append((t, k % 4))
        self.assertIsNone(estimate_grid(_vgm_with_keyons(onsets)))

    def test_slow_unit_is_folded_not_dropped(self):
        # Sega System 16C shop/ending themes: the fastest melodic event on a
        # channel is 9560 samples (and often 2x that), well past the 16th-note
        # band.  The estimator used to return None, the caller fell back to
        # 60 Hz/speed 4 = 15 rows/s, and per-frame pitch writes had nowhere to
        # go (Mysterious Shop).  The unit must fold in halves instead.
        onsets = []
        t = 0
        for k in range(60):
            onsets.append((t, k % 8))
            onsets.append((t + 4780, k % 8))
            if k % 2 == 0:
                onsets.append((t, (k + 1) % 8))
            t += 9560
        grid = estimate_grid(_vgm_with_keyons(onsets))
        self.assertIsNotNone(grid)
        self.assertGreaterEqual(grid.unit, 800.0)
        self.assertLessEqual(grid.unit, 8000.0)
        self.assertGreaterEqual(grid.hz / grid.speed, 45.0)  # rows/s
        self.assertGreaterEqual(grid.confidence, 0.9)


class TestPlacement(unittest.TestCase):
    def setUp(self):
        self.vgm = _vgm_with_keyons([(i * 5000, 0) for i in range(8)])

    def test_speed1_nearest_row(self):
        song = _song(44100.0 / 625.0, 1, 8, self.vgm)
        tick = 625.0
        for sample in (0, 100, 312, 313, 624, 625, 5000, 12345):
            row, delay = song.place(sample)
            self.assertEqual(delay, 0)
            self.assertEqual(row, int((sample / tick) + 0.5))
            placed = row * tick
            self.assertLessEqual(abs(placed - sample), tick / 2 + 0.5)

    def test_speed7_floor_plus_tick_delay(self):
        song = _song(60.0, 7, 1, self.vgm)
        row_len = 44100.0 / 60.0 * 7
        tick = 44100.0 / 60.0
        for sample in (0, 100, 700, 3000, 3600, 5000, 10290, 12000):
            row, delay = song.place(sample)
            self.assertGreaterEqual(delay, 0)
            self.assertLess(delay, 7)
            placed = row * row_len + delay * tick
            self.assertLessEqual(abs(placed - sample), tick / 2 + 0.5)


def _written_note_times(m, ch):
    """Reconstruct played note times, honouring D00 (0x0D) cuts and EDxx delays."""
    row_len = m.speed / m.hz
    tick = 1.0 / m.hz
    out = []
    g = 0
    for oi in range(m.n_ord):
        rows = m.patterns.get((ch, m.orders[ch][oi]))
        span = m.pat_len
        for r in range(m.pat_len):
            if rows and any(c == 0x0D for c, _v in rows[r].fx):
                span = r + 1
                break
        for r in range(span):
            if rows and 0 <= rows[r].note <= 179:
                delay = 0
                for c, v in rows[r].fx:
                    if c == 0xED:
                        delay = v
                out.append(((g + r) * row_len + delay * tick) * 44100.0)
        g += span
    return out


def _keyon_samples(vgm, ch):
    return [
        w.sample
        for w in vgm.writes
        if w.chip == "ym2151" and w.reg == 0x08 and (w.val & 0x78) and (w.val & 7) == ch
    ]


@unittest.skipUnless(SF.is_file(), "Salamander Starfield rip missing")
class TestSalamanderGrid(unittest.TestCase):
    def test_starfield_grid(self):
        song = analyze(load_vgm(SF), pcm=True)
        self.assertGreater(song.speed, 1)  # ticks per row (ED placement)
        self.assertEqual(song.row_subdiv, 8)
        self.assertAlmostEqual(song.hz / song.speed, 70.2, delta=1.5)  # rows/s
        self.assertIn("lattice", song.grid_note)

    def test_destroy_them_all_grid(self):
        song = analyze(load_vgm(DTA), pcm=True)
        self.assertGreater(song.speed, 1)
        self.assertEqual(song.row_subdiv, 8)
        self.assertAlmostEqual(song.hz / song.speed, 61.24, delta=1.5)  # rows/s
        self.assertIn("lattice", song.grid_note)

    def test_placement_within_half_tick(self):
        """Every key-on must land within half a tick of the original."""
        for path, tol_ms in ((SF, 2.0), (DTA, 2.0)):
            vgm = load_vgm(path)
            song = analyze(vgm, pcm=True)
            data = write_fur(song, include_pcm=True, include_fm=True)
            m = parse_fur(data)
            half_tick_ms = 1000.0 / m.hz / 2
            self.assertLessEqual(half_tick_ms + 0.6, tol_ms + 0.01)
            for ch in range(8):
                orig = _keyon_samples(vgm, ch)
                if not orig:
                    continue
                written = _written_note_times(m, ch)
                self.assertEqual(len(written), len(orig), f"{path.name} ch{ch} note count")
                for o, w in zip(orig, written):
                    self.assertLessEqual(
                        abs(w - o) / 44.1, tol_ms,
                        f"{path.name} ch{ch} sample {o}: {abs(w - o) / 44.1:.1f}ms",
                    )

    def test_note_values_100_102_survive_roundtrip(self):
        """Notes E-3/F-3/F#-3 are real pitches, not note-off codes (off = 180)."""
        vgm = load_vgm(SF)
        song = analyze(vgm, pcm=True)
        data = write_fur(song, include_pcm=True, include_fm=True)
        m = parse_fur(data)
        got = {}
        for (ch, idx), rows in m.patterns.items():
            got[ch] = got.get(ch, 0) + sum(1 for r in rows if 0 <= r.note <= 179)
        for ch in range(8):
            ons = [e for e in song.events if e.on and not e.pcm and e.ch == ch]
            self.assertEqual(got.get(ch, 0), len(ons), f"ch{ch}")


class TestPercussionSweeps(unittest.TestCase):
    """Twin16 channel-5 percussion pitch sweeps become 01xx/02xx ramps."""

    def _song_with_sweep(self):
        # A key-on lattice on ch0 so the grid estimator has something to
        # measure, plus one drum note on ch5 with a KF ramp like the driver's
        # (-5 fraction steps per ~88 samples via register steps of 20).
        writes = [
            ChipWrite(sample=k * 5000, chip="ym2151", chip_id=0, reg=0x08, val=0x78)
            for k in range(60)
        ]
        writes.append(ChipWrite(1000, "ym2151", 0, 0x08, 0x78 | 5))
        writes.append(ChipWrite(1040, "ym2151", 0, 0x28 | 5, 0x46))
        # KC drops a semitone every ~12.5 KF steps, skipping the unused
        # nibbles (0x43/0x47 alias the neighbouring notes).
        kc_steps = {13: 0x45, 26: 0x44, 39: 0x42}
        for k in range(46):
            writes.append(
                ChipWrite(1042 + k * 88, "ym2151", 0, 0x30 | 5, (0xEC - 20 * k) & 0xFF)
            )
            if (k + 1) in kc_steps:
                writes.append(
                    ChipWrite(1040 + (k + 1) * 88, "ym2151", 0, 0x28 | 5, kc_steps[k + 1])
                )
        writes.append(ChipWrite(5100, "ym2151", 0, 0x08, 5))
        return analyze(_vgm_with_writes(writes), pcm=False)

    def test_sweep_captured_with_end(self):
        song = self._song_with_sweep()
        drum = next(e for e in song.events if e.on and e.ch == 5)
        self.assertGreater(len(drum.sweep), 4)
        deltas = [d for _s, d in drum.sweep]
        self.assertLess(deltas[-1], -200)  # ~ -3.6 semitones in 1/64 units
        self.assertGreater(deltas[0], deltas[-1])  # descending
        self.assertEqual(drum.end, 5100)

    def test_note_off_keeps_sweep_end(self):
        song = self._song_with_sweep()
        data = write_fur(song, include_pcm=False, include_fm=True)
        m = parse_fur(data)
        ramps, stops = [], []
        for (ch, _idx), rows in m.patterns.items():
            if ch != 5:
                continue
            for r in rows:
                for c, v in r.fx:
                    if c == 0x02:
                        (stops if v == 0 else ramps).append(v)
        self.assertTrue(ramps, "no pitch ramp written")
        self.assertGreater(max(ramps), 0)
        self.assertTrue(stops, "no ramp stop written")

    def test_single_mid_note_step_is_a_sweep(self):
        # Block Hole's octave slams write one KC pair mid-note and hold it.
        # With no second write there is no spread *between* the points, and the
        # old range check threw the whole trajectory away: the note stayed at
        # its old pitch until the next key-on.
        writes = [
            ChipWrite(sample=k * 5000, chip="ym2151", chip_id=0, reg=0x08, val=0x78)
            for k in range(60)
        ]
        writes.append(ChipWrite(1000, "ym2151", 0, 0x08, 0x78 | 4))
        writes.append(ChipWrite(995, "ym2151", 0, 0x28 | 4, 0x40))  # set before key-on
        writes.append(ChipWrite(2000, "ym2151", 0, 0x28 | 4, 0x30))  # -12 key codes
        writes.append(ChipWrite(5100, "ym2151", 0, 0x08, 4))
        song = analyze(_vgm_with_writes(writes), pcm=False)
        note = next(e for e in song.events if e.on and e.ch == 4)
        self.assertEqual(len(note.sweep), 1)
        self.assertEqual(note.sweep[0][1], -768)  # one octave in 1/64 units
        data = write_fur(song, include_pcm=False, include_fm=True)
        m = parse_fur(data)
        ramps = [
            v for (ch, _i), rows in m.patterns.items() if ch == 4
            for r in rows for c, v in r.fx if c == 0x02 and v
        ]
        self.assertTrue(ramps, "the mid-note step was dropped instead of ramped")


def _traj_at(pts, t):
    """Step function over (sample, delta) pairs; 0 before the first write."""
    value = 0
    for s, d in pts:
        if s > t:
            break
        value = d
    return float(value)


class TestSweepEncoder(unittest.TestCase):
    """Structural guarantees of furwrite._emit_sweep (docs/salamander-opm.md).

    The writer samples the driver's step trajectory at row boundaries: the
    last (partial) row must be rated over its full span so the ramp lands on
    the trajectory's held final value instead of overshooting; a later note's
    own row may carry this sweep's stop but never a nonzero rate or fine tune;
    jumps that land on a row boundary become E5 steps.
    """

    HZ = 510.2018127441406  # speed 7 -> 605.1-sample rows (Burning Fighter)

    def _song(self, events):
        song = _song(self.HZ, 7, 4, _vgm_with_keyons([]))
        song.fm_patches = [FMPatch(alg=4, fb=0, ams=0, pms=0, ops=tuple(OpRegs() for _ in range(4)))]
        song.events = events
        return song

    def _rows(self, song, ch):
        m = parse_fur(write_fur(song, include_pcm=False))
        rows: dict[int, object] = {}
        for oi, pat in enumerate(m.orders[ch]):
            for i, row in enumerate(m.patterns[(ch, pat)]):
                rows[oi * m.pat_len + i] = row
        return rows

    def _rate(self, row):
        rate = 0
        for c, v in row.fx:
            if c == 0x02:
                rate = -v
            elif c == 0x01:
                rate = v
        return rate

    def test_last_row_rates_full_span(self):
        # A staircase dive that bottoms out and holds (the Surprise Attack
        # shape): the row containing the final write must carry a rate that
        # reaches the held value at the row's *end*, or the pitch overshoots
        # the bottom by the leftover ticks and E5 cannot pin that back.
        pts = tuple((1190 + 190 * k, -64 * (k + 1)) for k in range(18))
        end = pts[-1][0]
        ev = NoteEvent(sample=1000, ch=2, on=True, note=108, ins=0, vol=-1,
                       e5=0x80, pan=0, end=20000, sweep=pts)
        song = self._song([ev, NoteEvent(sample=20000, ch=2, on=False, note=180,
                                         ins=-1, vol=-1, e5=0x80, pan=0)])
        rows = self._rows(song, 2)
        end_row, _d = song.place(end)
        expected = (_traj_at(pts, song.samples_per_row * (end_row + 1)) - _traj_at(pts, song.samples_per_row * end_row)) / song.speed
        self.assertAlmostEqual(self._rate(rows[end_row]), round(expected), delta=2)
        # The ramp lands on the final value at the row's end (no overshoot).
        entry = _traj_at(pts, song.samples_per_row * end_row)
        self.assertAlmostEqual(entry + self._rate(rows[end_row]) * song.speed, pts[-1][1], delta=8)

    def test_later_note_row_is_kept_clean(self):
        # The dive's last writes run into the next note's row; the writer must
        # clip the sweep there instead of writing its tail rate/E5 onto the
        # new note's own row (which corrupted its attack and whole dive).
        pts = tuple((1190 + 190 * k, -64 * (k + 1)) for k in range(22))
        self.assertGreater(pts[-1][0], 8 * 605.1)  # crosses into row 8
        ev1 = NoteEvent(sample=1000, ch=2, on=True, note=108, ins=0, vol=-1,
                        e5=0x80, pan=0, end=4900, sweep=pts)
        ev2 = NoteEvent(sample=5000, ch=2, on=True, note=108, ins=0, vol=-1,
                        e5=0x80, pan=0, end=20000)
        song = self._song([ev1, ev2, NoteEvent(sample=20000, ch=2, on=False, note=180,
                                               ins=-1, vol=-1, e5=0x80, pan=0)])
        rows = self._rows(song, 2)
        next_row, _d = song.place(5000)
        # At most a stop: the key-on reads its own pitch, and a later sweep of
        # the new note replaces the stop with its own k=0 rate.
        self.assertEqual(self._rate(rows[next_row]), 0)
        # Its E5 may only be the note's own value (the replay restores it).
        self.assertFalse(
            any(c == 0xE5 and v != ev2.e5 for c, v in rows[next_row].fx),
            "the next note's row carries the previous sweep's fine tune",
        )
        # The tail still travels on the last row this sweep owns.
        self.assertNotEqual(self._rate(rows[next_row - 1]), 0)

    def test_jump_after_row_start_becomes_e5(self):
        # A driver jump just after a row start is snapped with E5 (clamped to
        # the fine-tune range) instead of being left to the ramp.
        row_len = 44100.0 * 7 / self.HZ
        pts = ((int(2 * row_len), 0), (int(2 * row_len) + 86, -300), (12000, -300))
        ev = NoteEvent(sample=1000, ch=2, on=True, note=108, ins=0, vol=-1,
                       e5=0x80, pan=0, end=20000, sweep=pts)
        song = self._song([ev, NoteEvent(sample=20000, ch=2, on=False, note=180,
                                         ins=-1, vol=-1, e5=0x80, pan=0)])
        rows = self._rows(song, 2)
        e5 = [v for c, v in rows[2].fx if c == 0xE5]
        self.assertEqual(e5, [0x40])  # -300 clamped to -64 (fine-tune range)

    def test_tail_pin_does_not_double_count(self):
        # A riser that reaches its held value inside the last row (the Fantasy
        # Zone II DX shape).  The last row is already rated over its full span,
        # so the E5 pin only fixes the rounding residue; subtracting the tail's
        # travel again dropped the pitch by up to a whole tone and E5 could not
        # express it, leaving the note flat until the next key-on.
        row_len = 44100.0 * 7 / self.HZ
        pts = ((1000 + int(2 * row_len), 0), (1000 + int(3.3 * row_len), 400))
        ev = NoteEvent(sample=1000, ch=2, on=True, note=108, ins=0, vol=-1,
                       e5=0x80, pan=0, end=20000, sweep=pts)
        song = self._song([ev, NoteEvent(sample=20000, ch=2, on=False, note=180,
                                         ins=-1, vol=-1, e5=0x80, pan=0)])
        rows = self._rows(song, 2)
        # A single steady ramp: its rows need no E5 steps, and the trajectory
        # ends *inside* the last ramping row, which is rated over its full span
        # so the ramp lands on the held value at the row end.  The pin must
        # therefore be the note's own fine tune (no write at all): the old
        # double-counted tail term wrote a bogus offset here.
        e5 = [(r, v) for r, cell in sorted(rows.items()) for c, v in cell.fx if c == 0xE5]
        # Only the ramp's rounding residue may be corrected: the old tail term
        # subtracted the travel the last row had already been rated for, which
        # clamped E5 to its limit and left the held note a semitone flat.
        for row, val in e5:
            self.assertLessEqual(abs(val - 0x80), 4, f"row {row}: E5 {val:02X}")


class TestKeyOnOperatorMask(unittest.TestCase):
    """KON is per operator: only a 0->1 bit transition restarts an envelope.

    Drivers write redundant key-ons and per-operator changes while a note
    sounds (Gradius II does both heavily); reading any nonzero mask as a full
    key-on turned every one of those into a spurious retrigger.
    """

    CH = 2

    def _writes(self, onsets, offs=()):
        w = [
            ChipWrite(sample=t, chip="ym2151", chip_id=0, reg=0x08, val=(m << 3) | self.CH)
            for t, m in onsets
        ]
        w += [ChipWrite(sample=t, chip="ym2151", chip_id=0, reg=0x08, val=self.CH) for t in offs]
        return w

    def _analyze(self, writes):
        return analyze(_vgm_with_writes(writes), pcm=False)

    def test_redundant_key_on_is_not_a_note(self):
        song = self._analyze(self._writes([(0, 0xF), (1000, 0xF)], [2000]))
        self.assertEqual(len([e for e in song.events if e.on]), 1)

    def test_partial_operator_change_keeps_the_note(self):
        # 0x78 -> 0x70 releases operator 4 while the other three sustain.
        song = self._analyze(self._writes([(0, 0xF), (1000, 0xE)], [2000]))
        self.assertEqual(len([e for e in song.events if e.on]), 1)
        self.assertEqual(len([e for e in song.events if not e.on]), 1)

    def test_partial_key_on_silences_unkeyed_operator(self):
        writes = [
            ChipWrite(sample=0, chip="ym2151", chip_id=0, reg=0x60 + op * 8 + self.CH, val=tl)
            for op, tl in enumerate((0x10, 0x20, 0x30, 0x40))
        ]
        writes += self._writes([(10, 0xB)], [1000])  # 1011: operator 3 un-keyed
        song = self._analyze(writes)
        ev = [e for e in song.events if e.on][0]
        patch = song.fm_patches[ev.ins]
        self.assertEqual([patch.ops[i].tl for i in range(4)], [0x10, 0x20, 0x7F, 0x40])


class TestChipClockFlags(unittest.TestCase):
    """The rip's OPM clock must reach Furnace (its default is 3.579545 MHz).

    Sega System 16C runs the YM2151 at 4 MHz.  Furnace keeps notes at their
    musical frequency at any clock (baseFreqOff in arcade.cpp), so the flag
    alone cannot move the sounding pitch: the notes carry the clock offset and
    the flag keeps the exported registers identical to the rip's.
    """

    def _song_with_clock(self, clock):
        song = _song(60.0, 1, 1, _vgm_with_writes([]))
        song.vgm.ym2151_clock = clock
        song.events.append(NoteEvent(sample=0, ch=0, on=True, note=60, ins=0,
                                     vol=-1, e5=0x80, pan=0))
        return song

    def test_non_default_clock_is_written(self):
        data = write_fur(self._song_with_clock(4000000), include_pcm=False, include_fm=True)
        self.assertIn(b"customClock=4000000", data)

    def test_default_clock_writes_no_override(self):
        data = write_fur(self._song_with_clock(3579545), include_pcm=False, include_fm=True)
        self.assertNotIn(b"customClock", data)


class TestClockTransposition(unittest.TestCase):
    """12·log2(clock/default) semitones, in 1/64-semitone units."""

    def test_offset_values(self):
        self.assertEqual(opm_pitch_offset(3579545), 0)
        self.assertEqual(opm_pitch_offset(None), 0)
        self.assertEqual(opm_pitch_offset(4000000), 123)  # +1.92 st (Sega 16C)

    def test_shift_carries_the_fraction(self):
        # 60 st + 32/64 st, shifted by +1 st + 59/64 st → 62 st + 27/64 st
        note, e5 = _shift_note_e5(60, 0x80 + 32, 123)
        self.assertEqual(note, 62)
        self.assertEqual(e5, 0x80 + 27)

    def test_song_notes_move_by_the_offset(self):
        def pitch(clock):
            writes = [
                ChipWrite(sample=0, chip="ym2151", chip_id=0, reg=0x28, val=0x4E),
                ChipWrite(sample=0, chip="ym2151", chip_id=0, reg=0x30, val=0x00),
                ChipWrite(sample=1, chip="ym2151", chip_id=0, reg=0x08, val=0x78),
            ]
            vgm = _vgm_with_writes(writes)
            vgm.ym2151_clock = clock
            song = analyze(vgm, pcm=False)
            ev = [e for e in song.events if e.on][0]
            return ev.note * 64 + (ev.e5 - 0x80)

        self.assertEqual(pitch(4000000) - pitch(3579545), 123)

    def test_pcm_notes_are_not_transposed(self):
        """PCM (ch 8-9) is pitched by its own chip clock, not the OPM's."""
        events = [
            NoteEvent(sample=0, ch=0, on=True, note=60, ins=0, vol=-1, e5=0x80, pan=0),
            NoteEvent(sample=0, ch=8, on=True, note=60, ins=0, vol=15, e5=0x80, pan=0, pcm=True),
            NoteEvent(sample=100, ch=9, on=True, note=72, ins=1, vol=15, e5=0x80, pan=0, pcm=True),
        ]
        transpose_opm_events(events, 4000000)
        self.assertEqual((events[0].note, events[0].e5), (61, 0x80 + 59))
        self.assertEqual((events[1].note, events[1].e5), (60, 0x80))
        self.assertEqual((events[2].note, events[2].e5), (72, 0x80))


class TestLoopJump(unittest.TestCase):
    """Bxx sits on the last content row so the loop restarts without a gap."""

    def test_loop_jump_at_last_content_row(self):
        for path in (SF, DTA):
            if not path.is_file():
                self.skipTest("rip missing")
            vgm = load_vgm(path)
            song = analyze(vgm, pcm=True)
            m = parse_fur(write_fur(song, include_pcm=True, include_fm=True))
            row_samples = 44100.0 * m.speed / m.hz
            g = 0
            abs_jump = None
            last_content = None
            for oi in range(m.n_ord):
                rows = m.patterns[(0, m.orders[0][oi])]
                span = m.pat_len
                for r in range(m.pat_len):
                    if any(c == 0x0D for c, _v in rows[r].fx):
                        span = r + 1
                        break
                    if any(c == 0x0B for c, _v in rows[r].fx):
                        abs_jump = g + r
                content = [
                    r for r in range(m.pat_len)
                    if rows[r].note >= 0
                    or any(c not in (0xFFFF, 0) and c != 0x0D and c != 0x0B for c, _v in rows[r].fx)
                ]
                if content:
                    last_content = g + max(content)
                g += span
            name = path.name
            self.assertIsNotNone(abs_jump, f"{name}: no Bxx")
            self.assertLessEqual(abs_jump, last_content + 1,
                                 f"{name}: Bxx at {abs_jump} but content ends at {last_content}")
            loop_rows = (abs_jump + 1) - song.row_of(song.loop_sample)
            expected = vgm.loop_samples / row_samples
            self.assertLessEqual(abs(loop_rows - expected), 2.0,
                                 f"{name}: loop {loop_rows} rows, expected {expected:.1f}")


class TestLeadPitchRoundTrip(unittest.TestCase):
    """Our (note, e5) must address the same chip pitch as the source registers.

    Furnace converts note+e5 back to KC/KF; the key nibbles are "gappy" (3/7/11
    alias 4/8/12 on the chip), so compare the *effective* semitone+fraction.
    """

    INV_NOTE = {0: 0, 1: 1, 2: 2, 3: 3, 4: 5, 5: 6, 6: 7, 7: 9, 8: 10, 9: 11, 10: 13, 11: 14}

    @staticmethod
    def _effective(reg: int) -> int:
        """YM2151 KC register -> effective semitone index (ymfm adjusted code)."""
        block = (reg >> 4) & 7
        nibble = reg & 15
        return block * 12 + (nibble - (nibble >> 2))

    def test_lead_notes_match_source_registers(self):
        if not SF.is_file():
            self.skipTest("rip missing")
        vgm = load_vgm(SF)
        song = analyze(vgm, pcm=True)
        # replay the source KC/KF state
        kc_state = {}
        kf_state = {}
        events = sorted(
            (e for e in song.events if e.on and e.ch in (1, 3)),
            key=lambda e: e.sample,
        )
        by_sample = {}
        for w in vgm.writes:
            if w.chip != "ym2151":
                continue
            if w.reg == 8 and (w.val & 0x78):
                ch = w.val & 7
                by_sample.setdefault((ch, w.sample), (kc_state.get(ch, 0), kf_state.get(ch, 0)))
            elif 0x28 <= w.reg <= 0x2F:
                kc_state[w.reg - 0x28] = w.val
            elif 0x30 <= w.reg <= 0x37:
                kf_state[w.reg - 0x30] = w.val
        checked = 0
        for e in events:
            src = by_sample.get((e.ch, e.sample))
            if src is None:
                continue
            src_kc, src_kf = src
            total = (e.note - 61) * 64 + (e.e5 - 0x80)
            block, rem = divmod(total, 768)
            sem, frac = divmod(rem, 64)
            our_kc = (block << 4) | self.INV_NOTE[sem]
            our_kf = (frac & 63) << 2
            self.assertEqual(self._effective(our_kc), self._effective(src_kc),
                             f"t={e.sample} ch{e.ch} note={e.note} e5={e.e5:02X}")
            self.assertEqual((our_kf >> 2) & 63, (src_kf >> 2) & 63,
                             f"t={e.sample} ch{e.ch} fine fraction")
            checked += 1
        self.assertGreater(checked, 100)


class TestVibratoRamp(unittest.TestCase):
    """FMS/AMS macros must follow the driver's slow sensitivity steps.

    The drivers step the LFO sensitivity once per ~5k samples of sustain, so a
    held note starts steady and drifts over ~1 s.  Converting that step with a
    fixed 60 Hz frame made the vibrato reach full depth in ~0.1 s.
    """

    @staticmethod
    def _source_steps(vgm):
        held = set()
        last = {}
        steps = []
        for w in vgm.writes:
            if w.chip != "ym2151":
                continue
            if w.reg == 8:
                ch = w.val & 7
                if w.val & 0x78:
                    held.add(ch)
                    last.pop(ch, None)
                else:
                    held.discard(ch)
            elif 0x38 <= w.reg <= 0x3F and (w.reg - 0x38) in held:
                ch = w.reg - 0x38
                if ch in last:
                    steps.append(w.sample - last[ch])
                last[ch] = w.sample
        return sorted(steps)

    def test_macro_step_matches_source_spacing(self):
        if not ATTACK.is_file():
            self.skipTest("rip missing")
        vgm = load_vgm(ATTACK)
        song = analyze(vgm, pcm=True)
        steps = self._source_steps(vgm)
        self.assertGreater(len(steps), 10)
        src = steps[len(steps) // 2]
        spt = VGM_RATE / song.hz
        macros = [p for p in song.fm_patches if p.fms_macro and len(p.fms_macro) >= 4]
        self.assertTrue(macros, "no vibrato instruments captured")
        macro_steps = sorted(p.macro_speed * spt for p in macros)
        for step in macro_steps:
            # no macro may step faster than ~45 ms or slower than ~0.3 s
            self.assertGreater(step, 2000, f"macro step {step:.0f} is near-instant")
            self.assertLess(step, 13000, f"macro step {step:.0f} too slow")
        # the typical macro step is the source's typical step
        med = macro_steps[len(macro_steps) // 2]
        self.assertLess(abs(med - src) / src, 0.35, f"median macro step {med:.0f} vs {src}")
        # and full-depth ramps take real time ("the alcohol is absorbed over time")
        for p in macros:
            if max(p.fms_macro) < 4:
                continue  # a short note only got part of the ramp
            dur = (len(p.fms_macro) - 1) * p.macro_speed * spt
            self.assertGreater(dur / VGM_RATE, 0.35, f"ramp {dur:.0f} samples is too quick")


if __name__ == "__main__":
    unittest.main()
