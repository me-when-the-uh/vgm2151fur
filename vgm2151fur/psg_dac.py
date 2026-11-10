"""SN76489 sample playback -> Generic PCM DAC bodies.

Some Mega Drive drivers use a PSG tone channel as a 4-bit streaming DAC:
the tone period stays 0 and the driver rewrites the 4-bit attenuation at
audio rate.  Furnace's SMS platform has no sample voice, so the stream is
rendered to 4-bit linear PCM (the 2 dB curve baked into the nibbles) and
played through a Generic PCM DAC system, like the YM2612 DAC maps.

The stream is split into short chunks so repeated hits share one body and a
long track does not become one enormous sample.  Chunk boundaries are also
placed at the VGM loop point, which keeps the pattern loop on a body start.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from vgm2151fur.vgm import VgmFile

# The source rewrites at a median of ~8 VGM samples (about 5.5 kHz).  A
# 22.05 kHz body keeps the attack edges with less than 0.1 ms error and is
# half the size of a 44.1 kHz body.
PSG_DAC_RATE = 22_050
# Same chunk length the YM2612 DAC path uses.
PSG_DAC_CHUNK_SECONDS = 0.25
PSG_DAC_MIN_WRITES = 64
PSG_DAC_MIN_ZERO_PERIOD = 0.90
PSG_DAC_MAX_GAP = 256


@dataclass
class PsgDacSample:
    data: bytes
    frames: int
    rate: int


@dataclass
class PsgDacPlay:
    sample_time: int
    body: int
    chip_id: int
    voice: int


@dataclass
class PsgDacCollect:
    samples: list[PsgDacSample] = field(default_factory=list)
    plays: list[PsgDacPlay] = field(default_factory=list)
    voices: list[tuple[int, int]] = field(default_factory=list)
    rate: int = PSG_DAC_RATE
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _attenuation_amplitude(att: int, base: int) -> float:
    """SN76489 2 dB/step curve, centred on the stream's idle attenuation."""
    return 2.0 ** (-(att & 0x0F) / 3.0) - 2.0 ** (-(base & 0x0F) / 3.0)


def _render(
    writes: list[tuple[int, int]],
    total_samples: int,
    base: int,
    rate: int,
) -> bytearray:
    """Hold each attenuation write until the next one at `rate` frames/s.

    Furnace's 4-bit depth decodes a nibble linearly to signed 16-bit, so the
    nonlinear 2 dB attenuation curve is baked into the nibble value: 8 is
    the idle/zero level and 0/15 are the negative/positive peaks.
    """
    frames = max(1, int(math.ceil(total_samples * rate / 44100.0)))
    out = bytearray(frames)
    peaks = [
        abs(_attenuation_amplitude(att, base)) for att in range(16)
    ]
    scale = 7.0 / max(peaks or [1.0])
    idx = 0
    cur = base
    for n in range(frames):
        t = n * 44100.0 / rate
        while idx < len(writes) and writes[idx][0] <= t:
            cur = writes[idx][1]
            idx += 1
        value = 8 + int(round(_attenuation_amplitude(cur, base) * scale))
        out[n] = max(0, min(15, value))
    return out


def _pack(values: bytes) -> bytes:
    """Pack 4-bit values high nibble first, as Furnace's depth-4 decoder reads."""
    data = bytearray((len(values) + 1) // 2)
    for i, value in enumerate(values):
        if i & 1:
            data[i >> 1] |= value & 0x0F
        else:
            data[i >> 1] = (value & 0x0F) << 4
    return bytes(data)


def _chunks(
    frames: bytearray, loop_frame: int, chunk_frames: int,
) -> list[tuple[int, int]]:
    """Split [first non-idle, last non-idle) at loop and chunk boundaries."""
    first = next((i for i, v in enumerate(frames) if v != 8), len(frames))
    last = len(frames) - next(
        (i for i, v in enumerate(reversed(frames)) if v != 8), len(frames)
    )
    if first >= last:
        return []
    bounds = {first, last}
    pos = first
    while pos < last:
        pos += chunk_frames
        if pos < last:
            bounds.add(pos)
    if first < loop_frame < last:
        bounds.add(loop_frame)
    points = sorted(bounds)
    return [(a, b) for a, b in zip(points, points[1:]) if b > a]


def collect_psg_dac(
    vgm: VgmFile, *, rate: int = PSG_DAC_RATE, lossy: bool = False,
) -> PsgDacCollect:
    """Find SN76489 DAC voices and turn their streams into PCM bodies."""
    out = PsgDacCollect(rate=rate)
    by_chip: dict[int, list] = defaultdict(list)
    for w in vgm.writes:
        if w.chip == "sn76489":
            by_chip[w.chip_id].append(w)
    if not by_chip:
        return out

    total = vgm.total_samples or 0
    if not total:
        return out
    chunk_frames = max(1, int(round(rate * PSG_DAC_CHUNK_SECONDS)))
    loop_frame = (
        int(round((vgm.loop_sample or 0) * rate / 44100.0))
        if vgm.loop_sample else 0
    )
    used_voices: list[tuple[int, int]] = []
    body_index: dict[tuple[int, bytes], int] = {}

    for chip_id in sorted(by_chip):
        writes = by_chip[chip_id]
        freq = [0, 0, 0]
        freq_high = [False, False, False]
        latch = 0
        latch_vol = False
        volume: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        for w in writes:
            b = w.val & 0xFF
            t = w.sample
            if b & 0x80:
                latch = (b >> 5) & 3
                latch_vol = bool(b & 0x10)
                if latch_vol and latch < 3:
                    period = freq[latch] if freq_high[latch] else 0
                    volume[latch].append((t, b & 0x0F, period))
            else:
                if latch_vol or latch == 3:
                    continue
                freq[latch] = (
                    (freq[latch] & 0x00F) | ((b & 0x3F) << 4)
                )
                freq_high[latch] = True

        for voice in range(3):
            entries = volume.get(voice, [])
            if len(entries) < PSG_DAC_MIN_WRITES:
                continue
            zero_period = sum(1 for _t, _a, p in entries if p == 0)
            if zero_period / len(entries) < PSG_DAC_MIN_ZERO_PERIOD:
                continue
            times = [t for t, _a, _p in entries]
            gaps = sorted(
                b - a for a, b in zip(times, times[1:]) if b > a
            )
            if not gaps:
                continue
            if gaps[len(gaps) // 2] > PSG_DAC_MAX_GAP:
                continue
            dac = [
                (t, att) for t, att, period in entries if period == 0
            ]
            if len(dac) < PSG_DAC_MIN_WRITES:
                continue
            held: Counter = Counter()
            for i, (t, att) in enumerate(dac):
                end = dac[i + 1][0] if i + 1 < len(dac) else total
                held[att] += max(0, end - t)
            base = held.most_common(1)[0][0]
            frames = _render(dac, total, base, rate)
            pieces = _chunks(frames, loop_frame, chunk_frames)
            if not pieces:
                continue
            used_voices.append((chip_id, voice))
            for a, b in pieces:
                values = bytes(frames[a:b])
                body = _pack(values)
                key = (len(values), body)
                idx = body_index.get(key)
                if idx is None:
                    idx = len(out.samples)
                    body_index[key] = idx
                    out.samples.append(PsgDacSample(body, len(values), rate))
                out.plays.append(PsgDacPlay(
                    sample_time=int(round(a * 44100.0 / rate)),
                    body=idx,
                    chip_id=chip_id,
                    voice=voice,
                ))
                out.stats["psg_dac_bodies"] = len(out.samples)
                out.stats["psg_dac_bytes"] = sum(
                    len(s.data) for s in out.samples
                )
                out.stats["psg_dac_plays"] = len(out.plays)

    out.voices = used_voices
    if out.voices:
        out.stats["psg_dac_voices"] = len(out.voices)
        out.stats["psg_dac_rate"] = rate
        out.stats["psg_dac_chunks"] = len(out.plays)
    return out
