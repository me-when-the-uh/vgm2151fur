"""YM2612 DAC: rate-exact PCM chunks (default), editable slices, or the old timeline.

The default emits one FM6 note per audio chunk. `slices=True` keeps the per-run
slice representation; `timeline=True` is the old comparison body.
"""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass

from vgm2151fur.timing import place_tick
from vgm2151fur.vgm import (
    VGM_RATE, DAC_STREAM_YM2612, DacStreamPlay, VgmFile,
)

DAC_GAP = 512
DAC_MIN_SAMPLES = 8
DAC_MAP_CAPACITY = 120
# A pause over this starts another phrase.
DAC_RUN_GAP = 2048
DAC_TIMELINE_RATE = 22050
# Keep the module below release Furnace's sample-count limit.
DAC_RELEASE_SAMPLES = 240
# Audio chunk length; short sources stay one chunk.
DAC_CHUNK_SECONDS = 0.25
# Furnace clamps a sample-playback rate to this value.
DAC_RATE_MAX = 192000
DAC_RATE_MIN = 1
# Bank samples whose bodies are the same audio at a small frame offset share
# one module sample. Rate and length are compared loosely, but the interior of
# the body must match almost exactly; only the leading/trailing fill may move.
DAC_SIMILAR_RATE = 32
DAC_SIMILAR_SHIFT = 16
DAC_SIMILAR_EDGE = 128
DAC_SIMILAR_MIN_MATCH = 0.90
DAC_SIMILAR_INTERIOR = 0.999
DAC_SIMILAR_MIN_LEN = 32
# A unique sample that starts this soon after the previous unique sample is
# part of the same contiguous one-shot phrase.
DAC_ONESHOT_GAP = 64
# A final body at least this long whose longest silent stretch is over 40%
# of it is split at that stretch.  Bank runs split earlier at any held
# silence of DAC_RUN_GAP samples, so a kick does not carry a delayed snare.
DAC_SILENCE_MIN_LEN = 8192
DAC_SILENCE_FRACTION = 0.40
# A signed DAC body whose peak is at or below this is effectively inaudible.
# A body where every byte is the same is a flat DC line, not a sample.  Both
# are dropped before they take a sample-map slot: the source driver often
# leaves a few bytes of near-zero noise between real hits.
DAC_QUIET_PEAK = 2
# The fallback path splits the word stream at silent holes instead of cutting
# fixed 0.25 s chunks.  A zero hold this long (in VGM samples) is a rest
# between hits, so the next audio starts a new candidate body.  A short dip
# inside a waveform does not.
DAC_ATTACK_SILENCE = 64
# A candidate shorter than this many frames is a fragment, not a playable
# drum hit.  Loud one-frame clicks still pass the amplitude test above.
DAC_ATTACK_MIN_FRAMES = 2
# A E5-like amplitude jump after this much near-silence is also an attack,
# even when the source never wrote an exact zero.  The thresholds are in
# signed 8-bit units and VGM samples.
DAC_ATTACK_QUIET_PEAK = 4
DAC_ATTACK_JUMP = 24
DAC_ATTACK_QUIET_SAMPLES = 32
# Keep the result inside this many sample-map slots.  Samples always start at
# frame 0, so a short fragment can only reuse a body whose opening frames
# sound like it.  The first DAC_HOT_ANCHORS most-used bodies always stay; the
# remaining slots go to the longest bodies, so a long one-shot beats a pile
# of micro-fragments.  Everything else maps to the closest anchor by a
# deliberately loose prefix comparison: these are fragments the row grid
# cannot resolve anyway, and the note's own span still cuts the shared body
# at the right time.
DAC_TARGET_SAMPLES = 32
DAC_HOT_ANCHORS = 16
DAC_FRAGMENT_LIMIT = 128
DAC_FRAGMENT_SHIFT = 8


@dataclass(frozen=True)
class DacSample:
    start: int
    length: int
    data: bytes  # signed 8-bit
    c4_rate: int
    onset: int


@dataclass(frozen=True)
class DacPlay:
    """One source run played on FM6.

    ``source`` marks a play whose sample is a bank segment longer than the
    run itself. A later note cuts it, so a short slice and the longer block it
    came from share one sample instead of storing a copy per length.
    """

    onset: int
    sample: int
    pan: int
    span: int
    collapsed: bool = False
    source: bool = False
    fresh: bool = False
    # Value the DAC holds after this run when the driver wrote silence next.
    # A dropped quiet run leaves ``None`` here; the body builder keeps the
    # previous payload byte instead.
    hold: int | None = None


def _signed_bank(raw: bytes) -> bytes:
    return bytes((x - 0x80) & 0xFF for x in raw)


def _body_peak(data: bytes) -> int:
    """Peak absolute signed value of an 8-bit body."""
    peak = 0
    for value in data:
        if value & 0x80:
            value = 256 - value
        if value > peak:
            peak = value
    return peak


def _audible_body(data: bytes) -> bool:
    """Whether a body carries audio rather than silence or a flat DC line."""
    if not data:
        return False
    first = data[0]
    varied = False
    for value in data[1:]:
        if value != first:
            varied = True
            break
    return varied and _body_peak(data) > DAC_QUIET_PEAK


def _pan_byte(raw: int) -> int:
    """Genesis 0xB6 pan bits to Furnace's split 4-bit pan byte."""
    left = bool(raw & 0x80)
    right = bool(raw & 0x40)
    if left and right:
        return 0xFF
    if left:
        return 0xF0
    if right:
        return 0x0F
    return 0


def _pan_timeline(vgm: VgmFile) -> list[tuple[int, int]]:
    """(sample, raw 0xB6 pan bits) for the first YM2612's channel 6.

    Channel 6 lives on the second register port, so its pan is ``$1B6``.
    Port-0 ``$B6`` is channel 3's pan and says nothing about the DAC.
    """
    out: list[tuple[int, int]] = []
    for w in vgm.writes:
        if (
            w.chip == "ym2612" and w.chip_id == 0
            and (w.reg & 0x100) and (w.reg & 0xFF) == 0xB6
        ):
            out.append((w.sample, w.val & 0xC0))
    return out


# How close a 0xB6 write must be to a DAC play to count as that hit's pan.
# The hardware DAC is mono: the register only pans FM6, and drivers that pan
# DAC drums write it right at the hit. A value left over from an FM6 voice
# should not hard-pan the drums. This is decided once per log: a driver that
# never keys FM6 has no other owner for the register, while one that does may
# leave an FM6 pan behind.
DAC_PAN_WINDOW = 128


def _fm6_is_used(vgm: VgmFile) -> bool:
    """Whether channel 6 is keyed as an FM voice while the DAC is off.

    A log that never touches FM6 has no other owner for the 0xB6 register, so
    a pan write belongs to the DAC drums. When the driver also plays FM6, its
    pan value is just channel state and must not pan the DAC.
    """
    enabled = False
    for w in vgm.writes:
        if w.chip != "ym2612" or w.chip_id != 0 or (w.reg & 0x100):
            continue
        r = w.reg & 0xFF
        if r == 0x2B:
            enabled = bool(w.val & 0x80)
        elif (r == 0x28 and (w.val & 7) == 6 and (w.val & 0xF0)
              and not enabled):
            return True
    return False


def _pan_at(timeline: list[tuple[int, int]] | None, t: int) -> int:
    """Pan byte for a DAC play at `t`, or -1 when the DAC is not panned."""
    if not timeline:
        return -1
    i = bisect.bisect_right(timeline, (t, 0xFFFF)) - 1
    raw = timeline[i][1] if i >= 0 else 0xC0
    return _pan_byte(raw)




def _dac_enable_timeline(vgm: VgmFile) -> list[tuple[int, bool]]:
    """(sample, enabled) for the first YM2612's 0x2B writes."""
    return [
        (w.sample, bool(w.val & 0x80))
        for w in vgm.writes
        if (
            w.chip == "ym2612" and w.chip_id == 0
            and not (w.reg & 0x100) and (w.reg & 0xFF) == 0x2B
        )
    ]


def _dac_enabled_at(timeline: list[tuple[int, bool]], t: int) -> bool:
    if not timeline:
        return True
    i = bisect.bisect_right(timeline, (t, True)) - 1
    return timeline[i][1] if i >= 0 else False


def _clamp_rate(rate: float, stats: dict) -> int:
    value = int(round(rate))
    if value > DAC_RATE_MAX:
        value = DAC_RATE_MAX
        stats["dac_rate_clamped"] = stats.get("dac_rate_clamped", 0) + 1
    elif value < DAC_RATE_MIN:
        value = DAC_RATE_MIN
        stats["dac_rate_clamped"] = stats.get("dac_rate_clamped", 0) + 1
    return value


def _byte_events(
    vgm: VgmFile, stats: dict, enable_tl: list[tuple[int, bool]],
) -> list[tuple[int, int, int]]:
    """(sample, signed byte, wait) for every audible DAC word, in order.

    Old bank plays carry their wait nibble.  Direct 0x2A writes have no
    explicit wait, so their hold is the time to the next DAC word, capped at
    `DAC_GAP` (a longer gap is a new run).  Pointer changes are folded into
    the bank address and must not break the stream by themselves.

    Writes while 0x2B bit 7 is clear are inaudible; the cursor still advances
    so the next enabled read lands at the source address.
    """
    bank = bytes(vgm.dac_data)
    signed = _signed_bank(bank)
    words: list[list[int]] = []
    cursor = 0
    pos = 0
    for t, kind, value in vgm.dac_events:
        if kind == 0:
            cursor = max(0, value)
            pos = 0
            continue
        if kind == 1:
            addr = cursor + pos
            pos += 1
            if addr >= len(bank):
                stats["dac_bank_overrun"] = stats.get("dac_bank_overrun", 0) + 1
                continue
            byte = signed[addr]
        elif kind == 2:
            byte = (value - 0x80) & 0xFF
        else:
            continue
        if not _dac_enabled_at(enable_tl, t):
            stats["dac_disabled_bytes"] = stats.get("dac_disabled_bytes", 0) + 1
            if byte:
                stats["dac_disabled_audible"] = stats.get("dac_disabled_audible", 0) + 1
            continue
        words.append([t, byte, value & 0x0F if kind == 1 else 0, kind])
    for i, w in enumerate(words):
        if w[3] != 2:
            continue
        nxt = words[i + 1][0] if i + 1 < len(words) else w[0]
        w[2] = max(0, min(DAC_GAP, nxt - w[0]))
    return [(w[0], w[1], w[2]) for w in words]


def _add_sample(
    samples: list[DacSample], by_key: dict[tuple[bytes, int], int],
    data: bytes, rate: int, onset: int,
) -> int:
    key = (data, rate)
    idx = by_key.get(key)
    if idx is None:
        idx = len(samples)
        samples.append(DacSample(-1, len(data), data, rate, onset))
        by_key[key] = idx
    return idx


def _collect_slices(
    vgm: VgmFile,
) -> tuple[list[DacSample], list[DacPlay], list[str], dict]:
    """Explicit per-run slice representation for editing."""
    if not vgm.dac_events and not vgm.dac_block_plays and not vgm.dac_stream_plays:
        return [], [], [], {}

    stats: dict = {}
    warnings: list[str] = []
    pan_timeline = None if _fm6_is_used(vgm) else _pan_timeline(vgm)
    enable_tl = _dac_enable_timeline(vgm)
    words = _byte_events(vgm, stats, enable_tl)
    samples: list[DacSample] = []
    by_key: dict[tuple[bytes, int], int] = {}
    plays: list[DacPlay] = []

    run: list[tuple[int, int, int]] = []

    def flush() -> None:
        nonlocal run
        if not run:
            return
        onset = run[0][0]
        last = run[-1][0]
        span = last - onset
        data = bytes(v[1] for v in run)
        count = len(data)
        stats["dac_runs"] = stats.get("dac_runs", 0) + 1
        stats["dac_source_bytes"] = stats.get("dac_source_bytes", 0) + count
        collapsed = count > 1 and len(set(data)) == 1
        if collapsed:
            data = data[:1]
            rate = VGM_RATE
            stats["dac_collapsed_bytes"] = stats.get("dac_collapsed_bytes", 0) + count
        elif count == 1:
            rate = VGM_RATE
        elif span > 0:
            rate = _clamp_rate(VGM_RATE * (count - 1) / span, stats)
        else:
            data = data[-1:]
            rate = VGM_RATE
            collapsed = True
        idx = _add_sample(samples, by_key, data, rate, onset)
        plays.append(DacPlay(onset, idx, _pan_at(pan_timeline, onset), span, collapsed))
        run = []

    i = 0
    while i < len(words):
        t, byte, wait = words[i]
        j = i + 1
        while j < len(words) and words[j][0] == t:
            j += 1
        if j - i > 1:
            # Same-time burst: only the final value sounds.
            if run:
                flush()
            kept = words[j - 1]
            if len({w[1] for w in words[i:j]}) == 1:
                stats["dac_collapsed_bytes"] = (
                    stats.get("dac_collapsed_bytes", 0) + (j - i)
                )
            else:
                stats["dac_same_time_runs"] = (
                    stats.get("dac_same_time_runs", 0) + 1
                )
                stats["dac_same_time_bytes"] = (
                    stats.get("dac_same_time_bytes", 0) + (j - i)
                )
                stats["dac_same_time_dropped"] = (
                    stats.get("dac_same_time_dropped", 0) + (j - i - 1)
                )
            stats["dac_source_bytes"] = (
                stats.get("dac_source_bytes", 0) + (j - i - 1)
            )
            run = [kept]
            flush()
            i = j
            continue
        if run and t > run[-1][0] + run[-1][2]:
            flush()
        run.append((t, byte, wait))
        i += 1
    flush()

    # Modern block plays: each 0x95 supplies one slice.
    for t, block, rate in vgm.dac_block_plays:
        if not (0 <= block < len(vgm.dac_blocks)):
            continue
        if not _dac_enabled_at(enable_tl, t):
            stats["dac_disabled_bytes"] = stats.get("dac_disabled_bytes", 0) + 1
            continue
        body = _signed_bank(bytes(vgm.dac_blocks[block]))
        if not body:
            continue
        rate = _clamp_rate(rate, stats)
        span = int(round(len(body) * VGM_RATE / rate))
        idx = _add_sample(samples, by_key, body, rate, t)
        plays.append(DacPlay(t, idx, _pan_at(pan_timeline, t), span, False))
        stats["dac_block_plays"] = stats.get("dac_block_plays", 0) + 1

    # Stream starts (0x93): each one is a contiguous slice of the PCM bank.
    for play in vgm.dac_stream_plays:
        if not _stream_is_dac(vgm, play, stats):
            continue
        body = _stream_body(vgm, play)
        if not body:
            continue
        if not _dac_enabled_at(enable_tl, play.sample):
            stats["dac_disabled_bytes"] = stats.get("dac_disabled_bytes", 0) + 1
            continue
        rate = _clamp_rate(play.rate or VGM_RATE, stats)
        span = int(round(len(body) * VGM_RATE / rate))
        idx = _add_sample(samples, by_key, body, rate, play.sample)
        plays.append(DacPlay(
            play.sample, idx, _pan_at(pan_timeline, play.sample), span, False,
        ))
        stats["dac_stream_plays"] = stats.get("dac_stream_plays", 0) + 1

    plays.sort(key=lambda p: p.onset)
    stats["dac_unique_samples"] = len(samples)
    stats["dac_sample_bytes"] = sum(len(s.data) for s in samples)

    if stats.get("dac_disabled_bytes") and not plays:
        warnings.append(
            f"{stats['dac_disabled_bytes']} YM2612 DAC writes while the DAC is "
            "disabled (0x2B bit 7 clear); no DAC audio"
        )
    elif stats.get("dac_disabled_audible"):
        warnings.append(
            f"{stats['dac_disabled_audible']} non-silent YM2612 DAC writes while "
            "the DAC is disabled; ignored"
        )
    if stats.get("dac_rate_clamped"):
        warnings.append(
            f"{stats['dac_rate_clamped']} YM2612 DAC runs clamped to "
            f"{DAC_RATE_MAX} Hz; their bytes are preserved but the timing is "
            "longer than the source"
        )
    if stats.get("dac_same_time_runs"):
        warnings.append(
            f"{stats['dac_same_time_runs']} YM2612 DAC bursts "
            f"({stats.get('dac_same_time_bytes', 0)} bytes) share one VGM "
            "sample; only the final value is kept because the intermediate "
            "writes cannot be timed at VGM resolution"
        )
    if stats.get("dac_bank_overrun"):
        warnings.append(
            f"{stats['dac_bank_overrun']} YM2612 DAC reads ran past the PCM bank"
        )
    return samples, plays, warnings, stats


def _packed_words(
    vgm: VgmFile, stats: dict, enable_tl: list[tuple[int, bool]],
) -> list[tuple[float, int, int]]:
    """Canonical words with block plays expanded, same-time groups reduced."""
    words: list[tuple[float, int, int]] = list(
        _byte_events(vgm, stats, enable_tl)
    )
    for t, block, rate in vgm.dac_block_plays:
        if not (0 <= block < len(vgm.dac_blocks)):
            continue
        if not _dac_enabled_at(enable_tl, t):
            stats["dac_disabled_bytes"] = stats.get("dac_disabled_bytes", 0) + 1
            continue
        body = _signed_bank(bytes(vgm.dac_blocks[block]))
        if not body:
            continue
        step = VGM_RATE / (rate if rate > 0 else VGM_RATE)
        for i, byte in enumerate(body):
            words.append((t + i * step, byte, 0))
        stats["dac_block_plays"] = stats.get("dac_block_plays", 0) + 1
    if not words:
        return []
    words.sort(key=lambda w: w[0])
    reduced: list[tuple[float, int, int]] = []
    i = 0
    while i < len(words):
        t = words[i][0]
        j = i + 1
        while j < len(words) and words[j][0] == t:
            j += 1
        group = words[i:j]
        kept = group[-1]
        if len(group) > 1:
            values = {w[1] for w in group}
            if len(values) == 1:
                stats["dac_collapsed_bytes"] = (
                    stats.get("dac_collapsed_bytes", 0) + len(group)
                )
            else:
                stats["dac_same_time_runs"] = (
                    stats.get("dac_same_time_runs", 0) + 1
                )
                stats["dac_same_time_bytes"] = (
                    stats.get("dac_same_time_bytes", 0) + len(group)
                )
                stats["dac_same_time_dropped"] = (
                    stats.get("dac_same_time_dropped", 0) + len(group) - 1
                )
        reduced.append(kept)
        i = j
    return reduced


def _prune_quiet_plays(
    samples: list[DacSample],
    plays: list[DacPlay],
    stats: dict,
) -> tuple[list[DacSample], list[DacPlay]]:
    """Drop samples that are silence, flat DC, or below the audible peak."""
    keep = [i for i, smp in enumerate(samples) if _audible_body(smp.data)]
    if len(keep) == len(samples):
        return samples, plays
    remap: dict[int, int] = {}
    compact: list[DacSample] = []
    for i in keep:
        remap[i] = len(compact)
        compact.append(samples[i])
    stats["dac_quiet_samples_removed"] = len(samples) - len(keep)
    return compact, [
        DacPlay(
            p.onset, remap[p.sample], p.pan, p.span, p.collapsed, p.source,
            p.fresh, p.hold,
        )
        for p in plays if p.sample in remap
    ]


def _attack_segments(
    words: list[tuple[float, int, int]],
    silence: int,
) -> list[list[tuple[float, int, int]]]:
    """Split a word stream at rests and sharp attacks.

    A silent hold of ``silence`` VGM samples is a rest between hits.  A jump
    out of a longer near-silent stretch is an attack too, so a driver that
    sends a DC level instead of an exact zero still starts a new body.  A
    short dip inside a waveform does not split it.
    """
    segments: list[list[tuple[float, int, int]]] = []
    current: list[tuple[float, int, int]] = []
    zero_start: float | None = None
    quiet_start: float | None = None
    for word in words:
        t, byte, _wait = word
        amplitude = abs(byte if byte < 128 else byte - 256)
        split = False
        if current and t - current[-1][0] >= silence:
            split = True
        if current and byte and zero_start is not None:
            split = split or t - zero_start >= silence
        if (
            not split and current and amplitude > DAC_ATTACK_JUMP
            and quiet_start is not None
        ):
            split = t - quiet_start >= DAC_ATTACK_QUIET_SAMPLES
        if split:
            segments.append(current)
            current = []
        if byte == 0:
            if zero_start is None:
                zero_start = t
        else:
            zero_start = None
        if amplitude <= DAC_ATTACK_QUIET_PEAK:
            if quiet_start is None:
                quiet_start = t
        else:
            quiet_start = None
        current.append(word)
    if current:
        segments.append(current)
    return segments


def _split_pan_segments(
    segments: list[list[tuple[float, int, int]]],
    pan_timeline: list[tuple[int, int]],
) -> list[list[tuple[float, int, int]]]:
    """A pan change inside a candidate starts a new playable body."""
    out: list[list[tuple[float, int, int]]] = []
    for segment in segments:
        if not segment:
            continue
        current: list[tuple[float, int, int]] = []
        last_pan = _pan_at(pan_timeline, segment[0][0])
        for word in segment:
            pan = _pan_at(pan_timeline, word[0])
            if current and pan != last_pan:
                out.append(current)
                current = []
            current.append(word)
            last_pan = pan
        if current:
            out.append(current)
    return out


def _share_prefix_samples(
    samples: list[DacSample],
    plays: list[DacPlay],
    stats: dict,
) -> tuple[list[DacSample], list[DacPlay]]:
    """Store a short body once when a longer same-rate body starts with it."""
    if len(samples) < 2:
        return samples, plays
    by_head: dict[tuple[int, int], list[int]] = {}
    for i, smp in enumerate(samples):
        if not smp.data:
            continue
        by_head.setdefault((smp.c4_rate, smp.data[0]), []).append(i)
    remap = list(range(len(samples)))
    reused = 0
    for indexes in by_head.values():
        if len(indexes) < 2:
            continue
        indexes.sort(key=lambda i: len(samples[i].data), reverse=True)
        for pos, i in enumerate(indexes):
            for j in indexes[:pos]:
                if not samples[j].data.startswith(samples[i].data):
                    continue
                remap[i] = j
                reused += 1
                break
    if not reused:
        return samples, plays
    # A prefix can itself have been merged into a still longer body.
    for i in range(len(remap)):
        while remap[i] != remap[remap[i]]:
            remap[i] = remap[remap[i]]
    new_plays = [
        DacPlay(
            p.onset, remap[p.sample], p.pan, p.span, p.collapsed, p.source,
            p.fresh, p.hold,
        )
        for p in plays
    ]
    used = {p.sample for p in new_plays}
    compact: list[DacSample] = []
    compact_map: dict[int, int] = {}
    for i, smp in enumerate(samples):
        if i not in used:
            continue
        compact_map[i] = len(compact)
        compact.append(smp)
    stats["dac_attack_prefix_reused"] = reused
    return compact, [
        DacPlay(
            p.onset, compact_map[p.sample], p.pan, p.span, p.collapsed,
            p.source, p.fresh, p.hold,
        )
        for p in new_plays
    ]


def _attack_sample_set(
    words: list[tuple[float, int, int]],
    pan_timeline: list[tuple[int, int]],
    silence: int,
) -> tuple[list[DacSample], list[DacPlay], dict]:
    """Rate-exact bodies split at attacks, with exact duplicates shared."""
    local: dict = {}
    segments = _split_pan_segments(
        _attack_segments(words, silence), pan_timeline,
    )
    samples: list[DacSample] = []
    by_key: dict[tuple[bytes, int], int] = {}
    plays: list[DacPlay] = []
    for segment in segments:
        start_i = 0
        end_i = len(segment)
        while start_i < end_i and segment[start_i][1] == 0:
            start_i += 1
        while end_i > start_i and segment[end_i - 1][1] == 0:
            end_i -= 1
        segment = segment[start_i:end_i]
        if not segment:
            local["dac_attack_silent_segments"] = (
                local.get("dac_attack_silent_segments", 0) + 1
            )
            continue
        data_bytes = bytes(w[1] for w in segment)
        if not _audible_body(data_bytes):
            local["dac_attack_quiet_segments"] = (
                local.get("dac_attack_quiet_segments", 0) + 1
            )
            continue
        times = [w[0] for w in segment]
        deltas = [b - a for a, b in zip(times, times[1:]) if b > a]
        min_wait = min(deltas) if deltas else 1.0
        rate = max(
            DAC_RATE_MIN,
            min(DAC_RATE_MAX, int(round(VGM_RATE / min_wait))),
        )
        body = _frames_from_times(segment, rate, local)
        if len(body) < DAC_ATTACK_MIN_FRAMES:
            local["dac_attack_short_segments"] = (
                local.get("dac_attack_short_segments", 0) + 1
            )
            continue
        start = int(round(times[0]))
        idx = _add_sample(samples, by_key, body, rate, start)
        plays.append(DacPlay(
            start, idx, _pan_at(pan_timeline, start),
            int(round(times[-1] - times[0])), False, True,
        ))
        local["dac_attack_segments"] = local.get("dac_attack_segments", 0) + 1
    samples, plays = _share_prefix_samples(samples, plays, local)
    local["dac_attack_samples"] = len(samples)
    local["dac_attack_bytes"] = sum(len(s.data) for s in samples)
    return samples, plays, local


def _merge_attack_tail(
    samples: list[DacSample],
    plays: list[DacPlay],
    budget: int,
    stats: dict,
) -> tuple[list[DacSample], list[DacPlay]]:
    """Join the least-significant tail bodies into the final sample.

    This is the last resort, the same rule the old chunk fallback used: the
    first ``budget - 1`` distinct bodies stay exact and the rest play in order
    out of one final body.  The initial attack split already keeps the common
    hits separate; this only runs when the candidate count itself overruns.
    """
    if budget < 2 or len(samples) <= budget:
        return samples, plays
    order: list[int] = []
    seen: set[int] = set()
    for p in sorted(plays, key=lambda p: p.onset):
        if p.sample not in seen:
            seen.add(p.sample)
            order.append(p.sample)
    if len(order) <= budget:
        return samples, plays
    keep = set(order[:budget - 1])
    tail = [i for i in order if i not in keep]
    tail_plays = [p for p in plays if p.sample not in keep]
    if not tail_plays:
        return samples, plays
    rates = {samples[i].c4_rate for i in tail}
    if len(rates) != 1:
        # Bodies at different rates cannot share one stored rate without
        # resampling; let the caller trim instead of detuning the tail.
        return samples, plays
    rate = rates.pop()
    first = min(tail_plays, key=lambda p: p.onset)
    start = first.onset
    body = bytearray()
    for p in sorted(tail_plays, key=lambda p: (p.onset, p.sample)):
        target = int(round((p.onset - start) * rate / VGM_RATE))
        if target > len(body):
            # The attack splitter put a rest here; hold silence between the
            # joined tail hits instead of repeating the previous byte.
            body.extend(b"\x00" * (target - len(body)))
        elif target < len(body):
            body = body[:target]
        body.extend(samples[p.sample].data)
    end = max(p.onset + p.span for p in tail_plays)

    remap: dict[int, int] = {}
    compact: list[DacSample] = []
    for i in order:
        if i not in keep:
            continue
        remap[i] = len(compact)
        compact.append(samples[i])
    tail_idx = len(compact)
    compact.append(DacSample(-1, len(body), bytes(body), rate, start))
    new_plays = [
        DacPlay(
            p.onset, remap[p.sample], p.pan, p.span, p.collapsed, p.source,
            p.fresh, p.hold,
        )
        for p in plays if p.sample in keep
    ]
    new_plays.append(DacPlay(
        start, tail_idx, first.pan, end - start, False, True,
        first.fresh, first.hold,
    ))
    stats["dac_release_merged_tail"] = len(tail)
    stats["dac_attack_tail_plays"] = len(tail_plays)
    return compact, new_plays


def _attack_chunks(
    words: list[tuple[float, int, int]],
    pan_timeline: list[tuple[int, int]],
) -> tuple[list[DacSample], list[DacPlay], dict]:
    """Attack-split fallback.  Widen the rest threshold a little, then fit."""
    silence = DAC_ATTACK_SILENCE
    best: tuple[list[DacSample], list[DacPlay], dict] | None = None
    while silence <= max(DAC_ATTACK_SILENCE, DAC_RUN_GAP):
        samples, plays, local = _attack_sample_set(
            words, pan_timeline, silence,
        )
        if best is None or len(samples) < len(best[0]):
            best = (samples, plays, local)
        if len(samples) <= DAC_RELEASE_SAMPLES:
            best[2]["dac_attack_silence"] = silence
            return best
        silence *= 2
    assert best is not None
    samples, plays = _merge_attack_tail(
        best[0], best[1], DAC_RELEASE_SAMPLES, best[2],
    )
    best[2]["dac_attack_silence"] = silence
    return samples, plays, best[2]


def _frames_from_times(
    selected: list[tuple[float, int, int]],
    rate: int,
    stats: dict | None = None,
) -> bytes:
    """Lay source bytes on a ``rate`` frame grid, holding the last value."""
    out = bytearray()
    previous = 0x00
    start = selected[0][0]
    for t, byte, _wait in selected:
        frame = int(round((t - start) * rate / VGM_RATE))
        if frame < len(out):
            out[frame] = byte
            if stats is not None:
                stats["dac_frame_collisions"] = (
                    stats.get("dac_frame_collisions", 0) + 1
                )
            previous = byte
            continue
        if frame > len(out):
            out.extend(bytes([previous]) * (frame - len(out)))
        out.append(byte)
        previous = byte
    return bytes(out)


def _source_runs(
    vgm: VgmFile,
    stats: dict,
    enable_tl: list[tuple[int, bool]],
) -> list[dict] | None:
    """Bank reads grouped by the `0xE0` pointer, with their source offsets.

    Each pointer change starts a run. A run keeps reading forward from that
    offset until the driver moves the pointer, so a shorter play of the same
    data is a prefix of the longer run. A `$2B` state change ends the run as
    well, because the driver resumes at the next bank address afterwards.
    Returns `None` when the log writes the DAC directly (`0x2A`), which has no
    bank offset to reuse.
    """
    bank = bytes(vgm.dac_data)
    signed = _signed_bank(bank)
    local: dict = {}
    runs: list[dict] = []
    cursor = 0
    pos = 0
    current: dict | None = None
    saw_pointer = False
    fresh_next = False
    # A 0x2B transition ends a bank run even when no bytes are written while
    # the DAC is off. The driver resumes reading at the next bank address, so
    # the post-enable bytes are a new source segment, not a held gap inside
    # the previous one.
    change_times: list[int] = []
    previous_state: bool | None = None
    for change_t, change_en in enable_tl:
        if change_en != previous_state:
            change_times.append(change_t)
            previous_state = change_en
    change_i = 0

    def flush() -> None:
        nonlocal current
        if current is not None and current["bytes"]:
            runs.append(current)
        current = None

    for t, kind, value in vgm.dac_events:
        while change_i < len(change_times) and t >= change_times[change_i]:
            # The state at this instant belongs to the new run.
            flush()
            fresh_next = True
            change_i += 1
        if kind == 0:
            flush()
            cursor = max(0, value)
            pos = 0
            saw_pointer = True
            fresh_next = False
            continue
        if kind != 1:
            # A direct 0x2A stream has no bank offset to reuse.
            return None
        addr = cursor + pos
        pos += 1
        if addr >= len(bank):
            local["dac_bank_overrun"] = local.get("dac_bank_overrun", 0) + 1
            continue
        if not _dac_enabled_at(enable_tl, t):
            # Inaudible bytes break the bank run: playback resumes at this
            # later address, which is a new source segment.
            flush()
            fresh_next = True
            local["dac_disabled_bytes"] = (
                local.get("dac_disabled_bytes", 0) + 1
            )
            if signed[addr]:
                local["dac_disabled_audible"] = (
                    local.get("dac_disabled_audible", 0) + 1
                )
            continue
        if current is None:
            current = {
                "t": t, "off": addr, "bytes": [], "times": [],
                "fresh": fresh_next,
            }
            fresh_next = False
        current["bytes"].append(signed[addr])
        current["times"].append(t)
    flush()

    if not saw_pointer or not runs:
        return None
    stats.update(local)
    return runs


def _source_pitch(run: dict) -> int:
    """Median byte cadence of a run, in bytes per second."""
    times = run["times"]
    waits = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not waits:
        return VGM_RATE
    return max(DAC_RATE_MIN, min(
        DAC_RATE_MAX, int(round(VGM_RATE / statistics.median(waits))),
    ))


def _split_silent_run(run: dict) -> list[dict] | None:
    """Split one bank run at a long held-silence gap.

    A driver can read from offset 0 through a kick into the next bank
    sample after a long pause.  Keeping that as one body delays the next
    sample and stores the pause; split at the gap and let the post part keep
    its own bank offset and onset.  A held silence of ``DAC_RUN_GAP`` or
    more is a rest between source segments, not sample data.
    """
    times = run["times"]
    values = run["bytes"]
    if len(times) < 2 or len(values) < 2:
        return None
    best_gap = 0
    best_i = -1
    for i in range(len(times) - 1):
        if values[i]:
            continue
        gap = times[i + 1] - times[i]
        if gap > best_gap:
            best_gap = gap
            best_i = i
    if best_i < 0 or best_gap < DAC_RUN_GAP:
        return None
    pre = dict(run)
    pre["bytes"] = values[:best_i + 1]
    pre["times"] = times[:best_i + 1]
    pre["pitch"] = _source_pitch(pre)
    post = dict(run)
    post["t"] = times[best_i + 1]
    post["bytes"] = values[best_i + 1:]
    post["times"] = times[best_i + 1:]
    post["off"] = run["off"] + best_i + 1
    post["pitch"] = _source_pitch(post)
    post["fresh"] = False
    return [p for p in (pre, post) if p["bytes"]]


def _split_silent_runs(runs: list[dict], stats: dict) -> list[dict]:
    """Apply `_split_silent_run` repeatedly, keeping source order."""
    out: list[dict] = []
    for run in runs:
        pieces = [run]
        changed = True
        while changed:
            changed = False
            expanded: list[dict] = []
            for piece in pieces:
                parts = _split_silent_run(piece)
                if parts is None:
                    expanded.append(piece)
                    continue
                changed = True
                stats["dac_source_silence_splits"] = (
                    stats.get("dac_source_silence_splits", 0) + 1
                )
                expanded.extend(parts)
            pieces = expanded
        out.extend(pieces)
    return out


def _collect_source(
    runs: list[dict],
    stats: dict,
    pan_timeline: list[tuple[int, int]],
) -> tuple[list[DacSample], list[DacPlay]]:
    """One sample per bank offset and one play per audible run.

    The longest read at an offset is the stored body.  Every shorter read at
    that offset is a prefix of it: the next note or the explicit note-off cuts
    it, so a one-frame hit and a full drum share one module sample.  The byte
    cadence is deliberately not part of the key.  Drivers alternate four- and
    five-sample waits at one nominal rate, and a median-wait key split the same
    bank body into several near-identical samples.  The longest read keeps its
    own rate; shorter reads play the same body through the same cut.

    Silent, flat-line and near-silent reads take no sample slot.  They are
    rests between hits, and storing them as samples only burns the release
    budget.  A dropped quiet run still leaves its last value on some plays so
    a coalesced one-shot body keeps its real silence.
    """
    stats["dac_source_runs_unsplit"] = len(runs)
    runs = _split_silent_runs(runs, stats)
    stats["dac_source_silent_runs"] = sum(
        1 for r in runs if not any(r["bytes"])
    )
    audible: list[dict] = []
    quiet_bytes = 0
    for i, run in enumerate(runs):
        if _audible_body(run["bytes"]):
            audible.append(run)
            continue
        quiet_bytes += len(run["bytes"])
    stats["dac_source_quiet_runs"] = len(runs) - len(audible)
    stats["dac_source_quiet_bytes"] = quiet_bytes
    if not audible:
        return [], []

    # Map each audible run to the value the DAC holds after it.  When a quiet
    # run follows before the next audible one, that run wrote the hold (0 for
    # a real rest); otherwise the last audio byte stays on the line.
    hold_after: dict[int, int] = {}
    for i, run in enumerate(runs):
        if not _audible_body(run["bytes"]):
            continue
        value = run["bytes"][-1] if run["bytes"] else 0
        if i + 1 < len(runs):
            nxt = runs[i + 1]
            if not _audible_body(nxt["bytes"]) and nxt["bytes"]:
                value = nxt["bytes"][-1]
        hold_after[id(run)] = value

    groups: dict[int, list[dict]] = {}
    for run in audible:
        groups.setdefault(run["off"], []).append(run)

    samples: list[DacSample] = []
    sample_of: dict[int, int] = {}
    for off, items in groups.items():
        rep = max(items, key=lambda r: (
            len(r["bytes"]), r["times"][-1] - r["times"][0],
        ))
        duration = rep["times"][-1] - rep["times"][0]
        n = len(rep["bytes"])
        waits = [
            b - a for a, b in zip(rep["times"], rep["times"][1:]) if b > a
        ]
        if waits and len(set(waits)) == 1:
            # A uniform cadence is exact: N bytes at one wait occupy N frames
            # of VGM_RATE/wait, not (N-1).
            dense = VGM_RATE / waits[0]
        else:
            dense = n * VGM_RATE / duration if duration > 0 else VGM_RATE
        # The fastest interval must still have its own frame or its byte is
        # overwritten when the body is laid out.
        fastest = VGM_RATE / min(waits) if waits else float(VGM_RATE)
        rate = max(1, min(
            DAC_RATE_MAX, max(int(round(dense)), int(round(fastest))),
        ))
        body = _frames_from_times(
            [(t, b, 0) for t, b in zip(rep["times"], rep["bytes"])],
            rate, stats,
        )
        sample_of[off] = len(samples)
        samples.append(DacSample(
            off, len(body), body, rate, rep["t"],
        ))

    plays: list[DacPlay] = []
    for run in audible:
        idx = sample_of[run["off"]]
        plays.append(DacPlay(
            run["t"], idx, _pan_at(pan_timeline, run["t"]),
            run["times"][-1] - run["t"], False, True,
            run.get("fresh", False), hold_after.get(id(run)),
        ))
    plays.sort(key=lambda p: p.onset)

    stats["dac_source_offsets"] = len({r["off"] for r in audible})
    stats["dac_source_runs"] = len(audible)
    stats["dac_source_groups"] = len(samples)
    stats["dac_source_runs_reused"] = len(audible) - len(samples)
    stats["dac_source_bytes"] = sum(len(r["bytes"]) for r in audible)
    stats["dac_source_sample_bytes"] = sum(len(s.data) for s in samples)
    return samples, plays


def _coalesce_singles(
    samples: list[DacSample],
    plays: list[DacPlay],
    stats: dict,
) -> tuple[list[DacSample], list[DacPlay]]:
    """Join consecutive runs that are each used exactly once.

    A one-shot sequence of bank samples can be stored as one body and played
    as-is: there is no later note that could need an earlier entry. Only
    source plays whose sample is used once, with the same pan and rate, join.
    """
    if len(plays) < 2:
        return samples, plays
    uses: dict[int, int] = {}
    for p in plays:
        uses[p.sample] = uses.get(p.sample, 0) + 1
    ordered = sorted(plays, key=lambda p: p.onset)
    chains: list[list[DacPlay]] = []
    current: list[DacPlay] = []

    def flush() -> None:
        nonlocal current
        if len(current) > 1:
            chains.append(current)
        current = []

    for p in ordered:
        smp = samples[p.sample]
        if not p.source or uses.get(p.sample) != 1 or p.collapsed:
            flush()
            continue
        if p.fresh:
            # The DAC was disabled before this run; the gap is silence, not
            # a held value, so it cannot join the previous body.
            flush()
        if current:
            prev = current[-1]
            psmp = samples[prev.sample]
            if (
                p.pan != prev.pan
                or smp.c4_rate != psmp.c4_rate
            ):
                flush()
        current.append(p)
    flush()
    if not chains:
        return samples, plays

    new_samples = list(samples)
    chained_ids = {id(q) for chain in chains for q in chain}
    new_plays = [p for p in ordered if id(p) not in chained_ids]
    for chain in chains:
        first = chain[0]
        rate = samples[first.sample].c4_rate
        start = first.onset
        end = chain[-1].onset + chain[-1].span
        body = bytearray()
        held = 0x00
        for p in chain:
            target = int(round((p.onset - start) * rate / VGM_RATE))
            if target > len(body):
                fill = held if held is not None else 0
                body.extend(bytes([fill]) * (target - len(body)))
            smp = samples[p.sample]
            frames = max(1, int(round(p.span * rate / VGM_RATE)))
            payload = smp.data[:frames]
            if not payload:
                continue
            if len(body) > target:
                body[target:target + len(payload)] = payload
            else:
                body.extend(payload)
            held = p.hold if p.hold is not None else payload[-1]
        if len(body) < 2:
            continue
        idx = len(new_samples)
        new_samples.append(DacSample(
            -1, len(body), bytes(body), rate, start,
        ))
        new_plays.append(DacPlay(
            start, idx, first.pan, end - start, False, True, first.fresh,
            chain[-1].hold,
        ))
        stats["dac_source_coalesced_chains"] = (
            stats.get("dac_source_coalesced_chains", 0) + 1
        )
        stats["dac_source_coalesced_plays"] = (
            stats.get("dac_source_coalesced_plays", 0) + len(chain)
        )
        stats["dac_source_coalesced_samples"] = (
            stats.get("dac_source_coalesced_samples", 0) + len(chain) - 1
        )

    used = {p.sample for p in new_plays}
    remap: dict[int, int] = {}
    compact: list[DacSample] = []
    for i, smp in enumerate(new_samples):
        if i in used:
            remap[i] = len(compact)
            compact.append(smp)
    rebuilt = [
        DacPlay(
            p.onset, remap[p.sample], p.pan, p.span, p.collapsed, p.source,
            p.fresh, p.hold,
        )
        for p in sorted(new_plays, key=lambda p: p.onset)
    ]
    stats["dac_unique_samples"] = len(compact)
    return compact, rebuilt


def _chunk_words(
    words: list[tuple[float, int, int]],
    stats: dict,
    pan_timeline: list[tuple[int, int]],
) -> tuple[list[DacSample], list[DacPlay], int, int]:
    """Turn per-byte DAC writes into release-capped rate-exact PCM chunks."""
    phrases: list[list[tuple[float, int, int]]] = []
    current: list[tuple[float, int, int]] = []
    for word in words:
        if current and word[0] - current[-1][0] > DAC_RUN_GAP:
            phrases.append(current)
            current = []
        current.append(word)
    if current:
        phrases.append(current)

    phrase_info: list[dict] = []
    trimmed = 0
    total_span = 0.0
    for phrase in phrases:
        audio = [i for i, w in enumerate(phrase) if w[1] != 0]
        if not audio:
            stats["dac_silent_phrases"] = (
                stats.get("dac_silent_phrases", 0) + 1
            )
            trimmed += len(phrase)
            continue
        first_i = audio[0]
        last_i = audio[-1]
        tail_i = None
        for j in range(last_i + 1, len(phrase)):
            if phrase[j][1] == 0:
                tail_i = j
                break
        trimmed += first_i + (
            len(phrase) - 1 - last_i - (1 if tail_i is not None else 0)
        )
        span = max(1.0, phrase[last_i][0] - phrase[first_i][0])
        phrase_info.append({
            "phrase": phrase,
            "audio": audio,
            "first_i": first_i,
            "last_i": last_i,
            "tail_i": tail_i,
            "span": span,
            "pans": [_pan_at(pan_timeline, phrase[i][0]) for i in audio],
        })
        total_span += span
        stats["dac_phrases"] = stats.get("dac_phrases", 0) + 1

    total_span = max(total_span, 1.0)
    target_chunks = max(1, min(
        DAC_RELEASE_SAMPLES,
        int(total_span / (VGM_RATE * DAC_CHUNK_SECONDS) + 0.9999),
    ))
    stats["dac_target_chunks"] = target_chunks

    samples: list[DacSample] = []
    plays: list[DacPlay] = []
    pan_chunks = 0
    for info in phrase_info:
        phrase = info["phrase"]
        audio = info["audio"]
        pans = info["pans"]
        budget = max(1, int(round(
            target_chunks * info["span"] / total_span
        )))
        segments: list[tuple[int, int, int]] = []
        seg_start = 0
        for k in range(1, len(audio)):
            if pans[k] != pans[seg_start]:
                segments.append((seg_start, k - 1, pans[seg_start]))
                seg_start = k
        segments.append((seg_start, len(audio) - 1, pans[seg_start]))
        allocations: list[int] = []
        for s0, s1, _pan in segments:
            count = max(1, int(round(
                budget * (s1 - s0 + 1) / len(audio)
            )))
            count = min(count, s1 - s0 + 1)
            allocations.append(count)
        for (seg0, seg1, seg_pan), count in zip(segments, allocations):
            seg_len = seg1 - seg0 + 1
            last_pan = None
            for k in range(count):
                a0 = seg0 + (k * seg_len) // count
                a1 = seg0 + ((k + 1) * seg_len) // count - 1
                if a1 < a0:
                    continue
                w0 = audio[a0]
                w1 = audio[a1]
                # A chunk may take the zero byte that immediately follows its
                # last word, but not a zero thousands of words later: that is
                # the phrase's trailing silence, and appending it stretched
                # every chunk to the whole phrase (two overlapped chunks then
                # made group_dac_plays double the rate).
                chunk_tail = (
                    w1 + 1
                    if w1 + 1 < len(phrase) and phrase[w1 + 1][1] == 0
                    else None
                )
                selected = phrase[w0:w1 + 1] + (
                    [phrase[chunk_tail]] if chunk_tail is not None else []
                )
                start = selected[0][0]
                times = [w[0] for w in selected]
                deltas = [
                    b - a for a, b in zip(times, times[1:]) if b > a
                ]
                min_wait = min(deltas) if deltas else 1.0
                rate = max(
                    DAC_RATE_MIN,
                    min(DAC_RATE_MAX, int(round(VGM_RATE / min_wait))),
                )
                out = bytearray()
                previous = 0x00
                for t, byte, _wait in selected:
                    frame = int(round((t - start) * rate / VGM_RATE))
                    if frame < len(out):
                        out[frame] = byte
                        stats["dac_frame_collisions"] = (
                            stats.get("dac_frame_collisions", 0) + 1
                        )
                        previous = byte
                        continue
                    if frame > len(out):
                        out.extend(bytes([previous]) * (frame - len(out)))
                    out.append(byte)
                    previous = byte
                data = bytes(out)
                idx = len(samples)
                samples.append(DacSample(0, len(data), data, rate, start))
                chunk_pan = _pan_at(pan_timeline, start)
                plays.append(DacPlay(
                    start, idx, chunk_pan,
                    int(round(selected[-1][0] - start)),
                ))
                if chunk_pan != last_pan:
                    pan_chunks += 1
                    last_pan = chunk_pan
                stats["dac_chunks"] = stats.get("dac_chunks", 0) + 1
                if chunk_pan != seg_pan:
                    stats["dac_pan_mismatch"] = (
                        stats.get("dac_pan_mismatch", 0) + 1
                    )

    if len(samples) > DAC_RELEASE_SAMPLES:
        keep = DAC_RELEASE_SAMPLES - 1
        tail = sorted(
            (p for p in plays if p.sample >= keep), key=lambda p: p.onset,
        )
        rates = {samples[p.sample].c4_rate for p in tail}
        pans = {p.pan for p in tail}
        if len(tail) > 1 and len(rates) == 1 and len(pans) == 1:
            # The rest of the song is played in order: one final sample can
            # hold the whole remainder and play as-is. This keeps the end of
            # a long stream instead of dropping it at the budget.
            first = tail[0]
            rate = samples[first.sample].c4_rate
            start = first.onset
            body = bytearray()
            held = 0x00
            for p in tail:
                target = int(round((p.onset - start) * rate / VGM_RATE))
                if target > len(body):
                    body.extend(bytes([held]) * (target - len(body)))
                elif target < len(body):
                    body = body[:target]
                payload = samples[p.sample].data
                body.extend(payload)
                held = payload[-1]
            end = tail[-1].onset + tail[-1].span
            samples = samples[:keep] + [DacSample(
                0, len(body), bytes(body), rate, start,
            )]
            plays = [p for p in plays if p.sample < keep]
            plays.append(DacPlay(
                start, keep, first.pan, end - start, False,
            ))
            stats["dac_release_merged_tail"] = len(tail)
        else:
            stats["dac_release_trimmed"] = (
                len(samples) - DAC_RELEASE_SAMPLES
            )
            del samples[DAC_RELEASE_SAMPLES:]
            plays = [p for p in plays if p.sample < DAC_RELEASE_SAMPLES]
    return samples, plays, pan_chunks, trimmed


def _stream_is_dac(vgm: VgmFile, play: DacStreamPlay, stats: dict) -> bool:
    """Whether a 0x93 start is the first YM2612's DAC and inside the song.

    Both collection modes take every PCM byte from `dac_data`, so a stream on
    another chip, another register, or the exporter's end-of-file flush has to
    be refused rather than played twice.
    """
    if play.chip_type != DAC_STREAM_YM2612:
        stats["dac_stream_other_chip"] = (
            stats.get("dac_stream_other_chip", 0) + 1
        )
        return False
    if play.reg != 0x2A:
        # The DAC takes only $2A. A stream aimed at another register is an FM
        # write stream, not PCM this tool can host in a sample map.
        stats["dac_stream_other_reg"] = (
            stats.get("dac_stream_other_reg", 0) + 1
        )
        return False
    if play.chip_id:
        stats["dac_stream_second_chip"] = (
            stats.get("dac_stream_second_chip", 0) + 1
        )
        return False
    if vgm.total_samples and play.sample >= vgm.total_samples:
        # The exporter flushes a final start at the end-of-file offset; the
        # song has already stopped there.
        stats["dac_stream_tail_plays"] = (
            stats.get("dac_stream_tail_plays", 0) + 1
        )
        return False
    return True


def _stream_body(vgm: VgmFile, play: DacStreamPlay) -> bytes:
    """The byte range a 0x93 start plays out of the stream's data bank.

    The YM2612's bank is the concatenated type-0x00 PCM database, which the
    loader keeps in `dac_data`.  Mode 1 counts commands (one byte each on the
    YM2612), mode 2 milliseconds of the 0x92 rate, mode 3 the rest of the bank.
    The 0x91 step size is in command units, so a step of two plays every other
    byte.  The database is unsigned (0x80 centre), while a module sample is
    signed 8-bit: convert here so the playback engine does not store a -128 DC
    offset, which clips the waveform against one rail.
    """
    bank = bytes(vgm.dac_data)
    start = max(0, min(len(bank), play.offset))
    if play.length_mode == 1:
        end = start + max(0, play.length)
    elif play.length_mode == 2:
        rate = play.rate or VGM_RATE
        end = start + max(0, int(round(play.length * rate / 1000.0)))
    elif play.length_mode == 3:
        end = len(bank)
    else:
        end = start
    step = play.step if play.step > 1 else 1
    body = bank[start:min(end, len(bank)):step]
    return _signed_bank(body[::-1] if play.reverse else body)


def _stream_chunks(
    vgm: VgmFile, stats: dict,
) -> list[tuple[int, bytes, int]]:
    """(onset, body, rate) chunks for YM2612 0x93 stream starts.

    A stream is already contiguous PCM at one rate, so it never needs the
    per-byte word expansion.  The release sample budget is spread over the
    plays by duration, which keeps a minute-long drum stream inside the same
    240-sample cap the direct-write path uses.
    """
    if not vgm.dac_stream_plays:
        return []
    bodies: list[tuple[int, bytes, int]] = []
    for play in vgm.dac_stream_plays:
        if not _stream_is_dac(vgm, play, stats):
            continue
        body = _stream_body(vgm, play)
        if not body:
            stats["dac_stream_empty"] = (
                stats.get("dac_stream_empty", 0) + 1
            )
            continue
        rate = _clamp_rate(play.rate or VGM_RATE, stats)
        if play.loop:
            stats["dac_stream_loops"] = (
                stats.get("dac_stream_loops", 0) + 1
            )
        bodies.append((play.sample, body, rate))
    if not bodies:
        return []
    spans = [len(body) * VGM_RATE / rate for _t, body, rate in bodies]
    total_span = max(1.0, sum(spans))
    target = max(1, min(
        DAC_RELEASE_SAMPLES,
        int(total_span / (VGM_RATE * DAC_CHUNK_SECONDS) + 0.9999),
    ))
    stats["dac_stream_plays"] = len(bodies)
    stats["dac_stream_target_chunks"] = target
    out: list[tuple[int, bytes, int]] = []
    for (onset, body, rate), span in zip(bodies, spans):
        count = max(1, int(round(target * span / total_span)))
        count = min(count, len(body))
        for k in range(count):
            i0 = (k * len(body)) // count
            i1 = ((k + 1) * len(body)) // count
            if i1 <= i0:
                continue
            start = onset + int(round(i0 * VGM_RATE / rate))
            out.append((start, body[i0:i1], rate))
            stats["dac_stream_chunks"] = (
                stats.get("dac_stream_chunks", 0) + 1
            )
    out.sort(key=lambda c: c[0])
    return out


def _pan_chunks(plays: list[DacPlay]) -> int:
    count = 0
    last: int | None = None
    for p in sorted(plays, key=lambda p: p.onset):
        if p.pan != last:
            count += 1
            last = p.pan
    return count


def _collect_packed(
    vgm: VgmFile,
    *,
    grid: tuple[float, int, int] | None = None,
) -> tuple[list[DacSample], list[DacPlay], list[str], dict]:
    """Build source-segment samples, or release-capped chunks as a fallback.

    A log whose DAC reads a PCM bank through `0xE0` pointers keeps one sample
    per (offset, byte cadence). Shorter reads are prefixes of the longest run
    at that offset, so the note is cut by the next run rather than stored
    again. Other logs (direct `0x2A` writes, block/stream plays) use the
    time-chunk path.
    """
    if not (
        vgm.dac_events or vgm.dac_block_plays or vgm.dac_stream_plays
    ):
        return [], [], [], {}
    stats: dict = {}
    warnings: list[str] = []
    pan_timeline = None if _fm6_is_used(vgm) else _pan_timeline(vgm)
    enable_tl = _dac_enable_timeline(vgm)

    source_runs = None
    if not vgm.dac_block_plays and not vgm.dac_stream_plays:
        source_runs = _source_runs(vgm, stats, enable_tl)

    samples: list[DacSample] = []
    plays: list[DacPlay] = []
    chunks: list[tuple[int, bytes, int]] = []
    if source_runs:
        if not any(any(run["bytes"]) for run in source_runs):
            warnings.append(
                "YM2612 DAC stream contains only silence; no DAC notes written"
            )
            return [], [], warnings, stats
        samples, plays = _collect_source(source_runs, stats, pan_timeline)
        samples, plays = _coalesce_singles(samples, plays, stats)
        samples, plays = _prune_quiet_plays(samples, plays, stats)
        if not samples or not plays:
            warnings.append(
                "YM2612 DAC stream contains only silence; no DAC notes written"
            )
            return [], [], warnings, stats
        if grid is not None:
            # Count the bodies the real row grouping will keep, not an upper
            # bound: clustering, one-shot joining and silence splitting can
            # remove most of the source slots.  A preview run avoids dropping
            # a 104-sample source representation for the 240-sample chunk path.
            preview, *_rest = group_dac_plays(
                samples, plays, hz=grid[0], speed=grid[1], t0=grid[2],
                total_samples=vgm.total_samples or 0, trim=False,
            )
            estimate = len(preview)
        else:
            estimate = len(samples)
        if estimate > DAC_RELEASE_SAMPLES:
            warnings.append(
                f"{estimate} YM2612 DAC source samples would exceed the "
                f"{DAC_RELEASE_SAMPLES}-sample budget; using time chunks"
            )
            stats["dac_source_over_budget"] = estimate
            source_runs = None

    if source_runs:
        stats["dac_mode"] = "source"
        stats["dac_chunks"] = len(samples)
        stats["dac_pan_chunks"] = _pan_chunks(plays)
        stats["dac_unique_samples"] = len(samples)
        stats["dac_sample_bytes"] = sum(len(s.data) for s in samples)
        stats["dac_notes"] = len(plays)
        stats["dac_source_bytes"] = stats.get(
            "dac_source_bytes", sum(len(s.data) for s in samples),
        )
        # A source sample longer than the run is cut by the next note; the
        # extra body is intentional.
        stats["dac_source_cut_plays"] = sum(
            1 for p in plays
            if p.source and p.span < len(samples[p.sample].data)
        )
        # A pan change during one long sample is channel state: the writer
        # places 08xx on the row it lands on, so the note keeps its body and
        # the pan still moves during playback.
        if pan_timeline:
            points: list[tuple[int, int]] = []
            last_pan: int | None = None
            for t, raw in pan_timeline:
                pan = _pan_byte(raw)
                if pan != last_pan:
                    points.append((t, pan))
                    last_pan = pan
            stats["dac_pan_points"] = points
    else:
        # Drop source-only counters so the chunk stats describe what ran.
        for key in (
            "dac_source_offsets", "dac_source_runs", "dac_source_groups",
            "dac_source_runs_reused", "dac_source_sample_bytes",
            "dac_source_coalesced_chains", "dac_source_coalesced_plays",
            "dac_source_coalesced_samples", "dac_source_runs_unsplit",
            "dac_source_silent_runs", "dac_source_quiet_runs",
            "dac_source_quiet_bytes", "dac_source_silence_splits",
        ):
            stats.pop(key, None)
        words = _packed_words(vgm, stats, enable_tl)
        chunks = _stream_chunks(vgm, stats)
        if not words and not chunks:
            if stats.get("dac_disabled_bytes"):
                warnings.append(
                    f"{stats['dac_disabled_bytes']} YM2612 DAC writes while "
                    "the DAC is disabled (0x2B bit 7 clear); no DAC audio"
                )
            return [], [], warnings, stats
        mode = "chunks"
        trimmed = 0
        if words:
            # Attack detection first: a rest or a sharp onset starts a new
            # candidate body, so a repeated drum hit reuses one sample instead
            # of every fixed 0.25 s window becoming its own slice.  Uniform
            # chunks remain a fallback only when they fit better.
            samples, plays, attack_stats = _attack_chunks(
                words, pan_timeline,
            )
            stats.update(attack_stats)
            pan_chunks = _pan_chunks(plays)
            mode = "attacks"
            if len(samples) > DAC_RELEASE_SAMPLES:
                chunk_samples, chunk_plays, chunk_pan, chunk_trim = (
                    _chunk_words(words, stats, pan_timeline)
                )
                if len(chunk_samples) < len(samples):
                    samples, plays = chunk_samples, chunk_plays
                    pan_chunks, trimmed = chunk_pan, chunk_trim
                    mode = "chunks"
        else:
            samples, plays, pan_chunks = [], [], 0
        for onset, body, rate in chunks:
            idx = len(samples)
            samples.append(DacSample(0, len(body), body, rate, onset))
            plays.append(DacPlay(
                onset, idx, _pan_at(pan_timeline, onset),
                int(round(len(body) * VGM_RATE / rate)),
            ))
        samples, plays = _prune_quiet_plays(samples, plays, stats)
        plays.sort(key=lambda p: p.onset)
        if len(samples) > DAC_RELEASE_SAMPLES:
            stats["dac_release_trimmed"] = (
                len(samples) - DAC_RELEASE_SAMPLES
            )
            del samples[DAC_RELEASE_SAMPLES:]
            plays = [p for p in plays if p.sample < DAC_RELEASE_SAMPLES]
        stats["dac_mode"] = mode
        stats["dac_source_bytes"] = len(words) + sum(
            len(body) for _t, body, _r in chunks
        )
        stats["dac_trimmed_silence_bytes"] = trimmed
        stats["dac_chunks"] = len(samples)
        stats["dac_pan_chunks"] = pan_chunks
        stats["dac_unique_samples"] = len(samples)
        stats["dac_sample_bytes"] = sum(len(s.data) for s in samples)
        stats["dac_notes"] = len(plays)

    if stats.get("dac_disabled_audible"):
        warnings.append(
            f"{stats['dac_disabled_audible']} non-silent YM2612 DAC writes "
            "while the DAC is disabled; ignored"
        )
    if stats.get("dac_same_time_runs"):
        warnings.append(
            f"{stats['dac_same_time_runs']} YM2612 DAC bursts "
            f"({stats.get('dac_same_time_bytes', 0)} bytes) share one VGM "
            "sample; only the final value is kept"
        )
    if stats.get("dac_frame_collisions"):
        warnings.append(
            f"{stats['dac_frame_collisions']} YM2612 DAC writes rounded to an "
            "already-written phrase frame; the later value at that frame is kept"
        )
    if stats.get("dac_bank_overrun"):
        warnings.append(
            f"{stats['dac_bank_overrun']} YM2612 DAC reads ran past the PCM bank"
        )
    if stats.get("dac_release_merged_tail"):
        warnings.append(
            f"{stats['dac_release_merged_tail']} YM2612 DAC tail chunks were "
            f"joined into the final sample at the {DAC_RELEASE_SAMPLES}-sample "
            "budget; they play in order"
        )
    if stats.get("dac_release_trimmed"):
        warnings.append(
            f"{stats['dac_release_trimmed']} YM2612 DAC chunks were dropped "
            f"to stay inside the {DAC_RELEASE_SAMPLES}-sample budget; the end "
            "of the stream is missing"
        )
    if not samples:
        warnings.append(
            "YM2612 DAC stream contains only silence; no DAC notes written"
        )
    return samples, plays, warnings, stats


def collect_dac(
    vgm: VgmFile, *, slices: bool = False, timeline: bool = False,
    grid: tuple[float, int, int] | None = None,
) -> tuple[list[DacSample], list[DacPlay], list[str], dict]:
    """Return (samples, plays, warnings, stats).

    Default: bank source samples reused by offset (or rate-exact chunks when
    the source representation cannot fit). `slices=True`: the per-run
    sample-map editor representation. `timeline=True`: the old 22.05 kHz
    comparison body. `grid=(hz, speed, t0)` lets the collector check the row
    grid's final sample count before choosing a mode.
    """
    if timeline:
        return _collect_timeline(vgm)
    if slices:
        return _collect_slices(vgm)
    return _collect_packed(vgm, grid=grid)


def _play_frames(samples: list[DacSample], p: DacPlay) -> int:
    """Frames this play needs, which can be fewer than its sample's body."""
    data = samples[p.sample].data
    if p.span <= 0 or not p.source:
        return len(data)
    frames = int(round(
        p.span * samples[p.sample].c4_rate / VGM_RATE
    ))
    return max(1, min(len(data), frames))


def _group_rate(
    samples: list[DacSample], items: list[DacPlay],
    next_onset: dict[int, int],
) -> int:
    """One playback rate for a merged row."""
    weighted = 0.0
    weight = 0
    for p in items:
        n = _play_frames(samples, p)
        if n > 1 and not p.collapsed:
            weighted += samples[p.sample].c4_rate * n
            weight += n
    natural = weighted / weight if weight else float(VGM_RATE)
    lower = 0.0
    if not any(p.source for p in items):
        # A source play is cut by the next note, so it must keep its own
        # pitch. The old chunks had to be stretched to fit; source samples
        # do not.
        for p in items:
            n = _play_frames(samples, p)
            nxt = next_onset.get(id(p))
            if n > 1 and nxt is not None and nxt > p.onset:
                lower = max(lower, VGM_RATE * n / (nxt - p.onset))
    return int(round(max(natural, lower)))


def _held_byte(
    samples: list[DacSample], previous: int | None, previous_time: int,
    cut_time: int,
) -> int:
    """The value the DAC is holding when the next note starts."""
    if previous is None:
        return 0x00
    smp = samples[previous]
    frame = int(round((cut_time - previous_time) * smp.c4_rate / VGM_RATE))
    frame = max(0, min(len(smp.data) - 1, frame))
    return smp.data[frame]


def _body_overlap(a: bytes, b: bytes, shift: int):
    """Overlap of two bodies when ``b[i]`` aligns with ``a[i + shift]``."""
    if shift >= 0:
        n = min(len(a) - shift, len(b))
        if n <= 0:
            return None
        return n, shift, 0
    n = min(len(a), len(b) + shift)
    if n <= 0:
        return None
    return n, 0, -shift


def _sampled_mismatch(a: bytes, b: bytes, shift: int, limit: int = 128) -> float:
    ov = _body_overlap(a, b, shift)
    if ov is None:
        return 1.0
    n, sa, sb = ov
    step = max(1, n // limit)
    count = len(range(0, n, step))
    mism = sum(
        1 for i in range(0, n, step) if a[i + sa] != b[i + sb]
    )
    return mism / max(1, count)


def _full_mismatch(a: bytes, b: bytes, shift: int) -> tuple[int, list[int]]:
    ov = _body_overlap(a, b, shift)
    if ov is None:
        return 0, []
    n, sa, sb = ov
    return n, [
        i for i in range(n) if a[i + sa] != b[i + sb]
    ]


def _similar_alignment(a: bytes, b: bytes) -> tuple[int, float, float] | None:
    """Best body offset if two bank samples are the same audio.

    The leading and trailing fill can move by a few frames when a driver
    reaches the same bank body from a different row position.  The interior
    of the bodies must agree almost exactly; returning the offset lets the
    writer move that play's note so its main body still lands on time.
    """
    if len(a) < DAC_SIMILAR_MIN_LEN or len(b) < DAC_SIMILAR_MIN_LEN:
        return None
    best_sample = min(
        (_sampled_mismatch(a, b, sh), abs(sh), sh)
        for sh in range(-DAC_SIMILAR_SHIFT, DAC_SIMILAR_SHIFT + 1)
    )
    if best_sample[0] > 0.5:
        # Even the best coarse alignment is clearly a different body.
        return None
    best: tuple[tuple[float, float, int, int], int, float, float] | None = None
    center = best_sample[2]
    for sh in range(center - 3, center + 4):
        if abs(sh) > DAC_SIMILAR_SHIFT:
            continue
        n, mism = _full_mismatch(a, b, sh)
        if n < max(8, len(a) // 4):
            continue
        edge = min(DAC_SIMILAR_EDGE, max(4, n // 8))
        interior_n = n - 2 * edge
        interior_mis = sum(1 for i in mism if edge <= i < n - edge)
        overall = 1.0 - len(mism) / n
        interior = (
            1.0 - interior_mis / interior_n if interior_n > 0 else overall
        )
        if overall < DAC_SIMILAR_MIN_MATCH:
            continue
        if interior < DAC_SIMILAR_INTERIOR:
            continue
        cand = (interior, overall, -len(mism), -abs(sh))
        if best is None or cand > best[0]:
            best = (cand, sh, overall, interior)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _similar_candidates(a: DacSample, b: DacSample) -> bool:
    if abs(a.c4_rate - b.c4_rate) > DAC_SIMILAR_RATE:
        return False
    small = min(len(a.data), len(b.data))
    if small < DAC_SIMILAR_MIN_LEN:
        return False
    tolerance = min(256, max(16, small // 16))
    return abs(len(a.data) - len(b.data)) <= tolerance


def _merge_one_shot_samples(
    samples: list[DacSample],
    notes: list[tuple[int, int, int]],
    off_items: list[tuple[int, int]],
    stats: dict,
) -> tuple[list[DacSample], list[tuple[int, int, int]], list[tuple[int, int]]]:
    """Join unique samples that play back-to-back into one body.

    A sample used exactly once cannot be needed again, so a sequence of such
    notes on the same channel, pan and rate is stored as one body and played
    by the first note.  The next note's onset truncates the previous body, so
    overlapping tails stay exact.
    """
    if len(notes) < 2:
        return samples, notes, off_items
    uses: dict[int, int] = {}
    for _t, idx, _pan in notes:
        uses[idx] = uses.get(idx, 0) + 1
    off_by_note: dict[int, list[int]] = {}
    for ni, t in off_items:
        off_by_note.setdefault(ni, []).append(t)

    ordered = sorted(
        range(len(notes)), key=lambda i: (notes[i][0], i),
    )
    runs: list[list[int]] = []
    current: list[int] = []
    for ni in ordered:
        t, idx, pan = notes[ni]
        if uses.get(idx) != 1:
            if len(current) > 1:
                runs.append(current)
            current = []
            continue
        if current:
            pt, pidx, ppan = notes[current[-1]]
            prev = samples[pidx]
            end = pt + len(prev.data) * VGM_RATE / prev.c4_rate
            cut = off_by_note.get(current[-1])
            if cut:
                end = min(end, min(cut))
            if (
                pan == ppan
                and samples[idx].c4_rate == prev.c4_rate
                and t >= pt
                and t <= end + DAC_ONESHOT_GAP
            ):
                current.append(ni)
                continue
            if len(current) > 1:
                runs.append(current)
        current = [ni]
    if len(current) > 1:
        runs.append(current)
    if not runs:
        return samples, notes, off_items

    removed: set[int] = set()
    for run in runs:
        first_ni = run[0]
        start, first_idx, pan = notes[first_ni]
        rate = samples[first_idx].c4_rate
        body = bytearray(samples[first_idx].data)
        held = body[-1] if body else 0x00
        for ni in run[1:]:
            t, idx, _pan = notes[ni]
            target = int(round((t - start) * rate / VGM_RATE))
            if target < len(body):
                del body[target:]
            elif target > len(body):
                body.extend(bytes([held]) * (target - len(body)))
            payload = samples[idx].data
            body.extend(payload)
            if payload:
                held = payload[-1]
            removed.add(ni)
        new_idx = len(samples)
        samples.append(DacSample(-1, len(body), bytes(body), rate, start))
        notes[first_ni] = (start, new_idx, pan)
        stats["dac_oneshot_chains"] = (
            stats.get("dac_oneshot_chains", 0) + 1
        )
        stats["dac_oneshot_notes"] = (
            stats.get("dac_oneshot_notes", 0) + len(run) - 1
        )
        stats["dac_oneshot_samples"] = (
            stats.get("dac_oneshot_samples", 0) + len(run) - 1
        )
    if not removed:
        return samples, notes, off_items

    remap_note: dict[int, int] = {}
    new_notes: list[tuple[int, int, int]] = []
    for i, note in enumerate(notes):
        if i in removed:
            continue
        remap_note[i] = len(new_notes)
        new_notes.append(note)
    new_offs = [
        (remap_note[ni], t) for ni, t in off_items if ni in remap_note
    ]
    return samples, new_notes, new_offs


def _cluster_similar_samples(
    samples: list[DacSample],
    notes: list[tuple[int, int, int]],
    off_items: list[tuple[int, int]],
    stats: dict,
) -> tuple[list[DacSample], list[tuple[int, int, int]], list[tuple[int, int]]]:
    """Share one body between bank samples that differ only by a start offset.

    The representative is the longest member.  A play that used a shorter
    body starts at its own note time, so the note moves by the body offset to
    keep the shared main waveform on the same spot on the timeline.
    """
    count = len(samples)
    if count < 2:
        return samples, notes, off_items
    groups: list[list[int]] = []
    for i, smp in enumerate(samples):
        placed = False
        for group in groups:
            rep = group[0]
            if not _similar_candidates(samples[rep], smp):
                continue
            if _similar_alignment(samples[rep].data, smp.data) is not None:
                group.append(i)
                placed = True
                break
        if not placed:
            groups.append([i])

    remap = list(range(count))
    shifts = [0] * count
    merged_groups = 0
    for group in groups:
        if len(group) < 2:
            continue
        rep = max(group, key=lambda i: len(samples[i].data))
        members = []
        for i in group:
            if i == rep:
                members.append(i)
                continue
            if not _similar_candidates(samples[rep], samples[i]):
                continue
            hit = _similar_alignment(samples[rep].data, samples[i].data)
            if hit is None:
                continue
            shifts[i] = hit[0]
            members.append(i)
        if len(members) < 2:
            continue
        merged_groups += 1
        for i in members:
            remap[i] = rep
        stats["dac_similar_samples"] = (
            stats.get("dac_similar_samples", 0) + len(members) - 1
        )
    if not merged_groups:
        return samples, notes, off_items
    stats["dac_similar_groups"] = merged_groups

    note_dt: list[int] = [0] * len(notes)
    new_notes: list[tuple[int, int, int]] = []
    for i, (t, idx, pan) in enumerate(notes):
        target = remap[idx]
        sh = shifts[idx]
        if sh:
            dt = -sh * VGM_RATE / samples[target].c4_rate
            note_dt[i] = int(round(dt))
            t = max(0, t + note_dt[i])
            stats["dac_similar_shifted_notes"] = (
                stats.get("dac_similar_shifted_notes", 0) + 1
            )
        new_notes.append((t, target, pan))
    new_notes.sort(key=lambda n: n[0])
    new_offs = [
        (ni, max(0, t + note_dt[ni])) for ni, t in off_items
    ]
    return samples, new_notes, new_offs


def _longest_silence(data: bytes) -> tuple[int, int]:
    """(length, start) of the longest run of signed-silence bytes (0)."""
    best_len = 0
    best_start = 0
    run = 0
    start = 0
    for i, value in enumerate(data):
        if value:
            run = 0
            continue
        if not run:
            start = i
        run += 1
        if run > best_len:
            best_len = run
            best_start = start
    return best_len, best_start


def _split_long_silence(
    samples: list[DacSample],
    notes: list[tuple[int, int, int]],
    off_items: list[tuple[int, int]],
    stats: dict,
) -> tuple[list[DacSample], list[tuple[int, int, int]], list[tuple[int, int]]]:
    """Remove a long silent stretch from a bank body and keep its timing.

    A driver can read one bank range across a long DAC hold, so a kick and a
    later snare end up in one body.  When over 40% of an 8,192-frame body is
    one silent run, split before and after it.  Plays that reach the later
    part get their own note at the original time; the silence becomes note
    spacing instead of sample data.
    """
    if not notes:
        return samples, notes, off_items
    split_info: dict[int, tuple[int, int, int, int | None, int | None]] = {}
    for si, smp in enumerate(samples):
        if len(smp.data) < DAC_SILENCE_MIN_LEN:
            continue
        run, start = _longest_silence(smp.data)
        if run <= len(smp.data) * DAC_SILENCE_FRACTION:
            continue
        pre_end = start
        post_start = start + run
        if pre_end == 0 and post_start >= len(smp.data):
            continue
        pre_idx = None
        post_idx = None
        if pre_end > 0:
            pre_idx = len(samples)
            samples.append(DacSample(
                -1, pre_end, smp.data[:pre_end], smp.c4_rate, smp.onset,
            ))
        if post_start < len(smp.data):
            body = smp.data[post_start:]
            post_idx = len(samples)
            samples.append(DacSample(
                -1, len(body), body, smp.c4_rate, smp.onset,
            ))
        split_info[si] = (pre_end, post_start, run, pre_idx, post_idx)
        stats["dac_silence_split_frames"] = (
            stats.get("dac_silence_split_frames", 0) + run
        )
        stats["dac_silence_split_samples"] = (
            stats.get("dac_silence_split_samples", 0) + 1
        )
    if not split_info:
        return samples, notes, off_items

    off_by_note: dict[int, list[int]] = {}
    for ni, t in off_items:
        off_by_note.setdefault(ni, []).append(t)
    ordered = sorted(
        range(len(notes)), key=lambda i: (notes[i][0], i),
    )
    next_onset: dict[int, int | None] = {}
    for k, ni in enumerate(ordered):
        next_onset[ni] = (
            notes[ordered[k + 1]][0] if k + 1 < len(ordered) else None
        )

    new_notes: list[tuple[int, int, int]] = []
    new_offs: list[tuple[int, int]] = []
    for ni, (t, si, pan) in enumerate(notes):
        info = split_info.get(si)
        if info is None:
            idx = len(new_notes)
            new_notes.append((t, si, pan))
            for off_t in off_by_note.get(ni, ()):
                new_offs.append((idx, off_t))
            continue
        pre_end, post_start, _run, pre_idx, post_idx = info
        smp = samples[si]
        rate = smp.c4_rate
        pre_natural = t + pre_end * VGM_RATE / rate
        post_time = t + post_start * VGM_RATE / rate
        natural_end = t + len(smp.data) * VGM_RATE / rate
        explicit = min(off_by_note.get(ni, ()), default=None)
        end = natural_end
        if explicit is not None:
            end = min(end, explicit)
        nxt = next_onset.get(ni)
        if nxt is not None:
            end = min(end, nxt)

        added = False
        if pre_idx is not None:
            idx = len(new_notes)
            new_notes.append((t, pre_idx, pan))
            added = True
            if explicit is not None and explicit < pre_natural:
                new_offs.append((idx, int(round(max(t, explicit)))))
        if post_idx is not None and end > post_time:
            placed = int(round(post_time))
            idx = len(new_notes)
            new_notes.append((placed, post_idx, pan))
            added = True
            if explicit is not None and explicit < natural_end:
                new_offs.append((idx, int(round(max(placed, explicit)))))
        if added:
            stats["dac_silence_split_notes"] = (
                stats.get("dac_silence_split_notes", 0) + 1
            )
        else:
            stats["dac_silence_dropped_notes"] = (
                stats.get("dac_silence_dropped_notes", 0) + 1
            )
    new_notes.sort(key=lambda n: n[0])
    return samples, new_notes, new_offs


def _compact_samples(
    samples: list[DacSample],
    notes: list[tuple[int, int, int]],
) -> tuple[list[DacSample], list[tuple[int, int, int]]]:
    """Drop bodies no note references and renumber the sample map."""
    used = {idx for _t, idx, _pan in notes}
    remap: dict[int, int] = {}
    compact: list[DacSample] = []
    for i, smp in enumerate(samples):
        if i not in used:
            continue
        remap[i] = len(compact)
        compact.append(smp)
    return compact, [
        (t, remap[idx], pan) for t, idx, pan in notes
    ]


def _prefix_mad(a: bytes, b: bytes, shift: int, limit: int) -> float | None:
    """Mean absolute difference of ``a`` against ``b`` shifted by ``shift``."""
    n = min(len(a), len(b) - shift, limit)
    if n <= 0:
        return None
    return sum(
        abs((a[i] if a[i] < 128 else a[i] - 256)
            - (b[i + shift] if b[i + shift] < 128 else b[i + shift] - 256))
        for i in range(n)
    ) / n


def _fragment_anchors(
    samples: list[DacSample],
    uses: dict[int, int],
    target: int,
) -> list[int]:
    """The hottest bodies, then the longest/hottest ones, up to ``target``."""
    if len(samples) <= target:
        return list(range(len(samples)))
    by_uses = sorted(
        range(len(samples)),
        key=lambda i: (uses.get(i, 0), len(samples[i].data)),
        reverse=True,
    )
    # Fill the remaining slots by length first: a long unique body is worth
    # more than another short fragment that would only stand in for others.
    by_length = sorted(
        range(len(samples)),
        key=lambda i: (len(samples[i].data), uses.get(i, 0)),
        reverse=True,
    )
    chosen: list[int] = []
    seen: set[int] = set()
    for i in by_uses[:min(DAC_HOT_ANCHORS, target)]:
        chosen.append(i)
        seen.add(i)
    for i in by_length:
        if len(chosen) >= target:
            break
        if i in seen:
            continue
        chosen.append(i)
        seen.add(i)
    return chosen


def _cluster_fragments(
    samples: list[DacSample],
    notes: list[tuple[int, int, int]],
    stats: dict,
    *,
    target: int = DAC_TARGET_SAMPLES,
) -> tuple[list[DacSample], list[tuple[int, int, int]]]:
    """Reduce the sample map to ``target`` bodies by clustering fragments.

    Samples always start at frame 0, so a fragment can only reuse a body
    whose opening frames sound like the fragment's opening.  The comparison
    is deliberately loose: these are sub-row fragments and the row grid
    cannot resolve their exact timbre anyway.  The note's own span still cuts
    the shared body at the source run's end.
    """
    if not notes or len(samples) <= target:
        return samples, notes
    uses: dict[int, int] = {}
    for _t, idx, _pan in notes:
        uses[idx] = uses.get(idx, 0) + 1
    anchors = _fragment_anchors(samples, uses, target)
    if not anchors:
        return samples, notes
    anchor_set = set(anchors)
    non_anchors = [i for i in range(len(samples)) if i not in anchor_set]

    remap: dict[int, int] = {}
    for i in non_anchors:
        smp = samples[i]
        limit = min(len(smp.data), DAC_FRAGMENT_LIMIT)
        best: tuple[float, int] | None = None
        for j in anchors:
            other = samples[j]
            score = min(
                (value for shift in range(-DAC_FRAGMENT_SHIFT,
                                          DAC_FRAGMENT_SHIFT + 1)
                 if (value := _prefix_mad(
                     smp.data, other.data, shift, limit,
                 )) is not None),
                default=None,
            )
            if score is None:
                continue
            # Rate and length are tie-breakers: a same-rate body that covers
            # the whole fragment is preferable to one that detunes or ends
            # early.  Every fragment still gets a home.
            rate_penalty = abs(math.log(smp.c4_rate / other.c4_rate)) * 40.0
            length_penalty = (
                40.0 if len(other.data) < len(smp.data) else 0.0
            )
            effective = score + rate_penalty + length_penalty
            if best is None or effective < best[0]:
                best = (effective, j)
        # Every fragment gets a home, even if its body is empty or shorter
        # than the comparison window.
        remap[i] = best[1] if best is not None else anchors[0]

    if not remap:
        return samples, notes
    new_notes = [
        (t, remap.get(idx, idx), pan) for t, idx, pan in notes
    ]
    used = {idx for _t, idx, _pan in new_notes}
    keep = [
        i for i in range(len(samples))
        if i in used and i in anchor_set
    ]
    compact = {old: new for new, old in enumerate(keep)}
    stats["dac_fragment_clustered"] = len(remap)
    stats["dac_fragment_samples_kept"] = len(keep)
    return [samples[i] for i in keep], [
        (t, compact[idx], pan) for t, idx, pan in new_notes
    ]


def group_dac_plays(
    samples: list[DacSample],
    plays: list[DacPlay],
    *,
    hz: float,
    speed: int,
    t0: int = 0,
    total_samples: int = 0,
    map_capacity: int = DAC_MAP_CAPACITY,
    trim: bool = True,
    drop_quiet: bool = True,
    cluster_fragments: bool = True,
) -> tuple[
    list[DacSample], list[tuple[int, int, int]],
    list[tuple[int, int]], list[tuple[int, int]],
    list[str], dict,
]:
    """Merge same-row plays into samples, note-ons, note-offs and pan events.

    A source sample can be longer than the run that triggered it. When the
    next run is late, the note-off list cuts the sample at the run's real end
    instead of letting the longer body play into the gap.
    """
    if not plays:
        return samples, [], [], [], [], {}
    stats: dict = {}
    row_samples = (VGM_RATE / hz) * speed
    tick = VGM_RATE / hz

    def place(sample: int) -> tuple[int, int]:
        return place_tick(sample, t0, row_samples, speed, tick, floor=True)

    def placed_time(row: int, delay: int) -> int:
        return int(round(t0 + row * row_samples + delay * tick))

    ordered = sorted(plays, key=lambda p: p.onset)
    groups: dict[int, list[DacPlay]] = {}
    for p in ordered:
        row, _delay = place(p.onset)
        groups.setdefault(row, []).append(p)
    # The lower-bound rate guard must see the next *note* (the next row), not
    # the next chunk of the same note. A phrase is split into 0.25 s chunks
    # that share a row; using a following chunk's onset made the group play at
    # twice its real rate (a 44.1 kHz chunk stretched to 88.2 kHz).
    rows = sorted(groups)
    next_onset: dict[int, int] = {}
    for i, row in enumerate(rows):
        nxt = groups[rows[i + 1]][0].onset if i + 1 < len(rows) else total_samples
        for p in groups[row]:
            next_onset[id(p)] = nxt

    out: list[DacSample] = []
    by_key: dict[tuple[bytes, int], int] = {}
    notes: list[tuple[int, int, int]] = []
    off_items: list[tuple[int, int]] = []
    pan_events: list[tuple[int, int]] = []
    previous_sample: int | None = None
    previous_time = 0

    def is_silent(p: DacPlay) -> bool:
        payload = samples[p.sample].data
        return bool(payload) and not any(payload)

    def add_off(idx: int, items: list[DacPlay], note_time: int, rate: int) -> None:
        """Cut a reused body at the run's real end when a gap follows."""
        last = items[-1]
        if not last.source:
            return
        span_end = last.onset + last.span
        body_end = note_time + len(out[idx].data) * VGM_RATE / rate
        tolerance = max(1.0, tick)
        next_time = next_onset.get(id(last), total_samples)
        if body_end <= span_end + tolerance:
            return
        if total_samples and next_time <= span_end + tolerance:
            # The next note already cuts this one on time.
            return
        off_items.append((len(notes) - 1, int(round(span_end))))
        stats["dac_cut_offs"] = stats.get("dac_cut_offs", 0) + 1

    for row in sorted(groups):
        items = groups[row]
        if all(is_silent(p) for p in items):
            # Pure-silent row: tail the previous sample with 0x00, no note.
            stats["dac_silent_groups"] = stats.get("dac_silent_groups", 0) + 1
            stats["dac_silent_notes"] = stats.get("dac_silent_notes", 0) + 1
            stats["dac_silent_plays"] = (
                stats.get("dac_silent_plays", 0) + len(items)
            )
            stats["dac_silent_bytes"] = (
                stats.get("dac_silent_bytes", 0)
                + sum(len(samples[p.sample].data) for p in items)
            )
            if notes:
                prev_time, prev_idx, prev_pan = notes[-1]
                prev = out[prev_idx]
                if prev.data and prev.data[-1] != 0:
                    target = int(round(
                        (items[0].onset - prev_time) * prev.c4_rate / VGM_RATE
                    ))
                    target = max(target, len(prev.data))
                    body = (
                        prev.data
                        + bytes([prev.data[-1]]) * (target - len(prev.data))
                        + b"\x00"
                    )
                    if any(i == prev_idx for _t, i, _p in notes[:-1]):
                        new_idx = _add_sample(
                            out, by_key, body, prev.c4_rate, prev.onset,
                        )
                    else:
                        by_key.pop((prev.data, prev.c4_rate), None)
                        out[prev_idx] = DacSample(
                            prev.start, len(body), body, prev.c4_rate, prev.onset,
                        )
                        by_key[(body, prev.c4_rate)] = prev_idx
                        new_idx = prev_idx
                    notes[-1] = (prev_time, new_idx, prev_pan)
                    previous_sample = new_idx
                    previous_time = prev_time
                    stats["dac_silence_tail_bytes"] = (
                        stats.get("dac_silence_tail_bytes", 0)
                        + len(body) - len(prev.data)
                    )
            continue
        first = items[0]
        _row, delay = place(first.onset)
        note_time = placed_time(_row, delay)
        rate = _group_rate(samples, items, next_onset)
        if rate > DAC_RATE_MAX:
            rate = DAC_RATE_MAX
            stats["dac_rate_clamped"] = stats.get("dac_rate_clamped", 0) + 1
        elif rate < DAC_RATE_MIN:
            rate = DAC_RATE_MIN
            stats["dac_rate_clamped"] = stats.get("dac_rate_clamped", 0) + 1
        lead = max(0, first.onset - note_time)
        lead_frames = int(round(lead * rate / VGM_RATE))
        held = _held_byte(out, previous_sample, previous_time, note_time)
        if first.fresh:
            # The DAC was off before this run; a lead is silence, not the
            # previous sample's last value.
            held = 0x00

        # One source play on its own row already has exactly the body the
        # note needs. Reusing it is what keeps a repeated bank sample one
        # module sample; only a same-row concatenation or a lead fill has to
        # build a new body.
        only = items[0]
        next_time = next_onset.get(id(only), total_samples)
        has_next = bool(total_samples) and next_time < total_samples
        span_frames = (
            max(1, int(round(only.span * rate / VGM_RATE)))
            if only.span > 0 else len(samples[only.sample].data)
        )
        if (
            len(items) == 1 and only.source
            and rate == samples[only.sample].c4_rate
            and (
                has_next
                or span_frames + 1 >= len(samples[only.sample].data)
            )
        ):
            src = samples[only.sample]
            idx = _add_sample(
                out, by_key, src.data, src.c4_rate, note_time,
            )
            notes.append((note_time, idx, only.pan))
            add_off(idx, items, note_time, rate)
            previous_sample = idx
            previous_time = note_time
            stats["dac_groups"] = stats.get("dac_groups", 0) + 1
            stats["dac_reused_notes"] = (
                stats.get("dac_reused_notes", 0) + 1
            )
            continue

        data = bytearray()
        if lead_frames:
            data.extend(bytes([held]) * lead_frames)
            stats["dac_lead_bytes"] = stats.get("dac_lead_bytes", 0) + lead_frames

        note_pan = first.pan
        last_i = len(items) - 1
        last_next = next_onset.get(id(items[-1]), total_samples)
        last_has_next = bool(total_samples) and last_next < total_samples
        for i, p in enumerate(items):
            target = int(round((p.onset - note_time) * rate / VGM_RATE))
            if len(data) > target:
                over = len(data) - target
                stats["dac_overshoot"] = stats.get("dac_overshoot", 0) + over
                del data[target:]
                if data:
                    held = data[-1]
            elif target > len(data):
                fill = target - len(data)
                data.extend(bytes([held]) * fill)
                stats["dac_hold_bytes"] = stats.get("dac_hold_bytes", 0) + fill
            payload = samples[p.sample].data
            if p.source and (i < last_i or not last_has_next):
                if i < last_i:
                    nxt = int(round(
                        (items[i + 1].onset - note_time) * rate / VGM_RATE
                    ))
                    frames = max(1, nxt - target)
                else:
                    frames = max(1, int(round(p.span * rate / VGM_RATE)))
                    if frames + 1 >= len(payload):
                        frames = len(payload)
                payload = payload[:frames]
            data.extend(payload)
            if payload:
                held = payload[-1]
            if i and p.pan != items[i - 1].pan:
                # Pan is channel state; land it at the row start.
                prow, _pdelay = place(p.onset)
                pan_events.append((placed_time(prow, 0), p.pan))

        body = bytes(data)
        idx = _add_sample(out, by_key, body, rate, note_time)
        notes.append((note_time, idx, note_pan))
        add_off(idx, items, note_time, rate)
        previous_sample = idx
        previous_time = note_time
        stats["dac_groups"] = stats.get("dac_groups", 0) + 1

    # Bank source bodies are whole audio regions, so two bodies that differ
    # only by a small start offset and a one-shot sequence that never repeats
    # can share storage.  The chunk path must not do this: those bodies are
    # adjacent time slices of one stream.
    if plays and all(p.source for p in plays):
        out, notes, off_items = _split_long_silence(
            out, notes, off_items, stats,
        )
        out, notes = _compact_samples(out, notes)
        out, notes, off_items = _merge_one_shot_samples(
            out, notes, off_items, stats,
        )
        out, notes = _compact_samples(out, notes)
        out, notes, off_items = _cluster_similar_samples(
            out, notes, off_items, stats,
        )
        out, notes = _compact_samples(out, notes)

    # Row grouping can invent a silence tail or a flat DC body that the
    # source-side filter never saw.  No audible note may select one, so drop
    # those slots (and their notes/offs) before the writer sees them.
    keep = (
        [i for i, smp in enumerate(out) if _audible_body(smp.data)]
        if drop_quiet else list(range(len(out)))
    )
    if len(keep) < len(out):
        remap_sample = {old: new for new, old in enumerate(keep)}
        remap_note: dict[int, int] = {}
        kept_notes: list[tuple[int, int, int]] = []
        for ni, (t, idx, pan) in enumerate(notes):
            new_idx = remap_sample.get(idx)
            if new_idx is None:
                continue
            remap_note[ni] = len(kept_notes)
            kept_notes.append((t, new_idx, pan))
        stats["dac_quiet_samples_removed"] = len(out) - len(keep)
        out = [out[i] for i in keep]
        notes = kept_notes
        off_items = [
            (remap_note[ni], t) for ni, t in off_items if ni in remap_note
        ]

    if cluster_fragments:
        out, notes = _cluster_fragments(
            out, notes, stats, target=DAC_TARGET_SAMPLES,
        )
    offs = [(t, 0) for _ni, t in off_items]

    warnings: list[str] = []
    trimmed = 0
    if trim and len(out) > DAC_RELEASE_SAMPLES:
        trimmed = len(out) - DAC_RELEASE_SAMPLES
        stats["dac_release_trimmed"] = (
            stats.get("dac_release_trimmed", 0) + trimmed
        )
        del out[DAC_RELEASE_SAMPLES:]
        notes = [n for n in notes if n[1] < DAC_RELEASE_SAMPLES]
    stats["dac_unique_samples"] = len(out)
    stats["dac_notes"] = len(notes)
    stats["dac_sample_bytes"] = sum(len(s.data) for s in out)
    stats["dac_maps"] = (len(out) + map_capacity - 1) // map_capacity
    if stats.get("dac_overshoot"):
        warnings.append(
            f"{stats['dac_overshoot']} YM2612 DAC samples were longer than the "
            "gap to the next run; the later bytes start late by that many frames"
        )
    if trimmed:
        warnings.append(
            f"{trimmed} YM2612 DAC samples were dropped to stay inside the "
            f"{DAC_RELEASE_SAMPLES}-sample budget; the end of the stream is "
            "missing"
        )
    return out, notes, offs, pan_events, warnings, stats


def _collect_timeline(
    vgm: VgmFile,
) -> tuple[list[DacSample], list[DacPlay], list[str], dict]:
    """Old 22.05 kHz comparison body."""
    warnings: list[str] = []
    bank = _signed_bank(bytes(vgm.dac_data))
    if not bank:
        return [], [], warnings, {}
    pan_timeline = None if _fm6_is_used(vgm) else _pan_timeline(vgm)
    samples: list[DacSample] = []
    plays: list[DacPlay] = []
    start = 0
    pos = 0
    run_start: int | None = None
    run_last = 0
    out = bytearray()
    cur = 0x00
    last_idx = 0

    def flush() -> None:
        nonlocal run_start, out, cur, last_idx
        if run_start is None:
            return
        need = int(round((run_last - run_start + 1) * DAC_TIMELINE_RATE / VGM_RATE))
        if need > len(out):
            out.extend(bytes([cur]) * (need - len(out)))
        if len(out) >= DAC_MIN_SAMPLES:
            data = bytes(out)
            idx = len(samples)
            samples.append(DacSample(-1, len(data), data, DAC_TIMELINE_RATE, run_start))
            plays.append(DacPlay(
                run_start, idx, _pan_at(pan_timeline, run_start),
                len(data) * VGM_RATE // DAC_TIMELINE_RATE,
            ))
        run_start = None
        out = bytearray()
        cur = 0x00
        last_idx = 0

    for t, kind, value in vgm.dac_events:
        if kind == 0:
            start = max(0, value)
            pos = 0
            continue
        if kind == 2:
            byte = (value - 0x80) & 0xFF
        elif kind == 1:
            if start + pos >= len(bank):
                continue
            byte = bank[start + pos] & 0xFF
            pos += 1
        else:
            continue
        if run_start is None:
            run_start = t
            run_last = t
            out = bytearray()
            cur = byte
            out.append(byte)
            last_idx = 0
            continue
        if t - run_last > DAC_RUN_GAP:
            flush()
            run_start = t
            run_last = t
            out = bytearray()
            cur = byte
            out.append(byte)
            last_idx = 0
            continue
        idx = int(round((t - run_start) * DAC_TIMELINE_RATE / VGM_RATE))
        if idx < last_idx:
            idx = last_idx
        if idx > len(out):
            out.extend(bytes([cur]) * (idx - len(out)))
        if idx == len(out):
            out.append(byte)
        else:
            out[idx] = byte
        last_idx = idx
        run_last = t
        cur = byte
    flush()
    return samples, plays, warnings, {"dac_timeline": 1}
