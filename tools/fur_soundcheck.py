"""Playback audit for one track: the source rip, the .fur, the exported VGM.

    python fur_soundcheck.py <source.vgm|vgz> <output.fur> [--wavdir DIR]

Renders the track three ways and reports how they compare:

  1. source rip                 via the reference renderer
  2. .fur through Furnace       what a Furnace user hears
  3. a VGM export of the .fur   via the reference renderer (portable truth)

Prints the pitch-shift table (from pitch_shift.analyse) for 1-vs-2 and 1-vs-3.
Both should sit at shift 0. It also echoes the renderer's Dev lines, so chip
clocks can be compared directly.

The failure this was built for (fixed in 0.9.18): C140 .furs exported clock
18432000 where the source said 12288000, so the file played 1.5x high (+7.02
semitones) in every conforming player. Register diffs cannot see it, because
the register stream is clock-scale invariant. Only the clock field and the
audible pitch can.

Renders land in --wavdir (default: <fur folder>/soundcheck) and are kept for
listening. Expect a few minutes on a long track.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import renderer
from pitch_shift import analyse
from vgm2151fur.furnace import export_vgm


def dev_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if re.match(r"\s*Dev \d+:", ln)]


def report(pair: str, res: dict) -> None:
    print("  %s: best shift %+d semitones (median %.3f; at 0: %.3f; %d windows)"
          % (pair, res["best"], res["best_score"], res["at_zero"], res["windows"]))
    if res["best"] != 0:
        print("      " + "  ".join("%+d:%.3f" % (s, res["med"][s]) for s in sorted(res["med"])))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="source rip (.vgm/.vgz)")
    ap.add_argument("fur", help="converted .fur")
    ap.add_argument("--wavdir", default=None, help="where renders are kept")
    args = ap.parse_args()
    src, fur = Path(args.source), Path(args.fur)
    if not src.is_file() or not fur.is_file():
        raise SystemExit("source and .fur must both exist")
    wavdir = Path(args.wavdir) if args.wavdir else fur.parent / "soundcheck"
    stem = fur.stem.replace(" ", "_")

    print(f"fur_soundcheck: {src.name} vs {fur.name}")
    print("  [1/4] rendering source with vgm2wav-mute ...")
    src_log = renderer.render(src, wavdir / (stem + ".src.wav"))
    print("  [2/4] rendering .fur with Furnace ...")
    from vgm2151fur.furnace import render_wav

    render_wav(fur, wavdir / (stem + ".fur.wav"))
    print("  [3/4] exporting .fur and rendering the export ...")
    exp = wavdir / (stem + ".export.vgm")
    export_vgm(fur, exp)
    exp_log = renderer.render(exp, wavdir / (stem + ".exp.wav"))
    print("  [4/4] comparing ...")
    report("source vs fur playback ", analyse(wavdir / (stem + ".src.wav"),
                                              wavdir / (stem + ".fur.wav")))
    report("source vs export render", analyse(wavdir / (stem + ".src.wav"),
                                              wavdir / (stem + ".exp.wav")))
    print("  chip clocks (source must match export):")
    for ln in dev_lines(src_log):
        print("    source: " + ln)
    for ln in dev_lines(exp_log):
        print("    export: " + ln)
    print(f"  renders kept in {wavdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
