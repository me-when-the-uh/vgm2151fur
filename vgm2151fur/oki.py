"""MSM6295 phrases and MSM6258 bursts for the Furnace writer.

MSM6295 plays a phrase at one chip-wide rate. VGM command 0xB8 is the
command port plus the bank registers VGMPlay uses for an NMK112-style
mapper (regs 0x0E and 0x10-0x13). The phrase ADPCM is stored as Furnace
VOX and triggered on a fixed note; pitch comes from the chip clock and
pin 7, volume from the OKI nibble.

MSM6258 is a single ADPCM stream. X68000 logs play it through DAC stream
control (data block 0x04 plus 0x90-0x95) aimed at chip type 0x17. A log
with no stream falls back to 0xB7 data-port bursts. Direct bursts shorter
than 4 bytes are the dummy bytes those stream logs also write, and are
dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, Msm6258Play, VgmFile

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

# Header 0x94 bits 0-1. Furnace effect 20xx is not this order: the platform
# sets msmDivider = max(2, 4 - (val & 3)), so 0=/1024, 1=/768, 2=/512.
MSM6258_DIVIDERS = (1024, 768, 512, 512)
# One note-map instrument holds this many samples (Furnace notes 60..179).
ADPCM_MAP_SLOTS = 120
MSM6258_MAP_NOTE = 60
# A 1- or 2-byte 0xB7 write is the dummy the stream logs emit beside 0x95.
MSM6258_MIN_BURST = 4
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
    """Furnace 20xx value, or None for the platform default (/512).

    The C++ core does `msmDivider = max(2, 4 - (val & 3))`: effect 0 is
    /1024, effect 1 is /768, effect 2 is /512.
    """
    if divider == 1024:
        return 0
    if divider == 768:
        return 1
    return None


def msm6258_best_divider(clock: int, flags: int, stream_hz: int) -> int:
    """Divider whose byte rate (clock/divider/2) is closest to the stream.

    Header flags win a tie. `stream_hz` is the 0x92 value, in bytes per
    second. With no stream, the header divider is used as-is.
    """
    header = msm6258_divider(flags)
    if stream_hz <= 0 or clock <= 0:
        return header
    best = header
    best_err = abs(clock / header / 2 - stream_hz)
    for div in (1024, 768, 512):
        err = abs(clock / div / 2 - stream_hz)
        if err < best_err:
            best = div
            best_err = err
    return best


def msm6258_ins_count(n_samples: int) -> int:
    if n_samples <= 0:
        return 0
    return (n_samples + ADPCM_MAP_SLOTS - 1) // ADPCM_MAP_SLOTS


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


def _msm_clock(vgm: VgmFile, chip_id: int, warnings: list[str]) -> int:
    clock = vgm.msm6258_clock
    if chip_id and vgm.extra_clocks.get(0x16, 0):
        clock = vgm.extra_clocks[0x16] & 0x3FFFFFFF
    if clock <= 0:
        clock = 4_000_000
        warnings.append(f"MSM6258 chip {chip_id}: clock missing, using {clock} Hz")
    return clock


def _stride(buf: bytes, offset: int, nbytes: int, step: int, base: int) -> bytes:
    """Bytes a stream writes, walking `step` bytes per command from `offset`."""
    if step <= 1 and base == 0:
        start = max(0, offset)
        if nbytes <= 0:
            return buf[start:]
        return buf[start:start + nbytes]
    out = bytearray()
    cmd = max(0, offset)
    limit = nbytes if nbytes > 0 else len(buf) + 1
    got = 0
    while got < limit:
        idx = cmd * step + base
        if idx < 0 or idx >= len(buf):
            break
        out.append(buf[idx])
        cmd += 1
        got += 1
    return bytes(out)


def _reverse_nibbles(data: bytes) -> bytes:
    """Reverse OKIM6258 nibble order. The chip plays the low nibble first."""
    nibs: list[int] = []
    for byte in data:
        nibs.append(byte & 0x0F)
        nibs.append(byte >> 4)
    nibs.reverse()
    if len(nibs) % 2:
        nibs.append(0)
    out = bytearray()
    for i in range(0, len(nibs), 2):
        out.append(nibs[i] | (nibs[i + 1] << 4))
    return bytes(out)


def _play_bytes(blocks: list[bytes], play: Msm6258Play) -> bytes:
    if play.length_mode == 3 and play.block >= 0:
        if play.block >= len(blocks):
            return b""
        data = _stride(blocks[play.block], 0, 0, play.step, play.step_base)
    else:
        bank = b"".join(blocks)
        nbytes = play.nbytes
        if play.length_mode == 2 and play.rate > 0:
            nbytes = max(0, int(round(play.rate * play.nbytes / 1000)))
        data = _stride(bank, play.offset, nbytes, play.step, play.step_base)
    if play.reverse and data:
        data = _reverse_nibbles(data)
    if play.loop and play.end > play.sample and data and play.rate > 0:
        need = int((play.end - play.sample) / VGM_RATE * play.rate) + 1
        if need > len(data):
            reps = (need + len(data) - 1) // len(data)
            data = (data * reps)[:need]
    return data


def _collect_msm6258_stream(
    vgm: VgmFile, warnings: list[str],
) -> list[AdpcmSample]:
    """One sample per distinct block body. Hits share it; a stop is the end."""
    samples: list[AdpcmSample] = []
    by_body: dict[tuple[int, int, bytes], AdpcmSample] = {}
    clocks: dict[int, int] = {}
    missing = 0
    for play in vgm.msm6258_plays:
        data = _play_bytes(vgm.adpcm_blocks, play)
        if not data:
            missing += 1
            continue
        clock = clocks.get(play.chip_id)
        if clock is None:
            clock = _msm_clock(vgm, play.chip_id, warnings)
            clocks[play.chip_id] = clock
        divider = msm6258_best_divider(clock, vgm.msm6258_flags, play.rate)
        key = (play.chip_id, divider, data)
        smp = by_body.get(key)
        if smp is None:
            smp = AdpcmSample(
                chip_id=play.chip_id,
                data=data,
                rate=max(1, int(round(clock / divider))),
                clock=clock,
                divider=divider,
                name="",
                hits=[],
            )
            by_body[key] = smp
            samples.append(smp)
        smp.hits.append(AdpcmHit(sample_time=play.sample, end=play.end))
    for i, smp in enumerate(samples):
        smp.name = (
            f"adpcm {i:02d}" if not smp.chip_id else f"adpcm{smp.chip_id + 1} {i:02d}"
        )
    if missing:
        warnings.append(
            f"MSM6258: skipped {missing} stream plays with no ADPCM block"
        )
    return samples


def collect_msm6258(vgm: VgmFile) -> tuple[list[AdpcmSample], list[str]]:
    """OKIM6258 phrases: DAC-stream blocks, else 0xB7 bursts of 4 bytes or more."""
    warnings: list[str] = []
    if vgm.msm6258_plays:
        return _collect_msm6258_stream(vgm, warnings), warnings
    return _collect_msm6258_bytes(vgm, warnings), warnings


def _collect_msm6258_bytes(
    vgm: VgmFile, warnings: list[str],
) -> list[AdpcmSample]:
    """Turn MSM6258 data-port bytes into one VOX sample per burst."""
    chip_ids = sorted({w.chip_id for w in vgm.writes if w.chip == "msm6258"})
    if vgm.msm6258_clock and not chip_ids:
        chip_ids = [0]
    if not chip_ids:
        return []
    samples: list[AdpcmSample] = []
    divider = msm6258_divider(vgm.msm6258_flags)
    for chip_id in chip_ids:
        clock = _msm_clock(vgm, chip_id, warnings)
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
        kept = 0
        short = 0
        for t0, t1, data in bursts:
            if len(data) < MSM6258_MIN_BURST or not any(data):
                short += 1
                continue
            smp = AdpcmSample(
                chip_id=chip_id,
                data=data,
                rate=rate,
                clock=clock,
                divider=divider,
                name=f"adpcm {kept:02d}" if not chip_id else f"adpcm{chip_id + 1} {kept:02d}",
                hits=[AdpcmHit(sample_time=t0, end=t1)],
            )
            samples.append(smp)
            kept += 1
        if short and not kept:
            warnings.append(
                f"MSM6258 chip {chip_id}: ignored {short} data burst(s) "
                f"shorter than {MSM6258_MIN_BURST} bytes"
            )
    return samples
