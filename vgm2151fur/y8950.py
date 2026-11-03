"""Yamaha Y8950: OPL1 FM plus one delta-T (ADPCM-B) voice.

The FM half is an OPL1/OPL2 register map and is collected by the shared OPL
path (`KIND_Y8950` in opl.py). This module owns the ADPCM half, which is the
same delta-T engine the YM2608 carries, exposed at registers `$07`-`$12`:

    0x07 control      bit 7 execute, bit 4 repeat, bit 0 reset
    0x08 pan/control  bit 7 left, bit 6 right, low bits ROM/RAM select
    0x09/0x0A start  low/high, in 32-byte units
    0x0B/0x0C end    low/high, in 32-byte units
    0x10/0x11 pitch  low/high, the ADPCM-B delta
    0x12 level       8-bit

The ROM is data block type `0x88`. Address shift, rate divider, byte
slicing and the 256-byte body padding are the YM2608's (`ADPCM_B_DIV`,
`_deltat_slice`, `deltat_store_body`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, assemble_rom, VgmFile
from vgm2151fur.ym2608 import _deltat_slice, deltat_store_body
from vgm2151fur.ym2610 import (
    ADPCM_B_DIV,
    AdpcmBHit,
    AdpcmBSample,
)

Y8950_DEFAULT_CLOCK = 3579545
ADPCM_B_ADDR_SHIFT = 5  # 32-byte address units
# How soon after the start bit a driver writes the sample's own pan/level/rate.
_Y8950_GATE = 64


@dataclass
class _State:
    start: int = 0
    end: int = 0
    freq: int = 0
    vol: int = 0
    pan: int = 0


def _pan_byte(pan_reg: int) -> int:
    """The engine's 2-bit L/R field as a Furnace 08xy byte."""
    left = bool(pan_reg & 0x80)
    right = bool(pan_reg & 0x40)
    if left and right:
        return 0xFF
    if left:
        return 0xF0
    if right:
        return 0x0F
    # The Y8950's ADPCM is mono; the platform forces bit 7. A cleared field
    # still plays, so treat it as centred rather than silent.
    return 0xFF


def collect_y8950_delta_t(
    vgm: VgmFile, rom: bytes | None = None,
) -> tuple[list[AdpcmBSample], list[str]]:
    """Group the Y8950's delta-T plays into samples."""
    warnings: list[str] = []
    clock = vgm.y8950_clock or Y8950_DEFAULT_CLOCK
    if rom is None:
        rom = assemble_rom(vgm, "y8950_deltat")
    state = _State()
    active: AdpcmBHit | None = None
    hits: list[AdpcmBHit] = []

    def advance(hit: AdpcmBHit, t: int) -> bool:
        if hit.ended:
            return False
        dt = t - hit.play_t
        if dt > 0 and not hit.loop:
            rate_hz = clock / ADPCM_B_DIV * (hit.play_rate & 0xFFFF) / 65535.0
            hit.play_pos += dt * rate_hz / VGM_RATE
        hit.play_t = t
        if not hit.loop and hit.play_pos >= (hit.end - hit.start + 1):
            hit.ended = True
            return False
        return True

    for w in vgm.writes:
        if w.chip != "y8950":
            continue
        r = w.reg & 0xFF
        val = w.val & 0xFF
        if r == 0x09:
            state.start = (state.start & 0xFF00) | val
        elif r == 0x0A:
            state.start = (state.start & 0x00FF) | (val << 8)
        elif r == 0x0B:
            state.end = (state.end & 0xFF00) | val
        elif r == 0x0C:
            state.end = (state.end & 0x00FF) | (val << 8)
        elif r == 0x10:
            state.freq = (state.freq & 0xFF00) | val
        elif r == 0x11:
            state.freq = (state.freq & 0x00FF) | (val << 8)
        elif r == 0x12:
            state.vol = val
        elif r == 0x08:
            state.pan = val
        if r != 0x07:
            continue
        # Control write: start, stop, or reset.
        start = bool(val & 0x80)
        reset = bool(val & 0x01)
        old = active
        if not start or reset:
            if old is not None:
                old.end_time = w.sample
                active = None
            continue
        if old is not None:
            old.end_time = w.sample
        lo = state.start << ADPCM_B_ADDR_SHIFT
        hi = ((state.end + 1) << ADPCM_B_ADDR_SHIFT) - 1
        if hi < lo:
            hi = lo + (1 << ADPCM_B_ADDR_SHIFT) - 1
        hit = AdpcmBHit(
            sample_time=w.sample,
            chip_id=0,
            start=lo,
            end=hi,
            rate=state.freq,
            vol=state.vol,
            pan=_pan_byte(state.pan),
            loop=bool(val & 0x10),
            play_t=w.sample,
            play_rate=state.freq,
        )
        hits.append(hit)
        active = hit

    if active is not None and not active.end_time:
        active.end_time = vgm.total_samples or 0

    # A level rewrite while the sample sounds is a mid-note step.
    for hit in hits:
        for w in vgm.writes:
            if w.chip != "y8950" or (w.reg & 0xFF) != 0x12:
                continue
            if not (hit.sample_time < w.sample <= (hit.end_time or hit.sample_time)):
                continue
            level = w.val & 0xFF
            last = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
            if level != last:
                hit.vol_pts.append((w.sample, level))

    samples: dict[tuple[int, int, int, int], AdpcmBSample] = {}
    for hit in hits:
        key = (hit.chip_id, hit.start, hit.end, hit.rate)
        smp = samples.get(key)
        if smp is None:
            data = _deltat_slice(rom, hit.start, hit.end)
            smp = AdpcmBSample(hit.chip_id, hit.start, hit.end, hit.rate, data)
            samples[key] = smp
        smp.hits.append(hit)

    if hits and not rom:
        warnings.append(
            "Y8950 ADPCM key-ons but no delta-T data block (0x88); the "
            "ADPCM channel is silent"
        )
    return list(samples.values()), warnings
