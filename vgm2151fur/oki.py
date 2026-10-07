"""MSM6295 phrases and MSM6258 bursts for the Furnace writer.

MSM6295 plays a phrase at one chip-wide rate. VGM command 0xB8 is the
command port plus the bank registers VGMPlay uses for an NMK112-style
mapper (regs 0x0E and 0x10-0x13). The phrase ADPCM is stored as Furnace
VOX and triggered on a fixed note; pitch comes from the chip clock and
pin 7, volume from the OKI nibble.

MSM6258 is a single CPU-fed ADPCM stream (command 0xB7). Each play burst
becomes one VOX sample. DAC-stream commands (0x90-0x95) are not expanded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, VgmFile

# Furnace format 157: note 0 = C-(-5), 108 = C-4.
FURNACE_C4 = 108
OKI_VOICES = 4
# Nibbles 0..8 are the nine hardware steps (~3 dB). 9..15 are silent.
# Furnace's MSM6295 volume column is 0..8 and the platform writes nibble
# (8 - volume): column 8 is nibble 0 (full scale).
OKI_SILENT_NIBBLE = 9

# furnace/src/engine/platform/msm6295.cpp setFlags, v0.6.8.
# case 8 is 4_000_000/2. rateSel false → divisor 132 (pin 7 high);
# rateSel true → divisor 165 (pin 7 low). Effect 20xx uses the same pair:
# 0 = /132, 1 = /165.
OKI_CLOCK_SEL = {
    1_000_000: 0,
    1_056_000: 1,
    4_000_000: 2,
    4_224_000: 3,
    3_579_545: 4,
    1_789_773: 5,
    1_022_727: 6,
    894_886: 7,
    2_000_000: 8,
    2_112_000: 9,
    875_000: 10,
    937_500: 11,
    1_500_000: 12,
    3_000_000: 13,
    1_193_182: 14,
    3_200_000: 15,
}
# Unbanked Furnace ROM is 256 KB with a 1 KB phrase book and 127 phrases.
OKI_PLAIN_BYTES = 250_000
OKI_PLAIN_PHRASES = 120

# Header 0x94 bits 0-1, and Furnace effect 20xx (0=/512, 1=/768, 2=/1024).
MSM6258_DIVIDERS = (1024, 768, 512, 512)
MSM6258_CLOCK_SEL = {
    4_000_000: 0,
    4_096_000: 1,
    8_000_000: 2,
    8_192_000: 3,
}


def oki_furnace_vol(nibble: int) -> int | None:
    """OKI volume nibble → Furnace column, or None when the chip stays silent."""
    n = nibble & 0x0F
    if n >= OKI_SILENT_NIBBLE:
        return None
    return 8 - n


def oki_rate(clock: int, pin7_high: bool) -> int:
    """Playback Hz. Pin 7 high divides the master clock by 132, low by 165."""
    if clock <= 0:
        return 1
    div = 132 if pin7_high else 165
    return max(1, int(round(clock / div)))


def oki_chip_setup(vgm: VgmFile, chip_id: int) -> tuple[int, bool]:
    """(clock Hz, pin 7 high) for one MSM6295. Chip 1 uses the extra header."""
    if chip_id == 0:
        return vgm.msm6295_clock, vgm.msm6295_pin7
    raw = vgm.extra_clocks.get(0x17, 0)
    if raw:
        return raw & 0x3FFFFFFF, bool(raw & 0x80000000)
    return vgm.msm6295_clock, vgm.msm6295_pin7


def oki_flag_text(clock: int, pin7_high: bool, *, banked: bool) -> str:
    """Furnace FLAG body for one MSM6295."""
    lines: list[str] = []
    sel = OKI_CLOCK_SEL.get(int(clock))
    if sel is not None:
        lines.append(f"clockSel={sel}")
    elif clock > 0:
        lines.append(f"customClock={int(clock)}")
    if not pin7_high:
        lines.append("rateSel=true")
    if banked:
        lines.append("isBanked=true")
    return "\n".join(lines)


def oki_needs_banked(samples: list[OkiSample]) -> bool:
    total = sum(len(s.data) for s in samples)
    return len(samples) > OKI_PLAIN_PHRASES or total > OKI_PLAIN_BYTES


def msm6258_flag_text(clock: int) -> str:
    sel = MSM6258_CLOCK_SEL.get(int(clock))
    if sel is not None:
        return f"clockSel={sel}"
    if clock > 0:
        return f"customClock={int(clock)}"
    return ""


def msm6258_divider(flags: int) -> int:
    return MSM6258_DIVIDERS[flags & 3]


def msm6258_div_fx(divider: int) -> int | None:
    """Furnace 20xx value, or None for the platform default (/512)."""
    if divider == 768:
        return 1
    if divider == 1024:
        return 2
    return None


@dataclass
class OkiHit:
    sample_time: int
    voice: int
    vol: int
    end: int = 0


@dataclass
class OkiSample:
    chip_id: int
    phrase: int
    data: bytes
    rate: int
    clock: int
    pin7: bool
    name: str
    hits: list[OkiHit] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.data)


@dataclass
class AdpcmHit:
    sample_time: int
    end: int = 0


@dataclass
class AdpcmSample:
    chip_id: int
    data: bytes
    rate: int
    clock: int
    divider: int
    name: str
    hits: list[AdpcmHit] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.data)


def _chip_to_phys(addr: int, banks: list[int], banked: bool) -> int:
    """OKI address → ROM offset.

    Unbanked reads are 18-bit. Banked reads follow Furnace's NMK112 map:
    the phrase book (addresses below 0x400) takes its page from
    bank[(addr >> 8) & 3], and every other 64 KB window takes its page
    from bank[(addr >> 16) & 3].
    """
    addr &= 0x3FFFF
    if not banked:
        return addr
    if addr < 0x400:
        page = banks[(addr >> 8) & 3] & 0xFF
        return (page << 16) | (addr & 0x3FF)
    page = banks[(addr >> 16) & 3] & 0xFF
    return (page << 16) | (addr & 0xFFFF)


def _read_mapped(rom: bytes, start: int, stop: int, banks: list[int], banked: bool) -> bytes:
    if stop < start:
        return b""
    out = bytearray()
    addr = start & 0x3FFFF
    end = stop & 0x3FFFF
    while addr <= end:
        phys = _chip_to_phys(addr, banks, banked)
        if not banked:
            n = end - addr + 1
        elif addr < 0x400:
            n = min(end - addr + 1, 0x400 - addr)
        else:
            window_end = (addr & ~0xFFFF) + 0xFFFF
            n = min(end - addr + 1, window_end - addr + 1)
        if phys >= len(rom):
            out += bytes(n)
        else:
            piece = rom[phys:phys + n]
            if len(piece) < n:
                piece = piece + bytes(n - len(piece))
            out += piece
        addr += n
    return bytes(out)


def assemble_oki_rom(vgm: VgmFile, chip_id: int) -> bytes:
    slices = [s for s in vgm.roms if s.kind == "msm6295" and s.chip == chip_id]
    if not slices:
        return b""
    rom_size = max(s.rom_size for s in slices)
    end = max(s.start + len(s.data) for s in slices)
    rom = bytearray(max(rom_size, end))
    for s in slices:
        rom[s.start:s.start + len(s.data)] = s.data
    return bytes(rom)


def collect_oki(vgm: VgmFile) -> tuple[list[OkiSample], list[str]]:
    """Pair 0xB8 command bytes into phrase starts and early stops."""
    warnings: list[str] = []
    chip_ids = sorted(
        {w.chip_id for w in vgm.writes if w.chip == "msm6295"}
        | {s.chip for s in vgm.roms if s.kind == "msm6295"}
    )
    samples: list[OkiSample] = []
    by_body: dict[tuple[int, bytes], OkiSample] = {}
    names: dict[str, int] = {}

    for chip_id in chip_ids:
        rom = assemble_oki_rom(vgm, chip_id)
        clock, pin7 = oki_chip_setup(vgm, chip_id)
        if clock <= 0:
            clock = 1_056_000
            warnings.append(f"MSM6295 chip {chip_id}: clock missing, using {clock} Hz")
        rate = oki_rate(clock, pin7)
        if not rom:
            warnings.append(f"MSM6295 chip {chip_id}: no ROM block (0x8B)")
        banks = [0, 0, 0, 0]
        banked = False
        latch: int | None = None
        active: list[OkiHit | None] = [None] * OKI_VOICES
        empty_phrases = 0

        for w in vgm.writes:
            if w.chip != "msm6295" or w.chip_id != chip_id:
                continue
            reg, val = w.reg, w.val
            if reg == 0x0E:
                banked = bool(val & 0x80)
                continue
            if 0x10 <= reg <= 0x13:
                banks[reg - 0x10] = val
                banked = True
                continue
            if reg != 0:
                continue
            if latch is not None:
                phrase = latch
                latch = None
                mask = (val >> 4) & 0x0F
                vol = oki_furnace_vol(val)
                if vol is None or mask == 0 or not rom:
                    continue
                hdr = _read_mapped(rom, phrase * 8, phrase * 8 + 5, banks, banked)
                start = ((hdr[0] << 16) | (hdr[1] << 8) | hdr[2]) & 0x3FFFF
                stop = ((hdr[3] << 16) | (hdr[4] << 8) | hdr[5]) & 0x3FFFF
                if stop < start or stop == start == 0:
                    empty_phrases += 1
                    continue
                if stop - start > 0x40000:
                    empty_phrases += 1
                    continue
                data = _read_mapped(rom, start, stop, banks, banked)
                if not data or not any(data):
                    empty_phrases += 1
                    continue
                body_key = (chip_id, data)
                smp = by_body.get(body_key)
                if smp is None:
                    label = f"{phrase:02X}"
                    if chip_id:
                        label = f"c{chip_id + 1} {label}"
                    seen = names.get(label, 0) + 1
                    names[label] = seen
                    if seen > 1:
                        label = f"{label} {seen}"
                    smp = OkiSample(
                        chip_id=chip_id,
                        phrase=phrase,
                        data=data,
                        rate=rate,
                        clock=clock,
                        pin7=pin7,
                        name=label,
                    )
                    by_body[body_key] = smp
                    samples.append(smp)
                for voice in range(OKI_VOICES):
                    if not mask & (1 << voice):
                        continue
                    hit = OkiHit(sample_time=w.sample, voice=voice, vol=vol)
                    smp.hits.append(hit)
                    active[voice] = hit
                continue
            if val & 0x80:
                latch = val & 0x7F
                continue
            # 0abcd000: bit (3 + voice) stops that voice.
            for voice in range(OKI_VOICES):
                if (val >> (3 + voice)) & 1:
                    hit = active[voice]
                    if hit is not None and hit.end == 0:
                        hit.end = w.sample
                    active[voice] = None
        if empty_phrases:
            warnings.append(
                f"MSM6295 chip {chip_id}: skipped {empty_phrases} empty phrase starts"
            )

    samples.sort(key=lambda s: s.chip_id)
    return samples, warnings


def collect_msm6258(vgm: VgmFile) -> tuple[list[AdpcmSample], list[str]]:
    """Turn MSM6258 data-port bytes into one VOX sample per burst."""
    warnings: list[str] = []
    chip_ids = sorted({w.chip_id for w in vgm.writes if w.chip == "msm6258"})
    if vgm.msm6258_clock and not chip_ids:
        chip_ids = [0]
    if not chip_ids:
        return [], warnings
    samples: list[AdpcmSample] = []
    divider = msm6258_divider(vgm.msm6258_flags)
    for chip_id in chip_ids:
        clock = vgm.msm6258_clock
        if chip_id and vgm.extra_clocks.get(0x16, 0):
            clock = vgm.extra_clocks[0x16] & 0x3FFFFFFF
        if clock <= 0:
            clock = 4_000_000
            warnings.append(f"MSM6258 chip {chip_id}: clock missing, using {clock} Hz")
        rate = max(1, int(round(clock / divider)))
        # Two nibbles per written byte. A gap of several sample-periods ends a burst.
        byte_samples = max(1, int(round(VGM_RATE * 2 / rate)))
        playing = False
        buf = bytearray()
        start_t = 0
        last_t = 0
        bursts: list[tuple[int, int, bytes]] = []

        def close(end_t: int) -> None:
            nonlocal buf, start_t
            if buf:
                bursts.append((start_t, end_t, bytes(buf)))
            buf = bytearray()

        for w in vgm.writes:
            if w.chip != "msm6258" or w.chip_id != chip_id:
                continue
            if w.reg == 0:
                if w.val & 0x01:
                    playing = False
                    close(w.sample)
                if w.val & 0x02:
                    playing = True
                continue
            if w.reg != 1:
                continue
            if buf and w.sample - last_t > byte_samples * 4:
                close(last_t)
            if not buf:
                start_t = w.sample
                playing = True
            buf.append(w.val)
            last_t = w.sample
        close(last_t)
        if not bursts:
            if playing or vgm.msm6258_clock:
                warnings.append(
                    f"MSM6258 chip {chip_id}: no data-port bytes "
                    "(DAC streams 0x90-0x95 are not converted)"
                )
            continue
        for i, (t0, t1, data) in enumerate(bursts):
            if not any(data):
                continue
            smp = AdpcmSample(
                chip_id=chip_id,
                data=data,
                rate=rate,
                clock=clock,
                divider=divider,
                name=f"adpcm {i:02d}" if not chip_id else f"adpcm{chip_id + 1} {i:02d}",
                hits=[AdpcmHit(sample_time=t0, end=t1)],
            )
            samples.append(smp)
    return samples, warnings
