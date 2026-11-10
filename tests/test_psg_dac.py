"""SN76489 sample playback (4-bit DAC) -> Generic PCM DAC."""

from __future__ import annotations

import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import decompress_fur, iter_blocks, parse_fur
from vgm2151fur.furwrite import CHIP_PCM_DAC, write_fur
from vgm2151fur.psg_dac import collect_psg_dac
from vgm2151fur.vgm import ChipWrite, VgmFile


def _vgm(writes, *, total_samples=4096, loop_sample=None, clock=3579545):
    return VgmFile(
        path=Path("psg_dac.vgm"), buf=b"", version=0x161,
        total_samples=total_samples, loop_samples=0, loop_file_pos=None,
        loop_sample=loop_sample, ym2151_clock=0, k007232_clock=0,
        gd3={}, roms=[], writes=sorted(writes, key=lambda w: w.sample),
        sn76489_clock=clock, ym2612_clock=0,
    )


def _dac_writes(total=4096):
    writes = [
        ChipWrite(0, "sn76489", 0, 0, 0x80),  # voice 0 period low 0
        ChipWrite(0, "sn76489", 0, 0, 0x00),  # period high 0
        ChipWrite(0, "sn76489", 0, 0, 0x90),  # full volume
    ]
    pattern = (5, 4, 6, 3, 7, 2, 8, 1, 9, 0, 10, 15, 5, 5, 5, 8)
    for i, t in enumerate(range(16, total, 8)):
        writes.append(ChipWrite(t, "sn76489", 0, 0, 0x90 | pattern[i % 16]))
    return writes


class TestPsgDacCollector(unittest.TestCase):
    def test_detects_the_stream_and_splits_chunks(self):
        got = collect_psg_dac(_vgm(_dac_writes(), loop_sample=2048))
        self.assertEqual(got.voices, [(0, 0)])
        self.assertGreater(len(got.samples), 1)
        self.assertGreater(len(got.plays), 1)
        self.assertEqual(got.rate, 22050)
        self.assertTrue(any(any(s.data) for s in got.samples))
        # The loop point gets its own chunk boundary.
        self.assertIn(2048, [p.sample_time for p in got.plays])

    def test_silent_chunks_are_filtered_out(self):
        writes = [
            ChipWrite(0, "sn76489", 0, 0, 0x80),
            ChipWrite(0, "sn76489", 0, 0, 0x00),
            ChipWrite(0, "sn76489", 0, 0, 0x90),
        ]
        pattern = (5, 4, 6, 3, 7, 2, 8, 1)
        for i, t in enumerate(range(16, 4096, 8)):
            writes.append(
                ChipWrite(t, "sn76489", 0, 0, 0x90 | pattern[i % 8])
            )
        # A long rest at the idle attenuation, then another burst.
        for t in range(4096, 30000, 8):
            writes.append(ChipWrite(t, "sn76489", 0, 0, 0x90 | 5))
        for i, t in enumerate(range(30000, 34096, 8)):
            writes.append(
                ChipWrite(t, "sn76489", 0, 0, 0x90 | pattern[i % 8])
            )
        got = collect_psg_dac(_vgm(writes, total_samples=34096))
        for smp in got.samples:
            self.assertTrue(
                any(v != 8 for v in smp.data),
                "a silent body was kept",
            )
            self.assertNotEqual(smp.data[0], 8, "a body starts in silence")

    def test_non_dac_volume_writes_are_not_a_stream(self):
        writes = [
            ChipWrite(0, "sn76489", 0, 0, 0x80),
            ChipWrite(0, "sn76489", 0, 0, 0x10),  # period 0x100, a tone
            ChipWrite(0, "sn76489", 0, 0, 0x90),
        ]
        writes += [
            ChipWrite(t, "sn76489", 0, 0, 0x90 | (t // 64) % 12)
            for t in range(500, 4096, 500)
        ]
        got = collect_psg_dac(_vgm(writes))
        self.assertEqual(got.voices, [])
        self.assertEqual(got.samples, [])


class TestPsgDacModule(unittest.TestCase):
    def test_module_uses_a_generic_pcm_dac_system(self):
        song = analyze(_vgm(_dac_writes(), loop_sample=2048))
        self.assertEqual(song.psg_dac_voices, [(0, 0)])
        self.assertTrue(song.psg_dac_samples)
        self.assertTrue(song.psg_dac_plays)
        data = write_fur(song)
        mod = parse_fur(data)
        self.assertIn(CHIP_PCM_DAC, mod.chips)
        self.assertEqual(mod.n_ch, 1)
        self.assertEqual(len(mod.samples), len(song.psg_dac_samples))
        # Source and body are both 4-bit; Furnace's depth-15 nibbles.
        self.assertEqual(mod.samples[0].depth, 15)
        # One one-voice DAC system: the PSG chip level 0.5 divided by the
        # four-voice mixer share, i.e. 0.125 of unity.
        raw = decompress_fur(data)
        for _off, mag, _size, payload in iter_blocks(raw):
            if mag in (b"INFO", b"INF2"):
                vols = list(payload[56:88])
                self.assertEqual(vols[0], 8)
                break
        notes = [
            row for (_ch, _pat), rows in mod.patterns.items()
            for row in rows if 0 <= row.note < 180
        ]
        self.assertTrue(notes)
        self.assertTrue(all(row.ins >= 0 for row in notes))


if __name__ == "__main__":
    unittest.main()
