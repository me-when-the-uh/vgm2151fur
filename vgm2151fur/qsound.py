"""Capcom QSound: sixteen 8-bit PCM voices and three ADPCM voices.

VGM command ``0xC4`` carries a 16-bit big-endian payload followed by the
register number.  Data block ``0x8F`` is the sample ROM.  Furnace runs the
chip at a fixed 60 MHz; old 4 MHz rips describe the same chip.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, VgmFile

QSOUND_CLOCK = 60_000_000
# The DSP executes one machine cycle every 2*1248 clocks.
QSOUND_OUT_RATE = QSOUND_CLOCK / (2 * 1248)
QSOUND_FREQ_DIV = 4096
QSOUND_MAX_FREQ = 0xEFFF
QSOUND_PCM_VOICES = 16
QSOUND_ADPCM_VOICES = 3
QSOUND_CHANNELS = QSOUND_PCM_VOICES + QSOUND_ADPCM_VOICES
# One ADPCM voice is updated every third output sample, one nibble per update.
QSOUND_ADPCM_NIBBLE_RATE = QSOUND_OUT_RATE / 3.0
# Writes this close after a phase key-on belong to the same note.
QSOUND_SETTLE = 256
QSOUND_MIN_NOTE = 64
# QSound drivers use a 15-byte "loop" for one-shot bodies.
QSOUND_NO_LOOP = 16


@dataclass
class QSoundPcmHit:
    sample_time: int
    chip_id: int
    voice: int
    start: int
    end: int
    loop: int
    bank: int
    freq: int
    volume: int
    pan: int
    echo: int
    sample: "QSoundSample | None" = None
    off_sample: int = 0
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    pan_pts: list[tuple[int, int]] = field(default_factory=list)
    echo_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class QSoundAdpcmHit:
    sample_time: int
    chip_id: int
    voice: int
    start: int
    end: int
    bank: int
    volume: int
    pan: int
    sample: "QSoundSample | None" = None
    off_sample: int = 0
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    pan_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class QSoundSample:
    kind: str  # "pcm" | "adpcm"
    data: bytes
    loop_start: int = -1
    loop_end: int = -1
    hits: list = field(default_factory=list)

    @property
    def rate(self) -> float:
        return (
            QSOUND_ADPCM_NIBBLE_RATE
            if self.kind == "adpcm" else QSOUND_OUT_RATE
        )


def qsound_effective_clock(clock: int) -> int:
    """The rate Furnace's fixed 60 MHz QSound core will use."""
    if clock and clock < 5_000_000:
        return clock * 15
    return QSOUND_CLOCK


def qsound_freq_hz(freq: int) -> float:
    return QSOUND_OUT_RATE * max(1, freq) / QSOUND_FREQ_DIV


def qsound_pan(raw: int) -> tuple[int, int]:
    """(linear pan 0-32, Furnace surround flag) for a pan register."""
    surround = 0 if (raw & 0x40) else 1
    p = raw & 0x3F
    linear = p if not surround else max(0, min(32, p - 0x10))
    return linear, surround


def qsound_pan_split(linear: int) -> tuple[int, int]:
    """Closest (left, right) 4-bit split for a linear 0-32 pan."""
    linear = max(0, min(32, linear))
    best = (0, 0)
    best_err = 1 << 30
    for left in range(16):
        for right in range(16):
            if left == right:
                value = 16
            else:
                diff = right - left
                # Furnace truncates `pan*range` to int; match that exactly.
                value = int(
                    (1.0 + diff / max(left, right)) * 0.5 * 32
                )
            err = abs(value - linear)
            if err < best_err or (err == best_err and abs(left - right)
                                  < abs(best[0] - best[1])):
                best = (left, right)
                best_err = err
    return best


def assemble_qsound_rom(vgm: VgmFile) -> tuple[bytes, list[tuple[int, int]]]:
    """Assemble QSound 0x8F slices and return (ROM, covered intervals)."""
    slices = [r for r in vgm.roms if r.kind == "qsound"]
    if not slices:
        return b"", []
    size = max(
        [r.rom_size for r in slices]
        + [r.start + len(r.data) for r in slices]
        + [0]
    )
    rom = bytearray(size)
    covered: list[tuple[int, int]] = []
    for sl in sorted(slices, key=lambda r: r.start):
        if not sl.data:
            continue
        start = min(size, max(0, sl.start))
        end = min(size, start + len(sl.data))
        rom[start:end] = sl.data[:end - start]
        covered.append((start, end))
    covered.sort()
    merged: list[tuple[int, int]] = []
    for a, b in covered:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return bytes(rom), merged


def _covered(intervals: list[tuple[int, int]], start: int, end: int) -> bool:
    if end <= start:
        return False
    for a, b in intervals:
        if a <= start and end <= b:
            return True
        if a > start:
            break
    return False


def _volume_to_255(raw: int, peak: int) -> int:
    if raw <= 0:
        return 0
    scale = max(16383, peak)
    return max(1, min(255, int(round(raw * 255.0 / scale))))


def _delay_fx(raw: int) -> int:
    return max(0, min(2725, int(raw) - 1370))


def _feedback_fx(raw: int) -> int:
    # Furnace's 10xx effect is an 8-bit level written as value<<6.
    return max(0, min(255, int(round(raw / 64.0))))


def _echo_fx(raw: int) -> int:
    # QSound channel echo register is written as value<<7.
    return max(0, min(255, int(raw) >> 7))


qsound_delay_fx = _delay_fx
qsound_feedback_fx = _feedback_fx
qsound_echo_fx = _echo_fx


def _effective_body(
    start: int, end: int, loop: int,
) -> tuple[int, int, int, int, bool] | None:
    """(body_start, body_end, loop_start, loop_end, wraps).

    ``wraps`` means the sample runs from ``start`` to the end of the 64 KB bank
    and then from 0 to ``end``.  Real QSound hardware wraps its 16-bit address
    counter; several drivers use that for a one-shot body.
    """
    start &= 0xFFFF
    end &= 0xFFFF
    if loop > QSOUND_NO_LOOP:
        if start < end:
            length = end - start
            loop_start = end - loop - start
            loop_end = length
            if 0 <= loop_start < loop_end:
                return start, end, loop_start, loop_end, False
            return start, end, -1, -1, False
        # A loop length that spans the apparent body start selects the loop
        # start directly; this is how a driver pads the end of a looping body.
        body_start = (end - loop) & 0xFFFF
        if body_start < end:
            return body_start, end, 0, end - body_start, False
        return None
    if start < end:
        return start, end, -1, -1, False
    # One-shot wrap-around body.
    return start, end, -1, -1, True


def _collect_pcm_voice(
    writes: list[tuple[int, int, int]],
    rom: bytes, covered: list[tuple[int, int]],
    chip_id: int, voice: int, peak_volume: int,
    warnings: list[str],
) -> tuple[list[QSoundPcmHit], list[QSoundSample]]:
    hits: list[QSoundPcmHit] = []
    samples: list[QSoundSample] = []
    seen: dict[tuple[bytes, int, int], QSoundSample] = {}
    state = {
        "bank": 0x8000, "start": 0, "end": 0, "loop": 0,
        "freq": 0, "vol": 0, "pan": 0x120, "echo": 0,
    }
    active: QSoundPcmHit | None = None

    def close(when: int, off: bool) -> None:
        nonlocal active
        if active is None:
            return
        if off and when > active.sample_time:
            active.off_sample = when
        active = None

    def open_hit(
        when: int, start: int, end: int, loop: int, bank: int,
        freq: int, vol: int, pan: int, echo: int,
    ) -> None:
        nonlocal active
        body = _effective_body(start, end, loop)
        if body is None:
            return
        bstart, bend, loop_start, loop_end, wraps = body
        base = ((bank & 0x7FFF) << 16) & 0xFFFFFF
        if wraps:
            first = rom[base + bstart:base + 0x10000]
            second = rom[base:base + bend]
            data = first + second
            want = (0x10000 - bstart) + bend
            covered_here = (
                _covered(covered, base + bstart, base + 0x10000)
                and _covered(covered, base, base + bend)
            )
        else:
            data = rom[base + bstart:base + bend]
            want = bend - bstart
            covered_here = _covered(covered, base + bstart, base + bend)
        if want <= 0:
            return
        if len(data) < want:
            data = data + bytes(want - len(data))
        # Furnace's QSound PCM voices are limited to 64 KB per sample.
        if len(data) > 65520:
            data = data[:65520]
            warnings.append(
                f"QSound voice {voice + 1} sample is longer than Furnace's "
                "64 KB limit; it was truncated"
            )
        if loop_start >= 0 and loop_end > len(data):
            loop_start = loop_end = -1
        if not covered_here:
            warnings.append(
                f"QSound voice {voice + 1} sample range is not covered by "
                "the ROM blocks; it is silent"
            )
        key = (data, loop_start, loop_end)
        smp = seen.get(key)
        if smp is None:
            smp = QSoundSample(
                kind="pcm", data=data,
                loop_start=loop_start, loop_end=loop_end,
            )
            seen[key] = smp
            samples.append(smp)
        hit = QSoundPcmHit(
            sample_time=when,
            chip_id=chip_id,
            voice=voice,
            start=bstart,
            end=bend,
            loop=loop,
            bank=bank,
            freq=max(1, freq),
            volume=_volume_to_255(vol, peak_volume),
            pan=pan,
            echo=echo,
            sample=smp,
        )
        rate = qsound_freq_hz(freq)
        if loop_start < 0:
            hit.off_sample = when + int(round(len(data) / rate * VGM_RATE))
        hits.append(hit)
        smp.hits.append(hit)
        active = hit

    i = 0
    while i < len(writes):
        t, reg, val = writes[i]
        if reg == 8:  # bank for this voice
            state["bank"] = val
            i += 1
            continue
        if reg == 3:
            if not (val & 0x8000):
                if active is not None:
                    close(t, off=True)
                i += 1
                continue
            # Key-on: look ahead for this note's initial parameters.
            close(t, off=True)
            j = i + 1
            start = state["start"]
            end = state["end"]
            loop = state["loop"]
            bank = state["bank"]
            freq = state["freq"]
            vol = state["vol"]
            pan = state["pan"]
            echo = state["echo"]
            first_freq = False
            first_vol = False
            while j < len(writes):
                tt, rr, vv = writes[j]
                if tt - t > QSOUND_SETTLE:
                    break
                if rr == 3 and (vv & 0x8000):
                    break
                if rr == 1:
                    start = vv
                elif rr == 5:
                    end = vv
                elif rr == 4:
                    loop = vv
                elif rr == 8:
                    bank = vv
                elif rr == 2:
                    if not first_freq:
                        freq = vv
                        first_freq = True
                elif rr == 6:
                    if not first_vol:
                        vol = vv
                        first_vol = True
                elif rr == 9:
                    pan = vv
                elif rr == 10:
                    echo = vv
                j += 1
            state.update({
                "start": start, "end": end, "loop": loop, "bank": bank,
                "freq": freq, "vol": vol, "pan": pan, "echo": echo,
            })
            open_hit(t, start, end, loop, bank, freq, vol, pan, echo)
            # Continue after the key-on; the writes already read by the
            # look-ahead are applied again, and later volume/frequency writes
            # become the note's live points (including a zero-volume cut).
            i += 1
            continue
        if reg == 1:
            state["start"] = val
            if active is not None and val != 0:
                # A start rewrite while sounding is the driver's next note;
                # keep it for the next phase write.
                pass
            i += 1
            continue
        if reg == 2:
            state["freq"] = val
            if active is not None:
                active.freq_pts.append((t, max(1, val)))
            i += 1
            continue
        if reg == 4:
            state["loop"] = val
            i += 1
            continue
        if reg == 5:
            state["end"] = val
            i += 1
            continue
        if reg == 6:
            state["vol"] = val
            if active is not None:
                level = _volume_to_255(val, peak_volume)
                active.vol_pts.append((t, level))
                if level == 0:
                    close(t, off=True)
            i += 1
            continue
        if reg == 9:
            state["pan"] = val
            if active is not None:
                active.pan_pts.append((t, val))
            i += 1
            continue
        if reg == 10:
            state["echo"] = val
            if active is not None:
                active.echo_pts.append((t, val))
            i += 1
            continue
        i += 1
    return hits, samples


def _collect_adpcm_voice(
    writes: list[tuple[int, int, int]],
    rom: bytes, covered: list[tuple[int, int]],
    chip_id: int, voice: int, peak_volume: int,
    warnings: list[str],
) -> tuple[list[QSoundAdpcmHit], list[QSoundSample]]:
    hits: list[QSoundAdpcmHit] = []
    samples: list[QSoundSample] = []
    seen: dict[bytes, QSoundSample] = {}
    state = {"start": 0, "end": 0, "bank": 0x8000, "vol": 0, "pan": 0x120}
    active: QSoundAdpcmHit | None = None
    previous_key = 0
    i = 0
    while i < len(writes):
        t, reg, val = writes[i]
        if reg == 4:
            state["start"] = val
            i += 1
            continue
        if reg == 5:
            state["end"] = val
            i += 1
            continue
        if reg == 6:
            state["bank"] = val
            i += 1
            continue
        if reg == 7:
            state["vol"] = val
            if active is not None:
                level = _volume_to_255(val, peak_volume)
                active.vol_pts.append((t, level))
                if level == 0:
                    active.off_sample = t
                    active = None
            i += 1
            continue
        if reg == 8:
            state["pan"] = val
            if active is not None:
                active.pan_pts.append((t, val))
            i += 1
            continue
        if reg == 0:  # key-on flag
            if val and not previous_key:
                if active is not None:
                    active = None
                start = state["start"]
                end = state["end"]
                bank = state["bank"]
                vol = state["vol"]
                pan = state["pan"]
                j = i + 1
                first_vol = False
                while j < len(writes):
                    tt, rr, vv = writes[j]
                    if tt - t > QSOUND_SETTLE:
                        break
                    if rr == 0 and vv:
                        break
                    if rr == 4:
                        start = vv
                    elif rr == 5:
                        end = vv
                    elif rr == 6:
                        bank = vv
                    elif rr == 7 and not first_vol:
                        vol = vv
                        first_vol = True
                    elif rr == 8:
                        pan = vv
                    j += 1
                state.update({
                    "start": start, "end": end, "bank": bank,
                    "vol": vol, "pan": pan,
                })
                if end > start + 1 and (bank & 0x8000):
                    base = ((bank & 0x7FFF) << 16) & 0xFFFFFF
                    data = rom[base + start:base + end]
                    if len(data) < end - start:
                        data = data + bytes((end - start) - len(data))
                    if not _covered(covered, base + start, base + end):
                        warnings.append(
                            f"QSound ADPCM voice {voice + 1} sample range is "
                            "not covered by the ROM blocks; it is silent"
                        )
                    smp = seen.get(data)
                    if smp is None:
                        smp = QSoundSample(kind="adpcm", data=data)
                        seen[data] = smp
                        samples.append(smp)
                    hit = QSoundAdpcmHit(
                        sample_time=t,
                        chip_id=chip_id,
                        voice=voice,
                        start=start,
                        end=end,
                        bank=bank,
                        volume=_volume_to_255(vol, peak_volume),
                        pan=pan,
                        sample=smp,
                        off_sample=t + int(round(
                            (2 * len(data)) / QSOUND_ADPCM_NIBBLE_RATE
                            * VGM_RATE
                        )),
                    )
                    hits.append(hit)
                    smp.hits.append(hit)
                    active = hit
                i += 1
                previous_key = val
                continue
            previous_key = val
            i += 1
            continue
        i += 1
    return hits, samples


def collect_qsound(
    vgm: VgmFile,
) -> tuple[
    list[QSoundPcmHit], list[QSoundAdpcmHit], list[QSoundSample], list[str],
]:
    """Walk the QSound register stream."""
    warnings: list[str] = []
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "qsound"})
    if not chips:
        return [], [], [], warnings
    rom, covered = assemble_qsound_rom(vgm)
    if not rom:
        warnings.append(
            "no QSound ROM block (0x8F); sample voices will be silent"
        )
    peak_volume = 0
    for w in vgm.writes:
        if w.chip != "qsound":
            continue
        reg = w.reg
        if reg < 0x80 and (reg & 7) == 6:
            peak_volume = max(peak_volume, int(w.val))
        elif 0xCD <= reg <= 0xD5 and (reg - 0xCD) % 4 == 0:
            peak_volume = max(peak_volume, int(w.val))
    pcm_hits: list[QSoundPcmHit] = []
    adpcm_hits: list[QSoundAdpcmHit] = []
    samples: list[QSoundSample] = []
    for cid in chips:
        voice_writes: list[list[tuple[int, int, int]]] = [
            [] for _ in range(QSOUND_CHANNELS)
        ]
        for w in vgm.writes:
            if w.chip != "qsound" or w.chip_id != cid:
                continue
            reg = w.reg
            val = w.val
            if reg < 0x80:
                ch = reg >> 3
                sub = reg & 7
                if sub == 0:
                    # Bank register applies to the next channel, matching the
                    # QSound core's rotated map.
                    voice_writes[(ch + 1) % 16].append((w.sample, 8, val))
                else:
                    voice_writes[ch].append((w.sample, sub, val))
            elif 0x80 <= reg < 0x90:
                voice_writes[reg - 0x80].append((w.sample, 9, val))
            elif 0xBA <= reg < 0xCA:
                voice_writes[reg - 0xBA].append((w.sample, 10, val))
            elif 0x90 <= reg <= 0x92:
                voice_writes[16 + (reg - 0x90)].append((w.sample, 8, val))
            elif 0xCA <= reg <= 0xD5:
                base = 0xCA + 4 * ((reg - 0xCA) // 4)
                index = (reg - base)
                v = (reg - 0xCA) // 4
                voice_writes[16 + v].append((w.sample, index + 4, val))
            elif 0xD6 <= reg <= 0xD8:
                voice_writes[16 + (reg - 0xD6)].append((w.sample, 0, val))
        for voice in range(16):
            h, s = _collect_pcm_voice(
                voice_writes[voice], rom, covered, cid, voice,
                peak_volume, warnings,
            )
            pcm_hits.extend(h)
            samples.extend(s)
        for voice in range(3):
            h, s = _collect_adpcm_voice(
                voice_writes[16 + voice], rom, covered, cid, voice,
                peak_volume, warnings,
            )
            adpcm_hits.extend(h)
            samples.extend(s)
    pcm_hits.sort(key=lambda h: (h.sample_time, h.chip_id, h.voice))
    adpcm_hits.sort(key=lambda h: (h.sample_time, h.chip_id, h.voice))
    deduped: list[str] = []
    for message in warnings:
        if message not in deduped:
            deduped.append(message)
    return pcm_hits, adpcm_hits, samples, deduped
