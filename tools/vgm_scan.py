"""Census: which VGM/VGZ files use which sample chips (and at what clock).

    python vgm_scan.py [root ...] [--max-mb 2] [--files]

Walks the tree (default: the current folder, skipping output/, third_party/
and the like), reads the first --max-mb MB of every .vgm/.vgz, and groups
the hits per pack directory:

    segapcm  clock at header 0x38, extra-header clock id 0x04
    c140     clock at header 0xA8, type byte 0x96 (0 = System 2, 1 = System
             21), extra-header clock id 0x1C; 21390 counts as legacy C140
             even in pre-1.61 files (that is how the System 21 rips log it)
    c352     VGM 1.71 clock at header 0xDC (or extra-header id 0x27). Its
             own 32-voice chip. Furnace 0.6.8.3 does not play chip 0xD0.
    k053260  Konami K053260 at header 0xAC (VGM 1.61+, or extra-header id
             0x1D). Four voices; Furnace 0.6.8.3 plays chip 0xCC.
    ymz280b  Yamaha YMZ280B at header 0x68 (VGM 1.51+, or extra-header id
             0x0F); bit 30 marks a second chip (0xAD writes). Eight voices.
    vsu      Nintendo Virtual Boy VSU at header 0xC4 (VGM 1.71+); bit 30
             marks a second chip. Five wavetable voices plus noise.
    c219     the C140-family ASIC (type byte 2). The converter does not
             support it; never batch-convert these into 0xCE.
    k051649  Konami SCC at header 0x9C; bit 31 of that field selects the
             K052539 "SCC+" variant (listed as its own kind, scc_plus)
    ay8910   AY-3-8910/YM2149 at header 0x74 (VGM 1.51+), type byte 0x78
    ym2203   YM2203 at header 0x44; bit 30 marks a second chip (0xA5 writes)
    ym2612   YM2612 at header 0x2C; bit 30 marks a second chip (0xA2/A3 writes)
    opl      YM3526 (OPL) at header 0x54; writes use command $5B. Rhythm mode
             is the same $BD register as on a YM3812 and becomes Furnace's drums
             system; the chip differs only in having no waveform select, so its
             operators are sine and an instrument's wave byte does nothing
    opl2     YM3812 (OPL2) at header 0x50; bit 30 marks a second chip ($AA
             writes) and bit 31 that the pair is hard left/right. Commands $5A
             (bank A) and $AA (second chip). Rhythm mode is the $BD register: a
             log that latches it becomes the drums system in Furnace.
    opl3     YMF262 (OPL3) at header 0x5C; both registers banks go through one
             chip, commands $5E (port 0) and $5F (port 1), the bank living in
             bit 8 of the register byte. $104 in port 1 pairs channels for
             four-operator voices, so a channel count says nothing about it.

Detection is heuristic (header clocks plus the extra-header clock list).
Run it before batch convert requests so the pack list is complete, and
after adding packs. Reads .vgz through zlib; unreadable files are skipped
silently.
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
        "node_modules", "soundcheck"}

CENSUS_KINDS = {"segapcm", "c140", "c219", "c352", "k053260", "ymz280b",
                "vsu", "vsu_dual", "k051649",
                "scc_plus", "ay8910", "ym2203", "ym2612",
                # The OPL family is FM rather than a sample chip, but a census
                # without it hides most of the AdLib and sound-card logs.
                "opl", "opl2", "opl3"}


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

    groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
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
