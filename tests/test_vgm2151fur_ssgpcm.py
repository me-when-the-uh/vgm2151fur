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
    SsgPcmHit,
    SsgPcmSample,
    cluster_ssg_pcm,
    collect_ssg_pcm,
)
from vgm2151fur.vgm import (
    ChipWrite, DacStreamPlay, VgmFile,
)

AY_CLOCK = 1789773
YM2203_CLOCK = 3993600


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

    def test_dominant_level_with_short_runs_is_a_waveform(self):
        # A body can spend most of its time at one level without reaching the
        # DC threshold: the run length, not the overall share, decides.
        writes = [_ay(0, 7, 0xBF)]
        writes += _square(100, 2500, 5, half=22, reg=8)
        writes += _square(100, 2500, 5, half=27, reg=9)
        t = 100
        for i in range(2500):
            writes.append(_ay(t, 10, 0 if i % 625 == 624 else 15))
            t += 5
        samples, hits, _warn = collect_ssg_pcm(_vgm(writes), chip="ay8910")
        self.assertEqual(len(samples), 3)
        self.assertEqual(sorted(s.voice for s in samples), [0, 1, 2])
        self.assertEqual(len(hits), 3)
        self.assertTrue(all(len(s.data) > 2048 for s in samples))

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

    def test_near_offsets_share_a_body(self):
        # A pointer a few bytes into the same sample is a start shift, not a
        # second body.
        body = bytes([(i // 4) % 16 for i in range(3000)])
        vgm = self._stream(body, length=2000, sample=1000)
        vgm.dac_stream_plays.append(DacStreamPlay(
            sample=10000, chip_type=0x06, chip_id=0, stream=0,
            port=0, reg=10, offset=4, length=2000, mode=1,
            rate=15964, loop=False, reverse=False, step=1,
            step_base=0, bank=0,
        ))
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(hits), 2)

    def test_distant_offsets_stay_separate(self):
        body = bytes([(i // 4) % 16 for i in range(3000)])
        vgm = self._stream(body, length=1000, sample=1000)
        vgm.dac_stream_plays.append(DacStreamPlay(
            sample=10000, chip_type=0x06, chip_id=0, stream=0,
            port=0, reg=10, offset=8, length=1000, mode=1,
            rate=15964, loop=False, reverse=False, step=1,
            step_base=0, bank=0,
        ))
        samples, _hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(samples), 2)

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

    def test_stream_and_direct_writes_coexist(self):
        # A driver can mix DAC-stream notes with direct volume-register PCM.
        # Both paths must keep their own bodies and rates.
        body = bytes([(i // 8) % 16 for i in range(2400)])
        vgm = self._stream(body, sample=1000)
        vgm.writes.extend(_pcm_writes(1000, 700, reg=8, chip="ym2203_ssg"))
        for k in range(4):
            vgm.dac_stream_plays.append(DacStreamPlay(
                sample=10000 + k * 5000, chip_type=0x06, chip_id=0,
                stream=0, port=0, reg=10, offset=0, length=2400, mode=1,
                rate=15964, loop=False, reverse=False, step=1,
                step_base=0, bank=0,
            ))
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertGreaterEqual(len(hits), 5)
        self.assertTrue(all(len(s.data) >= 128 for s in samples))
        self.assertTrue(any(s.rate > 10000 for s in samples))
        self.assertGreater(len({s.rate for s in samples}), 1)

    def test_high_dc_stream_is_dropped(self):
        # A tiny peak followed by a flat DC tail is not worth a sample slot.
        body = bytes([15] * 200 + [0] * 4000)
        vgm = self._stream(body, length=len(body), rate=15964)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual((samples, hits), ([], []))

    def test_dc_tail_is_trimmed(self):
        # 50% DC: keep the body but drop the trailing flat half.
        body = bytes([15] * 1500 + [0] * 1500)
        vgm = self._stream(body, length=len(body), rate=15964)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(hits), 1)
        self.assertGreater(len(samples[0].data), 1400)
        self.assertLess(len(samples[0].data), 1700)

    def test_short_body_is_left_alone(self):
        # 2,048 frames or fewer keep their exact shape even with a DC tail.
        body = bytes([15] * 1000 + [0] * 1000)
        vgm = self._stream(body, length=len(body), rate=15964)
        samples, hits, _warn = collect_ssg_pcm(vgm, chip="ym2203_ssg")
        self.assertEqual(len(hits), 1)
        self.assertGreater(len(samples[0].data), 1900)

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
    def test_writer_drops_a_body_no_note_selects(self):
        writes = [_ay(0, 7, 0xBF)]
        writes += _square(100, 700, 5, reg=8)
        song = analyze(_vgm(writes, total=4000))
        self.assertEqual(len(song.ssg_pcm_samples), 1)
        song.ssg_pcm_samples.append(SsgPcmSample(
            "unused", b"\x01\x02\x03", 8820, 0, 0,
        ))
        stats: dict = {}
        data = write_fur(song, stats=stats)
        mod = parse_fur(data)
        self.assertEqual(stats.get("ssg_pcm_samples_unused"), 1)
        self.assertEqual(len(mod.samples), 1)
        pcm = [e for e in song.events if e.on and e.pcm][0]
        self.assertEqual(_ins_sm_maps(data, pcm.ins)[pcm.note - 60], 0)

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


class TestSsgPcmClustering(unittest.TestCase):
    def test_cluster_folds_fragments_and_remaps_hits(self):
        samples = [
            SsgPcmSample(
                name=f"pcm{i}",
                data=bytes((i * 41 + j * 17) & 0xFF for j in range(32)),
                rate=11025, start=i * 1000, end=i * 1000 + 32,
            )
            for i in range(40)
        ]
        hits = [
            SsgPcmHit(
                sample_time=i * 4410, end_time=0, voice=0, chip_id=0,
                sample_index=i,
            )
            for i in range(40)
        ]
        before = len(samples)
        out, out_hits = cluster_ssg_pcm(samples, hits)
        self.assertLessEqual(len(out), 32)
        self.assertEqual(len(out_hits), before)
        self.assertTrue(all(0 <= h.sample_index < len(out) for h in out_hits))


if __name__ == "__main__":
    unittest.main()


