"""Export .fur files back to VGM with the bundled Furnace console build.

    python fur2vgm.py <file.fur|folder> [-o out.vgm]

The exported VGM is the ground truth for what a .fur means to a player: load
it with ``vgm2151fur.vgm.load_vgm`` next to the source rip and compare the
register streams (see vgmdiff.py and the compare command).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vgm2151fur.furnace import FurnaceError, export_vgm


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help=".fur file or a folder of them")
    ap.add_argument("-o", "--out", default=None, help="output .vgm (single input only)")
    args = ap.parse_args()

    src = Path(args.input)
    files = [src] if src.is_file() else sorted(src.glob("*.fur"))
    if not files:
        print(f"ERROR: no .fur under {src}", file=sys.stderr)
        return 1
    rc = 0
    for fur in files:
        out = Path(args.out) if args.out else fur.with_suffix(".vgm")
        try:
            export_vgm(fur, out)
        except FurnaceError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            rc = 1
            continue
        print(f"{fur.name} -> {out.name} ({out.stat().st_size} bytes)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
