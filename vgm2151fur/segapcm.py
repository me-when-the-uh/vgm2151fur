"""SegaPCM ROM → 8-bit signed samples → Furnace.

Register model (libvgm segapcm.c, the core VGM rips are logged against):

    voice = (offset >> 3) & 0xF, reg = (offset & 7) | (offset & 0x80)

The 0x80 half carries the playback address (0x84/0x85) and control (0x86);
the low half carries volume L/R (0x02/0x03), loop address (0x04/0x05), end
page (0x06) and the sample delta / pitch (0x07).

The start/loop registers are a 16-bit byte address inside a 64K window.
Playback stops at end_byte = ((end + 1) & 0xFF) << 8 in that window. The
ROM byte is

    ((ctrl & bankmask) << bankshift) | reg_addr

where bankshift is VGM header 0x3C, bankmask is header 0x3E (0 means 0x70)
masked with (0x1fffff >> shift). The stored body is those unsigned bytes
XOR 0x80 (silence 0x80 becomes 0). The playback rate is

    bytes/second = clock * freq / 32768      (16 voices -> clock / 128 ticks)

Control bits: 0 = disable (active when clear), 1 = loop disable (one-shot),
bits selected by bankmask = ROM bank.

0x84/0x85 are the live cursor, not a latched start address. The chip adds
the delta every tick (MAME sega_315_5218 voice_t::tick, libvgm SEGAPCM_update)
and a CPU write replaces those bits of the cursor (voice_addr_w). Rewriting
the original start after the cursor has moved seeks back to it, which is a
new note. A volume or delta rewrite that leaves the cursor on the same byte
is the same note.

One Z80 dump writes the address, then the end page and delta, then control.
Across the logged sets that dump spans at most 3 VGM samples. Writes
within 4 samples are one note, snapshotted after the dump.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, VgmFile, assemble_rom

SEGAPCM_VOICES = 16
SEGAPCM_SILENCE = 0x80  # unsigned centre of an idle SegaPCM body
SEGAPCM_RATE_DIV = 32768.0
SEGAPCM_BANK_MASK7 = 0x70  # libvgm default when header 0x3E is 0
# 16 voices, each updated at clock/128 (MAME CLOCK_DIVIDER = voices*8).
_TICK_DENOM = 128 * VGM_RATE
_ADDR_MASK = 0xFFFFFF
# Width of one register dump. Drivers write a dump within ~3 samples;
# restarts 13 samples apart stay apart.
_REGISTER_BURST = 4


def segapcm_bankmask(shift: int, intf_mask: int) -> int:
    """libvgm device_start_segapcm: mask & (0x1fffff >> shift), default 0x70."""
    mask = intf_mask & 0xFF
    if not mask:
        mask = SEGAPCM_BANK_MASK7
    shift &= 0xFF
    window = (0x1FFFFF >> shift) if shift < 21 else 0
    return mask & window


def segapcm_rate(clock: int, freq: int) -> float:
    """Hardware playback rate in bytes/second for an 8-bit delta register."""
    return (clock or 4000000) * (freq & 0xFF) / SEGAPCM_RATE_DIV


@dataclass
class _Voice:
    """MAME voice_t power-on values (segapcm.h). Control bit 0 set = stopped."""

    addr: int = 0xFFFF00
    loop: int = 0xFFFF
    end: int = 0xFF
    freq: int = 0xFF
    lvol: int = 0xFF
    rvol: int = 0xFF
    ctrl: int = 0xFF


@dataclass
class _Burst:
    start: int
    retrigger: bool = False
    when: int | None = None
    window: int | None = None
    # Mid-note volume (L, R) and frequency writes seen before this burst
    # commits.  A retrigger dump discards them: its values are the new note's
    # snapshot, not a step away from the previous note.
    pending_vol: list[tuple[int, int, int]] = field(default_factory=list)
    pending_freq: list[tuple[int, int]] = field(default_factory=list)


def _ticks_until_stop(addr: int, freq: int, stop_page: int) -> int:
    """Playback ticks until addr lands in stop_page. 0 if it is already there.

    freq is 1..255: one tick cannot jump over a 64K page.
    """
    if ((addr >> 16) & 0xFF) == stop_page:
        return 0
    target = stop_page << 16
    cur = addr & _ADDR_MASK
    if cur < target:
        gap = target - cur
    else:
        gap = (_ADDR_MASK + 1 - cur) + target
    return (gap + freq - 1) // freq


def _loop_cycle(loop: int, freq: int, stop_page: int) -> int:
    """Ticks from the address just after a loop relocation back to itself."""
    addr = (((loop & 0xFFFF) << 8) + freq) & _ADDR_MASK
    if ((addr >> 16) & 0xFF) == stop_page:
        return 1
    return _ticks_until_stop(addr, freq, stop_page) + 1


def _advance_voice(voice: _Voice, n_ticks: int) -> None:
    """Run MAME voice_t::tick n_ticks times.

    A disabled voice clears the fractional byte and holds. An enabled voice
    plays until page (end+1), then either sets control bit 0 (one-shot) or
    reloads the loop address and keeps going. The stop is tested before the
    delta add, so the end page itself is not played.
    """
    if n_ticks <= 0:
        return
    if voice.ctrl & 1:
        voice.addr &= 0xFFFF00
        return
    freq = voice.freq & 0xFF
    stop = (voice.end + 1) & 0xFF
    if freq == 0:
        if n_ticks and ((voice.addr >> 16) & 0xFF) == stop:
            if voice.ctrl & 2:
                voice.ctrl |= 1
                if n_ticks > 1:
                    voice.addr &= 0xFFFF00
            else:
                voice.addr = (voice.loop & 0xFFFF) << 8
        return
    while n_ticks > 0:
        if ((voice.addr >> 16) & 0xFF) == stop:
            if voice.ctrl & 2:
                voice.ctrl |= 1
                n_ticks -= 1
                if n_ticks > 0:
                    voice.addr &= 0xFFFF00
                return
            voice.addr = (((voice.loop & 0xFFFF) << 8) + freq) & _ADDR_MASK
            n_ticks -= 1
            if n_ticks == 0:
                return
            cycle = _loop_cycle(voice.loop, freq, stop)
            if cycle <= 1:
                return
            n_ticks %= cycle
            continue
        n = _ticks_until_stop(voice.addr, freq, stop)
        if n <= 0:
            return
        step = n if n < n_ticks else n_ticks
        voice.addr = (voice.addr + step * freq) & _ADDR_MASK
        n_ticks -= step


def _apply_write(voice: _Voice, off: int, val: int, bankmask: int) -> tuple[bool, bool, bool, int]:
    """Apply one CPU write. Return (seek, off_to_on, bank_changed, window)."""
    r = off & 7
    val &= 0xFF
    window = (voice.addr >> 8) & 0xFFFF
    if off & 0x80:
        if r == 4 or r == 5:
            shift = 8 if r == 4 else 16
            old = window
            voice.addr = (voice.addr & ~(0xFF << shift)) | (val << shift)
            voice.addr &= _ADDR_MASK
            window = (voice.addr >> 8) & 0xFFFF
            return window != old, False, False, window
        if r == 6:
            old = voice.ctrl
            voice.ctrl = val
            off_to_on = bool(old & 1) and not bool(val & 1)
            bank_changed = bool((old ^ val) & bankmask) and not bool(val & 1)
            return False, off_to_on, bank_changed, (voice.addr >> 8) & 0xFFFF
        return False, False, False, window
    if r == 2:
        voice.lvol = val & 0x7F
    elif r == 3:
        voice.rvol = val & 0x7F
    elif r == 4:
        voice.loop = (voice.loop & 0xFF00) | val
    elif r == 5:
        voice.loop = (voice.loop & 0x00FF) | (val << 8)
    elif r == 6:
        voice.end = val
    elif r == 7:
        voice.freq = val
    return False, False, False, window


@dataclass
class SegapcmHit:
    sample_time: int
    voice: int
    start: int
    end: int
    loop: int
    loop_on: bool
    freq: int
    vol_l: int
    vol_r: int
    ctrl: int = 0
    chip_id: int = 0
    # Mid-note register writes while the voice stays keyed: the (L, R) volume
    # pair (the balance is part of the write) and the 8-bit frequency.
    vol_pts: list[tuple[int, int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)


def hit_played_freq(hit: SegapcmHit) -> int:
    """The frequency a hit actually plays at (0 when the driver never set one).

    The register dump carries the pitch, so the key-on snapshot is usually
    right.  A voice keyed with a zero frequency takes the first later write,
    which is the note's own pitch rather than a bend.
    """
    if hit.freq & 0xFF:
        return hit.freq & 0xFF
    for _t, f in hit.freq_pts:
        if f & 0xFF:
            return f & 0xFF
    return 0


@dataclass
class SegapcmSample:
    start: int
    data: bytes  # signed 8-bit
    loop_start: int | None = None
    loop_end: int | None = None
    hits: list[SegapcmHit] = field(default_factory=list)
    chip_id: int = 0

    @property
    def length(self) -> int:
        return len(self.data)

    def mode_freq(self) -> int:
        """Most common nonzero key-on frequency (0 when unknown).

        A zero register means the driver had not set the pitch yet (or muted
        the voice), which must not pin the sample's reference rate to 0.
        """
        if not self.hits:
            return 0
        freqs = Counter(f for h in self.hits if (f := hit_played_freq(h)))
        if not freqs:
            return 0
        return freqs.most_common(1)[0][0]

    def c4_rate(self, clock: int) -> int:
        return max(1000, int(round(segapcm_rate(clock, self.mode_freq()))))


def _trim_end(rom: bytes, start: int, end: int, loop_off: int | None) -> int:
    """Drop trailing idle 0x80 bytes; never cut into the loop body."""
    floor = 2
    if loop_off is not None:
        floor = max(floor, loop_off + 64)
    while end > start + floor and rom[end - 1] == SEGAPCM_SILENCE:
        end -= 1
    return end


def collect_segapcm(vgm: VgmFile) -> tuple[bytes, list[SegapcmSample], list[str]]:
    ids = sorted({w.chip_id for w in vgm.writes if w.chip == "segapcm"})
    roms = {
        cid: assemble_rom(vgm, "segapcm", cid)
        for cid in (ids or [0])
    }
    rom = roms.get(0, b"")
    warnings: list[str] = []
    if not ids:
        return rom, [], warnings

    voices = {
        cid: [_Voice() for _ in range(SEGAPCM_VOICES)] for cid in ids
    }
    open_burst = {
        cid: [None] * SEGAPCM_VOICES for cid in ids
    }
    hits: list[SegapcmHit] = []
    active = {
        cid: [None] * SEGAPCM_VOICES for cid in ids
    }
    vol_last = {
        cid: [(0, 0)] * SEGAPCM_VOICES for cid in ids
    }
    freq_last = {cid: [0] * SEGAPCM_VOICES for cid in ids}
    bankmask = segapcm_bankmask(
        getattr(vgm, "segapcm_bankshift", 0),
        getattr(vgm, "segapcm_bankmask", 0),
    )
    clock = getattr(vgm, "segapcm_clock", 0) or 4000000
    tick_acc = 0
    last_t = 0

    def advance_to(t: int) -> None:
        nonlocal tick_acc, last_t
        dt = t - last_t
        if dt <= 0:
            return
        tick_acc += dt * clock
        n = tick_acc // _TICK_DENOM
        tick_acc -= n * _TICK_DENOM
        last_t = t
        if n:
            for chip_voices in voices.values():
                for voice in chip_voices:
                    _advance_voice(voice, n)

    def commit(cid: int, i: int) -> None:
        burst = open_burst[cid][i]
        open_burst[cid][i] = None
        if burst is None:
            return
        voice = voices[cid][i]
        if burst.retrigger and burst.window is not None:
            if voice.ctrl & 1:
                active[cid][i] = None
                return
            hit = SegapcmHit(
                sample_time=burst.when if burst.when is not None else burst.start,
                voice=i,
                start=burst.window & 0xFFFF,
                end=voice.end & 0xFF,
                loop=voice.loop & 0xFFFF,
                loop_on=not bool(voice.ctrl & 2),
                freq=voice.freq & 0xFF,
                vol_l=voice.lvol & 0x7F,
                vol_r=voice.rvol & 0x7F,
                ctrl=voice.ctrl & 0xFF,
                chip_id=cid,
            )
            hits.append(hit)
            active[cid][i] = hit
            vol_last[cid][i] = (hit.vol_l, hit.vol_r)
            freq_last[cid][i] = hit.freq
            return
        # A burst without a retrigger is a mid-note register update.  Flush
        # the volume and frequency steps it carried to the sounding note.
        # The steps were captured while it was still playing, so the last
        # one before a disable (usually a fade to zero) still counts.
        hit = active[cid][i]
        if hit is not None:
            for t, left, right in burst.pending_vol:
                if (left, right) != vol_last[cid][i]:
                    hit.vol_pts.append((t, left, right))
                    vol_last[cid][i] = (left, right)
            for t, freq in burst.pending_freq:
                if freq != freq_last[cid][i]:
                    hit.freq_pts.append((t, freq))
                    freq_last[cid][i] = freq
        if voice.ctrl & 1:
            active[cid][i] = None

    def close_expired(t: int) -> None:
        for cid, bursts in open_burst.items():
            for i, burst in enumerate(bursts):
                if burst is not None and t - burst.start > _REGISTER_BURST:
                    commit(cid, i)

    for w in vgm.writes:
        if w.chip != "segapcm":
            continue
        cid = w.chip_id if w.chip_id in voices else 0
        off = w.reg & 0x7FF
        vi = (off >> 3) & 0xF
        close_expired(w.sample)
        advance_to(w.sample)
        seek, off_to_on, bank_changed, window = _apply_write(
            voices[cid][vi], off, w.val, bankmask,
        )
        burst = open_burst[cid][vi]
        if burst is None:
            burst = _Burst(start=w.sample)
            open_burst[cid][vi] = burst
        if seek or off_to_on or bank_changed:
            burst.retrigger = True
            if burst.when is None:
                burst.when = w.sample
            if seek or burst.window is None:
                burst.window = window
        # Collect volume/pitch changes made while the voice keeps playing.
        # The burst decides later whether they are steps or the next note's
        # key-on values.
        if not (voices[cid][vi].ctrl & 1) and not (off & 0x80):
            r = off & 7
            if r in (2, 3):
                pair = (
                    voices[cid][vi].lvol & 0x7F,
                    voices[cid][vi].rvol & 0x7F,
                )
                if pair != vol_last[cid][vi]:
                    if burst.pending_vol and burst.pending_vol[-1][0] == w.sample:
                        burst.pending_vol[-1] = (w.sample, pair[0], pair[1])
                    else:
                        burst.pending_vol.append((w.sample, pair[0], pair[1]))
            elif r == 7:
                freq = voices[cid][vi].freq & 0xFF
                if freq != freq_last[cid][vi]:
                    if burst.pending_freq and burst.pending_freq[-1][0] == w.sample:
                        burst.pending_freq[-1] = (w.sample, freq)
                    else:
                        burst.pending_freq.append((w.sample, freq))
    for cid in voices:
        for i in range(SEGAPCM_VOICES):
            commit(cid, i)

    if not any(roms.values()):
        if hits:
            warnings.append(
                "no SegaPCM ROM block (0x80); PCM notes will be silent until samples are added"
            )
        return rom, [], warnings

    by_key: dict[tuple, SegapcmSample] = {}
    shift = getattr(vgm, "segapcm_bankshift", 0) & 0xFF
    for h in hits:
        # End page is inside the 64K window; the bank is only the ROM offset.
        win_end = ((h.end + 1) & 0xFF) << 8
        if win_end <= h.start:
            win_end = h.start + 2
        bank_base = (h.ctrl & bankmask) << shift
        abs_start = bank_base | h.start
        abs_end = bank_base + win_end if win_end > 0xFFFF else bank_base | win_end
        chip_rom = roms.get(h.chip_id, b"")
        if abs_start >= len(chip_rom) or abs_end <= abs_start:
            continue
        abs_end = min(abs_end, len(chip_rom))
        loop_off = None
        if h.loop_on and h.start <= h.loop < min(win_end, 0x10000):
            loop_off = h.loop - h.start
        key = (h.chip_id, abs_start, abs_end, loop_off)
        smp = by_key.get(key)
        if smp is None:
            end_trim = _trim_end(chip_rom, abs_start, abs_end, loop_off)
            if loop_off is not None and loop_off >= end_trim - abs_start:
                loop_off = None
            raw = chip_rom[abs_start:end_trim]
            body = bytes((b ^ 0x80) & 0xFF for b in raw)
            loop_end = end_trim - abs_start if loop_off is not None else None
            smp = SegapcmSample(
                start=abs_start,
                data=body,
                loop_start=loop_off,
                loop_end=loop_end,
                chip_id=h.chip_id,
            )
            by_key[key] = smp
        smp.hits.append(h)
    samples = sorted(
        by_key.values(),
        key=lambda s: (s.chip_id, s.start, s.loop_start or -1),
    )
    return rom, samples, warnings
