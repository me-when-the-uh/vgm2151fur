"""Left-to-right macro merging: a truncated copy shares the longer instrument.

A short note often produces a macro that is the prefix of the same note held
longer. The next key-on comes before the extra tail is heard, so the longer
instrument can play both. The merge only applies when *all* macros agree from
the beginning and the non-macro timbre is identical.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.analyze import (  # noqa: E402
    NoteEvent, ToneIns, _merge_ay_volume_offsets, _merge_fm_prefix_macros,
    _merge_tone_prefixes,
)
from vgm2151fur.fm import FMPatch, OpRegs  # noqa: E402


def _note(ins: int) -> NoteEvent:
    return NoteEvent(
        sample=ins * 1000, ch=0, on=True, note=60, ins=ins, vol=127,
        e5=0x80, pan=0, end=ins * 1000 + 500,
    )


def _fm(macro, speed: int = 1) -> FMPatch:
    return FMPatch(
        alg=7, fb=0, ams=0, pms=0,
        ops=tuple(OpRegs() for _ in range(4)),  # type: ignore[arg-type]
        op_mul_macro=((2, macro, speed),),
    )


class TestFMPrefixMerge(unittest.TestCase):
    def test_a_prefix_copy_shares_the_longer_macro(self):
        patches = [_fm((0, 1)), _fm((0, 1, 2, 3))]
        events = [_note(0), _note(1)]
        self.assertEqual(_merge_fm_prefix_macros(events, patches, 1000), 1)
        self.assertEqual(len(patches), 1)
        self.assertEqual([e.ins for e in events], [0, 0])
        self.assertEqual(patches[0].op_mul_macro[0][1], (0, 1, 2, 3))

    def test_a_different_speed_or_middle_value_does_not_merge(self):
        patches = [_fm((0, 1), 1), _fm((0, 1, 2), 2)]
        events = [_note(0), _note(1)]
        self.assertEqual(_merge_fm_prefix_macros(events, patches, 1000), 0)
        self.assertEqual(len(patches), 2)

        patches = [_fm((0, 1)), _fm((0, 9, 1))]
        events = [_note(0), _note(1)]
        self.assertEqual(_merge_fm_prefix_macros(events, patches, 1000), 0)
        self.assertEqual(len(patches), 2)

    def test_a_different_timbre_does_not_merge(self):
        short = _fm((0, 1))
        long = _fm((0, 1, 2))
        long.alg = 6
        patches = [short, long]
        events = [_note(0), _note(1)]
        self.assertEqual(_merge_fm_prefix_macros(events, patches, 1000), 0)
        self.assertEqual(len(patches), 2)


def _ay_note(ins: int, vol: int = 15) -> NoteEvent:
    return NoteEvent(
        sample=ins * 1000, ch=16 + ins, on=True, note=60, ins=ins,
        vol=vol, e5=0x80, pan=0, end=ins * 1000 + 500,
        tone_chip=2,
    )


class TestAyVolumeOffsetMerge(unittest.TestCase):
    def test_offsets_up_to_four_share_the_loudest_macro(self):
        bucket = [
            ToneIns(wave=1, vol=(10, 9, 8)),
            ToneIns(wave=1, vol=(12, 11, 10)),
            ToneIns(wave=1, vol=(14, 13, 12)),
        ]
        notes = [(_ay_note(0), 0), (_ay_note(1), 1), (_ay_note(2), 2)]
        mapping = _merge_ay_volume_offsets(bucket, notes)
        self.assertEqual(len(bucket), 1)
        self.assertEqual(mapping[0], mapping[1])
        self.assertEqual(mapping[0], mapping[2])
        self.assertEqual(bucket[0].vol, (14, 13, 12))
        # Each quieter note loses exactly the macro offset on its column:
        # effective = macro + column - 15 is unchanged.
        self.assertEqual([ev.vol for ev, _i in notes], [11, 13, 15])

    def test_a_difference_over_four_stays_separate(self):
        bucket = [
            ToneIns(wave=1, vol=(10, 9)),
            ToneIns(wave=1, vol=(15, 14)),
        ]
        notes = [(_ay_note(0), 0), (_ay_note(1), 1)]
        mapping = _merge_ay_volume_offsets(bucket, notes)
        self.assertEqual(len(bucket), 2)
        self.assertNotEqual(mapping[0], mapping[1])

    def test_a_nonconstant_difference_stays_separate(self):
        bucket = [
            ToneIns(wave=1, vol=(10, 9)),
            ToneIns(wave=1, vol=(10, 8)),
        ]
        notes = [(_ay_note(0), 0), (_ay_note(1), 1)]
        mapping = _merge_ay_volume_offsets(bucket, notes)
        self.assertEqual(len(bucket), 2)
        self.assertNotEqual(mapping[0], mapping[1])

    def test_another_macro_must_match(self):
        bucket = [
            ToneIns(wave=1, vol=(10, 9), pitch=(0,)),
            ToneIns(wave=1, vol=(11, 10), pitch=(1,)),
        ]
        notes = [(_ay_note(0), 0), (_ay_note(1), 1)]
        mapping = _merge_ay_volume_offsets(bucket, notes)
        self.assertEqual(len(bucket), 2)
        self.assertNotEqual(mapping[0], mapping[1])


class TestTonePrefixMerge(unittest.TestCase):
    def test_pitch_and_volume_prefixes_share_the_longer_instrument(self):
        bucket = [
            ToneIns(wave=1, pitch=(10, 20), vol=(5, 6)),
            ToneIns(wave=1, pitch=(10, 20, 30), vol=(5, 6, 7)),
            ToneIns(wave=2, pitch=(10, 20), vol=(5, 6)),
        ]
        mapping = _merge_tone_prefixes(bucket)
        self.assertEqual(len(bucket), 2)
        self.assertEqual(mapping[0], mapping[1])
        self.assertNotEqual(mapping[2], mapping[0])
        kept = bucket[mapping[0]]
        self.assertEqual(kept.pitch, (10, 20, 30))
        self.assertEqual(kept.vol, (5, 6, 7))

    def test_every_macro_must_match(self):
        bucket = [
            ToneIns(wave=1, pitch=(10,), vol=(5, 6)),
            ToneIns(wave=1, pitch=(10, 20), vol=(5, 8)),
        ]
        mapping = _merge_tone_prefixes(bucket)
        self.assertEqual(len(bucket), 2)
        self.assertNotEqual(mapping[0], mapping[1])

    def test_a_different_macro_speed_does_not_merge(self):
        bucket = [
            ToneIns(wave=1, pitch=(10,), pitch_speed=1),
            ToneIns(wave=1, pitch=(10, 20), pitch_speed=2),
        ]
        mapping = _merge_tone_prefixes(bucket)
        self.assertEqual(len(bucket), 2)
        self.assertNotEqual(mapping[0], mapping[1])


if __name__ == "__main__":
    unittest.main()
