"""Namco C352 ROM → signed samples → Furnace.

Register model follows libvgm c352.c (the core current VGM rips are logged
against). 32 voices. Each voice is eight uint16 registers at address
``voice * 8 + reg``:

    0 vol_f (front L/R)   1 vol_r (rear L/R)   2 freq   3 flags
    4 wave_bank           5 wave_start         6 wave_end   7 wave_loop

Key on and key off are latched in the flag word and executed by a write to
0x202. The strobe value is ignored. A second strobe without a new key-on bit
does not restart the voice.

Playback: output rate is clock/288, and the phase accumulator steps ``freq``
per output sample with a 16-bit fraction, so a voice plays
``clock * freq / (288 * 65536)`` ROM bytes per second. The .fur stores that
rate and the real crystal. A Furnace core copied from c140.cpp ticks at
clock/192 (1.5×) and needs the 2/3 clock scale the C140 flag already uses;
this module does not pre-apply that scale.

The end address is exclusive on a one-shot (the chip zeros that fetch) and
inclusive on a loop. Mulaw uses the C352 table, not the C140 table, and is
stored as linear int16. Phase invert, the FM flag, the filter bit, noise,
and the hardware volume slew are not applied: the target volume registers
are what the pattern receives.

Mid-note loop clear: System 12 drivers key a voice with LOOP set and clear
the flag while it runs, letting the sample finish at wave_end. The model
tracks the played frame count from the frequency trajectory and schedules a
note-off when the voice reaches the end, so the module stops the sample
instead of looping it forever. The stop is dropped when the voice is
retriggered, keyed off, or given its loop back first.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, VgmFile, assemble_rom

C352_VOICES = 32
C352_RATE_DIVISOR = 288 * 65536
C352_DEFAULT_CLOCK = 24_192_000
C352_REFIT_SAMPLES = 735  # one 60 Hz VGM frame: these drivers strobe first and
# write the note's own pitch right after, inside the same frame. A first write
# in that window is the note pitch, not a slide.
C352_MAX_FRAMES = 1 << 20

FLG_BUSY = 0x8000
FLG_KEYON = 0x4000
FLG_KEYOFF = 0x2000
FLG_LOOPHIST = 0x0800
FLG_LDIR = 0x0040
FLG_LINK = 0x0020
FLG_NOISE = 0x0010
FLG_MULAW = 0x0008
FLG_LOOP = 0x0002
FLG_REVERSE = 0x0001

REG_VOL_F = 0
REG_VOL_R = 1
REG_FREQ = 2
REG_FLAGS = 3
REG_BANK = 4
REG_START = 5
REG_END = 6
REG_LOOP = 7
ADDR_STROBE = 0x202


def c352_rate(clock: int, freq: int) -> float:
    """ROM bytes per second for a frequency register."""
    return (clock or C352_DEFAULT_CLOCK) * (freq & 0xFFFF) / C352_RATE_DIVISOR


def _build_mulaw() -> tuple[int, ...]:
    """libvgm device_start_c352. Byte 0x01 expands to 32, not C140's 256."""
    pos = [0] * 128
    step = 0
    for i in range(128):
        pos[i] = step << 5
        if i < 16:
            step += 1
        elif i < 24:
            step += 2
        elif i < 48:
            step += 4
        elif i < 100:
            step += 8
        else:
            step += 16
    out: list[int] = []
    for i in range(256):
        if i < 128:
            out.append(pos[i])
            continue
        v = (~pos[i - 128]) & ~0x1F & 0xFFFF
        if v & 0x8000:
            v -= 0x10000
        out.append(v)
    return tuple(out)


C352_MULAW = _build_mulaw()


def _mulaw_i16(byte: int) -> int:
    return C352_MULAW[byte & 0xFF]


def merge_step_writes(pts: list[tuple[int, int]], gap: int = 16) -> list[tuple[int, int]]:
    """Keep the last write of each cluster closer than ``gap`` samples."""
    merged: list[tuple[int, int]] = []
    for p in pts:
        if merged and p[0] - merged[-1][0] <= gap:
            merged[-1] = p
        else:
            merged.append(p)
    return merged


def hit_played_freq(hit: "C352Hit") -> int:
    """Frequency the hit plays at. A write just after key-on replaces a stale 0."""
    steps = merge_step_writes(hit.freq_pts)
    if steps and steps[0][1] and (
        steps[0][0] - hit.sample_time <= C352_REFIT_SAMPLES or not (hit.freq & 0xFFFF)
    ):
        return steps[0][1]
    return hit.freq & 0xFFFF


def _mix(vol_f: int, vol_r: int, mute_rear: bool) -> tuple[int, int]:
    """Front pair, plus the rear pair unless the header mutes it. Each side clamps at 255."""
    left = (vol_f >> 8) & 0xFF
    right = vol_f & 0xFF
    if not mute_rear:
        left = min(255, left + ((vol_r >> 8) & 0xFF))
        right = min(255, right + (vol_r & 0xFF))
    return left, right


@dataclass
class C352Hit:
    sample_time: int
    chip_id: int
    voice: int
    bank: int
    start: int
    end: int
    loop: int
    flags: int
    freq: int
    vol_l: int
    vol_r: int
    off_sample: int = 0
    vol_pts: list[tuple[int, int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    # The live (wave_start, wave_loop) the chip reads when the running
    # address reaches wave_end, recorded when the driver rewrites them
    # before the voice gets there. None means the key-on values.
    geo_at_cross: tuple[int, int] | None = None


@dataclass
class C352Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class C352Sample:
    start: int
    data: bytes
    loop_start: int | None = None
    loop_end: int | None = None
    hits: list[C352Hit] = field(default_factory=list)
    depth: int = 8
    loop_mode: int = 0  # Furnace SMP2: 0 forward, 2 ping-pong

    @property
    def length(self) -> int:
        if self.depth == 16:
            return len(self.data) // 2
        return len(self.data)

    def mode_freq(self) -> int:
        if not self.hits:
            return 0
        freqs = Counter(f for h in self.hits if (f := hit_played_freq(h)))
        if not freqs:
            return 0
        return freqs.most_common(1)[0][0]

    def c4_rate(self, clock: int) -> int:
        played = c352_rate(clock, self.mode_freq())
        if played <= 0:
            return 1000
        return max(1000, int(round(played)))


def _rom_mask(vgm: VgmFile, chip_id: int) -> int:
    """The wave ROM reads through pow2_mask(size), libvgm's window mask.

    The chip's address bus folds modulo the ROM window: drivers hand it
    0x80B1D1AE-style addresses (a ROM base byte on the 24-bit offset) and the
    core reads ``wave[pos & wave_mask]``, so the alias must address the same
    bytes as its low-24-bit form. The declared block size is the window.
    """
    sizes = [s.rom_size for s in vgm.roms if s.chip == chip_id and s.kind == "c352" and s.data]
    size = max(sizes, default=0)
    if size <= 0:
        return 0
    return (1 << (size - 1).bit_length()) - 1


def _covered(vgm: VgmFile, chip_id: int, addr: int) -> bool:
    addr &= _rom_mask(vgm, chip_id)
    for s in vgm.roms:
        if s.chip == chip_id and s.kind == "c352" and s.data:
            if s.start <= addr < s.start + len(s.data):
                return True
    return False


def _read(rom: bytes, vgm: VgmFile, chip_id: int, off: int, n: int) -> bytes | None:
    if n <= 0:
        return b""
    if off < 0:
        return None
    mask = _rom_mask(vgm, chip_id)
    start = off & mask
    if not _covered(vgm, chip_id, start):
        return None
    if start + n <= len(rom) and start + n <= mask + 1:
        return bytes(rom[start:start + n])
    # the span wraps the window: the core's counter keeps rising and every read
    # folds, so the bytes continue from the start of the ROM image
    out = bytearray()
    for i in range(n):
        a = (start + i) & mask
        if a >= len(rom):
            return None
        out.append(rom[a])
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


def _blank_state() -> dict:
    return {
        "reg": [[0] * 8 for _ in range(C352_VOICES)],
        "keyed": [False] * C352_VOICES,
        "active": [None] * C352_VOICES,
        "vol": [(0, 0)] * C352_VOICES,
        "freq": [0] * C352_VOICES,
        "played": [0.0] * C352_VOICES,  # frames played since key-on, integrated
        "anchor": [0] * C352_VOICES,    # sample time the integration is up to date to
        "stop": [None] * C352_VOICES,   # pending natural end after a mid-note loop clear
        "hard": [None] * C352_VOICES,   # immediate stop after a mid-note BUSY clear
    }


def _plan(hit: C352Hit, rom: bytes, vgm: VgmFile, acc: dict[str, int]) -> dict | None:
    """Describe the bytes a hit plays. None means the hit produces no sample."""
    flags = hit.flags
    mulaw = bool(flags & FLG_MULAW)
    looping = bool(flags & FLG_LOOP)
    reverse = bool(flags & FLG_REVERSE)
    link = bool(flags & FLG_LINK)
    bank = hit.bank & 0xFFFF
    start = hit.start & 0xFFFF
    end = hit.end & 0xFFFF
    loop = hit.loop & 0xFFFF
    if hit.geo_at_cross is not None:
        # the jump uses the live wave_loop and wave_start (the LINK target's
        # high half); end and bank keep their key-on values. The walk still
        # starts at the key-on address.
        start_l, loop = hit.geo_at_cross
        loop &= 0xFFFF
        start_l &= 0xFFFF
    else:
        start_l = start
    base = (bank << 16) | start

    if reverse and looping:
        lo = min(loop, end)
        hi = max(loop, end)
        # a voice that starts below the bounce region plays through the
        # pre-attack first; keep those bytes and put the loop at the region.
        body_start = start if start < lo else lo
        if start != body_start:
            acc["ping"] = acc.get("ping", 0) + 1
        rom_off = (bank << 16) | body_start
        n = (hi - body_start) + 1
        if n > C352_MAX_FRAMES:
            acc["cut"] = acc.get("cut", 0) + 1
            n = C352_MAX_FRAMES
        loop_off = lo - body_start
        if loop_off >= n:
            loop_off = 0
        return {
            "rom_off": rom_off,
            "chunks": [(rom_off, n)],
            "n": n,
            "loop_off": loop_off,
            "loop_mode": 2,
            "mulaw": mulaw,
            "reversed": False,
            "link_at": 0,
            "start": rom_off,
        }

    if reverse:
        if start == end:
            return None
        if start < end and bank == 0:
            # the walk would leave the ROM window (the reference reads nothing
            # there), so the voice is silent
            acc["reverse_wrap"] = acc.get("reverse_wrap", 0) + 1
            return None
        # a reverse voice walks down from start and stops at wave_end, so the
        # body is the descending run [end + 1, start] (wrapping into the
        # previous bank when start < end).
        n = (start - end) & 0xFFFF
        hi = base
        lo = hi - (n - 1)
        if lo < 0:
            return None
        return {
            "rom_off": lo,
            "chunks": [(lo, n)],
            "n": n,
            "loop_off": None,
            "loop_mode": 0,
            "mulaw": mulaw,
            "reversed": True,
            "link_at": 0,
            "start": base,
        }

    span = (end - start) & 0xFFFF
    if not looping:
        if span == 0:
            return None
        n = span
        if n > C352_MAX_FRAMES:
            acc["cut"] = acc.get("cut", 0) + 1
            n = C352_MAX_FRAMES
        return {
            "rom_off": base,
            "chunks": [(base, n)],
            "n": n,
            "loop_off": None,
            "loop_mode": 0,
            "mulaw": mulaw,
            "reversed": False,
            "link_at": 0,
            "start": base,
        }

    n = span + 1
    loop_off = (loop - start) & 0xFFFF
    if n > C352_MAX_FRAMES:
        acc["cut"] = acc.get("cut", 0) + 1
        n = C352_MAX_FRAMES
        if loop_off >= n:
            loop_off = 0

    # A note that is stopped before the running address reaches wave_end never
    # loops, so the loop region does not have to be carried (the module would
    # only hold bytes its playback never reaches). off_sample 0 means the note
    # was never stopped and runs to the end.
    reaches_loop = hit.off_sample == 0 or (hit.off_sample - hit.sample_time) >= n

    if link:
        intro_n = n
        target = (start_l << 16) | loop
        end_addr = base + intro_n - 1
        arrival_bank = (end_addr >> 16) & 0xFFFF
        normal = (arrival_bank << 16) | loop
        if target != normal and not reaches_loop:
            return {
                "rom_off": base,
                "chunks": [(base, intro_n)],
                "n": intro_n,
                "loop_off": None,
                "loop_mode": 0,
                "mulaw": mulaw,
                "reversed": False,
                "link_at": 0,
                "start": base,
            }
        if target != normal:
            loop_n = ((end - loop) & 0xFFFF) + 1
            if intro_n + loop_n > C352_MAX_FRAMES:
                acc["cut"] = acc.get("cut", 0) + 1
                loop_n = max(0, C352_MAX_FRAMES - intro_n)
            loop_body = _read(rom, vgm, hit.chip_id, target, loop_n)
            if loop_body is None or len(loop_body) < loop_n:
                acc["link_out"] = acc.get("link_out", 0) + 1
                # The chip jumps into ROM this rip does not dump; that region
                # reads as silence, so only the intro is heard - once. It must
                # not loop, or an ambient that goes quiet on the source keeps
                # droning in the module.
                return {
                    "rom_off": base,
                    "chunks": [(base, intro_n)],
                    "n": intro_n,
                    "loop_off": None,
                    "loop_mode": 0,
                    "mulaw": mulaw,
                    "reversed": False,
                    "link_at": 0,
                    "start": base,
                }
            else:
                return {
                    "rom_off": base,
                    "chunks": [(base, intro_n), (target, loop_n)],
                    "n": intro_n + loop_n,
                    "loop_off": intro_n,
                    "loop_mode": 0,
                    "mulaw": mulaw,
                    "reversed": False,
                    "link_at": target,
                    "start": base,
                }

    if loop_off >= n:
        # The loop register lies outside the sampled span: a loop register one
        # byte past wave_end. The chip jumps at wave_end to the arrival bank's
        # copy of `loop`, plays there until the low 16 bits meet wave_end again
        # (a full bank for the +1 case) and repeats. Without the region the
        # sample plays its intro once and stops.
        if not reaches_loop:
            return {
                "rom_off": base,
                "chunks": [(base, n)],
                "n": n,
                "loop_off": None,
                "loop_mode": 0,
                "mulaw": mulaw,
                "reversed": False,
                "link_at": 0,
                "start": base,
            }
        loop_addr = ((base + n - 1) & 0xFF0000) | loop
        loop_n = ((end - loop) & 0xFFFF) + 1
        if n + loop_n > C352_MAX_FRAMES:
            acc["cut"] = acc.get("cut", 0) + 1
            loop_n = max(0, C352_MAX_FRAMES - n)
        tail = _read(rom, vgm, hit.chip_id, loop_addr, loop_n) if loop_n else None
        if tail is None or len(tail) < loop_n:
            acc["link_out"] = acc.get("link_out", 0) + 1
            return {
                "rom_off": base,
                "chunks": [(base, n)],
                "n": n,
                "loop_off": None,
                "loop_mode": 0,
                "mulaw": mulaw,
                "reversed": False,
                "link_at": 0,
                "start": base,
            }
        return {
            "rom_off": base,
            "chunks": [(base, n), (loop_addr, loop_n)],
            "n": n + loop_n,
            "loop_off": n,
            "loop_mode": 0,
            "mulaw": mulaw,
            "reversed": False,
            "link_at": loop_addr,
            "start": base,
        }

    return {
        "rom_off": base,
        "chunks": [(base, n)],
        "n": n,
        "loop_off": loop_off,
        "loop_mode": 0,
        "mulaw": mulaw,
        "reversed": False,
        "link_at": 0,
        "start": base,
    }


def _decode(raw: bytes, mulaw: bool, reversed_play: bool) -> tuple[bytes, int]:
    if reversed_play:
        raw = raw[::-1]
    if not mulaw:
        return bytes(raw), 8
    out = bytearray()
    for b in raw:
        out += struct.pack("<h", _mulaw_i16(b))
    return bytes(out), 16


def _flush(acc: dict[str, int], warnings: list[str]) -> None:
    if acc.get("undumped"):
        warnings.append(
            f"{acc['undumped']} C352 triggers point at ROM that the VGM does not dump; "
            "those notes are silent"
        )
    if acc.get("noise"):
        warnings.append(f"{acc['noise']} C352 noise voices are not converted")
    if acc.get("reverse_wrap"):
        warnings.append(
            f"{acc['reverse_wrap']} C352 reverse voices wrap past the ROM window; "
            "those notes are silent"
        )
    if acc.get("link_out"):
        warnings.append(
            f"{acc['link_out']} C352 link targets fall outside the dumped ROM; "
            "those notes play the intro once (the chip reads silence past it)"
        )
    if acc.get("ping"):
        warnings.append(
            f"{acc['ping']} C352 ping-pong voices start inside the loop; "
            "the attack before the bounce is omitted"
        )
    if acc.get("cut"):
        warnings.append(f"{acc['cut']} C352 samples were cut at {C352_MAX_FRAMES} frames")


def collect_c352(
    vgm: VgmFile,
) -> tuple[bytes, list[C352Sample], list[C352Off], list[str]]:
    roms = {
        cid: assemble_rom(vgm, "c352", cid)
        for cid in sorted({w.chip_id for w in vgm.writes if w.chip == "c352"})
    }
    warnings: list[str] = []
    offs: list[C352Off] = []
    if not roms:
        return b"", [], offs, warnings

    mute_rear = bool(getattr(vgm, "c352_mute_rear", False))
    chips: dict[int, dict] = {}
    hits: list[C352Hit] = []
    acc: dict[str, int] = {}

    def state(cid: int) -> dict:
        st = chips.get(cid)
        if st is None:
            st = _blank_state()
            chips[cid] = st
        return st

    # Drivers key a voice with LOOP set and clear the loop flag while it runs,
    # so the chip finishes the sample at wave_end and stops. The flags snapshot
    # at key-on keeps the note looping forever in the module, so a stop is
    # scheduled for the time the voice reaches wave_end. It is cancelled by a
    # retrigger, a key-off, a loop re-set, or a direction change.
    clock = vgm.c352_clock or C352_DEFAULT_CLOCK

    def advance(st: dict, voice: int, t: int) -> None:
        """Integrate frames played up to time t (VGM samples)."""
        dt = t - st["anchor"][voice]
        if dt <= 0:
            return
        freq = st["freq"][voice]
        if freq:
            st["played"][voice] += c352_rate(clock, freq) * dt / VGM_RATE
        st["anchor"][voice] = t

    def natural_end(st: dict, voice: int, t: int) -> int | None:
        """Time the voice reaches wave_end with the loop off, from its position."""
        reg = st["reg"]
        start = reg[voice][REG_START] & 0xFFFF
        end = reg[voice][REG_END] & 0xFFFF
        loop = reg[voice][REG_LOOP] & 0xFFFF
        first = end - start
        if first <= 0:
            return None
        played = st["played"][voice]
        if played < first:
            pos = start + played
        else:
            wrap = end - loop
            if wrap <= 0:
                return None
            pos = loop + (played - first) % wrap
        remaining = end - pos
        freq = st["freq"][voice]
        if remaining <= 0 or not freq:
            return t
        rate = c352_rate(clock, freq)
        if rate <= 0:
            return None
        return t + int(round(remaining * VGM_RATE / rate))

    def cancel_stop(st: dict, voice: int) -> None:
        """A key event or loop re-arm supersedes a pending stop."""
        st["stop"][voice] = None

    def flush_stop(st: dict, voice: int, cid: int, t: int, cancel: bool) -> None:
        """Emit a pending stop that has passed; keep a future one unless told to cancel."""
        stop = st["stop"][voice]
        if stop is None:
            return
        if stop <= t:
            offs.append(C352Off(stop, cid, voice))
            if st["active"][voice] is not None:
                st["active"][voice].off_sample = stop
            st["active"][voice] = None  # the voice finished on its own
            st["stop"][voice] = None
        elif cancel:
            st["stop"][voice] = None

    def flush_hard(st: dict, voice: int, cid: int, t: int, cancel: bool) -> None:
        """Emit an immediate stop that has passed; a key event drops it silently."""
        hard = st["hard"][voice]
        if hard is None:
            return
        if cancel:
            st["hard"][voice] = None
        elif hard <= t:
            offs.append(C352Off(hard, cid, voice))
            if st["active"][voice] is not None:
                st["active"][voice].off_sample = hard
            st["active"][voice] = None
            st["hard"][voice] = None

    for w in vgm.writes:
        if w.chip != "c352":
            continue
        st = state(w.chip_id)
        reg = st["reg"]
        addr = w.reg & 0x7FFF
        if addr == ADDR_STROBE:
            for voice in range(C352_VOICES):
                flags = reg[voice][REG_FLAGS]
                if flags & FLG_KEYON:
                    # a retrigger supersedes a future natural end and a BUSY clear
                    flush_stop(st, voice, w.chip_id, w.sample, cancel=True)
                    flush_hard(st, voice, w.chip_id, w.sample, cancel=True)
                    active = st["active"][voice]
                    st["played"][voice] = 0.0
                    st["anchor"][voice] = w.sample
                    if flags & FLG_NOISE:
                        if active is not None:
                            active.off_sample = w.sample
                            offs.append(C352Off(w.sample, w.chip_id, voice))
                            st["active"][voice] = None
                        st["keyed"][voice] = False
                        acc["noise"] = acc.get("noise", 0) + 1
                    else:
                        if active is not None:
                            active.off_sample = w.sample
                        vol_l, vol_r = _mix(
                            reg[voice][REG_VOL_F], reg[voice][REG_VOL_R], mute_rear,
                        )
                        hit = C352Hit(
                            sample_time=w.sample,
                            chip_id=w.chip_id,
                            voice=voice,
                            bank=reg[voice][REG_BANK],
                            start=reg[voice][REG_START],
                            end=reg[voice][REG_END],
                            loop=reg[voice][REG_LOOP],
                            flags=flags,
                            freq=reg[voice][REG_FREQ] & 0xFFFF,
                            vol_l=vol_l,
                            vol_r=vol_r,
                        )
                        hits.append(hit)
                        st["active"][voice] = hit
                        st["keyed"][voice] = True
                        st["vol"][voice] = (vol_l, vol_r)
                        st["freq"][voice] = hit.freq
                    reg[voice][REG_FLAGS] = (flags | FLG_BUSY) & ~(FLG_KEYON | FLG_LOOPHIST)
                elif flags & FLG_KEYOFF:
                    # the write already silenced the voice; the strobe only adds
                    # a second stop when none has fired
                    flush_hard(st, voice, w.chip_id, w.sample, cancel=False)
                    flush_stop(st, voice, w.chip_id, w.sample, cancel=True)
                    if st["keyed"][voice] and st["active"][voice] is not None:
                        offs.append(C352Off(w.sample, w.chip_id, voice))
                        active = st["active"][voice]
                        if active is not None:
                            active.off_sample = w.sample
                            st["active"][voice] = None
                    st["keyed"][voice] = False
                    reg[voice][REG_FLAGS] = flags & ~(FLG_BUSY | FLG_KEYOFF)
                else:
                    # a natural end or BUSY clear that has passed fires
                    flush_stop(st, voice, w.chip_id, w.sample, cancel=False)
                    flush_hard(st, voice, w.chip_id, w.sample, cancel=False)
            continue
        if addr >= 0x100:
            continue
        voice = addr >> 3
        r = addr & 7
        val = w.val & 0xFFFF
        prev = reg[voice][r]
        reg[voice][r] = val
        if not st["keyed"][voice]:
            continue
        hit = st["active"][voice]
        if hit is None:
            continue
        advance(st, voice, w.sample)
        if r in (REG_VOL_F, REG_VOL_R):
            pair = _mix(reg[voice][REG_VOL_F], reg[voice][REG_VOL_R], mute_rear)
            if pair != st["vol"][voice]:
                hit.vol_pts.append((w.sample, pair[0], pair[1]))
                st["vol"][voice] = pair
        elif r == REG_FREQ:
            f = val & 0xFFFF
            if f != st["freq"][voice]:
                hit.freq_pts.append((w.sample, f))
                st["freq"][voice] = f
            if st["stop"][voice] is not None:
                st["stop"][voice] = natural_end(st, voice, w.sample)
        elif r == REG_FLAGS:
            if (prev ^ val) & FLG_REVERSE:
                # direction change: the run-to-end model no longer applies
                cancel_stop(st, voice)
            if (prev & FLG_BUSY) and not (val & FLG_BUSY):
                # the write clears BUSY: the chip stops the voice at once
                cancel_stop(st, voice)
                st["hard"][voice] = w.sample
            else:
                old_loop = prev & FLG_LOOP
                new_loop = val & FLG_LOOP
                if old_loop and not new_loop and not (val & FLG_REVERSE):
                    # the driver lets the voice finish at wave_end: schedule the stop
                    st["stop"][voice] = natural_end(st, voice, w.sample)
                elif not old_loop and new_loop:
                    # loops again: a stop that already passed still counts
                    cancel_stop(st, voice)
        elif r in (REG_START, REG_LOOP):
            # The chip jumps with the live registers when the running address
            # reaches wave_end, and these drivers rewrite start/loop a few ms
            # after the strobe, pointing the LINK jump at the byte after the
            # intro (the sample's real continuation). The body must use the
            # rewrite; a write after the crossing is for the next pass. end
            # and bank are also rewritten mid-note, but as stop/shorten
            # tricks, so the key-on values stay in charge.
            if st["played"][voice] < ((reg[voice][REG_END] - hit.start) & 0xFFFF):
                hit.geo_at_cross = (reg[voice][REG_START], reg[voice][REG_LOOP])
        elif r in (REG_END, REG_LOOP):
            if st["stop"][voice] is not None:
                st["stop"][voice] = natural_end(st, voice, w.sample)

    for cid, stp in chips.items():
        for voice in range(C352_VOICES):
            stop = stp["stop"][voice]
            if stop is not None:
                offs.append(C352Off(stop, cid, voice))
                if stp["active"][voice] is not None:
                    stp["active"][voice].off_sample = stop
                stp["stop"][voice] = None
            hard = stp["hard"][voice]
            if hard is not None:
                offs.append(C352Off(hard, cid, voice))
                if stp["active"][voice] is not None:
                    stp["active"][voice].off_sample = hard
                stp["hard"][voice] = None

    if not hits:
        _flush(acc, warnings)
        return b"", [], offs, warnings
    if not any(roms.values()):
        warnings.append(
            "no C352 ROM block (0x92); PCM notes will be silent until samples are added"
        )
        _flush(acc, warnings)
        return b"", [], offs, warnings

    by_key: dict[tuple, C352Sample] = {}
    for h in hits:
        rom = roms.get(h.chip_id, b"")
        if not rom:
            acc["undumped"] = acc.get("undumped", 0) + 1
            continue
        plan = _plan(h, rom, vgm, acc)
        if plan is None:
            continue
        # fold the ROM-window alias so an 0x80xxxxxx hit and its plain address
        # share one body (and one name) instead of duplicating the sample
        plan_mask = _rom_mask(vgm, h.chip_id)
        plan["rom_off"] &= plan_mask
        plan["start"] &= plan_mask
        if plan["link_at"]:
            plan["link_at"] &= plan_mask
        parts: list[bytes] = []
        failed = False
        for off, n in plan["chunks"]:
            chunk = _read(rom, vgm, h.chip_id, off, n)
            if chunk is None:
                failed = True
                break
            parts.append(chunk)
        if failed:
            acc["undumped"] = acc.get("undumped", 0) + 1
            continue
        raw = b"".join(parts)
        if not raw:
            continue
        # A clipped intro still loops at the bytes that were actually read.
        if plan["link_at"] and parts:
            loop_off = len(parts[0])
            if loop_off <= 0 or loop_off >= len(raw):
                loop_off = None
        else:
            loop_off = plan["loop_off"]
            if loop_off is not None and loop_off >= len(raw):
                loop_off = None
        key = (
            h.chip_id, plan["rom_off"], len(raw), loop_off, plan["loop_mode"],
            plan["mulaw"], plan["reversed"], plan["link_at"],
        )
        smp = by_key.get(key)
        if smp is None:
            data, depth = _decode(raw, plan["mulaw"], plan["reversed"])
            width = 2 if depth == 16 else 1
            if plan["loop_mode"] == 2:
                frames = len(data) // width
            else:
                frames = _trim_frames(data, width, loop_off)
            if loop_off is not None and loop_off >= frames:
                loop_off = None
            smp = C352Sample(
                start=plan["start"],
                data=data[:frames * width],
                loop_start=loop_off,
                loop_end=frames if loop_off is not None else None,
                depth=depth,
                loop_mode=plan["loop_mode"] if loop_off is not None else 0,
            )
            by_key[key] = smp
        smp.hits.append(h)

    _flush(acc, warnings)
    samples = sorted(
        by_key.values(),
        key=lambda s: (s.start, -1 if s.loop_start is None else s.loop_start, s.loop_mode),
    )
    return roms.get(0, next(iter(roms.values()), b"")), samples, offs, warnings
