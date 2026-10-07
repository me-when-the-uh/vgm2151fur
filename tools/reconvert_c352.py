"""Reconvert the C352 set: every C352 pack plus the cross-pack singles.

    python tools/reconvert_c352.py

Prints one line per track (speed, hz, orders). Watch for:
- a warning line about link targets outside the dump (those notes play the
  intro once; counts differ per track),
- n_ord growth vs the figures in docs/testing-pipeline.md,
- any "truncated to 256 orders" message (raise --speed or split the VGM).
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
FUR_DIR = BASE / "vgm2151fur"
sys.path.insert(0, str(FUR_DIR))

from vgm2151fur.furio import load_fur_bytes, parse_fur

PACKS: list[tuple[str, str]] = [
    ("tekken-tag", r"VGM_collection\new\Tekken_Tag_Tournament_(Namco_System_12)"),
    ("tekken", r"VGM_collection\new\Tekken_(Namco_System_11)"),
    ("tekken-2", r"VGM_collection\new\Tekken_2_(Namco_System_11)"),
    ("tekken-3", r"VGM_collection\new\Tekken_3_(Namco_System_12)"),
    ("soulcalibur", r"VGM_collection\new\SoulCalibur_(Namco_System_12)"),
    ("ace-driver", r"VGM_collection\new\Ace_Driver_-_Victory_Lap_(Namco_System_22)"),
    ("cyber-commando", r"VGM_collection\newest\Cyber_Commando_(Namco_System_22)"),
    ("outfoxies", r"VGM_collection\newest\The_Outfoxies_(Namco_NB-2)"),
    ("ridge-racer", r"VGM_collection\old\Ridge_Racer_(Namco_System_22)"),
    ("ridge-racer-2", r"VGM_collection\old\Ridge_Racer_2_(Namco_System_22)"),
]

JOBS: list[tuple[Path, Path]] = []
for slug, rel in PACKS:
    outdir = BASE / "output" / slug / "fur"
    outdir.mkdir(parents=True, exist_ok=True)
    for vgz in sorted((BASE / rel).glob("*.vgz")):
        JOBS.append((vgz, outdir))

for tag, vgz in (
    ("S11", BASE / r"VGM_collection\new\Tekken_(Namco_System_11)\06 Windermere, UK.vgz"),
    ("S12", BASE / r"VGM_collection\new\SoulCalibur_(Namco_System_12)\03 The New Legend (Kilik, Edge Master).vgz"),
    ("S22", BASE / r"VGM_collection\old\Ridge_Racer_(Namco_System_22)\01 Welcome Racer.vgz"),
):
    outdir = BASE / "output" / "_c352test" / "fresh" / tag
    outdir.mkdir(parents=True, exist_ok=True)
    JOBS.append((vgz, outdir))


def main() -> int:
    failed = []
    for vgz, outdir in JOBS:
        proc = subprocess.run(
            [sys.executable, "-m", "vgm2151fur", "convert", str(vgz), "-o", str(outdir)],
            cwd=str(FUR_DIR), capture_output=True, text=True,
        )
        fur = outdir / f"{vgz.stem}.fur"
        if proc.returncode != 0 or not fur.is_file():
            failed.append(vgz.stem)
            print(f"FAIL {vgz.stem}: rc={proc.returncode}")
            print((proc.stdout or "")[-400:])
            print((proc.stderr or "")[-400:])
            continue
        mod = parse_fur(load_fur_bytes(fur))
        note = ""
        for line in (proc.stdout or "").splitlines():
            low = line.lower()
            if "link" in low or "truncat" in low or "warn" in low:
                note = " | " + line.strip()[:120]
        print(f"ok   {vgz.stem[:44]:44s} speed={mod.speed} hz={mod.hz:7.2f} n_ord={mod.n_ord:3d}{note}",
              flush=True)
    print(f"done: {len(JOBS) - len(failed)} ok, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
