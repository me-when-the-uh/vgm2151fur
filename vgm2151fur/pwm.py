"""Sega 32X PWM: streamed 12-bit left/right samples -> PCM bodies.

The PWM chip is not a sample-ROM player.  The driver streams two 12-bit
values per stereo frame through the DAC-stream control commands:

    0x90 stream, chip 0x11, port 2 (left) or 3 (right)
    0x91 stream, bank 3, step size 2, step base 0/1
    0x92 stream, playback frequency (usually 22020 Hz)
    0x93/0x95 stream, position / data block

The data bank is VGM type 0x03 (sometimes n-Bit compressed as type 0x43).
Items are two-byte little-endian words whose low 12 bits are the PWM value.
Furnace has no 32X PWM platform, so each channel is written as a 16-bit PCM
sample on a Generic PCM DAC system and panned hard left/right.
"""

from __future__ import annotations

import array
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.samplecluster import cluster_fragments16
from vgm2151fur.vgm import STREAM_PWM, VGM_RATE, VgmFile

PWM_RATE = 22020
PWM_SIDES = 2  # left, right
PWM_SIDE_LEFT = 0
PWM_SIDE_RIGHT = 1
# Split the stream into YM2612-DAC-style pieces so one channel is not a single
# multi-megabyte sample.  0.25 s at 22020 Hz.
PWM_CHUNK_FRAMES = 5505
PWM_MAP_SLOTS = 120
# Attack segmentation.  A near-silent stretch separates two hits; the body is
# trimmed to the audible part and long bodies are split so a single segment
# cannot become a multi-second sample.  If this yields more unique bodies than
# fixed chunks, the collector falls back to chunks.
PWM_ATTACK_SILENCE = 250       # ~11 ms of near-silence splits a hit
# A body shorter than a quarter chunk (~62 ms at 22020 Hz) is a fragment, not
# a useful banked sample; it is folded into the following body.
PWM_ATTACK_MIN_FRAMES = PWM_CHUNK_FRAMES // 4
PWM_ATTACK_MAX_FRAMES = 44100  # split a sustained body at ~1 s
PWM_ATTACK_QUIET_DIV = 32      # quiet threshold = peak / 32
PWM_ATTACK_MAX_SAMPLES = 480   # above this, use fixed chunks
# Attack-transient candidates: a short rise from the idle band to a strong
# level is a new sample start even when there was no long rest before it.
PWM_ONSET_WINDOW = 8
PWM_ONSET_QUIET = 32
PWM_ONSET_MIN_GAP = 64
PWM_ONSET_LOOKAHEAD = 64


@dataclass
class PwmHit:
    """One chunk occurrence: where to start it and which side plays it."""

    time: int
    side: int


@dataclass
class PwmSample:
    chip_id: int
    side: int
    data: bytes  # signed 16-bit little-endian at PWM_RATE
    rate: int
    loop_start: int | None = None
    loop_end: int | None = None
    hits: list[PwmHit] = field(default_factory=list)
    chunk: int = 0
    first_chunk: bool = True

    @property
    def frames(self) -> int:
        return len(self.data) // 2

    @property
    def name(self) -> str:
        return f"PWM{self.chip_id + 1} {self.chunk:03d}"


def _signed12(value: int) -> int:
    value &= 0xFFF
    return value - 0x1000 if value & 0x800 else value


def _cycle_scale(cycle: int) -> tuple[int, int]:
    """(offset, scale) from a PWM cycle register value, matching libvgm."""
    cycle = (cycle - 1) & 0xFFF
    offset = (cycle // 2) + 1
    if offset <= 0:
        offset = 1
    return offset, 0x7FFF00 // offset


def _scale_value(value: int, cycle: int) -> int:
    if value == 0:
        return 0
    offset, scale = _cycle_scale(cycle)
    out = ((_signed12(value) - offset) * scale) >> 8
    return max(-32768, min(32767, out))


def _stream_positions(play, bank: bytes, blocks: list[bytes]) -> tuple[bytes, int]:
    """Data buffer and first byte for one DAC-stream play."""
    if play.block >= 0:
        if 0 <= play.block < len(blocks):
            data = blocks[play.block]
        else:
            data = bank
        start = 0
    else:
        data = bank
        start = play.offset if play.offset >= 0 else 0
    start += 2 * max(0, play.step_base)
    if start < 0:
        start = 0
    if start > len(data):
        start = len(data)
    return data, start


def _pcm_stats(data: bytes) -> tuple[array.array, int, int, int]:
    """(values, idle level, quiet width, peak) for one rendered side."""
    vals = array.array("h")
    vals.frombytes(data)
    if not vals:
        return vals, 0, 64, 0
    peak = max(abs(v) for v in vals)
    counts = Counter(vals)
    idle, idle_count = counts.most_common(1)[0]
    # Only a dominant low-level value is a real idle/rest level.  A common
    # musical sustain must not be mistaken for silence.
    if idle_count * 100 < len(vals) or abs(idle) > max(64, peak // 4):
        idle = 0
    width = max(64, peak // PWM_ATTACK_QUIET_DIV)
    return vals, idle, width, peak


def _audible_pcm(data: bytes, idle: int, width: int) -> bool:
    vals = array.array("h")
    vals.frombytes(data)
    return any(abs(v - idle) > width for v in vals)


def _onset_starts(
    vals: array.array, idle: int, width: int, peak: int,
) -> list[int]:
    """Attack-transient candidates: rises out of the idle band."""
    if not vals:
        return []
    amp = [abs(v - idle) for v in vals]
    env: list[int] = []
    window: list[int] = []
    for value in amp:
        window.append(value)
        if len(window) > PWM_ONSET_WINDOW:
            window.pop(0)
        env.append(max(window))
    low = width
    high = max(width * 4, peak // 8)
    starts: list[int] = []

    def add(pos: int) -> None:
        if not starts or pos - starts[-1] >= PWM_ONSET_MIN_GAP:
            starts.append(pos)

    if env[0] >= high:
        add(0)
    low_start: int | None = None
    for i, value in enumerate(env):
        if value <= low:
            if low_start is None:
                low_start = i
            continue
        if low_start is not None and i - low_start >= PWM_ONSET_QUIET:
            look = env[i:i + PWM_ONSET_LOOKAHEAD]
            if look and max(look) >= high:
                add(i)
        low_start = None
    for i in range(1, len(env)):
        if env[i] >= high and env[i - 1] <= low:
            add(i)
    return sorted(starts)


def _attack_bodies(
    data: bytes, vals: array.array, idle: int, width: int,
) -> list[tuple[int, bytes]]:
    """Split one rendered side into bodies starting at attack transients.

    Every detected attack is a candidate start, so pieces reused inside a
    longer musical passage can be banked separately.  When no attacks are
    found the older rest-based split is used; both paths trim silence and
    drop inaudible bodies.
    """
    if not vals:
        return []
    peak = max(abs(v - idle) for v in vals)
    starts = _onset_starts(vals, idle, width, peak)
    if len(starts) < 2:
        starts = []

    def quiet(value: int) -> bool:
        return abs(value - idle) <= width

    bounds: list[tuple[int, int]] = []
    if starts:
        for i, a in enumerate(starts):
            b = starts[i + 1] if i + 1 < len(starts) else len(vals)
            while b > a and quiet(vals[b - 1]):
                b -= 1
            # Keep short attacks as bounds too: the fold pass below merges
            # them into the next body, so their transient is not lost.
            if b > a:
                bounds.append((a, b))
    else:
        start = 0
        quiet_start: int | None = None
        for i, value in enumerate(vals):
            if quiet(value):
                if quiet_start is None:
                    quiet_start = i
                continue
            if quiet_start is not None and i - quiet_start >= PWM_ATTACK_SILENCE:
                a, b = start, quiet_start
                while a < b and quiet(vals[a]):
                    a += 1
                while b > a and quiet(vals[b - 1]):
                    b -= 1
                if b - a >= PWM_ATTACK_MIN_FRAMES:
                    bounds.append((a, b))
                start = i
            quiet_start = None
        a, b = start, len(vals)
        while a < b and quiet(vals[a]):
            a += 1
        while b > a and quiet(vals[b - 1]):
            b -= 1
        if b - a >= PWM_ATTACK_MIN_FRAMES:
            bounds.append((a, b))
    out: list[tuple[int, bytes]] = []
    # Fold short fragments into the following body so their attack is kept
    # without spending a sample slot on a few milliseconds of audio.
    merged: list[tuple[int, int]] = []
    i = 0
    while i < len(bounds):
        a, b = bounds[i]
        if b - a < PWM_ATTACK_MIN_FRAMES and i + 1 < len(bounds):
            bounds[i + 1] = (a, bounds[i + 1][1])
            i += 1
            continue
        merged.append((a, b))
        i += 1
    if merged and merged[-1][1] - merged[-1][0] < PWM_ATTACK_MIN_FRAMES:
        if len(merged) > 1:
            merged[-2] = (merged[-2][0], merged[-1][1])
            merged.pop()
        else:
            merged.pop()
    for a, b in merged:
        body = data[a * 2:b * 2]
        if not _audible_pcm(body, idle, width):
            continue
        while b - a > PWM_ATTACK_MAX_FRAMES:
            cut = a + PWM_ATTACK_MAX_FRAMES
            # A tiny tail would become its own sub-minimum sample; split the
            # body in half instead so both pieces are useful.
            if b - cut < PWM_ATTACK_MIN_FRAMES:
                cut = a + (b - a) // 2
            out.append((a, data[a * 2:cut * 2]))
            a = cut
        out.append((a, data[a * 2:b * 2]))
    return out


def collect_pwm(
    vgm: VgmFile, *, lossy: bool = False,
) -> tuple[list[PwmSample], list[str]]:
    """Render the VGM's PWM streams into one PCM body per side."""
    warnings: list[str] = []
    plays = [
        p for p in vgm.dac_stream_plays
        if p.chip_type == STREAM_PWM
    ]
    writes = [w for w in vgm.writes if w.chip == "pwm"]
    ids = sorted({p.chip_id for p in plays} | {w.chip_id for w in writes})
    if not ids:
        return [], warnings
    if not vgm.pwm_bank and not vgm.pwm_blocks:
        warnings.append(
            "PWM clock present but no type-0x03/0x43 PCM data block; "
            "the stream is silent"
        )
        return [], warnings

    # Cycle/control changes over time.  Only register 1 affects the audio.
    cycle_pts: list[tuple[int, int]] = [(0, 0)]
    direct: dict[int, list[tuple[int, int, int]]] = {}
    for w in writes:
        if w.reg == 1:
            cycle_pts.append((w.sample, w.val & 0xFFF))
        elif w.reg in (2, 3, 4):
            direct.setdefault(w.chip_id, []).append((w.sample, w.reg, w.val & 0xFFF))
    cycle_pts.sort()
    cycle_times = [t for t, _v in cycle_pts]
    cycle_vals = [v for _t, v in cycle_pts]

    def cycle_at(t: int) -> int:
        idx = bisect_right(cycle_times, t) - 1
        return cycle_vals[idx] if idx >= 0 else 0

    total = vgm.total_samples or 0
    if plays:
        total = max(total, max(p.sample for p in plays))
    frames = max(1, int(round(total / VGM_RATE * PWM_RATE)))
    if vgm.loop_sample is not None:
        loop_frame = max(0, int(round(
            vgm.loop_sample / VGM_RATE * PWM_RATE
        )))
    else:
        loop_frame = None

    samples: dict[tuple[int, int], PwmSample] = {}
    for cid in ids:
        for side in (PWM_SIDE_LEFT, PWM_SIDE_RIGHT):
            samples[(cid, side)] = PwmSample(
                chip_id=cid,
                side=side,
                data=bytes(frames * 2),
                rate=PWM_RATE,
                loop_start=loop_frame,
                loop_end=frames if loop_frame is not None else None,
            )

    # Group plays per chip and side; a mono stream (port 4 or a single stream)
    # feeds both sides.
    by_channel: dict[tuple[int, int], list] = {}
    mono: dict[int, list] = {}
    for play in plays:
        side = (
            PWM_SIDE_LEFT if play.reg == 2 else
            PWM_SIDE_RIGHT if play.reg == 3 else -1
        )
        if side < 0:
            mono.setdefault(play.chip_id, []).append(play)
        else:
            by_channel.setdefault((play.chip_id, side), []).append(play)
    for cid, sides in mono.items():
        for side in (PWM_SIDE_LEFT, PWM_SIDE_RIGHT):
            by_channel.setdefault((cid, side), []).extend(sides)

    for (cid, side), channel_plays in by_channel.items():
        channel_plays.sort(key=lambda p: (p.sample, p.stream))
        sample = samples.get((cid, side))
        if sample is None:
            continue
        dst = bytearray(sample.data)
        starts = [p.sample for p in channel_plays]
        for ci, play in enumerate(channel_plays):
            t0 = play.sample
            t1 = (
                channel_plays[ci + 1].sample
                if ci + 1 < len(channel_plays) else total
            )
            if t1 <= t0:
                continue
            data, start = _stream_positions(play, bytes(vgm.pwm_bank),
                                            vgm.pwm_blocks)
            stride = 2 * max(1, play.step)
            rate = play.rate or PWM_RATE
            if rate <= 0:
                continue
            items = (len(data) - start) // stride if len(data) > start else 0
            if items <= 0:
                continue
            frame0 = max(0, int(round(t0 / VGM_RATE * PWM_RATE)))
            frame1 = min(frames, int(round(t1 / VGM_RATE * PWM_RATE)))
            phase = 0.0
            for fr in range(frame0, frame1):
                idx = int(phase)
                if idx >= items:
                    if play.loop and items:
                        idx %= items
                    else:
                        break
                off = start + idx * stride
                value = data[off] | ((data[off + 1] & 0x0F) << 8)
                scaled = _scale_value(value, cycle_at(
                    int(fr * VGM_RATE / PWM_RATE)
                ))
                dst[fr * 2:fr * 2 + 2] = int(scaled).to_bytes(
                    2, "little", signed=True,
                )
                phase += rate / PWM_RATE
        sample.data = bytes(dst)

    # Direct level writes are not part of the usual log shape; apply them at
    # their exact sample and report when they are the only source, since the
    # stream normally overwrites the register at audio rate.
    for cid, entries in direct.items():
        for t, reg, value in entries:
            sides = (
                (PWM_SIDE_LEFT, PWM_SIDE_RIGHT) if reg == 4
                else (PWM_SIDE_LEFT,) if reg == 2
                else (PWM_SIDE_RIGHT,)
            )
            fr = max(0, min(frames - 1, int(round(
                t / VGM_RATE * PWM_RATE
            ))))
            for side in sides:
                sample = samples.get((cid, side))
                if sample is None:
                    continue
                scaled = _scale_value(value, cycle_at(t))
                dst = bytearray(sample.data)
                dst[fr * 2:fr * 2 + 2] = int(scaled).to_bytes(
                    2, "little", signed=True,
                )
                sample.data = bytes(dst)

    def occurrences(use_attack: bool) -> list[tuple[int, int, int, bytes]]:
        out: list[tuple[int, int, int, bytes]] = []
        for (cid, side), sample in sorted(samples.items()):
            full = sample.data
            frames = len(full) // 2
            if frames <= 0:
                continue
            vals, idle, width, _peak = _pcm_stats(full)
            if use_attack:
                for start, body in _attack_bodies(full, vals, idle, width):
                    out.append((cid, side, start, body))
            else:
                for start in range(0, frames, PWM_CHUNK_FRAMES):
                    end = min(frames, start + PWM_CHUNK_FRAMES)
                    body = full[start * 2:end * 2]
                    if _audible_pcm(body, idle, width):
                        out.append((cid, side, start, body))
        return out

    attack_occ = occurrences(True)
    chunk_occ = occurrences(False)
    attack_unique = len({(cid, body) for cid, _side, _s, body in attack_occ})
    # Prefer attack segmentation whenever it finds real splits.  Fixed chunks
    # are the fallback for a steady/continuous stream or a body budget blow-up.
    use_attack = (
        len(attack_occ) > 1
        and attack_unique <= PWM_ATTACK_MAX_SAMPLES
    )
    chosen = attack_occ if use_attack else chunk_occ

    audible: list[PwmSample] = []
    seen: dict[tuple[int, bytes], PwmSample] = {}
    for cid, side, start, body in chosen:
        hit = PwmHit(
            time=int(round(start / PWM_RATE * VGM_RATE)),
            side=side,
        )
        key = (cid, body)
        found = seen.get(key)
        if found is not None:
            found.hits.append(hit)
            continue
        chunk = PwmSample(
            chip_id=cid,
            side=side,
            data=body,
            rate=PWM_RATE,
            hits=[hit],
            chunk=len(audible),
            first_chunk=(start == 0),
        )
        seen[key] = chunk
        audible.append(chunk)
    if not audible:
        warnings.append("PWM streams decoded to silence")
    if lossy and len(audible) > 32:
        keep, remap = cluster_fragments16(
            [s.data for s in audible],
            [s.rate for s in audible],
            {i: len(s.hits) for i, s in enumerate(audible)},
        )
        compact = [audible[i] for i in keep]
        for old, smp in enumerate(audible):
            new = remap[old]
            if new < 0 or keep[new] == old:
                continue
            compact[new].hits.extend(smp.hits)
        if len(compact) < len(audible):
            warnings.append(
                f"PWM: lossy sample clustering {len(audible)} -> "
                f"{len(compact)} bodies"
            )
        audible = compact
    warnings = list(dict.fromkeys(warnings))
    return audible, warnings
