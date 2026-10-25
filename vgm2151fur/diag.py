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
from vgm2151fur.vgm import load_vgm
from vgm2151fur.ym2203 import opn_pitch_units
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
    out: list[tuple[int, int]] = []
    lo = hi = None
    min_period = ay_min_period(vgm.ym2203_clock or 3579545, 16)
    for w in vgm.writes:
        if w.chip != "ym2203_ssg" or w.chip_id != cid:
            continue
        if w.reg == ssg * 2:
            lo = w.val
        elif w.reg == ssg * 2 + 1:
            hi = w.val
        else:
            continue
        if lo is not None and hi is not None:
            period = (lo & 0xFF) | ((hi & 0x0F) << 8)
            if period >= min_period:
                out.append((w.sample, round(opn_ssg_units(period))))
    return out


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
        ssg = voice - ssg_at
        mixer = 0
        vol = [0, 0, 0]
        lo = hi = 0
        min_period = ay_min_period(vgm.ym2203_clock or 3579545, 16)
        for w in vgm.writes:
            if w.chip != "ym2203_ssg" or w.chip_id != cid:
                continue
            if w.reg == 7:
                mixer = w.val
            elif w.reg in (8, 9, 10):
                vol[w.reg - 8] = w.val
            elif w.reg == ssg * 2:
                lo = w.val
            elif w.reg == ssg * 2 + 1:
                hi = w.val
            else:
                continue
            level = vol[ssg]
            period = (lo & 0xFF) | ((hi & 0x0F) << 8)
            # Noise-only/noise-mixed percussion has no meaningful tone period;
            # comparing the stale period register would report false pitch
            # changes, so only tone-only gates count. Periods below the
            # module's representable range are the driver's silence gate.
            now = (not (mixer & (1 << ssg))
                   and (mixer & (1 << (ssg + 3)))
                   and ((level & 0x10) or (level & 0x0F))
                   and period >= min_period)
            if now and not on:
                start = w.sample
                on = True
            elif not now and on:
                out.append((start, w.sample))
                on = False
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


def _ym2612_chip_ids(vgm) -> list[int]:
    return sorted({w.chip_id for w in vgm.writes if w.chip == "ym2612"})


def _ym2612_ext(vgm) -> bool:
    """True when the source ever selects channel-3 special/CSM mode."""
    return any(
        w.chip == "ym2612" and (w.reg & 0xFF) == 0x27 and (w.val & 0xC0)
        for w in vgm.writes
    )


def _ym2612_channel_map(vgm, ch: int) -> tuple[int, str, int] | None:
    """(chip id, kind, voice/column) for one module channel.

    `kind` is "fm" for a normal channel, "ext" for a channel-3 operator
    column.  Returns None when the module layout has no such channel.
    """
    ids = _ym2612_chip_ids(vgm)
    chunk = 9 if _ym2612_ext(vgm) else 6
    chip, voice = divmod(ch, chunk)
    if chip >= len(ids):
        return None
    cid = ids[chip]
    if voice < 2:
        return cid, "fm", voice
    if chunk == 9:
        if voice < 6:
            return cid, "ext", voice - 2
        return cid, "fm", voice - 3
    return cid, "fm", voice


def ym2612_pitch_stream(vgm, ch: int) -> list[tuple[int, int]]:
    """(sample, pitch units) for one YM2612 module channel.

    The normal layout follows the channel's latched f-num/block.  The Ext
    layout follows the four channel-3 operator pairs for its columns.
    """
    mapped = _ym2612_channel_map(vgm, ch)
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
        if w.chip != "ym2612" or w.chip_id != cid:
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


def ym2612_keyed_intervals(vgm, ch: int) -> list[tuple[int, int]]:
    """Samples where one YM2612 module channel is keyed.

    In the Ext layout channel-3 columns follow the 0x28 mask bits; the other
    channels use their own key state.  DAC playback on channel 6 is not FM,
    so a keyed FM6 under DAC enable is treated as silent.
    """
    mapped = _ym2612_channel_map(vgm, ch)
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
    dac = False
    for w in vgm.writes:
        if w.chip != "ym2612" or w.chip_id != cid:
            continue
        r = w.reg & 0xFF
        if not (w.reg & 0x100) and r == 0x2B:
            dac = bool(w.val & 0x80)
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
        if target_ch == 5 and dac:
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
    driver keeps writing f-num during the gaps between notes, so the
    comparison only counts samples where the source has a keyed operator.
    """
    opn = any(w.chip == "ym2203" for w in src_vgm.writes)
    opn2 = any(w.chip == "ym2612" for w in src_vgm.writes)
    if opn2:
        stream = ym2612_pitch_stream
    elif opn:
        stream = opn_pitch_stream
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
    if opn2:
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
            # came back, so the run ends here.
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
