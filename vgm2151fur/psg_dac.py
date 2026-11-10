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
# A run this long at the exact idle attenuation starts a new attack sample.
# At 22.05 kHz this is about 11.6 ms; shorter gaps are waveform motion or
# noisy fragments that belong in the previous sample's tail.
PSG_DAC_REST_FRAMES = 256
# Do not spend a body on a fragment shorter than 128 packed bytes (256
# frames).  At 4-bit amplitude resolution these micro-fragments are noise,
# not useful independent attack samples, so they stay in the tail above.
PSG_DAC_MIN_BODY_FRAMES = 256
# Similarity clustering for reused attacks.  Compare a substantial opening
# window (0.19 s) with a small alignment tolerance; keep the longest body.
PSG_DAC_SIMILAR_FRAMES = 4096
PSG_DAC_SIMILAR_SHIFT = 8
PSG_DAC_SIMILAR_STEP = 4
PSG_DAC_SIMILAR_MAD = 0.6
# Attacks inside a continuous stream.  A short-window envelope that rises
# out of a quiet lookback is a new hit even with no silent rest between.
PSG_DAC_ATTACK_WINDOW = 64
PSG_DAC_ATTACK_LOOKBACK = 256
PSG_DAC_ATTACK_HIGH = 1.4
PSG_DAC_ATTACK_LOW = 0.5
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
    # Frames this event actually owns in the source.  A longer bank body
    # reuses a shorter event as a prefix, so playback must be cut here when
    # no following note would do it.
    frames: int = 0


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


def _segments(
    frames: bytearray, loop_frame: int,
) -> list[tuple[int, int, int]]:
    """Split at rests so every body starts on an attack transient.

    Returns ``(start, end, audible_end)``.  ``end`` carries the body's
    decay/silent tail to the next attack; ``audible_end`` is where the
    source event reached its rest.  A shorter event that reuses a longer
    bank body is cut at ``audible_end``, so the longer body's unrelated tail
    cannot play into the rest.
    """
    first = next((i for i, v in enumerate(frames) if v != 8), len(frames))
    if first >= len(frames):
        return []
    amplitude = [abs(v - 8) for v in frames]
    prefix = [0]
    for value in amplitude:
        prefix.append(prefix[-1] + value)

    def envelope(at: int) -> float:
        end = min(at + PSG_DAC_ATTACK_WINDOW, len(amplitude))
        if end <= at:
            return 0.0
        return (prefix[end] - prefix[at]) / (end - at)

    pieces: list[tuple[int, int, int]] = []
    start = first
    quiet_start = 0
    quiet = 0
    for i in range(first, len(frames)):
        if frames[i] == 8:
            if quiet == 0:
                quiet_start = i
            quiet += 1
            continue
        if quiet >= PSG_DAC_REST_FRAMES:
            # The previous body keeps the rest/decay tail all the way to this
            # attack.  Its audible part ended where the idle run began.
            if i > start:
                pieces.append((start, i, quiet_start))
            start = i
        elif (
            i - start >= PSG_DAC_MIN_BODY_FRAMES
            and i % 16 == 0
            and envelope(i) >= PSG_DAC_ATTACK_HIGH
        ):
            quiet_before = min(
                (
                    envelope(j)
                    for j in range(
                        max(start, i - PSG_DAC_ATTACK_LOOKBACK), i, 32,
                    )
                ),
                default=PSG_DAC_ATTACK_HIGH,
            )
            if quiet_before <= PSG_DAC_ATTACK_LOW:
                pieces.append((start, i, i))
                start = i
        quiet = 0
    if start < len(frames):
        if quiet >= PSG_DAC_REST_FRAMES:
            pieces.append((start, len(frames), quiet_start))
        else:
            pieces.append((start, len(frames), len(frames)))
    # Fold micro-attacks into the previous body's tail.  A chain of short
    # fragments extends that body until a real body is reached.
    merged: list[tuple[int, int, int]] = []
    for a, b, audible in pieces:
        if audible - a < PSG_DAC_MIN_BODY_FRAMES and merged:
            prev_a, _prev_b, _prev_audible = merged[-1]
            # The fragment stays inside the previous body's audible tail; it
            # is not a separate attack, but it must not be muted away either.
            merged[-1] = (prev_a, b, audible)
        else:
            merged.append((a, b, audible))
    if (
        len(merged) > 1
        and merged[0][2] - merged[0][0] < PSG_DAC_MIN_BODY_FRAMES
    ):
        merged[1] = (merged[0][0], merged[1][1], merged[1][2])
        merged.pop(0)
    pieces = merged
    if first < loop_frame < len(frames):
        split: list[tuple[int, int, int]] = []
        for a, b, audible in pieces:
            if a < loop_frame < b:
                split.append((a, loop_frame, min(audible, loop_frame)))
                split.append((loop_frame, b, max(audible, loop_frame)))
            else:
                split.append((a, b, audible))
        pieces = split
    return [(a, b, audible) for a, b, audible in pieces if b > a]


def _tail_is_quiet(data: bytes, start: int) -> bool:
    """True when everything past `start` is the idle nibble."""
    return all(v == 8 for v in data[start:])


def _min_audible(plays: list[PsgDacPlay]) -> dict[int, int]:
    """Shortest source event that uses each body."""
    out: dict[int, int] = {}
    for play in plays:
        old = out.get(play.body)
        if old is None or play.frames < old:
            out[play.body] = play.frames
    return out


def _share_prefix_samples(
    samples: list[PsgDacSample], plays: list[PsgDacPlay], stats: dict,
) -> None:
    """Keep the largest body when a shorter attack is its prefix.

    The next note cuts a long body at the source's attack, so a shorter hit
    can safely reuse a longer variant.  This is the "largest variant wins"
    rule the YM2612 attack splitter uses; it also makes sure a quiet decay
    tail is present when no following note arrives.
    """
    by_head: dict[tuple[int, bytes], list[int]] = {}
    for i, smp in enumerate(samples):
        if smp.data:
            by_head.setdefault((smp.rate, smp.data[:2]), []).append(i)
    remap = list(range(len(samples)))
    audible = _min_audible(plays)
    reused = 0
    for indexes in by_head.values():
        if len(indexes) < 2:
            continue
        indexes.sort(key=lambda i: samples[i].frames, reverse=True)
        for pos, i in enumerate(indexes):
            need = audible.get(i, samples[i].frames)
            for j in indexes[:pos]:
                longer = samples[j].data
                shorter = samples[i].data
                if (
                    longer.startswith(shorter)
                    and _tail_is_quiet(longer, need)
                ):
                    remap[i] = j
                    reused += 1
                    break
    if not reused:
        return
    for i in range(len(remap)):
        while remap[i] != remap[remap[i]]:
            remap[i] = remap[remap[i]]
    for play in plays:
        play.body = remap[play.body]
    used = {play.body for play in plays}
    compact: list[PsgDacSample] = []
    compact_map: dict[int, int] = {}
    for i, smp in enumerate(samples):
        if i not in used:
            continue
        compact_map[i] = len(compact)
        compact.append(smp)
    samples[:] = compact
    for play in plays:
        play.body = compact_map[play.body]
    stats["psg_dac_prefix_reused"] = reused


def _best_similarity(a: bytes, b: bytes, limit: int) -> float:
    """Smallest mean code difference over small start-time shifts."""
    best = 99.0
    for shift in range(-PSG_DAC_SIMILAR_SHIFT, PSG_DAC_SIMILAR_SHIFT + 1):
        aa = a[shift:] if shift >= 0 else a
        bb = b if shift >= 0 else b[-shift:]
        n = min(len(aa), len(bb), limit)
        if n <= 0:
            continue
        total = 0
        count = 0
        for k in range(0, n, PSG_DAC_SIMILAR_STEP):
            total += abs(aa[k] - bb[k])
            count += 1
        if count:
            score = total / count
            if score < best:
                best = score
                if best <= PSG_DAC_SIMILAR_MAD:
                    break
    return best


def _share_similar_samples(
    samples: list[PsgDacSample], plays: list[PsgDacPlay], stats: dict,
) -> None:
    """Bank similar attacks onto the longest body.

    Exact prefixes already share a body.  This pass catches the same hit with
    a one- or two-frame phase difference, which is common in the 4-bit
    stream.  The longest variant wins because the next note cuts its tail at
    the source's attack.
    """
    if len(samples) < 2:
        return
    order = sorted(
        range(len(samples)), key=lambda i: samples[i].frames, reverse=True,
    )
    audible = _min_audible(plays)
    anchors: list[int] = []
    remap = list(range(len(samples)))
    for i in order:
        limit = min(samples[i].frames, PSG_DAC_SIMILAR_FRAMES)
        need = audible.get(i, samples[i].frames)
        for anchor in anchors:
            # Only borrow a longer body when its extra tail is pure silence.
            # An audible tail belongs to a different event; keeping this
            # shorter body is preferable to muting a wrong tail later.
            if not _tail_is_quiet(samples[anchor].data, need):
                continue
            if _best_similarity(
                samples[i].data, samples[anchor].data, limit,
            ) <= PSG_DAC_SIMILAR_MAD:
                remap[i] = anchor
                break
        else:
            anchors.append(i)
    if len(anchors) == len(samples):
        return
    for i in range(len(remap)):
        while remap[i] != remap[remap[i]]:
            remap[i] = remap[remap[i]]
    for play in plays:
        play.body = remap[play.body]
    used = {play.body for play in plays}
    compact: list[PsgDacSample] = []
    compact_map: dict[int, int] = {}
    for i, smp in enumerate(samples):
        if i not in used:
            continue
        compact_map[i] = len(compact)
        compact.append(smp)
    samples[:] = compact
    for play in plays:
        play.body = compact_map[play.body]
    stats["psg_dac_similar_reused"] = sum(
        1 for i, r in enumerate(remap) if i != r
    )


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
            pieces = _segments(frames, loop_frame)
            if not pieces:
                continue
            used_voices.append((chip_id, voice))
            for a, b, audible in pieces:
                values = bytes(frames[a:b])
                if not any(v != 8 for v in values):
                    # A body must start on an audible attack.
                    continue
                key = (len(values), values)
                idx = body_index.get(key)
                if idx is None:
                    idx = len(out.samples)
                    body_index[key] = idx
                    out.samples.append(PsgDacSample(values, len(values), rate))
                out.plays.append(PsgDacPlay(
                    sample_time=int(round(a * 44100.0 / rate)),
                    body=idx,
                    chip_id=chip_id,
                    voice=voice,
                    frames=max(1, audible - a),
                ))
                out.stats["psg_dac_bodies"] = len(out.samples)
                out.stats["psg_dac_plays"] = len(out.plays)

    if len(out.samples) > 1:
        _share_prefix_samples(out.samples, out.plays, out.stats)
        _share_similar_samples(out.samples, out.plays, out.stats)

    out.voices = used_voices
    if out.voices:
        out.stats["psg_dac_voices"] = len(out.voices)
        out.stats["psg_dac_rate"] = rate
        out.stats["psg_dac_chunks"] = len(out.plays)
        out.stats["psg_dac_bodies"] = len(out.samples)
        out.stats["psg_dac_bytes"] = sum(
            (s.frames + 1) // 2 for s in out.samples
        )
    return out
