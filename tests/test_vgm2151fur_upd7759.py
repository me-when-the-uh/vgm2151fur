"""uPD7759 stand-alone/slave collection and Generic PCM DAC emission."""

from __future__ import annotations

import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import parse_fur
from vgm2151fur.furwrite import CHIP_PCM_DAC, write_fur
from vgm2151fur.upd7759 import (
    decode_stream,
    collect_upd7759,
)
from vgm2151fur.vgm import ChipWrite, RomSlice, VgmFile


def _rom() -> bytes:
    """One phrase: rate 1, four nibbles, end."""
    data = bytearray(0x20)
    data[0:5] = bytes([0x00, 0x5A, 0xA5, 0x69, 0x55])
    data[5:7] = (6).to_bytes(2, "big")  # phrase 0 -> 0x0D
    data[0x0D:0x12] = bytes([0x80, 0x03, 0x12, 0x34, 0x00])
    return bytes(data)


def _vgm(writes, **kw) -> VgmFile:
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=44100, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        writes=list(writes), upd7759_clock=640_000, **kw,
    )


def _start(sample: int, port: int = 0) -> list[ChipWrite]:
    return [
        ChipWrite(sample, "upd7759", 0, 2, port),
        ChipWrite(sample, "upd7759", 0, 1, 0),
        ChipWrite(sample, "upd7759", 0, 1, 1),
    ]


class TestUpd7759Collector(unittest.TestCase):
    def test_stand_alone_phrase(self):
        rom = _rom()
        v = _vgm(_start(0), roms=[RomSlice(0, len(rom), 0, rom, "upd7759")])
        samples, warn = collect_upd7759(v)
        self.assertEqual(warn, [])
        self.assertEqual(len(samples), 1)
        self.assertGreater(samples[0].frames, 0)
        self.assertEqual(samples[0].phrase, 0)
        self.assertEqual(len(samples[0].hits), 1)
        self.assertGreater(samples[0].hits[0].end, 0)

    def test_slave_stream_decodes_after_handshake(self):
        # Handshake (6 bytes) then a rate-1 block with four nibbles.
        burst = bytes([
            0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00,
            0x80, 0x03, 0x12, 0x34, 0x00,
        ])
        writes = [ChipWrite(0, "upd7759", 0, 1, 0)]
        writes.append(ChipWrite(0, "upd7759", 0, 1, 1))
        writes += [ChipWrite(i, "upd7759", 0, 2, b) for i, b in enumerate(burst)]
        writes.append(ChipWrite(1000, "upd7759", 0, 1, 0))
        v = _vgm(writes, upd7759_slave=True)
        samples, warn = collect_upd7759(v)
        self.assertEqual(warn, [])
        self.assertEqual(len(samples), 1)
        self.assertGreater(samples[0].frames, 0)

    def test_decode_stream_repeat(self):
        phrase = decode_stream(bytes([0xC1, 0x80, 0x01, 0xAB, 0x00]))
        self.assertEqual(phrase.blocks, 4)
        self.assertGreater(len(phrase.events), 2)


class TestUpd7759Module(unittest.TestCase):
    def test_pcm_dac_layout(self):
        rom = _rom()
        v = _vgm(_start(0), roms=[RomSlice(0, len(rom), 0, rom, "upd7759")])
        song = analyze(v)
        self.assertEqual(song.upd7759_ids, [0])
        self.assertEqual(song.upd7759_count, 1)
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [CHIP_PCM_DAC])
        self.assertEqual(mod.n_ch, 1)

    def test_dual_header_keeps_silent_second_chip(self):
        rom = _rom()
        v = _vgm(
            _start(0),
            roms=[RomSlice(0, len(rom), 0, rom, "upd7759")],
            upd7759_dual=True,
        )
        song = analyze(v)
        self.assertEqual(song.upd7759_ids, [0, 1])
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [CHIP_PCM_DAC, CHIP_PCM_DAC])
        self.assertEqual(mod.n_ch, 2)

    def test_identical_phrases_share_one_body(self):
        rom = _rom()
        # Two start edges for the same phrase.
        writes = _start(0)
        writes += [
            ChipWrite(5000, "upd7759", 0, 1, 0),
            ChipWrite(5000, "upd7759", 0, 1, 1),
        ]
        v = _vgm(writes, roms=[RomSlice(0, len(rom), 0, rom, "upd7759")])
        samples, _warn = collect_upd7759(v)
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(samples[0].hits), 2)


if __name__ == "__main__":
    unittest.main()
