"""Namco C140 ROM → signed samples → Furnace.

Register model follows libvgm c140.c (the core VGM rips are logged against):

    register = voice << 4 | reg     (24 voices, 9-bit address space)
    0x00 volume R   0x01 volume L   0x02/0x03 frequency MSB/LSB   0x04 bank
    0x05 mode       0x06/0x07 start 0x08/0x09 end   0x0A/0x0B loop

Mode bits: 7 = key on (6 = retrigger when already keyed), 4 = looping,
3 = mulaw. Each address step is one sample and one ROM byte. The ROM
address is find_sample:

    adrs = (bank << 16) | start
    System 2:  ((adrs & 0x200000) >> 2) | (adrs & 0x7ffff)
    System 21: ((adrs & 0x300000) >> 1) + (adrs & 0x7ffff)

Linear bodies are the raw signed bytes. Mulaw is expanded with the full
16-bit table and stored as linear int16 so Furnace does not decode it
again. End and loop registers are address differences (sample counts).
A key-off (mode bit 7 clear while the voice plays) is collected as C140Off
so the .fur can emit note-offs. Without them a looping voice never releases.

C-4 rate uses the reference step rate from c140_rate (freq * clock / 18874368)
and the Furnace clock flag carries 2/3 of the crystal. Both Furnace's
playback and its VGM export reproduce the chip exactly. Sign-flip (bit 6)
and sign-magnitude (bit 0) are not applied.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VgmFile, assemble_rom

C140_VOICES = 24
# Reference step rate, straight from the chip model both MAME and libvgm use
# (libvgm c140.c: sample_rate = clock/288 with a .16 fixed-point position, so
# a voice steps freq * clock / (288 * 65536) address words per second).
C140_RATE_DIVISOR = 288 * 65536
# Furnace runs its own C140 core at clock/192 ticks with a plain 16-bit
# fraction, i.e. freq * clock / (192 * 65536) words per second - exactly 1.5x
# the reference. Feeding it a clock scaled by 2/3 (12288000 -> 8192000, which
# is its default) makes its playback, and the clock it writes back out on VGM
# export, land on the reference rate. The register stream is unaffected:
# BASE * rate / clock is scale-invariant.
C140_FURNACE_CLOCK_SCALE = (2, 3)


def c140_furnace_clock(clock: int) -> int:
    """C140 clock in Furnace's notation (2/3 of the crystal)."""
    return (clock or 12288000) * C140_FURNACE_CLOCK_SCALE[0] // C140_FURNACE_CLOCK_SCALE[1]


def c140_rate(clock: int, freq: int) -> float:
    """Sample frame rate for a frequency register, in Hz."""
    return (clock or 12288000) * (freq & 0xFFFF) / C140_RATE_DIVISOR


def _decompress_table() -> tuple[int, ...]:
    """MAME c140_device::device_start (verified against Starblade)."""
    tbl = []
    for i in range(256):
        j = i - 256 if i >= 128 else i
        s1 = j & 7
        s2 = abs(j >> 3) & 31
        val = ((0x80 << s1) & 0xFF00) + (s2 << (s1 + 3 if s1 else 4))
        tbl.append(-val if j < 0 else val)
    return tuple(tbl)


C140_DECOMP = _decompress_table()


def _mulaw_i16(byte: int) -> int:
    """Full table value as a signed 16-bit sample (C INT16 store)."""
    v = C140_DECOMP[byte & 0xFF] & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def c140_rom_addr(bank: int, start: int, c140_type: int) -> int:
    """libvgm find_sample. Type 0 is System 2, type 1 is System 21."""
    adrs = ((bank & 0xFF) << 16) | (start & 0xFFFF)
    if c140_type == 0:
        return ((adrs & 0x200000) >> 2) | (adrs & 0x7FFFF)
    if c140_type == 1:
        return ((adrs & 0x300000) >> 1) + (adrs & 0x7FFFF)
    return adrs


def merge_step_writes(pts: list[tuple[int, int]], gap: int = 16) -> list[tuple[int, int]]:
    """Merge register steps written within ``gap`` samples (MSB/LSB pairs).

    A 12-bit frequency update is two register writes a few samples apart; the
    intermediate value is a write-order transient; only the last write of
    each cluster is kept (same rule as the FM sweep's KC/KF merge).
    """
    merged: list[tuple[int, int]] = []
    for p in pts:
        if merged and p[0] - merged[-1][0] <= gap:
            merged[-1] = p
        else:
            merged.append(p)
    return merged


# A frequency write this soon after key-on is the note's own pitch, not a
# mid-note bend: some drivers key the voice on before setting its frequency,
# so the key-on snapshot holds stale register contents.
C140_REFIT_SAMPLES = 735  # one 60 Hz VGM frame, like the C352 refit: the C140
# drivers write the note's own pitch inside the key-on's frame (Starblade's
# Theme keys a voice with a stale 0x0d1a and writes 0x861a 40 semitones up a
# few ms later), so the first in-frame write is the note, not a slide.


def hit_played_freq(hit: "C140Hit") -> int:
    """The frequency a hit actually plays at (0 when the driver never set one).

    Key-on first, unless the driver rewrote the frequency right after keying
    on - the key-on value is the previous note's leftover then.  A late write
    still wins when the key-on value was zero: nothing was playing yet, so the
    first real frequency is the note's pitch (a stale nonzero key-on keeps
    playing until the write, so it stays the base of a bend instead).
    """
    steps = merge_step_writes(hit.freq_pts)
    if steps and steps[0][1] and (
        steps[0][0] - hit.sample_time <= C140_REFIT_SAMPLES or not (hit.freq & 0xFFFF)
    ):
        return steps[0][1]
    return hit.freq & 0xFFFF


@dataclass
class C140Hit:
    sample_time: int
    chip_id: int
    voice: int
    start: int  # address count, one byte per step
    end: int
    loop: int
    loop_on: bool
    compressed: bool
    bank: int
    freq: int
    vol_l: int
    vol_r: int
    # Key-off sample (0 = still playing at the end of the VGM). Lets a pitch
    # sweep stop at the note's end instead of running past it.
    off_sample: int = 0
    # Mid-note register writes while the voice is keyed: volume as the (L, R)
    # register pair (the balance is part of the write), frequency as the
    # combined 12-bit register. Recorded only when the value changes.
    vol_pts: list[tuple[int, int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class C140Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class C140Sample:
    start: int
    data: bytes  # signed 8-bit, or signed 16-bit LE when depth == 16
    loop_start: int | None = None
    loop_end: int | None = None
    hits: list[C140Hit] = field(default_factory=list)
    depth: int = 8

    @property
    def length(self) -> int:
        """Frame count. A mulaw body is two bytes per frame."""
        if self.depth == 16:
            return len(self.data) // 2
        return len(self.data)

    def mode_freq(self) -> int:
        """Most common frequency the sample is played at (0 when unknown).

        Uses the played frequency (key-on or the post-key-on refit write), so
        drivers that key a voice on with stale zero registers before writing
        the frequency do not pin the reference to 0.
        """
        if not self.hits:
            return 0
        freqs = Counter(f for h in self.hits if (f := hit_played_freq(h)))
        if not freqs:
            return 0
        return freqs.most_common(1)[0][0]

    def c4_rate(self, clock: int) -> int:
        return max(1000, int(round(c140_rate(clock, self.mode_freq()))))


def _covered(vgm: VgmFile, chip_id: int, addr: int) -> bool:
    """True when addr falls inside a dumped C140 ROM slice."""
    for s in vgm.roms:
        if s.chip == chip_id and s.kind == "c140" and s.data:
            if s.start <= addr < s.start + len(s.data):
                return True
    return False


def _decode_body(rom: bytes, addr: int, n_frames: int, compressed: bool) -> bytes:
    """Linear: raw signed bytes. Mulaw: int16 LE of the full table value."""
    n_frames = max(0, min(n_frames, len(rom) - addr))
    raw = rom[addr:addr + n_frames]
    if not compressed:
        return bytes(raw)
    out = bytearray()
    for b in raw:
        out += struct.pack("<h", _mulaw_i16(b))
    return bytes(out)


def _trim_frames(body: bytes, width: int, loop_off: int | None) -> int:
    """Frame count after dropping trailing silence. Never cuts the loop."""
    frames = len(body) // width
    floor = 2
    if loop_off is not None:
        floor = max(floor, loop_off + 64)
    silence = b"\x00" * width
    while frames > floor and body[(frames - 1) * width:frames * width] == silence:
        frames -= 1
    return frames


def collect_c140(
    vgm: VgmFile,
) -> tuple[bytes, list[C140Sample], list[C140Off], list[str]]:
    roms = {
        cid: assemble_rom(vgm, "c140", cid)
        for cid in sorted({w.chip_id for w in vgm.writes if w.chip == "c140"})
    }
    warnings: list[str] = []
    offs: list[C140Off] = []
    if not any(roms.values()) and not roms:
        return b"", [], offs, warnings

    reg = [[0] * 16 for _ in range(C140_VOICES)]
    keyed = [False] * C140_VOICES
    hits: list[C140Hit] = []
    active: list[C140Hit | None] = [None] * C140_VOICES
    vol_last = [(0, 0)] * C140_VOICES
    freq_last = [0] * C140_VOICES

    for w in vgm.writes:
        if w.chip != "c140":
            continue
        addr = w.reg & 0x1FF
        if addr >= 0x180:
            continue
        voice = addr >> 4
        r = addr & 0x0F
        val = w.val & 0xFF
        if r == 0x05:
            if (val & 0x80) or (val & 0x40 and keyed[voice]):
                st = (reg[voice][0x06] << 8) | reg[voice][0x07]
                en = (reg[voice][0x08] << 8) | reg[voice][0x09]
                lp = (reg[voice][0x0A] << 8) | reg[voice][0x0B]
                if active[voice] is not None:
                    active[voice].off_sample = w.sample
                hit = C140Hit(
                    sample_time=w.sample,
                    chip_id=w.chip_id,
                    voice=voice,
                    start=st,
                    end=en,
                    loop=lp,
                    loop_on=bool(val & 0x10),
                    compressed=bool(val & 0x08),
                    bank=reg[voice][0x04],
                    freq=(reg[voice][0x02] << 8) | reg[voice][0x03],
                    vol_l=reg[voice][0x01],
                    vol_r=reg[voice][0x00],
                )
                hits.append(hit)
                active[voice] = hit
                vol_last[voice] = (hit.vol_l, hit.vol_r)
                freq_last[voice] = hit.freq
                keyed[voice] = True
            else:
                if keyed[voice]:
                    offs.append(C140Off(w.sample, w.chip_id, voice))
                    if active[voice] is not None:
                        active[voice].off_sample = w.sample
                        active[voice] = None
                keyed[voice] = False
        else:
            reg[voice][r] = val
            hit = active[voice]
            if keyed[voice] and hit is not None:
                if r in (0x00, 0x01):
                    pair = (reg[voice][0x01], reg[voice][0x00])
                    if pair != vol_last[voice]:
                        hit.vol_pts.append((w.sample, pair[0], pair[1]))
                        vol_last[voice] = pair
                elif r in (0x02, 0x03):
                    f = (reg[voice][0x02] << 8) | reg[voice][0x03]
                    if f != freq_last[voice]:
                        hit.freq_pts.append((w.sample, f))
                        freq_last[voice] = f

    if not hits:
        return b"", [], offs, warnings
    if not any(roms.values()):
        warnings.append(
            "no C140 ROM block (0x8D); PCM notes will be silent until samples are added"
        )
        return b"", [], offs, warnings

    by_key: dict[tuple, C140Sample] = {}
    undumped = 0
    c140_type = getattr(vgm, "c140_type", 0) & 0x03
    for h in hits:
        rom = roms.get(h.chip_id, b"")
        if not rom:
            continue
        end_addr = max(h.end, h.start + 1)
        n_frames = end_addr - h.start
        loop_off = None
        if h.loop_on and h.start <= h.loop < end_addr:
            loop_off = h.loop - h.start
        base = c140_rom_addr(h.bank, h.start, c140_type)
        if base < 0 or base >= len(rom) or not _covered(vgm, h.chip_id, base):
            undumped += 1
            continue
        depth = 16 if h.compressed else 8
        key = (h.chip_id, h.start, end_addr, loop_off, base, h.compressed)
        smp = by_key.get(key)
        if smp is None:
            body = _decode_body(rom, base, n_frames, h.compressed)
            width = 2 if h.compressed else 1
            frames = _trim_frames(body, width, loop_off)
            if loop_off is not None and loop_off >= frames:
                loop_off = None
            smp = C140Sample(
                start=h.start,
                data=body[:frames * width],
                loop_start=loop_off,
                loop_end=frames if loop_off is not None else None,
                depth=depth,
            )
            by_key[key] = smp
        smp.hits.append(h)
    if undumped:
        warnings.append(
            f"{undumped} C140 triggers point at ROM that the VGM does not dump; "
            "those notes are silent"
        )
    samples = sorted(by_key.values(), key=lambda s: (s.start, s.loop_start or -1))
    return roms.get(0, next(iter(roms.values()), b"")), samples, offs, warnings
