"""Register-level diff: a source rip against a Furnace C352 export.

    python tools/c352_regdiff.py <source.vgz|vgm> <export.vgm>

Both files are read through the converter's own C352 model: the export gets
the same reading the converter applies to the source. The .fur repacks
sample bodies to fresh addresses and remaps voices, so hits are keyed by
the body they play (content hash), then paired by time and compared for
frequency, volume, flags and count.

A healthy pair has 0 unmatched hits and tiny frequency deltas. Both
arguments are required.
"""

import hashlib
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from vgm2151fur.vgm import load_vgm
from vgm2151fur.c352 import collect_c352, hit_played_freq

FLAG_MASK = 0x0001 | 0x0002 | 0x0008 | 0x0020 | 0x0040  # rev, loop, mulaw, link, ldir


def eff_vol(h):
    """Volume the hit starts at; a step at the same sample wins (write order)."""
    if h.vol_pts and h.vol_pts[0][0] == h.sample_time:
        return h.vol_pts[0][1], h.vol_pts[0][2]
    return h.vol_l, h.vol_r


def analyse(path: Path, label: str):
    vgm = load_vgm(path)
    print(f"{label}: ver {vgm.version:#x} clock {vgm.c352_clock} div {vgm.c352_divider} "
          f"mute_rear {vgm.c352_mute_rear}")
    _, samples, offs, warnings = collect_c352(vgm)
    if warnings:
        print(f"{label} warnings: {warnings[:4]}")
    by_id = {}
    hits = defaultdict(list)
    for s in samples:
        sid = hashlib.sha1(s.data).hexdigest()[:12]
        by_id[sid] = s
        hits[sid] += s.hits
    print(f"{label}: samples={len(samples)} hits={sum(len(v) for v in hits.values())} offs={len(offs)}")
    return hits, by_id


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: c352_regdiff.py <source.vgz|vgm> <export.vgm>")
        return 2
    src, exp = Path(sys.argv[1]), Path(sys.argv[2])
    sh, sby = analyse(src, "src")
    eh, eby = analyse(exp, "exp")

    shared = sorted(set(sh) & set(eh))
    print("== bodies ==")
    print(f"shared bodies: {len(shared)}/{len(sby)} src, {len(eby)} exp")
    print(f"src-only bodies: {len(set(sh) - set(eh))}  exp-only: {len(set(eh) - set(sh))}")

    print("== hits by body ==")
    freq_ppm, vol_mismatch, flag_mismatch, unmatched = [], [], [], 0
    for sid in shared:
        a, b = sh[sid], eh[sid]
        if len(a) != len(b):
            gaps = [y.sample_time - x.sample_time
                    for x, y in zip(sorted(a, key=lambda h: h.sample_time),
                                    sorted(a, key=lambda h: h.sample_time)[1:])]
            close = [g for g in gaps if g < 400]
            print(f"  {sid} src={len(a)} exp={len(b)}  close-pairs(<400smp)={len(close)} "
                  f"min-gap={min(gaps) if gaps else '-'}")
        tb = sorted(b, key=lambda h: h.sample_time)
        used = [False] * len(tb)
        for x in sorted(a, key=lambda h: h.sample_time):
            best = None
            for i, y in enumerate(tb):
                if used[i]:
                    continue
                d = abs(y.sample_time - x.sample_time)
                if best is None or d < best[0]:
                    best = (d, i)
            if best is None or best[0] > 600:
                unmatched += 1
                continue
            _, i = best
            used[i] = True
            y = tb[i]
            xf, yf = hit_played_freq(x), hit_played_freq(y)
            if xf:
                freq_ppm.append((yf - xf) / xf * 1e6)
            if eff_vol(x) != eff_vol(y):
                vol_mismatch.append((sid, x, y))
            if (x.flags & FLAG_MASK) != (y.flags & FLAG_MASK):
                flag_mismatch.append((sid, x, y))

    if freq_ppm:
        freq_ppm.sort()
        print(f"freq delta ppm: max |d|={max(abs(v) for v in freq_ppm):.1f} "
              f"median={freq_ppm[len(freq_ppm) // 2]:+.1f}")
    deltas = sorted(max(abs(x.vol_l - y.vol_l), abs(x.vol_r - y.vol_r)) for _, x, y in vol_mismatch)
    if deltas:
        print(f"vol |delta|: median={deltas[len(deltas) // 2]} max={deltas[-1]} "
              f"count>8={sum(1 for d in deltas if d > 8)} of {len(deltas)}")
    bits = Counter()
    for _, x, y in flag_mismatch:
        bits[(x.flags & FLAG_MASK) ^ (y.flags & FLAG_MASK)] += 1
    print("flag diff bit patterns:", {f"{k:#x}": v for k, v in bits.most_common(8)})
    print(f"vol mismatches: {len(vol_mismatch)}  flag mismatches: {len(flag_mismatch)}  "
          f"unmatched hits: {unmatched}")
    for sid, x, y in vol_mismatch[:5]:
        print(f"  VOL {sid} t={x.sample_time} src={x.vol_l}/{x.vol_r} exp={y.vol_l}/{y.vol_r} "
              f"freq {x.freq:#x}->{y.freq:#x}")
    for sid in sorted(set(eh) - set(sh)):
        s = eby[sid]
        print(f"  exp-only body len={len(s.data)} depth={s.depth} loop={s.loop_start}/{s.loop_end}")
    for sid in sorted(set(sh) - set(eh)):
        s = sby[sid]
        print(f"  src-only body len={len(s.data)} depth={s.depth} loop={s.loop_start}/{s.loop_end}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
