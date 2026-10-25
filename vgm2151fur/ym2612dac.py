"""YM2612 DAC PCM bank -> sample slices and FM6 playback notes.

VGM 1.50 encodes a DAC stream as a type-0 data block (unsigned 8-bit PCM)
plus 0xE0 (set read pointer) and 0x80-0x8F (play one byte, then wait 0-15
samples).  A long gap (>512 samples) or a new pointer starts another slice.
The slice rate is derived from its byte count and the time between bytes.

Furnace's Genesis platform plays an Amiga sample on channel 6 when the
instrument type is DIV_INS_AMIGA.  The writer groups 120 slices into one
sample-map instrument; a pattern note 60+slot selects the slice and the
sample's C-4 rate makes the DAC clock match the source.
"""

from __future__ import annotations

from dataclasses import dataclass

from vgm2151fur.vgm import VGM_RATE, VgmFile

DAC_GAP = 512
DAC_MIN_SAMPLES = 8
DAC_MAP_CAPACITY = 120
# A dense old-style stream (Contra: a byte every 3-5 VGM samples) is one
# continuous PCM waveform, not a row of drum hits.  Render it as one sample
# per phrase; a gap larger than this starts another phrase.
DAC_RUN_GAP = 2048
DAC_TIMELINE_RATE = 22050


@dataclass(frozen=True)
class DacSample:
    start: int
    length: int
    data: bytes  # signed 8-bit
    c4_rate: int
    onset: int


@dataclass(frozen=True)
class DacPlay:
    onset: int
    start: int
    length: int
    duration: int


def _signed_bank(raw: bytes) -> bytes:
    return bytes((x - 0x80) & 0xFF for x in raw)


def _pan_at(vgm: VgmFile, t: int) -> int:
    """Genesis channel-6 pan as Furnace's split 4-bit pan byte.

    YM2612 0xB6 bits 7/6 are left/right enables; Furnace's 08 effect uses
    one 4-bit nibble per side, so C0 (both) must become FF, not C0 (left
    only), and 40 (right) must become 0F.
    """
    pan = 0xC0
    for w in vgm.writes:
        if w.sample > t:
            break
        if (
            w.chip == "ym2612" and w.chip_id == 0
            and not (w.reg & 0x100) and (w.reg & 0xFF) == 0xB6
        ):
            pan = w.val & 0xC0
    left = bool(pan & 0x80)
    right = bool(pan & 0x40)
    if left and right:
        return 0xFF
    if left:
        return 0xF0
    if right:
        return 0x0F
    return 0


def _stream_mode(vgm: VgmFile) -> bool:
    """True when the old DAC stream is a continuous waveform.

    A handful of direct hits uses one sample per slice; a byte every few VGM
    samples is a PCM stream whose timing cannot be preserved by one note per
    tracker row.
    """
    times = [t for t, kind, _v in vgm.dac_events if kind == 1]
    if len(times) < 256:
        return False
    gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    if not gaps:
        return False
    return gaps[len(gaps) // 2] <= 16


def _collect_stream(vgm: VgmFile) -> tuple[list[DacSample], list[tuple[int, int, int, int]], list[str]]:
    """Render the old DAC stream as continuous PCM runs at 22.05 kHz."""
    warnings: list[str] = []
    bank = _signed_bank(bytes(vgm.dac_data))
    if not bank:
        return [], [], warnings
    samples: list[DacSample] = []
    notes: list[tuple[int, int, int, int]] = []
    start = 0
    pos = 0
    run_start: int | None = None
    run_last = 0
    out = bytearray()
    cur = 0x80
    last_idx = 0

    def flush() -> None:
        nonlocal run_start, out, cur, last_idx
        if run_start is None:
            return
        need = int(round((run_last - run_start + 1) * DAC_TIMELINE_RATE / VGM_RATE))
        if need > len(out):
            out.extend(bytes([cur]) * (need - len(out)))
        if len(out) >= DAC_MIN_SAMPLES:
            # `out` already holds signed bytes from _signed_bank(); converting
            # again would turn silence into a -128 DC rail and clip the wave.
            data = bytes(out)
            idx = len(samples)
            samples.append(DacSample(-1, len(data), data, DAC_TIMELINE_RATE, run_start))
            notes.append((
                run_start, idx, 60 + (idx % DAC_MAP_CAPACITY),
                _pan_at(vgm, run_start),
            ))
        run_start = None
        out = bytearray()
        cur = 0x80
        last_idx = 0

    for t, kind, value in vgm.dac_events:
        if kind == 0:
            start = max(0, value)
            pos = 0
            continue
        if kind == 2:
            byte = value & 0xFF
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
    return samples, notes, warnings


def collect_dac(vgm: VgmFile) -> tuple[list[DacSample], list[tuple[int, int, int, int]], list[str]]:
    """Return (samples, plays as (onset, instrument_sample_index, note, pan), warnings)."""
    warnings: list[str] = []
    if not vgm.dac_events and not vgm.dac_block_plays:
        return [], [], warnings
    if _stream_mode(vgm):
        return _collect_stream(vgm)
    bank = _signed_bank(bytes(vgm.dac_data))
    if not bank:
        return [], [], warnings

    plays: list[DacPlay] = []
    start = 0
    length = 0
    duration = 0
    pause = 0
    first_time: int | None = None
    last_time = 0
    active = False

    def finalize() -> None:
        nonlocal length, duration, first_time, active
        if length >= DAC_MIN_SAMPLES and duration > 0 and first_time is not None:
            plays.append(DacPlay(first_time, start, length, duration))
        length = 0
        duration = 0
        first_time = None
        active = False

    for t, kind, value in vgm.dac_events:
        if kind == 0:  # set read pointer
            if active:
                finalize()
            start = max(0, value)
            length = 0
            duration = 0
            pause = 0
            active = False
            continue
        if kind != 1:  # direct 0x2A writes are collected below
            continue
        wait = value & 0x0F
        if active and pause > DAC_GAP:
            # Long silence: the previous slice ends at `start+length`; the
            # next play continues from that address.
            old_len = length
            finalize()
            start += old_len
        elif active:
            duration += pause
        if length == 0:
            first_time = t
            active = True
        length += 1
        last_time = t
        pause = wait

    if active:
        finalize()

    samples: list[DacSample] = []
    by_slice: dict[tuple[int, int], int] = {}
    for p in plays:
        if p.start < 0:
            continue
        if p.start >= len(bank):
            continue
        end = min(len(bank), p.start + p.length)
        if end - p.start < DAC_MIN_SAMPLES:
            continue
        key = (p.start, end - p.start)
        if key in by_slice:
            continue
        data = bank[p.start:end]
        rate = max(1, min(192000, round(VGM_RATE * len(data) / max(1, p.duration))))
        idx = len(samples)
        samples.append(DacSample(p.start, len(data), data, rate, p.onset))
        by_slice[key] = idx

    notes: list[tuple[int, int, int, int]] = []
    for p in plays:
        if p.start < 0:
            continue
        end = min(len(bank), p.start + p.length)
        idx = by_slice.get((p.start, end - p.start))
        if idx is None:
            continue
        slot = idx % DAC_MAP_CAPACITY
        notes.append((p.onset, idx, 60 + slot, _pan_at(vgm, p.onset)))

    # Direct 0x2A writes are already PCM bytes, not bank references.  Group
    # contiguous writes into one slice; append them after the bank samples so
    # their sample indices are stable.
    direct = [(t, v) for t, k, v in vgm.dac_events if k == 2]
    if direct:
        run: list[tuple[int, int]] = []
        runs: list[list[tuple[int, int]]] = []
        for t, v in direct:
            if run and t - run[-1][0] > DAC_GAP:
                runs.append(run)
                run = []
            run.append((t, v))
        if run:
            runs.append(run)
        for run in runs:
            if len(run) < DAC_MIN_SAMPLES:
                continue
            first = run[0][0]
            dur = max(1, run[-1][0] - first + 1)
            data = bytes((((v & 0xFF) - 0x80) & 0xFF) for _t, v in run)
            rate = max(1, min(192000, round(VGM_RATE * len(run) / dur)))
            idx = len(samples)
            samples.append(DacSample(-1, len(data), data, rate, first))
            notes.append((first, idx, 60 + (idx % DAC_MAP_CAPACITY), _pan_at(vgm, first)))
    return samples, notes, warnings
