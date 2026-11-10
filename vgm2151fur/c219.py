"""Namco ASIC219 ROM -> signed samples -> Furnace.

The C219 is the 16-voice C140-family part used by Namco NA-1/NA-2.  Its
register file is the C140 layout with 16 voices, but the differences matter:

    0x05 mode: 80 key on, 40 invert, 10 loop, 08 left invert/surround,
                04 noise, 01 C219 mu-law
    start/end/loop are WORD addresses; playback advances one byte per step
    bank 0x1F7/0x1F1/0x1F3/0x1F5 selects a 128 KB bank for voices 0-3/4-7/8-11/12-15

VGMPlay byte-swaps every C219 ROM payload pair before loading it.  The
collector does the same, so the bytes here are the order the C219 core fetches
them.  Linear bodies stay 8-bit and C219 mu-law is expanded to 8-bit linear
(Furnace's depth-12 upload path uses a zero `length8` for C219 samples);
noise notes use a silent body of the source window length so their duration
and loop flag survive.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VgmFile, assemble_rom

C219_VOICES = 16
C219_DEFAULT_CLOCK = 25_056_500
C219_RATE_DIVISOR = 288 * 65536
C219_FURNACE_CLOCK_MAX = 100_000_000
# Furnace's C219 core ticks at chipClock/1136; libvgm's reference core uses
# clock/288.  The .fur clock therefore has to be 1136/288 of the VGM clock.
C219_FURNACE_CLOCK_SCALE = (1136, 288)

C219_MODE_MULAW = 0x01
C219_MODE_NOISE = 0x04
C219_MODE_L_INV = 0x08
C219_MODE_LOOP = 0x10
C219_MODE_INVERT = 0x40
C219_MODE_KEYON = 0x80


def _mulaw_table() -> tuple[int, ...]:
    """C219 mu-law expansion, quantized to signed 8-bit.

    Furnace's C219 sample depth 12 stores the expansion table but its upload
    path uses ``length8``, which is zero for a C219-depth sample, so only four
    bytes reach chip memory.  Decoding to 8-bit linear here is the compatible
    representation and is what an 8-bit C219 body means on the wire.
    """
    table: list[int] = []
    j = 0
    for i in range(128):
        table.append(j << 5)
        if i < 16:
            j += 1
        elif i < 24:
            j += 2
        elif i < 48:
            j += 4
        elif i < 100:
            j += 8
        else:
            j += 16
    for i in range(128):
        table.append((~table[i]) & ~0x1F)
    out: list[int] = []
    for value in table:
        v = value & 0xFFFF
        if v & 0x8000:
            v -= 0x10000
        out.append(max(-128, min(127, int(round(v / 256.0)))) & 0xFF)
    return tuple(out)


C219_MULAW = _mulaw_table()

# A frequency write this soon after key-on is the note's own pitch.
C219_REFIT_SAMPLES = 735
C219_MAX_BODY = 131072
C219_BANK_SIZE = 0x20000
C219_MEMORY = 0x80000
C219_NEVER = 1 << 62


def c219_rate(clock: int, freq: int) -> float:
    """Reference byte rate for a C219 frequency register, in bytes/second."""
    return (clock or 25_056_500) * (freq & 0xFFFF) / C219_RATE_DIVISOR


def c219_furnace_clock(clock: int) -> int:
    """Furnace-notation C219 clock (1136/288 of the VGM clock, clamped)."""
    wanted = (clock or 25_056_500) * C219_FURNACE_CLOCK_SCALE[0] // (
        C219_FURNACE_CLOCK_SCALE[1]
    )
    return max(1_000_000, min(C219_FURNACE_CLOCK_MAX, wanted))


def c219_rate_scale(clock: int) -> float:
    """Sample-rate correction when the custom clock had to be clamped.

    A stored sample C-4 rate and the note chosen from it are a ratio.  Scaling
    the stored rate by the platform/reference rate ratio cancels the clamp.
    """
    wanted = (clock or 25_056_500) * C219_FURNACE_CLOCK_SCALE[0] / (
        C219_FURNACE_CLOCK_SCALE[1]
    )
    if wanted <= C219_FURNACE_CLOCK_MAX:
        return 1.0
    return C219_FURNACE_CLOCK_MAX / wanted


def _pow2_mask(size: int) -> int:
    if size <= 0:
        return 0
    return (1 << (size - 1).bit_length()) - 1


def c219_rom_index(bank: int, start_word: int, byte_offset: int, mask: int) -> int:
    """libvgm find_sample for the 219: 128 KB banks, byte addresses."""
    return ((bank & 0x03) << 17 | ((start_word & 0xFFFF) * 2 + byte_offset)) & mask


def merge_step_writes(pts: list[tuple[int, int]], gap: int = 16) -> list[tuple[int, int]]:
    """Merge MSB/LSB frequency write pairs."""
    merged: list[tuple[int, int]] = []
    for p in pts:
        if merged and p[0] - merged[-1][0] <= gap:
            merged[-1] = p
        else:
            merged.append(p)
    return merged


def hit_played_freq(hit: "C219Hit") -> int:
    """The frequency a hit actually plays at, after the post-key-on refit."""
    steps = merge_step_writes(hit.freq_pts)
    if steps and steps[0][1] and (
        steps[0][0] - hit.sample_time <= C219_REFIT_SAMPLES
        or not (hit.freq & 0xFFFF)
    ):
        return steps[0][1]
    return hit.freq & 0xFFFF


@dataclass
class C219Hit:
    sample_time: int
    chip_id: int
    voice: int
    start: int  # word address
    end: int
    loop: int
    loop_on: bool
    bank: int
    freq: int
    vol_l: int
    vol_r: int
    compressed: bool = False
    noise: bool = False
    invert: bool = False
    left_invert: bool = False
    off_sample: int = 0
    vol_pts: list[tuple[int, int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class C219Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class C219Sample:
    start: int
    data: bytes
    source_bank: int
    loop_start: int | None = None
    loop_end: int | None = None
    depth: int = 8
    noise: bool = False
    # Length before bank-gap trailing silence is appended by the layout pass.
    source_length: int = 0
    hits: list[C219Hit] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.data)

    def mode_freq(self) -> int:
        if not self.hits:
            return 0
        freqs = Counter(f for h in self.hits if (f := hit_played_freq(h)))
        if not freqs:
            return 0
        return freqs.most_common(1)[0][0]

    def c4_rate(self, clock: int) -> int:
        return max(
            1000,
            int(round(c219_rate(clock, self.mode_freq()) * c219_rate_scale(clock))),
        )


def _is_silent_body(data: bytes, depth: int) -> bool:
    """A constant/DC body produces no sound but still eats C219 memory."""
    if len(data) < 2:
        return False
    if depth == 16:
        values = [
            int.from_bytes(data[i:i + 2], "little", signed=True)
            for i in range(0, len(data) - 1, 2)
        ]
    else:
        values = [b - 256 if b >= 128 else b for b in data]
    return bool(values) and max(values) - min(values) <= 1


def _covered(vgm: VgmFile, chip_id: int, addr: int) -> bool:
    for s in vgm.roms:
        if s.chip == chip_id and s.kind == "c219" and s.data:
            if s.start <= addr < s.start + len(s.data):
                return True
    return False


def _decode_body(rom: bytes, mask: int, bank: int, start: int, frames: int) -> bytes:
    out = bytearray(frames)
    for i in range(frames):
        idx = c219_rom_index(bank, start, i, mask)
        if idx < len(rom):
            out[i] = rom[idx]
    return bytes(out)


def c219_furnace_length(smp: C219Sample) -> int:
    """Bytes Furnace's C219 uploader reserves for one sample body.

    `DivPlatformC140::renderSamples` uses ``length8 + 4``, clamps a body to
    one 128 KB bank and rounds an odd length up to an even one.
    """
    length = smp.length + 4
    if length > C219_MAX_BODY:
        length = C219_MAX_BODY
    if length & 1:
        length += 1
    return length


def _c219_furnace_addresses(
    samples: list[C219Sample],
) -> tuple[list[tuple[int, int, int, int]], int]:
    """Mirror Furnace's C219 sample upload.

    Returns one ``(bank, start_word, mem_pos, length)`` per sample and the
    final memory position.  A body whose end crosses a 128 KB boundary is
    moved to the next bank first.
    """
    out: list[tuple[int, int, int, int]] = []
    mem_pos = 0
    for smp in samples:
        length = c219_furnace_length(smp)
        if (mem_pos & 0xFE0000) != ((mem_pos + length) & 0xFE0000):
            mem_pos = (mem_pos + 0x1FFFF) & 0xFE0000
        sample_off = mem_pos >> 1
        out.append((sample_off >> 16 & 3, sample_off & 0xFFFF, mem_pos, length))
        mem_pos += length
    return out, mem_pos


def layout_c219_samples(samples: list[C219Sample]) -> list[C219Sample]:
    """Keep each source sample bank in its own Furnace 128 KB bank.

    Furnace switches a C219 group bank whenever a voice uses a sample from a
    different bank; that switch silences the other voices in the group.  The
    source keeps all voices of a group on one bank, so samples from different
    source banks must not share a Furnace bank.

    A gap between banks is filled by appending trailing silence to a one-shot
    body from the previous bank, not by adding a visible padding sample.
    Looping bodies are never extended, because Furnace derives the chip end
    address from the body length and extending one would move the loop.
    Groups with a one-shot donor are placed first, so the groups that can only
    be last are the ones without one.

    Furnace rounds an odd body up to an even length.  The gap is therefore
    sized against that rounded length and stops one byte pair before the
    boundary; ending exactly on the boundary would make Furnace move the
    donor itself into the next bank and split its source group.
    """
    by_bank: dict[int, list[C219Sample]] = {}
    for smp in samples:
        by_bank.setdefault(smp.source_bank, []).append(smp)
    groups = []
    for bank, group_samples in by_bank.items():
        total = sum(c219_furnace_length(s) for s in group_samples)
        one_shots = [s for s in group_samples if s.loop_start is None
                     and not s.noise]
        groups.append((bank, group_samples, total, bool(one_shots)))
    groups.sort(key=lambda g: (0 if g[3] else 1, -g[2], g[0]))

    out: list[C219Sample] = []
    mem_pos = 0
    for index, (bank, group_samples, _total, _has_one) in enumerate(groups):
        # Play looping bodies first; a one-shot is the donor for the gap to
        # the next source bank and therefore goes last in this group.
        ordered = sorted(
            group_samples,
            key=lambda s: (s.loop_start is None, s.start, s.loop_start or -1),
        )
        target = index * C219_BANK_SIZE
        if mem_pos < target:
            # Leave the two bytes Furnace's even-length rule needs; the next
            # sample's crossing check then aligns it to the bank boundary.
            wanted_end = target - 2
            donor = next(
                (s for s in reversed(out)
                 if s.loop_start is None and not s.noise),
                None,
            )
            if donor is not None:
                old_length = c219_furnace_length(donor)
                new_length = old_length + (wanted_end - mem_pos)
                new_body = max(donor.length, new_length - 4)
                donor.data += bytes(new_body - donor.length)
                mem_pos += c219_furnace_length(donor) - old_length
            else:
                pad_length = wanted_end - mem_pos
                if pad_length >= 4:
                    pad = C219Sample(
                        start=0, data=bytes(pad_length - 4),
                        source_bank=bank, depth=8,
                        source_length=pad_length - 4,
                    )
                    out.append(pad)
                    mem_pos += c219_furnace_length(pad)
        for smp in ordered:
            length = c219_furnace_length(smp)
            if (mem_pos & 0xFE0000) != ((mem_pos + length) & 0xFE0000):
                mem_pos = (mem_pos + 0x1FFFF) & 0xFE0000
            out.append(smp)
            mem_pos += length
    return out


def c219_layout_warnings(samples: list[C219Sample]) -> list[str]:
    """Check the module's sample layout against Furnace's C219 uploader.

    Furnace aligns a body to the next 128 KB bank when its end crosses a bank
    boundary, and truncates a body longer than 128 KB.  A source bank that
    lands in more than one Furnace bank makes a group switch and silence its
    other voices.  Report those cases instead of silently losing samples.
    """
    warnings: list[str] = []
    addresses, mem_end = _c219_furnace_addresses(samples)
    furnace_bank = {i: addr[0] for i, addr in enumerate(addresses)}
    for i, (smp, (_bank, _start, _pos, length)) in enumerate(
        zip(samples, addresses)
    ):
        if smp.length + 4 > C219_MAX_BODY:
            warnings.append(
                f"C219 sample {i} is {smp.length + 4} bytes; Furnace truncates "
                "a body over 128 KB"
            )
        if (
            smp.loop_start is not None
            and smp.source_length
            and smp.length != smp.source_length
        ):
            warnings.append(
                f"C219 looping sample {i} gained "
                f"{smp.length - smp.source_length} bytes; its loop end moved"
            )
    if mem_end > C219_MEMORY:
        warnings.append(
            f"C219 sample bodies need {mem_end} bytes; Furnace's memory is "
            "512 KB and truncates the tail"
        )
    by_source: dict[int, set[int]] = {}
    for i, smp in enumerate(samples):
        if not smp.hits:
            continue
        by_source.setdefault(smp.source_bank, set()).add(furnace_bank[i])
    for bank, furnace_banks in sorted(by_source.items()):
        if len(furnace_banks) > 1:
            warnings.append(
                f"C219 source bank {bank} spans Furnace banks "
                f"{sorted(furnace_banks)}; group-bank switching can cut "
                "voices"
            )

    # Furnace's C219 key-on writes the group's bank and then key-offs every
    # other active voice in that group.  Replay the key-ons in time order to
    # find voices whose sample would be silenced by that switch.
    events: list[tuple[int, int, int, int]] = []
    for i, smp in enumerate(samples):
        for h in smp.hits:
            events.append((
                h.sample_time, h.voice, i,
                h.off_sample if h.off_sample else C219_NEVER,
            ))
    events.sort(key=lambda e: (e[0], e[1]))
    group_bank = [0, 0, 0, 0]
    active_until: dict[int, int] = {}
    cut_counts: dict[tuple[int, int, int], list[int]] = {}
    at_time: dict[int, list[tuple[int, int, int, int]]] = {}
    for event in events:
        at_time.setdefault(event[0], []).append(event)
    for when in sorted(at_time):
        tick = at_time[when]
        keying = {voice for _t, voice, _i, _off in tick}
        for _t, voice, index, off in tick:
            group = voice >> 2
            bank = furnace_bank[index]
            if group_bank[group] != bank:
                for slot in range(4):
                    other = (voice & ~3) | slot
                    if other == voice or other not in active_until:
                        continue
                    if active_until[other] > when and other not in keying:
                        key = (group, group_bank[group], bank)
                        entry = cut_counts.setdefault(
                            key, [0, when, other],
                        )
                        entry[0] += 1
                group_bank[group] = bank
            active_until[voice] = off
    for (group, old_bank, new_bank), (count, when, voice) in sorted(
        cut_counts.items()
    ):
        warnings.append(
            f"C219 voice group {group} switches from Furnace bank "
            f"{old_bank} to {new_bank} at sample {when} and silences other "
            f"active voices (first: voice {voice}, {count} cut"
            f"{'s' if count != 1 else ''})"
        )
    return warnings


def collect_c219(
    vgm: VgmFile,
) -> tuple[list[C219Sample], list[C219Off], list[str]]:
    """Collect C219 hits and sample bodies from a parsed VGM."""
    ids = sorted({w.chip_id for w in vgm.writes if w.chip == "c219"})
    roms = {
        cid: assemble_rom(
            vgm, "c219", cid, fill=0xFF, swap16=True, declared_size=False,
        )
        for cid in ids
    }
    warnings: list[str] = []
    offs: list[C219Off] = []
    if not roms:
        return [], offs, warnings

    reg = [[0] * 16 for _ in range(C219_VOICES)]
    keyed = [False] * C219_VOICES
    hits: list[C219Hit] = []
    active: list[C219Hit | None] = [None] * C219_VOICES
    vol_last = [(0, 0)] * C219_VOICES
    freq_last = [0] * C219_VOICES
    # 0x1F7 -> group 0, 0x1F1 -> group 1, 0x1F3 -> group 2, 0x1F5 -> group 3.
    bank_reg = {0x1F7: 0, 0x1F1: 1, 0x1F3: 2, 0x1F5: 3}
    bank = [0] * 4

    for w in vgm.writes:
        if w.chip != "c219":
            continue
        addr = w.reg & 0x1FF
        # The 0x1F8/1F9/1FB/1FD mirrors alias the bank registers.
        if addr >= 0x1F8 and (addr & 1):
            addr &= ~0x08
        if addr in bank_reg:
            bank[bank_reg[addr]] = w.val & 0x03
            continue
        if addr >= 0x100:
            continue
        voice = addr >> 4
        r = addr & 0x0F
        val = w.val & 0xFF
        if r == 0x05:
            if val & C219_MODE_KEYON:
                if active[voice] is not None:
                    active[voice].off_sample = w.sample
                hit = C219Hit(
                    sample_time=w.sample,
                    chip_id=w.chip_id,
                    voice=voice,
                    start=(reg[voice][0x06] << 8) | reg[voice][0x07],
                    end=(reg[voice][0x08] << 8) | reg[voice][0x09],
                    loop=(reg[voice][0x0A] << 8) | reg[voice][0x0B],
                    loop_on=bool(val & C219_MODE_LOOP),
                    bank=bank[voice // 4],
                    freq=(reg[voice][0x02] << 8) | reg[voice][0x03],
                    vol_l=reg[voice][0x01],
                    vol_r=reg[voice][0x00],
                    compressed=bool(val & C219_MODE_MULAW),
                    noise=bool(val & C219_MODE_NOISE),
                    invert=bool(val & C219_MODE_INVERT),
                    left_invert=bool(val & C219_MODE_L_INV),
                )
                hits.append(hit)
                active[voice] = hit
                vol_last[voice] = (hit.vol_l, hit.vol_r)
                freq_last[voice] = hit.freq
                keyed[voice] = True
            else:
                if keyed[voice]:
                    offs.append(C219Off(w.sample, w.chip_id, voice))
                    if active[voice] is not None:
                        active[voice].off_sample = w.sample
                        active[voice] = None
                keyed[voice] = False
            continue
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
        return [], offs, warnings
    if not any(roms.values()):
        warnings.append(
            "no C219 ROM block (0x8D); PCM notes will be silent until samples are added"
        )
        return [], offs, warnings

    by_key: dict[tuple, C219Sample] = {}
    undumped = 0
    clipped = 0
    for h in hits:
        rom = roms.get(h.chip_id, b"")
        if not rom:
            continue
        mask = _pow2_mask(len(rom))
        base = c219_rom_index(h.bank, h.start, 0, mask)
        if base >= len(rom) or not _covered(vgm, h.chip_id, base):
            undumped += 1
            continue
        frames = (h.end - h.start) * 2
        if frames <= 0:
            frames = 2
        if frames > C219_MAX_BODY:
            frames = C219_MAX_BODY
            clipped += 1
        loop_off = None
        if h.loop_on and h.start <= h.loop < h.end:
            loop_off = (h.loop - h.start) * 2
            if loop_off >= frames:
                loop_off = None
        key = (
            h.chip_id, h.bank, h.start, h.end, loop_off,
            h.compressed, h.noise, h.loop_on,
        )
        smp = by_key.get(key)
        if smp is None:
            if h.noise:
                body = bytes(frames)
                depth = 8
            else:
                body = _decode_body(rom, mask, h.bank, h.start, frames)
                if h.compressed:
                    body = bytes(C219_MULAW[b] for b in body)
                depth = 8
            smp = C219Sample(
                start=h.start,
                data=body,
                source_bank=h.bank,
                loop_start=loop_off,
                loop_end=frames if loop_off is not None else None,
                depth=depth,
                noise=h.noise,
                source_length=len(body),
            )
            by_key[key] = smp
        smp.hits.append(h)
    if undumped:
        warnings.append(
            f"{undumped} C219 triggers point at ROM that the VGM does not dump; "
            "those notes are silent"
        )
    if clipped:
        warnings.append(
            f"{clipped} C219 sample windows exceeded 128 KB and were truncated"
        )
    samples = sorted(
        (
            s for s in by_key.values()
            if s.noise or not _is_silent_body(s.data, s.depth)
        ),
        key=lambda s: (s.source_bank, s.start, s.loop_start or -1),
    )
    silent_count = len(by_key) - len(samples)
    if silent_count:
        warnings.append(
            f"{silent_count} silent C219 sample bodies were dropped"
        )
    return samples, offs, warnings
