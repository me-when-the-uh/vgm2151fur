"""SSG/PSG PCM playback -> sample instruments.

AY-3-8910, YM2149 and the SSG inside the YM2203/YM2608/YM2610 can play raw
samples without any sample hardware: the driver disables the voice's tone and
noise in the mixer and writes the volume register at audio rate.  The volume
register then acts as a 4-bit DAC.

This module turns such a stream into ordinary 8-bit PCM bodies plus one note
per segment.  Furnace's AY platform plays them through a ``DIV_INS_AY``
instrument with ``amiga.useSample`` set (the reference ``SSGPCM-test.fur``
uses exactly this shape), at note 108 = C-4, where the sample's stored
``c4_rate`` is played 1:1.

The level -> byte map is the inverse of Furnace's ``dacLogTableAY``
(``platform/ay.cpp``).  ``runDAC`` computes the register level as
``dacLogTableAY[sampleByte ^ 0x80]``, so picking the byte whose table entry is
the source level reproduces the source register stream exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from vgm2151fur.vgm import VgmFile

# Furnace's AY sample DAC table (platform/ay.cpp).  Index is the sample byte
# XOR 0x80, value is the 4-bit volume register level.
DAC_LOG_TABLE_AY = (
    0, 2, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 5,
    6, 6, 6, 6, 6, 6, 6, 6, 6, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7, 7, 7, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8,
    8, 8, 8, 8, 8, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9,
    9, 9, 9, 9, 9, 9, 9, 9, 10, 10, 10, 10, 10, 10, 10, 10,
    10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 11,
    11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11,
    11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 12, 12, 12, 12, 12, 12,
    12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12,
    12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 13, 13, 13, 13, 13, 13,
    13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13,
    13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 14,
    14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14,
    14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 14,
    14, 14, 14, 14, 14, 14, 14, 14, 14, 14, 15, 15, 15, 15, 15, 15,
    15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15,
)

# First table index that produces each register level.  Level 1 is not in the
# table (it jumps 0 -> 2); map it to level 2 rather than silencing it.
_LEVEL_BYTE = [0] * 16
for _level in range(16):
    for _i, _v in enumerate(DAC_LOG_TABLE_AY):
        if _v == _level:
            _LEVEL_BYTE[_level] = _i
            break
_LEVEL_BYTE[1] = _LEVEL_BYTE[2]

# The volume-register level is the DAC value only while tone and noise are off
# and the hardware envelope is not driving the channel.
REG_VOL = (8, 9, 10)
REG_MIXER = 7

# A voice is PCM when the register writes are this close together.  A normal
# tone driver writes the volume at most once a frame (~735 samples); these
# streams write every 2-16 samples.
PCM_MAX_GAP = 64
PCM_MAX_MEDIAN_DT = 16
PCM_MIN_WRITES = 8
# Split a PCM window into samples at pauses longer than this (46 ms).  This
# separates drum hits while keeping a continuous stream in one body.
PCM_SPLIT_GAP = 2048
# Give up per-hit detail before the module runs out of instrument slots.
PCM_INSTRUMENT_BUDGET = 160
# The rate is the source write rate; clamp to sane sample rates.
PCM_MIN_RATE = 4000
PCM_MAX_RATE = 32000
VGM_RATE = 44100
# VGM DAC-stream (0x90-0x95) chip types that can name an SSG volume register.
# These are the header chip IDs the stream setup uses: YM2203, YM2608,
# YM2610 and the stand-alone AY-8910.
STREAM_CHIP = {
    0x06: "ym2203_ssg",
    0x07: "ym2608_ssg",
    0x08: "ym2610_ssg",
    0x12: "ay8910",
}
# A body shorter than this is a click or a segmentation fragment, not a
# sample.  Short segments are folded into the next one on the same voice so
# the source audio is kept instead of disappearing.
PCM_MIN_FRAMES = 512


@dataclass
class SsgPcmSample:
    """One 8-bit body and the register timing it came from."""

    name: str
    data: bytes
    rate: int
    start: int
    end: int
    voice: int = 0
    chip_id: int = 0


@dataclass
class SsgPcmHit:
    """One note that plays a body from the start."""

    sample_time: int
    end_time: int
    voice: int
    chip_id: int
    sample_index: int


def _dac_level(val: int) -> int:
    return val & 0x0F


def _volume_to_byte(level: int) -> int:
    """Sample byte that ``runDAC`` maps back to this register level."""
    return _LEVEL_BYTE[max(0, min(15, level))] ^ 0x80


def _dac_mode(mixer: int, voice: int, val: int) -> bool:
    """True while the voice's volume register is a DC DAC level."""
    tone_off = bool(mixer & (1 << voice))
    noise_off = bool(mixer & (1 << (voice + 3)))
    return tone_off and noise_off and not (val & 0x10)


def _volume_streams(
    vgm: VgmFile, chip: str,
) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """Per (chip instance, voice), the writes that landed in DAC mode.

    Each entry is ``(sample, level)``.  A later write at the same sample wins;
    the hardware only sounds the last one.
    """
    streams: dict[tuple[int, int], list[tuple[int, int]]] = {}
    state: dict[int, list[int]] = {}
    for w in vgm.writes:
        if w.chip != chip or w.reg > 0x0F:
            continue
        cid = w.chip_id
        reg = state.get(cid)
        if reg is None:
            reg = [0] * 16
            reg[REG_MIXER] = 0x38  # reset: tone on, noise off
            state[cid] = reg
        reg[w.reg] = w.val & 0xFF
        if w.reg not in REG_VOL:
            continue
        voice = REG_VOL.index(w.reg)
        if not _dac_mode(reg[REG_MIXER], voice, w.val):
            continue
        key = (cid, voice)
        times = streams.setdefault(key, [])
        # Same-sample writes: keep only the final value.
        if times and times[-1][0] == w.sample:
            times[-1] = (w.sample, _dac_level(w.val))
        else:
            times.append((w.sample, _dac_level(w.val)))
    return streams


def _stream_body(vgm: VgmFile, play) -> bytes:
    """The full byte range a 0x93 stream start plays out of the data bank.

    Mode 1 counts bytes, mode 2 milliseconds of the 0x92 rate and mode 3 the
    rest of the bank.  The stream is used in its entirety: a 2,000-byte slice
    is 2,000 register writes, not one block.
    """
    bank = bytes(vgm.dac_data)
    if not bank:
        return b""
    start = max(0, min(len(bank), play.offset))
    mode = play.mode & 0x07
    if mode == 1:
        end = start + max(0, play.length)
    elif mode == 2:
        rate = play.rate or VGM_RATE
        end = start + max(0, int(round(play.length * rate / 1000.0)))
    elif mode == 3:
        end = len(bank)
    else:
        return b""
    step = play.step if play.step > 1 else 1
    body = bank[start:min(end, len(bank)):step]
    return body[::-1] if play.reverse else body


def _stream_points(
    vgm: VgmFile, chip: str,
) -> tuple[dict[tuple[int, int], list[tuple[int, int]]], dict[int, list[int]], int]:
    """Expand DAC-stream plays that write an SSG volume register.

    Returns per-(chip instance, voice) ``(sample, level)`` points, the
    declared stream rate per instance, and a count of looping streams.
    """
    out: dict[tuple[int, int], list[tuple[int, int]]] = {}
    rates: dict[int, list[int]] = {}
    loops = 0
    if not vgm.dac_stream_plays or not vgm.dac_data:
        return out, rates, loops
    for play in vgm.dac_stream_plays:
        if STREAM_CHIP.get(play.chip_type) != chip:
            continue
        reg = play.reg & 0x0F
        if reg not in REG_VOL:
            continue
        body = _stream_body(vgm, play)
        if not body:
            continue
        voice = REG_VOL.index(reg)
        rate = play.rate or VGM_RATE
        rates.setdefault(play.chip_id, []).append(rate)
        if play.loop:
            loops += 1
        step = VGM_RATE / rate
        times = out.setdefault((play.chip_id, voice), [])
        for i, byte in enumerate(body):
            times.append((int(round(play.sample + i * step)), byte & 0x0F))
    for times in out.values():
        times.sort()
    return out, rates, loops


def _bursts(
    points: list[tuple[int, int]],
    max_gap: int,
    min_writes: int,
    max_median_dt: int,
) -> list[list[tuple[int, int]]]:
    """Group a voice's DAC writes into high-rate bursts."""
    out: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    prev = None
    for point in points:
        if prev is not None and point[0] - prev <= max_gap:
            cur.append(point)
        else:
            if len(cur) >= min_writes and _median_dt(cur) <= max_median_dt:
                out.append(cur)
            cur = [point]
        prev = point[0]
    if len(cur) >= min_writes and _median_dt(cur) <= max_median_dt:
        out.append(cur)
    return out


def _median_dt(points: list[tuple[int, int]]) -> int:
    if len(points) < 2:
        return 0
    dts = sorted(
        points[i + 1][0] - points[i][0] for i in range(len(points) - 1)
    )
    return dts[len(dts) // 2]


def _merge_bursts(
    bursts: list[list[tuple[int, int]]], split_gap: int,
) -> list[list[tuple[int, int]]]:
    """Join bursts separated by less than ``split_gap``.

    The fast-run grouper cuts at every gap over ``PCM_MAX_GAP``; this puts the
    short holes back so a continuous stream stays one body while a real pause
    starts a new sample.  It also gives the instrument budget something to
    turn: a larger gap merges neighbouring hits.
    """
    out: list[list[tuple[int, int]]] = []
    for burst in bursts:
        if burst and out and burst[0][0] - out[-1][-1][0] < split_gap:
            out[-1].extend(burst)
        elif burst:
            out.append(list(burst))
    return [seg for seg in out if len(seg) >= 2]


def _rate_for(points: list[tuple[int, int]]) -> int:
    """Playback rate from the typical write spacing.

    The nominal spacing carries the high-frequency content; a pause merely
    holds the current level and must not lower the sample rate.  The resample
    still spans the real timestamps, so the duration is unchanged.
    """
    median = _median_dt(points)
    if median <= 0:
        return VGM_RATE
    rate = int(round(VGM_RATE / median))
    return max(PCM_MIN_RATE, min(PCM_MAX_RATE, rate))


def _shared_rate(segments: list[list[tuple[int, int]]]) -> int:
    rates = sorted(_rate_for(seg) for seg in segments if len(seg) >= 2)
    if not rates:
        return VGM_RATE
    return rates[len(rates) // 2]


def _resample(points: list[tuple[int, int]], rate: int) -> bytes:
    """Sample-and-hold the register values on a uniform timeline."""
    t0 = points[0][0]
    t1 = points[-1][0]
    span = max(1, t1 - t0)
    frames = max(1, int(round(span * rate / VGM_RATE)) + 1)
    out = bytearray(frames)
    i = 0
    cur = points[0][1]
    step = VGM_RATE / rate
    for k in range(frames):
        t = t0 + k * step
        while i + 1 < len(points) and points[i + 1][0] <= t:
            i += 1
            cur = points[i][1]
        out[k] = _volume_to_byte(cur)
    return bytes(out)


def _body_frames(seg: list[tuple[int, int]], rate: int) -> int:
    span = seg[-1][0] - seg[0][0]
    return max(1, int(round(span * rate / VGM_RATE)) + 1)


def _merge_short(
    segments: list[list[tuple[int, int]]], rate: int,
) -> list[list[tuple[int, int]]]:
    """Fold sub-minimum segments into the next one on the same voice.

    A 2 ms fragment is not worth an instrument, but dropping it would lose
    the hit.  Extending the next body back to the fragment's start keeps the
    source audio (including the pause between them) in one playable sample.
    If the last segment is still short it joins the previous one; a lone
    short segment is dropped.
    """
    out: list[list[tuple[int, int]]] = []
    i = 0
    while i < len(segments):
        seg = list(segments[i])
        while _body_frames(seg, rate) < PCM_MIN_FRAMES and i + 1 < len(segments):
            i += 1
            seg.extend(segments[i])
        out.append(seg)
        i += 1
    if len(out) >= 2 and _body_frames(out[-1], rate) < PCM_MIN_FRAMES:
        out[-2].extend(out[-1])
        out.pop()
    return [seg for seg in out if _body_frames(seg, rate) >= PCM_MIN_FRAMES]


def _dedupe(
    samples: list[SsgPcmSample], hits: list[SsgPcmHit],
) -> tuple[list[SsgPcmSample], list[SsgPcmHit], int]:
    """Share identical bodies.  Returns (samples, hits, shared)."""
    by_body: dict[tuple[int, bytes], int] = {}
    out: list[SsgPcmSample] = []
    remap: dict[int, int] = {}
    shared = 0
    for i, smp in enumerate(samples):
        key = (smp.rate, smp.data)
        idx = by_body.get(key)
        if idx is None:
            idx = len(out)
            by_body[key] = idx
            out.append(smp)
        else:
            shared += 1
        remap[i] = idx
    for hit in hits:
        hit.sample_index = remap[hit.sample_index]
    return out, hits, shared


def collect_ssg_pcm(
    vgm: VgmFile,
    *,
    chip: str = "ay8910",
) -> tuple[list[SsgPcmSample], list[SsgPcmHit], list[str]]:
    """Extract PCM-played SSG voices into 8-bit sample bodies.

    Returns ``(samples, hits, warnings)``.  One sample per segment, one hit per
    segment start; the caller maps ``voice``/``chip_id`` to a module channel.
    """
    warnings: list[str] = []
    direct = _volume_streams(vgm, chip)
    streamed, stream_rates, stream_loops = _stream_points(vgm, chip)
    if not direct and not streamed:
        return [], [], warnings

    streams: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for key, points in direct.items():
        streams.setdefault(key, []).extend(points)
    for key, points in streamed.items():
        streams.setdefault(key, []).extend(points)
    for points in streams.values():
        points.sort()
        # Same-sample writes: the last value is the one that sounds.
        deduped: list[tuple[int, int]] = []
        for point in points:
            if deduped and deduped[-1][0] == point[0]:
                deduped[-1] = point
            else:
                deduped.append(point)
        points[:] = deduped

    # Per chip instance: gather bursts, pick a rate, split into segments, and
    # fall back to a coarser split if the instrument budget would be exceeded.
    per_chip: dict[int, list[tuple[int, list[tuple[int, int]]]]] = {}
    for (cid, voice), points in streams.items():
        per_chip.setdefault(cid, []).append((voice, points))

    def build(cid: int, split_gap: int):
        segments: list[tuple[int, list[tuple[int, int]]]] = []
        for voice, points in per_chip[cid]:
            bursts = _bursts(
                points, PCM_MAX_GAP, PCM_MIN_WRITES, PCM_MAX_MEDIAN_DT,
            )
            for seg in _merge_bursts(bursts, split_gap):
                segments.append((voice, seg))
        return segments

    all_segments: list[tuple[int, int, list[tuple[int, int]]]] = []
    rate_by_cid: dict[int, int] = {}
    for cid in sorted(per_chip):
        gap = PCM_SPLIT_GAP
        while True:
            segments = build(cid, gap)
            if len(segments) <= PCM_INSTRUMENT_BUDGET:
                break
            if gap >= 1 << 20:
                break
            gap *= 2
        # A DAC stream states its own byte rate; use it rather than inferring
        # from the 44.1 kHz-rounded timestamps.  Direct-write streams fall
        # back to the median spacing.
        if stream_rates.get(cid):
            declared = sorted(stream_rates[cid])
            rate = max(
                PCM_MIN_RATE,
                min(PCM_MAX_RATE, int(round(declared[len(declared) // 2]))),
            )
        else:
            rate = _shared_rate([seg for _v, seg in segments])
        rate_by_cid[cid] = rate
        for voice, seg in segments:
            all_segments.append((cid, voice, seg))

    # Fold fragments under the minimum length into their neighbours, per
    # voice so two simultaneous streams never join.
    by_voice: dict[tuple[int, int], list[list[tuple[int, int]]]] = {}
    for cid, voice, seg in all_segments:
        by_voice.setdefault((cid, voice), []).append(seg)
    merged_segments: list[tuple[int, int, list[tuple[int, int]]]] = []
    for (cid, voice), segs in by_voice.items():
        for seg in _merge_short(segs, rate_by_cid[cid]):
            merged_segments.append((cid, voice, seg))

    samples: list[SsgPcmSample] = []
    hits: list[SsgPcmHit] = []
    for cid, voice, seg in merged_segments:
        rate = rate_by_cid[cid]
        data = _resample(seg, rate)
        if len(data) < 2:
            continue
        idx = len(samples)
        samples.append(SsgPcmSample(
            name=f"SSG {cid:02d} {voice + 1} {seg[0][0]:08x}",
            data=data,
            rate=rate,
            start=seg[0][0],
            end=seg[-1][0],
            voice=voice,
            chip_id=cid,
        ))
        hits.append(SsgPcmHit(
            sample_time=seg[0][0],
            end_time=seg[-1][0],
            voice=voice,
            chip_id=cid,
            sample_index=idx,
        ))

    if not samples:
        return [], [], warnings
    samples, hits, shared = _dedupe(samples, hits)
    hits.sort(key=lambda h: (h.sample_time, h.chip_id, h.voice))
    label = {
        "ay8910": "AY-8910",
        "ym2203_ssg": "YM2203 SSG",
        "ym2608_ssg": "YM2608 SSG",
        "ym2610_ssg": "YM2610 SSG",
    }.get(chip, chip)
    warnings.append(
        f"{label}: {len(hits)} PCM sample note(s) in {len(samples)} "
        f"8-bit bod{'y' if len(samples) == 1 else 'ies'}"
        + (f" ({shared} shared)" if shared else "")
    )
    if stream_loops:
        warnings.append(
            f"{label}: {stream_loops} looping DAC stream(s) played once"
        )
    return samples, hits, warnings
