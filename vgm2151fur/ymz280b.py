"""Yamaha YMZ280B (PCMD8) register stream -> sample bodies and notes.

The YMZ280B has eight sample voices.  Each voice has a start, loop-start,
loop-end and stop address, a 9-bit frequency, an 8-bit linear level, a 0-15
pan value and a control byte selecting 4-bit ADPCM, 8-bit PCM or 16-bit PCM.

Register model (MAME ``ymz280b.cpp``)::

    ch*4 + 0x00  frequency low
    ch*4 + 0x01  control: bit 7 key-on, bit 4 loop, bit 0 frequency bit 8,
                        bits 5-6 mode (1 ADPCM, 2 PCM8, 3 PCM16)
    ch*4 + 0x02  total level (linear, 255 = full scale)
    ch*4 + 0x03  pan (0/1 hard left, 8 centre, 15 hard right)
    ch*4 + 0x20  start high       0x40 start mid       0x60 start low
    ch*4 + 0x21  loop start high  0x41 loop start mid  0x61 loop start low
    ch*4 + 0x22  loop end high    0x42 loop end mid    0x62 loop end low
    ch*4 + 0x23  stop high        0x43 stop mid        0x63 stop low

The address registers are byte addresses into the external sample ROM.  A
key-on latches the address/mode state; rewriting start/stop while the voice is
held does not affect the current note.  A control write with bit 7 already set
is a pitch/loop update, not a retrigger.

Playback rate::

    source samples/second = clock * (fnum + 1) / 98304

4-bit ADPCM uses only the low eight frequency bits (maximum clock/384), while
8-bit and 16-bit PCM use all nine (maximum clock/192).

The ROM is big-endian 16-bit PCM and high-nibble-first ADPCM.  Furnace's 16-bit
SMP2 bodies are little-endian, so each 16-bit pair is swapped while slicing.
ADPCM bodies are stored raw as Furnace depth 3 (YMZ/YMU ADPCM).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import (
    VGM_RATE, VgmFile, YMZ280B_DEFAULT_CLOCK, assemble_rom,
)

YMZ280B_VOICES = 8
YMZ280B_RATE_DIVISOR = 98304.0
# Furnace sample depths we store.
YMZ_DEPTH_ADPCM = 3
YMZ_DEPTH_PCM8 = 8
YMZ_DEPTH_PCM16 = 16


def ymz280b_clock(vgm: VgmFile, chip_id: int) -> int:
    """Clock for one YMZ280B instance."""
    if chip_id == 1 and vgm.ymz280b_clock_2:
        return vgm.ymz280b_clock_2
    return vgm.ymz280b_clock or YMZ280B_DEFAULT_CLOCK


def ymz280b_rate(clock: int, fnum: int, depth: int) -> float:
    """Source frames per second for a frequency register and sample depth."""
    mask = 0xFF if depth == YMZ_DEPTH_ADPCM else 0x1FF
    n = (int(fnum) & mask) + 1
    return max(1.0, (clock or YMZ280B_DEFAULT_CLOCK) * n / YMZ280B_RATE_DIVISOR)


def ymz_depth(mode: int) -> int:
    """Control mode bits -> Furnace sample depth (0 when no mode was set)."""
    return {
        1: YMZ_DEPTH_ADPCM,
        2: YMZ_DEPTH_PCM8,
        3: YMZ_DEPTH_PCM16,
    }.get(mode & 0x03, 0)


def ymz_pan_ratio(pan: int) -> tuple[int, int]:
    """The chip's 0-15 pan law as a 0-15 left/right pair.

    Pan 8 is centre.  Pan 1 is hard left (pan 0 is the same as 1); pan 15 is
    hard right.  The ratios ignore the level, which the volume column carries.
    """
    p = pan & 0x0F
    if p == 8:
        return 15, 15
    if p < 8:
        right = 0 if p == 0 else ((p - 1) * 15 + 3) // 7
        return 15, right
    left = ((15 - p) * 15 + 3) // 7
    return left, 15


def ymz_pan_fx(pan: int) -> int:
    """Furnace 08xy split for a YMZ pan value, loud side at 15."""
    left, right = ymz_pan_ratio(pan)
    return ((left & 0x0F) << 4) | (right & 0x0F)


def _swap16(data: bytes) -> bytes:
    """ROM big-endian 16-bit PCM -> little-endian SMP2 body."""
    out = bytearray(len(data))
    out[0::2] = data[1::2]
    out[1::2] = data[0::2]
    return bytes(out)


@dataclass
class YmzHit:
    sample_time: int
    chip_id: int
    voice: int
    start: int
    stop: int
    loop_start: int
    loop_end: int
    loop_on: bool
    depth: int
    fnum: int
    vol: int
    pan: int
    # Key-off sample (0 = still playing at the end).
    off_sample: int = 0
    # Mid-note writes while the voice stays keyed.
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    pan_pts: list[tuple[int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)

    @property
    def length(self) -> int:
        """Source frames this hit plays (before the loop wraps)."""
        if self.depth == YMZ_DEPTH_PCM16:
            return max(0, (self.stop - self.start) // 2)
        if self.depth == YMZ_DEPTH_ADPCM:
            return max(0, (self.stop - self.start) * 2)
        return max(0, self.stop - self.start)


@dataclass
class YmzOff:
    sample_time: int
    chip_id: int
    voice: int


def hit_played_fnum(hit: YmzHit) -> int:
    """The frequency a hit actually starts on.

    A few drivers write the frequency just after the key-on byte at the same
    sample; the collector folds those into the hit already.  If the register
    was still zero, accept the first later nonzero write as the note's own
    pitch instead of pinning the sample reference to 172 Hz.
    """
    if hit.fnum:
        return hit.fnum
    for _t, fnum in hit.freq_pts:
        if fnum:
            return fnum
    return 0


@dataclass
class YmzSample:
    chip_id: int
    start: int
    stop: int
    data: bytes
    depth: int
    loop_start: int | None = None
    loop_end: int | None = None
    hits: list[YmzHit] = field(default_factory=list)
    clock: int = 0

    @property
    def length(self) -> int:
        """Frame count in the sample's own units.

        8-bit: bytes.  16-bit: 16-bit frames.  ADPCM: nibbles.
        """
        if self.depth == YMZ_DEPTH_PCM16:
            return len(self.data) // 2
        if self.depth == YMZ_DEPTH_ADPCM:
            return len(self.data) * 2
        return len(self.data)

    def mode_fnum(self) -> int:
        """Most common nonzero key-on frequency (0 when unknown)."""
        if not self.hits:
            return 0
        freqs = Counter(f for h in self.hits if (f := hit_played_fnum(h)))
        if not freqs:
            return 0
        return freqs.most_common(1)[0][0]

    def c4_rate(self) -> int:
        return max(1, int(round(ymz280b_rate(
            self.clock or YMZ280B_DEFAULT_CLOCK, self.mode_fnum(), self.depth,
        ))))


@dataclass
class _Voice:
    start: int = 0
    stop: int = 0
    loop_start: int = 0
    loop_end: int = 0
    loop: bool = False
    mode: int = 0
    fnum: int = 0
    ctrl: int = 0
    vol: int = 0
    pan: int = 0


def _record_freq(hit: YmzHit | None, when: int, fnum: int,
                 old: int) -> None:
    if hit is None or fnum == old:
        return
    if when <= hit.sample_time:
        hit.fnum = fnum
    else:
        hit.freq_pts.append((when, fnum))


def _record_volume(hit: YmzHit | None, when: int, vol: int, old: int) -> None:
    if hit is None or vol == old:
        return
    if when <= hit.sample_time:
        hit.vol = vol
    else:
        hit.vol_pts.append((when, vol))


def _record_pan(hit: YmzHit | None, when: int, pan: int, old: int) -> None:
    if hit is None or pan == old:
        return
    if when <= hit.sample_time:
        hit.pan = pan
    else:
        hit.pan_pts.append((when, pan))


def collect_ymz280b(
    vgm: VgmFile,
) -> tuple[list[YmzSample], list[YmzOff], list[str]]:
    """Collect YMZ280B key-ons, key-offs and sample bodies."""
    writes = [w for w in vgm.writes if w.chip == "ymz280b"]
    warnings: list[str] = []
    if not writes:
        return [], [], warnings

    voices = [_Voice() for _ in range(YMZ280B_VOICES * 2)]
    active: list[YmzHit | None] = [None] * (YMZ280B_VOICES * 2)
    hits: list[YmzHit] = []
    offs: list[YmzOff] = []
    keyon_enable = [False, False]
    missed_keyons = 0
    bad_mode = 0
    midnote_mode_loop = 0
    ram_writes = 0
    # 0x84/0x85 latch the high/middle RAM-write address; 0x87 writes one byte
    # and increments it.  These logs normally ship a type-0x86 ROM instead.
    ram_addr = 0

    for w in writes:
        reg = w.reg & 0xFF
        val = w.val & 0xFF
        t = w.sample
        cid = w.chip_id & 1

        if reg < 0x80:
            vi = cid * YMZ280B_VOICES + ((reg >> 2) & 7)
            base = vi % YMZ280B_VOICES
            voice = voices[vi]
            kind = reg & 0xE3
            hit = active[vi]

            if kind == 0x00:
                old = voice.fnum
                voice.fnum = (voice.fnum & 0x100) | val
                _record_freq(hit, t, voice.fnum, old)
            elif kind == 0x01:
                old_fnum = voice.fnum
                old_ctrl = voice.ctrl
                old_loop = voice.loop
                old_mode = voice.mode
                voice.fnum = (voice.fnum & 0xFF) | ((val & 1) << 8)
                voice.loop = bool(val & 0x10)
                if val & 0x60:
                    voice.mode = (val & 0x60) >> 5
                key = bool(val & 0x80)
                was = bool(old_ctrl & 0x80)

                if key and not was:
                    if not keyon_enable[cid]:
                        missed_keyons += 1
                    else:
                        depth = ymz_depth(voice.mode)
                        if depth == 0:
                            bad_mode += 1
                        else:
                            new_hit = YmzHit(
                                sample_time=t,
                                chip_id=cid,
                                voice=base,
                                start=voice.start,
                                stop=voice.stop,
                                loop_start=voice.loop_start,
                                loop_end=voice.loop_end,
                                loop_on=voice.loop,
                                depth=depth,
                                fnum=voice.fnum,
                                vol=voice.vol,
                                pan=voice.pan & 0x0F,
                            )
                            hits.append(new_hit)
                            active[vi] = new_hit
                elif key and was and hit is not None:
                    _record_freq(hit, t, voice.fnum, old_fnum)
                    if voice.loop != old_loop or voice.mode != old_mode:
                        # The chip changes these live; the module's sample
                        # loop/depth is fixed at key-on.
                        midnote_mode_loop += 1
                elif not key and was:
                    if hit is not None:
                        hit.off_sample = t
                        offs.append(YmzOff(t, cid, base))
                        active[vi] = None
                voice.ctrl = val
            elif kind == 0x02:
                old = voice.vol
                voice.vol = val
                _record_volume(hit, t, val, old)
            elif kind == 0x03:
                old = voice.pan
                voice.pan = val & 0x0F
                _record_pan(hit, t, voice.pan, old)
            elif kind == 0x20:
                voice.start = (voice.start & 0x00FFFF) | (val << 16)
            elif kind == 0x21:
                voice.loop_start = (voice.loop_start & 0x00FFFF) | (val << 16)
            elif kind == 0x22:
                voice.loop_end = (voice.loop_end & 0x00FFFF) | (val << 16)
            elif kind == 0x23:
                voice.stop = (voice.stop & 0x00FFFF) | (val << 16)
            elif kind == 0x40:
                voice.start = (voice.start & 0xFF00FF) | (val << 8)
            elif kind == 0x41:
                voice.loop_start = (voice.loop_start & 0xFF00FF) | (val << 8)
            elif kind == 0x42:
                voice.loop_end = (voice.loop_end & 0xFF00FF) | (val << 8)
            elif kind == 0x43:
                voice.stop = (voice.stop & 0xFF00FF) | (val << 8)
            elif kind == 0x60:
                voice.start = (voice.start & 0xFFFF00) | val
            elif kind == 0x61:
                voice.loop_start = (voice.loop_start & 0xFFFF00) | val
            elif kind == 0x62:
                voice.loop_end = (voice.loop_end & 0xFFFF00) | val
            elif kind == 0x63:
                voice.stop = (voice.stop & 0xFFFF00) | val
            continue

        if reg == 0xFF:
            old_enable = keyon_enable[cid]
            keyon_enable[cid] = bool(val & 0x80)
            if old_enable and not keyon_enable[cid]:
                # Turning this chip off stops its voices.  A later re-enable
                # only resumes looping voices; the module already gets an off.
                for vi, hit in enumerate(active):
                    if hit is not None and vi // YMZ280B_VOICES == cid:
                        hit.off_sample = t
                        offs.append(YmzOff(
                            t, hit.chip_id, vi % YMZ280B_VOICES,
                        ))
                        active[vi] = None
        elif reg == 0x84:
            ram_addr = (ram_addr & 0x00FFFF) | (val << 16)
        elif reg == 0x85:
            ram_addr = (ram_addr & 0xFF00FF) | (val << 8)
        elif reg == 0x86:
            ram_addr = (ram_addr & 0xFFFF00) | val
        elif reg == 0x87:
            ram_writes += 1
            ram_addr = (ram_addr + 1) & 0xFFFFFF

    if missed_keyons:
        warnings.append(
            f"{missed_keyons} YMZ280B key-on(s) while 0xFF bit 7 was clear; "
            "the chip would stay silent"
        )
    if bad_mode:
        warnings.append(
            f"{bad_mode} YMZ280B key-on(s) had no sample mode set; "
            "those notes stay silent"
        )
    if midnote_mode_loop:
        warnings.append(
            f"{midnote_mode_loop} YMZ280B held-note loop/mode change(s) kept "
            "the key-on body"
        )
    if ram_writes:
        warnings.append(
            "YMZ280B register RAM writes (0x87) are not replayed; sample "
            "bodies come from the type-0x86 ROM block"
        )
    if not any(w.chip == "ymz280b" for w in vgm.writes):
        return [], offs, warnings

    # Slice the ROM once per chip.  Bodies are deduplicated by their full
    # address/loop/depth identity, so repeated drum hits share one instrument.
    by_key: dict[tuple, YmzSample] = {}
    chips = sorted({h.chip_id for h in hits})
    roms = {
        cid: assemble_rom(vgm, "ymz280b", cid)
        for cid in chips
    }
    missing_rom = [cid for cid in chips if not roms.get(cid)]
    if missing_rom:
        names = ", ".join(str(cid + 1) for cid in missing_rom)
        warnings.append(
            f"no YMZ280B ROM block (0x86) for chip {names}; "
            "those samples will be silent"
        )
    clamped = 0
    bad_bodies = 0
    for h in hits:
        rom = roms.get(h.chip_id, b"")
        clock = ymz280b_clock(vgm, h.chip_id)
        if not rom:
            continue
        start = max(0, min(int(h.start), len(rom)))
        stop = max(0, min(int(h.stop), len(rom)))
        if stop <= start:
            bad_bodies += 1
            continue
        if stop != h.stop or start != h.start:
            clamped += 1

        loop_key_start = h.loop_start if h.loop_on else 0
        loop_key_end = h.loop_end if h.loop_on else 0
        key = (
            h.chip_id, h.depth, start, stop, loop_key_start, loop_key_end,
        )
        smp = by_key.get(key)
        if smp is None:
            if h.depth == YMZ_DEPTH_PCM16:
                even_stop = start + ((stop - start) & ~1)
                body = _swap16(rom[start:even_stop])
            else:
                body = rom[start:stop]
            smp = YmzSample(
                chip_id=h.chip_id,
                start=start,
                stop=stop,
                data=body,
                depth=h.depth,
                clock=clock,
            )
            if h.loop_on:
                ls = int(h.loop_start)
                le = int(h.loop_end)
                if ls < start or le > stop or le <= ls:
                    warnings.append(
                        f"YMZ280B loop {ls:06x}-{le:06x} outside body "
                        f"{start:06x}-{stop:06x}; loop disabled"
                    )
                else:
                    if h.depth == YMZ_DEPTH_PCM16:
                        loop_start = (ls - start) // 2
                        loop_end = ((le - start) + 1) // 2
                    elif h.depth == YMZ_DEPTH_ADPCM:
                        loop_start = (ls - start) * 2
                        loop_end = (le - start) * 2
                    else:
                        loop_start = ls - start
                        loop_end = le - start
                    if 0 <= loop_start < loop_end <= smp.length:
                        smp.loop_start = loop_start
                        smp.loop_end = loop_end
                    else:
                        warnings.append(
                            f"YMZ280B loop {ls:06x}-{le:06x} does not fit "
                            f"body {start:06x}-{stop:06x}; loop disabled"
                        )
            by_key[key] = smp
        smp.hits.append(h)

    samples = sorted(
        by_key.values(), key=lambda s: (s.chip_id, s.start, s.depth),
    )
    if clamped:
        warnings.append(
            f"{clamped} YMZ280B key-on(s) addressed past the end of the "
            "dumped ROM; the body was clamped"
        )
    if bad_bodies:
        warnings.append(
            f"{bad_bodies} YMZ280B key-on(s) had an empty start/stop range"
        )
    return samples, offs, warnings
