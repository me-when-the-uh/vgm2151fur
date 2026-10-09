"""Loop finding: the downbeat, a few rows before the commands that play it.

The synthetic grids never render. The fixture checks run the finder over the
source rips in `VGM_collection` and skip when a pack is not on disk.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.analyze import NoteEvent, Song, analyze  # noqa: E402
from vgm2151fur.furio import parse_fur  # noqa: E402
from vgm2151fur.furwrite import write_fur  # noqa: E402
from vgm2151fur.loopfind import (  # noqa: E402
    _exact_rows,
    _seat_cost,
    _seat_seam,
    apply_loop,
    find_loop,
    row_to_sample,
)
from vgm2151fur.vgm import VgmFile, load_vgm  # noqa: E402

RIP = Path(__file__).resolve().parents[2]
COLLECTION = RIP / "VGM_collection"
CLOSE = COLLECTION / "Block_Hole_(Arcade)" / "05 The Closing Game In The Universe (BGM 3).vgz"
SHOOT = COLLECTION / "Gradius II AC Rip" / "06 A Shooting Star (Air Battle 2).vgm"
QUARTH = COLLECTION / "Block_Hole_(Arcade)" / "03 The Theme From Quarth (BGM 1).vgz"
HAWK = COLLECTION / "newest" / "Metal_Hawk_(Namco_System_2)" / "03 Game BGM 1.vgz"
MAP = COLLECTION / "newest" / "Metal_Hawk_(Namco_System_2)" / "02 Map Mode A.vgz"

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
        # Pulse 0 of the cell, then the row before those commands.
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

    def test_phrase_ahead_of_the_marker_is_not_a_seam(self):
        # The head of the loop repeats 96 rows in, an inner repetition. The
        # loop keeps the length the VGM gave it, so a copy that close is not
        # the seam the marker is missing; the marker stays where it is.
        notes = [(row, 0, 40 + row // 3) for row in range(0, 48, 3)]
        notes += [(48, 0, 60), (48, 1, 64), (48, 2, 67)]
        notes += [(48 + i * 2, 3, 72) for i in range(24)]
        notes += [(144, 0, 60), (144, 1, 64), (144, 2, 67)]
        notes += [(144 + i * 2, 3, 72) for i in range(24)]
        notes += [(100 + i * 4, 1, 50 + i) for i in range(35)]
        self.assertIsNone(find_loop(notes, loop_row=96, end_row=239, forward_rows=64))

    def test_phrase_behind_the_marker_becomes_the_loop(self):
        # The body starts 48 rows in and the marker sits inside it. The head
        # of the body comes back at the end of the track, so the loop has to
        # open at the body, 87 rows behind the marker, and keep its length.
        notes = [(row, 0, 40 + row // 3) for row in range(0, 48, 3)]
        notes += [(48, 0, 60), (48, 2, 67)]
        notes += [(48 + j * 4, 1, pitch) for j, pitch in enumerate(range(50, 98))]
        notes += [(240, 0, 60), (240, 2, 67)]
        notes += [(240 + j * 4, 1, pitch) for j, pitch in enumerate(range(50, 71))]
        fix = find_loop(notes, loop_row=132, end_row=320, forward_rows=128)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.start_row, 47)
        self.assertEqual(fix.end_row, 238)

    def test_marker_one_row_after_the_chord_lands_before_the_chord(self):
        notes, loop_row, end_row = _bar(loop_pulse=0)
        # The planted row is the downbeat. Move the marker one row later,
        # onto a row with no note.
        fix = find_loop(notes, loop_row=loop_row + 1, end_row=end_row + 1)
        self.assertIsNotNone(fix)
        self.assertEqual(fix.shift_pulses, 0)
        self.assertEqual(fix.start_row, loop_row - 1)
        self.assertEqual(fix.end_row - fix.start_row, end_row - loop_row)


class TestSeatSeam(unittest.TestCase):
    """The cut moves only to a neighbour whose rows across the seam match.

    The rows pair up `loop length` apart whatever the seat is, so a move
    never changes which rows face each other - it changes which pairs sit
    right after the jump. Only an exact window earns that move.
    """

    @staticmethod
    def _content(strays: int = 0, span: int = 40):
        # Content repeating every `span` rows: row x copies to x + span.
        # `strays` extra notes at the window start have no copy in the tail.
        notes = [(row, 0, 48 + (row % span) // 4) for row in range(100, 180) if row % 4 == 0]
        notes += [(row, 1, 60 + row) for row in range(100, 100 + strays)]
        return _exact_rows(notes)

    def test_the_cost_counts_voices_that_miss_across_the_seam(self):
        exact = self._content()
        self.assertEqual(_seat_cost(exact, 100, 139), 0)
        strayed = self._content(strays=1)
        self.assertEqual(_seat_cost(strayed, 100, 139), 1)

    def test_a_seat_with_an_exact_window_moves_with_its_end(self):
        # Pairs (100, 140) and (101, 141) miss; two rows later every pair
        # in the window is exact, so the cut moves and the end follows.
        exact = self._content(strays=2)
        self.assertEqual(_seat_seam(exact, 100, 139, 0, 10_000, 176), (102, 141))

    def test_a_thin_improvement_leaves_the_seat_alone(self):
        # One stray still faces the window from two rows later. A thinner
        # mismatch is the tail itself, not the seat, and moving would only
        # drag the wobble along.
        exact = self._content(strays=3)
        self.assertEqual(_seat_seam(exact, 100, 139, 0, 10_000, 176), (100, 139))

    def test_no_tail_past_the_jump_leaves_the_seat_alone(self):
        # The window has to fit inside the log. Comparing against the end
        # of the track proves nothing, so no move is made on it.
        exact = self._content(strays=2)
        self.assertEqual(_seat_seam(exact, 100, 139, 0, 10_000, 150), (100, 139))

    def test_the_end_never_leaves_the_log(self):
        # The winning seat would put the jump past the last content row.
        exact = self._content(strays=2)
        self.assertEqual(_seat_seam(exact, 100, 139, 0, 140, 176), (100, 139))


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


class TestFixtureLoops(unittest.TestCase):
    """The finder over the real rips: what a conversion does to each loop."""

    def _loop(self, src: Path):
        song = analyze(load_vgm(src), speed=None, pcm=True)
        return song, apply_loop(song)

    def test_closing_game_keeps_the_marker(self):
        # The marker sits on the downbeat and the one-row cut would land on
        # the row before it, where no command sits. The loop stays where the
        # rip put it.
        if not CLOSE.is_file():
            self.skipTest("source rip missing")
        song, text = self._loop(CLOSE)
        self.assertEqual(text, "loop kept")
        self.assertEqual(song.row_of(song.loop_sample), 158)

    def test_shooting_star_is_not_pulled_into_the_pickup(self):
        if not SHOOT.is_file():
            self.skipTest("source rip missing")
        song, text = self._loop(SHOOT)
        self.assertEqual(text, "loop kept")
        self.assertEqual(song.row_of(song.loop_sample), 388)

    def test_quarth_moves_back_one_pulse(self):
        if not QUARTH.is_file():
            self.skipTest("source rip missing")
        song, text = self._loop(QUARTH)
        self.assertEqual(text, "loop -1/8 (-17 rows)")
        self.assertEqual(song.row_of(song.loop_sample), 33)
        # The wrap sits one declared loop length after the new start. The
        # shorter last-content end restarted the loop two rows early and
        # put the copy of the head's chord on the jump row.
        self.assertEqual(song.row_of(song.loop_end_sample), 3627)

    def test_game_bgm1_opens_on_the_sample_chord(self):
        # The VGM marker sits inside the first loop phrase. The phrase's own
        # first command is the sample chord at row 199; the loop opens one
        # row before it and keeps the rip's 1:58 length.
        if not HAWK.is_file():
            self.skipTest("source rip missing")
        song, text = self._loop(HAWK)
        self.assertEqual(text, "loop 158 rows early")
        self.assertEqual(song.row_of(song.loop_sample), 199)
        self.assertEqual(song.row_of(song.loop_end_sample), 7405)

    def test_map_mode_a_opens_on_the_phrase_head(self):
        # The marker sits inside the phrase; the loop opens at the phrase
        # head, row 102, and keeps the rip's 0:38 length. The riff 1921 rows
        # in is an inner repetition and is not followed.
        if not MAP.is_file():
            self.skipTest("source rip missing")
        song, text = self._loop(MAP)
        self.assertEqual(text, "loop 124 rows early")
        self.assertEqual(song.row_of(song.loop_sample), 102)
        self.assertEqual(song.row_of(song.loop_end_sample), 2405)


if __name__ == "__main__":
    unittest.main()
