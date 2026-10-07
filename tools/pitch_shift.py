"""Measure the pitch offset between two renders of the same music.

    python pitch_shift.py renderA.wav renderB.wav [--window MS] [--hop MS]

Both files are flattened to mono, windowed (default 400 ms), and each window
gets a 12-bin chroma profile. Every window is then rotated -6..+6 semitones
to best match the other file, and the table of median similarities is
printed. The peak's location is the answer: "B is s semitones ABOVE A" when
the best shift is +s. Importable: ``analyse(a, b)`` returns the same table.

Why this works: chroma discards timbre and mix. Wrong notes or missing
layers lower the whole curve, while a *pitch scale* error (wrong chip clock,
wrong sample rate) shows up as a clean peak away from 0. Beware it is
octave-blind: +7 and -5 look identical, which is fine for hunting scale
errors (the classic C140 bug fixed in 0.9.18 was 1.5x = +7.02 = -4.98) as
long as the full table is read, not just the best bin.

Typical use - fur_soundcheck.py wraps the whole source/.fur/export trio:
    python pitch_shift.py source.wav fur_render.wav      # Furnace playback
    python pitch_shift.py source.wav export_render.wav   # exported VGM
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path


def read_mono(path: Path) -> tuple[list[float], float]:
    """16-bit stereo WAV (any rate) -> mono floats at ~6 kHz, for 6 octaves."""
    buf = Path(path).read_bytes()
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
        raise SystemExit(f"{path}: no data chunk")
    step = max(1, round(rate / 6000.0))
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


def analyse(a_path, b_path, window_ms: float = 400.0,
            hop_ms: float | None = None, span: int = 6) -> dict:
    """Chroma-rotation scan of B against A; see module docstring."""
    a, rate = read_mono(Path(a_path))
    b, _ = read_mono(Path(b_path))
    n = min(len(a), len(b))
    win = max(64, int(window_ms / 1000.0 * rate))
    hop = win // 2 if hop_ms is None else max(1, int(hop_ms / 1000.0 * rate))

    def rms(x, off):
        return math.sqrt(sum(v * v for v in x[off:off + win]) / win)

    quiet = sorted(rms(a, off) for off in range(0, max(1, n - win), hop))
    gate = quiet[len(quiet) // 2] * 0.5 if quiet else 0.0

    curves = {s: [] for s in range(-span, span + 1)}
    for off in range(0, n - win, hop):
        if rms(a, off) < gate:
            continue
        ca, cb = chroma(a[off:off + win], rate), chroma(b[off:off + win], rate)
        nb = math.sqrt(sum(v * v for v in cb))
        if not nb:
            continue
        for s in curves:
            rot = [ca[(i - s) % 12] for i in range(12)]
            nn = math.sqrt(sum(v * v for v in rot)) or 1.0
            curves[s].append(sum(x * y for x, y in zip(rot, cb)) / (nn * nb))
    med = {s: statistics.median(v) for s, v in curves.items() if v}
    if not med:
        raise SystemExit("no usable windows (track too short or silent)")
    best = max(med, key=med.get)
    return {
        "windows": len(curves[0]),
        "gate": gate,
        "med": med,
        "best": best,
        "best_score": med[best],
        "at_zero": med.get(0, 0.0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a", help="reference render (WAV)")
    ap.add_argument("b", help="render under test (WAV)")
    ap.add_argument("--window", type=float, default=400.0, metavar="MS")
    ap.add_argument("--hop", type=float, default=None, metavar="MS")
    ap.add_argument("--span", type=int, default=6, help="half-range of shifts tried")
    args = ap.parse_args()
    r = analyse(args.a, args.b, args.window, args.hop, args.span)
    print(f"{r['windows']} windows, gate {r['gate']:.4f}")
    for s in sorted(r["med"]):
        print("  shift %+d: %.3f" % (s, r["med"][s]))
    print("best shift %+d semitones (median %.3f; at 0: %.3f) - "
          "B is that many semitones above A" % (r["best"], r["best_score"], r["at_zero"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
