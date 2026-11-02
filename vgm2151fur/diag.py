"""Register-level diagnostics for a conversion.

The source VGM and the Furnace export are the two register streams to
compare, channel by channel. Pitch is the sensor: place and trigger errors
show up as missing notes, while a drifting pitch stream shows as a run where
the export sits away from the source. `pitch_runs` measures those runs and
prints them with the register values at the worst point, which is enough to
tell a lost glide from a wrong base pitch.

`export_fur` renders the .fur to a .vgm with the bundled Furnace build. The
export is the ground truth for what the .fur means to a player. Every
comparison here starts with it.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from vgm2151fur.analyze import kc_kf_units
from vgm2151fur.ay8910 import ay_min_period
from vgm2151fur.furnace import export_vgm
from vgm2151fur.opl import (
    N as OPL_N, SLOT_ADDR, OPLState, channel_count, is_drum_channel,
    opl_pitch_units,
)
from vgm2151fur.vgm import VGM_RATE, load_vgm
from vgm2151fur.ym2203 import opn_pitch_units
from vgm2151fur.ym2610 import (
    CHAN_OFF_4,
    CHAN_OFF_6,
    KON_4,
    KON_6,
    YM2610_DEFAULT_CLOCK,
)
from vgm2151fur.ym2608 import YM2608_DEFAULT_CLOCK, YM2608_SSG_DIVIDER
from vgm2151fur.ym2612 import EXT_FREQ_OP, ym2612_pitch_units


@dataclass
class PitchRun:
    t0: int
    t1: int
    worst_st: float

    def __str__(self) -> str:
        return (
            f"{self.t0 / 44100:7.3f}-{self.t1 / 44100:7.3f}s "
            f"({(self.t1 - self.t0) / 44.1:5.0f} ms)  {self.worst_st:+.2f} st"
        )


def kc_kf_stream(vgm, ch: int) -> list[tuple[int, int]]:
    """(sample, pitch units) after each KC/KF write on one YM2151 channel."""
    kc = kf = None
    out: list[tuple[int, int]] = []
    for w in vgm.writes:
        if w.chip != "ym2151":
            continue
        if 0x28 <= w.reg <= 0x2F and (w.reg & 7) == ch:
            kc = w.val
        elif 0x30 <= w.reg <= 0x37 and (w.reg & 7) == ch:
            kf = w.val
        else:
            continue
        if kc is not None and kf is not None:
            out.append((w.sample, kc_kf_units(kc, kf)))
    return out


def _opn_chip_ids(vgm) -> list[int]:
    return sorted({
        w.chip_id for w in vgm.writes if w.chip in ("ym2203", "ym2203_ssg")
    })


def _opn_ext(vgm) -> bool:
    """True for the YM2203's nine-channel channel-3-operator layout."""
    return any(
        w.chip == "ym2203" and 0xA8 <= w.reg <= 0xAE for w in vgm.writes
    )


def opn_ssg_units(period: int) -> float:
    """Pitch for an AY/SSG tone period; larger is higher, differences are
    meaningful (the clock/divider constant cancels between two logs)."""
    return -768.0 * math.log2(max(1, period))


def _opn_ssg_stream(vgm, cid: int, ssg: int) -> list[tuple[int, int]]:
    return _ssg_stream(vgm, "ym2203_ssg", cid, ssg,
                       vgm.ym2203_clock or 3579545, 16)


def opn_pitch_stream(vgm, ch: int) -> list[tuple[int, int]]:
    """(sample, pitch units) for one YM2203 voice.

    A standard YM2203 owns six Furnace channels: three FM then three SSG.
    The channel-3-operator layout owns nine: FM1, FM2, four FM3 operator
    columns, then the three SSG voices.  The FM stream uses f-num/block
    units; the SSG stream uses the tone period.
    """
    ids = _opn_chip_ids(vgm)
    ext = _opn_ext(vgm)
    chunk = 9 if ext else 6
    cidx, voice = divmod(ch, chunk)
    if cidx >= len(ids):
        return []
    cid = ids[cidx]
    ssg_at = 6 if ext else 3
    if voice >= ssg_at:
        return _opn_ssg_stream(vgm, cid, voice - ssg_at)

    ext_col = ext and voice >= 2
    src_ch = 2 if ext_col else voice
    group = EXT_FREQ_OP[voice - 2] if ext_col else 3
    wanted = group
    out: list[tuple[int, int]] = []
    lo = hi = None
    slo = [0, 0, 0]
    shi = [0, 0, 0]
    value: list[tuple[int, int] | None] = [None, None, None, None]
    special = False

    def emit(sample: int) -> None:
        g = wanted if (ext_col and special) else 3
        if value[g] is not None:
            out.append((sample, opn_pitch_units(*value[g])))

    for w in vgm.writes:
        if w.chip != "ym2203" or w.chip_id != cid:
            continue
        r = w.reg
        if r == 0x27:
            special = bool(w.val & 0xC0)
            if ext_col:
                emit(w.sample)
            continue
        if 0xA0 <= r <= 0xA6:
            if (r & 3) != src_ch:
                continue
            if r < 0xA4:
                lo = w.val
                if hi is not None:
                    value[3] = ((hi >> 3) & 7, lo | ((hi & 7) << 8))
                    emit(w.sample)
            else:
                hi = w.val
                if lo is not None:
                    value[3] = ((hi >> 3) & 7, lo | ((hi & 7) << 8))
                    emit(w.sample)
            continue
        if ext and 0xA8 <= r <= 0xAE:
            idx = r & 3
            if idx >= 3:
                continue
            if r <= 0xAA:
                slo[idx] = w.val
            else:
                shi[idx] = w.val
            value[idx] = ((shi[idx] >> 3) & 7,
                          slo[idx] | ((shi[idx] & 7) << 8))
            if ext_col and idx == group:
                emit(w.sample)
    return out


def state_at(stream: list[tuple[int, int]], t: int) -> int | None:
    i = bisect_right([s for s, _v in stream], t) - 1
    return stream[i][1] if i >= 0 else None


def opn_keyed_intervals(vgm, ch: int) -> list[tuple[int, int]]:
    """Samples where one YM2203 voice is sounding.

    `ch` uses the module layout: six channels per chip (three FM then three
    SSG) or nine when the source uses the channel-3-operator layout (FM1,
    FM2, four FM3 columns, three SSG).  FM uses the 0x28 operator mask (a
    column follows its own mask bit); SSG uses its mixer tone bit and level
    register.  Chips the log does not contain return an empty list.
    """
    ids = _opn_chip_ids(vgm)
    ext = _opn_ext(vgm)
    chunk = 9 if ext else 6
    chip, voice = divmod(ch, chunk)
    if chip >= len(ids):
        return []
    cid = ids[chip]
    out: list[tuple[int, int]] = []
    on = False
    start = 0
    ssg_at = 6 if ext else 3
    if voice >= ssg_at:
        # Noise-only percussion has no meaningful tone period, and a period
        # below the module's representable range is the driver's silence gate:
        # both are handled by the shared SSG walker, which gates on a tone-only
        # mixer and an in-range period.
        return _ssg_keyed_intervals(vgm, "ym2203_ssg", cid, voice - ssg_at,
                                    vgm.ym2203_clock or 3579545, 16)
    else:
        ext_col = ext and voice >= 2
        target_ch = 2 if ext_col else voice
        bit = (1 << (voice - 2)) if ext_col else None
        for w in vgm.writes:
            if (w.chip != "ym2203" or w.chip_id != cid or w.reg != 0x28
                    or (w.val & 3) != target_ch):
                continue
            mask = (w.val >> 4) & 0xF
            now = bool(mask & bit) if bit is not None else bool(mask)
            if now and not on:
                start = w.sample
                on = True
            elif not now and on:
                out.append((start, w.sample))
                on = False
    if on:
        end = vgm.total_samples or (
            max((w.sample for w in vgm.writes), default=start) + 1
        )
        out.append((start, end))
    return out


def _ym2612_chip_ids(vgm, chip: str = "ym2612") -> list[int]:
    return sorted({w.chip_id for w in vgm.writes if w.chip == chip})


def _ym2612_ext(vgm, chip: str = "ym2612") -> bool:
    """True when the source ever selects channel-3 special/CSM mode.

    The mode register is on port 0 for every two-port OPN chip; the YM2608's
    second FM block answers the same `$27` on its own port.
    """
    return any(
        w.chip == chip and not (w.reg & 0x100) and (w.reg & 0xFF) == 0x27
        and (w.val & 0xC0)
        for w in vgm.writes
    )


def _ym2612_channel_map(
    vgm, ch: int, chip: str = "ym2612",
) -> tuple[int, str, int] | None:
    """(chip id, kind, voice/column) for one module channel.

    `kind` is "fm" for a normal channel, "ext" for a channel-3 operator
    column.  Returns None when the module layout has no such channel.
    """
    ids = _ym2612_chip_ids(vgm, chip)
    chunk = 9 if _ym2612_ext(vgm, chip) else 6
    chip_i, voice = divmod(ch, chunk)
    if chip_i >= len(ids):
        return None
    cid = ids[chip_i]
    if voice < 2:
        return cid, "fm", voice
    if chunk == 9:
        if voice < 6:
            return cid, "ext", voice - 2
        return cid, "fm", voice - 3
    return cid, "fm", voice


def ym2612_pitch_stream(
    vgm, ch: int, chip: str = "ym2612",
) -> list[tuple[int, int]]:
    """(sample, pitch units) for one two-port OPN module channel.

    The normal layout follows the channel's latched f-num/block.  The Ext
    layout follows the four channel-3 operator pairs for its columns.  Every
    two-port OPN chip - YM2612 and YM2608 alike - uses this one mapping,
    because the frequency registers and their formula are shared.
    """
    mapped = _ym2612_channel_map(vgm, ch, chip)
    if mapped is None:
        return []
    cid, kind, voice = mapped
    group = EXT_FREQ_OP[voice] if kind == "ext" else 3
    out: list[tuple[int, int]] = []
    hi = [None] * 6
    shi = [None] * 3
    value: list[tuple[int, int] | None] = [None, None, None, None]
    voice_value: tuple[int, int] | None = None
    mode = 0

    def emit(sample: int) -> None:
        if kind == "ext":
            g = group if mode else 3
            if value[g] is not None:
                out.append((sample, ym2612_pitch_units(*value[g])))
        elif voice_value is not None:
            out.append((sample, ym2612_pitch_units(*voice_value)))

    for w in vgm.writes:
        if w.chip != chip or w.chip_id != cid:
            continue
        r = w.reg & 0xFF
        bank = bool(w.reg & 0x100)
        if not bank and r == 0x27:
            if kind == "ext":
                mode = (w.val >> 6) & 3
                emit(w.sample)
            continue
        if not bank and 0xA8 <= r <= 0xAE:
            idx = r & 3
            if idx == 3:
                continue
            if r < 0xAC:
                if shi[idx] is not None:
                    block = (shi[idx] >> 3) & 7
                    fnum = w.val | ((shi[idx] & 7) << 8)
                    value[idx] = (block, fnum)
                    shi[idx] = None
                    emit(w.sample)
            else:
                shi[idx] = w.val
            continue
        if 0xA0 <= r <= 0xA6:
            src_ch = (r & 3) + (3 if bank else 0)
            if (r & 3) == 3:
                continue
            if r < 0xA4:
                if hi[src_ch] is not None:
                    block = (hi[src_ch] >> 3) & 7
                    fnum = w.val | ((hi[src_ch] & 7) << 8)
                    if src_ch == 2:
                        value[3] = (block, fnum)
                        if kind == "ext" and (mode == 0 or group == 3):
                            emit(w.sample)
                        elif kind == "fm" and voice == 2:
                            voice_value = (block, fnum)
                            emit(w.sample)
                    elif kind == "fm" and src_ch == voice:
                        voice_value = (block, fnum)
                        emit(w.sample)
                    hi[src_ch] = None
            else:
                hi[src_ch] = w.val
    return out


def ym2612_keyed_intervals(
    vgm, ch: int, chip: str = "ym2612", *, dac: bool = True,
) -> list[tuple[int, int]]:
    """Samples where one two-port OPN module channel is keyed.

    In the Ext layout channel-3 columns follow the 0x28 mask bits; the other
    channels use their own key state.  DAC playback on channel 6 is not FM,
    so a keyed FM6 under DAC enable is treated as silent - that test is the
    chip's own, since `$2B` is an IRQ register on the YM2608.
    """
    mapped = _ym2612_channel_map(vgm, ch, chip)
    if mapped is None:
        return []
    cid, kind, voice = mapped
    if kind == "ext":
        target_ch = 2
        bit = 1 << voice
    else:
        target_ch = voice
        bit = None
    out: list[tuple[int, int]] = []
    on = False
    start = 0
    dac_on = False
    for w in vgm.writes:
        if w.chip != chip or w.chip_id != cid:
            continue
        r = w.reg & 0xFF
        if dac and not (w.reg & 0x100) and r == 0x2B:
            dac_on = bool(w.val & 0x80)
            continue
        if r != 0x28 or (w.reg & 0x100):
            continue
        raw_ch = w.val & 7
        if raw_ch in (3, 7):
            continue
        src_ch = raw_ch if raw_ch < 3 else raw_ch - 1
        if src_ch != target_ch:
            continue
        mask = (w.val >> 4) & 0x0F
        now = bool(mask & bit) if bit is not None else bool(mask)
        if dac and target_ch == 5 and dac_on:
            now = False
        if now and not on:
            start = w.sample
            on = True
        elif not now and on:
            out.append((start, w.sample))
            on = False
    if on:
        end = vgm.total_samples or (
            max((w.sample for w in vgm.writes), default=start) + 1
        )
        out.append((start, end))
    return out


def _opl_kind(vgm) -> str | None:
    """The OPL family this log drives, if any."""
    for kind in ("opl3", "opl2", "opl"):
        if any(w.chip == kind for w in vgm.writes):
            return kind
    return None


def _opl_channel(vgm, ch: int) -> tuple[str, int, int] | None:
    """(kind, chip_id, column) of a module channel.

    Returns None when the channel is not an OPL voice it can measure: past the
    last chip, or one of a rhythm module's kit columns. Two percussion voices
    share one frequency register there - snare with hi-hat, tom and cymbal with
    each other. The byte at a hit belongs to whichever was written last and
    says nothing about the voice being compared.
    """
    kind = _opl_kind(vgm)
    if kind is None:
        return None
    drums = any(
        w.chip == kind and (w.reg & 0xFF) == 0xBD and (w.val & 0x20)
        for w in vgm.writes
    )
    chunk = channel_count(kind, drums)
    ids = sorted({w.chip_id for w in vgm.writes if w.chip == kind}) or [0]
    index, col = divmod(ch, chunk)
    if index >= len(ids):
        return None
    if drums and is_drum_channel(kind, True, col):
        return None
    return kind, ids[index], col


def opl_pitch_stream(vgm, ch: int) -> list[tuple[int, int]]:
    """(sample, pitch units) for one OPL-family voice channel.

    The unit is ``fnum << block`` expressed in 1/64 semitone. A comparison
    between two streams of the same chip is therefore a pitch difference. Only the channel
    that owns the frequency register is followed: on an OPL3 four-operator pair
    that is the head, whose partner stays silent.
    """
    mapped = _opl_channel(vgm, ch)
    if mapped is None:
        return []
    kind, cid, col = mapped
    st = OPLState(kind, False)
    cm = st.chan_map[col]
    port = ((cm >> 8) & 1) if st.opl3 else 0
    off = cm & 0xFF
    lo: int | None = None
    out: list[tuple[int, int]] = []
    for w in vgm.writes:
        if w.chip != kind or w.chip_id != cid:
            continue
        if st.opl3 and ((w.reg >> 8) & 1) != port:
            continue
        r = w.reg & 0xFF
        if r == 0xA0 + off:
            lo = w.val
        elif r == 0xB0 + off and lo is not None:
            block = (w.val >> 2) & 7
            out.append((w.sample, int(round(opl_pitch_units(
                block, ((w.val & 3) << 8) | lo)))))
    return out


def opl_keyed_intervals(vgm, ch: int) -> list[tuple[int, int]]:
    """Spans where one OPL-family voice holds its key bit."""
    mapped = _opl_channel(vgm, ch)
    if mapped is None:
        return []
    kind, cid, col = mapped
    st = OPLState(kind, False)
    cm = st.chan_map[col]
    port = ((cm >> 8) & 1) if st.opl3 else 0
    target = (port, 0xB0 + (cm & 0xFF))
    out: list[tuple[int, int]] = []
    on = False
    start = 0
    for w in vgm.writes:
        if w.chip != kind or w.chip_id != cid:
            continue
        addr = (((w.reg >> 8) & 1) if st.opl3 else 0, w.reg & 0xFF)
        if addr != target:
            continue
        now = bool(w.val & 0x20)
        if now and not on:
            start, on = w.sample, True
        elif not now and on:
            out.append((start, w.sample))
            on = False
    if on:
        end = vgm.total_samples or (
            max((w.sample for w in vgm.writes), default=start) + 1
        )
        out.append((start, end))
    return out


def _ym2610_chip_ids(vgm) -> list[int]:
    return sorted({
        w.chip_id for w in vgm.writes if w.chip in ("ym2610", "ym2610_ssg")
    })


def _ym2610_fm_count(vgm) -> int:
    return 6 if getattr(vgm, "ym2610_b", False) else 4


def _ym2610_channel_map(vgm, ch: int) -> tuple[int, str, int] | None:
    """(chip id, kind, voice) for one YM2610 module channel."""
    ids = _ym2610_chip_ids(vgm)
    fm_count = _ym2610_fm_count(vgm)
    chunk = fm_count + 10
    chip, voice = divmod(ch, chunk)
    if chip >= len(ids):
        return None
    cid = ids[chip]
    if voice < fm_count:
        return cid, "fm", voice
    if voice < fm_count + 3:
        return cid, "ssg", voice - fm_count
    if voice < fm_count + 9:
        return cid, "adpcm_a", voice - (fm_count + 3)
    if voice == fm_count + 9:
        return cid, "adpcm_b", 0
    return None


def _ssg_stream(vgm, ssg_chip: str, cid: int, voice: int, clock: int,
                divider: int) -> list[tuple[int, int]]:
    """(sample, pitch units) from one SSG voice's tone period."""
    out: list[tuple[int, int]] = []
    lo = hi = None
    min_period = ay_min_period(clock, divider)
    for w in vgm.writes:
        if w.chip != ssg_chip or w.chip_id != cid:
            continue
        if w.reg == voice * 2:
            lo = w.val
        elif w.reg == voice * 2 + 1:
            hi = w.val
        else:
            continue
        if lo is not None and hi is not None:
            period = (lo & 0xFF) | ((hi & 0x0F) << 8)
            if period >= min_period:
                out.append((w.sample, round(opn_ssg_units(period))))
    return out


def _ssg_keyed_intervals(vgm, ssg_chip: str, cid: int, voice: int,
                         clock: int, divider: int) -> list[tuple[int, int]]:
    """Spans where one SSG voice has its tone bit on and a level to say so."""
    out: list[tuple[int, int]] = []
    mixer = 0
    vol = [0, 0, 0]
    lo = hi = 0
    on = False
    start = 0
    min_period = ay_min_period(clock, divider)
    for w in vgm.writes:
        if w.chip != ssg_chip or w.chip_id != cid:
            continue
        if w.reg == 7:
            mixer = w.val
        elif w.reg in (8, 9, 10):
            vol[w.reg - 8] = w.val
        elif w.reg == voice * 2:
            lo = w.val
        elif w.reg == voice * 2 + 1:
            hi = w.val
        else:
            continue
        level = vol[voice]
        period = (lo & 0xFF) | ((hi & 0x0F) << 8)
        now = (not (mixer & (1 << voice))
               and (mixer & (1 << (voice + 3)))
               and ((level & 0x10) or (level & 0x0F))
               and period >= min_period)
        if now and not on:
            start = w.sample
            on = True
        elif not now and on:
            out.append((start, w.sample))
            on = False
    if on:
        out.append((start, vgm.total_samples or (
            max((w.sample for w in vgm.writes), default=start) + 1)))
    return out


def _ym2610_ssg_stream(vgm, cid: int, ssg: int) -> list[tuple[int, int]]:
    return _ssg_stream(vgm, "ym2610_ssg", cid, ssg,
                       vgm.ym2610_clock or YM2610_DEFAULT_CLOCK, 32)


def ym2610_pitch_stream(vgm, ch: int) -> list[tuple[int, int]]:
    """(sample, pitch units) for one YM2610 module channel.

    FM follows the latched f-num/block.  The SSG follows its tone period.
    ADPCM channels have no comparable pitch and return nothing.
    """
    mapped = _ym2610_channel_map(vgm, ch)
    if mapped is None:
        return []
    cid, kind, voice = mapped
    if kind == "ssg":
        return _ym2610_ssg_stream(vgm, cid, voice)
    if kind != "fm":
        return []
    offs = CHAN_OFF_6 if _ym2610_fm_count(vgm) == 6 else CHAN_OFF_4
    bank, off = offs[voice]
    base = 0x100 if bank else 0
    lo_reg = base | (0xA0 + off)
    hi_reg = base | (0xA4 + off)
    out: list[tuple[int, int]] = []
    lo = hi = None
    for w in vgm.writes:
        if w.chip != "ym2610" or w.chip_id != cid:
            continue
        if w.reg == lo_reg:
            lo = w.val
            if hi is not None:
                out.append((w.sample, opn_pitch_units((hi >> 3) & 7,
                                                      lo | ((hi & 7) << 8))))
        elif w.reg == hi_reg:
            hi = w.val
            if lo is not None:
                out.append((w.sample, opn_pitch_units((hi >> 3) & 7,
                                                      lo | ((hi & 7) << 8))))
    return out


def ym2610_keyed_intervals(vgm, ch: int) -> list[tuple[int, int]]:
    """Samples where one YM2610 module voice is sounding.

    FM uses its 0x28 channel field; the SSG uses the mixer tone bit and
    level register.  ADPCM channels are not pitch-compared.
    """
    mapped = _ym2610_channel_map(vgm, ch)
    if mapped is None:
        return []
    cid, kind, voice = mapped
    out: list[tuple[int, int]] = []
    on = False
    start = 0
    if kind == "ssg":
        return _ssg_keyed_intervals(vgm, "ym2610_ssg", cid, voice,
                                    vgm.ym2610_clock or YM2610_DEFAULT_CLOCK, 32)
    elif kind == "fm":
        kon = KON_6 if _ym2610_fm_count(vgm) == 6 else KON_4
        for w in vgm.writes:
            if (w.chip != "ym2610" or w.chip_id != cid
                    or (w.reg & 0x100) or (w.reg & 0xFF) != 0x28):
                continue
            if kon.get(w.val & 7) != voice:
                continue
            now = bool((w.val >> 4) & 0x0F)
            if now and not on:
                start = w.sample
                on = True
            elif not now and on:
                out.append((start, w.sample))
                on = False
    else:
        return []

    if on:
        end = vgm.total_samples or (
            max((w.sample for w in vgm.writes), default=start) + 1
        )
        out.append((start, end))
    return out


def _ym2608_channel_map(vgm, ch: int) -> tuple[int, str, int] | None:
    """(chip id, kind, walk channel) for one YM2608 module channel.

    `kind` is "fm" for any FM voice or channel-3 operator column - the walk
    channel then indexes a run of six per chip, or nine when channel 3 is split,
    which is what :func:`ym2612_pitch_stream` expects - "ssg" for one of the
    three tone voices, and "rhythm" or "delta_t" for the sample columns.
    """
    ids = _ym2612_chip_ids(vgm, "ym2608")
    fm_count = 9 if _ym2612_ext(vgm, "ym2608") else 6
    chunk = fm_count + 3 + 6 + 1
    idx, voice = divmod(ch, chunk)
    if idx >= len(ids):
        return None
    cid = ids[idx]
    if voice < fm_count:
        return cid, "fm", idx * fm_count + voice
    if voice < fm_count + 3:
        return cid, "ssg", voice - fm_count
    if voice < fm_count + 9:
        return cid, "rhythm", voice - (fm_count + 3)
    return cid, "delta_t", 0


def ym2608_pitch_stream(vgm, ch: int) -> list[tuple[int, int]]:
    """(sample, pitch units) for one YM2608 module channel.

    The OPNA's FM voices - split channel 3 included - are the same two-port
    OPN registers, so the stream comes from that walk with the chip swapped.
    Its SSG voices follow tone periods; rhythm and delta-T have no pitch to
    compare, exactly as on the YM2610.
    """
    mapped = _ym2608_channel_map(vgm, ch)
    if mapped is None:
        return []
    cid, kind, voice = mapped
    if kind == "fm":
        return ym2612_pitch_stream(vgm, voice, chip="ym2608")
    if kind == "ssg":
        return _ssg_stream(vgm, "ym2608_ssg", cid, voice,
                           vgm.ym2608_clock or YM2608_DEFAULT_CLOCK,
                           YM2608_SSG_DIVIDER)
    return []


def ym2608_keyed_intervals(vgm, ch: int) -> list[tuple[int, int]]:
    """Samples where one YM2608 module voice is sounding."""
    mapped = _ym2608_channel_map(vgm, ch)
    if mapped is None:
        return []
    cid, kind, voice = mapped
    if kind == "fm":
        return ym2612_keyed_intervals(vgm, voice, chip="ym2608", dac=False)
    if kind == "ssg":
        return _ssg_keyed_intervals(vgm, "ym2608_ssg", cid, voice,
                                    vgm.ym2608_clock or YM2608_DEFAULT_CLOCK,
                                    YM2608_SSG_DIVIDER)
    return []


def opl_attacks(vgm, kind: str | None = None, drums: bool | None = None):
    """[(chip_id, column, sample, register image)] at each key-on, folded per sample.

    A driver that re-attacks quickly writes the new voice's operator levels and
    its key bit in one sample, interleaved with the previous note's release. The
    chip applies those in stream order, and a tracker row holds one value. The
    state that matters - and the state :mod:`vgm2151fur.analyze` snapshots when
    it opens a note - is the register set once that sample is finished. This walk
    gives exactly that: every write of a sample is applied, then its key edges
    emit an image. Comparing two streams this way says whether a converted module
    hands the chip the same registers the rip did.

    Bank B of an OPL3 keeps its number in bit 8, and the keys it reports are
    columns of Furnace's own table. A four-operator voice and the kit's shared
    frequency registers are named as the engine names them.
    """
    if kind is None:
        kind = _opl_kind(vgm)
        if kind is None:
            return []
    st = OPLState(kind, bool(drums) if drums is not None else any(
        w.chip == kind and (w.reg & 0xFF) == 0xBD and (w.val & 0x20)
        for w in vgm.writes))
    chan_of: dict[tuple[int, int], list[int]] = {}
    for fch, cm in enumerate(st.chan_map):
        chan_of.setdefault((((cm >> 8) & 1) if st.opl3 else 0, cm & 0xFF),
                           []).append(fch)
    per: dict[int, list] = {}
    for w in vgm.writes:
        if w.chip == kind:
            per.setdefault(w.chip_id, []).append(w)
    out = []
    for cid, ws in per.items():
        image: dict[tuple[int, int], int] = {}
        keyed: dict[tuple[int, int], bool] = {}
        trig = 0
        i, n = 0, len(ws)
        while i < n:
            sample = ws[i].sample
            j = i + 1
            while j < n and ws[j].sample == sample:
                j += 1
            edges: list[tuple[int, int]] = []
            for w in ws[i:j]:
                port = ((w.reg >> 8) & 1) if st.opl3 else 0
                r = w.reg & 0xFF
                image[(port, r)] = w.val
                if 0xB0 <= r <= 0xB8:
                    on = bool(w.val & 0x20)
                    if on and not keyed.get((port, r), False):
                        for fch in chan_of.get((port, r - 0xB0), ()):
                            if not st.drums or fch < st.melodic:
                                edges.append((fch, -1))
                    keyed[(port, r)] = on
                elif r == 0xBD and not st.opl3:
                    bits = (w.val & 0x1F) if (w.val & 0x20) else 0
                    for k in range(5):
                        bit = 1 << (4 - k)
                        if (bits & bit) and not (trig & bit):
                            edges.append((st.melodic + k, bit))
                    trig = bits
            for fch, _bit in edges:
                out.append((cid, fch, sample, dict(image)))
            i = j
    return out


def opl_voice_addrs(kind: str, drums: bool, col: int, image) -> list[int]:
    """Operator addresses of the voice a column owns in one register image.

    Four when the sample's ``0x104`` pairs the column with its neighbour: such a
    voice takes two operators from each hardware channel of the pair.
    """
    st = OPLState(kind, drums)
    four = (st.opl3 and col < 12 and not (col & 1)
            and bool((image.get((1, 0x04), 0) >> (col // 2)) & 1))
    return [SLOT_ADDR[st.slots[row][col]] for row in range(4 if four else 2)
            if col < len(st.slots[row]) and st.slots[row][col] != OPL_N]


def pitch_runs(
    src_vgm,
    other_vgm,
    ch: int,
    *,
    threshold_st: float = 0.5,
    min_run_ms: float = 100.0,
) -> list[PitchRun]:
    """Stretches where the two pitch streams differ by more than `threshold_st`.

    The comparison walks every write time of the two streams in order and
    samples both states. A short mismatch inside a glide is ignored while a
    held wrong pitch is reported with its full span.  For YM2203 chips the
    driver keeps writing f-num during the gaps between notes, and the
    comparison therefore only counts samples where the source has a keyed operator. An
    OPL-family voice additionally trims its key, because a row-tick note and the
    sample the driver used do not coincide.
    """
    opnb = any(
        w.chip in ("ym2610", "ym2610_ssg") for w in src_vgm.writes
    )
    opna = any(
        w.chip in ("ym2608", "ym2608_ssg") for w in src_vgm.writes
    )
    opn = any(w.chip == "ym2203" for w in src_vgm.writes)
    opn2 = any(w.chip == "ym2612" for w in src_vgm.writes)
    opl = _opl_kind(src_vgm) is not None
    if opnb:
        stream = ym2610_pitch_stream
    elif opna:
        stream = ym2608_pitch_stream
    elif opn2:
        stream = ym2612_pitch_stream
    elif opn:
        stream = opn_pitch_stream
    elif opl:
        stream = opl_pitch_stream
    else:
        stream = kc_kf_stream
    a = stream(src_vgm, ch)
    b = stream(other_vgm, ch)
    # Loop finding cuts the source's post-loop tail from the module.  The
    # export's declared length is the content it really plays; comparing past
    # it would score the module's loop restart against the source's tail.
    limit: int | None = None
    if other_vgm.total_samples:
        limit = other_vgm.total_samples
        if src_vgm.total_samples:
            limit = min(limit, src_vgm.total_samples)
    times = {s for s, _v in a} | {s for s, _v in b}
    if limit is not None:
        times = {t for t in times if t <= limit}
    keyed_at = None
    if opnb or opna:
        intervals = (ym2610_keyed_intervals if opnb else ym2608_keyed_intervals)(
            src_vgm, ch)
        times |= {s for s, _e in intervals}
        times |= {e for _s, e in intervals}

        def keyed_at(t: int) -> bool:
            return any(s <= t < e for s, e in intervals)
    elif opn2:
        intervals = ym2612_keyed_intervals(src_vgm, ch)
        times |= {s for s, _e in intervals}
        times |= {e for _s, e in intervals}

        def keyed_at(t: int) -> bool:
            return any(s <= t < e for s, e in intervals)
    elif opn:
        intervals = opn_keyed_intervals(src_vgm, ch)
        times |= {s for s, _e in intervals}
        times |= {e for _s, e in intervals}

        def keyed_at(t: int) -> bool:
            return any(s <= t < e for s, e in intervals)
    elif opl:
        # The OPL keeps the last f-num in its register after a release, and a
        # rhythm column shares one register with another drum. Only the spans
        # where this voice itself held the key are compared. A module places a
        # note on a row tick while the driver wrote it at an exact sample, and
        # the two agree only a few milliseconds apart at each boundary: comparing
        # there would measure that drift as wrong pitch. Trim the ends, and a
        # note shorter than the trim drops out - grace notes carry no evidence.
        trim = int(0.035 * VGM_RATE)
        intervals = [(s + trim, e - trim) for s, e in opl_keyed_intervals(src_vgm, ch)
                     if e - s > 2 * trim]
        times |= {s for s, _e in intervals}
        times |= {e for _s, e in intervals}

        def keyed_at(t: int) -> bool:
            return any(s <= t < e for s, e in intervals)
    if limit is not None:
        times = {t for t in times if t <= limit}
    times = sorted(times)
    threshold = threshold_st * 64
    min_run = int(min_run_ms * 44.1)

    runs: list[PitchRun] = []
    cur: PitchRun | None = None
    ia = ib = 0
    for t in times:
        if keyed_at is not None and not keyed_at(t):
            if cur is not None:
                cur.t1 = t
                if cur.t1 - cur.t0 >= min_run:
                    runs.append(cur)
                cur = None
            continue
        while ia < len(a) and a[ia][0] <= t:
            ia += 1
        while ib < len(b) and b[ib][0] <= t:
            ib += 1
        if ia == 0 or ib == 0:
            continue
        delta = b[ib - 1][1] - a[ia - 1][1]
        if abs(delta) > threshold:
            if cur is None:
                cur = PitchRun(t, t, delta / 64.0)
            else:
                cur.t1 = t
                if abs(delta) > abs(cur.worst_st) * 64:
                    cur.worst_st = delta / 64.0
        elif cur is not None:
            # The state only moves at write times, and every write time is in
            # `times`. The first non-divergent sample is the moment the pitch
            # came back, and the run ends there.
            cur.t1 = t
            if cur.t1 - cur.t0 >= min_run:
                runs.append(cur)
            cur = None
    if cur is not None and cur.t1 - cur.t0 >= min_run:
        runs.append(cur)
    return runs


def register_writes(
    vgm,
    chip: str,
    ch: int,
    regs: list[int],
    *,
    t0: int = 0,
    t1: int | None = None,
    only_changes: bool = True,
) -> list[tuple[int, int, int]]:
    """(sample, register, value) rows for one chip channel.

    Registers below 0x20 are chip-wide (the LFO block) and are not offset by
    the channel; the rest are matched per channel.
    """
    last: dict[int, int] = {}
    out: list[tuple[int, int, int]] = []
    for w in vgm.writes:
        if w.chip != chip or w.sample < t0 or (t1 is not None and w.sample > t1):
            continue
        for r in regs:
            if (w.reg == r) if r < 0x20 else (w.reg == r + ch):
                if only_changes and last.get(r) == w.val:
                    break
                last[r] = w.val
                out.append((w.sample, r, w.val))
                break
    return out


def export_fur(fur: Path, out: Path | None = None) -> Path:
    fur = Path(fur)
    if out is None:
        out = fur.with_name(fur.stem + ".export.vgm")
    return export_vgm(fur, Path(out))


def compare_fur_to_source(
    src_vgm: Path,
    fur: Path,
    ch: int = 0,
    *,
    threshold_st: float = 0.5,
    min_run_ms: float = 100.0,
    export_to: Path | None = None,
) -> tuple[list[PitchRun], Path]:
    """Export the .fur and return the pitch divergences on one channel."""
    exported = export_fur(fur, export_to)
    src = load_vgm(src_vgm)
    other = load_vgm(exported)
    return pitch_runs(src, other, ch, threshold_st=threshold_st, min_run_ms=min_run_ms), exported
