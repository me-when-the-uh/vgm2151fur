"""Render .fur files to WAV with the bundled Furnace console build.

    python fur2wav.py <file.fur|folder> [-o out.wav] [--loops N]

The fast way to hear what a conversion sounds like in Furnace. fur2vgm.py
proves what the VGM export contains instead. Both need the Furnace console
build; see the README for where it is expected.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vgm2151fur.furnace import FurnaceError, render_wav


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help=".fur file or a folder of them")
    ap.add_argument("-o", "--out", default=None, help="output .wav (single input only)")
    ap.add_argument("--loops", type=int, default=1, help="song loops to render")
    args = ap.parse_args()

    src = Path(args.input)
    files = [src] if src.is_file() else sorted(src.glob("*.fur"))
    if not files:
        print(f"ERROR: no .fur under {src}", file=sys.stderr)
        return 1
    rc = 0
    for fur in files:
        out = Path(args.out) if args.out else fur.with_suffix(".wav")
        try:
            render_wav(fur, out, loops=args.loops)
        except FurnaceError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            rc = 1
            continue
        print(f"{fur.name} -> {out}  ({out.stat().st_size} bytes)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
