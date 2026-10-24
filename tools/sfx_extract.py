"""Turn stop-first MAME recordings into per-code SFX WAVs + an index.

The recipe this expects: for each sound code, one ``record`` run that pokes
the stop code at the end of boot, waits, and then pokes the code (see
docs/sfx-codes.md). The poke lands at a fixed time, so each recording holds
[silence][the code's sound][silence]. This tool finds the sound, trims it
(poke to first quiet gap, never longer than --cap), mixes to mono, matches
the level to the existing sample set and writes code_XX.wav.

    python tools/sfx_extract.py --wav-dir output/gradius2/wav \
        --pattern "*_seq_*.wav" --extra 0x40=gradius2_code_40_seq.wav \
        --out output/other/gradius2/sfx --start 11.5 --cap 5

--normalize-dir peak-matches an existing folder in place (used for the
uPD7759 speech set so both sets sit at the same level).
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Mono float array (channels averaged) and the sample rate."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: only 16-bit PCM supported")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    clipped = np.clip(np.rint(samples), -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(clipped.tobytes())


def trim(x: np.ndarray, rate: int, start_s: float, cap_s: float,
         threshold_db: float, gap_ms: float) -> dict | None:
    """Find the first sound after `start_s` and cut it at the first real gap."""
    lo = int(start_s * rate)
    seg = x[lo:]
    if not len(seg):
        return None
    peak = float(np.max(np.abs(seg)))
    if peak <= 0:
        return None
    thr = max(10.0 * 32768 / 1000, peak * 10 ** (threshold_db / 20))
    loud = np.abs(seg) > thr
    idx = np.nonzero(loud)[0]
    if not len(idx):
        return None
    onset = int(idx[0])
    gap = int(gap_ms / 1000 * rate)
    # a gap is `gap_ms` of samples whose running peak stays under the threshold
    win = np.convolve(loud.astype(np.int32), np.ones(gap, dtype=np.int32), "full")
    quiet_after = np.nonzero(win[onset:onset + int(cap_s * rate)] == 0)[0]
    cap = int(cap_s * rate)
    if len(quiet_after):
        end = onset + int(quiet_after[0])
        truncated = False
    else:
        end = min(len(seg) - 1, onset + cap)
        truncated = True
    pre = int(0.005 * rate)
    body = seg[max(0, onset - pre):min(len(seg), end + int(0.060 * rate))]
    return {
        "samples": body,
        "peak_raw": float(np.max(np.abs(body))),
        "seconds": round(len(body) / rate, 4),
        "truncated": truncated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav-dir", required=True, help="folder with the stop-first recordings")
    ap.add_argument("--pattern", default="*_seq_*.wav",
                    help="glob for recordings; the code comes from '_seq_XX.wav' (default %(default)s)")
    ap.add_argument("--extra", action="append", default=[], metavar="CODE=FILE",
                    help="extra recording for one code, e.g. 0x40=gradius2_code_40_seq.wav")
    ap.add_argument("--skip", action="append", default=[], metavar="GLOB",
                    help="recording names to ignore, e.g. --skip 'gradius3_seq_5*.wav' "
                         "for a song range you are not delivering here")
    ap.add_argument("--out", required=True, help="output folder for code_XX.wav")
    ap.add_argument("--start", type=float, default=11.5,
                    help="seconds; the code poke happens just after this (default %(default)s)")
    ap.add_argument("--cap", type=float, default=5.0,
                    help="hard length limit per sample in seconds (default %(default)s)")
    ap.add_argument("--target-peak", type=int, default=32700,
                    help="peak to scale each sample to; 0 disables (default %(default)s)")
    ap.add_argument("--threshold-db", type=float, default=-54.0,
                    help="level under which a sample counts as silence (default %(default)s)")
    ap.add_argument("--gap-ms", type=float, default=150.0,
                    help="silence length that ends a sample (default %(default)s)")
    ap.add_argument("--normalize-dir", action="append", default=[], metavar="DIR",
                    help="peak-match every WAV in DIR in place and report it")
    ap.add_argument("--index", default="sfx_index.json", help="index file name (default %(default)s)")
    ap.add_argument("--notes", default="", help="free text stored in the index")
    args = ap.parse_args()

    wav_dir = Path(args.wav_dir)
    jobs: list[tuple[int, Path]] = []
    for path in sorted(wav_dir.glob(args.pattern)):
        if any(path.match(s) for s in args.skip):
            print(f"skip {path.name}: --skip")
            continue
        stem = path.stem
        if "_seq_" not in stem:
            print(f"skip {path.name}: no '_seq_' in the name")
            continue
        try:
            jobs.append((int(stem.rsplit("_seq_", 1)[1], 16), path))
        except ValueError:
            print(f"skip {path.name}: tail of the name is not hex")
    for item in args.extra:
        code, _, name = item.partition("=")
        jobs.append((int(code, 0), wav_dir / name))
    jobs.sort()

    out = Path(args.out)
    entries = []
    silent = []
    print(f"{'code':5s} {'file':22s} {'secs':>6s} {'peak':>7s} {'gain':>6s}  note")
    for code, path in jobs:
        if not path.exists():
            silent.append(f"0x{code:02X}")
            print(f"0x{code:02X}  {path.name:22s}  (missing recording)")
            continue
        x, rate = read_wav(path)
        cut = trim(x, rate, args.start, args.cap, args.threshold_db, args.gap_ms)
        if cut is None:
            silent.append(f"0x{code:02X}")
            print(f"0x{code:02X}  {path.name:22s}  (no sound above {args.threshold_db:.0f} dBFS)")
            continue
        body = cut["samples"]
        gain = 1.0
        if args.target_peak:
            gain = args.target_peak / max(cut["peak_raw"], 1.0)
            body = body * gain
        out_file = out / f"code_{code:02X}.wav"
        write_wav(out_file, body, rate)
        note = "cut at a gap" if not cut["truncated"] else f"capped at {args.cap:g}s"
        entries.append({
            "code": f"0x{code:02X}",
            "file": out_file.name,
            "seconds": cut["seconds"],
            "rate": rate,
            "peak_raw": int(cut["peak_raw"]),
            "gain": round(gain, 4),
            "note": note,
            "recording": path.name,
        })
        print(f"0x{code:02X}  {path.name:22s}  {cut['seconds']:6.2f} {int(cut['peak_raw']):7d} "
              f"{gain:6.2f}  {note}")

    normalized = {}
    for folder in args.normalize_dir:
        folder_path = Path(folder)
        peaks = []
        for path in sorted(folder_path.glob("*.wav")):
            x, rate = read_wav(path)
            peak = float(np.max(np.abs(x)))
            if not peak:
                continue
            gain = args.target_peak / peak
            write_wav(path, x * gain, rate)
            peaks.append({"file": path.name, "peak_raw": int(peak), "gain": round(gain, 4)})
        normalized[str(folder_path)] = peaks
        if peaks:
            print(f"normalized {len(peaks)} file(s) in {folder_path} to peak {args.target_peak}")

    index = {
        "source": args.notes or "stop-first MAME recordings, one per sound code",
        "method": {
            "poke_time": args.start,
            "cap_seconds": args.cap,
            "target_peak": args.target_peak,
            "silence_threshold_db": args.threshold_db,
            "gap_ms": args.gap_ms,
        },
        "codes": entries,
        "silent_codes": silent,
        "normalized_folders": normalized,
    }
    (out / args.index).write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {len(entries)} sample(s) to {out}, index {args.index}, silent {len(silent)}")
    if silent:
        print("silent:", " ".join(silent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
