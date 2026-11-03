"""Ricoh RF5C68 / RF5C164 sample RAM → 8-bit signed samples → Furnace.

Both chips share one register file (`$B0`/`$B1` register writes, `$C1`/`$C2`
memory writes, 16-bit LE RAM offsets).  Furnace hosts them as a single system
(`0x95`) with a `chipType` flag: 0 is the RF5C68 (FM Towns, 8 MHz) and 1 the
RF5C164 (Sega CD, 12.5 MHz).

Register model (MAME rf5c68.cpp, which Furnace's core copies):

    0x00 envelope     0x01 pan (low nibble left, high nibble right)
    0x02/0x03 step    0x04/0x05 loop address
    0x06 start (8 bits, units of 256 bytes)
    0x07 control: bit 7 chip enable, bit 6 select channel (bits 2-0) else
         select the 4 KB write bank (bits 3-0 → address bits 12-15)
    0x08 channel enable bitmap: a clear bit keys the voice, a set bit disables
         it and rewinds it to the start address

A voice addresses sample RAM as `start << 8`; it advances `step` per output
sample and reads `(addr >> 11) & 0xFFFF`.  A read of 0xFF is the end marker:
the voice jumps to `loop` and keeps playing if that byte is real audio, or
stops when the loop byte is also 0xFF.  The stored byte is sign-magnitude
(bit 7 = sign, bits 0-6 = magnitude), silence is 0x80.

The playback rate is `clock * step / 786432` bytes/second.  Furnace's
`CHIP_FREQBASE` for the core is the same 786432, so a note's C-4 rate is the
rate of the sample's usual step and the note/E5 pair is the ratio to it.

The `$C1`/`$C2` memory writes and the `0xC0`/`0xC1` RAM blocks are folded into
one flat 64 KB buffer here; playback only addresses the low 64 KB window.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, VgmFile

RF5C_VOICES = 8
RF5C_RAM_SIZE = 0x10000  # one 64 KB window, the width the core reads
RF5C_END_MARK = 0xFF  # end-of-sample byte
RF5C_RATE_DIV = 786432.0  # Furnace CHIP_FREQBASE; 384 * 2048
RF5C68_DEFAULT_CLOCK = 8000000
RF5C164_DEFAULT_CLOCK = 12500000
# A driver retriggers by disabling the voice, writing start/loop, then
# enabling it in the same burst. A disable that is followed by an enable this
# soon is that burst, not a real note-off.
RF5C_RETRIGGER = 64


def rf5c_rate(clock: int, step: int) -> float:
    """Bytes per second for a 16-bit frequency step."""
    default = RF5C68_DEFAULT_CLOCK
    return (clock or default) * (step & 0xFFFF) / RF5C_RATE_DIV


@dataclass
class Rf5cHit:
    sample_time: int
    voice: int
    start: int  # byte offset in sample RAM
    loop: int  # byte offset in sample RAM
    step: int  # 16-bit frequency step
    env: int  # 8-bit envelope
    pan: int  # low nibble left, high nibble right


@dataclass
class Rf5cOff:
    sample_time: int
    voice: int


@dataclass
class Rf5cSample:
    start: int
    data: bytes  # signed 8-bit
    loop_start: int | None = None
    loop_end: int | None = None
    hits: list[Rf5cHit] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.data)

    def mode_step(self) -> int:
        """Most common nonzero key-on step (0 when unknown)."""
        steps = Counter(h.step for h in self.hits if h.step)
        if not steps:
            return 0
        return steps.most_common(1)[0][0]

    def c4_rate(self, clock: int) -> int:
        return max(1000, int(round(rf5c_rate(clock, self.mode_step()))))


@dataclass
class _Voice:
    env: int = 0
    pan: int = 0
    step: int = 0
    loop: int = 0
    start: int = 0
    enabled: bool = False


def _sign_magnitude(val: int) -> int:
    """Core byte → signed 8-bit. Bit 7 is the sign, bits 0-6 the magnitude."""
    if val & 0x80:
        return val & 0x7F
    return -(val & 0x7F)


def _apply_reg(voice: _Voice, reg: int, val: int) -> None:
    if reg == 0x00:
        voice.env = val & 0xFF
    elif reg == 0x01:
        voice.pan = val & 0xFF
    elif reg == 0x02:
        voice.step = (voice.step & 0xFF00) | (val & 0xFF)
    elif reg == 0x03:
        voice.step = (voice.step & 0x00FF) | ((val & 0xFF) << 8)
    elif reg == 0x04:
        voice.loop = (voice.loop & 0xFF00) | (val & 0xFF)
    elif reg == 0x05:
        voice.loop = (voice.loop & 0x00FF) | ((val & 0xFF) << 8)
    elif reg == 0x06:
        voice.start = val & 0xFF


def build_rf5c_ram(vgm: VgmFile, chip: str) -> bytes:
    """Reconstruct a chip's sample RAM from its blocks and memory writes."""
    ram = bytearray(RF5C_RAM_SIZE)
    for slice_ in vgm.roms:
        if slice_.kind != chip:
            continue
        start = slice_.start & 0xFFFF
        end = min(start + len(slice_.data), RF5C_RAM_SIZE)
        if end > start:
            ram[start:end] = slice_.data[: end - start]
    for write in vgm.writes:
        if write.chip != f"{chip}_mem":
            continue
        addr = write.reg & 0xFFFF
        ram[addr] = write.val & 0xFF
    return bytes(ram)


def _scan_end(ram: bytes, start: int) -> int:
    """First end-marker byte at or after start (or the buffer end)."""
    end = start
    n = len(ram)
    while end < n and ram[end] != RF5C_END_MARK:
        end += 1
    return end


def collect_rf5c(
    vgm: VgmFile, chip: str = "rf5c68", clock: int | None = None,
) -> tuple[bytes, list[Rf5cSample], list[Rf5cOff], list[str]]:
    """Read one RF5C chip's notes and the samples they play.

    `chip` is "rf5c68" or "rf5c164"; they share the register model and differ
    only in clock, RAM contents and which write commands feed them.
    """
    warnings: list[str] = []
    ram = build_rf5c_ram(vgm, chip)
    writes = [w for w in vgm.writes if w.chip == chip]
    if not writes:
        return ram, [], [], warnings
    if clock is None:
        clock = (
            vgm.rf5c68_clock or RF5C68_DEFAULT_CLOCK
            if chip == "rf5c68"
            else vgm.rf5c164_clock or RF5C164_DEFAULT_CLOCK
        )

    voices = [_Voice() for _ in range(RF5C_VOICES)]
    current = 0
    hits: list[Rf5cHit] = []
    offs: list[Rf5cOff] = []
    # A disable is held here until the next event on the voice proves whether
    # it was a real note-off or the first half of a retrigger burst.
    pending_off: list[int | None] = [None] * RF5C_VOICES

    def flush_off(i: int) -> None:
        if pending_off[i] is not None:
            offs.append(Rf5cOff(pending_off[i], i))
            pending_off[i] = None

    for write in writes:
        reg = write.reg & 0xFF
        if reg == 0x07:
            if write.val & 0x40:
                current = write.val & 0x07
            # A clear bit 6 selects the RAM write bank, already folded into
            # the memory-write addresses by vgm.py.
            continue
        if reg == 0x08:
            # The enable register is a whole-chip bitmap. A clear bit keys a
            # voice; a set bit disables and rewinds it.
            was = [v.enabled for v in voices]
            for i in range(RF5C_VOICES):
                voices[i].enabled = not bool((write.val >> i) & 1)
            for i in range(RF5C_VOICES):
                if voices[i].enabled and not was[i]:
                    if (
                        pending_off[i] is not None
                        and write.sample - pending_off[i] <= RF5C_RETRIGGER
                    ):
                        # Retrigger: the disable belonged to this same note.
                        pending_off[i] = None
                    else:
                        flush_off(i)
                    voice = voices[i]
                    hits.append(Rf5cHit(
                        sample_time=write.sample,
                        voice=i,
                        start=(voice.start << 8) & 0xFFFF,
                        loop=voice.loop & 0xFFFF,
                        step=voice.step & 0xFFFF,
                        env=voice.env & 0xFF,
                        pan=voice.pan & 0xFF,
                    ))
                elif was[i] and not voices[i].enabled:
                    flush_off(i)
                    pending_off[i] = write.sample
            continue
        if reg <= 0x06:
            _apply_reg(voices[current], reg, write.val)

    for i in range(RF5C_VOICES):
        flush_off(i)

    if not hits:
        return ram, [], offs, warnings

    by_key: dict[tuple[int, int | None], Rf5cSample] = {}
    for hit in hits:
        start = hit.start
        if start >= RF5C_RAM_SIZE:
            continue
        end = _scan_end(ram, start)
        if end <= start + 2:
            continue
        loop_off: int | None = None
        if start < hit.loop < end:
            # The chip has no loop-disable bit. Drivers that want a one-shot
            # point the loop at the trailing silence just before the end
            # marker; that is a hold, not a musical loop.
            candidate = hit.loop - start
            if candidate < (end - start) - 2:
                loop_off = candidate
        key = (start, loop_off)
        sample = by_key.get(key)
        if sample is None:
            body = bytes(_sign_magnitude(b) & 0xFF for b in ram[start:end])
            loop_end = (end - start) if loop_off is not None else None
            sample = Rf5cSample(
                start=start, data=body, loop_start=loop_off, loop_end=loop_end,
            )
            by_key[key] = sample
        sample.hits.append(hit)

    if not any(s.length >= 8 for s in by_key.values()):
        # Notes exist but the RAM behind them is empty: a rip that lost its
        # sample block. Say so instead of writing silent channels.
        warnings.append(
            f"{chip.upper()} notes but no sample RAM data; the PCM channels "
            "will be silent until samples are added"
        )
        return ram, [], offs, warnings

    samples = sorted(by_key.values(), key=lambda s: (s.start, s.loop_start or -1))
    return ram, samples, offs, warnings
