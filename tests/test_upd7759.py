"""The uPD7759 dump, on synthetic ROMs. No MAME or game ROM needed."""

from __future__ import annotations

import json
import sys
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vgm2151fur.upd7759 import (  # noqa: E402
    decode_phrase, dump_rom, phrase_address, phrase_count, render_levels, render_wav_bytes,
)


def rom_three_phrases() -> bytes:
    """Header + three vectors + data. See the test docstrings for the layout."""
    data = bytearray(0x20)
    data[0:5] = bytes([0x02, 0x5A, 0xA5, 0x69, 0x55])
    data[5:7] = (6).to_bytes(2, "big")    # phrase 0 -> 0x0D
    data[7:9] = (9).to_bytes(2, "big")    # phrase 1 -> 0x13
    data[9:11] = (12).to_bytes(2, "big")  # phrase 2 -> 0x19
    # phrase 0: rate 1, 4 nibbles 1 2 3 4, end
    data[0x0D:0x12] = bytes([0x80, 0x03, 0x12, 0x34, 0x00])
    # phrase 1: rate 2, 2 nibbles A B, end (preceded by a silence block)
    data[0x13:0x18] = bytes([0x01, 0x81, 0x01, 0xAB, 0x00])
    # phrase 2: rate 2, 3 nibbles (odd: the last low nibble is dropped)
    data[0x19:0x1E] = bytes([0x81, 0x02, 0x12, 0x34, 0x00])
    return bytes(data)


def rom_repeat() -> bytes:
    """One phrase: 0xC1 repeats the following block twice."""
    data = bytearray(0x12)
    data[0:5] = bytes([0x00, 0x5A, 0xA5, 0x69, 0x55])
    data[5:7] = (5).to_bytes(2, "big")  # -> 0x0B
    data[0x0B:0x10] = bytes([0xC1, 0x80, 0x01, 0xAB, 0x00])
    return bytes(data)


class TestTable(unittest.TestCase):
    def test_header_and_vectors(self):
        rom = rom_three_phrases()
        self.assertEqual(phrase_count(rom), 3)
        self.assertEqual(phrase_address(rom, 0), 0x0D)
        self.assertEqual(phrase_address(rom, 1), 0x13)
        self.assertEqual(phrase_address(rom, 2), 0x19)

    def test_bad_magic(self):
        with self.assertRaises(ValueError):
            phrase_count(bytes([0x00, 0x00, 0x00, 0x00, 0x00]))

    def test_index_range(self):
        rom = rom_three_phrases()
        with self.assertRaises(IndexError):
            phrase_address(rom, 3)


class TestDecode(unittest.TestCase):
    def test_nibble_block(self):
        # step_table[0] first nibbles: 1 -> 0, 2 -> +1, 3 -> +2, 4 -> +3
        # each block header costs 36 clocks (held level), the count byte 36 more
        phrase = decode_phrase(rom_three_phrases(), 0)
        self.assertEqual(
            phrase.events,
            [(0, 36), (0, 36), (0, 4), (1, 4), (3, 4), (6, 4), (0, 1024)],
        )
        self.assertEqual(phrase.end, 0x12)
        self.assertEqual(phrase.rates, {1})
        self.assertAlmostEqual(phrase.seconds(), 1112 / 640000, places=9)

    def test_silence_and_negative_steps(self):
        phrase = decode_phrase(rom_three_phrases(), 1)
        self.assertEqual(
            phrase.events,
            [(0, 2048), (0, 36), (0, 36), (-1, 8), (-3, 8), (0, 1024)],
        )

    def test_odd_nibble_count_drops_low_nibble(self):
        phrase = decode_phrase(rom_three_phrases(), 2)
        self.assertEqual(
            phrase.events,
            [(0, 36), (0, 36), (0, 8), (1, 8), (3, 8), (0, 1024)],
        )
        self.assertEqual(phrase.rates, {2})

    def test_repeat_block_plays_twice(self):
        phrase = decode_phrase(rom_repeat(), 0)
        self.assertEqual(
            phrase.events,
            [(0, 36), (0, 36), (0, 36), (-1, 4), (-3, 4),
             (-3, 36), (-3, 36), (-4, 4), (-6, 4), (0, 1024)],
        )
        self.assertEqual(phrase.end, 0x10)


class TestRender(unittest.TestCase):
    def test_levels_interpolate_between_event_points(self):
        phrase = decode_phrase(rom_three_phrases(), 0)
        levels = render_levels(phrase, 160000)  # one output frame per clock/4
        self.assertEqual(len(levels), 278)
        # event points hold levels 0 (x3), 1, 3, 6, then the final silence
        self.assertAlmostEqual(levels[0], 0.0, places=6)
        self.assertAlmostEqual(levels[18], 0.0, places=6)
        self.assertAlmostEqual(levels[19], 0.5, places=6)
        self.assertAlmostEqual(levels[20], 2.0, places=6)
        self.assertAlmostEqual(levels[21], 4.5, places=6)
        self.assertGreater(max(levels), 5.9)  # peak sits between output samples
        self.assertLess(levels[-1], 0.1)  # ramps to silence, no click

    def test_wav_bytes_are_sixteen_bit(self):
        phrase = decode_phrase(rom_three_phrases(), 0)
        pcm = render_wav_bytes(phrase, 44100)
        self.assertEqual(len(pcm) % 2, 0)
        self.assertGreater(len(pcm), 0)


class TestDump(unittest.TestCase):
    def test_dump_writes_wavs_and_index(self):
        rom = rom_three_phrases()
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "upd"
            index = dump_rom(rom, out, source="synthetic", rate=44100, code_base=0x70)
            self.assertEqual(len(index["items"]), 3)
            self.assertEqual(index["items"][0]["code"], "0x70")
            self.assertEqual(index["gain"], 1.0)
            with wave.open(str(out / "voice_00_code70.wav"), "rb") as w:
                self.assertEqual(w.getframerate(), 44100)
                self.assertEqual(w.getnchannels(), 1)
                self.assertEqual(w.getsampwidth(), 2)
            written = json.loads((out / "upd7759_index.json").read_text(encoding="utf-8"))
            self.assertEqual(written["phrases"], 3)
            self.assertEqual([item["index"] for item in written["items"]], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
