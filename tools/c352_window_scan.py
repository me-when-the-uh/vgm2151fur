"""Windowed spectral scan: source rip render vs .fur render, pass 1 only.

    python tools/c352_window_scan.py <source.vgz|vgm> <module.fur>

Renders both sides (cached next to the .fur in ./soundcheck/) and compares
0.5 s windows over the first pass only. The reference player renders the
source for ~2 passes and Furnace renders the module for ~1.9 with loop
points that can differ by a row. Windows past pass 1 then show false
"missing content" spikes at the loop boundary. Do not chase those.

Band deltas are dB (fur minus source) per octave band, 20 Hz to 22 kHz. A
healthy pair stays under the threshold in every window and has a roughly
flat level profile.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "vgm2151fur"))
sys.path.insert(0, str(BASE / "vgm2151fur" / "tools"))

import renderer
from vgm2151fur.vgm import load_vgm
from vgm2151fur.furnace import render_wav

BANDS = [(20, 60), (60, 120), (120, 250), (250, 500), (500, 1000), (1000, 2000),
         (2000, 4000), (4000, 8000), (8000, 16000), (16000, 22050)]


def load_wav(path: Path) -> np.ndarray:
    data = path.read_bytes()
    pos = 12
    while pos + 8 <= len(data):
        ident = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if ident == b"data":
            body = data[pos + 8:pos + 8 + size]
            break
        pos += 8 + size + (size & 1)
    a = np.frombuffer(body, dtype="<i2").astype(np.float64)
    return a.reshape(-1, 2).mean(axis=1) / 32768.0


def band_profile(x: np.ndarray) -> np.ndarray:
    f = np.fft.rfft(x * np.hanning(len(x)))
    p = np.abs(f) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / 44100)
    return np.array([p[(fr >= lo) & (fr < hi)].sum() for lo, hi in BANDS])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("fur")
    ap.add_argument("--win", type=float, default=0.5, help="window seconds (default 0.5)")
    ap.add_argument("--worst", type=int, default=12, help="windows to print (default 12)")
    ap.add_argument("--threshold", type=float, default=6.0,
                    help="flag windows whose RMS of band deltas exceeds this (default 6 dB)")
    ap.add_argument("--render", action="store_true", help="force fresh renders")
    args = ap.parse_args()

    src = Path(args.source)
    fur = Path(args.fur)
    vgm = load_vgm(src)
    outdir = fur.parent / "soundcheck"
    outdir.mkdir(parents=True, exist_ok=True)
    # same cache names as fur_soundcheck.py so renders are shared
    stem = fur.stem.replace(" ", "_")
    srcwav = outdir / f"{stem}.src.wav"
    furwav = outdir / f"{stem}.fur.wav"
    if args.render or not srcwav.is_file():
        renderer.render(src, srcwav)
    if args.render or not furwav.is_file():
        render_wav(fur, furwav)

    a = load_wav(srcwav)
    b = load_wav(furwav)
    win = int(args.win * 44100)
    limit = min(len(a), len(b), vgm.total_samples) - win
    if limit <= 0:
        print("renders too short to compare")
        return 1
    rows = []
    for start in range(0, limit, win):
        ls = 10 * np.log10(band_profile(a[start:start + win]) + 1e-12)
        lf = 10 * np.log10(band_profile(b[start:start + win]) + 1e-12)
        d = float(np.sqrt(np.mean((ls - lf) ** 2)))
        rows.append((start / 44100, d, lf - ls))
    over = [r for r in rows if r[1] > args.threshold]
    print(f"pass-1 windows: {len(rows)} at {args.win:.2f}s; over {args.threshold:.1f} dB: {len(over)}")
    for t, d, diff in sorted(rows, key=lambda r: -r[1])[: args.worst]:
        print(f"  {t:7.2f}s d={d:5.1f}: {' '.join(f'{v:+.0f}' for v in diff)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
