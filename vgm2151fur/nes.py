"""NES APU (2A03) register stream -> tone/noise notes and DPCM samples.

The APU has no key-on of its own: a pulse/triangle/noise voice sounds while it
is enabled in ``$4015``, its length counter is loaded or halted, and (for the
pulses and noise) the volume nibble is non-zero.  Like the SN76489 collector,
this keeps one note across same-pitch rewrites and turns mid-note register
changes into pitch/volume/duty points.

Register map (VGM command ``0xB4``, register ``0x00-0x17`` = ``$4000-$4017``):

    0x00/0x04 pulse 1/2 duty+volume+envelope   0x01/0x05 sweep
    0x02/0x06 pulse 1/2 timer low              0x03/0x07 timer high+length
    0x08     triangle linear counter            0x0A/0x0B triangle timer
    0x0C     noise volume+envelope              0x0E/0x0F noise period+length
    0x10-0x13 DPCM rate/level/address/length    0x15     channel enable

``$4011`` is a 7-bit DAC.  A driver can either play 1-bit DPCM samples from
the ``$C000`` RAM (the data-block type ``0xC2``) or bit-bang ``$4011`` at
audio rate, which is the same idea as the SSG volume-register PCM.  Both are
turned into sample instruments here: DPCM bodies keep their 1-bit form, the
bit-bang bodies become 8-bit signed PCM.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from vgm2151fur.pitch import furnace_period_note
from vgm2151fur.vgm import VGM_RATE, VgmFile

NES_CHANNELS = 5
NES_DEFAULT_CLOCK = 1789773  # COLOR_NTSC / 2
# The exact clock Furnace's NES core runs at (COLOR_NTSC / 2).
NES_FURNACE_CLOCK = 315_000_000.0 / 88.0 / 2.0
NES_CPU_DIVIDER = 16
NES_MAX_PERIOD = 0x7FF
# Shorter than this is a click/housekeeping gate, not a note.
NES_MIN_NOTE = 100
# A period change this large is a new note rather than a bend.
NES_NOTE_JUMP_ST = 1.0
# NES drivers re-strike a note by writing a new volume without clearing the
# gate. A jump this large while a note is open is a new hit, not a volume
# step: folding it into the note's instrument would make one macro play
# several source notes. A rise from a very low level to an attack level is
# the same even when the driver leaves the period alone.
NES_RETRIGGER_RISE = 4
NES_RETRIGGER_LOW = 3
# A period write this close to the volume jump belongs to the same hit.
NES_RETRIGGER_WINDOW = 128
# A pulse period that has been held for a while and then steps by this much
# (1/64 semitones) is the next note, not a bend. Vibrato rewrites the period
# every frame, so it never looks like a held step.
NES_STEP_ST = 32
NES_STEP_HOLD = 1500

# NTSC DMC output-bit periods in CPU cycles, indexed by ``$4010 & 0x0F``.
DPCM_PERIODS = (
    428, 380, 340, 320, 286, 254, 226, 214,
    190, 160, 142, 128, 106, 84, 72, 54,
)

# Bit-banged ``$4011`` PCM: same fast-write test as the SSG PCM detector.
PCM_MAX_GAP = 64
PCM_MAX_MEDIAN_DT = 16
PCM_MIN_WRITES = 8
PCM_SPLIT_GAP = 2048
PCM_MIN_RATE = 4000
PCM_MAX_RATE = 32000
PCM_MIN_FRAMES = 128
# Long bit-banged bodies are cut into chunks so a small tracker/sample-rate
# error cannot desync the whole song. Each chunk gets its own sample-map note
# at the source time, and 120 chunks share one map instrument. Furnace sample
# indices are one byte, so keep the chunks inside the same 240-slot budget the
# YM2612 DAC uses; a longer body gets proportionally longer chunks.
NES_PCM_CHUNK_FRAMES = 8192
NES_PCM_MAP_SLOTS = 120
NES_PCM_SAMPLE_BUDGET = 240
# A zero run at least this long (VGM samples) is a rest between phrases, not
# sample data. Split there first, then chunk the sounding parts.
NES_PCM_SILENCE_GAP = PCM_SPLIT_GAP

REG_PULSE1 = (0x00, 0x01, 0x02, 0x03)
REG_PULSE2 = (0x04, 0x05, 0x06, 0x07)


def nes_dpcm_rate(clock: int, index: int) -> int:
    """DMC output sample rate in Hz for a ``$4010`` rate index."""
    period = DPCM_PERIODS[max(0, min(15, index))]
    return max(1, int(round((clock or NES_DEFAULT_CLOCK) / period)))


def nes_period_hz(clock: int, period: int) -> float:
    """Pulse/triangle tone frequency for an 11-bit timer value."""
    return (clock or NES_DEFAULT_CLOCK) / (16.0 * (max(0, period) + 1))


def nes_note(clock: int, period: int) -> tuple[int, int]:
    """11-bit NES timer -> (Furnace note, E5) for the same register.

    Furnace's NES platform computes ``period = pitch_table_value - 1``, so the
    inverse asks the table for ``period + 1``.
    """
    return furnace_period_note(
        clock or NES_DEFAULT_CLOCK, NES_CPU_DIVIDER, max(0, min(NES_MAX_PERIOD, period)),
        period_offset=1, maximum=NES_MAX_PERIOD,
    )


def _period_delta(base: int, new: int) -> int:
    """1/64-semitone change from one NES timer value to another.

    A timer of 0 is a valid (highest) pitch, so the +1 in the ratio is the
    real register value; a move to or from 0 is a large jump, not a bend.
    """
    if base < 0 or new < 0:
        return 0
    return int(round(64.0 * 12.0 * math.log2((base + 1.0) / (new + 1.0))))


def _noise_note(period: int) -> int:
    """Furnace's NES noise column for a ``$400E`` period (properNoiseLayout)."""
    return 60 + (15 - (period & 0x0F))


@dataclass
class NesHit:
    sample_time: int
    voice: int                 # 0/1 pulse, 2 triangle, 3 noise
    period: int                # 11-bit tone timer, 0-15 noise period
    vol: int                   # $4000/$4004/$400C low nibble
    duty: int                  # pulse duty 0-3; noise mode 0-1
    env_mode: int              # (register >> 4) & 3
    linear: int = 0            # triangle $4008
    off_sample: int = 0
    pitch_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    duty_pts: list[tuple[int, int]] = field(default_factory=list)
    env_pts: list[tuple[int, int]] = field(default_factory=list)
    linear_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class NesOff:
    sample_time: int
    voice: int


@dataclass
class NesDpcmSample:
    """One 1-bit DPCM body from the ``$C000`` sample RAM."""

    start: int
    length: int
    data: bytes
    rate_index: int
    loop: bool = False


@dataclass
class NesDpcmHit:
    sample_time: int
    sample_index: int
    rate_index: int
    end_time: int = 0


@dataclass
class NesPcmSample:
    """One 8-bit body from a bit-banged ``$4011`` stream."""

    name: str
    data: bytes
    rate: int
    start: int
    end: int


@dataclass
class NesPcmHit:
    sample_time: int
    end_time: int
    sample_index: int


def _tone_audible(vol: int, env_mode: int) -> bool:
    """Volume nibble is a level (constant mode) or the envelope is running."""
    if env_mode & 1:
        return vol > 0
    return True


def collect_nes_tones(
    vgm: VgmFile, clock: int | None = None,
) -> tuple[list[NesHit], list[NesOff], list[str]]:
    """Pulse, triangle and noise notes from the ``0xB4`` register stream."""
    warnings: list[str] = []
    writes = [w for w in vgm.writes if w.chip == "nes"]
    if not writes:
        return [], [], warnings
    clock = clock or vgm.nes_clock or NES_DEFAULT_CLOCK

    period = [0, 0, 0]
    freq_hi = [False, False, False]
    length = [0, 0, 0, 0]
    vol = [0, 0, 0]           # pulse 1, pulse 2, noise
    duty = [0, 0]             # pulse 1, pulse 2
    env_mode = [0, 0, 0]
    linear = 0
    noise_period = 0
    noise_mode = 0
    enabled = 0x1F
    active: list[NesHit | None] = [None] * 4
    hits: list[NesHit] = []
    offs: list[NesOff] = []
    glitches = 0
    # Volume jumps that may be a re-attack, and the last small period change
    # that may belong to the same hit.
    rise_t = [-1, -1, -1, -1]
    period_change_t = [-1, -1, -1, -1]
    last_period_t = [-1, -1, -1, -1]
    # Last period value acted on. A write of the same value is not a change,
    # while a return to the note's base pitch still is (and must reach the
    # pitch macro).
    prev_period = [0, 0, 0]

    def close(voice: int, t: int, *, emit_off: bool = True) -> None:
        nonlocal glitches
        hit = active[voice]
        if hit is None:
            return
        hit.off_sample = t
        if t <= 0:
            hits.append(hit)
        elif t - hit.sample_time >= NES_MIN_NOTE:
            hits.append(hit)
            if emit_off:
                offs.append(NesOff(t, voice))
        else:
            glitches += 1
        active[voice] = None

    def open_hit(voice: int, t: int) -> None:
        last_period_t[voice] = t
        if voice != 3:
            prev_period[voice] = period[voice]
        if voice == 3:
            active[voice] = NesHit(
                sample_time=t, voice=3, period=noise_period,
                vol=vol[2], duty=noise_mode, env_mode=env_mode[2] & 3,
            )
        else:
            active[voice] = NesHit(
                sample_time=t, voice=voice, period=period[voice],
                vol=vol[voice], duty=duty[voice] if voice < 2 else 0,
                env_mode=env_mode[voice] & 3,
                linear=linear if voice == 2 else 0,
            )

    def evaluate(voice: int, t: int) -> None:
        if voice == 3:
            on = bool(enabled & 0x08) and _tone_audible(vol[2], env_mode[2])
            on = on and bool(env_mode[2] & 2 or length[3] > 0)
        elif voice == 2:
            on = bool(enabled & 0x04) and freq_hi[2]
            on = on and (linear & 0x7F) > 0 and (length[2] > 0 or bool(linear & 0x80))
        else:
            on = bool(enabled & (1 << voice)) and freq_hi[voice]
            on = on and _tone_audible(vol[voice], env_mode[voice])
            on = on and bool(env_mode[voice] & 2 or length[voice] > 0)
        hit = active[voice]
        if on and hit is None:
            open_hit(voice, t)
            return
        if not on:
            if hit is not None:
                close(voice, t)
            return
        assert hit is not None
        if voice == 3:
            if noise_period != hit.period:
                close(voice, t, emit_off=False)
                open_hit(voice, t)
                return
            if noise_mode != hit.duty:
                if t == hit.sample_time:
                    hit.duty = noise_mode
                elif not hit.duty_pts or hit.duty_pts[-1][1] != noise_mode:
                    hit.duty_pts.append((t, noise_mode))
        elif voice == 2:
            if period[2] != prev_period[2]:
                prev_period[2] = period[2]
                if t == hit.sample_time:
                    hit.period = period[2]
                elif abs(_period_delta(hit.period, period[2])) >= int(
                        NES_NOTE_JUMP_ST * 64):
                    close(voice, t, emit_off=False)
                    open_hit(voice, t)
                    return
                elif not hit.pitch_pts or hit.pitch_pts[-1][1] != period[2]:
                    hit.pitch_pts.append((t, period[2]))
            if linear != hit.linear:
                if t == hit.sample_time:
                    hit.linear = linear
                elif not hit.linear_pts or hit.linear_pts[-1][1] != linear:
                    hit.linear_pts.append((t, linear))
        else:
            if period[voice] != prev_period[voice]:
                prev_period[voice] = period[voice]
                change = _period_delta(hit.period, period[voice])
                if t == hit.sample_time:
                    hit.period = period[voice]
                    last_period_t[voice] = t
                elif abs(change) >= int(NES_NOTE_JUMP_ST * 64):
                    close(voice, t, emit_off=False)
                    open_hit(voice, t)
                    return
                elif (abs(change) >= NES_STEP_ST
                      and t - last_period_t[voice] >= NES_STEP_HOLD):
                    # A held pitch step is the next note in a melody, not a
                    # bend. Keeping it in the pitch macro made one instrument
                    # play several source notes.
                    close(voice, t, emit_off=False)
                    open_hit(voice, t)
                    return
                elif (rise_t[voice] >= 0
                      and t - rise_t[voice] <= NES_RETRIGGER_WINDOW):
                    # The volume just jumped and the pitch moved: a new hit.
                    close(voice, t, emit_off=False)
                    open_hit(voice, t)
                    return
                else:
                    if not hit.pitch_pts or hit.pitch_pts[-1][1] != period[voice]:
                        period_change_t[voice] = t
                        hit.pitch_pts.append((t, period[voice]))
                    last_period_t[voice] = t
            if duty[voice] != hit.duty:
                if t == hit.sample_time:
                    hit.duty = duty[voice]
                elif not hit.duty_pts or hit.duty_pts[-1][1] != duty[voice]:
                    hit.duty_pts.append((t, duty[voice]))
        if voice != 2:
            slot = 2 if voice == 3 else voice
            if vol[slot] != hit.vol:
                if t == hit.sample_time:
                    hit.vol = vol[slot]
                elif not hit.vol_pts or hit.vol_pts[-1][1] != vol[slot]:
                    hit.vol_pts.append((t, vol[slot]))
            if (env_mode[slot] & 3) != hit.env_mode:
                if t == hit.sample_time:
                    hit.env_mode = env_mode[slot] & 3
                elif not hit.env_pts or hit.env_pts[-1][1] != (env_mode[slot] & 3):
                    hit.env_pts.append((t, env_mode[slot] & 3))

    def evaluate_all(t: int) -> None:
        for voice in range(4):
            evaluate(voice, t)

    for w in writes:
        reg = w.reg & 0x7F
        val = w.val & 0xFF
        t = w.sample
        if reg == 0x00 or reg == 0x04:
            voice = 0 if reg == 0x00 else 1
            before = vol[voice]
            vol[voice] = val & 0x0F
            env_mode[voice] = (val >> 4) & 3
            duty[voice] = (val >> 6) & 3
            if (active[voice] is not None
                    and t > active[voice].sample_time
                    and vol[voice] - before >= NES_RETRIGGER_RISE):
                # A re-attack: the driver raised the level from (near) silence
                # without clearing the gate, or moved the pitch at the same
                # time. Start a new note instead of turning several source
                # notes into one long volume/pitch macro.
                rise_t[voice] = t
                recent = (
                    period_change_t[voice] >= 0
                    and t - period_change_t[voice] <= NES_RETRIGGER_WINDOW
                )
                if before <= NES_RETRIGGER_LOW or recent:
                    close(voice, t, emit_off=False)
                    open_hit(voice, t)
            evaluate(voice, t)
        elif reg == 0x02 or reg == 0x06:
            voice = 0 if reg == 0x02 else 1
            period[voice] = (period[voice] & 0x700) | val
            # Some drivers write the timer high byte before the low one.  The
            # high write then opened the note on the previous low byte; the
            # low write is the one that completes this pitch.
            if freq_hi[voice]:
                evaluate(voice, t)
        elif reg == 0x03 or reg == 0x07:
            voice = 0 if reg == 0x03 else 1
            period[voice] = (period[voice] & 0xFF) | ((val & 7) << 8)
            length[voice] = (val >> 3) & 0x1F
            freq_hi[voice] = True
            evaluate(voice, t)
        elif reg == 0x08:
            linear = val
            evaluate(2, t)
        elif reg == 0x0A:
            period[2] = (period[2] & 0x700) | val
            if freq_hi[2]:
                evaluate(2, t)
        elif reg == 0x0B:
            period[2] = (period[2] & 0xFF) | ((val & 7) << 8)
            length[2] = (val >> 3) & 0x1F
            freq_hi[2] = True
            evaluate(2, t)
        elif reg == 0x0C:
            vol[2] = val & 0x0F
            env_mode[2] = (val >> 4) & 3
            evaluate(3, t)
        elif reg == 0x0E:
            noise_period = val & 0x0F
            noise_mode = (val >> 7) & 1
            evaluate(3, t)
        elif reg == 0x0F:
            length[3] = (val >> 3) & 0x1F
            evaluate(3, t)
        elif reg == 0x15:
            enabled = val & 0x1F
            evaluate_all(t)
        # 0x01/0x05 sweep, 0x09/0x0D unused, 0x10-0x13 DPCM, 0x17 frame ctl

    for voice in range(4):
        if active[voice] is not None:
            close(voice, 0, emit_off=False)
    if glitches:
        warnings.append(f"NES APU: ignored {glitches} sub-frame tone gates")
    return hits, offs, warnings


def _dpcm_dumps(vgm: VgmFile) -> list[tuple[int, int, bytes]]:
    """(stream time, address, data) for every DPCM RAM dump, in order.

    A driver may reload the same 4 KB window while the song plays, so the
    image is only valid from the block's position in the stream. The old code
    applied every block up front, which left each hit playing whatever the
    last block happened to contain.
    """
    out = [
        (getattr(s, "time", 0), s.start & 0xFFFF, s.data)
        for s in vgm.roms if s.kind == "nes_dpcm"
    ]
    out.sort(key=lambda d: d[0])
    return out


def _apply_dpcm_bank(
    ram: bytearray, covered: bytearray, vgm: VgmFile,
    ram_writes: list, upto: int, cursor: list[int],
) -> None:
    """Replay 0x68 bank copies whose time has arrived."""
    bank = bytes(getattr(vgm, "nes_dpcm_bank", b""))
    while cursor[0] < len(ram_writes) and ram_writes[cursor[0]].sample <= upto:
        r = ram_writes[cursor[0]]
        cursor[0] += 1
        chunk = bank[r.src:r.src + r.length]
        for i, b in enumerate(chunk):
            addr = (r.dest + i) & 0xFFFF
            ram[addr] = b
            covered[addr] = 1


def collect_nes_dpcm(
    vgm: VgmFile, clock: int | None = None,
) -> tuple[list[NesDpcmSample], list[NesDpcmHit], list[str]]:
    """Standard DPCM playback: ``$4012``/``$4013`` + ``$4015`` bit 4."""
    warnings: list[str] = []
    writes = [w for w in vgm.writes if w.chip == "nes"]
    if not writes:
        return [], [], warnings
    clock = clock or vgm.nes_clock or NES_DEFAULT_CLOCK
    # 64 KB CPU-address image of the dumped DPCM RAM (0xAA = silence). RAM
    # dumps and 0x68 bank copies are replayed in stream order, so a hit reads
    # the bank that was loaded when the driver triggered it.
    ram = bytearray(b"\xAA" * 0x10000)
    covered = bytearray(0x10000)
    dumps = _dpcm_dumps(vgm)
    dump_cursor = [0]

    def apply_dumps(upto: int) -> None:
        while dump_cursor[0] < len(dumps) and dumps[dump_cursor[0]][0] <= upto:
            _t, start, data = dumps[dump_cursor[0]]
            dump_cursor[0] += 1
            for i, b in enumerate(data):
                addr = (start + i) & 0xFFFF
                ram[addr] = b
                covered[addr] = 1

    bank_writes = sorted(
        (r for r in getattr(vgm, "pcm_ram_writes", ())
         if r.type == 7),          # NES DPCM database
        key=lambda r: r.sample,
    )
    bank_cursor = [0]
    has_data = bool(bank_writes or any(s.kind == "nes_dpcm" for s in vgm.roms))

    rate = 0
    loop_bit = False
    addr_reg = 0
    len_reg = 0
    playing = False
    armed = False
    hits: list[NesDpcmHit] = []
    samples: list[NesDpcmSample] = []
    by_key: dict[tuple[int, int, bool, bytes], int] = {}
    undumped = 0
    starts = 0

    for w in writes:
        reg = w.reg & 0x7F
        val = w.val & 0xFF
        t = w.sample
        apply_dumps(t)
        _apply_dpcm_bank(ram, covered, vgm, bank_writes, t, bank_cursor)
        if reg == 0x10:
            rate = val & 0x0F
            loop_bit = bool(val & 0x40)
        elif reg == 0x12:
            addr_reg = val
        elif reg == 0x13:
            len_reg = val
            armed = True
        elif reg == 0x15:
            bit = bool(val & 0x10)
            if bit and not playing and armed:
                addr = (0xC000 + (addr_reg << 6)) & 0xFFFF
                length = (len_reg << 4) + 1
                playing = True
                starts += 1
                covered_bytes = sum(
                    1 for i in range(length) if covered[(addr + i) & 0xFFFF]
                )
                if covered_bytes == 0:
                    undumped += 1
                body = bytes(ram[(addr + i) & 0xFFFF] for i in range(length))
                # The same address/length can hold different data after a
                # bank reload, so the body is part of the identity.
                key = (addr, length, loop_bit, body)
                idx = by_key.get(key)
                if idx is None:
                    idx = len(samples)
                    by_key[key] = idx
                    samples.append(NesDpcmSample(
                        start=addr, length=length, data=body,
                        rate_index=rate, loop=loop_bit,
                    ))
                hits.append(NesDpcmHit(
                    sample_time=t, sample_index=idx, rate_index=rate,
                ))
            elif not bit and playing:
                playing = False
                if hits:
                    hits[-1].end_time = t
    if starts and not has_data:
        warnings.append(
            f"{starts} NES DPCM trigger(s) but no DPCM data block or RAM dump; "
            "those notes are silent"
        )
    elif undumped:
        warnings.append(
            f"{undumped} NES DPCM sample(s) point at RAM the VGM does not dump; "
            "those notes are silent"
        )
    return samples, hits, warnings


def _median_dt(points: list[tuple[int, int]]) -> int:
    if len(points) < 2:
        return 0
    dts = sorted(
        points[i + 1][0] - points[i][0] for i in range(len(points) - 1)
    )
    return dts[len(dts) // 2]


def _pcm_bursts(points: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """High-rate ``$4011`` writes grouped into bodies."""
    out: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    prev = None
    for point in points:
        if prev is not None and point[0] - prev <= PCM_MAX_GAP:
            cur.append(point)
        else:
            if len(cur) >= PCM_MIN_WRITES and _median_dt(cur) <= PCM_MAX_MEDIAN_DT:
                out.append(cur)
            cur = [point]
        prev = point[0]
    if len(cur) >= PCM_MIN_WRITES and _median_dt(cur) <= PCM_MAX_MEDIAN_DT:
        out.append(cur)
    return out


def _pcm_resample(points: list[tuple[int, int]], rate: int) -> bytes:
    """Sample-and-hold the 7-bit DAC on a uniform timeline (signed 8-bit)."""
    t0 = points[0][0]
    t1 = points[-1][0]
    span = max(1, t1 - t0)
    frames = max(1, int(round(span * rate / VGM_RATE)) + 1)
    out = bytearray(frames)
    i = 0
    cur = points[0][1]
    step = VGM_RATE / rate
    for k in range(frames):
        t = t0 + k * step
        while i + 1 < len(points) and points[i + 1][0] <= t:
            i += 1
            cur = points[i][1]
        # Furnace's NES PCM path writes (data8 + 0x80) >> 1 to $4011.
        out[k] = ((cur << 1) - 0x80) & 0xFF
    return bytes(out)


def _pcm_sounding_pieces(data: bytes, rate: int) -> list[tuple[int, bytes]]:
    """Split resampled PCM at long zero runs; return (frame offset, body)."""
    gap = max(1, int(round(NES_PCM_SILENCE_GAP * rate / VGM_RATE)))
    out: list[tuple[int, bytes]] = []
    start = 0
    i = 0
    n = len(data)
    while i < n:
        if data[i] != 0:
            i += 1
            continue
        j = i
        while j < n and data[j] == 0:
            j += 1
        if j - i >= gap:
            if i > start:
                out.append((start, data[start:i]))
            start = j
        i = j
    if start < n:
        out.append((start, data[start:]))
    return out


def collect_nes_pcm(
    vgm: VgmFile, clock: int | None = None,
) -> tuple[list[NesPcmSample], list[NesPcmHit], list[str]]:
    """Direct ``$4011`` bit-bang PCM (the NES equivalent of SSG volume PCM)."""
    warnings: list[str] = []
    writes = [w for w in vgm.writes if w.chip == "nes" and (w.reg & 0x7F) == 0x11]
    if not writes:
        return [], [], warnings
    points: list[tuple[int, int]] = []
    for w in writes:
        value = w.val & 0x7F
        if points and points[-1][0] == w.sample:
            points[-1] = (w.sample, value)
        else:
            points.append((w.sample, value))
    bursts = _pcm_bursts(points)
    if not bursts:
        return [], [], warnings
    # Join short holes, then split at real pauses.
    merged: list[list[tuple[int, int]]] = []
    for burst in bursts:
        if burst and merged and burst[0][0] - merged[-1][-1][0] < PCM_SPLIT_GAP:
            merged[-1].extend(burst)
        elif burst:
            merged.append(list(burst))
    prepared: list[tuple[list[tuple[int, int]], int, int, bytes]] = []
    for seg in merged:
        if len(seg) < 2:
            continue
        median = _median_dt(seg)
        if median <= 0:
            continue
        rate = max(PCM_MIN_RATE, min(PCM_MAX_RATE, int(round(VGM_RATE / median))))
        data = _pcm_resample(seg, rate)
        if len(data) < PCM_MIN_FRAMES:
            continue
        for base_off, piece in _pcm_sounding_pieces(data, rate):
            if piece:
                prepared.append((seg, rate, base_off, piece))
    if not prepared:
        return [], [], warnings
    total_frames = sum(len(piece) for _seg, _rate, _off, piece in prepared)
    chunk_frames = NES_PCM_CHUNK_FRAMES
    if total_frames > chunk_frames * NES_PCM_SAMPLE_BUDGET:
        chunk_frames = int(math.ceil(
            total_frames / float(NES_PCM_SAMPLE_BUDGET),
        ))
    samples: list[NesPcmSample] = []
    hits: list[NesPcmHit] = []
    by_data: dict[bytes, int] = {}
    for seg, rate, base_off, data in prepared:
        total = len(data)
        for off in range(0, total, chunk_frames):
            part = data[off:off + chunk_frames]
            if not part:
                continue
            start_t = seg[0][0] + int(round(
                (base_off + off) * VGM_RATE / rate,
            ))
            final = off + len(part) >= total
            end_t = seg[0][0] + int(round(
                (base_off + total) * VGM_RATE / rate,
            ))
            idx = by_data.get(part)
            if idx is None:
                idx = len(samples)
                by_data[part] = idx
                samples.append(NesPcmSample(
                    name=f"NES PCM {idx:02d}", data=part, rate=rate,
                    start=start_t, end=end_t,
                ))
            hits.append(NesPcmHit(
                sample_time=start_t,
                # Non-final chunks are cut by the next chunk's note. The last
                # chunk of each sounding piece gets the OFF that covers the
                # following silent gap.
                end_time=end_t if final else 0,
                sample_index=idx,
            ))
    return samples, hits, warnings
