"""Konami K053260 ROM -> samples -> Furnace.

Register model follows Furnace's vgsound_emu core (the core the module
plays on). Four voices, eight registers each at 0x08 + voice*8:

    0/1 pitch (12-bit)   2/3 length (16-bit)   4/5/6 start (21-bit)
    7 volume (7-bit)

0x28 key on/off in bits 0-3 and reverse in bits 4-7; 0x2A loop in bits 0-3
and KADPCM in bits 4-7; 0x2C/0x2D pan, three bits per voice.

The chip reads ``clock / (4096 - pitch)`` source steps a second. A PCM step
plays one byte; an ADPCM step plays one nibble from the byte fetched every
two steps. Key-on pre-increments the address. The byte at ``start`` is never
played. A loop resets the address to ``start`` and replays the whole
``length`` again: there is no separate loop point.

KADPCM is the 16-entry delta table, low nibble first. Furnace stores it as
sample depth 7 and decodes the same way. A sample that loops is marked
loopable with the loop covering the whole body, which is all the chip can do.
Reverse playback is stored as a forward body, which keeps Furnace's sample
direction forward.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, VgmFile, assemble_rom

K053260_VOICES = 4
K053260_DEFAULT_CLOCK = 3579545
K053260_REFIT_SAMPLES = 735
K053260_MAX_ADDR = 0x1FFFFF
K053260_DEFAULT_PAN = 4

KADPCM = (0, 1, 2, 4, 8, 16, 32, 64, -128, -64, -32, -16, -8, -4, -2, -1)
PAN_LUT = (
    (0x00, 0x00),
    (0x7F, 0x00),
    (0x74, 0x34),
    (0x68, 0x49),
    (0x5A, 0x5A),
    (0x49, 0x68),
    (0x34, 0x74),
    (0x00, 0x7F),
)


def k053260_rate(clock: int, pitch: int) -> float:
    """Source steps a second for a 12-bit pitch register."""
    period = 4096 - (pitch & 0xFFF)
    if period <= 0:
        period = 1
    return (clock or K053260_DEFAULT_CLOCK) / period


def k053260_pan_fx(pan: int) -> int:
    """Chip pan position -> Furnace 08xy split pan (15 = loud side)."""
    left, right = PAN_LUT[pan & 7]
    peak = max(left, right)
    if peak <= 0:
        return 0
    lv = (left * 15 + peak // 2) // peak
    rv = (right * 15 + peak // 2) // peak
    return ((lv & 15) << 4) | (rv & 15)


def merge_step_writes(pts: list[tuple[int, int]], gap: int = 16) -> list[tuple[int, int]]:
    """Keep the last write of each cluster closer than ``gap`` samples."""
    merged: list[tuple[int, int]] = []
    for p in pts:
        if merged and p[0] - merged[-1][0] <= gap:
            merged[-1] = p
        else:
            merged.append(p)
    return merged


def hit_played_pitch(hit: "K053260Hit") -> int:
    """Pitch the hit actually plays at.

    A pitch write just after key-on is the note's own pitch, not a bend: some
    drivers key the voice before its registers are set. The first write inside
    the frame replaces a stale key-on value.
    """
    steps = merge_step_writes(hit.freq_pts)
    if steps and steps[0][1] and (
        steps[0][0] - hit.sample_time <= K053260_REFIT_SAMPLES
        or not (hit.pitch & 0xFFF)
    ):
        return steps[0][1]
    return hit.pitch & 0xFFF


def hit_played_volume(hit: "K053260Hit") -> tuple[int, int]:
    """(volume, pan) at the note attack, using the first in-frame write."""
    vol, pan = hit.volume, hit.pan
    for t, v, p in hit.state_pts:
        if t - hit.sample_time > K053260_REFIT_SAMPLES:
            break
        vol, pan = v, p
    return vol, pan


def hit_played_steps(hit: "K053260Hit", end_sample: int, clock: int) -> float:
    """Source steps the voice advances from key-on until ``end_sample``."""
    if end_sample <= hit.sample_time:
        return 0.0
    freq = hit.pitch & 0xFFF
    prev = hit.sample_time
    total = 0.0
    for t, f in hit.freq_pts:
        if t <= prev:
            freq = f
            continue
        if t >= end_sample:
            break
        total += k053260_rate(clock, freq) * (t - prev) / VGM_RATE
        freq = f
        prev = t
    total += k053260_rate(clock, freq) * (end_sample - prev) / VGM_RATE
    return total


@dataclass
class K053260Hit:
    sample_time: int
    chip_id: int
    voice: int
    start: int
    length: int
    pitch: int
    volume: int
    pan: int
    loop: bool
    adpcm: bool
    reverse: bool
    off_sample: int = 0
    # Changes while keyed: (sample, volume, pan). The attack values are not
    # repeated here.
    state_pts: list[tuple[int, int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    last_state: tuple[int, int] = (0, K053260_DEFAULT_PAN)


@dataclass
class K053260Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class K053260Sample:
    start: int
    data: bytes
    hits: list[K053260Hit] = field(default_factory=list)
    loop_start: int | None = None
    loop_end: int | None = None
    depth: int = 8  # 8 = PCM, 7 = K053260 ADPCM
    reverse: bool = False

    @property
    def length(self) -> int:
        """Played frames: one per PCM byte, two per ADPCM byte."""
        return len(self.data) * 2 if self.depth == 7 else len(self.data)

    def mode_pitch(self) -> int:
        if not self.hits:
            return 0
        freqs = Counter(f for h in self.hits if (f := hit_played_pitch(h)))
        if not freqs:
            return 0
        return freqs.most_common(1)[0][0]

    def c4_rate(self, clock: int) -> int:
        return max(1000, int(round(k053260_rate(clock, self.mode_pitch()))))


def _covered(vgm: VgmFile, chip_id: int, addr: int) -> bool:
    """True when the masked address falls inside a dumped ROM slice."""
    addr &= K053260_MAX_ADDR
    for s in vgm.roms:
        if s.chip == chip_id and s.kind == "k053260" and s.data:
            if s.start <= addr < s.start + len(s.data):
                return True
    return False


def _read(rom: bytes, addr: int, n: int) -> bytes:
    """``n`` ROM bytes from a 21-bit wrapping address; gaps read as zero."""
    mask = K053260_MAX_ADDR
    out = bytearray(n)
    for i in range(n):
        a = (addr + i) & mask
        if a < len(rom):
            out[i] = rom[a]
    return bytes(out)


def _trim(data: bytes, floor: int) -> bytes:
    """Drop trailing silent bytes, never below ``floor``."""
    end = len(data)
    while end > floor and data[end - 1] == 0:
        end -= 1
    return data[:end]


def _blank_state() -> dict:
    return {
        "reg": [[0] * 8 for _ in range(K053260_VOICES)],
        "key": 0,
        "loop": 0,
        "adpcm": 0,
        "pan": [K053260_DEFAULT_PAN] * K053260_VOICES,
        # Logs that never write 0x2F still play; honor bit 1 once it appears.
        "ctrl": 0x02,
        "active": [None] * K053260_VOICES,
        "keyed": [False] * K053260_VOICES,
        "rev": [False] * K053260_VOICES,
    }


def collect_k053260(
    vgm: VgmFile,
) -> tuple[bytes, list[K053260Sample], list[K053260Off], list[str]]:
    """One body per distinct (start, length, ADPCM, direction, loop) keyed."""
    roms = {
        cid: assemble_rom(vgm, "k053260", cid)
        for cid in sorted({w.chip_id for w in vgm.writes if w.chip == "k053260"})
    }
    warnings: list[str] = []
    offs: list[K053260Off] = []
    acc: dict[str, int] = {}
    if not roms:
        return b"", [], offs, warnings

    chips: dict[int, dict] = {}
    hits: list[K053260Hit] = []

    def state(cid: int) -> dict:
        st = chips.get(cid)
        if st is None:
            st = _blank_state()
            chips[cid] = st
        return st

    def keyed_off_at(index: int, chip_id: int, voice: int) -> bool:
        """True when the last key write at this sample clears ``voice``.

        Drivers often rewrite 0x2A and stop the voice in the same tick. A mode
        change there never reaches the output.
        """
        t = vgm.writes[index].sample
        last = None
        j = index + 1
        while j < len(vgm.writes) and vgm.writes[j].sample == t:
            w2 = vgm.writes[j]
            if (
                w2.chip == "k053260"
                and w2.chip_id == chip_id
                and w2.reg == 0x28
            ):
                last = w2.val
            j += 1
        return last is not None and not ((last >> voice) & 1)

    for wi, w in enumerate(vgm.writes):
        if w.chip != "k053260":
            continue
        st = state(w.chip_id)
        reg = st["reg"]
        pan = st["pan"]
        addr = w.reg & 0x3F
        val = w.val & 0xFF
        if addr < 0x08:
            continue
        if addr < 0x28:
            voice = (addr - 0x08) >> 3
            r = (addr - 0x08) & 7
            prev = reg[voice][r]
            reg[voice][r] = val
            hit = st["active"][voice]
            if hit is None or not st["keyed"][voice] or prev == val:
                continue
            if r == 7:
                vol = val & 0x7F
                if (vol, pan[voice]) != hit.last_state:
                    hit.state_pts.append((w.sample, vol, pan[voice]))
                    hit.last_state = (vol, pan[voice])
            elif r in (0, 1):
                pitch = (reg[voice][0] | ((reg[voice][1] & 0x0F) << 8)) & 0xFFF
                if not hit.freq_pts or hit.freq_pts[-1][1] != pitch:
                    hit.freq_pts.append((w.sample, pitch))
            elif r in (2, 3, 4, 5, 6) and hit.loop:
                # A running loop reloads the live start/length at the wrap. A
                # Furnace sample cannot move its loop. The key-on values win.
                acc["geometry_loop"] = acc.get("geometry_loop", 0) + 1
            continue
        if addr == 0x28:
            prev = st["key"]
            st["key"] = val
            for voice in range(K053260_VOICES):
                up = (val >> voice) & 1
                was = (prev >> voice) & 1
                reverse = bool(val & (0x10 << voice))
                if up and not was:
                    if st["active"][voice] is not None:
                        st["active"][voice].off_sample = w.sample
                        offs.append(K053260Off(w.sample, w.chip_id, voice))
                    st["keyed"][voice] = True
                    if not (st["ctrl"] & 0x02):
                        acc["disabled"] = acc.get("disabled", 0) + 1
                        st["active"][voice] = None
                        continue
                    volume = reg[voice][7] & 0x7F
                    hit = K053260Hit(
                        sample_time=w.sample,
                        chip_id=w.chip_id,
                        voice=voice,
                        start=(
                            reg[voice][4]
                            | (reg[voice][5] << 8)
                            | ((reg[voice][6] & 0x1F) << 16)
                        ),
                        length=reg[voice][2] | (reg[voice][3] << 8),
                        pitch=(
                            reg[voice][0] | ((reg[voice][1] & 0x0F) << 8)
                        ) & 0xFFF,
                        volume=volume,
                        pan=pan[voice],
                        loop=bool(st["loop"] & (1 << voice)),
                        adpcm=bool(st["adpcm"] & (1 << voice)),
                        reverse=reverse,
                        last_state=(volume, pan[voice]),
                    )
                    hits.append(hit)
                    st["active"][voice] = hit
                elif not up and was:
                    if st["active"][voice] is not None:
                        st["active"][voice].off_sample = w.sample
                        offs.append(K053260Off(w.sample, w.chip_id, voice))
                        st["active"][voice] = None
                    st["keyed"][voice] = False
                elif up and was and reverse != st["rev"][voice]:
                    acc["direction"] = acc.get("direction", 0) + 1
                st["rev"][voice] = reverse
            continue
        if addr == 0x2A:
            prev_loop, prev_adpcm = st["loop"], st["adpcm"]
            st["loop"] = val & 0x0F
            st["adpcm"] = (val >> 4) & 0x0F
            for voice in range(K053260_VOICES):
                if st["active"][voice] is None:
                    continue
                if keyed_off_at(wi, w.chip_id, voice):
                    continue
                if bool(prev_adpcm & (1 << voice)) != bool(st["adpcm"] & (1 << voice)):
                    acc["adpcm_change"] = acc.get("adpcm_change", 0) + 1
                if bool(prev_loop & (1 << voice)) != bool(st["loop"] & (1 << voice)):
                    acc["loop_change"] = acc.get("loop_change", 0) + 1
            continue
        if addr in (0x2C, 0x2D):
            first = 0 if addr == 0x2C else 2
            pan[first] = val & 7
            pan[first + 1] = (val >> 3) & 7
            for voice in (first, first + 1):
                hit = st["active"][voice]
                if hit is None or not st["keyed"][voice]:
                    continue
                if (reg[voice][7] & 0x7F, pan[voice]) != hit.last_state:
                    hit.state_pts.append(
                        (w.sample, reg[voice][7] & 0x7F, pan[voice])
                    )
                    hit.last_state = (reg[voice][7] & 0x7F, pan[voice])
            continue
        if addr == 0x2F:
            st["ctrl"] = val
            continue

    if not hits:
        _flush(acc, warnings)
        return b"", [], offs, warnings
    if not any(roms.values()):
        warnings.append(
            "no K053260 ROM block (0x8E); samples are silent until they are added"
        )
        _flush(acc, warnings)
        return b"", [], offs, warnings

    by_key: dict[tuple, K053260Sample] = {}
    for h in hits:
        if h.length <= 0:
            # The core returns before reading ROM when the length is zero.
            continue
        rom = roms.get(h.chip_id, b"")
        if not rom:
            acc["undumped"] = acc.get("undumped", 0) + 1
            continue
        first = (h.start - 1) & K053260_MAX_ADDR if h.reverse else (
            h.start + 1
        ) & K053260_MAX_ADDR
        if not _covered(vgm, h.chip_id, first):
            acc["undumped"] = acc.get("undumped", 0) + 1
            continue
        if h.reverse:
            raw = _read(rom, h.start - h.length, h.length)[::-1]
        else:
            raw = _read(rom, h.start + 1, h.length)
        if h.adpcm and h.reverse:
            raw = bytes(((b & 0x0F) << 4) | (b >> 4) for b in raw)
        key = (
            h.chip_id, h.start, h.length, h.adpcm, h.reverse, h.loop,
        )
        smp = by_key.get(key)
        if smp is None:
            # A loop covers the programmed length. KADPCM zero nibbles hold the
            # accumulator. Its length also matters. Trim only PCM one-shots.
            body = raw if (h.loop or h.adpcm) else _trim(raw, 2)
            if not body:
                continue
            frames = len(body) * 2 if h.adpcm else len(body)
            loop_start = 0 if h.loop else None
            loop_end = frames if h.loop else None
            smp = K053260Sample(
                start=h.start,
                data=body,
                loop_start=loop_start,
                loop_end=loop_end,
                depth=7 if h.adpcm else 8,
                reverse=h.reverse,
            )
            by_key[key] = smp
        smp.hits.append(h)

    _flush(acc, warnings)
    samples = sorted(
        by_key.values(),
        key=lambda s: (
            s.start, s.depth, s.reverse,
            -1 if s.loop_start is None else s.loop_start,
        ),
    )
    return roms.get(0, next(iter(roms.values()), b"")), samples, offs, warnings


def _flush(acc: dict[str, int], warnings: list[str]) -> None:
    if acc.get("undumped"):
        warnings.append(
            f"{acc['undumped']} K053260 triggers point at ROM that the VGM "
            "does not dump; those notes are silent"
        )
    if acc.get("disabled"):
        warnings.append(
            f"{acc['disabled']} K053260 key-ons happened while sound output "
            "was disabled; those notes are silent"
        )
    if acc.get("adpcm_change"):
        warnings.append(
            f"{acc['adpcm_change']} K053260 KADPCM mode changes while a voice "
            "was playing are not converted"
        )
    if acc.get("loop_change"):
        warnings.append(
            f"{acc['loop_change']} K053260 loop changes while a voice was "
            "playing are not converted; the key-on mode is used"
        )
    if acc.get("direction"):
        warnings.append(
            f"{acc['direction']} K053260 direction changes while a voice was "
            "playing are not converted; the key-on direction is used"
        )
    if acc.get("geometry_loop"):
        warnings.append(
            f"{acc['geometry_loop']} K053260 start/length writes while a "
            "looping voice was playing were ignored (the key-on values are used)"
        )
