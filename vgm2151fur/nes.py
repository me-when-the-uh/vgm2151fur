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
from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.pitch import furnace_period_note
from vgm2151fur.samplecluster import cluster_fragments
from vgm2151fur.vgm import VGM_RATE, VgmFile

NES_CHANNELS = 5
NES_DEFAULT_CLOCK = 1789773  # COLOR_NTSC / 2
# The exact clock Furnace's NES core runs at (COLOR_NTSC / 2).
NES_FURNACE_CLOCK = 315_000_000.0 / 88.0 / 2.0
NES_CPU_DIVIDER = 16
NES_MAX_PERIOD = 0x7FF
# Shorter than this is a click/housekeeping gate, not a note.
NES_MIN_NOTE = 100
# The APU length-counter table: the 5-bit load index maps to a number of
# half-frames (decremented at 120 Hz).  Index 0 is 10, so a driver that writes
# ``$400F = 0`` to mark a noise hit still plays an 83 ms burst, exactly as the
# console does.
NES_LENGTH_TABLE = (
    10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 120, 12, 240, 14,
    1, 18, 2, 20, 4, 22, 6, 24, 8, 26, 10, 28, 12, 30, 14, 32,
)
# VGM samples per frame-sequencer tick: the length counter runs at 120 Hz
# (half frames) and the triangle's linear counter at 240 Hz (quarter frames).
SAMPLES_PER_HALF_FRAME = VGM_RATE / 120.0
SAMPLES_PER_QUARTER_FRAME = VGM_RATE / 240.0
# A timer this low renders above ~16 kHz, which the console's output filter
# drops.  Some drivers park an unused voice at timer 0 between phrases, so a
# write there is silence, not the top of the tracker's note range.
NES_MIN_PERIOD = 6
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
    # A dual-APU log interleaves two chips on one command, so every note keeps
    # the instance its register file belonged to.
    chip_id: int = 0
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
    chip_id: int = 0


@dataclass
class NesDpcmSample:
    """One 1-bit DPCM body from the ``$C000`` sample RAM."""

    start: int
    length: int
    data: bytes
    rate_index: int
    loop: bool = False
    chip_id: int = 0


@dataclass
class NesDpcmHit:
    sample_time: int
    sample_index: int
    rate_index: int
    end_time: int = 0
    chip_id: int = 0


@dataclass
class NesPcmSample:
    """One 8-bit body from a bit-banged ``$4011`` stream."""

    name: str
    data: bytes
    rate: int
    start: int
    end: int
    chip_id: int = 0


@dataclass
class NesPcmHit:
    sample_time: int
    end_time: int
    sample_index: int
    chip_id: int = 0


def _nes_writes_by_chip(vgm: VgmFile) -> dict[int, list]:
    """The ``0xB4`` stream split by APU instance (register bit 7).

    Two APUs share one command byte, so every collector keeps their register
    files apart: a shared machine lets one chip's write close the other's open
    gate and puts ten voices' notes on five columns.
    """
    out: dict[int, list] = {}
    for w in vgm.writes:
        if w.chip == "nes":
            out.setdefault(w.chip_id & 1, []).append(w)
    return out


def _tone_audible(vol: int, env_mode: int) -> bool:
    """Volume nibble is a level (constant mode) or the envelope is running."""
    if env_mode & 1:
        return vol > 0
    return True


def collect_nes_tones(
    vgm: VgmFile, clock: int | None = None,
) -> tuple[list[NesHit], list[NesOff], list[str]]:
    """Pulse, triangle and noise notes from the ``0xB4`` register stream.

    A dual-APU log (header bit 30, register bit 7) interleaves two independent
    register files on the same command, so each instance runs its own machine.
    One shared machine would let chip 2's ``$4000`` rewrite chip 1's open pulse
    gate and collapse both APUs onto the same five columns. ``clock`` is kept
    for symmetry with the other collectors; a hit carries raw timer values and
    the caller turns them into notes.
    """
    warnings: list[str] = []
    by_cid = _nes_writes_by_chip(vgm)
    if not by_cid:
        return [], [], warnings

    hits: list[NesHit] = []
    offs: list[NesOff] = []
    glitches = 0
    total = vgm.total_samples or 0
    for cid in sorted(by_cid):
        chip_hits, chip_offs, chip_glitches = _nes_tones_one(
            by_cid[cid], cid, total,
        )
        hits.extend(chip_hits)
        offs.extend(chip_offs)
        glitches += chip_glitches
    hits.sort(key=lambda h: (h.sample_time, h.chip_id, h.voice))
    offs.sort(key=lambda o: (o.sample_time, o.chip_id, o.voice))
    if glitches:
        warnings.append(f"NES APU: ignored {glitches} sub-frame tone gates")
    return hits, offs, warnings


def _nes_tones_one(
    writes: list, chip_id: int, total: int = 0,
) -> tuple[list[NesHit], list[NesOff], int]:
    """The voice machine of one 2A03 instance.

    `writes` are already limited to that instance and in stream order. Returns
    (hits, offs, dropped sub-frame gates).
    """
    period = [0, 0, 0]
    freq_hi = [False, False, False]
    length = [0, 0, 0, 0]     # half-frames loaded, 0 = counter never loaded
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
    # Absolute sample at which the current gate's counters run out, 0 = held.
    # A length reload that arrives after it starts a new hit; one that arrives
    # before it just extends the same note (drivers that hold a voice by
    # rewriting the length register every frame).
    gate_end = [0, 0, 0, 0]
    # Volume jumps that may be a re-attack, and the last small period change
    # that may belong to the same hit.
    rise_t = [-1, -1, -1, -1]
    period_change_t = [-1, -1, -1, -1]
    last_period_t = [-1, -1, -1, -1]
    # Last period value acted on. A write of the same value is not a change,
    # while a return to the note's base pitch still is (and must reach the
    # pitch macro).
    prev_period = [0, 0, 0]

    def gate_end_at(voice: int, t: int) -> int:
        """Sample where a voice's own counter closes the gate, 0 for a held one.

        The reference player gates each noise hit by its length counter (a
        ``$400F`` write of 0 loads the table's 10 half-frames, an 83 ms burst)
        and each triangle note by its linear counter ((value/240) s), while
        pulse notes keep sounding until the driver's next write.  Drivers mark
        a hit by rewriting those registers, so a reload that arrives after the
        gate ran out starts a new note and one that arrives before it extends
        the same note.
        """
        if voice == 3:
            if env_mode[2] & 2 or length[3] == 0:
                return 0
            return t + int(round(length[3] * SAMPLES_PER_HALF_FRAME))
        if voice == 2:
            if linear & 0x80 or (linear & 0x7F) == 0:
                return 0
            return t + int(round((linear & 0x7F) * SAMPLES_PER_QUARTER_FRAME))
        return 0

    def close(voice: int, t: int, *, emit_off: bool = True) -> None:
        nonlocal glitches
        hit = active[voice]
        if hit is None:
            return
        hit.off_sample = t
        gate_end[voice] = 0
        if t <= 0:
            hits.append(hit)
        elif t - hit.sample_time >= NES_MIN_NOTE:
            hits.append(hit)
            if emit_off:
                offs.append(NesOff(t, voice, chip_id))
        else:
            glitches += 1
        active[voice] = None

    def open_hit(voice: int, t: int) -> None:
        last_period_t[voice] = t
        if voice != 3:
            prev_period[voice] = period[voice]
        gate_end[voice] = gate_end_at(voice, t)
        if voice == 3:
            active[voice] = NesHit(
                sample_time=t, voice=3, period=noise_period,
                vol=vol[2], duty=noise_mode, env_mode=env_mode[2] & 3,
                chip_id=chip_id,
            )
        else:
            active[voice] = NesHit(
                sample_time=t, voice=voice, period=period[voice],
                vol=vol[voice], duty=duty[voice] if voice < 2 else 0,
                env_mode=env_mode[voice] & 3,
                chip_id=chip_id,
                linear=linear if voice == 2 else 0,
            )

    def evaluate(voice: int, t: int, *, rearm: bool = False) -> None:
        # The voice's own counter may already have closed this note.  End it at
        # that time, so the pattern gets the note-off where the chip went quiet,
        # and let this write start the next note.  A reload that arrives while
        # the gate is still open only extends the same note (drivers hold a
        # voice by rewriting the length register every frame).
        if (active[voice] is not None and gate_end[voice]
                and gate_end[voice] <= t):
            close(voice, gate_end[voice])
        elif rearm and active[voice] is not None:
            gate_end[voice] = gate_end_at(voice, t)
        if voice == 3:
            on = bool(enabled & 0x08) and _tone_audible(vol[2], env_mode[2])
            on = on and bool(env_mode[2] & 2 or length[3] > 0)
        elif voice == 2:
            on = bool(enabled & 0x04) and freq_hi[2]
            on = on and period[2] >= NES_MIN_PERIOD
            on = on and (linear & 0x7F) > 0 and (length[2] > 0 or bool(linear & 0x80))
        else:
            on = bool(enabled & (1 << voice)) and freq_hi[voice]
            on = on and period[voice] >= NES_MIN_PERIOD
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

    def timer_high_follows(index: int, low_reg: int, t: int) -> bool:
        """True when the next write completes this timer update.

        A driver that writes the timer low byte first leaves a stale high byte
        for a sample or two.  Evaluating that half-updated period turns a real
        note into a spurious bend or split: Santa Claus writes 270 -> 0 as
        low 0x00 (270 & 0x700 = 256) then high 0xC0, and 256 is a held step
        that closed the note without a note-off.  Let the high write evaluate
        the combined period instead.
        """
        high_reg = {0x02: 0x03, 0x06: 0x07, 0x0A: 0x0B}[low_reg]
        for nxt in writes[index + 1:index + 4]:
            if nxt.sample > t + 8:
                break
            if (nxt.reg & 0x7F) == high_reg:
                return True
        return False

    for index, w in enumerate(writes):
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
            # low write is the one that completes this pitch.  The other order
            # leaves a stale high byte for a sample or two, so wait for the
            # high write when one follows.
            if freq_hi[voice] and not timer_high_follows(index, reg, t):
                evaluate(voice, t)
        elif reg == 0x03 or reg == 0x07:
            voice = 0 if reg == 0x03 else 1
            period[voice] = (period[voice] & 0xFF) | ((val & 7) << 8)
            length[voice] = NES_LENGTH_TABLE[(val >> 3) & 0x1F]
            freq_hi[voice] = True
            evaluate(voice, t, rearm=True)
        elif reg == 0x08:
            linear = val
            evaluate(2, t)
        elif reg == 0x0A:
            period[2] = (period[2] & 0x700) | val
            if freq_hi[2] and not timer_high_follows(index, reg, t):
                evaluate(2, t)
        elif reg == 0x0B:
            period[2] = (period[2] & 0xFF) | ((val & 7) << 8)
            length[2] = NES_LENGTH_TABLE[(val >> 3) & 0x1F]
            freq_hi[2] = True
            evaluate(2, t, rearm=True)
        elif reg == 0x0C:
            vol[2] = val & 0x0F
            env_mode[2] = (val >> 4) & 3
            evaluate(3, t)
        elif reg == 0x0E:
            noise_period = val & 0x0F
            noise_mode = (val >> 7) & 1
            evaluate(3, t)
        elif reg == 0x0F:
            length[3] = NES_LENGTH_TABLE[(val >> 3) & 0x1F]
            evaluate(3, t, rearm=True)
        elif reg == 0x15:
            enabled = val & 0x1F
            evaluate_all(t)
        # 0x01/0x05 sweep, 0x09/0x0D unused, 0x10-0x13 DPCM, 0x17 frame ctl

    for voice in range(4):
        if active[voice] is None:
            continue
        end = gate_end[voice]
        if end and (not total or end <= total):
            # A percussion gate that ran out before the stream did: end the
            # note there so the pattern shows the hit's real length.
            close(voice, end)
        else:
            close(voice, 0, emit_off=False)
    return hits, offs, glitches


def _dpcm_dumps(vgm: VgmFile, chip_id: int = 0) -> list[tuple[int, int, bytes]]:
    """(stream time, address, data) for one APU's DPCM RAM dumps, in order.

    Every 2A03 has its own ``$C000`` sample RAM, so a type-``0xC2`` dump only
    belongs to the instance named by bit 31 of its length. A driver may reload
    the same 4 KB window while the song plays, so the image is only valid from
    the block's position in the stream. The old code applied every block up
    front, which left each hit playing whatever the last block happened to
    contain.
    """
    out = [
        (getattr(s, "time", 0), s.start & 0xFFFF, s.data)
        for s in vgm.roms
        if s.kind == "nes_dpcm" and (getattr(s, "chip", 0) or 0) == chip_id
    ]
    out.sort(key=lambda d: d[0])
    return out


def _apply_dpcm_bank(
    ram: bytearray, covered: bytearray, vgm: VgmFile,
    ram_writes: list, upto: int, cursor: list[int], base: int = 0,
) -> None:
    """Replay this instance's 0x68 bank copies whose time has arrived.

    `base` is where this APU's own type-``0x07`` database starts inside the
    concatenated bank. A dual-APU log carries one database per chip and every
    0x68 source address is relative to the chip it names, so a shared bank
    would read the other cart's samples.
    """
    bank = bytes(getattr(vgm, "nes_dpcm_bank", b""))
    while cursor[0] < len(ram_writes) and ram_writes[cursor[0]].sample <= upto:
        r = ram_writes[cursor[0]]
        cursor[0] += 1
        start = base + r.src
        chunk = bank[start:start + r.length]
        for i, b in enumerate(chunk):
            addr = (r.dest + i) & 0xFFFF
            ram[addr] = b
            covered[addr] = 1


def collect_nes_dpcm(
    vgm: VgmFile, clock: int | None = None,
) -> tuple[list[NesDpcmSample], list[NesDpcmHit], list[str]]:
    """Standard DPCM playback: ``$4012``/``$4013`` + ``$4015`` bit 4.

    Both instances read the module's one sample list, so a body two APUs play
    is stored once and each hit says which chip plays it. ``clock`` is accepted
    for symmetry with the other collectors; a hit carries its own ``$4010``
    rate index and the writer turns that into a Hz value.
    """
    warnings: list[str] = []
    by_cid = _nes_writes_by_chip(vgm)
    if not by_cid:
        return [], [], warnings

    samples: list[NesDpcmSample] = []
    hits: list[NesDpcmHit] = []
    # The same address/length can hold different data after a bank reload, and
    # the two chips share one body when they play the same window.
    by_key: dict[tuple[int, int, bool, bytes], int] = {}
    starts = 0
    undumped = 0
    has_data = (
        any(r.type == 7 for r in getattr(vgm, "pcm_ram_writes", ()))
        or any(s.kind == "nes_dpcm" for s in vgm.roms)
    )
    for cid in sorted(by_cid):
        chip_starts, chip_undumped = _collect_dpcm_chip(
            vgm, by_cid[cid], cid, samples, hits, by_key,
        )
        starts += chip_starts
        undumped += chip_undumped

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


def _collect_dpcm_chip(
    vgm: VgmFile, writes: list, chip_id: int,
    samples: list[NesDpcmSample], hits: list[NesDpcmHit],
    by_key: dict[tuple[int, int, bool, bytes], int],
) -> tuple[int, int]:
    """One APU's DMC voice. Returns (triggers, triggers with no dumped data)."""
    # 64 KB CPU-address image of the dumped DPCM RAM (0xAA = silence). RAM
    # dumps and 0x68 bank copies are replayed in stream order, so a hit reads
    # the bank that was loaded when the driver triggered it.
    ram = bytearray(b"\xAA" * 0x10000)
    covered = bytearray(0x10000)
    dumps = _dpcm_dumps(vgm, chip_id)
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
         if r.type == 7 and (r.chip_id or 0) == chip_id),  # NES DPCM database
        key=lambda r: r.sample,
    )
    bank_cursor = [0]
    base = int(getattr(vgm, "nes_dpcm_bank_base", {}).get(chip_id, 0))

    rate = 0
    loop_bit = False
    addr_reg = 0
    len_reg = 0
    playing = False
    armed = False
    last_hit: NesDpcmHit | None = None
    undumped = 0
    starts = 0

    for w in writes:
        reg = w.reg & 0x7F
        val = w.val & 0xFF
        t = w.sample
        apply_dumps(t)
        _apply_dpcm_bank(ram, covered, vgm, bank_writes, t, bank_cursor, base)
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
                key = (addr, length, loop_bit, body)
                idx = by_key.get(key)
                if idx is None:
                    idx = len(samples)
                    by_key[key] = idx
                    samples.append(NesDpcmSample(
                        start=addr, length=length, data=body,
                        rate_index=rate, loop=loop_bit, chip_id=chip_id,
                    ))
                last_hit = NesDpcmHit(
                    sample_time=t, sample_index=idx, rate_index=rate,
                    chip_id=chip_id,
                )
                hits.append(last_hit)
            elif not bit and playing:
                playing = False
                # Only this chip's own last trigger stops.
                if last_hit is not None:
                    last_hit.end_time = t
    return starts, undumped


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
    """Direct ``$4011`` bit-bang PCM (the NES equivalent of SSG volume PCM).

    A dual-APU log bit-bangs either chip, and a burst is one chip's stream: two
    interleaved drivers would look like one burst with doubled gaps and be
    thrown away. So the writes are grouped per instance first, while the chunk
    budget and the body list stay shared. ``clock`` is kept for symmetry with
    the other collectors; a ``$4011`` stream's rate comes from its own write
    cadence.
    """
    warnings: list[str] = []
    streams: dict[int, list[tuple[int, int]]] = {}
    for w in vgm.writes:
        if w.chip != "nes" or (w.reg & 0x7F) != 0x11:
            continue
        points = streams.setdefault(w.chip_id & 1, [])
        value = w.val & 0x7F
        if points and points[-1][0] == w.sample:
            points[-1] = (w.sample, value)
        else:
            points.append((w.sample, value))
    if not streams:
        return [], [], warnings
    # Join short holes, then split at real pauses.
    merged: list[tuple[int, list[tuple[int, int]]]] = []
    for cid in sorted(streams):
        bursts = _pcm_bursts(streams[cid])
        chip_merged: list[list[tuple[int, int]]] = []
        for burst in bursts:
            if (burst and chip_merged
                    and burst[0][0] - chip_merged[-1][-1][0] < PCM_SPLIT_GAP):
                chip_merged[-1].extend(burst)
            elif burst:
                chip_merged.append(list(burst))
        merged.extend((cid, seg) for seg in chip_merged)
    if not merged:
        return [], [], warnings
    prepared: list[tuple[int, list[tuple[int, int]], int, int, bytes]] = []
    for cid, seg in merged:
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
                prepared.append((cid, seg, rate, base_off, piece))
    if not prepared:
        return [], [], warnings
    total_frames = sum(len(piece) for _c, _seg, _rate, _off, piece in prepared)
    chunk_frames = NES_PCM_CHUNK_FRAMES
    if total_frames > chunk_frames * NES_PCM_SAMPLE_BUDGET:
        chunk_frames = int(math.ceil(
            total_frames / float(NES_PCM_SAMPLE_BUDGET),
        ))
    dual = len(streams) > 1
    samples: list[NesPcmSample] = []
    hits: list[NesPcmHit] = []
    by_data: dict[bytes, int] = {}
    for cid, seg, rate, base_off, data in prepared:
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
                    name=(f"NES PCM {cid + 1}-{idx:02d}" if dual
                          else f"NES PCM {idx:02d}"),
                    data=part, rate=rate,
                    start=start_t, end=end_t, chip_id=cid,
                ))
            hits.append(NesPcmHit(
                sample_time=start_t,
                # Non-final chunks are cut by the next chunk's note. The last
                # chunk of each sounding piece gets the OFF that covers the
                # following silent gap.
                end_time=end_t if final else 0,
                sample_index=idx, chip_id=cid,
            ))
    return samples, hits, warnings


def cluster_nes_pcm(
    samples: list[NesPcmSample],
    hits: list[NesPcmHit],
    *,
    target: int = 32,
    hot: int = 16,
) -> tuple[list[NesPcmSample], list[NesPcmHit]]:
    """Optionally fold short NES raw-PCM bodies into the hottest/longest ones."""
    if len(samples) <= target:
        return samples, hits
    uses: Counter[int] = Counter(hit.sample_index for hit in hits)
    keep, remap = cluster_fragments(
        [smp.data for smp in samples],
        [smp.rate for smp in samples],
        dict(uses),
        target=target,
        hot=hot,
    )
    if len(keep) == len(samples):
        return samples, hits
    new_samples = [samples[i] for i in keep]
    for hit in hits:
        hit.sample_index = remap[hit.sample_index]
    return new_samples, hits
