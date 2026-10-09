"""K007232 ROM → 7-bit PCM samples → WAV + JSON."""

from __future__ import annotations

import json
import math
import struct
import wave
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from vgm2151fur.vgm import VgmFile, assemble_k007232_rom, K007232_TRIGGER_REG


def k007232_rate(clock: int, pitch: int) -> float:
    """Hardware playback rate in Hz for a 12-bit pitch register.

    Matches Furnace/MAME: chipClock/4 / (4096 - pitch).
    """
    period = 4096 - (pitch & 0xFFF)
    if period <= 0:
        period = 1
    return (clock / 4.0) / period


def decode_7bit_pcm(rom: bytes, start: int) -> bytes:
    """Bytes from start until (and not including) the first byte with bit 7 set.

    Hardware stops on bit 7; that marker byte is not audible.
    """
    if start < 0 or start >= len(rom):
        return b""
    out = bytearray()
    for i in range(start, len(rom)):
        b = rom[i]
        if b & 0x80:
            break
        out.append(b)
    return bytes(out)


def pcm7_to_i16(data: bytes) -> bytes:
    """7-bit unsigned (0..127, center 64) → 16-bit signed little-endian."""
    out = bytearray(len(data) * 2)
    for i, b in enumerate(data):
        v = ((b & 0x7F) - 64) << 9
        struct.pack_into("<h", out, i * 2, max(-32768, min(32767, v)))
    return bytes(out)


def pcm7_to_s8(data: bytes) -> bytes:
    """7-bit unsigned → 8-bit signed (Furnace 8-bit PCM)."""
    return bytes(max(-128, min(127, ((b & 0x7F) - 64) * 2)) & 0xFF for b in data)


def frames_to_pcm7(frames: list[float]) -> bytes:
    """Float −1..1 → 7-bit unsigned (center 64)."""
    out = bytearray(len(frames))
    for i, v in enumerate(frames):
        out[i] = max(0, min(127, 64 + int(round(v * 64.0))))
    return bytes(out)


def scale_pcm7(data: bytes, gain: float) -> bytes:
    """Scale 7-bit unsigned PCM about center 64. gain=1 leaves the body as-is."""
    if abs(gain - 1.0) < 1e-6:
        return data
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = max(0, min(127, 64 + int(round(((b & 0x7F) - 64) * gain))))
    return bytes(out)


def write_wav(path: Path, pcm7: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = pcm7_to_i16(pcm7)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(max(1000, int(rate)))
        w.writeframes(payload)


def split_rom_on_end_markers(rom: bytes) -> list[tuple[int, bytes]]:
    """Every region that starts after an end marker (or at 0)."""
    samples: list[tuple[int, bytes]] = []
    i = 0
    n = len(rom)
    while i < n:
        while i < n and (rom[i] & 0x80):
            i += 1
        if i >= n:
            break
        start = i
        body = decode_7bit_pcm(rom, start)
        if body:
            samples.append((start, body))
            i = start + len(body) + 1
        else:
            i += 1
    return samples


@dataclass
class PcmHit:
    sample_time: int
    ch: int
    start: int
    pitch: int
    bank: int
    vol_l: int
    vol_r: int


@dataclass
class PcmSample:
    start: int
    pcm7: bytes
    hits: list[PcmHit] = field(default_factory=list)
    # Multiply hardware Hz (Furnace C-4 and the synth path).
    rate_scale: float = 1.0
    # If set, C-4 / pattern notes use this pitch instead of mode_pitch().
    c4_pitch: int | None = None
    # Linear amplitude correction against the recorded ROM body.
    volume_scale: float = 1.0

    @property
    def length(self) -> int:
        return len(self.pcm7)

    def mode_pitch(self) -> int:
        if not self.hits:
            return 4051
        return Counter(h.pitch for h in self.hits).most_common(1)[0][0]

    def effective_c4_pitch(self) -> int:
        return self.c4_pitch if self.c4_pitch is not None else self.mode_pitch()

    def c4_rate(self, clock: int) -> int:
        hz = k007232_rate(clock, self.effective_c4_pitch()) * self.rate_scale
        return max(1000, int(round(hz)))


def collect_pcm_hits(vgm: VgmFile) -> list[PcmHit]:
    ch_pitch = [0, 0]
    ch_start = [0, 0]
    ch_bank = [0, 0]
    vol = [0, 0, 0, 0]  # 0x10..0x13
    hits: list[PcmHit] = []
    for w in vgm.writes:
        if w.chip != "k007232" or w.chip_id != 0:
            continue
        r, v = w.reg, w.val
        if r == 0:
            ch_pitch[0] = (ch_pitch[0] & 0xF00) | v
        elif r == 1:
            ch_pitch[0] = (ch_pitch[0] & 0x0FF) | ((v & 0x0F) << 8)
        elif r == 2:
            ch_start[0] = (ch_start[0] & 0x1FF00) | v
        elif r == 3:
            ch_start[0] = (ch_start[0] & 0x100FF) | (v << 8)
        elif r == 4:
            ch_start[0] = (ch_start[0] & 0x0FFFF) | ((v & 1) << 16)
        elif r == 6:
            ch_pitch[1] = (ch_pitch[1] & 0xF00) | v
        elif r == 7:
            ch_pitch[1] = (ch_pitch[1] & 0x0FF) | ((v & 0x0F) << 8)
        elif r == 8:
            ch_start[1] = (ch_start[1] & 0x1FF00) | v
        elif r == 9:
            ch_start[1] = (ch_start[1] & 0x100FF) | (v << 8)
        elif r == 10:
            ch_start[1] = (ch_start[1] & 0x0FFFF) | ((v & 1) << 16)
        elif r == 0x14:
            ch_bank[0] = v
        elif r == 0x15:
            ch_bank[1] = v
        elif 0x10 <= r <= 0x13:
            vol[r - 0x10] = v
        elif r == K007232_TRIGGER_REG:
            ch = 1 if (v & 0x7F) == 11 else 0
            addr = (ch_start[ch] & 0x1FFFF) | ((ch_bank[ch] & 0xFF) << 17)
            hits.append(PcmHit(
                sample_time=w.sample,
                ch=ch,
                start=addr,
                pitch=ch_pitch[ch] & 0xFFF,
                bank=ch_bank[ch],
                vol_l=vol[ch * 2],
                vol_r=vol[ch * 2 + 1],
            ))
    return hits


def build_pcm_bank(vgm: VgmFile) -> tuple[bytes, list[PcmSample]]:
    rom = assemble_k007232_rom(vgm)
    hits = collect_pcm_hits(vgm)
    by_start: dict[int, PcmSample] = {}
    for h in hits:
        smp = by_start.get(h.start)
        if smp is None:
            body = decode_7bit_pcm(rom, h.start) if rom else b""
            smp = PcmSample(start=h.start, pcm7=body)
            by_start[h.start] = smp
        smp.hits.append(h)
    samples = sorted(by_start.values(), key=lambda s: s.start)
    return rom, samples


def dump_pcm(vgm: VgmFile, out_dir: Path, *, min_length: int = 8) -> dict:
    """Write one WAV per triggered start address plus a JSON index."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rom, samples = build_pcm_bank(vgm)
    clock = vgm.k007232_clock or 3579545
    index: dict = {
        "source": str(vgm.path),
        "clock": clock,
        "rom_size": len(rom),
        "samples": [],
    }
    for i, smp in enumerate(samples):
        if smp.length < min_length:
            continue
        rate = smp.c4_rate(clock)
        name = f"smp_{i:02d}_{smp.start:05x}.wav"
        write_wav(out_dir / name, smp.pcm7, rate)
        pitch_hist = Counter(h.pitch for h in smp.hits)
        index["samples"].append({
            "id": i,
            "file": name,
            "start": smp.start,
            "length": smp.length,
            "c4_rate": rate,
            "mode_pitch": smp.mode_pitch(),
            "triggers": len(smp.hits),
            "channels": sorted({h.ch for h in smp.hits}),
            "pitches": {str(k): v for k, v in pitch_hist.most_common()},
        })
    (out_dir / "pcm_index.json").write_text(
        json.dumps(index, indent=2) + "\n", encoding="utf-8"
    )
    return index


def pitch_to_note(pitch: int, c4_pitch: int, clock: int) -> tuple[int, int]:
    """Return (furnace_note, e5xx) relative to a sample whose C-4 is c4_pitch.

    Furnace C-4 = 108. E5 80 is center; 00 is -1 semitone, FF is almost +1.
    """
    r = k007232_rate(clock, pitch)
    r0 = k007232_rate(clock, c4_pitch)
    return rate_to_note(r, r0)


def rate_to_note(rate: float, ref_rate: float) -> tuple[int, int]:
    """(furnace_note, e5xx) for a playback rate relative to a sample's C-4."""
    if rate <= 0 or ref_rate <= 0:
        return 108, 0x80
    semis = 12.0 * math.log2(rate / ref_rate)
    note = int(round(semis))
    frac = semis - note
    furnace = max(0, min(179, 108 + note))
    e5 = max(0, min(255, int(round(0x80 + frac * 128))))
    return furnace, e5
