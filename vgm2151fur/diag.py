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

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from vgm2151fur.analyze import kc_kf_units
from vgm2151fur.furnace import export_vgm
from vgm2151fur.vgm import load_vgm


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


def state_at(stream: list[tuple[int, int]], t: int) -> int | None:
    i = bisect_right([s for s, _v in stream], t) - 1
    return stream[i][1] if i >= 0 else None


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
    held wrong pitch is reported with its full span.
    """
    a = kc_kf_stream(src_vgm, ch)
    b = kc_kf_stream(other_vgm, ch)
    times = sorted({s for s, _v in a} | {s for s, _v in b})
    threshold = threshold_st * 64
    min_run = int(min_run_ms * 44.1)

    runs: list[PitchRun] = []
    cur: PitchRun | None = None
    ia = ib = 0
    for t in times:
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
