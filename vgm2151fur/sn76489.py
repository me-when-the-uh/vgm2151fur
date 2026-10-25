"""SN76489 (Genesis PSG) serial stream -> tone/noise notes.

The chip has no key-on: a voice sounds while its 4-bit attenuation is below
15.  Tone period is 10 bits (latch low nibble, then a high data byte); the
noise channel takes a 3-bit mode (white/periodic + three rates or channel 3's
tone).  Furnace's SMS platform exposes the same registers, so a source period
is mapped through the period pitch table (toneDivider 64) and played back as
the same register value.

Mid-note period writes become pitch-macro points; volume writes become
volume-column steps.  A period jump of a semitone or more is a new note.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from vgm2151fur.pitch import furnace_period_note, pitch_table
from vgm2151fur.vgm import VgmFile

# Furnace's SMS platform default (COLOR_NTSC) and period divider.  The VGM
# header normally says 3579545, which is within a hertz of this.
PSG_FURNACE_CLOCK = 315_000_000.0 / 88.0
PSG_DEFAULT_CLOCK = 3579545
PSG_DIVIDER = 64
PSG_MAX_PERIOD = 0x3FF
PSG_MIN_NOTE = 100          # shorter gates are driver housekeeping
PSG_NOTE_JUMP_ST = 1.0      # a larger period jump is a new note


@dataclass
class PsgHit:
    sample_time: int
    voice: int
    period: int             # 10-bit tone period (0 for noise)
    vol: int                # Furnace volume 0-15 (15 = loudest)
    off_sample: int = 0
    noise: bool = False
    noise_mode: int = 0
    pitch_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    mode_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class PsgOff:
    sample_time: int
    voice: int


def psg_note(period: int) -> tuple[int, int]:
    """10-bit SN76489 period -> (Furnace note, E5) for the same register.

    Period values below the pitch table's highest note (a very high source
    frequency, e.g. 14) cannot be represented by a note; clamp to the
    shortest period the table can produce instead of letting the platform
    fall back to its slowest one (1023).
    """
    value = max(0, min(PSG_MAX_PERIOD, int(period)))
    tbl = psg_pitch_table()
    shortest = max(1, tbl.value(179, 127))
    value = max(shortest, value)
    # Prefer a note whose *base* period is exactly the source period: an E5
    # fine tune is applied after the note-on, so a one-unit late write was
    # visible as a wrong pitch at the gate.  Fall back to the nearest
    # note/fine pair when the table has no exact base entry.
    centre = int(round(tbl.note_fraction(value)))
    for note in range(max(0, centre - 3), min(179, centre + 3) + 1):
        if tbl.value(note, 0) == value:
            return note, 0x80
    return furnace_period_note(
        clock=PSG_FURNACE_CLOCK,
        divider=PSG_DIVIDER,
        value=value,
        maximum=PSG_MAX_PERIOD,
    )


def psg_pitch_table():
    return pitch_table(
        PSG_FURNACE_CLOCK, PSG_DIVIDER, maximum=PSG_MAX_PERIOD,
    )


def psg_rate(clock: int, period: int) -> float:
    """Actual SN76489 tone frequency for a period, for reports/audits."""
    return (clock or PSG_DEFAULT_CLOCK) / (32.0 * max(1, period))


def _jump_st(a: int, b: int) -> float:
    if a <= 0 or b <= 0:
        return 12.0
    return abs(12.0 * math.log2(b / a))


def collect_sn76489(
    vgm: VgmFile, clock: int | None = None,
) -> tuple[list[PsgHit], list[PsgOff], list[str]]:
    warnings: list[str] = []
    writes = [w for w in vgm.writes if w.chip == "sn76489"]
    if not writes:
        return [], [], warnings
    clock = clock or vgm.sn76489_clock or PSG_DEFAULT_CLOCK
    freq = [0, 0, 0]
    freq_high = [False, False, False]
    vol = [15, 15, 15, 15]
    noise_mode = 0
    latch_ch = 0
    latch_vol = False
    active: list[PsgHit | None] = [None, None, None, None]
    hits: list[PsgHit] = []
    offs: list[PsgOff] = []
    glitches = 0

    def close(voice: int, t: int, *, emit_off: bool = True) -> None:
        nonlocal glitches
        hit = active[voice]
        if hit is None:
            return
        hit.off_sample = t
        if t <= 0:
            # End of the log while the gate is still open: a held note.
            hits.append(hit)
        elif t - hit.sample_time >= PSG_MIN_NOTE:
            hits.append(hit)
            if emit_off:
                offs.append(PsgOff(t, voice))
        else:
            glitches += 1
        active[voice] = None

    def open_hit(voice: int, t: int) -> None:
        active[voice] = PsgHit(
            sample_time=t,
            voice=voice,
            period=freq[voice] if voice < 3 else 0,
            vol=15 - vol[voice],
            noise=voice == 3,
            noise_mode=noise_mode if voice == 3 else 0,
        )

    def evaluate_tone(voice: int, t: int) -> None:
        on = freq_high[voice] and vol[voice] < 15
        hit = active[voice]
        if on and hit is None:
            open_hit(voice, t)
            return
        if not on:
            if hit is not None:
                close(voice, t)
            return
        assert hit is not None
        period = max(0, min(PSG_MAX_PERIOD, freq[voice]))
        if period != hit.period:
            if t == hit.sample_time:
                # The high byte and the gate share one sample: the final
                # period is the note.
                hit.period = period
            elif _jump_st(hit.period, period) > PSG_NOTE_JUMP_ST:
                close(voice, t, emit_off=False)
                open_hit(voice, t)
                return
            else:
                if not hit.pitch_pts or hit.pitch_pts[-1][1] != period:
                    hit.pitch_pts.append((t, period))
        if hit.vol != 15 - vol[voice]:
            level = 15 - vol[voice]
            if t == hit.sample_time:
                hit.vol = level
            else:
                if not hit.vol_pts or hit.vol_pts[-1][1] != level:
                    hit.vol_pts.append((t, level))

    def evaluate_noise(t: int) -> None:
        on = vol[3] < 15
        hit = active[3]
        if on and hit is None:
            open_hit(3, t)
            return
        if not on:
            if hit is not None:
                close(3, t)
            return
        assert hit is not None
        if hit.noise_mode != noise_mode:
            if t == hit.sample_time:
                hit.noise_mode = noise_mode
            else:
                hit.mode_pts.append((t, noise_mode))
        level = 15 - vol[3]
        if hit.vol != level:
            if t == hit.sample_time:
                hit.vol = level
            elif not hit.vol_pts or hit.vol_pts[-1][1] != level:
                hit.vol_pts.append((t, level))

    for w in writes:
        b = w.val & 0xFF
        t = w.sample
        if b & 0x80:
            latch_ch = (b >> 5) & 3
            latch_vol = bool(b & 0x10)
            data = b & 0x0F
            if latch_vol:
                vol[latch_ch] = data
                if latch_ch == 3:
                    evaluate_noise(t)
                else:
                    evaluate_tone(latch_ch, t)
            elif latch_ch == 3:
                noise_mode = data & 0x07
                evaluate_noise(t)
            else:
                freq[latch_ch] = (freq[latch_ch] & 0x3F0) | data
        else:
            if latch_vol or latch_ch == 3:
                continue
            data = b & 0x3F
            freq[latch_ch] = (freq[latch_ch] & 0x00F) | (data << 4)
            freq_high[latch_ch] = True
            evaluate_tone(latch_ch, t)

    for voice in range(4):
        if active[voice] is not None:
            # Still sounding at the end of the log: a held note, no off.
            close(voice, 0, emit_off=False)
    if glitches:
        warnings.append(f"SN76489: ignored {glitches} sub-frame gates")
    return hits, offs, warnings
