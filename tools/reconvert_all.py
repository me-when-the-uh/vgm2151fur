"""Reconvert every pack under output/ from its VGM_collection source.

    python tools/reconvert_all.py [--only slug,...] [--workers N] [--dry-run]

The output tree is the source of truth for what to reconvert: every
`output/<slug>/fur/*.fur` is matched back to the VGM_collection pack its
track stems came from. Volume normalization is on (the CLI default), so
every track lands at -1.5 dBFS peak.

The grid comes from the estimator, never from the stored speed. A stored
`--speed` is the legacy manual path, which forces hz to 60; re-passing a
measured grid's speed as a manual speed collapses the row rate (Fantasy
Zone II DX: 500 Hz / speed 5 is 100 rows/s and converts to 60 Hz / speed 5,
12 rows/s; Assault 04: 497.7 Hz / speed 8 is 62 rows/s and converts to
7.5 rows/s), swallowing notes, vibrato and sub-row detail. The estimator
reproduces the stored grids (verified against the hand-kept
`fantasy-zone-2-dx-old` copies and the sources). A track that truly needs a
manual pin should be converted alone with `--speed N` afterwards.

Prints one line per track and a summary. Failures do not stop the batch.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
FUR_DIR = BASE / "vgm2151fur"
sys.path.insert(0, str(FUR_DIR))

from vgm2151fur.convert import (
    _MANIFEST,
    _MANIFEST_HEADER,
    _manifest_line,
    convert_variants,
    convert_vgm,
)
from vgm2151fur.levels import peak_dbfs
from vgm2151fur.packs import find_packs
from vgm2151fur.priority import low_priority, resolve_workers, set_low_priority


def _fur_root(fur: Path) -> Path:
    """The destination root: `fur/`, or `fur/default`'s parent in the dir layout."""
    return fur.parent.parent if fur.parent.name == "default" else fur.parent


def job(src: str, dst: str, slug: str, optimize: str = "none",
        verify: bool = False) -> tuple[str, dict | None, str | None]:
    set_low_priority()
    dst = Path(dst)
    try:
        if optimize != "none":
            results = convert_variants(
                Path(src), _fur_root(dst), factors=(1, 2, 3, 4), mode=optimize,
                normalize=True, verify=verify, layout="dirs",
            )
            info = dict(next(r for r in results if r["pick"]))
            info["variants"] = results
        else:
            info = convert_vgm(Path(src), dst, normalize=True)
    except Exception as exc:
        return slug, None, str(exc)
    return slug, info, None


def plan() -> list[tuple[str, Path, Path]]:
    """(slug, source, destination) for every existing .fur."""
    sources = find_packs(BASE / "VGM_collection", max_depth=6, limit=400)
    index = [(p, {t.stem.lower(): t for t in p.tracks}) for p in sources]
    jobs: list[tuple[str, Path, Path]] = []
    for pack_dir in sorted((BASE / "output").iterdir()):
        if not pack_dir.is_dir() or pack_dir.name.startswith((".", "_")):
            continue
        if pack_dir.name.endswith("-old"):
            continue  # hand-kept reference copies, never reconvert in place
        fur_dir = pack_dir / "fur"
        if not fur_dir.is_dir():
            continue
        furs = sorted(fur_dir.glob("*.fur"))
        if not furs:
            # The dir layout keeps its canonical 1x files in default/.
            furs = sorted((fur_dir / "default").glob("*.fur"))
        if not furs:
            continue
        best, best_score = None, 0
        for pack, by_stem in index:
            score = sum(1 for f in furs if f.stem.lower() in by_stem)
            if score > best_score:
                best, best_score = by_stem, score
        if best is None or best_score * 2 < len(furs):
            print(f"skip {pack_dir.name}: no VGM_collection source matched ({len(furs)} fur)",
                  flush=True)
            continue
        unmatched = [f.stem for f in furs if f.stem.lower() not in best]
        if unmatched:
            print(f"note {pack_dir.name}: {len(unmatched)} fur with no source track "
                  f"({', '.join(unmatched[:3])})", flush=True)
        for fur in furs:
            src = best.get(fur.stem.lower())
            if src is not None:
                jobs.append((pack_dir.name, src, fur))
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma-separated slugs")
    ap.add_argument(
        "--workers", type=int, default=None,
        help=(
            "tracks at once, at low priority (default: logical CPUs minus one "
            "or two threads, capped at 16; requests cap at threads minus one, 31 max)"
        ),
    )
    ap.add_argument("--dry-run", action="store_true", help="print the plan only")
    ap.add_argument(
        "--optimize", choices=("none", "lossless", "all"), default="none",
        help="also write the 1x..4x set into default/ and xN optimised/ "
             "(lossless keeps only the factors that add no loss); default none",
    )
    ap.add_argument(
        "--verify", action="store_true",
        help="with --optimize lossless: render each variant through Furnace and "
             "reject any that adds a sustained pitch divergence over 1x",
    )
    args = ap.parse_args()

    print("planning...", flush=True)
    jobs = plan()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        jobs = [j for j in jobs if j[0] in wanted]
    if args.dry_run:
        for slug, src, _dst in jobs:
            print(f"{slug:22s} {src.name:44s} auto")
        print(f"plan: {len(jobs)} tracks")
        return 0
    if not jobs:
        print("nothing to do")
        return 1

    n_workers, note = resolve_workers(args.workers, len(jobs))
    print(f"reconverting {len(jobs)} tracks, {n_workers} workers ({note})", flush=True)
    started = time.time()
    ok, failed = _run_jobs(jobs, n_workers, args.optimize, args.verify)
    print(f"done: {ok} ok, {failed} failed in {time.time() - started:.0f}s")
    return 1 if failed else 0


def _print_result(slug: str, dst: Path, info, error) -> bool:
    if error is not None:
        print(f"FAIL {slug}/{dst.name}: {error}", flush=True)
        return False
    vol = f"vol {info['gain']:.2f}x" if info.get("gain") is not None else "vol -"
    peak = f" peak={peak_dbfs(info['peak']):+.1f}dBFS" if info.get("peak") else ""
    warn = ""
    if info["warnings"]:
        warn = "  | " + "; ".join(info["warnings"])[:120]
    print(
        f"ok   {slug[:16]:16s} {dst.name[:40]:40s} speed={info['speed']} "
        f"{vol}{peak}{warn}",
        flush=True,
    )
    return True


def _run_jobs(jobs, n_workers: int, optimize: str = "none",
              verify: bool = False) -> tuple[int, int]:
    ok = failed = 0
    seen: set[str] = set()

    def take(slug: str, dst: Path, info, error) -> None:
        nonlocal ok, failed
        if optimize != "none" and info and info.get("variants"):
            manifest = _fur_root(dst) / _MANIFEST
            if str(manifest) not in seen:
                seen.add(str(manifest))
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(_MANIFEST_HEADER, encoding="utf-8")
            with manifest.open("a", encoding="utf-8") as fh:
                for v in info["variants"]:
                    fh.write(_manifest_line(dst, v))
        if _print_result(slug, dst, info, error):
            ok += 1
        else:
            failed += 1

    if n_workers <= 1:
        with low_priority():
            for slug, src, dst in jobs:
                _slug, info, error = job(str(src), str(dst), slug, optimize, verify)
                take(slug, dst, info, error)
        return ok, failed

    with ProcessPoolExecutor(max_workers=n_workers, initializer=set_low_priority) as pool:
        futs = {
            pool.submit(job, str(src), str(dst), slug, optimize, verify): (slug, dst)
            for slug, src, dst in jobs
        }
        for fut in as_completed(futs):
            slug, dst = futs[fut]
            try:
                _slug, info, error = fut.result()
            except Exception as exc:
                info, error = None, str(exc)
            take(slug, dst, info, error)
    return ok, failed


if __name__ == "__main__":
    sys.exit(main())
