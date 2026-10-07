"""Pitch trajectories of a source rip against any other VGM (usually an export).

    python vgmdiff.py <source.vgz> <other.vgm> [channel ...]

Thin CLI over ``vgm2151fur.diag.pitch_runs``. A run is a stretch where the
second file sits away from the source by more than half a semitone for at
least 100 ms. A healthy conversion has no runs. Even one run is worth
reading, because a held wrong pitch sounds worse than a missed note.

Export the .fur with fur2vgm.py first, or use the compare command for the
whole loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vgm2151fur.diag import pitch_runs
from vgm2151fur.vgm import load_vgm


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src = load_vgm(Path(sys.argv[1]))
    other = load_vgm(Path(sys.argv[2]))
    channels = [int(a) for a in sys.argv[3:]] or list(range(8))
    print(f"source: {src.total_samples} samples (loop {src.loop_sample})")
    print(f"other:  {other.total_samples} samples (loop {other.loop_sample})")
    rc = 0
    for ch in channels:
        runs = pitch_runs(src, other, ch)
        print(f"ch{ch}: {len(runs)} run(s)")
        for run in runs:
            print("  ", run)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
