"""SSG/PSG PCM (volume-register DAC) detection and .fur emission."""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from vgm2151fur.analyze import analyze
from vgm2151fur.furio import decompress_fur, iter_blocks, parse_fur
from vgm2151fur.furwrite import INS_AY, write_fur
from vgm2151fur.ssgpcm import (
    DAC_LOG_TABLE_AY,
    collect_ssg_pcm,
)
from vgm2151fur.vgm import (
    ChipWrite, DacStreamPlay, VgmFile, load_vgm,
)

AY_CLOCK = 1789773
YM2203_CLOCK = 3993600
EYE = Path(
    r"F:\NASbackup\JamesAlan\_devspace\grbsd\ripwip\_finished\rusty-pc98"
    r"\m-rerip\done\20 Eye Catch.vgz"
)


def _vgm(writes, *, total=200000, ay_clock=AY_CLOCK, ym2203_clock=0):
    return VgmFile(
        path=Path("synthetic.vgm"), buf=b"", version=0x161,
        total_samples=total, loop_samples=0, loop_file_pos=None,
        loop_sample=None, ym2151_clock=3579545, k007232_clock=0, gd3={},
        roms=[], writes=list(writes), ay8910_clock=ay_clock,
        ym2203_clock=ym2203_clock,
    )


def _ay(sample, reg, val, chip="ay8910"):
    return ChipWrite(sample, chip, 0, reg, val)


def _square(start=0, writes=200, dt=5, level_hi=0x0F, level_lo=0x00,
            half=22, reg=10, chip="ay8910"):
    """A square wave written through the volume register."""
    out = []
    for i in range(writes):
        val = level_hi if ((i // half) % 2 == 0) else level_lo
        out.append(_ay(start + i * dt, reg, val, chip))
    return out


def _pcm_writes(start, count, dt=5, reg=10, chip="ay8910", mixer=0xBF,
                half=22):
    out = [_ay(start, 7, mixer, chip)]
    out += _square(start + 100, count, dt, reg=reg, chip=chip, half=half)
    return out


def _ins_types(data: bytes) -> list[int]:
    out = []
    for _off, mag, _size, blk in iter_blocks(decompress_fur(data)):
        if mag == b"INS2":
            out.append(struct.unpack_from("<H", blk, 2)[0])
    return out


def _ins_sm_maps(data: bytes, index: int) -> list[int]:
    """The 120 note-map sample indices of the INS2 at block position `index`."""
    n = 0
    for _off, mag, _size, blk in iter_blocks(decompress_fur(data)):
        if mag != b"INS2":
            continue
        if n == index:
            pos = 4
            while pos + 2 <= len(blk):
                feat = blk[pos:pos + 2]
                if feat == b"EN":
                    break
                fsz = struct.unpack_from("<H", blk, pos + 2)[0]
                body = blk[pos + 4:pos + 4 + fsz]
                if feat == b"SM":
                    return [
                        struct.unpack_from("<h", body, 4 + i * 4 + 2)[0]
                        for i in range(120)
                    ]
                pos += 4 + fsz
            return []
        n += 1
    return []


class TestSsgPcmDetect(unittest.TestCase):
    def test_mixer_off_fast_volume_writes_become_one_sample(self):
        samples, hits, _warn = collect_ssg_pcm(
            _vgm(_pcm_writes(1000, 700)), chip="ay8910",
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(hits), 1)
        self.assertEqual((samples[0].voice, hits[0].voice), (2, 2))
        self.assertEqual(samples[0].rate, 8820)
        self.assertGreaterEqual(len(samples[0].data), 512)

    def test_fast_writes_with_tone_on_are_not_pcm(self):
        # Mixer bit clear = tone enabled; the register is not a DAC level.
        samples, hits, _warn = collect_ssg_pcm(
            _vgm(_pcm_writes(1000, 700, mixer=0xB8)), chip="ay8910",
        )
        self.assertEqual(samples, [])
        self.assertEqual(hits, [])

    def test_slow_volume_ramp_is_not_pcm(self):
        writes = [_ay(0, 7, 0xBF)]
        for i in range(60):
            writes.append(_ay(i * 500, 10, (i % 16) & 0x0F))
        samples, hits, _warn = collect_ssg_pcm(_vgm(writes), chip="ay8910")
        self.assertEqual((samples, hits), ([], []))

    def test_long_gap_splits_hits(self):
        writes = _pcm_writes(1000, 700)
        writes += _pcm_writes(1000 + 700 * 5 + 4096, 700)
        samples, hits, _warn = collect_ssg_pcm(_vgm(writes), chip="ay8910")
        # Identical bodies share one instrument; the two hits remain.
        self.assertEqual(len(hits), 2)
        self.assertGreaterEqual(len(samples), 1)

    def test_short_gap_keeps_one_body(self):
        writes = _pcm_writes(1000, 700)
        writes += _pcm_writes(1000 + 700 * 5 + 512, 700)
        samples, hits, _warn = collect_ssg_pcm(_vgm(writes), chip="ay8910")
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(hits), 1)

    def test_identical_bodies_are_shared(self):
        writes = _pcm_writes(1000, 700)
        writes += _pcm_writes(1000 + 700 * 5 + 4096, 700)
        samples, hits, _warn = collect_ssg_pcm(_vgm(writes), chip="ay8910")
        self.assertEqual(len(samples), 1)
        self.assertEqual({h.sample_index for h in hits}, {0})

    def test_levels_round_trip_through_the_dac_table(self):
        # Alternate 15 and 0 levels; the stored bytes must decode back to
        # exactly those levels via Furnace's runDAC table.
        samples, _hits, _warn = collect_ssg_pcm(
            _vgm(_pcm_writes(1000, 700, half=5)), chip="ay8910",
        )
        levels = {
            DAC_LOG_TABLE_AY[byte ^ 0x80] for byte in samples[0].data
        }
        self.assertEqual(levels, {0, 15})

    def test_three_voices_run_together(self):
        writes = [_ay(0, 7, 0xBF)]
        for voice in range(3):
            writes += _square(100, 700, 5, reg=8 + voice)
        samples, hits, _warn = collect_ssg_pcm(_vgm(writes), chip="ay8910")
        # The three voices play at once; identical bodies share one instrument.
        self.assertEqual(sorted(h.voice for h in hits), [0, 1, 2])
        self.assertEqual({h.sample_index for h in hits}, {0})

    def test_ym2203_ssg_uses_the_same_path(self):
        writes = [_ay(0, 7, 0xBF, chip="ym2203_ssg")]
        writes += _square(100, 700, 5, chip="ym2203_ssg")
        samples, hits, _warn = collect_ssg_pcm(
            _vgm(writes, ym2203_clock=YM2203_CLOCK), chip="ym2203_ssg",
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(hits), 1)


class TestSsgPcmDacStream(unittest.TestCase):
    """0x90-0x95 streams writing an SSG volume register."""

    def _stream(self, body, length=None, mode=1, rate=15964, sample=1000):
        return VgmFile(
            path=Path("stream.vgm"), buf=b"", version=0x161,
            total_samples=200000, loop_samples=0, loop_file_pos=None,
            loop_sample=None, ym2151_clock=3579545, k007232_clock=0,
            gd3={}, roms=[], writes=[], ym2203_clock=YM2203_CLOCK,
            dac_data=bytearray(body),
            dac_stream_plays=[DacStreamPlay(
                sample=sample, chip_type=0x06, chip_id=0, stream=0,
                port=0, reg=10, offset=0,
                length=len(body) if length is None else length,
                mode=mode, rate=rate, loop=False, reverse=False,
                step=1, step_base=0, bank=0,
            )],
        )

    def test_stream_body_uses_the_whole_length(self):
        body = bytes([(i // 8) % 16 for i in range(1200)])
        vgm = self._stream(body)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(hits), 1)
        # 1200 bytes at 15964 Hz is ~3315 VGM samples.
        self.assertGreater(len(samples[0].data), 1000)
        self.assertEqual(samples[0].rate, 15964)
        self.assertEqual(samples[0].voice, 2)

    def test_same_offset_plays_share_the_longest_body(self):
        body = bytes([(i // 8) % 16 for i in range(1200)])
        vgm = self._stream(body, length=1200, sample=1000)
        vgm.dac_stream_plays.append(DacStreamPlay(
            sample=10000, chip_type=0x06, chip_id=0, stream=0,
            port=0, reg=10, offset=0, length=600, mode=1,
            rate=15964, loop=False, reverse=False, step=1,
            step_base=0, bank=0,
        ))
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        # Both plays read bank offset 0; the 600-byte play is a prefix of the
        # 1,200-byte one, so it reuses that body and is cut at its own stop.
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(hits), 2)
        self.assertGreater(len(samples[0].data), 1000)
        first = min(hits, key=lambda h: h.sample_time)
        self.assertEqual(first.sample_time, 1000)
        self.assertLess(
            first.end_time,
            1000 + 1200 * 44100 // 15964,
        )
        self.assertEqual({h.sample_index for h in hits}, {0})

    def test_stream_levels_round_trip(self):
        body = bytes([0, 15] * 600)
        vgm = self._stream(body)
        samples, _hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        levels = {
            DAC_LOG_TABLE_AY[b ^ 0x80] for b in samples[0].data
        }
        self.assertEqual(levels, {0, 15})

    def test_stream_ignores_other_registers(self):
        vgm = self._stream(bytes([15] * 600))
        vgm.dac_stream_plays[0].reg = 0  # tone period, not a DAC level
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual((samples, hits), ([], []))

    def test_millisecond_length_mode(self):
        body = bytes([(i // 8) % 16 for i in range(3000)])
        # 100 ms at 15964 Hz = 1596 bytes of the bank.
        vgm = self._stream(body, length=100, mode=2)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(hits), 1)
        self.assertGreater(len(samples[0].data), 1400)

    def test_high_dc_stream_is_dropped(self):
        # A tiny peak followed by a flat DC tail is not worth a sample slot.
        body = bytes([15] * 200 + [0] * 4000)
        vgm = self._stream(body, length=len(body), rate=15964)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual((samples, hits), ([], []))

    def test_dc_tail_is_trimmed(self):
        # 50% DC: keep the body but drop the trailing flat half.
        body = bytes([15] * 1000 + [0] * 1000)
        vgm = self._stream(body, length=len(body), rate=15964)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(hits), 1)
        self.assertGreater(len(samples[0].data), 900)
        self.assertLess(len(samples[0].data), 1300)

    def test_pure_dc_stream_is_dropped(self):
        body = bytes([15] * 3000)
        vgm = self._stream(body, length=len(body), rate=15964)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual((samples, hits), ([], []))

    def test_stop_sample_ends_the_play_early(self):
        body = bytes([(i // 8) % 16 for i in range(3000)])
        vgm = self._stream(body, length=3000, rate=15964, sample=1000)
        # A 0x94 600 frames in cuts the 3,000-byte declared length short.
        vgm.dac_stream_plays[0].stop_sample = 1000 + int(round(600 * 44100 / 15964))
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(hits), 1)
        self.assertGreater(len(samples[0].data), 500)
        self.assertLess(len(samples[0].data), 800)


class TestSsgPcmUnused(unittest.TestCase):
    def test_unreferenced_bodies_are_not_written(self):
        # A stream aimed at a second YM2203 instance the module does not have.
        # The body must not survive as an unused SMP2 block.
        vgm = VgmFile(
            path=Path("unused.vgm"), buf=b"", version=0x161,
            total_samples=50000, loop_samples=0, loop_file_pos=None,
            loop_sample=None, ym2151_clock=3579545, k007232_clock=0,
            gd3={}, roms=[],
            writes=[ChipWrite(0, "ym2203", 0, 0x27, 0)],
            ym2203_clock=YM2203_CLOCK,
            dac_data=bytearray([15] * 600),
            dac_stream_plays=[DacStreamPlay(
                sample=1000, chip_type=0x06, chip_id=1, stream=0,
                port=0, reg=10, offset=0, length=600, mode=1,
                rate=15964, loop=False, reverse=False, step=1,
                step_base=0, bank=0,
            )],
        )
        song = analyze(vgm)
        self.assertEqual(song.ssg_pcm_samples, [])
        self.assertEqual(song.ssg_pcm_count, 0)
        data = write_fur(song)
        m = parse_fur(data)
        self.assertEqual(len(m.samples), 0)
        self.assertFalse([t for t in _ins_types(data) if t == INS_AY])


class TestSsgPcmEmission(unittest.TestCase):
    def _song(self):
        writes = [_ay(0, 7, 0xB8)]
        # A normal tone on voice 0, then PCM on voice 2.
        writes += [
            _ay(100, 0, 0x00), _ay(100, 1, 0x01),
            _ay(120, 8, 0x0F),
            _ay(1500, 8, 0x00),
        ]
        writes += _pcm_writes(3000, 700)
        return analyze(_vgm(writes, total=4000))

    def test_tone_note_survives_beside_the_pcm_note(self):
        song = self._song()
        tone = [e for e in song.events if e.on and e.tone_chip == 2]
        pcm = [e for e in song.events if e.on and e.pcm]
        self.assertTrue(tone)
        self.assertEqual(len(pcm), 1)
        self.assertLess(tone[0].sample, pcm[0].sample)

    def test_pcm_note_is_c4_and_8_bit(self):
        song = self._song()
        pcm = [e for e in song.events if e.on and e.pcm][0]
        # Slot 0 of the sample map; every map slot stores C-4.
        self.assertEqual(pcm.note, 60)
        self.assertEqual(pcm.vol, 15)
        self.assertTrue(pcm.no_pan)
        data = write_fur(song)
        m = parse_fur(data)
        self.assertEqual(len(song.ssg_pcm_samples), 1)
        self.assertEqual(m.samples[-1].depth, 8)
        self.assertEqual(m.samples[-1].c4_rate, 8820)
        self.assertEqual(_ins_types(data)[pcm.ins], INS_AY)
        self.assertEqual(_ins_sm_maps(data, pcm.ins)[pcm.note - 60], 0)

    def test_three_pcm_notes_get_three_channels(self):
        writes = [_ay(0, 7, 0xBF)]
        for voice in range(3):
            # Different duty per voice so the bodies stay distinct.
            writes += _square(100, 700, 5, half=22 + voice * 5, reg=8 + voice)
        song = analyze(_vgm(writes, total=4000))
        pcm = [e for e in song.events if e.on and e.pcm]
        self.assertEqual(len(pcm), 3)
        self.assertEqual(len({e.ch for e in pcm}), 3)
        data = write_fur(song)
        m = parse_fur(data)
        self.assertEqual(len(m.samples), 3)
        # All three bodies fit one sample-map instrument.
        self.assertEqual(len({e.ins for e in pcm}), 1)
        maps = _ins_sm_maps(data, pcm[0].ins)
        self.assertEqual(
            [maps[e.note - 60] for e in pcm], [0, 1, 2],
        )


class TestSsgPcmRealRips(unittest.TestCase):
    """The user's reference rips, when present."""

    def _check(self, path: Path, chip: str, min_notes: int):
        vgm = load_vgm(path)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip=chip)
        self.assertGreaterEqual(len(hits), min_notes)
        self.assertTrue(all(len(s.data) == 0 or s.rate for s in samples))
        self.assertEqual({s.rate for s in samples}.__len__(), 1)

    @unittest.skipUnless(
        Path(r"D:\Downloads\rforce.vgm").is_file(), "rforce.vgm missing"
    )
    def test_rforce_drops_the_dc_voice(self):
        # Voice 0's body is ~96% one level (a flat DC offset); it is dropped
        # instead of spending a sample slot on the flat line.
        vgm = load_vgm(Path(r"D:\Downloads\rforce.vgm"))
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(samples), 2)
        self.assertEqual(sorted(s.voice for s in samples), [1, 2])

    @unittest.skipUnless(
        Path(r"F:\NASbackup\JamesAlan\_devspace\grbsd\SELECT-ay.vgm").is_file(),
        "SELECT-ay.vgm missing",
    )
    def test_select_ay_stream(self):
        self._check(
            Path(r"F:\NASbackup\JamesAlan\_devspace\grbsd\SELECT-ay.vgm"),
            "ay8910", 20,
        )

    @unittest.skipUnless(EYE.is_file(), "20 Eye Catch missing")
    def test_eye_uses_the_dac_stream_in_full(self):
        vgm = load_vgm(EYE)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertGreaterEqual(len(hits), 5)
        # Every body clears the tiny-fragment minimum.  The file has both
        # DAC-stream notes (~15.9 kHz) and direct-write PCM sections, so the
        # rates are not all the same.
        self.assertTrue(all(len(s.data) >= 128 for s in samples))
        self.assertTrue(any(s.rate > 10000 for s in samples))


if __name__ == "__main__":
    unittest.main()


