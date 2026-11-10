"""AY-3-8910 (and the YM2149-compatible register set) -> notes -> Furnace.

Register model follows MAME/libvgm and matches Furnace's AY core:

    0/1, 2/3, 4/5  tone period, 12-bit, low byte then high nibble
    6              noise period (5-bit)
    7              mixer: bits 0-2 tone enables, 3-5 noise enables (a clear
                   bit enables), 6/7 I/O port directions
    8/9/10         per-channel volume: low nibble = level, bit 4 = hardware
                   envelope instead of the level
    11/12          envelope period, 16-bit
    13             envelope shape
    14/15          I/O port data (not converted)

A tone channel sounds at ``clock / (16 * period)`` (period 0 reads as 1).
There is no key-on register: the gate is the volume (0 = off) or the mixer
(tone and noise both disabled = off).  MSX and Spectrum drivers use the volume
as the gate for percussion and retrigger the hardware envelope for drum
decays, so a new note starts when the voice becomes audible or the envelope
is re-armed.  A period change of a semitone or more that holds for a frame
is its own note; a smaller change stays on the note as vibrato or a slide.
A period below the pitch table's range mutes the voice.

Furnace's AY platform writes the pitch table's value straight into the period
registers, so a source period round-trips through ``pitch.furnace_period_note``.
Channel mode (tone/noise/envelope), noise frequency and the hardware envelope
are pattern effects (20xx/21xx/22xy/23xx/24xx), which is how the platform
exposes them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vgm2151fur.pitch import (
    NOTE_MAX, furnace_period_note, module_clock, pitch_table,
)
from vgm2151fur.vgm import VgmFile

AY_VOICES = 3
AY_DEFAULT_CLOCK = 1789772  # COLOR_NTSC/2, the MSX/ZX Spectrum PSG clock
# Gates shorter than this are driver housekeeping, not music.
AY_MIN_NOTE = 100
# A loud gate level followed by the real level this soon is one note.
AY_GATE_PAIR = 1000
# ...but only when the drop is large enough to be a gate-opening trick.  A
# percussion attack decays by a step or two per frame (three or four on a
# roll), so absorbing the first frame would throw away the peak and make the
# transient too quiet.
AY_GATE_DROP = 6
# A shape write this soon after the gate belongs to the note's opening
# register burst. Anything later re-arms the envelope and is its own hit.
AY_ENV_OPEN = 64
# A level jump this large while the gate stays open is a retriggered drum
# (a snare roll writes the next hit straight from the decayed level back up).
# Without it the whole roll stayed one note with a volume macro.
AY_RETRIGGER_RISE = 4

REG_TONE_LO = (0, 2, 4)
REG_TONE_HI = (1, 3, 5)
REG_VOL = (8, 9, 10)
REG_NOISE = 6
REG_MIXER = 7
REG_ENV_LO = 11
REG_ENV_HI = 12
REG_ENV_SHAPE = 13

# Furnace effect numbers (platform/ay.cpp, sysDef ayPostEffectHandlerMap).
FX_MODE = 0x20  # channel mode: bits tone 1, noise 2, envelope 4 (value - 1)
FX_NOISE = 0x21  # noise frequency, value = 31 - register
FX_ENV_SET = 0x22  # xy = shape, channel enable
FX_ENV_LO = 0x23
FX_ENV_HI = 0x24


def ay_rate(clock: int, period: int) -> float:
    """Tone frequency for a period register, in Hz (period 0 reads as 1)."""
    return (clock or AY_DEFAULT_CLOCK) / (16.0 * max(1, period))


def ay_note(clock: int, period: int, divider: int = 8) -> tuple[int, int]:
    """Period register -> (Furnace note, E5 fine tune).

    ``divider`` is the Furnace pitch-table divider: 8 for a stand-alone
    AY-8910, 16 for the YM2203's SSG (Furnace runs that core with extDiv 16).
    """
    return furnace_period_note(
        clock=module_clock(clock),
        divider=divider,
        value=max(1, period),
    )


def ay_mode(tone: bool, noise: bool, envelope: bool) -> int:
    """Furnace effect value for a channel mode: bits tone 1, noise 2, env 4."""
    return (1 if tone else 0) | (2 if noise else 0) | (4 if envelope else 0)


@dataclass
class AyHit:
    sample_time: int
    chip_id: int
    voice: int
    period: int  # 12-bit tone period at the gate
    vol: int  # volume register low nibble
    env: bool  # volume register bit 4: the envelope drives this channel
    tone: bool  # mixer: tone enabled (follows later mixer writes)
    opened_tone: bool  # tone bit at the gate; later mixer writes leave this
    noise: bool  # mixer: noise enabled
    noise_period: int  # register 6 (5-bit)
    env_shape: int  # register 13
    env_period: int  # registers 11/12, 16-bit
    off_sample: int = 0  # gate-off sample (0 = still sounding at the end)
    # When a retrigger closes this note silently and then the new gate turns
    # out to be a sub-frame glitch, this holds the last note that really
    # sounded. Its key-off has to be emitted at the glitch's close, or the
    # predecessor rings until the voice's next note.
    split_from: "AyHit | None" = None
    # Mid-note register writes while the voice sounds.
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    # Furnace effects the note needs: (sample, effect, value).  The note's own
    # mode/noise/envelope writes are in here at the note's own sample.
    fx_pts: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass
class AyOff:
    sample_time: int
    chip_id: int
    voice: int


# The mixer's power-up value is undefined on a real AY. A zeroed image
# enables noise on all three voices, which turns a song's opening notes into
# tone-plus-hiss wherever the driver writes pitch and volume before its
# first $07. Tone-only (this value) matches the first mixer byte those
# drivers write ($BA / $B8, noise disabled on every voice). A driver that
# wants noise says so there. Every other register stays at reset.
REG_MIXER_DEFAULT = 0x38


def _blank_chip() -> dict:
    reg = [0] * 16
    reg[REG_MIXER] = REG_MIXER_DEFAULT
    return {
        "reg": reg,
        "active": [None] * AY_VOICES,
    }


def _tone_on(reg: list[int], voice: int) -> bool:
    return not (reg[REG_MIXER] & (1 << voice))


def _noise_on(reg: list[int], voice: int) -> bool:
    return not (reg[REG_MIXER] & (1 << (voice + 3)))


def ay_min_period(clock: int, divider: int = 8) -> int:
    """Smallest period the module can express for this clock.

    The pitch table tops out at B-9, and drivers mute a channel by writing a
    period below that (an inaudible tone): the SCC-style "silence gate".
    Treating it as silence keeps those housekeeping writes from becoming
    notes with a pitch nothing can represent.
    """
    tbl = pitch_table(module_clock(clock or AY_DEFAULT_CLOCK), divider)
    return max(2, tbl.value(NOTE_MAX, 127))


def _audible(reg: list[int], voice: int, min_period: int = 0) -> bool:
    """True when the voice can be heard with the current registers."""
    tone = _tone_on(reg, voice)
    if tone:
        period = reg[REG_TONE_LO[voice]] | ((reg[REG_TONE_HI[voice]] & 0x0F) << 8)
        tone = period >= max(1, min_period)
    if not (tone or _noise_on(reg, voice)):
        # The AY mixes tone and noise into the DAC; with both disabled the
        # channel outputs a constant at the volume level. A *fixed* level is
        # DC and inaudible, but the hardware envelope makes that level an
        # audio-rate waveform (the `$0D` decay heard under a muted voice).
        # Keep the note alive while the envelope drives it.
        return bool(reg[REG_VOL[voice]] & 0x10)
    if reg[REG_VOL[voice]] & 0x10:
        return True  # the hardware envelope drives this channel
    return (reg[REG_VOL[voice]] & 0x0F) > 0


def collect_ay8910(
    vgm: VgmFile,
    *,
    chip: str = "ay8910",
    clock: int | None = None,
    divider: int = 8,
) -> tuple[list[AyHit], list[AyOff], list[str]]:
    """Walk an AY-compatible register stream into notes.

    ``chip`` selects the write stream (the YM2203's SSG uses ``ym2203_ssg``),
    and ``divider`` is the Furnace pitch-table divider (8 for an AY-8910,
    16 for the YM2203 SSG).  Returns (hits, key-offs, warnings); each hit is
    one gate interval of one voice with everything the writer needs.
    """
    warnings: list[str] = []
    hits: list[AyHit] = []
    offs: list[AyOff] = []
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == chip})
    if not chips:
        return [], [], warnings
    state: dict[int, dict] = {cid: _blank_chip() for cid in chips}
    acc: Counter = Counter()
    clock = clock or vgm.ay8910_clock or AY_DEFAULT_CLOCK
    min_period = ay_min_period(clock, divider)

    def current_period(reg: list[int], voice: int) -> int:
        return reg[REG_TONE_LO[voice]] | ((reg[REG_TONE_HI[voice]] & 0x0F) << 8)

    def env_period(reg: list[int]) -> int:
        return reg[REG_ENV_LO] | (reg[REG_ENV_HI] << 8)

    def note_fx(reg: list[int], voice: int) -> list[tuple[int, int, int]]:
        """Effects a note needs: mode, noise frequency, hardware envelope."""
        env = bool(reg[REG_VOL[voice]] & 0x10)
        fx = [(FX_MODE, ay_mode(_tone_on(reg, voice), _noise_on(reg, voice), env) - 1)]
        if _noise_on(reg, voice):
            fx.append((FX_NOISE, 31 - (reg[REG_NOISE] & 0x1F)))
        if env:
            fx.append((FX_ENV_SET, ((reg[REG_ENV_SHAPE] & 0x0F) << 4) | 1))
            fx.append((FX_ENV_LO, env_period(reg) & 0xFF))
            fx.append((FX_ENV_HI, (env_period(reg) >> 8) & 0xFF))
        return fx

    def drop_env_fx(hit: AyHit) -> None:
        """Remove a note-start envelope setup that a volume write turned off.

        The note opens while the previous envelope bit is still set, so
        `note_fx` queues the shape/period. If the real volume write a few
        samples later clears bit 4, keeping that setup would turn the envelope
        back on after the mode-off write and make the note follow the generator
        instead of its fixed level.
        """
        hit.fx_pts = [
            entry for entry in hit.fx_pts
            if entry[1] not in (FX_ENV_SET, FX_ENV_LO, FX_ENV_HI)
        ]

    def settle_env_off(hit: AyHit, sample: int) -> None:
        """Turn the note's opening state into fixed volume.

        Used when the envelope bit clears inside the opening write burst. The
        pattern row can only carry the settled state, so drop the envelope
        setup and replace the opening mode value rather than appending a
        second mode write: the last value on the row wins.
        """
        hit.env = False
        drop_env_fx(hit)
        off_mode = ay_mode(hit.tone, hit.noise, False) - 1
        for i, (t, fx, _val) in enumerate(hit.fx_pts):
            if fx == FX_MODE and t <= hit.sample_time:
                hit.fx_pts[i] = (t, fx, off_mode)
                return
        hit.fx_pts.append((sample, FX_MODE, off_mode))

    def open_hit(
        st: dict, cid: int, voice: int, t: int,
        split_from: AyHit | None = None,
    ) -> None:
        reg = st["reg"]
        env = bool(reg[REG_VOL[voice]] & 0x10)
        hit = AyHit(
            sample_time=t,
            chip_id=cid,
            voice=voice,
            period=current_period(reg, voice),
            vol=reg[REG_VOL[voice]] & 0x0F,
            env=env,
            tone=_tone_on(reg, voice),
            opened_tone=_tone_on(reg, voice),
            noise=_noise_on(reg, voice),
            noise_period=reg[REG_NOISE] & 0x1F,
            env_shape=reg[REG_ENV_SHAPE] & 0x0F,
            env_period=env_period(reg),
            split_from=split_from,
        )
        hit.fx_pts = [(t, fx, val) for fx, val in note_fx(reg, voice)]
        st["active"][voice] = hit

    def close_hit(st: dict, cid: int, voice: int, t: int, *, emit_off: bool = True) -> None:
        hit = st["active"][voice]
        if hit is None:
            return
        hit.off_sample = t
        if t - hit.sample_time >= AY_MIN_NOTE:
            hits.append(hit)
            if emit_off:
                offs.append(AyOff(t, cid, voice))
        elif hit.split_from is not None and emit_off:
            # The note before this one was closed without a key-off by the
            # same retrigger. This gate never sounded, so the predecessor is
            # still ringing and needs the cut it never got.
            acc["glitch"] += 1
            offs.append(AyOff(t, cid, voice))
        else:
            acc["glitch"] += 1
        st["active"][voice] = None

    def update_gate(st: dict, cid: int, voice: int, t: int, *, retrigger: bool = False) -> None:
        on = _audible(st["reg"], voice, min_period)
        active = st["active"][voice]
        if on and (active is None or retrigger):
            root = None
            if active is not None:
                root = active.split_from or active
                close_hit(st, cid, voice, t, emit_off=False)
            open_hit(st, cid, voice, t, split_from=root)
        elif not on:
            close_hit(st, cid, voice, t)

    for w in vgm.writes:
        if w.chip != chip:
            continue
        cid = w.chip_id
        st = state.setdefault(cid, _blank_chip())
        reg = st["reg"]
        if w.reg > 0x0F:
            continue
        r = w.reg
        val = w.val & 0xFF
        # A same-value $0D write still re-arms the envelope on real hardware
        # (set_shape resets the counter), so it must not be dropped. It is
        # handled below as either the opening burst or a new hit.
        if reg[r] == val and r != REG_ENV_SHAPE:
            continue
        reg[r] = val
        voice = None
        for i in range(AY_VOICES):
            if r in (REG_TONE_LO[i], REG_TONE_HI[i]):
                voice = i
                break
        if voice is not None:
            hit = st["active"][voice]
            new = current_period(reg, voice)
            audible = _audible(reg, voice, min_period)
            if hit is not None and audible and _tone_on(reg, voice):
                # Compare with what the voice was last seen playing, so a step
                # back to the note's own pitch is recorded too. Period writes
                # while the tone is disabled are the noise channel's leftover
                # register, not a pitch.
                cur = hit.freq_pts[-1][1] if hit.freq_pts else hit.period
                if new != cur:
                    hit.freq_pts.append((w.sample, new))
            # A period below the music range is how drivers mute a channel
            # while leaving the volume up. That is a gate-off, not a pitch.
            update_gate(st, cid, voice, w.sample)
            continue
        if r in REG_VOL:
            voice = REG_VOL.index(r)
            hit = st["active"][voice]
            audible = _audible(reg, voice, min_period)
            if hit is None:
                if audible:
                    open_hit(st, cid, voice, w.sample)
                continue
            if not audible:
                close_hit(st, cid, voice, w.sample)
                continue
            if val & 0x10:
                # The chip restarts the envelope on the enable write; that is
                # how MSX drivers trigger drum decays.
                update_gate(st, cid, voice, w.sample, retrigger=True)
                continue
            level = val & 0x0F
            if not hit.env:
                prev = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
                if level - prev >= AY_RETRIGGER_RISE:
                    # The driver keeps the gate open and jumps the level back
                    # up for the next hit. Close this one silently and let the
                    # new level open a fresh note, so the roll reaches the
                    # pattern instead of living only in the volume macro.
                    root = hit.split_from or hit
                    close_hit(st, cid, voice, w.sample, emit_off=False)
                    open_hit(st, cid, voice, w.sample, split_from=root)
                    continue
            if (w.sample - hit.sample_time <= AY_GATE_PAIR
                    and hit.vol - level >= AY_GATE_DROP):
                # Loud gate then the real quiet level: keep the quiet level
                # as the note's opening, not a separate volume step.  A small
                # drop is the first frame of a normal decay and must keep the
                # loud attack.
                hit.vol = level
                window = hit.sample_time + AY_GATE_PAIR
                hit.vol_pts = [p for p in hit.vol_pts if p[0] > window]
                if hit.env:
                    settle_env_off(hit, w.sample)
                continue
            if hit.env:
                # The level write also clears the envelope bit. Inside the
                # opening burst, the settled fixed level is the note's real
                # start. After that the envelope has run for several rows, so
                # keep its note-start setup and append the mode-off write.
                if w.sample - hit.sample_time <= AY_GATE_PAIR:
                    hit.vol = level
                    window = hit.sample_time + AY_GATE_PAIR
                    hit.vol_pts = [p for p in hit.vol_pts if p[0] > window]
                    settle_env_off(hit, w.sample)
                    continue
                hit.env = False
                hit.fx_pts.append((
                    w.sample, FX_MODE, ay_mode(hit.tone, hit.noise, False) - 1,
                ))
            hit.vol_pts.append((w.sample, level))
            continue
        if r == REG_MIXER:
            for voice in range(AY_VOICES):
                hit = st["active"][voice]
                if hit is None:
                    continue
                tone = _tone_on(reg, voice)
                noise = _noise_on(reg, voice)
                if (tone, noise) == (hit.tone, hit.noise):
                    continue
                if noise and not hit.noise and tone:
                    # The mixer adding noise to a voice that is still
                    # sounding (both tone bits on) is a new percussion hit.
                    # Drivers hold the volume open through a drum's noise
                    # attack and its tonal body, so without this the attack is
                    # glued to the end of the previous body's instrument macro
                    # and never gets its own row. A tone-to-noise handover
                    # (tone off as noise comes on) stays one note: that is a
                    # mode change on the same sound, not a new attack. The
                    # period and noise-period writes just before this mixer
                    # write belong to the new hit.
                    update_gate(st, cid, voice, w.sample, retrigger=True)
                    continue
                hit.fx_pts.append((
                    w.sample, FX_MODE,
                    ay_mode(tone, noise, bool(reg[REG_VOL[voice]] & 0x10)) - 1,
                ))
                if noise and not hit.noise:
                    # A tone-to-noise handover stays on the same note, but
                    # the driver may have set the noise period while noise
                    # was off. Carry it over so the first tick does not use
                    # the previous hit's period.
                    hit.fx_pts.append((
                        w.sample, FX_NOISE, 31 - (reg[REG_NOISE] & 0x1F),
                    ))
                hit.tone, hit.noise = tone, noise
            for voice in range(AY_VOICES):
                update_gate(st, cid, voice, w.sample)
            continue
        if r == REG_NOISE:
            for voice in range(AY_VOICES):
                hit = st["active"][voice]
                if hit is not None and hit.noise:
                    hit.fx_pts.append((w.sample, FX_NOISE, 31 - (val & 0x1F)))
            continue
        if r == REG_ENV_SHAPE:
            for voice in range(AY_VOICES):
                hit = st["active"][voice]
                if hit is None or not hit.env:
                    continue
                if w.sample - hit.sample_time > AY_ENV_OPEN:
                    # The chip re-arms the envelope on every $0D write, even
                    # one that repeats the current shape. A tracker can only
                    # restart it with a note (the instrument's phase-reset
                    # macro forces the write), so this is a new hit at the
                    # source's own rate instead of one long held note.
                    root = hit.split_from or hit
                    close_hit(st, cid, voice, w.sample, emit_off=False)
                    open_hit(st, cid, voice, w.sample, split_from=root)
                else:
                    hit.fx_pts.append((
                        w.sample, FX_ENV_SET, ((val & 0x0F) << 4) | 1,
                    ))
            continue
        if r in (REG_ENV_LO, REG_ENV_HI):
            period = env_period(reg)
            for voice in range(AY_VOICES):
                hit = st["active"][voice]
                if hit is not None and hit.env:
                    hit.fx_pts.append((w.sample, FX_ENV_LO, period & 0xFF))
                    hit.fx_pts.append((w.sample, FX_ENV_HI, (period >> 8) & 0xFF))
            continue
        if r in (14, 15):
            acc["io_ports"] += 1

    for cid, st in state.items():
        for voice in range(AY_VOICES):
            hit = st["active"][voice]
            if hit is not None:
                # Still sounding at the end of the log: no key-off row.
                hit.off_sample = 0
                hits.append(hit)
                st["active"][voice] = None

    if acc.get("io_ports"):
        label = "YM2203 SSG" if chip == "ym2203_ssg" else "AY-8910"
        warnings.append(f"{acc['io_ports']} {label} I/O port writes are not converted")
    return hits, offs, warnings
