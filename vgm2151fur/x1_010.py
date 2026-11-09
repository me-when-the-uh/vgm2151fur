"""Seta/Allumer X1-010: wavetable voices and 8-bit PCM bodies.

VGM 1.71 carries the clock at header 0xD8 and writes the chip with ``0xC8``:
a 16-bit big-endian register/memory offset followed by one byte.  The first
0x80 bytes are the sixteen channel register blocks, 0x80-0xFFF is envelope
shape RAM (32 shapes of 128 bytes), 0x1000-0x1FFF is waveform RAM (32 shapes
of 128 bytes), and data block 0x91 supplies the 1 MB sample ROM.

Each voice is either an LFSR-style wavetable voice or an 8-bit PCM voice.
Wave voices read a 128-byte signed waveform selected by register 1 and a
128-byte hardware envelope selected by register 5.  PCM voices read
``start*0x1000 .. (0x100-end)*0x1000`` from the ROM at
``clock/8192 * frequency`` Hz.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vgm2151fur.vgm import VGM_RATE, X1_010_DEFAULT_CLOCK, VgmFile, assemble_rom

X1_CHANNELS = 16
# Furnace's DivPlatformX1_010 uses this as the pitch-table frequency base.
X1_FREQ_DIV = 4_194_304
X1_MAX_FREQ = 0xFFFF
X1_WAVE_BYTES = 128
X1_ENV_BYTES = 128
X1_ROM_SIZE = 0x100000

REG_STATUS = 0
REG_VOL_WAVE = 1
REG_FREQ_LO = 2
REG_FREQ_HI = 3
REG_START_ENVPERIOD = 4
REG_END_ENVSHAPE = 5


def _s8(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value >= 0x80 else value


def x1_wave_reg(freq: int) -> int:
    """Wave-mode register value after the status bit-7 divider."""
    return max(1, freq)


def x1_pcm_freq(freq: int) -> int:
    """PCM frequency register.  The core substitutes 4 for zero."""
    return freq or 4


def x1_pcm_rate(clock: int, freq: int) -> int:
    return max(1, int(round(clock / 8192.0 * x1_pcm_freq(freq))))


@dataclass
class X1Hit:
    """One wavetable voice note."""

    sample_time: int
    chip_id: int
    voice: int
    freq: int  # 16-bit pitch register at the gate
    env_period: int
    env_shape: int  # source envelope number (0..31)
    env_oneshot: bool
    wave: int  # source waveform number (0..31)
    clock: int
    wave_data: bytes
    env_data: bytes
    off_sample: int = 0
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    wave_pts: list[tuple[int, bytes]] = field(default_factory=list)
    env_pts: list[tuple[int, bytes]] = field(default_factory=list)
    env_period_pts: list[tuple[int, int]] = field(default_factory=list)
    env_mode_pts: list[tuple[int, int]] = field(default_factory=list)
    # Period-0 envelopes hold envelope byte 0.  Some drivers rewrite that byte
    # while a note sounds, so keep the resulting mono level steps.
    env_level_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class X1PcmHit:
    """One PCM voice note with its exact ROM range."""

    sample_time: int
    chip_id: int
    voice: int
    start: int
    end: int
    freq: int
    volume: int  # mono average of the two register nibbles, 0..15
    pan: int  # raw register byte, for diagnostics
    data: bytes
    clock: int
    off_sample: int = 0
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)

    @property
    def rate(self) -> int:
        return x1_pcm_rate(self.clock, self.freq)

    @property
    def natural_end(self) -> int:
        """Source sample where the voice keys itself off at the ROM end.

        The X1-010 clears status bit 0 when PCM playback reaches the end
        address. A later key-on write is then a real 0 -> 1 edge even when
        the register byte still reads 1, which is how drivers retrigger a
        sample without writing an explicit off first.
        """
        frames = len(self.data)
        return self.sample_time + int(round(frames * VGM_RATE / self.rate))


@dataclass
class X1Off:
    sample_time: int
    chip_id: int
    voice: int


def _blank_channels() -> list[bytearray]:
    return [bytearray(8) for _ in range(X1_CHANNELS)]


def collect_x1_010(
    vgm: VgmFile,
) -> tuple[list[X1Hit], list[X1PcmHit], list[X1Off], list[str]]:
    """Walk an X1-010 register stream.

    Returns ``(wave_hits, pcm_hits, offs, warnings)``.  Wave/envelope contents
    are snapshotted when a note starts; the handful of unique bodies is
    deduplicated by the analyzer.  PCM bodies are the exact source address
    range, so long phrases are never shortened by silence trimming.
    """
    warnings: list[str] = []
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "x1_010"})
    if not chips:
        return [], [], [], warnings
    clock = vgm.x1_010_clock or X1_010_DEFAULT_CLOCK

    roms = {
        cid: assemble_rom(vgm, "x1_010", cid, fill=0)
        for cid in chips
    }


    regs = {cid: _blank_channels() for cid in chips}
    wave_ram = {cid: bytearray(0x1000) for cid in chips}
    env_ram = {cid: bytearray(0x1000) for cid in chips}
    active: dict[tuple[int, int], X1Hit | X1PcmHit | None] = {
        (cid, ch): None for cid in chips for ch in range(X1_CHANNELS)
    }
    hits: list[X1Hit] = []
    pcm_hits: list[X1PcmHit] = []
    offs: list[X1Off] = []
    warnings_seen: dict[str, int] = {
        "short": 0,
        "range": 0,
        "volume": 0,
    }

    def close(cid: int, ch: int, t: int, *, emit_off: bool = True) -> None:
        hit = active[(cid, ch)]
        if hit is None:
            return
        hit.off_sample = t
        if hit.sample_time >= t:
            warnings_seen["short"] += 1
        else:
            if isinstance(hit, X1Hit):
                hits.append(hit)
            else:
                pcm_hits.append(hit)
            if emit_off and t < (vgm.total_samples or 0):
                offs.append(X1Off(t, cid, ch))
        active[(cid, ch)] = None

    def open_hit(cid: int, ch: int, t: int, status: int) -> None:
        reg = regs[cid][ch]
        mode_wave = bool(status & 0x02)
        div = 1 if status & 0x80 else 0
        if mode_wave:
            freq = ((reg[REG_FREQ_HI] << 8) | reg[REG_FREQ_LO]) >> div
            wave = reg[REG_VOL_WAVE] & 0x1F
            shape = reg[REG_END_ENVSHAPE] & 0x1F
            start = wave * X1_WAVE_BYTES
            env_start = shape * X1_ENV_BYTES
            active[(cid, ch)] = X1Hit(
                sample_time=t,
                chip_id=cid,
                voice=ch,
                freq=max(1, freq),
                env_period=reg[REG_START_ENVPERIOD],
                env_shape=shape,
                env_oneshot=bool(status & 0x04),
                wave=wave,
                clock=clock,
                wave_data=bytes(wave_ram[cid][start:start + X1_WAVE_BYTES]),
                env_data=bytes(env_ram[cid][env_start:env_start + X1_ENV_BYTES]),
            )
            return
        start = reg[REG_START_ENVPERIOD] * 0x1000
        end = (0x100 - reg[REG_END_ENVSHAPE]) * 0x1000
        rom = roms[cid]
        if not rom:
            warnings_seen["no_rom"] = warnings_seen.get("no_rom", 0) + 1
            active[(cid, ch)] = None
            return
        if start >= end or start >= len(rom):
            warnings_seen["range"] += 1
            active[(cid, ch)] = None
            return
        end = min(end, len(rom))
        body = bytes(rom[start:end])
        volume_byte = reg[REG_VOL_WAVE]
        left = (volume_byte >> 4) & 0x0F
        right = volume_byte & 0x0F
        if left != right:
            warnings_seen["volume"] += 1
        active[(cid, ch)] = X1PcmHit(
            sample_time=t,
            chip_id=cid,
            voice=ch,
            start=start,
            end=end,
            freq=x1_pcm_freq(reg[REG_FREQ_LO] >> div),
            volume=(left + right + 1) // 2,
            pan=volume_byte,
            data=body,
            clock=clock,
        )

    for w in vgm.writes:
        if w.chip != "x1_010":
            continue
        cid = w.chip_id
        t = w.sample
        addr = w.reg & 0x1FFF
        val = w.val & 0xFF
        if addr >= 0x1000:
            wave_ram[cid][addr & 0x0FFF] = val
            # A waveform rewrite while a note uses it is audible on hardware.
            # Keep the original snapshot for now and report the limitation.
            for ch in range(X1_CHANNELS):
                hit = active[(cid, ch)]
                if isinstance(hit, X1Hit):
                    start = hit.wave * X1_WAVE_BYTES
                    if start <= (addr & 0x0FFF) < start + X1_WAVE_BYTES:
                        warnings_seen["wave_live"] = (
                            warnings_seen.get("wave_live", 0) + 1
                        )
            continue
        if addr >= 0x80:
            env_off = addr & 0x0FFF
            env_ram[cid][env_off] = val
            level = (((val >> 4) & 0x0F) + (val & 0x0F) + 1) // 2
            for ch in range(X1_CHANNELS):
                hit = active[(cid, ch)]
                if not isinstance(hit, X1Hit):
                    continue
                start = hit.env_shape * X1_ENV_BYTES
                if start <= env_off < start + X1_ENV_BYTES:
                    if env_off == start and hit.env_period == 0:
                        last = (
                            hit.env_level_pts[-1][1]
                            if hit.env_level_pts
                            else ((((hit.env_data[0] >> 4) & 0x0F)
                                   + (hit.env_data[0] & 0x0F) + 1) // 2)
                        )
                        if level != last:
                            hit.env_level_pts.append((t, level))
                    else:
                        warnings_seen["env_live"] = (
                            warnings_seen.get("env_live", 0) + 1
                        )
            continue
        ch = addr >> 3
        reg = addr & 7
        old_status = regs[cid][ch][REG_STATUS]
        regs[cid][ch][reg] = val
        hit = active[(cid, ch)]
        if reg == REG_STATUS:
            was_on = bool(old_status & 0x01)
            now_on = bool(val & 0x01)
            if isinstance(hit, X1PcmHit) and t >= hit.natural_end:
                # PCM playback keys itself off at the ROM end address even
                # though the status byte still reads 1. A later key-on write
                # is therefore a real 0 -> 1 edge: drivers retrigger a sample
                # without writing an explicit off first.
                close(cid, ch, hit.natural_end, emit_off=not now_on)
                hit = None
                was_on = False
            if now_on and not was_on:
                close(cid, ch, t, emit_off=False)
                open_hit(cid, ch, t, val)
                continue
            if not now_on and was_on:
                close(cid, ch, t)
                continue
            if not now_on:
                continue
            if isinstance(hit, X1Hit):
                one_shot = bool(val & 0x04)
                if one_shot != hit.env_oneshot:
                    hit.env_oneshot = one_shot
                    hit.env_mode_pts.append(
                        (t, (1 | (2 if one_shot else 0)))
                    )
                if bool(val & 0x80) != (old_status & 0x80 != 0):
                    # The divider changed mid-note.  Recompute from the
                    # registers that are already in force.
                    freq = (
                        (regs[cid][ch][REG_FREQ_HI] << 8)
                        | regs[cid][ch][REG_FREQ_LO]
                    ) >> (1 if val & 0x80 else 0)
                    if freq != (hit.freq_pts[-1][1] if hit.freq_pts else hit.freq):
                        hit.freq_pts.append((t, max(1, freq)))
            continue
        if hit is None:
            continue
        if isinstance(hit, X1Hit):
            if reg == REG_FREQ_LO or reg == REG_FREQ_HI:
                status = regs[cid][ch][REG_STATUS]
                freq = (
                    (regs[cid][ch][REG_FREQ_HI] << 8)
                    | regs[cid][ch][REG_FREQ_LO]
                ) >> (1 if status & 0x80 else 0)
                last = hit.freq_pts[-1][1] if hit.freq_pts else hit.freq
                if freq != last:
                    hit.freq_pts.append((t, max(1, freq)))
            elif reg == REG_VOL_WAVE:
                wave = val & 0x1F
                last = hit.wave_pts[-1][1] if hit.wave_pts else hit.wave_data
                start = wave * X1_WAVE_BYTES
                body = bytes(wave_ram[cid][start:start + X1_WAVE_BYTES])
                if body != last:
                    hit.wave = wave
                    hit.wave_pts.append((t, body))
            elif reg == REG_END_ENVSHAPE:
                shape = val & 0x1F
                last = hit.env_pts[-1][1] if hit.env_pts else hit.env_data
                start = shape * X1_ENV_BYTES
                body = bytes(env_ram[cid][start:start + X1_ENV_BYTES])
                if body != last:
                    hit.env_shape = shape
                    hit.env_pts.append((t, body))
            elif reg == REG_START_ENVPERIOD:
                last = (
                    hit.env_period_pts[-1][1]
                    if hit.env_period_pts else hit.env_period
                )
                if val != last:
                    hit.env_period_pts.append((t, val))
        else:
            if reg == REG_FREQ_LO:
                freq = x1_pcm_freq(val)
                last = hit.freq_pts[-1][1] if hit.freq_pts else hit.freq
                if freq != last:
                    hit.freq_pts.append((t, freq))
            elif reg == REG_VOL_WAVE:
                left = (val >> 4) & 0x0F
                right = val & 0x0F
                if left != right:
                    warnings_seen["volume"] += 1
                volume = (left + right + 1) // 2
                last = hit.vol_pts[-1][1] if hit.vol_pts else hit.volume
                if volume != last:
                    hit.vol_pts.append((t, volume))
            elif reg == REG_START_ENVPERIOD or reg == REG_END_ENVSHAPE:
                warnings_seen["range"] += 1

    for cid in chips:
        for ch in range(X1_CHANNELS):
            close(cid, ch, vgm.total_samples or 0, emit_off=False)

    if warnings_seen.get("no_rom"):
        warnings.append(
            "X1-010 PCM note(s) have no 0x91 sample ROM block; "
            "their bodies are missing"
        )
    if warnings_seen["short"]:
        warnings.append(
            f"{warnings_seen['short']} zero-length X1-010 gate(s) skipped"
        )
    if warnings_seen["range"]:
        warnings.append(
            f"{warnings_seen['range']} X1-010 PCM write(s) changed the address "
            "range while a note played; the initial range is used"
        )
    if warnings_seen["volume"]:
        warnings.append(
            f"{warnings_seen['volume']} X1-010 PCM volume write(s) have "
            "different left/right nibbles; the mono average is used"
        )
    if warnings_seen.get("wave_live"):
        warnings.append(
            f"{warnings_seen['wave_live']} X1-010 waveform RAM write(s) "
            "changed a waveform while it was playing; the note-start snapshot "
            "is used"
        )
    if warnings_seen.get("env_live"):
        warnings.append(
            f"{warnings_seen['env_live']} X1-010 envelope RAM write(s) "
            "changed a shape while it was playing; the note-start snapshot is "
            "used"
        )
    return hits, pcm_hits, offs, warnings
