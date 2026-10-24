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
decays, so a new note starts when the voice becomes audible, when the envelope
is re-armed, or when the period changes while the voice sounds; the analyzer
turns each pitch change into its own note because these chips survive a
retrigger unchanged.

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


def ay_note(clock: int, period: int) -> tuple[int, int]:
    """Period register -> (Furnace note, E5 fine tune)."""
    return furnace_period_note(
        clock=module_clock(clock),
        divider=8,
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
    tone: bool  # mixer: tone enabled
    noise: bool  # mixer: noise enabled
    noise_period: int  # register 6 (5-bit)
    env_shape: int  # register 13
    env_period: int  # registers 11/12, 16-bit
    off_sample: int = 0  # gate-off sample (0 = still sounding at the end)
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


def _blank_chip() -> dict:
    return {
        "reg": [0] * 16,
        "active": [None] * AY_VOICES,
    }


def _tone_on(reg: list[int], voice: int) -> bool:
    return not (reg[REG_MIXER] & (1 << voice))


def _noise_on(reg: list[int], voice: int) -> bool:
    return not (reg[REG_MIXER] & (1 << (voice + 3)))


def ay_min_period(clock: int) -> int:
    """Smallest period the module can express for this clock.

    The pitch table tops out at B-9, and drivers mute a channel by writing a
    period below that (an inaudible tone): the SCC-style "silence gate".
    Treating it as silence keeps those housekeeping writes from becoming
    notes with a pitch nothing can represent.
    """
    tbl = pitch_table(module_clock(clock or AY_DEFAULT_CLOCK), 8)
    return max(2, tbl.value(NOTE_MAX, 127))


def _audible(reg: list[int], voice: int, min_period: int = 0) -> bool:
    """True when the voice can be heard with the current registers."""
    tone = _tone_on(reg, voice)
    if tone:
        period = reg[REG_TONE_LO[voice]] | ((reg[REG_TONE_HI[voice]] & 0x0F) << 8)
        tone = period >= max(1, min_period)
    if not (tone or _noise_on(reg, voice)):
        return False
    if reg[REG_VOL[voice]] & 0x10:
        return True  # the hardware envelope drives this channel
    return (reg[REG_VOL[voice]] & 0x0F) > 0


def collect_ay8910(
    vgm: VgmFile,
) -> tuple[list[AyHit], list[AyOff], list[str]]:
    """Walk the AY register stream into notes.

    Returns (hits, key-offs, warnings).  Each hit is one gate interval of one
    voice with everything the writer needs to emit it as a note.
    """
    warnings: list[str] = []
    hits: list[AyHit] = []
    offs: list[AyOff] = []
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "ay8910"})
    if not chips:
        return [], [], warnings
    state: dict[int, dict] = {cid: _blank_chip() for cid in chips}
    acc: Counter = Counter()
    clock = vgm.ay8910_clock or AY_DEFAULT_CLOCK
    min_period = ay_min_period(clock)

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

    def open_hit(st: dict, cid: int, voice: int, t: int) -> None:
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
            noise=_noise_on(reg, voice),
            noise_period=reg[REG_NOISE] & 0x1F,
            env_shape=reg[REG_ENV_SHAPE] & 0x0F,
            env_period=env_period(reg),
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
        else:
            acc["glitch"] += 1
        st["active"][voice] = None

    def update_gate(st: dict, cid: int, voice: int, t: int, *, retrigger: bool = False) -> None:
        on = _audible(st["reg"], voice, min_period)
        active = st["active"][voice]
        if on and (active is None or retrigger):
            if active is not None:
                close_hit(st, cid, voice, t, emit_off=False)
            open_hit(st, cid, voice, t)
        elif not on:
            close_hit(st, cid, voice, t)

    for w in vgm.writes:
        if w.chip != "ay8910":
            continue
        cid = w.chip_id
        st = state.setdefault(cid, _blank_chip())
        reg = st["reg"]
        r = w.reg & 0x0F
        val = w.val & 0xFF
        if reg[r] == val:
            continue
        reg[r] = val
        voice = None
        for i in range(AY_VOICES):
            if r in (REG_TONE_LO[i], REG_TONE_HI[i]):
                voice = i
                break
        if voice is not None:
            hit = st["active"][voice]
            if hit is not None:
                new = current_period(reg, voice)
                # Compare with what the voice was last seen playing, so a step
                # back to the note's own pitch is recorded too; the pair of
                # period writes is one value, which the analyzer merges.
                cur = hit.freq_pts[-1][1] if hit.freq_pts else hit.period
                if new != cur:
                    # A period rewrite inside a note is a pitch change:
                    # drivers glide drums and vibrato the tone voice. It
                    # becomes the note's pitch macro.
                    hit.freq_pts.append((w.sample, new))
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
            if hit.env:
                # The level write also clears the envelope bit; the channel
                # mode has to follow or the register keeps using the
                # generator.
                hit.env = False
                hit.fx_pts.append((
                    w.sample, FX_MODE, ay_mode(hit.tone, hit.noise, False) - 1,
                ))
            hit.vol_pts.append((w.sample, val & 0x0F))
            continue
        if r == REG_MIXER:
            for voice in range(AY_VOICES):
                hit = st["active"][voice]
                if hit is None:
                    continue
                tone = _tone_on(reg, voice)
                noise = _noise_on(reg, voice)
                if (tone, noise) != (hit.tone, hit.noise):
                    hit.fx_pts.append((
                        w.sample, FX_MODE,
                        ay_mode(tone, noise, bool(reg[REG_VOL[voice]] & 0x10)) - 1,
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
                if hit is not None and hit.env:
                    hit.fx_pts.append((w.sample, FX_ENV_SET, ((val & 0x0F) << 4) | 1))
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
        warnings.append(f"{acc['io_ports']} AY-8910 I/O port writes are not converted")
    return hits, offs, warnings
