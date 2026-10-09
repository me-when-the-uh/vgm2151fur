"""Batch-convert VGM packs and report each module's grid.

    python tools/reconvert_c352.py PACK_DIR... [-o OUT]

Each PACK_DIR is a folder of .vgz tracks. Tracks convert into
OUT/<pack-slug>/fur (default OUT: output/ next to the repo root). Prints one
line per track (speed, hz, orders) plus any warning the conversion logged.
A "truncated to 256 orders" message means the track needs a later manual
`speed` pin or the VGM needs splitting.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from vgm2151fur.furio import load_fur_bytes, parse_fur


def slug(name: str) -> str:
    """Folder name as an output slug: lowercase, non-alphanumerics to dashes."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "pack"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packs", nargs="+", help="folders of .vgz tracks")
    ap.add_argument("-o", "--out", default=None, help="output root (default: output/)")
    args = ap.parse_args()
    root = Path(args.out) if args.out else REPO / "output"

    failed: list[str] = []
    ok = 0
    for pack in (Path(p) for p in args.packs):
        outdir = root / slug(pack.name) / "fur"
        outdir.mkdir(parents=True, exist_ok=True)
        for vgz in sorted(pack.glob("*.vgz")):
            proc = subprocess.run(
                [sys.executable, "-m", "vgm2151fur", "convert", str(vgz), "-o", str(outdir)],
                cwd=str(REPO), capture_output=True, text=True,
            )
            fur = outdir / f"{vgz.stem}.fur"
            if proc.returncode != 0 or not fur.is_file():
                failed.append(vgz.stem)
                print(f"FAIL {vgz.stem}: rc={proc.returncode}")
                print((proc.stdout or "")[-400:])
                print((proc.stderr or "")[-400:])
                continue
            ok += 1
            mod = parse_fur(load_fur_bytes(fur))
            note = ""
            for line in (proc.stdout or "").splitlines():
                if "warn" in line.lower() or "link" in line.lower():
                    note = " | " + line.strip()[:120]
            print(f"ok   {vgz.stem[:44]:44s} speed={mod.speed} hz={mod.hz:7.2f} "
                  f"n_ord={mod.n_ord:3d}{note}", flush=True)
    print(f"done: {ok} ok, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
