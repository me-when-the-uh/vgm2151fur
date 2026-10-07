"""Census: which VGM/VGZ files use which sample chips (and at what clock).

    python vgm_scan.py [root ...] [--max-mb 2] [--files]

Walks the tree (default: this repo, skipping output/, third_party/ and the
like), reads the first --max-mb MB of every .vgm/.vgz, and groups the hits
per pack directory:

    segapcm  clock at header 0x38, extra-header clock id 0x04
    c140     clock at header 0xA8, type byte 0x96 (0 = System 2, 1 = System
             21), extra-header clock id 0x1C; 21390 counts as legacy C140
             even in pre-1.61 files (that is how the System 21 rips log it)
    c352     VGM 1.71 clock at header 0xDC (or extra-header id 0x27). Its
             own 32-voice chip. Furnace 0.6.8.3 does not play chip 0xD0.
    c219     the C140-family ASIC (type byte 2). The converter does not
             support it; never batch-convert these into 0xCE.

Detection is heuristic (header clocks plus the extra-header clock list) but
reproduced this collection's 19 chip packs exactly (11 SegaPCM, 6 C140,
2 C219-only). Run it before "convert every X rip" requests so the pack list
is complete, and after adding packs. Reads .vgz through zlib; unreadable
files are skipped silently.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vgm2151fur.vgm import chips_in_head, head_bytes

SKIP = {"output", "third_party", ".git", "__pycache__", ".commandcode",
        "vgmtools", "node_modules", "soundcheck"}

CENSUS_KINDS = {"segapcm", "c140", "c219", "c352"}


def detect(p: Path, want: int) -> set[str] | None:
    kinds = chips_in_head(head_bytes(p, want)) & CENSUS_KINDS
    return kinds or None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", help="directories to scan (default: current folder)")
    ap.add_argument("--max-mb", type=int, default=2, help="bytes read per file (default 2 MB)")
    ap.add_argument("--files", action="store_true", help="list matching file names")
    args = ap.parse_args()
    roots = [Path(r) for r in args.roots] if args.roots else [Path.cwd()]
    want = args.max_mb * 1024 * 1024

    groups: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"segapcm": [], "c140": [], "c219": [], "c352": []})
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
            for fn in sorted(filenames):
                if not fn.lower().endswith((".vgm", ".vgz")):
                    continue
                p = Path(dirpath) / fn
                kinds = detect(p, want)
                if not kinds:
                    continue
                base = Path.cwd()
                resolved = p.resolve()
                rel = resolved.relative_to(base) if resolved.is_relative_to(base) else p
                parts = rel.parts
                key = ("/".join(parts[:3])
                       if parts and parts[0].lower().startswith("vgm") and len(parts) >= 4
                       else str(rel.parent))
                for k in kinds:
                    groups[key][k].append(fn)

    hits = 0
    for key in sorted(groups):
        g = groups[key]
        counts = {k: len(v) for k, v in g.items() if v}
        if not counts:
            continue
        hits += 1
        print("%-64s %s" % (key, "  ".join(f"{k} {c}" for k, c in sorted(counts.items()))))
        if args.files:
            for k in sorted(counts):
                for fn in g[k]:
                    print("      %-8s %s" % (k, fn))
    print(f"{hits} directories with chip hits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
