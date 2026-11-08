"""Yamaha YMW258-F (MultiPCM) ROM samples and voices.

The VGM stream does not write channel registers directly.  Command 0xB5 is
the chip's indirect port:

    0xB5 0x01 dd   select a channel (value 0-31; every 8th selector is invalid)
    0xB5 0x02 dd   choose the channel register (0-10)
    0xB5 0x00 dd   write dd to that register

Registers 0x10/0x11/0x12 select a Sega ROM bank, and command 0xC3 asks the
player to write those same registers.  Bit 7 of a 0xB5/0xC3 register byte
selects the second chip instance.

The sample header sits at ``(index * 12) & rom_mask``:

    0-2   start address; bits 20-23 are the format (bit 2 = 12-bit)
    3-4   loop start, relative to the body
    5-6   0x10000 - body length
    7     LFO rate and vibrato depth
    8     attack rate and decay 1 rate
    9     decay level and decay 2 rate
    10    rate correction and release rate
    11    amplitude LFO depth

Only the bodies a song actually triggers are kept.  The module uses Furnace's
native MultiPCM system, whose platform writes one instrument header per module
instrument; the converter therefore remaps the source's 9-bit sample index to
a module instrument index and keeps the source body/loop/ADSR behind it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, VgmFile, assemble_rom

# libvgm's VALUE_TO_CHANNEL: a channel selector with every 8th entry invalid.
VALUE_TO_CHANNEL = (
    0, 1, 2, 3, 4, 5, 6, -1,
    7, 8, 9, 10, 11, 12, 13, -1,
    14, 15, 16, 17, 18, 19, 20, -1,
    21, 22, 23, 24, 25, 26, 27, -1,
)
MULTIPCM_VOICES = 28
MULTIPCM_CLOCK_DIVIDER = 180  # VGM clock -> chip output rate


def _pow2_mask(size: int) -> int:
    """libvgm pow2_mask: the ROM address mask used by the chip."""
    if size <= 1:
        return 0
    mask = 1
    while mask * 2 <= size:
        mask *= 2
    return mask - 1


def chip_rate(clock: int) -> float:
    """MultiPCM sample-step rate for a raw VGM clock.

    VGM stores the clock with the old /180 divider.  VGMPlay multiplies it by
    224/180 for the core, and the core divides by 224, so the effective rate is
    simply ``clock / 180``.
    """
    return max(1.0, (clock or 8_053_975) / MULTIPCM_CLOCK_DIVIDER)


def played_rate(clock: int, octave: int, fnum: int) -> float:
    """Playback rate in samples/second for an octave/f-num pair.

    The chip's octave is a signed 4-bit value.  ``octave = 8`` is a negative
    shift, so 0xF is -1 rather than 15.
    """
    if octave & 8:
        octave -= 16
    if octave <= -8:
        return 0.0
    return (
        chip_rate(clock)
        * (1024 + fnum)
        / 1024.0
        * (2.0 ** (octave - 1))
    )


@dataclass
class MultipcmHit:
    sample_time: int
    chip_id: int
    voice: int
    sample: int
    octave: int
    fnum: int
    tl: int
    pan: int
    direct: bool
    clock: int
    # Mid-note (time, octave, fnum) writes.
    freq_pts: list[tuple[int, int, int]] = field(default_factory=list)
    # Mid-note (time, TL, pan) writes.
    vol_pts: list[tuple[int, int, int]] = field(default_factory=list)
    # LFO/VIB/AM register writes while held: (time, code, value), where code is
    # 0x20 = LFO rate, 0x21 = PM depth and 0x22 = AM depth.
    lfo_pts: list[tuple[int, int, int]] = field(default_factory=list)
    off_sample: int = 0


@dataclass
class MultipcmSample:
    chip_id: int
    index: int
    start: int
    data: bytes
    frames: int
    depth: int  # 8 or 12
    loop_start: int | None
    loop_end: int | None
    ar: int
    d1r: int
    dl: int
    d2r: int
    rr: int
    rc: int
    lfo: int
    vib: int
    am: int
    direct: bool
    clock: int
    hits: list[MultipcmHit] = field(default_factory=list)

    @property
    def length(self) -> int:
        return self.frames

    def mode_pitch(self) -> tuple[int, int]:
        """Most common nonzero (octave, fnum) among the hits."""
        if not self.hits:
            return 1, 0
        freqs = Counter(
            (h.octave, h.fnum) for h in self.hits
            if h.fnum or h.octave != 8
        )
        if not freqs:
            return 1, 0
        return freqs.most_common(1)[0][0]

    def c4_rate(self) -> int:
        octave, fnum = self.mode_pitch()
        rate = played_rate(self.clock, octave, fnum)
        return max(1, min(192000, int(round(rate))))


@dataclass
class _Header:
    start: int
    depth: int
    loop: int
    end: int
    ar: int
    d1r: int
    dl: int
    d2r: int
    rr: int
    rc: int
    lfo: int
    vib: int
    am: int


@dataclass
class _Channel:
    regs: list[int] = field(default_factory=lambda: [0] * 11)
    active: bool = False
    hit: MultipcmHit | None = None
    sample_index: int = 0


def _read_header(rom: bytes, mask: int, index: int) -> _Header | None:
    off = (index * 12) & mask
    h = rom[off:off + 12]
    if len(h) < 12:
        return None
    start = (h[0] << 16) | (h[1] << 8) | h[2]
    fmt = (start >> 20) & 0xFE
    start &= 0x3FFFFF
    loop = (h[3] << 8) | h[4]
    end = 0x10000 - ((h[5] << 8) | h[6])
    return _Header(
        start=start,
        depth=12 if (fmt & 4) else 8,
        loop=loop,
        end=end,
        ar=(h[8] >> 4) & 0xF,
        d1r=h[8] & 0xF,
        dl=(h[9] >> 4) & 0xF,
        d2r=h[9] & 0xF,
        rc=(h[10] >> 4) & 0xF,
        rr=h[10] & 0xF,
        lfo=(h[7] >> 3) & 7,
        vib=h[7] & 7,
        am=h[11] & 0xF,
    )


def _resolve_base(start: int, banking: bool, bank0: int, bank1: int) -> int:
    """Apply libvgm's Sega MultiPCM bank mapping to a sample start address."""
    base = start & 0x1FFFFF
    if banking and (base & 0x100000):
        if base & 0x080000:
            base = (base & 0x07FFFF) | bank1
        else:
            base = (base & 0x07FFFF) | bank0
    return base


def collect_multipcm(
    vgm: VgmFile,
) -> tuple[list[MultipcmSample], list[str]]:
    """Decode MultiPCM register writes into sample bodies and hits."""
    warnings: list[str] = []
    ids = sorted({
        w.chip_id for w in vgm.writes if w.chip in ("multipcm", "multipcm_bank")
    })
    if not ids:
        return [], warnings

    roms: dict[int, bytes] = {}
    masks: dict[int, int] = {}
    headers: dict[tuple[int, int], _Header | None] = {}
    for cid in ids:
        rom = assemble_rom(vgm, "multipcm", cid)
        if not rom:
            warnings.append(
                f"MultiPCM chip {cid + 1} has no type-0x89 ROM block; "
                "its voices will be silent"
            )
        roms[cid] = rom
        masks[cid] = _pow2_mask(len(rom)) if rom else 0

    channels: dict[int, list[_Channel]] = {
        cid: [_Channel() for _ in range(MULTIPCM_VOICES)] for cid in ids
    }
    banks: dict[int, list[int]] = {cid: [0, 0, 0] for cid in ids}  # banking, bank0, bank1
    cur: dict[int, int] = {cid: -1 for cid in ids}
    addr: dict[int, int] = {cid: 0 for cid in ids}
    samples: dict[tuple[int, int], MultipcmSample] = {}
    clock = vgm.multipcm_clock or 8_053_975

    def apply_bank(cid: int, mask: int, number: int) -> None:
        st = banks[cid]
        if mask == 3 and not (number & 0x08):
            # 1 MB banking (Sega Model 1): VGMPlay writes register 0x10 with
            # number / 0x10, and the core shifts that by 20.
            st[0] = 1
            bank = (number // 0x10) << 20
            st[1] = bank
            st[2] = bank | 0x080000
        else:
            # 512 KB banking (Sega Multi 32): registers 0x11/0x12 take
            # number / 8, shifted by 19 by the core.
            st[0] = 1
            bank = (number // 0x08) << 19
            if mask & 0x02:
                st[1] = bank
            if mask & 0x01:
                st[2] = bank

    def ensure_sample(cid: int, index: int) -> MultipcmSample | None:
        key = (cid, index)
        found = samples.get(key)
        if found is not None:
            return found
        rom = roms[cid]
        if not rom:
            return None
        header = headers.get(key)
        if key not in headers:
            header = _read_header(rom, masks[cid], index)
            headers[key] = header
        if header is None:
            warnings.append(
                f"MultiPCM chip {cid + 1} sample {index}: header lies past "
                "the ROM"
            )
            return None
        base = _resolve_base(header.start, bool(banks[cid][0]),
                             banks[cid][1], banks[cid][2])
        if header.depth == 12:
            byte_len = (header.end * 3 + 1) // 2
        else:
            byte_len = header.end
        body = rom[base:base + byte_len]
        if not body:
            warnings.append(
                f"MultiPCM chip {cid + 1} sample {index}: body at "
                f"{base:#x} is empty"
            )
            return None
        if len(body) < byte_len:
            warnings.append(
                f"MultiPCM chip {cid + 1} sample {index}: body truncated "
                f"({len(body)} of {byte_len} bytes)"
            )
            body = body + bytes(byte_len - len(body))
        frames = header.end
        # Furnace's 8-bit MultiPCM buffer tops out at 65535 frames.
        if header.depth == 8 and frames > 65535:
            warnings.append(
                f"MultiPCM chip {cid + 1} sample {index}: {frames} frames, "
                "trimmed to Furnace's 65535-frame limit"
            )
            frames = 65535
            body = body[:frames]
        loop_start: int | None = None
        loop_end: int | None = None
        if 0 <= header.loop < frames - 1:
            loop_start = header.loop
            loop_end = frames
        sample = MultipcmSample(
            chip_id=cid,
            index=index,
            start=base,
            data=bytes(body),
            frames=frames,
            depth=header.depth,
            loop_start=loop_start,
            loop_end=loop_end,
            ar=header.ar,
            d1r=header.d1r,
            dl=header.dl,
            d2r=header.d2r,
            rr=header.rr,
            rc=header.rc,
            lfo=header.lfo,
            vib=header.vib,
            am=header.am,
            direct=True,
            clock=clock,
        )
        samples[key] = sample
        return sample

    def open_hit(cid: int, voice: int, t: int) -> None:
        ch = channels[cid][voice]
        index = ch.regs[1] | ((ch.regs[2] & 1) << 8)
        sample = ensure_sample(cid, index)
        if sample is None:
            return
        if ch.active and ch.hit is not None and not ch.hit.off_sample:
            ch.hit.off_sample = t
        octave = (ch.regs[3] >> 4) & 0xF
        fnum = ((ch.regs[3] & 0xF) << 6) | (ch.regs[2] >> 2)
        tl = (ch.regs[5] >> 1) & 0x7F
        pan = (ch.regs[0] >> 4) & 0xF
        hit = MultipcmHit(
            sample_time=t,
            chip_id=cid,
            voice=voice,
            sample=index,
            octave=octave,
            fnum=fnum,
            tl=tl,
            pan=pan,
            direct=bool(ch.regs[5] & 1),
            clock=clock,
        )
        sample.hits.append(hit)
        ch.hit = hit
        ch.active = True
        ch.sample_index = index

    for w in vgm.writes:
        cid = w.chip_id
        if cid not in channels:
            continue
        if w.chip == "multipcm_bank":
            apply_bank(cid, w.reg & 3, w.val)
            continue
        if w.chip != "multipcm":
            continue
        reg = w.reg
        if reg == 1:
            cur[cid] = VALUE_TO_CHANNEL[w.val & 0x1F]
            continue
        if reg == 2:
            addr[cid] = w.val
            continue
        if reg in (0x10, 0x11, 0x12):
            st = banks[cid]
            st[0] = 1
            if reg == 0x10:
                st[1] = (w.val << 20) & 0xFFFFFF
                st[2] = ((w.val << 20) & 0xFFFFFF) | 0x080000
            elif reg == 0x11:
                st[1] = (w.val << 19) & 0xFFFFFF
            else:
                st[2] = (w.val << 19) & 0xFFFFFF
            continue
        if reg != 0:
            continue
        voice = cur[cid]
        if voice < 0:
            continue
        ch = channels[cid][voice]
        a = addr[cid]
        if a >= 11:
            continue
        old = ch.regs[a]
        ch.regs[a] = w.val
        if a == 1:
            # Selecting a sample re-inits the header and retriggers a playing
            # voice.  The source almost always writes it before key-on.
            if ch.active and w.val != old:
                open_hit(cid, voice, w.sample)
        elif a in (2, 3):
            if ch.active and ch.hit is not None:
                octave = (ch.regs[3] >> 4) & 0xF
                fnum = ((ch.regs[3] & 0xF) << 6) | (ch.regs[2] >> 2)
                ch.hit.freq_pts.append((w.sample, octave, fnum))
        elif a == 4:
            if w.val & 0x80:
                open_hit(cid, voice, w.sample)
            elif ch.active and ch.hit is not None:
                if not ch.hit.off_sample:
                    ch.hit.off_sample = w.sample
                ch.active = False
        elif a == 5:
            if ch.active and ch.hit is not None:
                tl = (w.val >> 1) & 0x7F
                pan = (ch.regs[0] >> 4) & 0xF
                ch.hit.vol_pts.append((w.sample, tl, pan))
                ch.hit.direct = bool(w.val & 1)
        elif a == 0:
            if ch.active and ch.hit is not None:
                tl = (ch.regs[5] >> 1) & 0x7F
                pan = (w.val >> 4) & 0xF
                ch.hit.vol_pts.append((w.sample, tl, pan))
        elif a == 6:
            if ch.active and ch.hit is not None:
                ch.hit.lfo_pts.append((w.sample, 0x20, (w.val >> 3) & 7))
                ch.hit.lfo_pts.append((w.sample, 0x21, w.val & 7))
        elif a == 10:
            if ch.active and ch.hit is not None:
                ch.hit.lfo_pts.append((w.sample, 0x22, w.val & 7))

    audible = [s for s in samples.values() if s.hits and s.length > 0]
    audible.sort(key=lambda s: (s.chip_id, s.index))
    for sample in audible:
        direct = Counter(bool(h.direct) for h in sample.hits)
        sample.direct = direct.most_common(1)[0][0] if direct else True
    warnings = list(dict.fromkeys(warnings))
    return audible, warnings
