"""Sega 32X PWM stream collection and Generic PCM DAC emission."""

from __future__ import annotations

import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import parse_fur
from vgm2151fur.furwrite import CHIP_PCM_DAC, write_fur
from vgm2151fur.pwm import PWM_RATE, collect_pwm
from vgm2151fur.vgm import ChipWrite, DacStreamPlay, STREAM_PWM, VgmFile


def _bank(frames: int = 16) -> bytes:
    out = bytearray()
    for i in range(frames):
        left = 0x100 + i
        right = 0x200 + i
        out += bytes((left & 0xFF, (left >> 8) & 0x0F))
        out += bytes((right & 0xFF, (right >> 8) & 0x0F))
    return bytes(out)


def _play(sample: int, reg: int, base: int) -> DacStreamPlay:
    return DacStreamPlay(
        sample=sample, chip_type=STREAM_PWM, chip_id=0, stream=reg - 2,
        port=0, reg=reg, offset=0, length=0, mode=3, rate=PWM_RATE,
        step=2, step_base=base, bank=3, block=-1,
    )


def _vgm(**kw) -> VgmFile:
    bank = _bank()
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=22020, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=0, k007232_clock=0, gd3={},
        writes=[ChipWrite(0, "pwm", 0, 1, 0x400)],
        dac_stream_plays=[_play(0, 2, 0), _play(0, 3, 1)],
        pwm_clock=23_011_361, pwm_bank=bytearray(bank),
        pwm_blocks=[bank], **kw,
    )


class TestPwmCollector(unittest.TestCase):
    def test_two_sides(self):
        samples, warn = collect_pwm(_vgm())
        self.assertEqual(warn, [])
        self.assertEqual(len(samples), 2)
        self.assertEqual({s.side for s in samples}, {0, 1})
        self.assertGreater(samples[0].frames, 0)
        self.assertNotEqual(samples[0].data, samples[1].data)
        self.assertGreater(any(samples[0].data), 0)

    def test_mono_stream_feeds_both_sides(self):
        v = _vgm()
        v.dac_stream_plays = [DacStreamPlay(
            sample=0, chip_type=STREAM_PWM, chip_id=0, stream=0,
            port=0, reg=4, offset=0, length=0, mode=3, rate=PWM_RATE,
            step=1, step_base=0, bank=3, block=-1,
        )]
        samples, _warn = collect_pwm(v)
        self.assertEqual(len(samples), 1)
        self.assertEqual({h.side for h in samples[0].hits}, {0, 1})

    def test_long_steady_stream_is_one_attack_body(self):
        from vgm2151fur.pwm import PWM_CHUNK_FRAMES
        frames = PWM_CHUNK_FRAMES * 2 + 10
        v = _vgm()
        v.dac_stream_plays = [
            DacStreamPlay(
                sample=0, chip_type=STREAM_PWM, chip_id=0, stream=0,
                port=0, reg=reg, offset=0, length=0, mode=3, rate=PWM_RATE,
                step=1, step_base=0, bank=3, block=-1,
            )
            for reg in (2, 3)
        ]
        v.pwm_bank = bytearray(_bank(frames))
        v.pwm_blocks = [bytes(v.pwm_bank)]
        v.total_samples = int(frames / PWM_RATE * 44100) + 100
        samples, _warn = collect_pwm(v)
        # A steady body has no attack split, so it is one sample on both sides.
        self.assertEqual(len(samples), 1)
        self.assertEqual({h.side for h in samples[0].hits}, {0, 1})

    def test_repeated_chunks_share_one_body(self):
        from vgm2151fur import pwm as pwmmod
        from vgm2151fur.pwm import PWM_CHUNK_FRAMES
        old = pwmmod.PWM_ATTACK_MAX_SAMPLES
        pwmmod.PWM_ATTACK_MAX_SAMPLES = 0  # force the chunk fallback
        try:
            v = _vgm()
            v.dac_stream_plays = [
                DacStreamPlay(
                    sample=0, chip_type=STREAM_PWM, chip_id=0, stream=0,
                    port=0, reg=reg, offset=0, length=0, mode=3, rate=PWM_RATE,
                    step=1, step_base=0, bank=3, block=-1,
                )
                for reg in (2, 3)
            ]
            item = bytes((0x00, 0x01))  # a constant 0x100 PWM value
            chunk = item * PWM_CHUNK_FRAMES
            body = chunk + chunk
            v.pwm_bank = bytearray(body)
            v.pwm_blocks = [body]
            v.total_samples = int(
                (2 * PWM_CHUNK_FRAMES) / PWM_RATE * 44100
            ) + 100
            samples, _warn = collect_pwm(v)
            self.assertEqual(len(samples), 1)
            self.assertEqual(len(samples[0].hits), 4)  # two chunks, two sides
        finally:
            pwmmod.PWM_ATTACK_MAX_SAMPLES = old

    def test_attack_bodies_share_repeated_hits(self):
        v = _vgm()
        v.dac_stream_plays = [
            DacStreamPlay(
                sample=0, chip_type=STREAM_PWM, chip_id=0, stream=0,
                port=0, reg=reg, offset=0, length=0, mode=3, rate=PWM_RATE,
                step=1, step_base=0, bank=3, block=-1,
            )
            for reg in (2, 3)
        ]
        a = bytes((0x00, 0x01)) * 200   # burst A
        b = bytes((0x00, 0x03)) * 200   # burst B
        gap = bytes(2) * 300            # 300 frames of silence
        body = a + gap + b + gap + a + gap + b + gap
        v.pwm_bank = bytearray(body)
        v.pwm_blocks = [body]
        v.total_samples = int((len(body) // 2) / PWM_RATE * 44100) + 100
        samples, _warn = collect_pwm(v)
        self.assertEqual(len(samples), 2)
        self.assertEqual([len(s.hits) for s in samples], [4, 4])


class TestPwmModule(unittest.TestCase):
    def test_pcm_dac_pair(self):
        song = analyze(_vgm())
        self.assertEqual(song.pwm_ids, [0])
        self.assertEqual(song.pwm_count, 2)
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(mod.chips, [CHIP_PCM_DAC, CHIP_PCM_DAC])
        self.assertEqual(mod.n_ch, 2)

    def test_dual_header_keeps_silent_second_chip(self):
        v = _vgm(pwm_dual=True)
        song = analyze(v)
        self.assertEqual(song.pwm_ids, [0, 1])
        mod = parse_fur(write_fur(song, include_fm=False))
        self.assertEqual(
            mod.chips, [CHIP_PCM_DAC, CHIP_PCM_DAC, CHIP_PCM_DAC, CHIP_PCM_DAC],
        )
        self.assertEqual(mod.n_ch, 4)


if __name__ == "__main__":
    unittest.main()
