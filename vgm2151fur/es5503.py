"""Ensoniq ES5503 (Apple IIgs) -> Generic PCM DAC sample bodies.

The ES5503 has 32 oscillators playing 8-bit unsigned wave data out of a
128 KB RAM.  Each oscillator has a 16-bit frequency register, an 8-bit
volume, a wave-table pointer (256-byte aligned), a size class (256 to 32768
bytes) and a resolution class.  The chip reads one byte per output sample
step, so the source rate of a wave body is

    output_rate * freq / 2 ** (9 + resolution - size_class)

where ``output_rate = (clock / 8) / (enabled_oscillators + 2)``.  Wave data
is terminated by a 0x00 byte: the chip halts an oscillator when it reads one,
which makes every body one-shot unless a driver avoids zero bytes and leaves
the oscillator in free-run mode.

Furnace has no ES5503 platform, so each used oscillator becomes one
1-channel Generic PCM DAC system.  The wave byte is stored as a signed
8-bit sample (the chip's 0x80 centre line), and the oscillator rate becomes
the note/E5 pair relative to the body's most common rate.  Volume stays the
source's linear 0-255 value; no extra gain is applied.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VgmFile, assemble_rom

ES5503_OSCILLATORS = 32
ES5503_DEFAULT_CLOCK = 7_159_090
ES5503_RAM_SIZE = 0x20000
ES5503_MAX_BODY = 32768

MODE_FREE = 0
MODE_ONESHOT = 1
MODE_SYNCAM = 2
MODE_SWAP = 3


def es5503_out_rate(clock: int, oscillators_enabled: int) -> int:
    """Chip output (accumulator) rate for the enabled oscillator count."""
    clock = clock or ES5503_DEFAULT_CLOCK
    count = max(1, min(ES5503_OSCILLATORS, oscillators_enabled))
    return max(1000, (clock // 8) // (count + 2))


def es5503_play_rate(
    out_rate: int, freq: int, resolution: int, size_class: int,
) -> float:
    """Wave frames per second for one oscillator setting."""
    if freq <= 0:
        return 0.0
    step = float(1 << (9 + (resolution & 7) - (size_class & 7)))
    return out_rate * freq / step


def es5503_volume_fx(value: int) -> int:
    """The chip's linear volume register as a 0-255 Furnace level."""
    return max(0, min(255, value))


def es5503_pan_fx(mask: int, output_channels: int) -> int:
    """Furnace 08xy for an oscillator's output-channel mask.

    libvgm alternates the stereo channels L/R and sends a trailing odd output
    channel to both.  With the usual two-channel header, mask 0 is left and
    mask 1 right; masks past the outputs are silent.
    """
    channels = output_channels or 2
    mask &= 0x0F
    if channels <= 1:
        return 0xFF
    stereo = channels & ~1
    if mask < stereo:
        return 0xF0 if (mask & 1) == 0 else 0x0F
    if mask == stereo:
        return 0xFF
    return 0x00


@dataclass
class Es5503Hit:
    sample_time: int
    chip_id: int
    osc: int
    freq: int
    vol: int
    ptr: int
    size_class: int
    resolution: int
    mode: int
    chan_mask: int
    out_rate: int
    control: int
    off_sample: int = 0
    sample_index: int = -1
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Es5503Off:
    sample_time: int
    chip_id: int
    osc: int


@dataclass
class Es5503Sample:
    chip_id: int
    ptr: int
    size_class: int
    data: bytes
    c4_rate: int
    loop: bool = False
    hits: list[Es5503Hit] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.data)


def _signed_body(ram: bytes, ptr: int, size: int) -> bytes:
    """Wave bytes from ``ptr`` up to the 0x00 terminator or the size limit."""
    if ptr < 0 or ptr >= len(ram):
        return b""
    end = min(len(ram), ptr + size)
    stop = ram.find(b"\x00", ptr, end)
    if stop >= 0:
        end = stop
    body = ram[ptr:end]
    return bytes((value - 0x80) & 0xFF for value in body)


def collect_es5503(
    vgm: VgmFile,
) -> tuple[list[Es5503Sample], list[Es5503Off], list[str]]:
    """Collect ES5503 oscillator hits and their 8-bit wave bodies."""
    warnings: list[str] = []
    hits: list[Es5503Hit] = []
    offs: list[Es5503Off] = []

    freq = [0] * ES5503_OSCILLATORS
    vol = [0] * ES5503_OSCILLATORS
    ptr = [0] * ES5503_OSCILLATORS
    size_class = [0] * ES5503_OSCILLATORS
    resolution = [0] * ES5503_OSCILLATORS
    control: list[int | None] = [None] * ES5503_OSCILLATORS
    active: list[Es5503Hit | None] = [None] * ES5503_OSCILLATORS
    changed_wave = [False] * ES5503_OSCILLATORS
    oscillators_enabled = 1

    clock = vgm.es5503_clock or ES5503_DEFAULT_CLOCK

    for write in vgm.writes:
        if write.chip != "es5503":
            continue
        reg = write.reg & 0xFF
        value = write.val & 0xFF
        if reg == 0xE1:
            # Global oscillator-enable count, (data >> 1) + 1.
            oscillators_enabled = ((value >> 1) & 0x1F) + 1
            continue
        if reg >= 0xE0:
            continue
        osc = reg & 0x1F
        cls = reg & 0xE0
        current = active[osc]
        if cls == 0x00:
            freq[osc] = (freq[osc] & 0xFF00) | value
            if current is not None:
                current.freq_pts.append((write.sample, freq[osc]))
        elif cls == 0x20:
            freq[osc] = (freq[osc] & 0x00FF) | (value << 8)
            if current is not None:
                current.freq_pts.append((write.sample, freq[osc]))
        elif cls == 0x40:
            vol[osc] = value
            if current is not None:
                current.vol_pts.append((write.sample, value))
        elif cls == 0x80:
            ptr[osc] = (ptr[osc] & 0x10000) | (value << 8)
            if current is not None:
                changed_wave[osc] = True
        elif cls == 0xA0:
            old = control[osc]
            control[osc] = value
            running = not (value & 1)
            was_running = old is not None and not (old & 1)
            if running and not was_running:
                if current is not None:
                    current.off_sample = write.sample
                hit = Es5503Hit(
                    sample_time=write.sample,
                    chip_id=write.chip_id,
                    osc=osc,
                    freq=freq[osc],
                    vol=vol[osc],
                    ptr=ptr[osc],
                    size_class=size_class[osc],
                    resolution=resolution[osc],
                    mode=(value >> 1) & 3,
                    chan_mask=(value >> 4) & 0x0F,
                    out_rate=es5503_out_rate(clock, oscillators_enabled),
                    control=value,
                )
                hits.append(hit)
                active[osc] = hit
            elif not running and was_running:
                if current is not None:
                    current.off_sample = write.sample
                    offs.append(Es5503Off(
                        write.sample, write.chip_id, osc,
                    ))
                    active[osc] = None
        elif cls == 0xC0:
            ptr[osc] = (ptr[osc] & 0xFFFF) | (
                0x10000 if value & 0x40 else 0
            )
            size_class[osc] = (value >> 3) & 7
            resolution[osc] = value & 7
            if current is not None:
                changed_wave[osc] = True

    for osc, hit in enumerate(active):
        if hit is not None:
            offs.append(Es5503Off(vgm.total_samples or 0, hit.chip_id, osc))

    for osc, changed in enumerate(changed_wave):
        if changed:
            warnings.append(
                f"ES5503 oscillator {osc + 1}: wave table changed mid-note; "
                "only the key-on table is used"
            )

    if hits and not vgm.roms:
        warnings.append(
            "ES5503 writes present but no ES5503 RAM data block; "
            "the wave bodies are empty"
        )

    ram_by_chip = {
        chip_id: assemble_rom(vgm, "es5503", chip_id)
        for chip_id in sorted({hit.chip_id for hit in hits})
    }

    by_body: dict[tuple[int, int, int], list[Es5503Hit]] = {}
    for hit in hits:
        by_body.setdefault(
            (hit.chip_id, hit.ptr, hit.size_class), []
        ).append(hit)

    samples: list[Es5503Sample] = []
    for (chip_id, body_ptr, body_size_class), body_hits in by_body.items():
        ram = ram_by_chip.get(chip_id, b"")
        size = 256 << body_size_class
        data = _signed_body(ram, body_ptr, size)
        if len(data) < 2:
            warnings.append(
                f"ES5503 {chip_id}: wave at {body_ptr:05x} is empty or "
                "one byte; its notes are silent"
            )
            continue
        rates = Counter(
            int(round(es5503_play_rate(
                hit.out_rate, hit.freq, hit.resolution, hit.size_class,
            )))
            for hit in body_hits
            if hit.freq > 0
        )
        if not rates:
            warnings.append(
                f"ES5503 {chip_id}: wave at {body_ptr:05x} has no pitched "
                "hits; its notes are silent"
            )
            continue
        c4_rate = rates.most_common(1)[0][0]
        modes = Counter(hit.mode for hit in body_hits)
        loop = (
            modes.most_common(1)[0][0] == MODE_FREE
            and len(data) >= size
        )
        smp = Es5503Sample(
            chip_id=chip_id,
            ptr=body_ptr,
            size_class=body_size_class,
            data=data,
            c4_rate=max(1, c4_rate),
            loop=loop,
        )
        smp.hits.extend(body_hits)
        for hit in body_hits:
            hit.sample_index = len(samples)
        samples.append(smp)

    # Drop hits whose body was silent; their notes must not reach the module.
    kept = {id(hit) for smp in samples for hit in smp.hits}
    hits = [hit for hit in hits if id(hit) in kept]

    if any(hit.mode in (MODE_SYNCAM, MODE_SWAP) for hit in hits):
        warnings.append(
            "ES5503 sync/AM and swap oscillator modes are approximated "
            "as one-shot"
        )

    return samples, offs, warnings
