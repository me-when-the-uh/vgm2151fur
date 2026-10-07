"""Chip volume editing on .fur files: both value copies, and the caps."""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _synth import note_song  # noqa: E402
from vgm2151fur.analyze import analyze  # noqa: E402
from vgm2151fur.edit import (  # noqa: E402
    MAX_INFO_VOLUME, FurEditError, _read_info, read_chips, set_chip_volume,
)
from vgm2151fur.furwrite import CHIP_YM2151, FUR_MAGIC, write_fur  # noqa: E402

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_edit"
TMP.mkdir(exist_ok=True)


def _one_chip_fur() -> bytes:
    song = analyze(note_song(key_on=60000, key_off=160000), pcm=False)
    return write_fur(song, include_pcm=False, include_fm=True)


def _info_parts(data: bytes) -> tuple[int, int]:
    _chips, vol_off, triples = _read_info(data)
    return vol_off, triples


class TestReadChips(unittest.TestCase):
    def test_lists_the_written_chips(self):
        chips = read_chips(_one_chip_fur())
        self.assertEqual([c.id for c in chips], [CHIP_YM2151])
        self.assertAlmostEqual(chips[0].volume, 1.0, places=3)

    def test_rejects_a_foreign_buffer(self):
        with self.assertRaises((FurEditError, ValueError)):
            read_chips(b"not a fur at all")


class TestSetChipVolume(unittest.TestCase):
    def test_factor_scales_the_byte_and_the_float(self):
        data = _one_chip_fur()
        vol_off, triples = _info_parts(data)
        patched, chips = set_chip_volume(data, factor=0.5)
        self.assertAlmostEqual(chips[0].volume, 0.5, places=3)
        self.assertEqual(patched[vol_off], 32)
        self.assertAlmostEqual(struct.unpack_from("<f", patched, triples)[0], 0.5, places=3)

    def test_value_sets_outright(self):
        patched, chips = set_chip_volume(_one_chip_fur(), value=0.25)
        self.assertAlmostEqual(chips[0].volume, 0.25, places=3)

    def test_info_caps_at_unity(self):
        patched, chips = set_chip_volume(_one_chip_fur(), factor=4.0)
        self.assertAlmostEqual(chips[0].volume, MAX_INFO_VOLUME, places=3)

    def test_targets_keep_other_chips(self):
        data = _one_chip_fur()
        patched, chips = set_chip_volume(data, factor=0.5, targets=[])
        self.assertAlmostEqual(chips[0].volume, 1.0, places=3)
        self.assertEqual(patched, data)

    def test_needs_exactly_one_of_factor_value(self):
        with self.assertRaises(ValueError):
            set_chip_volume(_one_chip_fur())
        with self.assertRaises(ValueError):
            set_chip_volume(_one_chip_fur(), factor=1.0, value=1.0)

    def test_round_trip_keeps_the_file_readable(self):
        data = _one_chip_fur()
        patched, _chips = set_chip_volume(data, factor=0.75)
        chips = read_chips(patched)
        self.assertAlmostEqual(chips[0].volume, 0.75, places=3)


class TestInf2(unittest.TestCase):
    @staticmethod
    def _inf2(volume: float) -> bytes:
        def s(text: str) -> bytes:
            return text.encode() + b"\x00"

        payload = bytearray()
        for text in ("name", "author", "system", "", "", "", "", ""):
            payload += s(text)
        payload += struct.pack("<f", 440.0) + b"\x00"
        payload += struct.pack("<f", 1.0)
        payload += struct.pack("<HH", 8, 1)
        payload += struct.pack("<HH", 0x82, 8)
        payload += struct.pack("<fff", volume, 0.0, 0.0)
        buf = bytearray(16)
        buf[0:16] = FUR_MAGIC
        buf += struct.pack("<HH", 157, 0)
        buf += struct.pack("<I", 32)
        buf += bytes(8)
        buf += b"INF2" + struct.pack("<I", len(payload)) + bytes(payload)
        return bytes(buf)

    def test_reads_and_patches_floats(self):
        data = self._inf2(0.8)
        chips = read_chips(data)
        self.assertEqual(len(chips), 1)
        self.assertAlmostEqual(chips[0].volume, 0.8, places=3)
        patched, chips = set_chip_volume(data, factor=1.25)
        self.assertAlmostEqual(chips[0].volume, 1.0, places=3)

    def test_inf2_allows_more_than_unity(self):
        patched, chips = set_chip_volume(self._inf2(1.0), value=2.0)
        self.assertAlmostEqual(chips[0].volume, 2.0, places=3)


if __name__ == "__main__":
    unittest.main()
