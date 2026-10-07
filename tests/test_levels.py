"""Peak normalisation: the WAV scan, the master-volume patch, the fit.

`normalize_module` renders the module once at a reduced volume, reads the
peak, and writes the master volume that lands the track at -1.5 dBFS. The
render is the measurement, so the tests that render are the real ones;
they are skipped when the Furnace console build is missing.
"""

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
from vgm2151fur import furio  # noqa: E402
from vgm2151fur.analyze import analyze  # noqa: E402
from vgm2151fur.furnace import FurnaceError, find_furnace, render_wav  # noqa: E402
from vgm2151fur.furwrite import write_fur  # noqa: E402
from vgm2151fur.levels import (  # noqa: E402
    MAX_GAIN,
    TARGET_PEAK,
    normalize_module,
    peak_dbfs,
    wav_peak,
)

TMP = Path(tempfile.gettempdir()) / "vgm2151fur_levels"


def _write_wav(path: Path, samples: list[float], fmt: int, bits: int) -> None:
    if bits == 32:
        data = struct.pack(f"<{len(samples)}f", *samples)
    else:
        data = struct.pack(f"<{len(samples)}h", *samples)
    block = bits // 8 * 2
    out = bytearray(b"RIFF")
    out += struct.pack("<I", 4 + 24 + 8 + len(data))
    out += b"WAVEfmt " + struct.pack("<IHHIIHH", 16, fmt, 2, 44100, 44100 * block, block, bits)
    out += b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(bytes(out))


def _song_module() -> bytes:
    song = analyze(note_song(key_on=60000, key_off=160000), pcm=False)
    return write_fur(song, include_pcm=False, include_fm=True)


class TestWavPeak(unittest.TestCase):
    def test_f32_peak(self):
        TMP.mkdir(parents=True, exist_ok=True)
        wav = TMP / "peak.f32.wav"
        _write_wav(wav, [0.5, -1.25, 0.3], fmt=3, bits=32)
        self.assertAlmostEqual(wav_peak(wav), 1.25, places=6)

    def test_s16_peak(self):
        TMP.mkdir(parents=True, exist_ok=True)
        wav = TMP / "peak.s16.wav"
        _write_wav(wav, [-32768, 100, 0], fmt=1, bits=16)
        self.assertAlmostEqual(wav_peak(wav), 1.0, places=6)

    def test_peak_dbfs(self):
        self.assertAlmostEqual(peak_dbfs(TARGET_PEAK), -1.5, places=6)
        self.assertEqual(peak_dbfs(0.0), float("-inf"))


class TestMasterVolumePatch(unittest.TestCase):
    def test_roundtrip_and_single_field_touch(self):
        data = _song_module()
        self.assertEqual(furio.master_volume(data), 1.0)
        patched = furio.set_master_volume(data, 2.5)
        self.assertEqual(furio.master_volume(patched), 2.5)
        self.assertEqual(furio.parse_fur(patched).master_vol, 2.5)
        self.assertEqual(len(patched), len(data))
        changed = sum(a != b for a, b in zip(data, patched))
        self.assertLessEqual(changed, 4)


@unittest.skipUnless(find_furnace(), "Furnace console build missing")
class TestNormalize(unittest.TestCase):
    def _render_peak(self, data: bytes, name: str) -> float:
        # write_fur output is INFO at 1.0, so a unity render needs no probe.
        TMP.mkdir(parents=True, exist_ok=True)
        fur = TMP / f"{name}.fur"
        wav = TMP / f"{name}.f32.wav"
        fur.write_bytes(data)
        render_wav(fur, wav, loops=1, outformat="f32")
        return wav_peak(wav)

    def test_lands_at_minus_1_5_dbfs(self):
        data = _song_module()
        unity = self._render_peak(data, "unity")
        result = normalize_module(data, name="fit")
        self.assertIsNotNone(result)
        self.assertLessEqual(result.gain, MAX_GAIN)
        self.assertAlmostEqual(result.gain, TARGET_PEAK / unity, places=3)
        self.assertAlmostEqual(furio.master_volume(result.data), result.gain, places=6)
        after = self._render_peak(result.data, "after")
        self.assertAlmostEqual(peak_dbfs(after), -1.5, places=2)

    def test_loud_module_steps_down_the_probe(self):
        loud = furio.set_master_volume(_song_module(), 40.0)
        result = normalize_module(loud, name="loud")
        self.assertIsNotNone(result)
        after = self._render_peak(result.data, "loud_after")
        self.assertAlmostEqual(peak_dbfs(after), -1.5, places=2)


if __name__ == "__main__":
    unittest.main()
