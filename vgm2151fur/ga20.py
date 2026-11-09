"""Irem GA20 ROM -> signed 8-bit sample bodies for Furnace.

The GA20 is a four-voice sample player.  Each voice has a 20-bit start and
end address (both carry the low nibble zero), an 8-bit frequency register and
an 8-bit volume register.  Playback advances until the ROM byte is zero; the
end register is a bound rather than a loop point.  Furnace's GA20 platform
re-packs the bodies and appends its own terminator, so this collector only
extracts the audible span.

VGM writes command 0xBF as (register bit 7 selects the second chip,
register, data).  The clock is header 0xE0 for VGM 1.71+ and the ROM is data
block 0x93.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from vgm2151fur.vgm import GA20_DEFAULT_CLOCK, VgmFile, assemble_rom

GA20_VOICES = 4
GA20_MAX_BODY = 1_048_576
# Furnace's GA20 pitch table plays note 108 at this period (register 0x95 for
# a COLOR_NTSC clock).  The platform ignores a sample's center rate, so every
# GA20 body uses the same C-4 reference.
GA20_C4_DIVISOR = 428


def ga20_rate(clock: int, freq: int) -> float:
    """Playback rate for one GA20 frequency register value."""
    divider = (0x100 - (freq & 0xFF)) * 4
    if divider <= 0:
        return 0.0
    return (clock or GA20_DEFAULT_CLOCK) / divider


def ga20_c4_rate(clock: int) -> int:
    """Furnace's GA20 playback rate at note 108 (C-4)."""
    return max(1000, int(round((clock or GA20_DEFAULT_CLOCK) / GA20_C4_DIVISOR)))


def _ga20_pitch_table(clock: int) -> tuple[list[int], list[int], int]:
    """The 13-entry period table Furnace builds for the GA20 clock."""
    tuning = 440.0
    clock = clock or GA20_DEFAULT_CLOCK
    shift = 0
    while shift < 14:
        nbase = (shift - 4) * 12
        fbase = (tuning * 0.0625) * 2.0 ** ((nbase + 3) / 12.0)
        if round((clock / fbase) / 64.0) <= 0x100:
            break
        shift += 1
    pitch: list[int] = []
    for i in range(13):
        nbase = i + (shift - 5) * 12
        fbase = (tuning * 0.0625) * 2.0 ** ((nbase + 3) / 12.0)
        pitch.append(round((clock / fbase) / 64.0))
    diff = [pitch[i + 1] - pitch[i] for i in range(12)] + [0]
    return pitch, diff, shift


def _ga20_period(
    note: int, fine: int, pitch: list[int], diff: list[int], shift: int,
) -> int:
    offset = (note << 7) + fine
    coarse = max(0, offset >> 7)
    frac = offset & 127
    index = coarse % 12
    octave = (coarse // 12) - shift
    root = pitch[index]
    delta = diff[index]
    if octave > 0:
        root = (root + (1 << (octave - 1))) >> octave
        delta = (delta + (1 << (octave - 1))) >> octave
    elif octave < 0:
        return 0x100
    return root + ((delta * frac) >> 7)


@lru_cache(maxsize=8)
def _ga20_note_map(clock: int) -> dict[int, tuple[int, int]]:
    """period -> (note, E5) for every reachable GA20 pitch value."""
    pitch, diff, shift = _ga20_pitch_table(clock)
    out: dict[int, tuple[int, int]] = {}
    for note in range(180):
        for e5 in range(256):
            period = _ga20_period(note, e5 - 0x80, pitch, diff, shift)
            if 1 <= period <= 0x100 and period not in out:
                out[period] = (note, e5)
    return out


@dataclass
class Ga20Hit:
    sample_time: int
    chip_id: int
    voice: int
    start: int
    end: int
    freq: int
    volume: int
    off_sample: int = 0
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)

    def played_rate(self, clock: int) -> float:
        # The key-on value supplies the note; later writes are pitch motion.
        return ga20_rate(clock, self.freq)


@dataclass
class Ga20Off:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class Ga20Sample:
    chip_id: int
    start: int
    data: bytes
    hits: list[Ga20Hit] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.data)

    def mode_freq(self) -> int:
        freqs = Counter(
            h.freq for h in self.hits if h.freq
        )
        return freqs.most_common(1)[0][0] if freqs else 0

    def c4_rate(self, clock: int) -> int:
        # Furnace's GA20 pitch table does not depend on the body's center
        # rate; the writer stores the platform's C-4 reference for every body.
        return ga20_c4_rate(clock)


def _covered(vgm: VgmFile, chip_id: int, addr: int) -> bool:
    for s in vgm.roms:
        if s.chip == chip_id and s.kind == "ga20" and s.data:
            if s.start <= addr < s.start + len(s.data):
                return True
    return False


def _body(rom: bytes, start: int, end: int, warnings: list[str], cid: int) -> bytes:
    # MAME/libvgm stop on the zero byte; the end register is not used during
    # playback (games routinely write it past the real sample end).
    stop = rom.find(b"\x00", start, min(len(rom), start + GA20_MAX_BODY))
    if stop < 0:
        warnings.append(
            f"GA20 {cid}: sample at {start:05x} has no zero terminator; "
            "the body was capped"
        )
        stop = min(len(rom), start + GA20_MAX_BODY)
        if end > start:
            stop = min(stop, end)
    if stop <= start:
        return b""
    # The ROM stores unsigned 8-bit samples with 0x80 as silence.  Furnace's
    # SMP2 body is signed; its GA20 uploader XORs 0x80 again.
    return bytes((value - 0x80) & 0xFF for value in rom[start:stop])


def collect_ga20(vgm: VgmFile) -> tuple[list[Ga20Sample], list[Ga20Off], list[str]]:
    """Collect GA20 hits and sample bodies from a parsed VGM."""
    ids = sorted({w.chip_id for w in vgm.writes if w.chip == "ga20"})
    warnings: list[str] = []
    offs: list[Ga20Off] = []
    if not ids:
        return [], offs, warnings
    roms = {
        cid: assemble_rom(
            vgm, "ga20", cid, fill=0xFF, declared_size=False,
        )
        for cid in ids
    }
    if not any(roms.values()):
        warnings.append(
            "no GA20 ROM block (0x93); PCM notes will be silent until "
            "samples are added"
        )
        return [], offs, warnings

    regs = [[0] * 8 for _ in range(GA20_VOICES)]
    keyed = [False] * GA20_VOICES
    active: list[Ga20Hit | None] = [None] * GA20_VOICES
    hits: list[Ga20Hit] = []
    for w in vgm.writes:
        if w.chip != "ga20":
            continue
        voice = w.reg >> 3
        reg = w.reg & 0x07
        if not 0 <= voice < GA20_VOICES:
            continue
        val = w.val & 0xFF
        regs[voice][reg] = val
        if reg == 0x06:
            if val & 0x02:
                if active[voice] is not None:
                    active[voice].off_sample = w.sample
                hit = Ga20Hit(
                    sample_time=w.sample,
                    chip_id=w.chip_id,
                    voice=voice,
                    start=((regs[voice][0x00] | (regs[voice][0x01] << 8)) << 4),
                    end=((regs[voice][0x02] | (regs[voice][0x03] << 8)) << 4),
                    freq=regs[voice][0x04],
                    volume=regs[voice][0x05],
                )
                hits.append(hit)
                active[voice] = hit
                keyed[voice] = True
            else:
                if keyed[voice]:
                    offs.append(Ga20Off(w.sample, w.chip_id, voice))
                    if active[voice] is not None:
                        active[voice].off_sample = w.sample
                        active[voice] = None
                keyed[voice] = False
            continue
        hit = active[voice]
        if not keyed[voice] or hit is None:
            continue
        if reg == 0x04 and val != hit.freq:
            hit.freq_pts.append((w.sample, val))
        elif reg == 0x05 and val != hit.volume:
            hit.vol_pts.append((w.sample, val))

    by_key: dict[tuple, Ga20Sample] = {}
    undumped = 0
    for h in hits:
        rom = roms.get(h.chip_id, b"")
        if not rom or h.start >= len(rom) or not _covered(
            vgm, h.chip_id, h.start
        ):
            undumped += 1
            continue
        body = _body(rom, h.start, h.end, warnings, h.chip_id)
        if not body:
            continue
        key = (h.chip_id, h.start, body)
        smp = by_key.get(key)
        if smp is None:
            smp = Ga20Sample(
                chip_id=h.chip_id, start=h.start, data=body,
            )
            by_key[key] = smp
        smp.hits.append(h)
    if undumped:
        warnings.append(
            f"{undumped} GA20 triggers point at ROM that the VGM does not "
            "dump; those notes are silent"
        )
    samples = sorted(
        by_key.values(), key=lambda s: (s.chip_id, s.start),
    )
    warnings[:] = list(dict.fromkeys(warnings))
    return samples, offs, warnings


def ga20_note(hit: Ga20Hit, sample: Ga20Sample, clock: int) -> tuple[int, int]:
    """(Furnace note, E5) that makes the platform write the source register.

    Furnace's GA20 period table is coarser than equal temperament, so a simple
    semitone is not enough.  The source frequency register itself gives the
    period Furnace must load (``0x100 - freq``); the map returns the closest
    reachable note/E5 pair.
    """
    target = 0x100 - (hit.freq & 0xFF)
    table = _ga20_note_map(clock)
    if target in table:
        return table[target]
    nearest = min(table, key=lambda period: abs(period - target))
    return table[nearest]
