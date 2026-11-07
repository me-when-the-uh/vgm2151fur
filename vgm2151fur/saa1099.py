"""Philips SAA1099: six square/noise voices with two shared noise generators.

The chip is a stereo PSG: registers `0x00`-`0x05` are the per-voice amplitude
(low nibble = left, high nibble = right), `0x08`-`0x0D` the frequency offset,
`0x10`-`0x12` the octave pairs, `0x14`/`0x15` the tone/noise enables, `0x16`
the two noise generators (one per three-voice group), `0x18`/`0x19` the two
envelope generators and `0x1C` the power/sync control.  There is no key-on:
the gate is the amplitude and the tone/noise enables.

Furnace's `DIV_SYSTEM_SAA1099` (`0x97`) is the six-voice, no-software-timer
variant.  Its pitch table uses `divider=2` and `maximum=0xffff`; the platform
turns the table value into an 8-bit offset and 3-bit octave with

    k = bit_length(freq) - 9          (0 when freq <= 511)
    offset = 255 - (freq >> k)        (stored as a byte)
    octave = 7 - k

so the chip plays `clock * 2**octave / (512 * (511 - offset))`.  Inverting the
platform's conversion is easier than inverting the chip formula: a source
`(offset, octave)` corresponds to every pitch-table value in
``[(511-offset) << k, (511-offset+1) << k)`` with ``k = 7 - octave``.  We hand
the midpoint to the pitch-table inverse, which finds the note/E5 pair whose
register is closest; every one of the 2048 register pairs round-trips.

A Furnace export writes the volume and pan macros separately, so the same
voice can receive several amplitude writes in one tick.  The collector applies
all writes at a sample first and then takes one decision, so only the final
register state is heard.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import groupby

from vgm2151fur.pitch import furnace_period_note
from vgm2151fur.vgm import VgmFile

SAA_VOICES = 6
SAA_DEFAULT_CLOCK = 8_000_000
SAA_DIVIDER = 2
SAA_PITCH_MAX = 0xFFFF
# Furnace's three clock presets for system 0x97.
SAA_CLOCK_8M = 8_000_000.0
SAA_CLOCK_NTSC = (315_000_000.0 / 88.0) * 2.0      # COLOR_NTSC * 2
SAA_CLOCK_PAL = 4_433_618.75 * 8.0 / 5.0           # COLOR_PAL * 8 / 5

# A gate shorter than this is driver housekeeping, not music.
SAA_MIN_NOTE = 100
# A loud opening level followed by the real level this soon is one note.
SAA_GATE_PAIR = 1000
SAA_GATE_DROP = 6
# An amplitude jump this large while the gate stays open is a retrigger.
SAA_RETRIGGER_RISE = 4

# Furnace SAA1099 effects.
FX_MODE = 0x10   # 10xy: x = noise, y = tone
FX_NOISE = 0x11  # 11xx: noise generator frequency/group source
FX_ENV = 0x12    # 12xx: envelope control register


def saa_module_clock(clock: int | float) -> float:
    """The clock Furnace will actually run the chip at.

    A clock within a couple of hertz of one of the three system presets is
    replaced by that preset's exact double, matching the chip flag we write.
    """
    value = float(clock or SAA_DEFAULT_CLOCK)
    for preset in (SAA_CLOCK_8M, SAA_CLOCK_NTSC, SAA_CLOCK_PAL):
        if abs(value - preset) <= 2:
            return preset
    return value


def saa_period_value(freq: int, octave: int) -> int:
    """Pitch-table value the Furnace platform converts to this register pair."""
    k = 7 - (octave & 7)
    base = 511 - (freq & 0xFF)
    # The midpoint leaves the most room for the note/fine pitch to land back
    # inside the same register bucket.
    return (base << k) + ((1 << (k - 1)) if k else 0)


def saa_rate(clock: int, freq: int, octave: int) -> float:
    """Tone frequency for an offset/octave pair, in Hz."""
    value = saa_module_clock(clock) * (2.0 ** (octave & 7))
    return value / (512.0 * (511 - (freq & 0xFF)))


def saa_period_note(clock: int, period: int) -> tuple[int, int]:
    """Platform pitch-table value -> (Furnace note, E5 fine pitch)."""
    return furnace_period_note(
        saa_module_clock(clock),
        SAA_DIVIDER,
        max(1, period),
        maximum=SAA_PITCH_MAX,
    )


def saa_note(clock: int, freq: int, octave: int) -> tuple[int, int]:
    """Register pair -> (Furnace note, E5 fine pitch)."""
    return saa_period_note(clock, saa_period_value(freq, octave))


@dataclass
class SaaHit:
    """One gate interval of one SAA1099 voice."""

    sample_time: int
    chip_id: int
    voice: int
    # Furnace pitch-table value for the opening frequency/octave.
    period: int
    # Opening amplitude nibbles, 0-15 each.
    left: int
    right: int
    tone: bool
    noise: bool
    noise_freq: int
    env: int
    # True for the silent note that keeps an envelope generator's source
    # oscillator at the right pitch when that voice has no audible note.
    # The SAA envelope is clocked by channel 1/4; without this note those
    # registers stay at reset and the envelope rate is wrong.
    hidden: bool = False
    off_sample: int = 0
    # (sample, pitch-table value) after the opening.
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    # (sample, left, right) after the opening.
    amp_pts: list[tuple[int, int, int]] = field(default_factory=list)
    # (sample, tone, noise) after the opening.
    mode_pts: list[tuple[int, bool, bool]] = field(default_factory=list)
    # (sample, noise generator value 0-3) after the opening.
    noise_pts: list[tuple[int, int]] = field(default_factory=list)
    # (sample, envelope control byte) for the group's output voice.
    env_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class SaaOff:
    sample_time: int
    chip_id: int
    voice: int


def _blank_chip() -> dict:
    return {
        "amp": [0] * SAA_VOICES,
        "freq": [0] * SAA_VOICES,
        "oct": [0] * SAA_VOICES,
        "tone": 0,       # register 0x14, bits 0-5
        "noise": 0,      # register 0x15, bits 0-5
        "noise_ctl": 0,  # register 0x16, two 2-bit fields
        "env": [0, 0],   # registers 0x18/0x19
        "power": True,   # register 0x1C bit 0
        "active": [None] * SAA_VOICES,
    }


def _tone_on(st: dict, voice: int) -> bool:
    return bool(st["tone"] & (1 << voice))


def _noise_on(st: dict, voice: int) -> bool:
    return bool(st["noise"] & (1 << voice))


def _noise_freq(st: dict, voice: int) -> int:
    return (st["noise_ctl"] >> (0 if voice < 3 else 4)) & 0x03


def _audible(st: dict, voice: int) -> bool:
    if not st["power"]:
        return False
    if st["amp"][voice] == 0:
        return False
    return _tone_on(st, voice) or _noise_on(st, voice)


def _period(st: dict, voice: int) -> int:
    return saa_period_value(st["freq"][voice], st["oct"][voice])


def _env_source_on(st: dict, voice: int) -> bool:
    """True when this voice clocks an enabled internal envelope.

    The envelope control's bit 7 enables it and bit 5 selects the source
    channel's frequency (clear) instead of the external timer.  Only that
    mode needs the silent source note.
    """
    if voice % 3 != 1:
        return False
    env = st["env"][voice // 3]
    return bool(env & 0x80) and not bool(env & 0x20)


def collect_saa1099(
    vgm: VgmFile,
) -> tuple[list[SaaHit], list[SaaOff], list[str]]:
    """Walk the 0xBD register stream into notes.

    All writes at one sample are applied before any gate decision is made;
    a Furnace export often writes the volume and pan macros as separate
    register writes on the same tick.
    """
    warnings: list[str] = []
    hits: list[SaaHit] = []
    offs: list[SaaOff] = []
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "saa1099"})
    if not chips:
        return [], [], warnings
    state: dict[int, dict] = {cid: _blank_chip() for cid in chips}
    acc: Counter = Counter()

    def open_hit(st: dict, cid: int, voice: int, t: int) -> None:
        hit = SaaHit(
            sample_time=t,
            chip_id=cid,
            voice=voice,
            period=_period(st, voice),
            left=st["amp"][voice] & 0x0F,
            right=(st["amp"][voice] >> 4) & 0x0F,
            tone=_tone_on(st, voice),
            noise=_noise_on(st, voice),
            noise_freq=_noise_freq(st, voice),
            env=st["env"][voice // 3],
        )
        st["active"][voice] = hit

    def open_hidden(st: dict, cid: int, voice: int, t: int) -> None:
        """Silent note that keeps the envelope's source oscillator running."""
        st["active"][voice] = SaaHit(
            sample_time=t,
            chip_id=cid,
            voice=voice,
            period=_period(st, voice),
            left=0,
            right=0,
            tone=True,
            noise=False,
            noise_freq=0,
            env=0,
            hidden=True,
        )

    def close_hit(
        st: dict, cid: int, voice: int, t: int, *, emit_off: bool = True,
    ) -> None:
        hit = st["active"][voice]
        if hit is None:
            return
        hit.off_sample = t
        if t - hit.sample_time >= SAA_MIN_NOTE:
            hits.append(hit)
            if emit_off:
                offs.append(SaaOff(t, cid, voice))
        else:
            acc["glitch"] += 1
        st["active"][voice] = None

    def last_amp(hit: SaaHit) -> tuple[int, int]:
        return hit.amp_pts[-1][1:] if hit.amp_pts else (hit.left, hit.right)

    def last_period(hit: SaaHit) -> int:
        return hit.freq_pts[-1][1] if hit.freq_pts else hit.period

    def last_mode(hit: SaaHit) -> tuple[bool, bool]:
        return hit.mode_pts[-1][1:] if hit.mode_pts else (hit.tone, hit.noise)

    def last_noise(hit: SaaHit) -> int:
        return hit.noise_pts[-1][1] if hit.noise_pts else hit.noise_freq

    def last_env(hit: SaaHit) -> int:
        return hit.env_pts[-1][1] if hit.env_pts else hit.env

    def apply_write(st: dict, r: int, val: int) -> None:
        val &= 0xFF
        if r <= 5:
            st["amp"][r] = val
        elif 8 <= r <= 13:
            st["freq"][r - 8] = val
        elif r == 0x10:
            st["oct"][0] = val & 7
            st["oct"][1] = (val >> 4) & 7
        elif r == 0x11:
            st["oct"][2] = val & 7
            st["oct"][3] = (val >> 4) & 7
        elif r == 0x12:
            st["oct"][4] = val & 7
            st["oct"][5] = (val >> 4) & 7
        elif r == 0x14:
            st["tone"] = val & 0x3F
        elif r == 0x15:
            st["noise"] = val & 0x3F
        elif r == 0x16:
            st["noise_ctl"] = val & 0xFF
        elif r == 0x18:
            st["env"][0] = val
        elif r == 0x19:
            st["env"][1] = val
        elif r == 0x1C:
            st["power"] = bool(val & 0x01)
        elif r > 0x1F:
            acc["bad_reg"] += 1
        elif val:
            acc["unknown_reg"] += 1

    def evaluate(cid: int, st: dict, t: int) -> None:
        for voice in range(SAA_VOICES):
            active = st["active"][voice]
            on = _audible(st, voice)
            env_source = _env_source_on(st, voice)
            if active is not None and active.hidden:
                if on:
                    close_hit(st, cid, voice, t, emit_off=False)
                    open_hit(st, cid, voice, t)
                elif not env_source:
                    close_hit(st, cid, voice, t)
                else:
                    period = _period(st, voice)
                    if period != last_period(active):
                        active.freq_pts.append((t, period))
                continue
            if active is None:
                if on:
                    open_hit(st, cid, voice, t)
                elif env_source:
                    open_hidden(st, cid, voice, t)
                continue
            if not on:
                close_hit(st, cid, voice, t)
                if env_source:
                    open_hidden(st, cid, voice, t)
                continue

            left = st["amp"][voice] & 0x0F
            right = (st["amp"][voice] >> 4) & 0x0F
            tone = _tone_on(st, voice)
            noise = _noise_on(st, voice)
            prev_left, prev_right = last_amp(active)
            prev_tone, prev_noise = last_mode(active)
            newmax = max(left, right)
            oldmax = max(prev_left, prev_right)

            # A loud opening followed immediately by the real level is the
            # driver's gate trick, not a separate note.
            if (
                newmax < oldmax
                and t - active.sample_time <= SAA_GATE_PAIR
                and oldmax - newmax >= SAA_GATE_DROP
                and not active.amp_pts
            ):
                active.left, active.right = left, right
                continue

            # A level jump back up while the gate stays open is a retriggered
            # drum or a new note, not a step in the current decay.
            if newmax - oldmax >= SAA_RETRIGGER_RISE:
                close_hit(st, cid, voice, t, emit_off=False)
                open_hit(st, cid, voice, t)
                continue

            # Adding noise to a sounding tone voice is a new percussion hit.
            if noise and not prev_noise and tone:
                close_hit(st, cid, voice, t, emit_off=False)
                open_hit(st, cid, voice, t)
                continue

            period = _period(st, voice)
            if period != last_period(active):
                active.freq_pts.append((t, period))
            if (left, right) != (prev_left, prev_right):
                active.amp_pts.append((t, left, right))
            if (tone, noise) != (prev_tone, prev_noise):
                active.mode_pts.append((t, tone, noise))
            nf = _noise_freq(st, voice)
            if nf != last_noise(active):
                active.noise_pts.append((t, nf))
            if voice % 3 == 2:
                env = st["env"][voice // 3]
                if env != last_env(active):
                    active.env_pts.append((t, env))

    saa_writes = [w for w in vgm.writes if w.chip == "saa1099"]
    for sample, group in groupby(saa_writes, key=lambda w: w.sample):
        batch = list(group)
        touched: set[int] = set()
        for w in batch:
            st = state.setdefault(w.chip_id, _blank_chip())
            apply_write(st, w.reg, w.val)
            touched.add(w.chip_id)
        for cid in touched:
            evaluate(cid, state[cid], sample)

    for cid, st in state.items():
        for voice in range(SAA_VOICES):
            hit = st["active"][voice]
            if hit is not None:
                # Still sounding at the end of the log: no key-off row.
                hit.off_sample = 0
                hits.append(hit)
                st["active"][voice] = None

    if acc.get("glitch"):
        warnings.append(
            f"{acc['glitch']} SAA1099 gate(s) shorter than {SAA_MIN_NOTE} "
            "samples were dropped"
        )
    if acc.get("unknown_reg"):
        warnings.append(
            f"{acc['unknown_reg']} nonzero SAA1099 write(s) to unused "
            "registers were ignored"
        )
    return hits, offs, warnings
