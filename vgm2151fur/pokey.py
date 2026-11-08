"""Atari POKEY (4-channel PSG) -> notes -> Furnace.

Register model (VGM command 0xBB):

    0/1, 2/3, 4/5, 6/7  AUDFx / AUDCx for the four voices
    8                   AUDCTL
    15                  SKCTL (serial/keyboard; not converted)

AUDCx: low nibble volume, bit 4 volume-only (a DC level, not a tone), bits 5-7
distortion (Furnace's waveform 0-7).  There is no key-on register: a voice
sounds while its AUDC volume is non-zero.

AUDCTL:
    bit 7  9-bit poly
    bit 6  high clock for voice 1 (~1.79 MHz)
    bit 5  high clock for voice 3
    bit 4  join voices 1+2 (AUDF1 low byte, AUDF2 high byte)
    bit 3  join voices 3+4 (AUDF3 low byte, AUDF4 high byte)
    bit 2  high-pass filter 1+3
    bit 1  high-pass filter 2+4
    bit 0  15 kHz base clock for the non-high voices

A joined pair is one voice: the low channel's module column drives it, the
high channel's AUDC supplies volume and waveform, and the 16-bit AUDF value is
binary (AUDFlow | AUDFhigh << 8).  That is also how VGMPlay and Furnace's POKEY
core interpret it, so the source register value is passed through rather than
re-derived from a multiplicative prescaler.

The module's POKEY platform turns a pitch-table value into the written AUDF
with a wave/AUDCTL dependent transform (platform/pokey.cpp, tick()).  We invert
that transform, so the module writes the same AUDF the source did.  Furnace's
DIV_INS_POKEY macros are volume, arpeggio, AUDCTL, waveform and pitch; this
converter keeps AUDCTL and waveform as pattern effects (0x11/0x10) and uses
instruments for the per-frame volume contour and the pitch trajectory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from vgm2151fur.pitch import (
    E5_CENTER, furnace_period_note, module_clock,
)
from vgm2151fur.vgm import VgmFile

POKEY_VOICES = 4
POKEY_DEFAULT_CLOCK = 1_789_772
# Furnace effect numbers (sysDef.cpp POKEY map).
FX_WAVE = 0x10
FX_AUDCTL = 0x11
# Furnace's POKEY pitch table maximum (DivPlatformPOKEY::notifyPitchTable).
POKEY_PITCH_MAX = 0x0FFFFFFF

REG_AUDF = (0, 2, 4, 6)
REG_AUDC = (1, 3, 5, 7)
REG_AUDCTL = 8
REG_SKCTL = 0x0F

# A gate shorter than this is driver housekeeping, not music.
POKEY_MIN_NOTE = 80
# A loud level followed by a rise this large while the gate is open is a new
# hit (a drum roll jumps straight from the decayed level back to the attack).
POKEY_RETRIGGER_RISE = 4

# The platform's buzz/bass period snap tables (pokey.cpp).
SNAP_LONG = (0, 1, 1, 3, 3, 6, 6, 7, 7, 10, 10, 12, 12, 13, 13)
SNAP_SHORT = (2, 2, 2, 2, 5, 5, 5, 8, 8, 11, 11, 11, 11, 17, 17)
SNAP_LONG16 = (0, 0, 3, 3, 3, 5, 6, 6, 8, 9, 9, 11, 11, 14, 14)
SNAP_SHORT16 = (1, 1, 1, 4, 4, 4, 4, 4, 10, 10, 10, 10, 13, 13, 13)


def _pair_joined(audctl: int, low: int) -> bool:
    return bool(audctl & (0x10 if low == 0 else 0x08))


def _is_high(voice: int, audctl: int) -> bool:
    return bool(
        (voice == 0 and audctl & 0x40)
        or (voice == 2 and audctl & 0x20)
    )


def furnace_written_register(
    value: int, audctl: int, voice: int, wave: int, joined: bool,
) -> int:
    """Python port of DivPlatformPOKEY::tick()'s pitch conversion.

    ``value`` is the engine pitch-table value (``calcFreq()``).  Returns the
    AUDF byte/word the platform writes to the chip after its wave and AUDCTL
    adjustments, including the core's small high-clock offsets.
    """
    f = max(0, int(value))
    high = _is_high(voice, audctl)
    if not high:
        f //= 7
        if wave == 6:
            f //= 5
            f >>= 1
        elif wave == 7:
            f //= 5 if audctl & 1 else 15
            f >>= 1
        else:
            f >>= 2
    else:
        if wave == 6:
            f = (f * 2) // 5
        elif wave == 7:
            f = (f * 2) // 15
    if audctl & 1 and not high:
        f >>= 2
    f -= 1
    if f < 0:
        f = 0
    min_freq = 255
    if wave == 7:
        if high:
            f = 15 * (f // 15) + SNAP_LONG16[f % 15] + 1
        elif not (audctl & 1):
            f = 15 * (f // 15) + SNAP_LONG[f % 15]
    elif wave == 6:
        if high:
            f = 15 * (f // 15) + SNAP_SHORT16[f % 15] + 1
        elif not (audctl & 1):
            f = 15 * (f // 15) + SNAP_SHORT[f % 15]
        min_freq = 251
    if joined:
        if f > 65535:
            f = 65535
    else:
        if f > min_freq:
            f = min_freq
    return f


@lru_cache(maxsize=8192)
def _invert_register(
    target: int, audctl: int, voice: int, wave: int, joined: bool,
) -> int:
    """Smallest pitch-table value whose platform register is near ``target``.

    The platform transform is monotone nondecreasing in ``value`` (the wave
    snapping only quantizes it), so a binary search finds the block and a few
    neighbours pick the closest representable register.
    """
    target = max(0, min(65535 if joined else 255, target))

    def written(v: int) -> int:
        return furnace_written_register(v, audctl, voice, wave, joined)

    lo, hi = 1, 1 << 24
    if written(hi) < target:
        best = hi
    else:
        while lo < hi:
            mid = (lo + hi) // 2
            if written(mid) < target:
                lo = mid + 1
            else:
                hi = mid
        best = lo
    best_err = abs(written(best) - target)
    for cand in (best - 3, best - 2, best - 1, best + 1, best + 2, best + 3):
        if cand < 1:
            continue
        err = abs(written(cand) - target)
        if err < best_err:
            best, best_err = cand, err
    return best


def pokey_value(
    audf: int, audctl: int, voice: int, wave: int, *, joined: bool = False,
    high: int | None = None,
) -> int:
    """Source AUDF (or a joined pair's 16-bit register) -> pitch-table value.

    ``voice`` is the module voice (0-3).  For a joined pair pass the pair's
    low byte as ``audf`` and the high byte as ``high``; the target is the
    binary word Furnace and VGMPlay both write.
    """
    target = audf & 0xFF
    if joined:
        target |= (high or 0) << 8
    return _invert_register(target, audctl & 0xFF, voice, wave & 7, joined)


def pokey_note(clock: int | None, value: int) -> tuple[int, int]:
    """Pitch-table value -> (Furnace note, E5 fine tune)."""
    return furnace_period_note(
        module_clock(clock or POKEY_DEFAULT_CLOCK), 1, max(1, int(value)),
        maximum=POKEY_PITCH_MAX,
    )


def pokey_semitones(clock: int | None, value: int) -> float:
    note, e5 = pokey_note(clock, value)
    return note + (e5 - E5_CENTER) / 128.0


@dataclass
class PokeyHit:
    sample_time: int
    chip_id: int
    voice: int  # module column (0-3); a joined pair lives on its low voice
    value: int  # Furnace pitch-table value at the gate
    vol: int  # AUDC volume 0-15
    clock: int
    off_sample: int = 0  # 0 = still sounding at the end
    wave: int = 5  # effective waveform at the gate
    freq_pts: list[tuple[int, int]] = field(default_factory=list)
    vol_pts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class PokeyOff:
    sample_time: int
    chip_id: int
    voice: int


def _blank_chip() -> dict:
    return {
        "audf": [0, 0, 0, 0],
        "vol": [0, 0, 0, 0],
        "wave": [0, 0, 0, 0],
        "volonly": [False, False, False, False],
        "audctl": 0,
        "skctl": 0,
        "active": [None, None, None, None],
    }


def collect_pokey(
    vgm: VgmFile,
) -> tuple[
    list[PokeyHit], list[PokeyOff],
    list[tuple[int, int, int]], list[tuple[int, int, int, int]],
    list[str],
]:
    """Walk a POKEY register stream into notes.

    Returns ``(hits, offs, audctl_pts, wave_pts, warnings)``.  AUDCTL is a
    chip-wide register, so its changes are returned as ``(sample, chip,
    value)``; waveform changes are per module column as ``(sample, chip,
    voice, wave)``.  The emitter turns both into 0x11/0x10 row effects.
    """
    warnings: list[str] = []
    hits: list[PokeyHit] = []
    offs: list[PokeyOff] = []
    audctl_pts: list[tuple[int, int, int]] = []
    wave_pts: list[tuple[int, int, int, int]] = []
    chips = sorted({w.chip_id for w in vgm.writes if w.chip == "pokey"})
    if not chips:
        return [], [], [], [], warnings
    base_clock = vgm.pokey_clock or POKEY_DEFAULT_CLOCK
    clocks = {
        cid: (
            (vgm.pokey_clock_2 or base_clock) if cid == 1
            else base_clock
        )
        for cid in chips
    }
    state: dict[int, dict] = {cid: _blank_chip() for cid in chips}
    warnings_seen: dict[str, int] = {
        "volume_only": 0, "two_tone": 0, "glitch": 0,
    }

    def joined_low(voice: int, st: dict) -> int | None:
        low = voice & ~1
        return low if _pair_joined(st["audctl"], low) else None

    def effective_volume(voice: int, st: dict) -> int:
        low = joined_low(voice, st)
        ctrl = (low + 1) if low is not None else voice
        return 0 if st["volonly"][ctrl] else st["vol"][ctrl]

    def effective_wave(voice: int, st: dict) -> int:
        low = joined_low(voice, st)
        ctrl = (low + 1) if low is not None else voice
        return st["wave"][ctrl] & 7

    def audible(voice: int, st: dict) -> bool:
        low = joined_low(voice, st)
        if low is not None and voice != low:
            return False
        return effective_volume(voice, st) > 0

    def value_for(voice: int, st: dict) -> int:
        low = joined_low(voice, st)
        if low is not None:
            return pokey_value(
                st["audf"][low], st["audctl"], low,
                st["wave"][low + 1], joined=True, high=st["audf"][low + 1],
            )
        return pokey_value(
            st["audf"][voice], st["audctl"], voice, st["wave"][voice],
        )

    def close(st: dict, cid: int, voice: int, t: int, *, emit_off: bool = True) -> None:
        hit = st["active"][voice]
        if hit is None:
            return
        hit.off_sample = t
        if t - hit.sample_time >= POKEY_MIN_NOTE:
            hits.append(hit)
            if emit_off:
                offs.append(PokeyOff(t, cid, voice))
        else:
            warnings_seen["glitch"] += 1
        st["active"][voice] = None

    def open_hit(st: dict, cid: int, voice: int, t: int, value: int) -> None:
        wave = effective_wave(voice, st)
        hit = PokeyHit(
            sample_time=t,
            chip_id=cid,
            voice=voice,
            value=value,
            vol=effective_volume(voice, st),
            clock=clocks[cid],
            wave=wave,
        )
        st["active"][voice] = hit
        # The module starts every channel on waveform 5, so the first note of
        # a channel must state the source's waveform even when the source
        # never changed it from its own reset value.
        wave_pts.append((t, cid, voice, wave))

    def refresh(st: dict, cid: int, voice: int, t: int) -> None:
        hit = st["active"][voice]
        on = audible(voice, st)
        if not on:
            if hit is not None:
                close(st, cid, voice, t)
            return
        value = value_for(voice, st)
        if hit is None:
            open_hit(st, cid, voice, t, value)
            return
        cur = hit.freq_pts[-1][1] if hit.freq_pts else hit.value
        if value != cur:
            hit.freq_pts.append((t, value))
        wave = effective_wave(voice, st)
        if wave != hit.wave:
            hit.wave = wave
            wave_pts.append((t, cid, voice, wave))
        level = effective_volume(voice, st)
        prev = hit.vol_pts[-1][1] if hit.vol_pts else hit.vol
        if level != prev:
            if level - prev >= POKEY_RETRIGGER_RISE:
                # A drum roll jumps straight from the decayed level back to
                # the attack while the gate stays open.  Start a new note so
                # the hit reaches the pattern instead of living in the tail of
                # the previous volume macro.
                close(st, cid, voice, t, emit_off=False)
                open_hit(st, cid, voice, t, value)
            else:
                hit.vol_pts.append((t, level))

    def module_voice_for_audf(voice: int, st: dict) -> int:
        low = joined_low(voice, st)
        return low if low is not None else voice

    for w in vgm.writes:
        if w.chip != "pokey":
            continue
        cid = w.chip_id
        st = state.setdefault(cid, _blank_chip())
        if w.reg > REG_SKCTL:
            continue
        r = w.reg
        val = w.val & 0xFF
        t = w.sample
        if r == REG_AUDCTL:
            if val == st["audctl"]:
                continue
            old = st["audctl"]
            st["audctl"] = val
            audctl_pts.append((t, cid, val))
            # Join transitions move a pair between two columns.
            for low in (0, 2):
                bit = 0x10 if low == 0 else 0x08
                was = bool(old & bit)
                now = bool(val & bit)
                if was == now:
                    continue
                high = low + 1
                if st["active"][low] is not None:
                    close(st, cid, low, t, emit_off=False)
                if st["active"][high] is not None:
                    close(st, cid, high, t, emit_off=False)
                if now:
                    if audible(low, st):
                        open_hit(st, cid, low, t, value_for(low, st))
                else:
                    if audible(low, st):
                        open_hit(st, cid, low, t, value_for(low, st))
                    if audible(high, st):
                        open_hit(st, cid, high, t, value_for(high, st))
            # The clock/join bits retune every sounding voice.
            for voice in range(POKEY_VOICES):
                if st["active"][voice] is not None:
                    refresh(st, cid, voice, t)
            continue
        if r == REG_SKCTL:
            if val & 0x80 and not st["skctl"] & 0x80:
                warnings_seen["two_tone"] += 1
            st["skctl"] = val
            continue
        if r in REG_AUDF:
            voice = r // 2
            st["audf"][voice] = val
            module = module_voice_for_audf(voice, st)
            # A low join byte that is not the output byte still changes the
            # pair's combined period.
            if st["active"][module] is not None:
                refresh(st, cid, module, t)
            continue
        if r in REG_AUDC:
            voice = r // 2
            st["vol"][voice] = val & 0x0F
            st["wave"][voice] = (val >> 5) & 7
            st["volonly"][voice] = bool(val & 0x10)
            if val & 0x10:
                warnings_seen["volume_only"] += 1
            low = joined_low(voice, st)
            if low is not None and voice == low:
                # The joined pair's output is controlled by the high byte's
                # AUDC; the low register is ignored while joined.
                continue
            module = low if low is not None else voice
            refresh(st, cid, module, t)
            continue

    for cid, st in state.items():
        for voice in range(POKEY_VOICES):
            hit = st["active"][voice]
            if hit is not None:
                hit.off_sample = 0
                hits.append(hit)
                st["active"][voice] = None

    if warnings_seen["volume_only"]:
        warnings.append(
            f"{warnings_seen['volume_only']} POKEY volume-only (AUDC bit 4) "
            "writes are not converted (Furnace has no DC/DAC mode)"
        )
    if warnings_seen["two_tone"]:
        warnings.append(
            f"{warnings_seen['two_tone']} POKEY two-tone (SKCTL bit 7) setup "
            "writes are not converted"
        )
    if warnings_seen["glitch"]:
        warnings.append(
            f"{warnings_seen['glitch']} sub-frame POKEY gate(s) skipped"
        )
    return hits, offs, audctl_pts, wave_pts, warnings
