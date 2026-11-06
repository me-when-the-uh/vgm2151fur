"""Nintendo Virtual Boy VSU (VSU-VUE) -> notes, wavetables and macros.

The VSU has six voices: five 32-entry, 6-bit wavetable channels and one
noise channel sharing an LFSR.  The five wavetables are shared; a voice
chooses one through its ``RAM`` register.

VGM command ``0xC7`` carries a 16-bit **big-endian register word index**
(the hardware byte address divided by four), matching Furnace's
``writeS_BE(addr >> 2)``.  Register map::

    0x000-0x09F  wave RAM: five tables of 32 6-bit samples
    0x0A0-0x0BF  modulation table: 32 signed bytes
    0x100 + ch*0x10 + r   channel registers
        0 INTL   key-on bit 7, interval enable bit 5, interval 0-4
        1 LRV    left level (high nibble), right level (low nibble)
        2 FQL    frequency low
        3 FQH    frequency high (3 bits)
        4 EV0    envelope value (high nibble), speed/direction (low)
        5 EV1    bit 0 enable, bit 1 loop, bits 4-6 tap/sweep/mod
        6 RAM    wavetable index (0-4; >4 is silence)
        7 SCW    channel 4 sweep/modulation control
    0x160        reset: bit 0 stops every voice

Playback rate: the hardware advances one wave step every
``2048 - Frequency`` VSU clocks.  Furnace's VBOY platform writes
``2048 - pitchTableValue``, so a note is built from the period table value
``2048 - Frequency`` with the same divider (16) the platform uses.

Envelopes: EV1 bit 0 enables a hardware envelope.  Its value steps every
``(EV0 & 7) + 1`` envelope ticks, one tick being 76,800 VSU clocks.  The
converter replays that state machine into a volume timeline; Furnace then
drives EV0 from a volume macro.  EV1 bits 4-6 are the noise tap (channel 5)
or the sweep/modulation enables (channel 4).

Sweep/modulation on channel 4 is translated to Furnace's native VBOY
effects (``13xx`` sweep, ``14xy`` modulation) and the instrument's FD
modulation table, so the same Mednafen core runs it at its own rate.

A driver may rewrite wave RAM while voices are playing (the ``a64`` and
Galactic Pinball logs show both static and per-tick cases).  Bursts of wave
writes are settled into images; active voices get a wave point at the burst
start.  The analyzer carries those to the pattern as ``10xx`` effects.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.pitch import furnace_period_note
from vgm2151fur.vgm import VGM_RATE, VgmFile

VSU_CHANNELS = 6
VSU_WAVE_CHANNELS = 5
VSU_WAVE_LEN = 32
VSU_WAVE_MAX = 63
VSU_DEFAULT_CLOCK = 5_000_000
VSU_PITCH_DIVIDER = 16
VSU_MAX_PERIOD = 0x7FF
# One 60 Hz frame: drivers write the note's own registers a few ms after the
# gate, exactly like the SCC/C140 paths.
VSU_REFIT_SAMPLES = 735
# Shorter than this is a driver init/glitch gate, not music.
VSU_MIN_NOTE = 100
# Core timing, in VSU clocks.
VSU_EFFECT_CLOCKS = 4800
VSU_INTERVAL_CLOCKS = VSU_EFFECT_CLOCKS * 4
VSU_ENVELOPE_CLOCKS = VSU_EFFECT_CLOCKS * 16
# A wave rewrite is a burst of byte writes; writes within this window belong
# to one settled image.
VSU_WAVE_BURST = 64
# ``10xx`` carries one byte.  Keep the palette inside that limit.
VSU_WAVE_LIMIT = 256


def vsu_clock(vgm: VgmFile, chip_id: int) -> int:
    """Clock for one VSU instance."""
    if chip_id == 1 and vgm.vsu_clock_2:
        return vgm.vsu_clock_2
    return vgm.vsu_clock or VSU_DEFAULT_CLOCK


def vsu_note(clock: int, period: int) -> tuple[int, int]:
    """Period-table value (2048 - Frequency) -> (Furnace note, E5)."""
    period = max(0, min(VSU_MAX_PERIOD, period))
    return furnace_period_note(
        float(clock or VSU_DEFAULT_CLOCK), VSU_PITCH_DIVIDER, period,
        maximum=VSU_MAX_PERIOD,
    )


def _env_tick_samples(clock: int) -> float:
    return VSU_ENVELOPE_CLOCKS * VGM_RATE / float(clock or VSU_DEFAULT_CLOCK)


def _interval_tick_samples(clock: int) -> float:
    return VSU_INTERVAL_CLOCKS * VGM_RATE / float(clock or VSU_DEFAULT_CLOCK)


def _wave_bytes(raw: bytes | bytearray) -> bytes:
    return bytes(v & 0x3F for v in raw)


def _signed32(raw: bytes | bytearray) -> bytes:
    """The raw modulation bytes; Furnace stores them as signed chars."""
    return bytes(v & 0xFF for v in raw)


@dataclass
class VsuHit:
    sample_time: int
    chip_id: int
    voice: int
    # 2048 - Frequency register (the pitch-table form Furnace writes).
    period: int
    vol: int
    pan: int
    wave: int
    tap: int = 0
    mod: int = 0
    sweep: int = 0
    ev1: int = 0
    off_sample: int = 0
    # Mid-note values, all in source sample time.
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    pan_pts: list[tuple[int, int]] = field(default_factory=list)
    pitch_pts: list[tuple[int, int]] = field(default_factory=list)
    wave_pts: list[tuple[int, int]] = field(default_factory=list)
    tap_pts: list[tuple[int, int]] = field(default_factory=list)
    # Channel 4: (sample, SweepControl, EV1).
    sw_pts: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass
class VsuOff:
    sample_time: int
    chip_id: int
    voice: int


@dataclass
class _Voice:
    index: int = 0
    keyed: bool = False
    intl: int = 0
    period: int = 0
    vol: int = 0
    ev0_low: int = 0
    ev1: int = 0
    pan: int = 0
    ram: int = 0
    sweep: int = 0
    active: VsuHit | None = None
    refit_until: int = 0
    auto_end: int = 0
    env_value: int = 0
    env_reload: int = 0
    env_counter: int = 1
    env_next: float = 0.0
    env_mod_mask: int = 0


@dataclass
class _Chip:
    waves: list[bytearray]
    dirty: set[int]
    burst: list[int]
    settled: list[int]
    wave_map: dict[bytes, int]
    wave_images: list[bytes]
    mod: bytearray
    mod_dirty: bool
    mod_burst: int
    mod_settled: int
    mod_map: dict[bytes, int]
    mod_images: list[bytes]
    voices: list[_Voice]
    dynamic: bool = False


def _new_chip() -> _Chip:
    zero = bytes(VSU_WAVE_LEN)
    return _Chip(
        waves=[bytearray(VSU_WAVE_LEN) for _ in range(VSU_WAVE_CHANNELS)],
        dirty=set(),
        burst=[0] * VSU_WAVE_CHANNELS,
        settled=[0] * VSU_WAVE_CHANNELS,
        wave_map={zero: 0},
        wave_images=[zero],
        mod=bytearray(VSU_WAVE_LEN),
        mod_dirty=False,
        mod_burst=0,
        mod_settled=0,
        mod_map={zero: 0},
        mod_images=[zero],
        voices=[_Voice(index=i) for i in range(VSU_CHANNELS)],
    )


def _record_vol(hit: VsuHit | None, when: int, value: int) -> None:
    if hit is None:
        return
    value = max(0, min(15, value))
    if when <= hit.sample_time:
        hit.vol = value
        return
    if hit.vol_pts:
        last_t, last_v = hit.vol_pts[-1]
        if last_t == when:
            hit.vol_pts[-1] = (when, value)
            return
        if last_v == value:
            return
    hit.vol_pts.append((when, value))


def _record_pan(hit: VsuHit | None, when: int, value: int) -> None:
    if hit is None:
        return
    value &= 0xFF
    if when <= hit.sample_time:
        hit.pan = value
        return
    if hit.pan_pts:
        last_t, last_v = hit.pan_pts[-1]
        if last_t == when:
            hit.pan_pts[-1] = (when, value)
            return
        if last_v == value:
            return
    hit.pan_pts.append((when, value))


def _record_period(hit: VsuHit | None, when: int, value: int) -> None:
    if hit is None:
        return
    value = max(0, min(VSU_MAX_PERIOD, value))
    if when <= hit.sample_time:
        hit.period = value
        return
    if hit.pitch_pts:
        last_t, last_v = hit.pitch_pts[-1]
        if last_t == when:
            hit.pitch_pts[-1] = (when, value)
            return
        if last_v == value:
            return
    hit.pitch_pts.append((when, value))


def _record_wave(hit: VsuHit | None, when: int, index: int) -> None:
    if hit is None:
        return
    if when <= hit.sample_time:
        hit.wave = index
        return
    if hit.wave_pts:
        last_t, last_v = hit.wave_pts[-1]
        if last_t == when:
            hit.wave_pts[-1] = (when, index)
            return
        if last_v == index:
            return
    elif hit.wave == index:
        return
    hit.wave_pts.append((when, index))


def _record_tap(hit: VsuHit | None, when: int, value: int) -> None:
    if hit is None:
        return
    value &= 7
    if when <= hit.sample_time:
        hit.tap = value
        return
    if hit.tap_pts:
        last_t, last_v = hit.tap_pts[-1]
        if last_t == when:
            hit.tap_pts[-1] = (when, value)
            return
        if last_v == value:
            return
    elif hit.tap == value:
        return
    hit.tap_pts.append((when, value))


def _record_sw(hit: VsuHit | None, when: int, sweep: int, ev1: int) -> None:
    if hit is None:
        return
    sweep &= 0xFF
    ev1 &= 0xFF
    if when <= hit.sample_time:
        hit.sweep = sweep
        hit.ev1 = ev1
        return
    if hit.sw_pts:
        last_t, last_s, last_e = hit.sw_pts[-1]
        if last_t == when:
            hit.sw_pts[-1] = (when, sweep, ev1)
            return
        if last_s == sweep and last_e == ev1:
            return
    elif hit.sweep == sweep and hit.ev1 == ev1:
        return
    hit.sw_pts.append((when, sweep, ev1))


def _settle(chip: _Chip, _when: int = 0) -> None:
    """Turn finished wave/mod bursts into settled images and points."""
    for w in tuple(chip.dirty):
        img = _wave_bytes(chip.waves[w])
        idx = chip.wave_map.get(img)
        if idx is None:
            idx = len(chip.wave_images)
            chip.wave_map[img] = idx
            chip.wave_images.append(img)
        chip.settled[w] = idx
        burst = chip.burst[w]
        for voice in chip.voices:
            if voice.active is None or voice.ram != w:
                continue
            if burst < voice.active.sample_time:
                continue
            if burst > voice.refit_until:
                chip.dynamic = True
            _record_wave(voice.active, burst, idx)
    chip.dirty.clear()
    if chip.mod_dirty:
        img = _signed32(chip.mod)
        idx = chip.mod_map.get(img)
        if idx is None:
            idx = len(chip.mod_images)
            chip.mod_map[img] = idx
            chip.mod_images.append(img)
        chip.mod_settled = idx
        chip.mod_dirty = False
        burst = chip.mod_burst
        voice = chip.voices[4]
        hit = voice.active
        if hit is not None and burst >= hit.sample_time:
            if burst <= hit.refit_until:
                hit.mod = idx
            elif hit.mod != idx:
                # A live mod-table swap cannot ride the instrument. Keep the
                # first image; the next note will pick up the new one.
                pass


def _env_advance(voice: _Voice, when: int, env_tick: float,
                 interval_tick: float) -> None:
    """Replay the VSU envelope up to `when` (enabled steps only)."""
    hit = voice.active
    if hit is None:
        return
    while voice.env_next <= when + 1e-9:
        tick = voice.env_next
        voice.env_next += env_tick
        if not (voice.ev1 & 0x01):
            # The core leaves EnvelopeCounter alone while the envelope is
            # disabled, but the clock keeps running.
            continue
        voice.env_counter -= 1
        if voice.env_counter > 0:
            continue
        voice.env_counter = (voice.ev0_low & 0x07) + 1
        value = voice.env_value
        up = bool(voice.ev0_low & 0x08)
        if value < 15 and up:
            value += 1
        elif value > 0 and not up:
            value -= 1
        elif (voice.ev1 & 0x02) and voice.env_mod_mask != 2:
            value = voice.env_reload
            voice.env_mod_mask = 0
        elif voice.env_mod_mask == 0:
            voice.env_mod_mask = 1
        if value != voice.env_value:
            voice.env_value = value
            _record_vol(hit, int(round(tick)), value)


def _start(chip: _Chip, voice: _Voice, chip_id: int, t: int,
           env_tick: float, interval_tick: float) -> None:
    if voice.active is not None:
        return
    wave = chip.settled[voice.ram] if voice.ram <= 4 else 0
    mod = chip.mod_settled if voice.index == 4 else 0
    hit = VsuHit(
        sample_time=t,
        chip_id=chip_id,
        voice=voice.index,
        period=2048 - voice.period,
        vol=voice.vol,
        pan=voice.pan,
        wave=wave,
        tap=(voice.ev1 >> 4) & 7 if voice.index == 5 else 0,
        mod=mod,
        sweep=voice.sweep,
        ev1=voice.ev1,
    )
    voice.active = hit
    voice.refit_until = t + VSU_REFIT_SAMPLES
    voice.auto_end = 0
    if voice.intl & 0x20:
        voice.auto_end = t + int(
            round(((voice.intl & 0x1F) + 1) * interval_tick)
        )
    voice.env_value = voice.vol
    voice.env_reload = voice.vol
    voice.env_counter = (voice.ev0_low & 0x07) + 1
    voice.env_next = t + env_tick
    voice.env_mod_mask = 0


def _close(chip: _Chip, voice: _Voice, t: int, hits: list[VsuHit],
           offs: list[VsuOff], *, emit_off: bool, open_end: bool = False
           ) -> None:
    hit = voice.active
    if hit is None:
        return
    voice.active = None
    if open_end:
        off_sample = 0
    else:
        off_sample = t if t > hit.sample_time else 0
    hit.off_sample = off_sample
    if off_sample and off_sample - hit.sample_time < VSU_MIN_NOTE:
        return
    hits.append(hit)
    if emit_off and off_sample:
        offs.append(VsuOff(off_sample, hit.chip_id, hit.voice))


def _advance(voice: _Voice, t: int, env_tick: float,
             interval_tick: float, hits: list[VsuHit],
             offs: list[VsuOff], chip: _Chip) -> None:
    if voice.active is None:
        return
    if voice.auto_end and voice.auto_end <= t:
        end = voice.auto_end
        _env_advance(voice, end, env_tick, interval_tick)
        _close(chip, voice, end, hits, offs, emit_off=True)
        voice.keyed = False
        voice.auto_end = 0
        return
    _env_advance(voice, t, env_tick, interval_tick)


def _cap_wave_images(
    images: list[bytes], hits: list[VsuHit], limit: int,
) -> tuple[list[bytes], int]:
    """Keep the most used images and map the rest to the nearest one.

    ``10xx`` can only name 256 wavetables, and a per-tick wavetable driver
    can settle more than that.  The palette keeps the notes' actual timbres
    where it can and the closest retained waveform elsewhere.
    """
    if len(images) <= limit:
        return images, 0
    counts: Counter[int] = Counter()
    counts[0] = 1
    for h in hits:
        counts[h.wave] += 1
        for _t, idx in h.wave_pts:
            counts[idx] += 1
    keep = sorted(
        range(len(images)),
        key=lambda i: (-counts.get(i, 0), i),
    )[:limit]
    keep_set = set(keep)
    remap: dict[int, int] = {old: i for i, old in enumerate(keep)}
    kept_images = [images[i] for i in keep]
    dropped = [i for i in range(len(images)) if i not in keep_set]
    for old in dropped:
        src = images[old]
        best = min(
            range(len(kept_images)),
            key=lambda j: sum(
                (src[k] - kept_images[j][k]) ** 2
                for k in range(VSU_WAVE_LEN)
            ),
        )
        remap[old] = best
    for h in hits:
        h.wave = remap.get(h.wave, 0)
        if h.wave_pts:
            h.wave_pts = [(t, remap.get(i, 0)) for t, i in h.wave_pts]
    return kept_images, len(dropped)


def collect_vsu(
    vgm: VgmFile,
) -> tuple[list[VsuHit], list[VsuOff], list[bytes], list[bytes],
           bool, list[str]]:
    """Collect VSU notes, key-offs, wave images and mod-table images.

    Returns (hits, offs, wave_images, mod_images, saw_dynamic_waves, warnings).
    """
    warnings: list[str] = []
    writes = [w for w in vgm.writes if w.chip == "vsu"]
    if not writes:
        return [], [], [], [], False, warnings

    chips: dict[int, _Chip] = {}
    for cid in sorted({w.chip_id & 1 for w in writes}):
        chips[cid] = _new_chip()

    hits: list[VsuHit] = []
    offs: list[VsuOff] = []
    ram_silent = 0
    last_wave_write = 0
    last_sample = 0

    for w in writes:
        cid = w.chip_id & 1
        chip = chips.get(cid)
        if chip is None:
            continue
        t = w.sample
        last_sample = max(last_sample, t)
        reg = w.reg & 0x7FFF
        val = w.val & 0xFF
        clock = vsu_clock(vgm, cid)
        env_tick = _env_tick_samples(clock)
        interval_tick = _interval_tick_samples(clock)

        if reg < 0x0A0:
            table = reg >> 5
            if table >= VSU_WAVE_CHANNELS:
                continue
            if table not in chip.dirty:
                chip.dirty.add(table)
                chip.burst[table] = t
            chip.waves[table][reg & 0x1F] = val & 0x3F
            last_wave_write = max(last_wave_write, t)
            continue
        if reg < 0x100:
            if not chip.mod_dirty:
                chip.mod_dirty = True
                chip.mod_burst = t
            chip.mod[reg & 0x1F] = val
            last_wave_write = max(last_wave_write, t)
            continue

        # A non-wave write ends every open wave/mod burst first.
        _settle(chip, t)

        if reg == 0x160:
            for voice in chip.voices:
                _advance(voice, t, env_tick, interval_tick, hits, offs, chip)
                voice.keyed = False
                voice.auto_end = 0
                _close(chip, voice, t, hits, offs, emit_off=True)
            continue
        if reg < 0x100 or reg >= 0x160:
            continue
        ch = (reg - 0x100) >> 4
        r = (reg - 0x100) & 0x0F
        if ch >= VSU_CHANNELS:
            continue
        voice = chip.voices[ch]
        idx = ch

        _advance(voice, t, env_tick, interval_tick, hits, offs, chip)

        if r == 0x0:
            was = voice.keyed
            now = bool(val & 0x80)
            voice.intl = val
            if now and not was:
                # A key-on while a voice is already keyed resets its phase in
                # the core; treat it as a fresh articulation.
                if voice.active is not None:
                    _close(chip, voice, t, hits, offs, emit_off=False)
                voice.keyed = True
                if voice.ram > 4 and idx != 5:
                    ram_silent += 1
                else:
                    _start(chip, voice, cid, t, env_tick, interval_tick)
            elif now and was:
                # Re-key without an intervening key-off: the core resets the
                # phase/counters.  Start a fresh note and cut the old one.
                if voice.active is not None:
                    _close(chip, voice, t, hits, offs, emit_off=False)
                _start(chip, voice, cid, t, env_tick, interval_tick)
            elif not now and was:
                voice.keyed = False
                voice.auto_end = 0
                _close(chip, voice, t, hits, offs, emit_off=True)
            continue

        if not voice.keyed:
            # Volume/frequency/pan writes with the gate off configure the
            # next note.  There is no active hit to update.
            if r == 1:
                voice.pan = val
            elif r == 2:
                voice.period = (voice.period & 0x700) | val
            elif r == 3:
                voice.period = (voice.period & 0xFF) | ((val & 0x07) << 8)
            elif r == 4:
                voice.vol = (val >> 4) & 0x0F
                voice.ev0_low = val & 0x0F
            elif r == 5:
                voice.ev1 = val
            elif r == 6:
                voice.ram = val & 0x0F
            elif r == 7 and idx == 4:
                voice.sweep = val
            continue

        hit = voice.active
        if r == 1:
            voice.pan = val
            if hit is not None and t <= voice.refit_until:
                hit.pan = val
            else:
                _record_pan(hit, t, val)
        elif r == 2:
            voice.period = (voice.period & 0x700) | val
            if hit is not None and t <= voice.refit_until:
                hit.period = 2048 - voice.period
            else:
                _record_period(hit, t, 2048 - voice.period)
        elif r == 3:
            voice.period = (voice.period & 0xFF) | ((val & 0x07) << 8)
            if hit is not None and t <= voice.refit_until:
                hit.period = 2048 - voice.period
            else:
                _record_period(hit, t, 2048 - voice.period)
        elif r == 4:
            new_vol = (val >> 4) & 0x0F
            new_low = val & 0x0F
            old_vol = voice.vol
            if (
                hit is not None
                and t > voice.refit_until
                and new_vol > old_vol
            ):
                # This driver articulates by raising EV0's value while the
                # gate stays on.  It is a new note, not a volume step.
                _close(chip, voice, t, hits, offs, emit_off=False)
                voice.vol = new_vol
                voice.ev0_low = new_low
                voice.env_value = new_vol
                voice.env_reload = new_vol
                voice.env_counter = (new_low & 0x07) + 1
                voice.env_next = t + env_tick
                voice.env_mod_mask = 0
                _start(chip, voice, cid, t, env_tick, interval_tick)
            else:
                voice.vol = new_vol
                voice.ev0_low = new_low
                voice.env_value = new_vol
                voice.env_reload = new_vol
                if voice.env_mod_mask == 1:
                    voice.env_mod_mask = 2
                if hit is not None and t <= voice.refit_until:
                    hit.vol = new_vol
                else:
                    _record_vol(hit, t, new_vol)
        elif r == 5:
            old_tap = (voice.ev1 >> 4) & 7
            old_sw = voice.ev1 & 0x70
            voice.ev1 = val
            if idx == 5:
                tap = (val >> 4) & 7
                if tap != old_tap:
                    if hit is not None and t <= voice.refit_until:
                        hit.tap = tap
                    else:
                        _record_tap(hit, t, tap)
            elif idx == 4 and (val & 0x70) != old_sw:
                if hit is not None and t <= voice.refit_until:
                    hit.sweep = voice.sweep
                    hit.ev1 = val
                else:
                    _record_sw(hit, t, voice.sweep, val)
        elif r == 6:
            old_ram = voice.ram
            voice.ram = val & 0x0F
            if voice.ram != old_ram:
                idx = 0 if voice.ram > 4 else chip.settled[voice.ram]
                if hit is not None and t <= voice.refit_until:
                    hit.wave = idx
                else:
                    _record_wave(hit, t, idx)
        elif r == 7 and idx == 4:
            voice.sweep = val
            if hit is not None and t <= voice.refit_until:
                hit.sweep = val
            else:
                _record_sw(hit, t, val, voice.ev1)

    # Settle the final burst and close voices still sounding.
    end = vgm.total_samples or last_sample
    for cid, chip in chips.items():
        _settle(chip, last_wave_write or end)
        for voice in chip.voices:
            _advance(voice, end, _env_tick_samples(vsu_clock(vgm, cid)),
                     _interval_tick_samples(vsu_clock(vgm, cid)),
                     hits, offs, chip)
            if voice.active is not None:
                _close(chip, voice, end, hits, offs,
                       emit_off=False, open_end=True)

    # Count dynamic wave activity: a settled image for a table that an active
    # voice uses, beyond the five initial images, means the driver morphs.
    wave_images: list[bytes] = []
    wave_index_offset: dict[bytes, int] = {}
    for chip in chips.values():
        for img in chip.wave_images:
            if img not in wave_index_offset:
                wave_index_offset[img] = len(wave_images)
                wave_images.append(img)
    # The per-chip images were indexed independently; remap each hit to the
    # combined list.
    for h in hits:
        chip = chips.get(h.chip_id)
        if chip is None:
            continue
        src = chip.wave_images[h.wave] if h.wave < len(chip.wave_images) else bytes(VSU_WAVE_LEN)
        h.wave = wave_index_offset[src]
        if h.wave_pts:
            h.wave_pts = [
                (t, wave_index_offset[chip.wave_images[i]]
                 if i < len(chip.wave_images) else 0)
                for t, i in h.wave_pts
            ]
    mod_images: list[bytes] = []
    mod_map: dict[bytes, int] = {}
    for chip in chips.values():
        for img in chip.mod_images:
            if img not in mod_map:
                mod_map[img] = len(mod_images)
                mod_images.append(img)
    for h in hits:
        chip = chips.get(h.chip_id)
        if chip is None:
            continue
        src = chip.mod_images[h.mod] if h.mod < len(chip.mod_images) else bytes(VSU_WAVE_LEN)
        h.mod = mod_map[src]

    dynamic = any(chip.dynamic for chip in chips.values())
    if ram_silent:
        warnings.append(
            f"{ram_silent} VSU note(s) selected wavetable RAM 5-15, which "
            "silences the voice; they were not emitted"
        )
    if dynamic:
        warnings.append(
            "VSU wave RAM is rewritten while voices are active; the module "
            "uses live 10xx wave changes and screwThis is set"
        )
    wave_images, dropped = _cap_wave_images(wave_images, hits, VSU_WAVE_LIMIT)
    if dropped:
        warnings.append(
            f"{dropped} VSU wave image(s) exceeded the 10xx palette; the "
            "closest retained waveform was used"
        )
    return hits, offs, wave_images, mod_images, dynamic, warnings
