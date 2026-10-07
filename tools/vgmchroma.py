"""Compare the *rendered audio* of a source rip and a Furnace export.

    python vgmchroma.py <source.vgm|vgz> <export.vgm> [--from SEC] [--to SEC]
                        [--window MS] [--top N] [--wavdir DIR]

`vgmdiff.py` compares KC/KF registers, which cannot see LFO vibrato (PMS/AMS),
macros, or anything a driver does with the sample chips.  This tool renders
both files with the *same* renderer (vgm2wav-mute, so the gains match) and compares
a 12-bin chroma profile per window.  The cosine similarity per window says how
much of the sounding pitch content is shared:

    0.97+  healthy: the window sounds like the same music
    0.9    audible differences (a wrong note head, missing pitch motion)
    <0.8   something structural: dropped notes, wrong octave, missing channel

It is a coarse instrument by design (a mix of 8 channels has no single pitch),
so use it to *find* windows, then `vgmdiff.py` or a WAV render to explain them.

The two renders land in --wavdir (default: next to the export) unless they are
already there.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import renderer

NOTES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
DECIMATE = 5  # 44100 -> 8820 Hz, enough for 4 octaves of chroma


def read_mono(path: Path) -> tuple[list[float], float]:
    """16-bit stereo WAV -> mono float list at 44100/DECIMATE Hz."""
    buf = path.read_bytes()
    pos, data, rate = 12, None, 44100
    while pos + 8 <= len(buf):
        ident, size = buf[pos:pos + 4], int.from_bytes(buf[pos + 4:pos + 8], "little")
        body = buf[pos + 8:pos + 8 + size]
        if ident == b"fmt ":
            rate = int.from_bytes(body[4:8], "little")
        elif ident == b"data":
            data = body
            break
        pos += 8 + size + (size & 1)
    if data is None:
        raise SystemExit(f"{path.name}: no data chunk")
    step = max(1, round(rate / 8820.0))
    out = []
    for i in range(0, len(data) // 4 - step, step):
        a = int.from_bytes(data[4 * i:4 * i + 2], "little", signed=True)
        c = int.from_bytes(data[4 * i + 2:4 * i + 4], "little", signed=True)
        out.append((a + c) * 0.5 / 32768.0)
    return out, rate / step


def goertzel(x: list[float], f: float, rate: float) -> float:
    w = 2.0 * math.pi * f / rate
    c = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + c * s1 - s2
        s2, s1 = s1, s0
    return max(0.0, s1 * s1 + s2 * s2 - c * s1 * s2)


def chroma(x: list[float], rate: float) -> list[float]:
    prof = [0.0] * 12
    for octv in range(1, 7):
        for i in range(12):
            midi = (octv + 1) * 12 + i
            f = 440.0 * 2 ** ((midi - 69) / 12.0)
            if f > rate * 0.4:
                continue
            prof[i] += goertzel(x, f, rate)
    total = sum(prof) or 1.0
    return [p / total for p in prof]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("export")
    ap.add_argument("--from", dest="t0", type=float, default=0.0, metavar="SEC")
    ap.add_argument("--to", dest="t1", type=float, default=1e9, metavar="SEC")
    ap.add_argument("--window", type=float, default=100.0, metavar="MS")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--wavdir", metavar="DIR", help="where to write the two renders")
    ap.add_argument("--mute", action="append", default=[], metavar="SPEC",
                    help='vgm2wav-mute muting spec, e.g. "K007232#0.Disabled=True"; '
                         "applies to both renders (use it to isolate the FM side)")
    args = ap.parse_args()

    src, exp = Path(args.source), Path(args.export)
    wavdir = Path(args.wavdir) if args.wavdir else exp.parent
    src_wav = wavdir / (src.stem + ".src.wav")
    exp_wav = wavdir / (exp.stem + ".exp.wav")
    try:
        renderer.render(src, src_wav, args.mute)
        renderer.render(exp, exp_wav, args.mute)
    except renderer.RendererError as exc:
        raise SystemExit(str(exc))
    a, rate = read_mono(src_wav)
    b, _ = read_mono(exp_wav)
    n = min(len(a), len(b))
    win = max(64, int(args.window / 1000.0 * rate))

    sims = []
    for off in range(0, n - win, win):
        t = off / rate
        if t < args.t0 or t > args.t1:
            continue
        ca = chroma(a[off:off + win], rate)
        cb = chroma(b[off:off + win], rate)
        na = math.sqrt(sum(v * v for v in ca))
        nb = math.sqrt(sum(v * v for v in cb))
        sims.append((t, sum(x * y for x, y in zip(ca, cb)) / (na * nb) if na and nb else 0.0))
    if not sims:
        print("no windows in range")
        return 1
    vals = sorted(s for _t, s in sims)
    print(f"{len(vals)} windows of {args.window:.0f} ms, "
          f"chroma cosine median {vals[len(vals) // 2]:.3f}, "
          f"p10 {vals[int(len(vals) * .1)]:.3f}, min {vals[0]:.3f}, "
          f"{100.0 * sum(1 for v in vals if v < 0.9) / len(vals):.0f}% below 0.9")
    for t, s in sorted(sims, key=lambda r: r[1])[:args.top]:
        print(f"   {t:7.2f}s  cos {s:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
