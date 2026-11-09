"""Konami K054539 ROM -> sample bodies for the Generic PCM DAC.

The K054539 has eight voices and no Furnace platform.  Each hardware voice is
converted to one 1-channel Generic PCM DAC system, so pitch, volume, panning
and note cuts remain per voice.

Register model follows libvgm k054539.c:

    0x00-0x02 pitch delta (24-bit)     0x03 volume (0 = max, 0x40 = -36 dB)
    0x04 reverb send                   0x05 pan (0x11-0x1f / 0x81-0x8f)
    0x08-0x0a loop address             0x0c-0x0e start address
    0x200+2*ch type (bits 2-3), reverse (bit 5), loop (byte +1 bit 0)
    0x214 key-on bits                  0x215 key-off bits
    0x22c active bits                  0x22e ROM bank (readback only)
    0x22f bit 0 = PCM enable, bit 7 = disable register updates

Sample formats are signed 8-bit (marker 0x80), signed 16-bit LE (marker
0x00 0x80) and 4-bit DPCM (marker 0x88).  A marker ends a one-shot or jumps to
the loop address.  The body is decoded to linear 8/16-bit PCM; DPCM is decoded
to 16-bit.  Reverb is not representable on the Generic PCM DAC and is reported
as a conversion limit.
"""

from __future__ import annotations

import math
import struct
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VgmFile, assemble_rom

K054539_VOICES = 8
K054539_OUT_DIV = 384
K054539_RATE_DIVISOR = K054539_OUT_DIV * 65536
K054539_DEFAULT_CLOCK = 18_432_000
K054539_MAX_BODY = 4 * 1024 * 1024

FORMAT_8BIT = 0
FORMAT_16BIT = 1
FORMAT_DPCM = 2

K054539_UPDATE_AT_KEYON = 0x04

DPCM_STEP = (
    0, 1, 2, 4, 8, 16, 32, 64,
    0, -64, -32, -16, -8, -4, -2, -1,
)


def k054539_rate(clock: int, delta: int) -> float:
    """Sample frame rate for a pitch delta, in frames/second."""
    return (clock or K054539_DEFAULT_CLOCK) * abs(delta) / K054539_RATE_DIVISOR


def k054539_out_rate(clock: int) -> int:
    return max(1000, int(round((clock or K054539_DEFAULT_CLOCK) / K054539_OUT_DIV)))


def k054539_volume(level: int) -> float:
    """Linear amplitude for the attenuation byte (0 = unity)."""
    return 10.0 ** (-36.0 * (level & 0xFF) / 64.0 / 20.0)


def k054539_pan_index(pan: int) -> int:
    if 0x81 <= pan <= 0x8F:
        return pan - 0x81
    if 0x11 <= pan <= 0x1F:
        return pan - 0x11
    return 7  # centre


def k054539_pan_levels(pan: int) -> tuple[int, int]:
    """(left, right) 0-255 amplitudes, square-root constant-power law."""
    idx = k054539_pan_index(pan)
    left = math.sqrt(idx) / math.sqrt(14.0)
    right = math.sqrt(14 - idx) / math.sqrt(14.0)
    return int(round(left * 255)), int(round(right * 255))


def k054539_pan_fx(pan: int) -> int:
    """Furnace 08xy from the 15 K054539 pan positions."""
    left, right = k054539_pan_levels(pan)
    return ((int(round(left * 15 / 255)) & 15) << 4) | (
        int(round(right * 15 / 255)) & 15
    )


def k054539_volume_fx(level: int) -> int:
    return max(0, min(255, int(round(k054539_volume(level) * 255))))


def hit_played_delta(hit: "K054539Hit") -> int:
    """Pitch delta a hit actually uses.

    Drivers that key on before writing the pitch leave the key-on snapshot at
    zero.  The first in-frame delta write is then the note's own pitch.
    """
    if hit.delta:
        return abs(hit.delta)
    for _t, delta in hit.freq_pts:
        if delta:
            return abs(delta)
    return 0


def _pow2_mask(size: int) -> int:
    if size <= 0:
        return 0
    return (1 << (size - 1).bit_length()) - 1


def _covered(vgm: VgmFile, chip_id: int, addr: int) -> bool:
    for s in vgm.roms:
        if s.chip == chip_id and s.kind == "k054539" and s.data:
            if s.start <= addr < s.start + len(s.data):
                return True
    return False


@dataclass
class K054539Hit:
    sample_time: int
    chip_id: int
    voice: int
    start: int
    loop: int
    loop_on: bool
    format: int
    reverse: bool
    delta: int
    vol: int
    pan: int
    reverb: int
    off_sample: int = 0
    vol_pts: list[tuple[int, int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class K054539Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class K054539Sample:
    start: int
    end: int
    data: bytes
    depth: int
    loop_start: int | None = None
    loop_end: int | None = None
    reverse: bool = False
    hits: list[K054539Hit] = field(default_factory=list)

    @property
    def width(self) -> int:
        return 2 if self.depth == 16 else 1

    @property
    def length(self) -> int:
        return len(self.data) // self.width

    def mode_delta(self) -> int:
        values = Counter(
            delta for h in self.hits if (delta := hit_played_delta(h))
        )
        if not values:
            return 0
        return values.most_common(1)[0][0]

    def c4_rate(self, clock: int) -> int:
        return max(1000, int(round(k054539_rate(clock, self.mode_delta()))))


@dataclass
class _K054539State:
    regs: list[int] = field(default_factory=lambda: [0] * 0x230)
    pos_latch: list[list[int]] = field(
        default_factory=lambda: [[0, 0, 0] for _ in range(K054539_VOICES)]
    )
    keyed: list[bool] = field(default_factory=lambda: [False] * K054539_VOICES)
    active: list[K054539Hit | None] = field(
        default_factory=lambda: [None] * K054539_VOICES
    )
    key_last: int = 0
    enabled: bool = False

    def regupdate(self) -> bool:
        return not (self.regs[0x22F] & 0x80)

    def pcm_enabled(self) -> bool:
        return bool(self.regs[0x22F] & 0x01)


def _start_addr(regs: list[int], ch: int) -> int:
    b = ch * 0x20
    return regs[b + 0x0C] | (regs[b + 0x0D] << 8) | (regs[b + 0x0E] << 16)


def _loop_addr(regs: list[int], ch: int) -> int:
    b = ch * 0x20
    return regs[b + 0x08] | (regs[b + 0x09] << 8) | (regs[b + 0x0A] << 16)


def _channel_regs(regs: list[int], ch: int) -> list[int]:
    return regs[ch * 0x20:ch * 0x20 + 0x20]


def _find_marker(rom: bytes, start: int, fmt: int) -> int:
    """First end-marker address at or after start, or -1."""
    n = len(rom)
    if start >= n:
        return -1
    if fmt == FORMAT_8BIT:
        idx = rom.find(b"\x80", start, min(n, start + K054539_MAX_BODY))
        return idx
    if fmt == FORMAT_16BIT:
        end = min(n - 1, start + K054539_MAX_BODY)
        for i in range(start, end, 2):
            if rom[i] == 0x00 and rom[i + 1] == 0x80:
                return i
        return -1
    if fmt == FORMAT_DPCM:
        idx = rom.find(b"\x88", start, min(n, start + K054539_MAX_BODY))
        return idx
    return -1


def _decode_dpcm(rom: bytes, start: int, end: int) -> bytes:
    out = bytearray()
    acc = 0
    for i in range(start, end):
        byte = rom[i]
        for nib in (byte & 0x0F, byte >> 4):
            acc += DPCM_STEP[nib] << 8
            acc = max(-32768, min(32767, acc))
            out += struct.pack("<h", acc)
    return bytes(out)


def _body(
    rom: bytes, hit: K054539Hit, warnings: list[str], chip_id: int,
) -> tuple[bytes, int, int | None, int | None] | None:
    start = hit.start
    if start < 0 or start >= len(rom):
        return None
    marker = _find_marker(rom, start, hit.format)
    if marker < 0:
        marker = min(len(rom), start + K054539_MAX_BODY)
        warnings.append(
            f"K054539 {chip_id}: sample at {start:06x} has no end marker; "
            "the body was capped"
        )
    if hit.format == FORMAT_8BIT:
        if marker <= start:
            return None
        data = bytes(rom[start:marker])
        depth = 8
        frames = len(data)
        loop_start = hit.loop - start if hit.loop_on and start <= hit.loop < marker else None
        loop_end = frames if loop_start is not None else None
    elif hit.format == FORMAT_16BIT:
        if marker - start < 2:
            return None
        marker -= (marker - start) & 1
        data = bytes(rom[start:marker])
        depth = 16
        frames = len(data) // 2
        loop_start = (
            (hit.loop - start) // 2
            if hit.loop_on and start <= hit.loop < marker else None
        )
        loop_end = frames if loop_start is not None else None
    elif hit.format == FORMAT_DPCM:
        if marker <= start:
            return None
        data = _decode_dpcm(rom, start, marker)
        depth = 16
        frames = len(data) // 2
        loop_start = (
            (hit.loop - start) * 2
            if hit.loop_on and start <= hit.loop < marker else None
        )
        loop_end = frames if loop_start is not None else None
    else:
        return None
    if hit.reverse:
        if depth == 16:
            pairs = [data[i:i + 2] for i in range(0, len(data), 2)]
            data = b"".join(reversed(pairs))
        else:
            data = bytes(reversed(data))
        if loop_start is not None and loop_end is not None:
            loop_start, loop_end = frames - loop_end, frames - loop_start
    return data, depth, loop_start, loop_end


def _open_hit(
    st: _K054539State, vgm: VgmFile, cid: int, ch: int, when: int,
    hits: list[K054539Hit], warnings: list[str],
) -> None:
    regs = st.regs
    b = ch * 0x20
    base2 = 0x200 + ch * 2
    fmt = (regs[base2] >> 2) & 0x03
    reverse = bool(regs[base2] & 0x20)
    loop_on = bool(regs[base2 + 1] & 0x01)
    start = _start_addr(regs, ch)
    loop = _loop_addr(regs, ch)
    delta = (
        regs[b] | (regs[b + 1] << 8) | (regs[b + 2] << 16)
    )
    hit = K054539Hit(
        sample_time=when,
        chip_id=cid,
        voice=ch,
        start=start,
        loop=loop,
        loop_on=loop_on,
        format=fmt,
        reverse=reverse,
        delta=delta,
        vol=regs[b + 0x03],
        pan=regs[b + 0x05],
        reverb=regs[b + 0x04],
    )
    if st.active[ch] is not None:
        st.active[ch].off_sample = when
    hits.append(hit)
    st.active[ch] = hit
    st.keyed[ch] = True
    if hit.reverb:
        warnings.append(
            f"K054539 {cid}: reverb send is not representable on Generic PCM DAC"
        )


def collect_k054539(
    vgm: VgmFile,
) -> tuple[list[K054539Sample], list[K054539Off], list[str]]:
    ids = sorted({w.chip_id for w in vgm.writes if w.chip == "k054539"})
    roms = {
        cid: assemble_rom(vgm, "k054539", cid, fill=0xFF)
        for cid in ids
    }
    warnings: list[str] = []
    offs: list[K054539Off] = []
    hits: list[K054539Hit] = []
    states = {cid: _K054539State() for cid in ids}
    vol_last: dict[tuple[int, int], tuple[int, int]] = {}
    freq_last: dict[tuple[int, int], int] = {}

    def close(cid: int, ch: int, when: int) -> None:
        st = states[cid]
        if st.active[ch] is not None:
            st.active[ch].off_sample = when
            st.active[ch] = None
        st.keyed[ch] = False
        offs.append(K054539Off(when, cid, ch))

    def keyon(cid: int, ch: int, when: int) -> None:
        st = states[cid]
        if not st.regupdate():
            return
        if st.pcm_enabled():
            for r in range(3):
                st.regs[ch * 0x20 + 0x0C + r] = st.pos_latch[ch][r]
        st.keyed[ch] = True
        if st.pcm_enabled():
            _open_hit(st, vgm, cid, ch, when, hits, warnings)
            b = ch * 0x20
            vol_last[(cid, ch)] = (st.regs[b + 0x03], st.regs[b + 0x05])
            freq_last[(cid, ch)] = (
                st.regs[b] | (st.regs[b + 1] << 8) | (st.regs[b + 2] << 16)
            )

    def keyoff(cid: int, ch: int, when: int) -> None:
        st = states[cid]
        if not st.regupdate():
            return
        if st.keyed[ch] or st.active[ch] is not None:
            close(cid, ch, when)

    for w in vgm.writes:
        if w.chip != "k054539":
            continue
        cid = w.chip_id
        st = states[cid]
        off = w.reg & 0x7FFF
        val = w.val & 0xFF
        if off == 0x214:
            # The core's key-on bits are processed before the register is
            # stored, but 0x214 is not a field the PCM update reads.
            st.regs[off] = val
            for ch in range(K054539_VOICES):
                if val & (1 << ch):
                    keyon(cid, ch, w.sample)
            continue
        if off == 0x215:
            st.regs[off] = val
            for ch in range(K054539_VOICES):
                if val & (1 << ch):
                    keyoff(cid, ch, w.sample)
            continue
        if off == 0x22F:
            old_enabled = st.enabled
            st.regs[off] = val
            st.enabled = bool(val & 0x01)
            if st.enabled and not old_enabled:
                for ch in range(K054539_VOICES):
                    if st.keyed[ch] and st.active[ch] is None:
                        _open_hit(st, vgm, cid, ch, w.sample, hits, warnings)
            elif not st.enabled and old_enabled:
                for ch in range(K054539_VOICES):
                    if st.active[ch] is not None:
                        close(cid, ch, w.sample)
            continue
        st.regs[off] = val
        if 0x00 <= off < 0x100:
            ch = off >> 5
            r = off & 0x1F
            if st.pcm_enabled() and 0x0C <= r <= 0x0E:
                # libvgm forces UPDATE_AT_KEYON on.  A start write is latched
                # and copied at the next key-on while PCM is enabled.
                st.pos_latch[ch][r - 0x0C] = val
                continue
            hit = st.active[ch]
            if hit is not None and st.keyed[ch]:
                b = ch * 0x20
                if r == 0x03:
                    pair = (st.regs[b + 0x03], st.regs[b + 0x05])
                    if pair != vol_last.get((cid, ch)):
                        hit.vol_pts.append((w.sample, pair[0], pair[1]))
                        vol_last[(cid, ch)] = pair
                elif r == 0x05:
                    pair = (st.regs[b + 0x03], st.regs[b + 0x05])
                    if pair != vol_last.get((cid, ch)):
                        hit.vol_pts.append((w.sample, pair[0], pair[1]))
                        vol_last[(cid, ch)] = pair
                elif r in (0x00, 0x01, 0x02):
                    delta = (
                        st.regs[b] | (st.regs[b + 1] << 8)
                        | (st.regs[b + 2] << 16)
                    )
                    if delta != freq_last.get((cid, ch)):
                        hit.freq_pts.append((w.sample, delta))
                        freq_last[(cid, ch)] = delta

    # A key-on that is closed at the same VGM sample produces no audio: both
    # register writes take effect before the next hardware tick.  Some logs
    # start with a full key-on/key-off mask pair to reset every voice; those
    # zero-length hits must not become samples or notes.
    hits = [
        h for h in hits
        if not (h.off_sample and h.off_sample <= h.sample_time)
    ]
    if not hits:
        return [], offs, warnings
    if not any(roms.values()):
        warnings.append(
            "no K054539 ROM block (0x8C); PCM notes will be silent until "
            "samples are added"
        )
        return [], offs, warnings

    by_key: dict[tuple, K054539Sample] = {}
    undumped = 0
    for h in hits:
        rom = roms.get(h.chip_id, b"")
        if not rom:
            continue
        marker = _find_marker(rom, h.start, h.format)
        end = marker if marker >= 0 else min(len(rom), h.start + K054539_MAX_BODY)
        if h.start >= len(rom) or not _covered(vgm, h.chip_id, h.start):
            undumped += 1
            continue
        if end <= h.start:
            continue
        key = (
            h.chip_id, h.start, end, h.loop if h.loop_on else -1,
            h.format, h.reverse,
        )
        smp = by_key.get(key)
        if smp is None:
            body = _body(rom, h, warnings, h.chip_id)
            if body is None:
                continue
            data, depth, loop_start, loop_end = body
            smp = K054539Sample(
                start=h.start,
                end=end,
                data=data,
                depth=depth,
                loop_start=loop_start,
                loop_end=loop_end,
                reverse=h.reverse,
            )
            by_key[key] = smp
        smp.hits.append(h)
    if undumped:
        warnings.append(
            f"{undumped} K054539 triggers point at ROM that the VGM does not "
            "dump; those notes are silent"
        )
    samples = sorted(
        by_key.values(), key=lambda s: (s.start, s.loop_start or -1, s.depth)
    )
    # A file with several distinct send values would otherwise repeat the
    # same warning on every key-on.
    warnings[:] = list(dict.fromkeys(warnings))
    return samples, offs, warnings
