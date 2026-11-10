"""Bandai WonderSwan sound: four wavetable voices plus one PCM voice.

VGM command ``0xBC`` writes the sound ports ``0x80-0x94`` and ``0xC6``
writes the 16 KB internal RAM (the wave tables and, for DMA drivers, PCM
data).  The chip's wave RAM is only 16 KB; addresses wrap at ``0x3FFF`` and
many rips use a mirrored ``0x8000+`` address.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, VgmFile

WSWAN_DEFAULT_CLOCK = 3_072_000
WS_PITCH_DIVIDER = 32
WS_MAX_PERIOD = 0x800
WS_WAVE_LEN = 32
WS_VOICES = 4
# A musical pitch jump this large is a new note, not a bend.  Comparing
# period registers directly is wrong: one unit near period 32 is a semitone,
# while near period 2000 it is a few cents.
WS_NOTE_JUMP_ST = 0.9
WS_MIN_NOTE = 48
# A level jump this large while the gate stays open is a retriggered drum or
# a new note.  Opposite-side volume flips are separate hits even when the peak
# level does not rise.
WS_RETRIGGER_RISE = 4
# Memory writes closer than this are one wave upload burst.
WS_WAVE_BURST = 256
# A volume write this close to a note's period-driven start is that note's
# initial level, not a mid-note step.
WS_SETTLE = 8
# PCM write gaps larger than this split one stream into separate sample runs.
WS_PCM_GAP = 2_000
WS_PCM_MIN_RATE = 2_000
WS_PCM_MAX_RATE = 48_000


@dataclass
class WsHit:
    sample_time: int
    chip_id: int
    voice: int
    period: int
    wave: bytes
    vol_l: int
    vol_r: int
    noise: int = 0
    sweep_time: int = 0
    sweep_amount: int = 0
    vol_pts: list[tuple[int, int, int]] = field(default_factory=list)
    period_pts: list[tuple[int, int]] = field(default_factory=list)
    wave_pts: list[tuple[int, bytes]] = field(default_factory=list)
    noise_pts: list[tuple[int, int]] = field(default_factory=list)
    sweep_pts: list[tuple[int, int, int]] = field(default_factory=list)
    off_sample: int = 0


@dataclass
class WsPcmHit:
    sample_time: int
    chip_id: int
    volume: int
    pan_l: int
    pan_r: int
    vol_pts: list[tuple[int, int, int]] = field(default_factory=list)
    off_sample: int = 0


@dataclass
class WsPcmSample:
    chip_id: int
    data: bytes
    rate: int
    hits: list[WsPcmHit] = field(default_factory=list)


def _wave_from_ram(ram: bytearray, base: int, voice: int) -> bytes:
    start = (base + voice * 16) & 0x3FFF
    out = bytearray()
    for i in range(16):
        b = ram[(start + i) & 0x3FFF]
        # The core reads the low nibble first, then the high nibble.
        out.append(b & 0x0F)
        out.append((b >> 4) & 0x0F)
    return bytes(out)


def _signed(value: int) -> int:
    return (value & 0xFF) - 0x80


def _pcm_from_points(
    points: list[tuple[int, int]],
) -> tuple[bytes, int, int] | None:
    """Zero-order-hold reconstruction at the stream's nominal write rate."""
    if len(points) < 2:
        return None
    gaps = [
        b[0] - a[0]
        for a, b in zip(points, points[1:])
        if 0 < b[0] - a[0] <= WS_PCM_GAP
    ]
    if not gaps:
        return None
    gap = Counter(gaps).most_common(1)[0][0]
    rate = max(
        WS_PCM_MIN_RATE,
        min(WS_PCM_MAX_RATE, int(round(VGM_RATE / gap))),
    )
    start = points[0][0]
    end = points[-1][0]
    frames = max(1, int(round((end - start) / VGM_RATE * rate)) + 1)
    out = bytearray(frames)
    for i, (when, value) in enumerate(points):
        first = max(0, int(round((when - start) / VGM_RATE * rate)))
        if i + 1 < len(points):
            last = int(round((points[i + 1][0] - start) / VGM_RATE * rate))
        else:
            last = frames
        last = max(first + 1, min(frames, last))
        v = _signed(value)
        for j in range(first, last):
            out[j] = v & 0xFF
    return bytes(out), rate, start


def collect_wonderswan(
    vgm: VgmFile,
) -> tuple[list[WsHit], list[WsPcmSample], list[str]]:
    """Return wavetable notes and streamed PCM bodies."""
    warnings: list[str] = []
    chips = sorted({
        w.chip_id for w in vgm.writes if w.chip == "wonderswan"
    })
    if not chips:
        return [], [], warnings
    total = vgm.total_samples or 0
    state: dict[int, dict] = {}
    for cid in chips:
        state[cid] = {
            "ram": bytearray(0x4000),
            "base": 0,
            "snd": 0,
            "sweep_time": 0,
            "sweep_amount": 0,
            "noise": 0,
            "pcm_vol": 0,
            "pcm_on": False,
            "pcm_points": [],
            "pcm_start": 0,
            "pcm_end": 0,
            "pcm_vol_pts": [],
            "wave_t": -1,
            "dirty": [False] * WS_VOICES,
            "ch": [
                {
                    "period_raw": 0xFFFF,
                    "vol_l": 0,
                    "vol_r": 0,
                    "active": None,
                    "last_note_t": -1,
                    "last_period_t": -100000,
                    "last_vol_t": -100000,
                }
                for _ in range(WS_VOICES)
            ],
        }
    hits: list[WsHit] = []
    samples: list[WsPcmSample] = []
    seen: dict[tuple[int, bytes], WsPcmSample] = {}

    def period_of(raw: int) -> int:
        return 2048 - (raw & 0x7FF)

    def body_for(cid: int, voice: int) -> bytes:
        st = state[cid]
        return _wave_from_ram(st["ram"], st["base"], voice)

    def close(cid: int, voice: int, when: int, off: bool) -> None:
        st = state[cid]["ch"][voice]
        hit: WsHit | None = st["active"]
        if hit is None:
            return
        if off and when > hit.sample_time:
            hit.off_sample = when
        st["active"] = None

    def open_hit(cid: int, voice: int, when: int) -> WsHit | None:
        st = state[cid]
        ch = st["ch"][voice]
        if not (st["snd"] & (1 << voice)):
            return None
        if ch["vol_l"] == 0 and ch["vol_r"] == 0:
            return None
        raw = ch["period_raw"]
        if raw == 0xFFFF:
            return None
        prev = ch.get("last_hit")
        if prev is not None and prev.sample_time == when:
            # Two gate/register paths can identify the same attack (for
            # example the noise-enable bit and the noise-reset write).  Keep
            # one hit with the final state instead of two notes on one row.
            prev.period = period_of(raw)
            prev.wave = body_for(cid, voice)
            prev.vol_l = ch["vol_l"]
            prev.vol_r = ch["vol_r"]
            prev.noise = (
                (st["noise"] & 7) + 1
                if voice == 3 and (st["snd"] & 0x80) else 0
            )
            prev.sweep_time = st["sweep_time"]
            prev.sweep_amount = st["sweep_amount"]
            prev.off_sample = 0
            ch["active"] = prev
            return prev
        hit = WsHit(
            sample_time=when,
            chip_id=cid,
            voice=voice,
            period=period_of(raw),
            wave=body_for(cid, voice),
            vol_l=ch["vol_l"],
            vol_r=ch["vol_r"],
            noise=(st["noise"] & 7) + 1 if voice == 3 and (st["snd"] & 0x80) else 0,
            sweep_time=st["sweep_time"],
            sweep_amount=st["sweep_amount"],
        )
        hits.append(hit)
        ch["active"] = hit
        ch["last_hit"] = hit
        ch["last_note_t"] = when
        return hit

    def refresh(cid: int, voice: int, when: int) -> None:
        ch = state[cid]["ch"][voice]
        hit: WsHit | None = ch["active"]
        if hit is None:
            open_hit(cid, voice, when)
            return
        if ch["vol_l"] == 0 and ch["vol_r"] == 0:
            close(cid, voice, when, off=True)
            return
        last_l = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol_l
        last_r = hit.vol_pts[-1][2] if hit.vol_pts else hit.vol_r
        if (ch["vol_l"], ch["vol_r"]) != (last_l, last_r):
            newmax = max(ch["vol_l"], ch["vol_r"])
            oldmax = max(last_l, last_r)
            hard_flip = (
                (ch["vol_l"] > 0 and ch["vol_r"] == 0
                 and last_l == 0 and last_r > 0)
                or (ch["vol_r"] > 0 and ch["vol_l"] == 0
                    and last_r == 0 and last_l > 0)
            )
            if when - hit.sample_time <= WS_SETTLE and not hit.vol_pts:
                # The period write opened the note a sample or two before the
                # driver wrote its companion volume byte.
                hit.vol_l = ch["vol_l"]
                hit.vol_r = ch["vol_r"]
            elif (
                (newmax - oldmax >= WS_RETRIGGER_RISE and not hard_flip)
                or hard_flip
            ) and when - hit.sample_time >= WS_MIN_NOTE:
                # A gate jump or an opposite-side volume flip is a new hit,
                # not a volume step in a long instrument macro.
                close(cid, voice, when, off=False)
                open_hit(cid, voice, when)
                return
            else:
                hit.vol_pts.append((when, ch["vol_l"], ch["vol_r"]))
        raw = ch["period_raw"]
        if raw != 0xFFFF:
            per = period_of(raw)
            last_per = (
                hit.period_pts[-1][1] if hit.period_pts else hit.period
            )
            if when - hit.sample_time <= WS_SETTLE and not hit.period_pts:
                hit.period = per
            elif abs(12.0 * math.log2(max(1, per) / max(1, last_per))) >= (
                WS_NOTE_JUMP_ST
            ):
                close(cid, voice, when, off=True)
                open_hit(cid, voice, when)
            elif per != last_per:
                hit.period_pts.append((when, per))
        st = state[cid]
        if voice == 3:
            noise = (st["noise"] & 7) + 1 if (st["snd"] & 0x80) else 0
            last_noise = (
                hit.noise_pts[-1][1] if hit.noise_pts else hit.noise
            )
            if noise != last_noise:
                hit.noise_pts.append((when, noise))
        if voice == 2:
            last_sweep = (
                (hit.sweep_pts[-1][1], hit.sweep_pts[-1][2])
                if hit.sweep_pts else (hit.sweep_time, hit.sweep_amount)
            )
            if (st["sweep_time"], st["sweep_amount"]) != last_sweep:
                hit.sweep_pts.append((
                    when, st["sweep_time"], st["sweep_amount"],
                ))

    def flush_waves(cid: int, when: int) -> None:
        st = state[cid]
        for voice in range(WS_VOICES):
            if not st["dirty"][voice]:
                continue
            st["dirty"][voice] = False
            hit = st["ch"][voice]["active"]
            if hit is None:
                continue
            body = body_for(cid, voice)
            last_wave = (
                hit.wave_pts[-1][1] if hit.wave_pts else hit.wave
            )
            if body != last_wave:
                hit.wave_pts.append((when, body))

    def finish_pcm(cid: int, when: int) -> None:
        st = state[cid]
        points = st["pcm_points"]
        st["pcm_points"] = []
        if not points:
            return
        built = _pcm_from_points(points)
        if built is None:
            warnings.append(
                "WonderSwan PCM stream has no stable write rate; it was dropped"
            )
            return
        data, rate, start = built
        if not any(v != 0x80 for v in data):
            return
        key = (rate, data)
        smp = seen.get(key)
        vol, pl, pr = _pcm_volume(st["pcm_vol_pts"], start, st["pcm_vol"])
        if smp is None:
            smp = WsPcmSample(chip_id=cid, data=data, rate=rate)
            seen[key] = smp
            samples.append(smp)
        hit = WsPcmHit(
            sample_time=start,
            chip_id=cid,
            volume=vol,
            pan_l=pl,
            pan_r=pr,
            off_sample=min(when, start + int(round(len(data) / rate * VGM_RATE)))
            if when else start + int(round(len(data) / rate * VGM_RATE)),
        )
        for vt, raw in st["pcm_vol_pts"]:
            if start < vt < hit.off_sample:
                v, l, r = _pcm_volume([(vt, raw)], vt, st["pcm_vol"])
                hit.vol_pts.append((vt, v, (l << 4) | r))
        smp.hits.append(hit)

    def _pcm_volume(points, when, default_raw):
        raw = default_raw
        for t, v in points:
            if t <= when:
                raw = v
        # The WS PCM volume register packs left in bits 2-3 and right in
        # bits 0-1 (core: PCMVolumeRight = ((value<<2)&0xc)*2).
        left = (raw >> 2) & 3
        right = raw & 3
        level = max(left, right)
        vol = 15 if level >= 3 else 10 if level == 2 else 0
        return vol, left * 5, right * 5

    for w in vgm.writes:
        if w.chip != "wonderswan":
            continue
        cid = w.chip_id
        st = state[cid]
        t = w.sample
        reg = w.reg
        val = w.val & 0xFF

        if reg >= 0x10000:
            addr = (reg - 0x10000) & 0x3FFF
            st["ram"][addr] = val
            st["wave_t"] = t
            base = st["base"] & 0x3FFF
            for voice in range(WS_VOICES):
                start = (base + voice * 16) & 0x3FFF
                if ((addr - start) & 0x3FFF) < 16:
                    st["dirty"][voice] = True
            continue

        flush_waves(cid, t)
        if reg == 0x8F:
            st["base"] = (val << 6) & 0xFFFF
            for voice in range(WS_VOICES):
                hit = st["ch"][voice]["active"]
                if hit is not None:
                    body = body_for(cid, voice)
                    last_wave = (
                        hit.wave_pts[-1][1] if hit.wave_pts else hit.wave
                    )
                    if body != last_wave:
                        hit.wave_pts.append((t, body))
            continue
        if reg == 0x90:
            old = st["snd"]
            st["snd"] = val
            for voice in range(WS_VOICES):
                was = bool(old & (1 << voice))
                now = bool(val & (1 << voice))
                if now and not was:
                    open_hit(cid, voice, t)
                elif was and not now:
                    close(cid, voice, t, off=True)
            pcm_was = bool(old & 0x20)
            pcm_now = bool(val & 0x20)
            if pcm_now and not pcm_was:
                st["pcm_on"] = True
                st["pcm_points"] = []
                st["pcm_start"] = t
                st["pcm_vol_pts"] = []
            elif pcm_was and not pcm_now:
                st["pcm_on"] = False
                st["pcm_end"] = t
                finish_pcm(cid, t)
            if (val & 0x80) and not (old & 0x80):
                hit = st["ch"][3]["active"]
                if hit is not None and t - hit.sample_time >= WS_MIN_NOTE:
                    # Turning the noise generator on is a new percussion hit.
                    close(cid, 3, t, off=False)
                    open_hit(cid, 3, t)
            refresh(cid, 3, t)
            refresh(cid, 2, t)
            continue
        if reg == 0x94:
            st["pcm_vol"] = val
            st["pcm_vol_pts"].append((t, val))
            continue
        if reg in (0x92, 0x93):
            continue
        if reg == 0x89 and st["pcm_on"]:
            st["pcm_points"].append((t, val))
            continue
        if 0x80 <= reg <= 0x87:
            # 0x80/0x81 voice 0, 0x82/0x83 voice 1, 0x84/0x85 voice 2,
            # 0x86/0x87 voice 3.  Even is the low byte, odd the high byte.
            voice = (reg - 0x80) // 2
            ch = st["ch"][voice]
            if (reg - 0x80) % 2 == 0:
                ch["period_raw"] = (ch["period_raw"] & 0xFF00) | val
            else:
                ch["period_raw"] = (ch["period_raw"] & 0x00FF) | (val << 8)
            refresh(cid, voice, t)
            continue
        if 0x88 <= reg <= 0x8B:
            voice = reg - 0x88
            ch = st["ch"][voice]
            ch["vol_l"] = (val >> 4) & 0x0F
            ch["vol_r"] = val & 0x0F
            refresh(cid, voice, t)
            continue
        if reg == 0x8C:
            st["sweep_amount"] = val
            refresh(cid, 2, t)
            continue
        if reg == 0x8D:
            st["sweep_time"] = val
            refresh(cid, 2, t)
            continue
        if reg == 0x8E:
            st["noise"] = val
            hit = st["ch"][3]["active"]
            if (val & 0x08) and hit is not None and (
                t - hit.sample_time >= WS_MIN_NOTE
            ):
                # The noise reset bit is written once per percussion hit.
                close(cid, 3, t, off=False)
                open_hit(cid, 3, t)
                continue
            refresh(cid, 3, t)
            continue
        if reg == 0x89 and st["pcm_on"]:
            st["pcm_points"].append((t, val))
            continue

    for cid in chips:
        st = state[cid]
        for voice in range(WS_VOICES):
            close(cid, voice, total, off=False)
        if st["pcm_on"]:
            finish_pcm(cid, total)

    hits = [h for h in hits if h.period > 0]
    hits.sort(key=lambda h: (h.sample_time, h.chip_id, h.voice))
    samples.sort(key=lambda s: (s.chip_id, s.rate, s.data))
    return hits, samples, warnings
