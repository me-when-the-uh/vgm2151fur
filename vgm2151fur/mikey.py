"""Atari Lynx Mikey: LFSR voices and CPU-fed 8-bit DAC samples.

VGM 1.72 carries the chip clock at header 0xE4 (16 MHz on the Lynx) and writes
its registers with command 0x40: ``40 aa vv``.  Four voices at 0x20+8*n have

    0 volume (signed)
    1 feedback taps
    2 output level (the DAC byte a driver streams)
    3 shift register low byte
    4 timer backup
    5 control (shift, count/reload enable, interrupt, f7, integrate)
    6 counter
    7 shift register high nibble / control B

plus the shared stereo registers 0x40-0x43 attenuation, 0x44 pan enable and
0x50 stereo mute.

A voice is either an LFSR tone (the driver writes volume/feedback/timer) or a
software DAC (the driver streams register 2).  Furnace's native Lynx system
supports both: ``DIV_INS_MIKEY`` carries the LFSR macros, and an Amiga-style
sample instrument plays 8-bit bodies through the same four columns.
"""

from __future__ import annotations

import array
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.samplecluster import cluster_fragments
from vgm2151fur.vgm import MIKEY_DEFAULT_CLOCK, VGM_RATE, VgmFile

MIKEY_VOICES = 4
# Mikey timer: period = (backup+1) << shift microseconds (1 MHz tick source).
MIKEY_TIMER_CLOCK = 1_000_000
# Furnace's DivPlatformLynx pitch table: 16 MHz clock, divider 64 in the
# default (untuned) mode, and a 26-bit period maximum.
MIKEY_DIVIDER = 64
MIKEY_MAX_PERIOD = 0x3FFFFFF

REG_VOLUME = 0
REG_FEEDBACK = 1
REG_OUTPUT = 2
REG_SHIFT = 3
REG_BACKUP = 4
REG_CONTROL = 5
REG_COUNTER = 6
REG_OTHER = 7
REG_ATTEN = 0x40
REG_PAN = 0x44
REG_STEREO = 0x50

# A register stream faster than this on the output byte is a software DAC.
MIKEY_DAC_WRITES = 32
# Sub-frame gates are driver housekeeping rather than notes.
MIKEY_MIN_NOTE = 80
# Output chunks: 0.25 s at the reconstructed rate.
MIKEY_CHUNK_DIV = 4
# A gap of at most this many samples is the DAC's nominal write period.
MIKEY_MAX_GAP = 64


def _s8(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value >= 0x80 else value


def mikey_duty(feedback: int, control: int) -> int:
    """Feedback/control registers -> Furnace's 10-bit duty/int macro value."""
    return (
        (feedback & 0x3F)
        | (((feedback >> 6) & 1) << 7)
        | (((feedback >> 7) & 1) << 8)
        | (((control >> 7) & 1) << 6)
        | (((control >> 5) & 1) << 9)
    )


def mikey_lfsr(shift: int, other: int) -> int:
    """Shift/other registers -> the 12-bit LFSR seed."""
    return (shift & 0xFF) | ((other & 0xF0) << 4)


def mikey_period(backup: int, control: int) -> int:
    """Timer divider value Furnace's Lynx pitch table expects.

    The hardware fires every ``(backup+1) << shift`` microseconds; Furnace's
    ``MikeyFreqDiv`` takes exactly that value (before dividing by the pitch
    table's 64).
    """
    return max(1, (backup + 1) << (control & 7))


def _effective_pan(atten: int, pan_bits: int, stereo: int, voice: int) -> int:
    """Attenuation register and pan/mute bits -> Furnace's 08xy byte.

    The chip uses an attenuation register when its pan-enable bit is set and
    falls back to full scale when the bit is clear; the stereo bits mute a
    side.  VGM drivers often leave the pan bits at zero while setting the
    attenuation bytes to 0xFF (full), so treating a clear bit as "mute" would
    silence the whole chip.
    """
    left = ((atten >> 4) & 0x0F) if (pan_bits & (1 << voice)) else 0x0F
    right = (atten & 0x0F) if (pan_bits & (0x10 << voice)) else 0x0F
    if stereo & (1 << voice):
        left = 0
    if stereo & (0x10 << voice):
        right = 0
    return ((left & 0x0F) << 4) | (right & 0x0F)


@dataclass
class MikeyHit:
    """One LFSR tone on one module column."""

    sample_time: int
    chip_id: int
    voice: int
    value: int  # timer divider at the gate
    vol: int  # 0..127
    clock: int
    off_sample: int = 0
    duty: int = 0
    lfsr: int = 0
    pan: int = 0xFF
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    duty_pts: list[tuple[int, int]] = field(default_factory=list)
    lfsr_pts: list[tuple[int, int]] = field(default_factory=list)
    pan_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class MikeyOff:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class MikeyPcmHit:
    time: int
    voice: int
    pan: int = 0


@dataclass
class MikeySample:
    """One banked 8-bit DAC body."""

    chip_id: int
    voice: int
    data: bytes  # signed 8-bit, at ``rate``
    rate: int
    hits: list[MikeyPcmHit] = field(default_factory=list)
    chunk: int = 0

    @property
    def frames(self) -> int:
        return len(self.data)

    @property
    def name(self) -> str:
        return f"MIKEY{self.chip_id + 1} {self.chunk:03d}"


def _blank_channel() -> dict:
    return {
        "volume": 0,
        "feedback": 0,
        "output": 0,
        "shift": 0,
        "backup": 0,
        "control": 0,
        "counter": 0,
        "other": 0,
        "active": None,
        "output_writes": 0,
        "tone_writes": 0,
    }


def _pcm_stream(
    points: list[tuple[int, int]],
) -> tuple[array.array, int, int, int] | None:
    """Reconstruct one output-register stream as signed 8-bit PCM.

    Returns ``(samples, rate, first_sample_time, nominal_gap)``.  The write
    rate is the most common small gap; log jitter and pauses do not move it.
    """
    if not points:
        return None
    gaps = [
        b[0] - a[0]
        for a, b in zip(points, points[1:])
        if 0 < b[0] - a[0] <= MIKEY_MAX_GAP
    ]
    if not gaps:
        return None
    gap = Counter(gaps).most_common(1)[0][0]
    rate = max(1_000, min(44_100, int(round(VGM_RATE / gap))))
    start = points[0][0]
    end = points[-1][0]
    frames = max(1, int(round((end - start) / VGM_RATE * rate)) + 1)
    out = array.array("b", [0]) * frames
    for i, (when, value) in enumerate(points):
        first = max(0, int(round((when - start) / VGM_RATE * rate)))
        if i + 1 < len(points):
            last = int(round((points[i + 1][0] - start) / VGM_RATE * rate))
        else:
            last = frames
        last = max(first + 1, min(frames, last))
        v = _s8(value)
        for j in range(first, last):
            out[j] = v
    return out, rate, start, gap


def _asset_has_signal(samples: array.array, start: int, end: int) -> bool:
    for i in range(start, end):
        if samples[i]:
            return True
    return False


def collect_mikey(
    vgm: VgmFile, *, lossy: bool = False,
) -> tuple[list[MikeyHit], list[MikeyOff], list[MikeySample], list[str]]:
    """Walk a Mikey register stream.

    Returns ``(tone_hits, tone_offs, dac_samples, warnings)``.  The caller
    binds tone hits to instrument macros and DAC hits to a 120-slot sample map.
    """
    warnings: list[str] = []
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "mikey"})
    if not chips:
        return [], [], [], warnings
    clock = vgm.mikey_clock or MIKEY_DEFAULT_CLOCK
    atten = {cid: [0xFF] * MIKEY_VOICES for cid in chips}
    pan_bits = {cid: 0xFF for cid in chips}
    stereo = {cid: 0 for cid in chips}
    pan_changes: dict[tuple[int, int], list[tuple[int, int]]] = {}
    state = {cid: [_blank_channel() for _ in range(MIKEY_VOICES)] for cid in chips}

    # First pass: distinguish a streamed software DAC from an LFSR voice.
    for w in vgm.writes:
        if w.chip != "mikey" or not (0x20 <= w.reg < 0x40):
            continue
        ch = (w.reg >> 3) & 3
        reg = w.reg & 7
        st = state[w.chip_id][ch]
        if reg == REG_OUTPUT:
            st["output_writes"] += 1
        else:
            st["tone_writes"] += 1
    dac_voice = {
        (cid, ch): (
            st["output_writes"] >= MIKEY_DAC_WRITES
            and st["output_writes"] > st["tone_writes"]
        )
        for cid in chips
        for ch, st in enumerate(state[cid])
    }
    mixed = [
        (cid, ch) for cid, ch in dac_voice
        if dac_voice[(cid, ch)]
        and state[cid][ch]["tone_writes"] >= MIKEY_DAC_WRITES
    ]
    if mixed:
        warnings.append(
            f"{len(mixed)} Mikey voice(s) mix DAC and LFSR writes; "
            "the DAC stream wins"
        )

    hits: list[MikeyHit] = []
    offs: list[MikeyOff] = []
    warnings_seen: dict[str, int] = {
        "glitch": 0, "negative_volume": 0, "clamped": 0,
    }

    def effective_pan(cid: int, ch: int) -> int:
        return _effective_pan(atten[cid][ch], pan_bits[cid], stereo[cid], ch)

    for cid in chips:
        for ch in range(MIKEY_VOICES):
            pan_changes[(cid, ch)] = [(0, effective_pan(cid, ch))]

    def record_pan(cid: int, t: int) -> None:
        for ch in range(MIKEY_VOICES):
            pan = effective_pan(cid, ch)
            pts = pan_changes[(cid, ch)]
            if pts[-1][1] != pan:
                pts.append((t, pan))

    def pan_at(cid: int, ch: int, t: int) -> int:
        pts = pan_changes.get((cid, ch))
        if not pts:
            return effective_pan(cid, ch)
        idx = bisect_right([p[0] for p in pts], t) - 1
        return pts[idx][1] if idx >= 0 else pts[0][1]

    def close(cid: int, ch: int, t: int, *, emit_off: bool = True) -> None:
        st = state[cid][ch]
        hit = st["active"]
        if hit is None:
            return
        hit.off_sample = t
        if t - hit.sample_time >= MIKEY_MIN_NOTE:
            hits.append(hit)
            if emit_off and t < (vgm.total_samples or 0):
                offs.append(MikeyOff(t, cid, ch))
        else:
            warnings_seen["glitch"] += 1
        st["active"] = None

    def open_hit(cid: int, ch: int, t: int) -> None:
        st = state[cid][ch]
        volume = abs(_s8(st["volume"]))
        if st["volume"] < 0 or st["volume"] >= 128:
            warnings_seen["negative_volume"] += 1
        volume = max(0, min(127, volume))
        hit = MikeyHit(
            sample_time=t,
            chip_id=cid,
            voice=ch,
            value=mikey_period(st["backup"], st["control"]),
            vol=volume,
            clock=clock,
            duty=mikey_duty(st["feedback"], st["control"]),
            lfsr=mikey_lfsr(st["shift"], st["other"]),
            pan=effective_pan(cid, ch),
        )
        st["active"] = hit

    def refresh(cid: int, ch: int, t: int) -> None:
        st = state[cid][ch]
        on = (
            abs(_s8(st["volume"])) > 0
            and bool(st["control"] & 0x08)  # timer count enabled
        )
        hit = st["active"]
        if not on:
            if hit is not None:
                close(cid, ch, t)
            return
        if hit is None:
            open_hit(cid, ch, t)
            return
        value = mikey_period(st["backup"], st["control"])
        if value != (hit.freq_pts[-1][1] if hit.freq_pts else hit.value):
            hit.freq_pts.append((t, value))
        duty = mikey_duty(st["feedback"], st["control"])
        if duty != (hit.duty_pts[-1][1] if hit.duty_pts else hit.duty):
            hit.duty_pts.append((t, duty))
        lfsr = mikey_lfsr(st["shift"], st["other"])
        if lfsr != (hit.lfsr_pts[-1][1] if hit.lfsr_pts else hit.lfsr):
            hit.lfsr_pts.append((t, lfsr))
        volume = max(0, min(127, abs(_s8(st["volume"]))))
        if volume != (hit.vol_pts[-1][1] if hit.vol_pts else hit.vol):
            hit.vol_pts.append((t, volume))
        pan = effective_pan(cid, ch)
        if pan != (hit.pan_pts[-1][1] if hit.pan_pts else hit.pan):
            hit.pan_pts.append((t, pan))

    def refresh_pan(cid: int, t: int) -> None:
        for ch in range(MIKEY_VOICES):
            st = state[cid][ch]
            hit = st["active"]
            if hit is None:
                continue
            pan = effective_pan(cid, ch)
            if pan != (hit.pan_pts[-1][1] if hit.pan_pts else hit.pan):
                hit.pan_pts.append((t, pan))

    # DAC output points, in stream order.
    dac_points: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for w in vgm.writes:
        if w.chip != "mikey":
            continue
        cid = w.chip_id
        t = w.sample
        reg = w.reg
        if 0x20 <= reg < 0x40:
            ch = (reg >> 3) & 3
            sub = reg & 7
            st = state[cid][ch]
            if dac_voice.get((cid, ch)):
                if sub == REG_OUTPUT:
                    dac_points.setdefault((cid, ch), []).append((t, w.val))
                continue
            if sub == REG_VOLUME:
                old = st["volume"]
                st["volume"] = w.val
                # A signed volume byte: negative means phase-inverted output.
                if (_s8(w.val) < 0) != (_s8(old) < 0):
                    warnings_seen["negative_volume"] += 1
                refresh(cid, ch, t)
            elif sub == REG_FEEDBACK:
                st["feedback"] = w.val
                refresh(cid, ch, t)
            elif sub == REG_SHIFT:
                st["shift"] = w.val
                refresh(cid, ch, t)
            elif sub == REG_BACKUP:
                st["backup"] = w.val
                refresh(cid, ch, t)
            elif sub == REG_CONTROL:
                old = st["control"]
                st["control"] = w.val
                # A 0->1 count-enable transition is the driver's key-on.
                if (old & 0x08) == 0 and (w.val & 0x08):
                    close(cid, ch, t, emit_off=False)
                    if abs(_s8(st["volume"])) > 0:
                        open_hit(cid, ch, t)
                    continue
                refresh(cid, ch, t)
            elif sub == REG_OTHER:
                st["other"] = w.val
                refresh(cid, ch, t)
            continue
        if reg in (REG_ATTEN, REG_ATTEN + 1, REG_ATTEN + 2, REG_ATTEN + 3):
            ch = reg - REG_ATTEN
            atten[cid][ch] = w.val
            record_pan(cid, t)
            refresh_pan(cid, t)
        elif reg == REG_PAN:
            pan_bits[cid] = w.val
            record_pan(cid, t)
            refresh_pan(cid, t)
        elif reg == REG_STEREO:
            stereo[cid] = w.val
            record_pan(cid, t)
            refresh_pan(cid, t)

    for cid in chips:
        for ch in range(MIKEY_VOICES):
            close(cid, ch, vgm.total_samples or 0, emit_off=False)

    hits = [h for h in hits if h.off_sample >= h.sample_time]

    # Bank the DAC streams.  A fixed 0.25-second grid keeps the sample data
    # byte-exact; an all-zero chunk is a rest and costs no sample.
    samples: list[MikeySample] = []
    seen: dict[tuple[int, int, bytes], MikeySample] = {}
    for (cid, ch), points in sorted(dac_points.items()):
        stream = _pcm_stream(points)
        if stream is None:
            warnings.append(
                f"Mikey voice {ch + 1} has DAC writes but no usable timing"
            )
            continue
        pcm, rate, start, gap = stream
        chunk = max(1, rate // MIKEY_CHUNK_DIV)
        for first in range(0, len(pcm), chunk):
            last = min(len(pcm), first + chunk)
            if not _asset_has_signal(pcm, first, last):
                continue
            body = bytes((pcm[i] & 0xFF) for i in range(first, last))
            when = start + int(round(first / rate * VGM_RATE))
            key = (cid, rate, body)
            found = seen.get(key)
            if found is not None:
                found.hits.append(MikeyPcmHit(
                    when, ch, pan_at(cid, ch, when),
                ))
                continue
            smp = MikeySample(
                chip_id=cid,
                voice=ch,
                data=body,
                rate=rate,
                hits=[MikeyPcmHit(when, ch, pan_at(cid, ch, when))],
                chunk=len(samples),
            )
            seen[key] = smp
            samples.append(smp)
        if gap <= 0:
            warnings.append(f"Mikey voice {ch + 1} has no stable DAC rate")
    if lossy and len(samples) > 32:
        bodies = [s.data for s in samples]
        keep, remap = cluster_fragments(
            bodies,
            [s.rate for s in samples],
            {i: len(s.hits) for i, s in enumerate(samples)},
        )
        compact = [samples[i] for i in keep]
        for old, smp in enumerate(samples):
            new = remap[old]
            if new < 0 or keep[new] == old:
                continue
            compact[new].hits.extend(smp.hits)
        if len(compact) < len(samples):
            warnings.append(
                f"Mikey: lossy sample clustering {len(samples)} -> "
                f"{len(compact)} bodies"
            )
        samples = compact

    if warnings_seen["glitch"]:
        warnings.append(
            f"{warnings_seen['glitch']} sub-frame Mikey gate(s) skipped"
        )
    if warnings_seen["negative_volume"]:
        warnings.append(
            f"{warnings_seen['negative_volume']} Mikey negative-volume "
            "write(s) play with positive amplitude (phase is inverted at "
            "the source)"
        )
    return hits, offs, samples, warnings
