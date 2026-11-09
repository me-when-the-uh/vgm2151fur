"""Game Boy DMG (LR35902 APU) register stream -> notes and wavetables.

The DMG has four voices: two pulse channels (channel 1 also has a hardware
frequency sweep), one 32-nibble wavetable voice, and a noise channel.  Unlike
the NES, every voice has its own trigger bit (NRx4/NR44 bit 7), so a note
starts on that write.  The envelope lives in NRx2 for the pulse/noise voices,
while the wave voice's level is NR32.

Register map (VGM command ``0xB3``, register ``0x00`` = ``FF10``/NR10):

    0x00    NR10 sweep (ch1)        0x01-0x04 NR11-14 pulse 1
    0x06-0x09 NR21-24 pulse 2       0x0A-0x0E NR30-34 wave
    0x10-0x13 NR41-44 noise         0x14 NR50 master volume
    0x15    NR51 stereo output      0x16 NR52 sound on/off
    0x20-0x2F wave RAM (32 nibbles)

Two envelope encodings appear in real logs.  A driver that writes NRx2 once
at the note start is using the *hardware* envelope (initial volume, direction
and period); that maps straight onto Furnace's GB envelope fields.  A driver
that keeps rewriting NRx2 during the note is using the hardware's "zombie"
volume bug (Furnace's software envelope): the writes are replayed through the
same state machine the emulator uses, and the resulting volume trajectory
becomes a volume macro.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from vgm2151fur.pitch import furnace_period_note
from vgm2151fur.vgm import VgmFile

GB_CHANNELS = 4
GB_DEFAULT_CLOCK = 4194304
# Furnace's GB pitch table: clock / 16, an 11-bit period register, and the
# platform writes ``2048 - freq`` back to NRx3/NRx4.
GB_PITCH_DIVIDER = 16
GB_MAX_PERIOD = 0x7FF
# Shorter than this is a click/housekeeping gate, not a note.
GB_MIN_NOTE = 100
# A pitch write this soon after the trigger is the note's real base, not a
# bend (drivers often set the frequency just after starting the voice).
GB_REFIT_SAMPLES = 735
# An envelope or NR51 write this close to the trigger belongs to that note
# (drivers write them at or a sample or two after the trigger). The window is
# deliberately tiny: a frame-sized one would swallow the next note's setup,
# which lands ~700 samples later in fast percussion.
GB_ENV_REFIT = 16
# Two mid-note NRx2 writes mean the driver is doing a software volume ramp.
GB_SOFT_ENV_WRITES = 2
# A held pitch jump of at least this many 1/64 semitones (0.75 st) is a
# legato note change, not a slide. The pattern gets a note cell there.
GB_LEGATO_UNITS = 48
# A whole-wave RAM rewrite is a burst of byte writes; writes this close belong
# to the same morph step.
GB_WAVE_BURST = 64

REG_SWEEP = 0x00
REG_P1_DUTY = 0x01
REG_P1_ENV = 0x02
REG_P1_LO = 0x03
REG_P1_HI = 0x04
REG_P2_DUTY = 0x06
REG_P2_ENV = 0x07
REG_P2_LO = 0x08
REG_P2_HI = 0x09
REG_W_ON = 0x0A
REG_W_LEN = 0x0B
REG_W_VOL = 0x0C
REG_W_LO = 0x0D
REG_W_HI = 0x0E
REG_N_LEN = 0x10
REG_N_ENV = 0x11
REG_N_FREQ = 0x12
REG_N_CTL = 0x13
REG_VOL = 0x14
REG_PAN = 0x15
REG_ON = 0x16
REG_WAVE = 0x20

# Furnace's noiseTable (gb.cpp).  The index is note-60; the value is the NR43
# register it writes.  Only dividers 4-7 and the shift-0 row are present, which
# is what the platform can express.
_NOISE_TABLE: list[int] = [0]
for _shift in range(15, -1, -1):
    for _div in range(7, 3, -1):
        _NOISE_TABLE.append((_shift << 4) | _div)
_NOISE_TABLE.extend([0x03, 0x02, 0x01, 0x00])


def gb_note(clock: int, period: int) -> tuple[int, int]:
    """11-bit NRx3/NRx4 value -> (Furnace note, E5).

    Furnace writes ``2048 - table_value`` to the register, so the table must
    produce ``2048 - period``.
    """
    period = max(0, min(GB_MAX_PERIOD, period))
    return furnace_period_note(
        clock or GB_DEFAULT_CLOCK, GB_PITCH_DIVIDER, 2048 - period,
        period_offset=0, maximum=GB_MAX_PERIOD,
    )


def gb_period_delta(base: int, new: int) -> int:
    """1/64-semitone change between two NRx3/NRx4 period values."""
    pa = 2048 - max(0, min(GB_MAX_PERIOD, base))
    pb = 2048 - max(0, min(GB_MAX_PERIOD, new))
    if pa <= 0 or pb <= 0:
        return 0
    return int(round(64.0 * 12.0 * math.log2(pa / pb)))


def gb_noise_note(nr43: int) -> int:
    """Furnace's noise column for an NR43 value.

    The platform's noise table is a lookup indexed by ``note - 60``; the note
    name is not a 12-TET pitch.  Each chromatic step moves the hardware rate by
    two to four semitones (and the last few steps by up to an octave), so the
    converter selects the table entry whose period matches NR43 instead of
    transposing by note.

    The table also starts with a duplicate of the fastest rate (period 0.5) at
    C-0, and only covers shift 0-15 with dividers 4-7 (plus the shift-0 row).
    Furnace renders the entries faster than period 8 as a near-DC rumble, not
    noise, so the top of the range is folded onto C-5 (period 8), the fastest
    rate that still reads as noise.  This is what makes the noise note appear
    at C-5 rather than C-0.  Slower values keep their relative pitch below it.
    """
    value = nr43 & 0xF7
    try:
        # Skip the leading duplicate so NR43=0 uses the real end-of-table
        # entry rather than the C-0 alias.
        pos = _NOISE_TABLE.index(value, 1)
    except ValueError:
        want = _noise_period(value)
        pos = min(
            range(len(_NOISE_TABLE)),
            key=lambda i: abs(
                math.log2(max(1e-9, _noise_period(_NOISE_TABLE[i])))
                - math.log2(max(1e-9, want))
            ),
        )
    if pos == 0:
        pos = len(_NOISE_TABLE) - 1
    return 60 + min(pos, 60)


def _noise_period(nr43: int) -> float:
    shift = (nr43 >> 4) & 0x0F
    div = nr43 & 7
    return (div if div else 0.5) * (1 << shift)


def wave_volume(nr32: int) -> int:
    """NR32 output level -> Furnace 0-15 column volume."""
    return (0, 15, 10, 5)[(nr32 >> 5) & 3]


def wave_nibbles(image: bytes | bytearray) -> bytes:
    """16 wave-RAM bytes -> the 32 nibbles Furnace stores, sample order."""
    out = bytearray(32)
    for i in range(16):
        out[i * 2] = (image[i] >> 4) & 0x0F
        out[i * 2 + 1] = image[i] & 0x0F
    return bytes(out)


@dataclass
class GbHit:
    sample_time: int
    voice: int                 # 0/1 pulse, 2 wave, 3 noise
    period: int                # NRx3/NRx4 value, or NR43 for noise
    vol: int                   # 0-15 at note start
    duty: int                  # pulse duty 0-3; noise width 0/1
    wave: int = 0              # wavetable index for voice 2
    env_vol: int = 0
    env_dir: int = 0
    env_len: int = 0
    sound_len: int = 63        # Furnace field (63 = length disabled)
    soft_env: bool = False
    env_raw: int = 0
    off_sample: int = 0
    pitch_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    wave_pts: list[tuple[int, int]] = field(default_factory=list)
    duty_pts: list[tuple[int, int]] = field(default_factory=list)
    pan: int = 0xFF
    pan_pts: list[tuple[int, int]] = field(default_factory=list)
    sweep: int = 0
    sweep_pts: list[tuple[int, int]] = field(default_factory=list)
    # A pitch-only split: the pattern gets a note cell with EAxx legato so
    # the hardware envelope is not restarted.
    legato: bool = False


@dataclass
class GbOff:
    sample_time: int
    voice: int


@dataclass
class _EnvState:
    """The emulator's NRx2 "zombie" volume state."""

    volume: int = 0
    countdown: int = 0
    clock: bool = False
    locked: bool = False


def _nrx2_step(vol: int, value: int, old_value: int, st: _EnvState) -> int:
    """One NRx2 write, using the CGB-E (single-pass) rule.

    The converter tags the chip as a CGB-E so Furnace's own emulator uses the
    same rule.  The full source lives in ``sound/gb/apu.c``.
    """
    if st.clock:
        st.countdown = value & 7
    should_tick = bool((value & 7) and not (old_value & 7) and not st.locked)
    should_invert = bool((value & 8) ^ (old_value & 8))
    if (value & 0xF) == 8 and (old_value & 0xF) == 8 and not st.locked:
        should_tick = True
    if should_invert:
        if value & 8:
            if not (old_value & 7) and not st.locked:
                vol ^= 0xF
            else:
                vol = (0x0E - vol) & 0xF
            should_tick = False
        else:
            vol = (0x10 - vol) & 0xF
    if should_tick:
        vol = (vol + (1 if value & 8 else -1)) & 0xF
    elif not (value & 7) and st.clock:
        if not should_invert:
            if vol == 0xF and (value & 8):
                st.locked = True
            elif vol == 0 and not (value & 8):
                st.locked = True
        elif vol == 1 and not (value & 8):
            st.locked = True
        elif vol == 0x0E and (value & 8):
            st.locked = True
        st.clock = False
    return vol


def _softenv_volumes(
    writes: list[tuple[int, int]], start: int, end: int, env_vol: int,
    env_raw: int = 0,
) -> tuple[list[tuple[int, int]], int]:
    """Replay mid-note NRx2 writes into a coalesced volume timeline.

    ``writes`` is [(sample, value)] for one voice.  ``env_raw`` is the NRx2
    value in force at the trigger.  Returns the (sample, volume) points
    strictly inside (start, end) and the write count.
    """
    st = _EnvState(volume=env_vol, countdown=env_vol & 7)
    vol = env_vol
    old = env_raw
    points: list[tuple[int, int]] = []
    count = 0
    for t, value in writes:
        if not (start < t < end):
            continue
        vol = _nrx2_step(vol, value, old, st)
        old = value
        count += 1
        if points and points[-1][0] == t:
            points[-1] = (t, vol)
        else:
            points.append((t, vol))
    return points, count


def collect_gb(
    vgm: VgmFile, clock: int | None = None,
) -> tuple[list[GbHit], list[GbOff], list[bytes], list[str]]:
    """Pulse, wave and noise notes + the wavetables they use."""
    warnings: list[str] = []
    writes = [w for w in vgm.writes if w.chip == "gb" and w.chip_id == 0]
    if not writes:
        return [], [], [], warnings

    # A merged log can preserve a sample group but not the order of its
    # writes. Some tools emit NRx4/NR44 (the trigger byte) before the period
    # low byte, duty, envelope/DAC or wave level at the same sample; on the
    # chip that trigger sees the previous (often muted) state. Apply every
    # other write at the sample first and the trigger last, matching the
    # YM2612 key-on handling. Correctly ordered logs are unchanged.
    def is_trigger(w) -> bool:
        reg = w.reg & 0x7F
        return bool(w.val & 0x80) and (
            reg in (REG_P1_HI, REG_P2_HI, REG_W_HI) or reg == REG_N_CTL
        )

    writes.sort(key=lambda w: (w.sample, 1 if is_trigger(w) else 0))

    duty = [0, 0]                       # pulse 1/2 duty
    length_val = [0, 0, 0, 0]           # raw length counter
    length_en = [False, False, False, False]
    env = [0, 0, 0, 0]                  # NRx2 / NR42
    period = [0, 0, 0]                  # pulse 1/2, wave
    noise_period = 0
    noise_width = 0
    wave_on = False
    wave_started = False
    wave_vol = 0
    sweep = 0
    pan = [0xFF, 0xFF, 0xFF, 0xFF]
    enabled = True

    active: list[GbHit | None] = [None] * 4
    env_writes: list[list[tuple[int, int]]] = [[] for _ in range(4)]
    hits: list[GbHit] = []
    offs: list[GbOff] = []
    glitches = 0
    prev_period = [0, 0, 0]

    # Wave RAM image. Drivers rewrite only the bytes that changed, so the
    # accumulator is the live image and a note takes whatever it holds when it
    # starts. Treating "all 16 bytes touched since the last image" as a
    # complete update missed partial rewrites and made notes play a stale wave.
    wave_image = bytearray(16)
    wave_index_by_key: dict[bytes, int] = {}
    waves: list[bytes] = []

    def wave_now(t: int, record: bool = False) -> int:
        nib = wave_nibbles(wave_image)
        idx = wave_index_by_key.get(nib)
        if idx is None:
            idx = len(waves)
            wave_index_by_key[nib] = idx
            waves.append(nib)
        if record:
            hit = active[2]
            if hit is not None and t > hit.sample_time:
                if (hit.wave_pts
                        and t - hit.wave_pts[-1][0] <= GB_WAVE_BURST):
                    # A whole-image rewrite is a burst of byte writes; keep the
                    # first time and the settled image.
                    hit.wave_pts[-1] = (hit.wave_pts[-1][0], idx)
                elif not hit.wave_pts or hit.wave_pts[-1][1] != idx:
                    hit.wave_pts.append((t, idx))
        return idx

    def dac_on(voice: int) -> bool:
        if voice == 2:
            return wave_on
        return (env[voice] & 0xF8) != 0

    def voice_audible(voice: int) -> bool:
        """Whether a trigger right now would produce sound.

        Drivers stop a pulse/noise voice by writing NRx2 = 0x08 (DAC on, volume
        0) and re-triggering, or by clearing the DAC. Such a "trigger" is a
        note-off, not a new note: the previous note must get an explicit off.
        A hardware attack from 0 (direction up, period > 0) does rise, so it
        stays audible.
        """
        if voice == 2:
            # A wave trigger with NR32 = 0 is muted; the note starts when the
            # output level rises (see the NR32 branch). A trigger while the
            # level is up starts a normal note.
            return wave_on and wave_volume(wave_vol) > 0
        e = env[voice]
        if (e & 0xF8) == 0:
            return False
        return (e >> 4) > 0 or bool((e & 8) and (e & 7))

    def final_sound_len(voice: int) -> int:
        # Pulses disable length counting at ``soundLen == 63``; the noise
        # platform tests ``soundLen < 64``, so 64 is its "disabled" value.
        # Without that, every noise note is cut after 64 length ticks even
        # when the source never enabled the counter.
        if length_en[voice] and length_val[voice] > 0:
            return 63 - length_val[voice]
        return 64 if voice == 3 else 63

    def close(voice: int, t: int, *, emit_off: bool = True) -> None:
        nonlocal glitches
        hit = active[voice]
        if hit is None:
            return
        hit.off_sample = t
        env_end = t or 10 ** 12
        pts, count = _softenv_volumes(
            env_writes[voice], hit.sample_time, env_end, hit.env_vol,
            hit.env_raw,
        )
        # Only a note whose NRx2 period is zero is using the zombie volume
        # bug. A note with a hardware envelope (period > 0) decays on its own;
        # mid-note decay-rate changes are handled by splitting the note where
        # they happen (see the NRx2 branch below).
        if count >= GB_SOFT_ENV_WRITES and hit.env_len == 0:
            hit.soft_env = True
            hit.vol_pts = list(pts)
        env_writes[voice].clear()
        # A pulse/noise note with volume 0 and no growth is silent. A hardware
        # attack from 0 (direction up, period > 0) does sound, so keep it.
        silent = (
            voice != 2 and hit.vol == 0 and not hit.soft_env
            and not (hit.env_dir and hit.env_len)
        )
        if voice == 2 and hit.vol == 0 and not hit.vol_pts:
            silent = True
        if t <= 0:
            if not silent:
                hits.append(hit)
        elif t - hit.sample_time >= GB_MIN_NOTE:
            if not silent:
                hits.append(hit)
                if emit_off:
                    offs.append(GbOff(t, voice))
        else:
            glitches += 1
        active[voice] = None

    def open_hit(voice: int, t: int, vol_override: int | None = None) -> None:
        env_writes[voice] = []
        if voice == 2:
            if not wave_on:
                return
            index = wave_now(t)
            active[voice] = GbHit(
                sample_time=t, voice=2, period=period[2],
                vol=wave_volume(wave_vol), duty=0, wave=index,
                sound_len=63, pan=pan[2],
            )
            return
        if not dac_on(voice) and vol_override is None:
            return
        start_vol = env[voice] >> 4 if vol_override is None else vol_override
        if voice == 3:
            active[voice] = GbHit(
                sample_time=t, voice=3, period=noise_period,
                vol=start_vol, duty=noise_width,
                env_vol=start_vol, env_dir=(env[3] >> 3) & 1,
                env_len=env[3] & 7, sound_len=final_sound_len(3),
                pan=pan[3], env_raw=env[3],
            )
        else:
            active[voice] = GbHit(
                sample_time=t, voice=voice, period=period[voice],
                vol=start_vol, duty=duty[voice],
                env_vol=start_vol, env_dir=(env[voice] >> 3) & 1,
                env_len=env[voice] & 7, sound_len=final_sound_len(voice),
                pan=pan[voice], sweep=sweep if voice == 0 else 0,
                env_raw=env[voice],
            )
            prev_period[voice] = period[voice]

    def trigger(voice: int, t: int) -> None:
        audible = voice_audible(voice)
        # A silent re-trigger (NRx2 = 0x08 or a cleared wave DAC) is how many
        # drivers stop a voice, so the previous note needs a real off. A real
        # re-trigger replaces the previous note on the same row.
        close(voice, t, emit_off=not audible)
        if audible:
            open_hit(voice, t)

    def record_pitch(voice: int, t: int) -> None:
        hit = active[voice]
        if hit is None or t <= hit.sample_time:
            return
        if period[voice] == prev_period[voice]:
            return
        old_period = prev_period[voice]
        prev_period[voice] = period[voice]
        # A pitch write just after the trigger is the note's real base, not a
        # bend. This matters for a voice started muted whose frequency is set
        # a few samples before the output level rises (Kirby DL2 wave).
        if t - hit.sample_time <= GB_REFIT_SAMPLES and not hit.pitch_pts:
            hit.period = period[voice]
            return
        # A pulse driver that plays a legato melody writes a new NRx3/NRx4
        # without a trigger. A clear jump of about a semitone or more becomes
        # its own note cell, marked legato so Furnace changes pitch without
        # restarting the hardware envelope. Slow slides stay in pitch_pts and
        # become row effects later.
        if (
            voice != 3
            and abs(gb_period_delta(old_period, period[voice]))
            >= GB_LEGATO_UNITS
        ):
            close(voice, t, emit_off=False)
            open_hit(voice, t)
            new_hit = active[voice]
            if new_hit is not None:
                new_hit.legato = True
            return
        # A frequency write without a trigger keeps the hardware envelope
        # running. It is a pitch bend, not a new note: splitting here (an
        # arpeggio, for instance) would restart the envelope at the note's
        # initial volume and hold notes the source lets decay.
        if hit.pitch_pts and hit.pitch_pts[-1][0] == t:
            hit.pitch_pts[-1] = (t, period[voice])
        elif not hit.pitch_pts or hit.pitch_pts[-1][1] != period[voice]:
            hit.pitch_pts.append((t, period[voice]))

    pending_pitch: set[int] = set()
    group_sample: int | None = None

    def flush_pitch(sample: int) -> None:
        # A pitch change is a two-byte write (NRx3 then NRx4). Splitting on
        # the first byte would use the previous high byte and leave the new
        # note on the wrong pitch, so evaluate once per sample group.
        for voice in sorted(pending_pitch):
            record_pitch(voice, sample)
        pending_pitch.clear()

    for w in writes:
        reg = w.reg & 0x7F
        val = w.val & 0xFF
        t = w.sample

        if group_sample is not None and t != group_sample:
            flush_pitch(group_sample)
        group_sample = t

        if reg == REG_SWEEP:
            sweep = val
            hit = active[0]
            if hit is not None and t > hit.sample_time:
                hit.sweep_pts.append((t, val))
        elif reg in (REG_P1_DUTY, REG_P2_DUTY):
            voice = 0 if reg == REG_P1_DUTY else 1
            duty[voice] = (val >> 6) & 3
            length_val[voice] = val & 0x3F
            hit = active[voice]
            if hit is not None and t > hit.sample_time:
                if not hit.duty_pts or hit.duty_pts[-1][1] != duty[voice]:
                    hit.duty_pts.append((t, duty[voice]))
        elif reg in (REG_P1_ENV, REG_P2_ENV, REG_N_ENV):
            voice = {REG_P1_ENV: 0, REG_P2_ENV: 1, REG_N_ENV: 3}[reg]
            env[voice] = val
            hit = active[voice]
            if hit is not None and t > hit.sample_time:
                if (val & 0xF8) == 0:
                    close(voice, t)          # DAC off stops the voice
                    continue
                # Volume 0 with no attack is the driver's silence/kill write
                # (0x08); it is a note-off, not a decay-rate change.
                if (val >> 4) == 0 and not ((val & 8) and (val & 7)):
                    close(voice, t)
                    continue
                if t - hit.sample_time <= GB_ENV_REFIT:
                    # Drivers often write NRx2 just after the trigger (e.g.
                    # Blitz's / Kuma's noise: NR43, trigger, then NR42). That
                    # write is this note's envelope, not a mid-note change.
                    hit.env_vol = val >> 4
                    hit.env_dir = (val >> 3) & 1
                    hit.env_len = val & 7
                    hit.env_raw = val
                    hit.vol = val >> 4
                    continue
                if (
                    hit.env_len == 0
                    and (val >> 4) > 0
                    and (val & 0xF8) != 0
                    and not env_writes[voice]
                    and (val >> 4) >= hit.env_vol
                ):
                    # A mid-note NRx2 write that raises the initial volume
                    # without a trigger is the driver's software attack. It
                    # is a real note start in the pattern, not a zombie volume
                    # step: restart the note at the current pitch and keep the
                    # envelope fields the write carries.
                    close(voice, t, emit_off=False)
                    open_hit(voice, t, vol_override=val >> 4)
                    continue
                if hit.env_len > 0:
                    # A hardware envelope whose period or direction changes
                    # mid-note is how drivers speed a decay up or slow it
                    # down. Split the note at the change; the new segment
                    # starts at the volume the old envelope had reached.
                    new_period = val & 7
                    new_dir = (val >> 3) & 1
                    if (new_period != hit.env_len
                            or new_dir != hit.env_dir):
                        # VGMPlay (and the driver's intent) takes the new
                        # high nibble as the level from here on, so the split
                        # segment starts at it rather than the old envelope's
                        # reached volume.
                        close(voice, t, emit_off=False)
                        open_hit(voice, t, vol_override=val >> 4)
                        continue
                env_writes[voice].append((t, val))
        elif reg in (REG_P1_LO, REG_P2_LO, REG_W_LO):
            voice = {REG_P1_LO: 0, REG_P2_LO: 1, REG_W_LO: 2}[reg]
            period[voice] = (period[voice] & 0x700) | val
            pending_pitch.add(voice)
        elif reg in (REG_P1_HI, REG_P2_HI, REG_W_HI):
            voice = {REG_P1_HI: 0, REG_P2_HI: 1, REG_W_HI: 2}[reg]
            period[voice] = (period[voice] & 0xFF) | ((val & 7) << 8)
            length_en[voice] = bool(val & 0x40)
            if val & 0x80:
                if voice == 2:
                    wave_started = True
                trigger(voice, t)
            else:
                pending_pitch.add(voice)
        elif reg == REG_W_ON:
            was_on = wave_on
            wave_on = bool(val & 0x80)
            length_val[2] = val & 0x3F
            if was_on and not wave_on:
                wave_started = False
                close(2, t)
        elif reg == REG_W_LEN:
            length_val[2] = val
            length_en[2] = bool(val & 0x40)
        elif reg == REG_W_VOL:
            wave_vol = val
            v = wave_volume(val)
            hit = active[2]
            if hit is not None and t > hit.sample_time:
                if v == 0:
                    # NR32 = 0 mutes the voice. Treat it as a note-off so the
                    # next output level starts a fresh note instead of one
                    # enormous note carrying silent volume steps and a long
                    # pitch macro.
                    close(2, t)
                else:
                    last = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
                    if v != last:
                        hit.vol_pts.append((t, v))
            elif v > 0 and wave_started and wave_on:
                # The voice was triggered while muted (Kirby's Dream Land 2
                # sets NR32 = 0, triggers, then raises NR32 a few samples
                # later). The level rising starts the note; the pitch write
                # that follows is folded in as the note's base.
                open_hit(2, t)
        elif reg == REG_N_LEN:
            length_val[3] = val & 0x3F
            length_en[3] = bool(val & 0x40)
        elif reg == REG_N_FREQ:
            noise_period = val
            noise_width = (val >> 3) & 1
            hit = active[3]
            if hit is not None and t > hit.sample_time:
                # NR43 is the noise timbre/pitch. Changing it mid-note is a
                # sweep (a drum body), not a new note: keeping it in one note
                # lets the hardware envelope finish its decay.
                last = (
                    hit.pitch_pts[-1][1] if hit.pitch_pts
                    else (hit.period & 0xF7)
                )
                cur = val & 0xF7
                if cur != last:
                    if hit.pitch_pts and hit.pitch_pts[-1][0] == t:
                        hit.pitch_pts[-1] = (t, cur)
                    else:
                        hit.pitch_pts.append((t, cur))
                if noise_width != hit.duty:
                    if not hit.duty_pts or hit.duty_pts[-1][1] != noise_width:
                        hit.duty_pts.append((t, noise_width))
        elif reg == REG_N_CTL:
            length_en[3] = bool(val & 0x40)
            if val & 0x80:
                trigger(3, t)
        elif reg == REG_PAN:
            for v in range(4):
                left = (val >> (4 + v)) & 1
                right = (val >> v) & 1
                new_pan = (0xF0 if left else 0) | (0x0F if right else 0)
                if new_pan != pan[v]:
                    pan[v] = new_pan
                    hit = active[v]
                    if hit is not None:
                        if t - hit.sample_time <= GB_ENV_REFIT:
                            # The driver writes NR51 at (or just after) the
                            # trigger; that is this note's pan, not a mid-note
                            # move.
                            hit.pan = new_pan
                        elif not hit.pan_pts or hit.pan_pts[-1][1] != new_pan:
                            hit.pan_pts.append((t, new_pan))
        elif reg == REG_ON:
            on = bool(val & 0x80)
            if enabled and not on:
                for v in range(4):
                    close(v, t)
            enabled = on
        elif REG_WAVE <= reg <= REG_WAVE + 15:
            i = reg - REG_WAVE
            wave_image[i] = val
            wave_now(t, record=True)
        # 0x14 master volume: the module mix is normalised separately.

    if group_sample is not None:
        flush_pitch(group_sample)
    for voice in range(4):
        if active[voice] is not None:
            close(voice, 0, emit_off=False)
    # A 16-byte rewrite passes through intermediate images; only the states a
    # note starts on or settles on are real wavetables. Drop the rest and
    # remap the indices so the module stays well inside the 256-wavetable cap.
    used: dict[int, int] = {}
    for h in hits:
        if h.voice != 2:
            continue
        if h.wave not in used:
            used[h.wave] = len(used)
        for _t, idx in h.wave_pts:
            if idx not in used:
                used[idx] = len(used)
    if len(used) != len(waves):
        pruned = [b""] * len(used)
        for old, new in used.items():
            pruned[new] = waves[old]
        for h in hits:
            if h.voice != 2:
                continue
            h.wave = used[h.wave]
            h.wave_pts = [(t, used[idx]) for t, idx in h.wave_pts]
        waves = pruned
    if glitches:
        warnings.append(f"Game Boy: ignored {glitches} sub-frame tone gates")
    return hits, offs, waves, warnings
