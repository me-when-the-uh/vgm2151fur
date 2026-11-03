"""Yamaha YMF278B (OPL4): OPL3-shaped FM plus 24 wave channels.

The FM half is the OPL3 register map and is collected by the shared OPL path.
This module owns the wave half: `$D0 02 aa dd` writes 24 slots of 10 registers
each (`0x08-0xF7`, slot = (reg-8) % 24, sub-register = (reg-8) / 24):

    0 sample ID low          1 f-num LSB + sample ID bit 8
    2 f-num MSB + octave     3 total level
    4 key/damp/LFO/pan       5 LFO speed + vibrato
    6 AR/D1R                 7 D1L/D2R
    8 RC/RR                  9 AM

The sample ID selects a 12-byte descriptor in the wave memory: IDs below 384
live in the YRW801 ROM (descriptor at `id * 12`), the rest in the card's RAM
(descriptor at `(id - 384) * 12`). A descriptor is

    byte 0   bits 7-6 sample format (0 = 8-bit, 1 = 12-bit, 2 = 16-bit),
             bits 5-0 start address bits 16-21
    byte 1   start address bits 8-15
    byte 2   start address bits 0-7
    byte 3/4 loop address (16 bits, offset from the start)
    byte 5/6 end address (16 bits, a *negative* length: len = 0x10000 - end)
    byte 7-11 envelope/LFO defaults (not used here)

The wave memory is the ROM at 0x000000-0x1FFFFF and the RAM at 0x200000+.
Playback rate is `clock * step / (768 * 65536)` where
`step = (fnum + 1024) << (5 + octave)`.

Furnace's OPL4 core does not load the YRW801 ROM itself, so this extracts the
sample bodies and embeds them as .fur samples.
"""

from __future__ import annotations

import os
import struct
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from vgm2151fur.vgm import VGM_RATE, VgmFile

OPL4_WAVE_SLOTS = 24
OPL4_ROM_SAMPLES = 384  # sample IDs below this read the YRW801 ROM
OPL4_RAM_BASE = 0x200000  # RAM window in the wave address space
OPL4_DESCRIPTOR = 12
OPL4_DEFAULT_CLOCK = 33868800
SMP_DEPTH_8BIT = 8
SMP_DEPTH_16BIT = 16

def opl4_pan_fx(pan: int) -> int:
    """Pan nibble -> Furnace ``08xy`` that comes back as the same nibble.

    ymf278.cpp's table is 0 = centre, 1-7 = right (left falls), 8 = both off,
    9-15 = left (right falls). Furnace stores that nibble from the effect as
    ``8 ^ min(convertPanSplitToLinearLR(x, y, 15) + 1, 15)`` (opl.cpp:2267),
    which is not the naive gain split: pan 0 is centre, not hard left, and the
    sides are reversed from the raw level order. Inverting the formula here
    keeps the drum slots (pan 0) centred and every other slot on its
    hardware side. Pan 8 has no ``08xy`` form (it would need convert == -1);
    the caller mutes the note through the volume instead.
    """
    pan &= 0x0F
    if pan == 8:
        return 0
    n = 8 ^ pan
    convert = 14 if n == 15 else n - 1
    if convert <= 7:
        left, right = 15, 2 * convert + (1 if convert else 0)
    else:
        left, right = 30 - 2 * convert, 15
    return ((left & 15) << 4) | (right & 15)


# The project's own furnace/ folder (next to furnace.exe), and the same
# relative location under the current working directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_YRW801_FALLBACKS = (
    _PROJECT_ROOT / "furnace" / "yrw801.rom",
    Path.cwd() / "furnace" / "yrw801.rom",
    Path.cwd() / "yrw801.rom",
)


def _read_yrw801(candidate: Path) -> bytes:
    if candidate.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(candidate) as archive:
                for info in archive.infolist():
                    if info.file_size == 0x200000 and info.filename.lower().endswith(
                            ".rom"):
                        return archive.read(info)
        except zipfile.BadZipFile:
            return b""
        return b""
    data = candidate.read_bytes()
    return data if len(data) == 0x200000 else b""


def load_yrw801(path: str | None = None) -> bytes:
    """Read the 2 MB YRW801 ROM from a `.rom` file or a `.zip`.

    The order is: an explicit `path`, `VGM2151FUR_YRW801`, then the usual
    in_vgm/Winamp location. Furnace does not load the ROM itself (as of this
    version), so the converter extracts the ROM samples and embeds them.
    """
    candidates: list[Path | str] = []
    if path:
        candidates.append(path)
    env = os.environ.get("VGM2151FUR_YRW801")
    if env:
        candidates.append(env)
    candidates.extend(_YRW801_FALLBACKS)
    for raw in candidates:
        candidate = Path(raw)
        if not candidate.is_file():
            continue
        data = _read_yrw801(candidate)
        if data:
            return data
    return b""


def opl4_step(octave: int, fnum: int) -> int:
    """The 16.16 sample increment for an octave/f-num pair (ymf278.cpp)."""
    if octave == -8:
        return 0
    return (fnum + 1024) << (5 + octave) & 0xFFFFFFFF


def opl4_rate(clock: int, step: int) -> float:
    """Wave samples per second: output rate clock/768 times step/65536."""
    return (clock or OPL4_DEFAULT_CLOCK) * (step & 0xFFFFFFFF) / (768.0 * 65536.0)


@dataclass
class Opl4WaveHit:
    sample_time: int
    slot: int
    wave: int
    step: int
    level: int
    pan: int
    end_time: int = 0
    vol_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Opl4WaveSample:
    wave: int
    data: bytes  # signed 16-bit little-endian
    loop_start: int | None = None
    loop_end: int | None = None
    hits: list[Opl4WaveHit] = field(default_factory=list)

    @property
    def frames(self) -> int:
        return len(self.data) // 2

    def mode_step(self) -> int:
        steps = Counter(h.step for h in self.hits if h.step)
        if not steps:
            return 0
        return steps.most_common(1)[0][0]

    def c4_rate(self, clock: int) -> int:
        return max(1000, int(round(opl4_rate(clock, self.mode_step()))))


@dataclass
class _Slot:
    wave: int = 0
    fnum: int = 0
    octave: int = 0
    tl: int = 0
    pan: int = 0
    key: bool = False


def _descriptor(mem: bytes, offset: int) -> tuple[int, int, int, int] | None:
    data = mem[offset:offset + OPL4_DESCRIPTOR]
    if len(data) < OPL4_DESCRIPTOR:
        return None
    bits = (data[0] >> 6) & 0x03
    if bits == 3:
        return None
    start = data[2] | (data[1] << 8) | ((data[0] & 0x3F) << 16)
    loop = data[4] | (data[3] << 8)
    end = data[6] | (data[5] << 8)
    return bits, start, loop, end


def _s16(val: int) -> int:
    val &= 0xFFFF
    return val - 0x10000 if val & 0x8000 else val


def _body(mem: bytes, start: int, frames: int, bits: int) -> bytes:
    """Extract `frames` samples in the descriptor's format as int16 LE."""
    if frames <= 0:
        return b""
    out = bytearray(frames * 2)
    n = len(mem)
    if bits == 0:  # 8-bit signed, one byte per sample
        for pos in range(frames):
            addr = start + pos
            b = mem[addr] if addr < n else 0
            val = ((b - 0x100) << 8) if b & 0x80 else (b << 8)
            struct.pack_into("<h", out, pos * 2, val)
    elif bits == 1:  # 12-bit, two samples per three bytes
        for pos in range(frames):
            addr = start + (pos // 2) * 3
            b0 = mem[addr] if addr < n else 0
            b1 = mem[addr + 1] if addr + 1 < n else 0
            b2 = mem[addr + 2] if addr + 2 < n else 0
            if pos & 1:
                val = (b2 << 8) | (b1 & 0xF0)
            else:
                val = (b0 << 8) | ((b1 << 4) & 0xF0)
            struct.pack_into("<h", out, pos * 2, _s16(val))
    else:  # 16-bit big-endian
        for pos in range(frames):
            addr = start + pos * 2
            b0 = mem[addr] if addr < n else 0
            b1 = mem[addr + 1] if addr + 1 < n else 0
            struct.pack_into("<h", out, pos * 2, _s16((b0 << 8) | b1))
    return bytes(out)


def collect_opl4_wave(
    vgm: VgmFile, rom: bytes = b"", ram: bytes = b"",
) -> tuple[list[Opl4WaveSample], list[str]]:
    """Group the YMF278B wave plays into samples.

    `rom` is the YRW801 image (2 MB) and `ram` the 0x87 RAM dump. ROM sample
    IDs (0-383) need `rom`; RAM IDs (384-511) need `ram`.
    """
    warnings: list[str] = []
    slots = [_Slot() for _ in range(OPL4_WAVE_SLOTS)]
    hits: list[Opl4WaveHit] = []
    # The hit currently sounding on a slot, so a $5n level write inside the
    # note becomes a volume step instead of being lost.
    active: list[Opl4WaveHit | None] = [None] * OPL4_WAVE_SLOTS

    for w in vgm.writes:
        if w.chip != "ymf278b" or (w.reg >> 8) != 2:
            continue
        r = w.reg & 0xFF
        if not 0x08 <= r <= 0xF7:
            continue
        slot = (r - 8) % OPL4_WAVE_SLOTS
        sub = (r - 8) // OPL4_WAVE_SLOTS
        st = slots[slot]
        if sub == 0:
            st.wave = (st.wave & 0x100) | (w.val & 0xFF)
        elif sub == 1:
            st.wave = (st.wave & 0xFF) | ((w.val & 0x01) << 8)
            st.fnum = (st.fnum & 0x380) | (w.val >> 1)
        elif sub == 2:
            st.fnum = (st.fnum & 0x07F) | ((w.val & 0x07) << 7)
            octave = (w.val >> 4) & 0x0F
            st.octave = octave - 16 if octave & 0x08 else octave
        elif sub == 3:
            level = (w.val >> 1) & 0x7F
            hit = active[slot]
            if st.key and hit is not None:
                if w.sample == hit.sample_time:
                    # Same write group as the key-on: the note attacks at
                    # this level, so it belongs in the instrument column
                    # rather than in a step on the note row.
                    hit.level = level
                else:
                    last = hit.vol_pts[-1][1] if hit.vol_pts else hit.level
                    if level != last:
                        hit.vol_pts.append((w.sample, level))
            st.tl = level
        elif sub == 4:
            st.pan = w.val & 0x0F
            key = bool(w.val & 0x80)
            if key and not st.key:
                hit = Opl4WaveHit(
                    sample_time=w.sample,
                    slot=slot,
                    wave=st.wave,
                    step=opl4_step(st.octave, st.fnum),
                    level=st.tl,
                    pan=st.pan,
                )
                hits.append(hit)
                active[slot] = hit
            elif st.key and not key:
                # Attach the release now. Waiting for the next key-on (or
                # the end of the file) lost every intermediate note-off.
                hit = active[slot]
                if hit is not None and not hit.end_time:
                    hit.end_time = w.sample
                active[slot] = None
            st.key = key

    if not hits:
        return [], warnings

    by_wave: dict[int, Opl4WaveSample] = {}
    rom_missing = False
    for hit in hits:
        wave = hit.wave
        if wave < OPL4_ROM_SAMPLES:
            if not rom:
                rom_missing = True
                continue
            desc = _descriptor(rom, wave * OPL4_DESCRIPTOR)
            mem = rom
            base = 0
        else:
            if not ram:
                continue
            desc = _descriptor(ram, (wave - OPL4_ROM_SAMPLES) * OPL4_DESCRIPTOR)
            mem = ram
            base = OPL4_RAM_BASE
        if desc is None:
            continue
        bits, start, loop, end = desc
        frames = 0x10000 - end
        if frames <= 2:
            continue
        offset = start - base
        if offset < 0 or offset >= len(mem):
            continue
        sample = by_wave.get(wave)
        if sample is None:
            # A loop point in the last two frames is the descriptor's loop
            # padding, not a sustain: the reference modules trim the body to
            # the loop point and play it once. A full 0x10000-frame body
            # wraps the slot's 16-bit position counter, so its loop address
            # cannot take effect either; those are one-shot too.
            if frames >= 0x10000:
                loop_start = None
            elif loop >= frames - 2 and loop > 0:
                frames = loop
                loop_start = None
            else:
                loop_start = loop if 0 < loop < frames else None
            data = _body(mem, offset, frames, bits)
            sample = Opl4WaveSample(
                wave=wave, data=data, loop_start=loop_start,
                loop_end=frames if loop_start is not None else None,
            )
            by_wave[wave] = sample
        sample.hits.append(hit)

    if rom_missing:
        warnings.append(
            "YMF278B wave notes use the YRW801 ROM; set VGM2151FUR_YRW801 to "
            "its path to extract those samples"
        )
    samples = sorted(by_wave.values(), key=lambda s: s.wave)
    return samples, warnings
