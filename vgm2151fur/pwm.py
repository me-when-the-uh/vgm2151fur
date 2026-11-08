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

from bisect import bisect_right
from dataclasses import dataclass, field

from vgm2151fur.vgm import STREAM_PWM, VGM_RATE, VgmFile

PWM_RATE = 22020
PWM_SIDES = 2  # left, right
PWM_SIDE_LEFT = 0
PWM_SIDE_RIGHT = 1
# Split the stream into YM2612-DAC-style pieces so one channel is not a single
# multi-megabyte sample.  0.25 s at 22020 Hz.
PWM_CHUNK_FRAMES = 5505
PWM_MAP_SLOTS = 120


@dataclass
class PwmSample:
    chip_id: int
    side: int
    data: bytes  # signed 16-bit little-endian at PWM_RATE
    rate: int
    loop_start: int | None = None
    loop_end: int | None = None
    hits: list[int] = field(default_factory=list)  # note-on sample times
    chunk: int = 0
    first_chunk: bool = True

    @property
    def frames(self) -> int:
        return len(self.data) // 2

    @property
    def name(self) -> str:
        side = "L" if self.side == 0 else "R"
        return f"PWM{self.chip_id + 1} {side} {self.chunk:03d}"


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


def collect_pwm(vgm: VgmFile) -> tuple[list[PwmSample], list[str]]:
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

    audible: list[PwmSample] = []
    for (cid, side), sample in sorted(samples.items()):
        full = sample.data
        frames = len(full) // 2
        if frames <= 0:
            continue
        chunk_no = 0
        for start in range(0, frames, PWM_CHUNK_FRAMES):
            end = min(frames, start + PWM_CHUNK_FRAMES)
            body = full[start * 2:end * 2]
            if not any(body):
                continue
            chunk = PwmSample(
                chip_id=cid,
                side=side,
                data=body,
                rate=PWM_RATE,
                hits=[int(round(start / PWM_RATE * VGM_RATE))],
                chunk=chunk_no,
                first_chunk=(start == 0),
            )
            audible.append(chunk)
            chunk_no += 1
    if not audible:
        warnings.append("PWM streams decoded to silence")
    return audible, warnings
