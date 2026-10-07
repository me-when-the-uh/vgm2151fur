"""Loop finding: the downbeat, one row before the commands that play it.

The synthetic grids never render. The fixture checks read .fur files that
are already converted and skip when those files are not on disk.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.analyze import NoteEvent, Song  # noqa: E402
from vgm2151fur.furio import load_fur_bytes, parse_fur  # noqa: E402
from vgm2151fur.furwrite import write_fur  # noqa: E402
from vgm2151fur.loopfind import apply_loop, find_loop, row_to_sample  # noqa: E402
from vgm2151fur.vgm import VgmFile  # noqa: E402

RIP = Path(__file__).resolve().parents[2]
CLOSE = RIP / "output" / "block-hole" / "fur" / "05 The Closing Game In The Universe (BGM 3).fur"
SHOOT = RIP / "output" / "other" / "gradius2" / "fur" / "06 A Shooting Star (Air Battle 2).fur"
QUARTH = RIP / "output" / "block-hole" / "fur" / "03 The Theme From Quarth (BGM 1).fur"
HAWK = RIP / "output" / "metal-hawk" / "fur" / "03 Game BGM 1.fur"
MAP = RIP / "output" / "metal-hawk" / "fur" / "02 Map Mode A.fur"

NOTE_OFF = 180


def _vgm():
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x151, total_samples=1,
        loop_samples=0, loop_file_pos=None, loop_sample=None,
        ym2151_clock=3579545, k007232_clock=3579545, gd3={}, roms=[], writes=[],
    )


def _event(sample: int, ch: int, pitch: int) -> NoteEvent:
    return NoteEvent(
        sample=sample, ch=ch, on=True, note=pitch, ins=0, vol=-1, e5=0x80, pan=0,
    )


def _bar(loop_pulse: int, *, pulses: int = 8, step: int = 4, bars: int = 4, intro: int = 1):
    """A repeating cell. Pulse 0 is the downbeat (three voices); the lead continues.

    Returns notes, the planted loop row, and the planted end row.
    """
    origin = intro * pulses * step
    notes = []
    total = (intro + bars) * pulses
    for pulse in range(total):
        row = pulse * step
        lead = 60 if pulse % pulses == 0 else 64 + (pulse % pulses)
        notes.append((row, 0, lead))
        if pulse % pulses == 0:
            notes.append((row, 1, 48))
            notes.append((row, 2, 55))
        elif pulse % pulses == 4:
            notes.append((row, 1, 50))
    loop_row = origin + loop_pulse * step
    length = bars * pulses * step
    end_row = loop_row + length - 1
    # A note on the end row so the length is real content.
    if not any(row == end_row for row, _ch, _pitch in notes):
        notes.append((end_row, 0, 60))
    return notes, loop_row, end_row


class TestFindLoop(unittest.TestCase):
    def test_mid_bar_marker_moves_to_the_downbeat(self):
        notes, loop_row, end_row = _bar(loop_pulse=1)
        fix = find_loop(notes, loop_row=loop_row, end_row=end_row)
        self.assertIsNotNone(fix)
        # Pulse 0 of the cell, then one row before those commands.
        self.assertEqual(fix.shift_pulses, -1)
        self.assertEqual(fix.period, 8)
        self.assertEqual(fix.start_row, loop_row - fix.step_rows - 1)
        self.assertEqual(fix.end_row - fix.start_row, end_row - loop_row)

    def test_downbeat_marker_stays_on_the_beat(self):
        notes, loop_row, end_row = _bar(loop_pulse=0)
        fix = find_loop(notes, loop_row=loop_row, end_row=end_row)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.shift_pulses, 0)
        self.assertEqual(fix.start_row, loop_row - 1)
        self.assertEqual(fix.end_row - fix.start_row, end_row - loop_row)

    def test_pickup_into_a_chord_is_left_alone(self):
        notes = []
        for i, pitch in enumerate(range(40, 72)):
            notes.append((i * 4, 0, pitch))
        chord = 40 * 4
        for ch, pitch in enumerate((60, 64, 67, 48)):
            notes.append((chord, ch, pitch))
        for i in range(8):
            notes.append((chord + 8 + i * 5, 1, 70 - i))
        fix = find_loop(notes, loop_row=chord, end_row=chord + 48)
        self.assertIsNone(fix)

    def test_irregular_gaps_are_left_alone(self):
        notes = [(row, 0, 60) for row in (0, 3, 7, 8, 20, 21, 50, 80, 81, 140, 200)]
        self.assertIsNone(find_loop(notes, loop_row=50, end_row=200))

    def test_empty_row_without_a_clock_stays(self):
        # No regular gap. The row before the marker happens to be a chord.
        # That is not enough to move the VGM marker.
        notes = [
            (0, 0, 60), (0, 1, 64),
            (5, 0, 62), (9, 1, 65), (14, 0, 67),
            (20, 0, 60), (20, 1, 64),
        ]
        self.assertIsNone(find_loop(notes, loop_row=21, end_row=40))

    def test_a_bar_still_wins_when_a_forward_window_is_open(self):
        # The phrase start is one pulse behind the marker. The four-second
        # search must not pull that loop forward.
        notes, loop_row, end_row = _bar(loop_pulse=1)
        fix = find_loop(notes, loop_row=loop_row, end_row=end_row, forward_rows=200)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.shift_pulses, -1)
        self.assertEqual(fix.start_row, loop_row - fix.step_rows - 1)

    def test_phrase_ahead_of_the_marker_becomes_the_loop(self):
        # The marker sits in a fill. The repeating phrase starts 12 rows
        # later, and the end of the track is that phrase coming back.
        notes = [(row, 0, 40 + row // 3) for row in range(0, 36, 3)]
        for rep in range(5):
            base = 48 + rep * 48
            notes.extend((base, ch, pitch) for ch, pitch in ((0, 60), (1, 64), (2, 67)))
            notes.extend((base + offset, 3, 72) for offset in range(0, 40, 2))
            notes.extend((base + offset, 0, pitch) for offset, pitch in (
                (6, 62), (14, 65), (22, 69), (30, 64), (38, 67),
            ))
        fix = find_loop(notes, loop_row=36, end_row=280, forward_rows=40)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.start_row, 47)
        self.assertEqual(fix.end_row, 47 + (240 - 48) - 1)
        self.assertIsNone(find_loop(notes, loop_row=36, end_row=280))

    def test_marker_one_row_after_the_chord_lands_before_the_chord(self):
        notes, loop_row, end_row = _bar(loop_pulse=0)
        # The planted row is the downbeat. Move the marker one row later,
        # onto a row with no note.
        fix = find_loop(notes, loop_row=loop_row + 1, end_row=end_row + 1)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.shift_pulses, 0)
        self.assertEqual(fix.start_row, loop_row - 1)
        self.assertEqual(fix.end_row - fix.start_row, end_row - loop_row)


class TestApplyAndWrite(unittest.TestCase):
    def test_adjusted_end_is_where_bxx_sits(self):
        # speed 1 at 60 Hz is 735 samples/row. Rows 0, 16 and 40 carry notes.
        events = [_event(row * 735, 0, 60) for row in (0, 16, 40)]
        song = Song(
            vgm=_vgm(), t0=0, loop_sample=16 * 735, speed=1, hz=60.0,
            fm_patches=[], pcm_samples=[], events=events,
            loop_end_sample=40 * 735,
        )
        mod = parse_fur(write_fur(song, include_pcm=False, include_fm=True))
        self.assertEqual(mod.orders[0][0:2], [0, 1])
        # Order 1 starts at row 16. Bxx is on absolute row 40.
        rows = mod.patterns[(0, mod.orders[0][1])]
        bxx = [i for i, row in enumerate(rows) if any(c == 0x0B for c, _v in row.fx)]
        self.assertEqual(bxx, [40 - 16])

    def test_row_to_sample_round_trips(self):
        song = Song(
            vgm=_vgm(), t0=100, loop_sample=None, speed=4, hz=120.0,
            fm_patches=[], pcm_samples=[], events=[],
        )
        for row in (0, 1, 17, 158):
            self.assertEqual(song.row_of(row_to_sample(song, row)), row)

    def test_apply_loop_sets_both_ends(self):
        notes, loop_row, end_row = _bar(loop_pulse=1, step=4)
        song = Song(
            vgm=_vgm(), t0=0, loop_sample=loop_row * 735, speed=1, hz=60.0,
            fm_patches=[], pcm_samples=[],
            events=[_event(row * 735, ch, pitch) for row, ch, pitch in notes],
        )
        # The planted end is the last note only if we put one there. apply
        # uses the last event row, so drop anything past end_row and make
        # sure something sits on it.
        song.events = [ev for ev in song.events if song.row_of(ev.sample) <= end_row]
        if song.row_of(song.events[-1].sample) != end_row:
            song.events.append(_event(end_row * 735, 0, 60))
        text = apply_loop(song)
        self.assertIsNotNone(text)
        self.assertNotEqual(text, "loop kept")
        self.assertEqual(song.row_of(song.loop_sample), loop_row - 4 - 1)
        self.assertEqual(
            song.row_of(song.loop_end_sample) - song.row_of(song.loop_sample),
            end_row - loop_row,
        )
        self.assertTrue(song.warnings)


def _fur_loop(path: Path):
    mod = parse_fur(load_fur_bytes(path))
    pat_len = mod.pat_len

    def span(oi):
        spans = []
        for ch in range(mod.n_ch):
            rows = mod.patterns.get((ch, mod.orders[ch][oi]), [])
            width = len(rows) if rows else pat_len
            for i, row in enumerate(rows):
                if any(c == 0x0D for c, _v in row.fx):
                    width = min(width, i + 1)
            spans.append(width if rows else 0)
        return max(spans) if spans else pat_len

    bases, total = [], 0
    spans = []
    for oi in range(mod.n_ord):
        bases.append(total)
        width = span(oi)
        spans.append(width)
        total += width
    end = target = None
    for oi in range(mod.n_ord):
        for ch in range(mod.n_ch):
            rows = mod.patterns.get((ch, mod.orders[ch][oi]), [])
            for i, row in enumerate(rows[:spans[oi]]):
                for cmd, val in row.fx:
                    if cmd == 0x0B:
                        end, target = bases[oi] + i, val
    notes = []
    for oi in range(mod.n_ord):
        for ch in range(mod.n_ch):
            rows = mod.patterns.get((ch, mod.orders[ch][oi]), [])
            for i, row in enumerate(rows[:spans[oi]]):
                if 0 <= row.note < NOTE_OFF:
                    notes.append((bases[oi] + i, ch, row.note))
    return notes, bases[target], end


def _forward_rows(path: Path) -> int:
    mod = parse_fur(load_fur_bytes(path))
    return int(round(4.0 * mod.hz / mod.speed))


class TestFixtureLoops(unittest.TestCase):
    def test_closing_game_stays_on_its_beat(self):
        if not CLOSE.is_file():
            self.skipTest("fur missing")
        notes, loop_row, end_row = _fur_loop(CLOSE)
        fix = find_loop(
            notes, loop_row=loop_row, end_row=end_row,
            forward_rows=_forward_rows(CLOSE),
        )
        self.assertIsNotNone(fix)
        self.assertEqual(fix.shift_pulses, 0)
        self.assertEqual(fix.start_row, loop_row - 1)
        self.assertEqual(fix.end_row - fix.start_row, end_row - loop_row)

    def test_shooting_star_is_not_pulled_into_the_pickup(self):
        if not SHOOT.is_file():
            self.skipTest("fur missing")
        notes, loop_row, end_row = _fur_loop(SHOOT)
        fix = find_loop(
            notes, loop_row=loop_row, end_row=end_row,
            forward_rows=_forward_rows(SHOOT),
        )
        if fix is not None:
            self.assertEqual(fix.shift_pulses, 0)
            self.assertGreaterEqual(fix.start_row, loop_row - 2)

    def test_quarth_moves_back_one_pulse(self):
        if not QUARTH.is_file():
            self.skipTest("fur missing")
        notes, loop_row, end_row = _fur_loop(QUARTH)
        fix = find_loop(
            notes, loop_row=loop_row, end_row=end_row,
            forward_rows=_forward_rows(QUARTH),
        )
        self.assertIsNotNone(fix)
        self.assertEqual(fix.period, 8)
        self.assertEqual(fix.shift_pulses, -1)
        self.assertEqual(fix.start_row, loop_row - fix.step_rows - 1)
        self.assertEqual(fix.end_row - fix.start_row, end_row - loop_row)

    def test_game_bgm1_opens_on_the_sample_chord(self):
        # Order 1D line 136 is the row before this chord returns. The copy
        # inside the four seconds after the VGM marker is absolute row 584.
        if not HAWK.is_file():
            self.skipTest("fur missing")
        notes, loop_row, end_row = _fur_loop(HAWK)
        fix = find_loop(
            notes, loop_row=loop_row, end_row=end_row,
            forward_rows=_forward_rows(HAWK),
        )
        self.assertIsNotNone(fix)
        self.assertEqual(fix.start_row, 583)
        self.assertEqual(fix.end_row, 7404)
        self.assertEqual(fix.end_row - fix.start_row, 7406 - 584 - 1)

    def test_map_mode_a_opens_on_the_riff(self):
        # The riff that comes back at the end begins just past four seconds.
        if not MAP.is_file():
            self.skipTest("fur missing")
        notes, loop_row, end_row = _fur_loop(MAP)
        fix = find_loop(
            notes, loop_row=loop_row, end_row=end_row,
            forward_rows=_forward_rows(MAP),
        )
        self.assertIsNotNone(fix)
        self.assertEqual(fix.start_row, 486)
        self.assertEqual(fix.end_row, 2406)


if __name__ == "__main__":
    unittest.main()
