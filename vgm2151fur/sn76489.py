"""SN76489 (Genesis PSG) and Toshiba T6W28 serial stream -> notes.

The chip has no key-on: a voice sounds while its 4-bit attenuation is below
15.  Tone period is 10 bits (latch low nibble, then a high data byte); the
noise channel takes a 3-bit mode (white/periodic + three rates or channel 3's
tone).  Furnace's SMS platform exposes the same registers, so a source period
is mapped through the period pitch table (toneDivider 64) and played back as
the same register value.

A dual SN76489 keeps a second, independent register file on command 0x30.  A
T6W28 (Neo Geo Pocket Color) uses the same two streams as the two halves of
one stereo chip: the left half carries tone periods and left attenuation, the
right half right attenuation plus the noise parameters.  Furnace's T6W28
platform runs its pitch table at divider 16 (the noise channel at 15) and
takes independent left/right volumes, which this collector maps onto the
volume column plus 08xy panning.

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
# Furnace's T6W28 platform uses its own clock (3072000 Hz) and the SN pitch
# tables with dividers 16 (tone) and 15 (noise).
T6W28_CLOCK = 3_072_000
T6W28_TONE_DIVIDER = 16
T6W28_NOISE_DIVIDER = 15
PSG_MAX_PERIOD = 0x3FF
PSG_MIN_NOTE = 100          # shorter gates are driver housekeeping
PSG_NOTE_JUMP_ST = 1.0      # a larger period jump is a new note
PSG_ATTACK_STEP = 2         # a volume rise this large is a new attack


@dataclass
class PsgHit:
    sample_time: int
    voice: int
    period: int             # 10-bit tone/noise period (0 for fixed noise)
    vol: int                # Furnace volume 0-15 (15 = loudest)
    off_sample: int = 0
    chip: int = 0           # 0 = first SN76489, 1 = second/T6W28 right half
    noise: bool = False
    noise_mode: int = 0
    # Game Gear 08xy pan value (0xF0 left, 0x0F right, 0xFF both). Zero when
    # the source did not declare stereo.  On a T6W28 this is the independent
    # x=left / y=right split computed from the two attenuation nibbles.
    pan: int = 0
    opened_pan: int = 0
    t6w28: bool = False
    # T6W28 left/right attenuation steps and their pan at the same instant.
    mix_pts: list[tuple[int, int, int]] = field(default_factory=list)
    pan_pts: list[tuple[int, int]] = field(default_factory=list)
    pitch_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)
    mode_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class PsgOff:
    sample_time: int
    voice: int
    chip: int = 0


def _period_note(period: int, clock: float, divider: float) -> tuple[int, int]:
    """Register value -> (Furnace note, E5) for one period pitch table."""
    value = max(0, min(PSG_MAX_PERIOD, int(period)))
    tbl = pitch_table(float(clock), divider, maximum=PSG_MAX_PERIOD)
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
        clock=float(clock),
        divider=divider,
        value=value,
        maximum=PSG_MAX_PERIOD,
    )


def psg_note(period: int) -> tuple[int, int]:
    """10-bit SN76489 period -> (Furnace note, E5) for the same register.

    Period values below the pitch table's highest note (a very high source
    frequency, e.g. 14) cannot be represented by a note; clamp to the
    shortest period the table can produce instead of letting the platform
    fall back to its slowest one (1023).
    """
    return _period_note(period, PSG_FURNACE_CLOCK, PSG_DIVIDER)


def t6w28_note(period: int, clock: int) -> tuple[int, int]:
    """T6W28 tone register -> (note, E5) for Furnace's 16x divider."""
    return _period_note(period, clock or T6W28_CLOCK, T6W28_TONE_DIVIDER)


def t6w28_noise_note(period: int, clock: int) -> tuple[int, int]:
    """T6W28 noise period -> (note, E5) for Furnace's 15x noise divider."""
    return _period_note(period, clock or T6W28_CLOCK, T6W28_NOISE_DIVIDER)


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
    """Read every SN76489/T6W28 instance in a log."""
    warnings: list[str] = []
    writes = [w for w in vgm.writes if w.chip == "sn76489"]
    if not writes:
        return [], [], warnings
    clock = clock or vgm.sn76489_clock or PSG_DEFAULT_CLOCK
    if getattr(vgm, "sn76489_t6w28", False):
        return _collect_t6w28(writes, clock, warnings)
    by_chip: dict[int, list] = {}
    for w in writes:
        by_chip.setdefault(w.chip_id, []).append(w)
    stereo = bool(getattr(vgm, "sn76489_stereo", False))
    hits: list[PsgHit] = []
    offs: list[PsgOff] = []
    for cid in sorted(by_chip):
        h, o, warn = _collect_plain(by_chip[cid], clock, stereo, cid)
        hits.extend(h)
        offs.extend(o)
        warnings.extend(warn)
    return hits, offs, warnings


def _collect_plain(
    writes: list, clock: int, stereo: bool, chip: int,
) -> tuple[list[PsgHit], list[PsgOff], list[str]]:
    warnings: list[str] = []
    stereo_mask = 0xFF
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

    def pan_for(voice: int) -> int:
        """The 08xy value for one voice under the current Game Gear mask."""
        if not stereo:
            return 0
        left = 0xF0 if stereo_mask & (0x10 << voice) else 0
        right = 0x0F if stereo_mask & (0x01 << voice) else 0
        return left | right

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
                offs.append(PsgOff(t, voice, chip))
        else:
            glitches += 1
        active[voice] = None

    def noise_period() -> int:
        return max(0, min(PSG_MAX_PERIOD, freq[2]))

    def set_noise_period(hit: PsgHit, t: int) -> None:
        period = noise_period()
        if hit.period == period:
            return
        if t == hit.sample_time:
            hit.period = period
        elif not hit.pitch_pts or hit.pitch_pts[-1][1] != period:
            hit.pitch_pts.append((t, period))

    def open_hit(voice: int, t: int) -> None:
        pan = pan_for(voice)
        level = 15 - vol[voice]
        if stereo and pan == 0:
            # Both Game Gear sides are off for this voice. The SMS panning
            # command cannot say "both off" (zero is read as centre), so the
            # level column carries the silence.
            level = 0
        active[voice] = PsgHit(
            sample_time=t,
            voice=voice,
            period=(
                freq[voice] if voice < 3
                else noise_period() if (noise_mode & 3) == 3
                else 0
            ),
            vol=level,
            chip=chip,
            noise=voice == 3,
            noise_mode=noise_mode if voice == 3 else 0,
            pan=pan,
            opened_pan=pan,
        )

    def evaluate_tone(voice: int, t: int) -> None:
        if voice == 2 and active[3] is not None and (noise_mode & 3) == 3:
            # Noise mode 3 borrows tone generator 3's period. Track that
            # register so the noise note follows it too.
            set_noise_period(active[3], t)
        # Period 0 is the usual silence gate on this chip (the tone generator
        # cannot divide by it). It must close the note, not become a new one.
        on = freq_high[voice] and vol[voice] < 15 and freq[voice] > 0
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
            else:
                # Compare with the pitch the voice is on now, not the note's
                # original period: a slow glide is many sub-semitone steps,
                # and comparing against the start accumulated the drift and
                # split it into a new note every semitone.
                last = hit.pitch_pts[-1][1] if hit.pitch_pts else hit.period
                if _jump_st(last, period) > PSG_NOTE_JUMP_ST:
                    close(voice, t, emit_off=False)
                    open_hit(voice, t)
                    return
                if not hit.pitch_pts or hit.pitch_pts[-1][1] != period:
                    hit.pitch_pts.append((t, period))
        level = 15 - vol[voice]
        last_level = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
        if last_level != level:
            if t == hit.sample_time:
                hit.vol = level
            elif level >= last_level + PSG_ATTACK_STEP:
                # A clear attenuation jump back up is a new attack, not
                # just a volume step. Put a note on the row so it shows in
                # the pattern; the SN76489 has no envelope to restart.
                close(voice, t, emit_off=False)
                open_hit(voice, t)
                return
            elif not hit.vol_pts or hit.vol_pts[-1][1] != level:
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
        if (noise_mode & 3) == 3:
            set_noise_period(hit, t)
        level = 15 - vol[3]
        last_level = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
        if last_level != level:
            if t == hit.sample_time:
                hit.vol = level
            elif level >= last_level + PSG_ATTACK_STEP:
                # Same attack rule as the tone voices: a clear jump back
                # up is a new note, not a volume step inside one note.
                close(3, t, emit_off=False)
                open_hit(3, t)
                return
            elif not hit.vol_pts or hit.vol_pts[-1][1] != level:
                hit.vol_pts.append((t, level))

    for w in writes:
        b = w.val & 0xFF
        t = w.sample
        if w.reg == 1:
            # Game Gear stereo mask. A change during a held note is a pan
            # point; a mask that turns both sides off also mutes that voice
            # through the level column (the SMS 08xy zero reads as centre).
            if b != stereo_mask:
                stereo_mask = b
                for voice, hit in enumerate(active):
                    if hit is None:
                        continue
                    new_pan = pan_for(voice)
                    old_pan = hit.pan
                    if new_pan == old_pan:
                        continue
                    if t == hit.sample_time:
                        hit.pan = new_pan
                        hit.opened_pan = new_pan
                        if new_pan == 0:
                            hit.vol = 0
                        elif old_pan == 0:
                            hit.vol = 15 - vol[voice]
                        continue
                    if new_pan:
                        hit.pan_pts.append((t, new_pan))
                    if new_pan == 0:
                        hit.vol_pts.append((t, 0))
                    elif old_pan == 0:
                        hit.vol_pts.append((t, 15 - vol[voice]))
                    hit.pan = new_pan
            continue
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


def _collect_t6w28(
    writes: list, clock: int, warnings: list[str],
) -> tuple[list[PsgHit], list[PsgOff], list[str]]:
    """Decode the two SN76489-compatible halves of one T6W28.

    The left stream (command 0x50) owns the three tone periods and the left
    attenuation; the right stream (0x30) owns the right attenuation and the
    noise select/extra period.  Furnace's T6W28 reads an independent panL and
    panR, so the two attenuations become one volume column plus an 08xy split.
    """
    freq = [0, 0, 0]
    freq_high = [False, False, False]
    vol_l = [15, 15, 15, 15]
    vol_r = [15, 15, 15, 15]
    noise_select = 0
    noise_white = False
    noise_extra = 0
    latch_l_ch = latch_r_ch = 0
    latch_l_vol = latch_r_vol = False
    active: list[PsgHit | None] = [None, None, None, None]
    hits: list[PsgHit] = []
    offs: list[PsgOff] = []
    glitches = 0

    def levels(voice: int) -> tuple[int, int] | None:
        att_l, att_r = vol_l[voice], vol_r[voice]
        low = min(att_l, att_r)
        if low >= 15:
            return None
        pan_l = 15 - (att_l - low)
        pan_r = 15 - (att_r - low)
        return 15 - low, (pan_l << 4) | pan_r

    def close(voice: int, t: int, *, emit_off: bool = True) -> None:
        nonlocal glitches
        hit = active[voice]
        if hit is None:
            return
        hit.off_sample = t
        # A two-write gate-off (left then right at the same sample) briefly
        # looks like a one-sided fade.  Nothing at or after the close is heard.
        if t > 0:
            hit.mix_pts = [pt for pt in hit.mix_pts if pt[0] < t]
            hit.pitch_pts = [pt for pt in hit.pitch_pts if pt[0] < t]
            hit.mode_pts = [pt for pt in hit.mode_pts if pt[0] < t]
        if t <= 0:
            hits.append(hit)
        elif t - hit.sample_time >= PSG_MIN_NOTE:
            hits.append(hit)
            if emit_off:
                offs.append(PsgOff(t, voice, 0))
        else:
            glitches += 1
        active[voice] = None

    def noise_period() -> int:
        if noise_select < 3:
            value = 0x100 << noise_select
        else:
            value = noise_extra
        return max(0, min(PSG_MAX_PERIOD, value))

    def open_hit(voice: int, t: int) -> None:
        lp = levels(voice)
        if lp is None:
            return
        level, pan = lp
        active[voice] = PsgHit(
            sample_time=t,
            voice=voice,
            period=(
                freq[voice] if voice < 3 else noise_period()
            ),
            vol=level,
            chip=0,
            noise=voice == 3,
            noise_mode=(4 if noise_white else 0) if voice == 3 else 0,
            pan=pan,
            opened_pan=pan,
            t6w28=True,
        )

    def set_levels(voice: int, t: int) -> None:
        hit = active[voice]
        if hit is None:
            return
        lp = levels(voice)
        if lp is None:
            return
        level, pan = lp
        if t == hit.sample_time:
            hit.vol = level
            hit.pan = pan
            hit.opened_pan = pan
        else:
            last = (
                hit.mix_pts[-1][1:] if hit.mix_pts
                else (hit.vol, hit.pan)
            )
            if (level, pan) != last:
                hit.mix_pts.append((t, level, pan))

    def evaluate_tone(voice: int, t: int) -> None:
        lp = levels(voice)
        on = freq_high[voice] and freq[voice] > 0 and lp is not None
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
                hit.period = period
            else:
                last = hit.pitch_pts[-1][1] if hit.pitch_pts else hit.period
                if _jump_st(last, period) > PSG_NOTE_JUMP_ST:
                    close(voice, t, emit_off=False)
                    open_hit(voice, t)
                    return
                if not hit.pitch_pts or hit.pitch_pts[-1][1] != period:
                    hit.pitch_pts.append((t, period))
        set_levels(voice, t)

    def evaluate_noise(t: int) -> None:
        on = vol_l[3] < 15 or vol_r[3] < 15
        hit = active[3]
        if on and hit is None:
            open_hit(3, t)
            return
        if not on:
            if hit is not None:
                close(3, t)
            return
        assert hit is not None
        mode = 4 if noise_white else 0
        if hit.noise_mode != mode:
            if t == hit.sample_time:
                hit.noise_mode = mode
            elif not hit.mode_pts or hit.mode_pts[-1][1] != mode:
                hit.mode_pts.append((t, mode))
        period = noise_period()
        if hit.period != period:
            if t == hit.sample_time:
                hit.period = period
            elif not hit.pitch_pts or hit.pitch_pts[-1][1] != period:
                hit.pitch_pts.append((t, period))
        set_levels(3, t)

    for w in writes:
        b = w.val & 0xFF
        t = w.sample
        if w.reg == 1:
            # The Game Gear mask has no T6W28 meaning; both halves are already
            # a stereo pair.
            continue
        if w.chip_id == 0:
            if b & 0x80:
                latch_l_ch = (b >> 5) & 3
                latch_l_vol = bool(b & 0x10)
                data = b & 0x0F
                if latch_l_vol:
                    vol_l[latch_l_ch] = data
                    if latch_l_ch == 3:
                        evaluate_noise(t)
                    else:
                        evaluate_tone(latch_l_ch, t)
                elif latch_l_ch == 3:
                    # The left half's noise register does nothing on a T6W28.
                    pass
                else:
                    freq[latch_l_ch] = (freq[latch_l_ch] & 0x3F0) | data
            else:
                if latch_l_vol or latch_l_ch == 3:
                    continue
                freq[latch_l_ch] = (
                    (freq[latch_l_ch] & 0x00F) | ((b & 0x3F) << 4)
                )
                freq_high[latch_l_ch] = True
                evaluate_tone(latch_l_ch, t)
            continue
        # Right half.
        if b & 0x80:
            latch_r_ch = (b >> 5) & 3
            latch_r_vol = bool(b & 0x10)
            data = b & 0x0F
            if latch_r_vol:
                vol_r[latch_r_ch] = data
                if latch_r_ch == 3:
                    evaluate_noise(t)
                else:
                    evaluate_tone(latch_r_ch, t)
            elif latch_r_ch == 2:
                noise_extra = (noise_extra & 0x3F0) | data
                evaluate_noise(t)
            elif latch_r_ch == 3:
                noise_select = data & 3
                noise_white = bool(data & 4)
                evaluate_noise(t)
            # Right-half channel 0/1 tone writes are unused by the hardware.
        else:
            if latch_r_vol:
                continue
            if latch_r_ch == 2:
                noise_extra = (noise_extra & 0x00F) | ((b & 0x3F) << 4)
                evaluate_noise(t)
            elif latch_r_ch == 3:
                noise_select = b & 3
                noise_white = bool(b & 4)
                evaluate_noise(t)

    for voice in range(4):
        if active[voice] is not None:
            close(voice, 0, emit_off=False)
    if glitches:
        warnings.append(f"T6W28: ignored {glitches} sub-frame gates")
    return hits, offs, warnings
