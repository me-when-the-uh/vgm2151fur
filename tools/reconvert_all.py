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

from vgm2151fur.convert import convert_vgm
from vgm2151fur.levels import peak_dbfs
from vgm2151fur.packs import find_packs
from vgm2151fur.priority import low_priority, resolve_workers, set_low_priority


def job(src: str, dst: str, slug: str) -> tuple[str, dict | None, str | None]:
    set_low_priority()
    try:
        info = convert_vgm(Path(src), Path(dst), normalize=True)
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
        help="tracks at once, at low priority (default: physical cores minus one)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print the plan only")
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
    ok, failed = _run_jobs(jobs, n_workers)
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


def _run_jobs(jobs, n_workers: int) -> tuple[int, int]:
    ok = failed = 0

    def take(slug: str, dst: Path, info, error) -> None:
        nonlocal ok, failed
        if _print_result(slug, dst, info, error):
            ok += 1
        else:
            failed += 1

    if n_workers <= 1:
        with low_priority():
            for slug, src, dst in jobs:
                _slug, info, error = job(str(src), str(dst), slug)
                take(slug, dst, info, error)
        return ok, failed

    with ProcessPoolExecutor(max_workers=n_workers, initializer=set_low_priority) as pool:
        futs = {
            pool.submit(job, str(src), str(dst), slug): (slug, dst)
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
