"""Convert VGMs to .fur files, one at a time or as a folder."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from vgm2151fur.analyze import analyze, format_report
from vgm2151fur.furnace import FurnaceError, restore_console
from vgm2151fur.furwrite import dev_max, loss_total, write_fur
from vgm2151fur.levels import normalize_module
from vgm2151fur.loopfind import apply_loop
from vgm2151fur.pcm import dump_pcm as dump_pcm_files
from vgm2151fur.priority import low_priority, resolve_workers, set_low_priority
from vgm2151fur.vgm import load_vgm

# The desktop default: logical CPUs minus one or two threads, capped at 16.
CONVERT_WORKERS = resolve_workers(None, jobs=10**6)[0]
VGM_SUFFIXES = {".vgm", ".vgz"}
VGM_GLOBS = ("*.vgm", "*.vgz", "*.VGM", "*.VGZ")


def track_number(path: Path) -> int | None:
    m = re.match(r"^(\d+)", path.stem)
    return int(m.group(1)) if m else None


def iter_vgms(path: Path) -> list[Path]:
    """`.vgm`/`.vgz` files in `path`, or `[path]` when that file is one.

    A directory is not walked. Nested packs are a separate scan.
    """
    if path.is_file():
        return [path] if path.suffix.lower() in VGM_SUFFIXES else []
    files: list[Path] = []
    for glob in VGM_GLOBS:
        files.extend(path.glob(glob))
    seen: set[str] = set()
    out: list[Path] = []
    for p in sorted(files, key=lambda x: x.name.lower()):
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def select_tracks(files: list[Path], single_track: str | int | None) -> list[Path]:
    """`15` matches a leading track number, any other token a stem substring."""
    if single_track is None or single_track == "":
        return list(files)
    token = str(single_track).strip()
    if token.isdigit():
        n = int(token)
        hit = [p for p in files if track_number(p) == n]
    else:
        low = token.lower()
        hit = [p for p in files if low in p.stem.lower()]
    if not hit:
        names = ", ".join(p.name for p in files[:8])
        raise FileNotFoundError(
            f"no track matching {single_track!r} (looked at: {names}{'...' if len(files) > 8 else ''})"
        )
    return hit


def report_vgm(
    path: Path, *, speed: int | None = None, pcm: bool = True,
    tick_samples: float | None = None, min_row: float | None = None,
) -> str:
    vgm = load_vgm(path)
    song = analyze(vgm, speed=speed, pcm=pcm, tick_samples=tick_samples, min_row=min_row)
    return format_report(song)


def _report_one(
    path_str: str, speed: int | None, pcm: bool,
    tick_samples: float | None, min_row: float | None,
) -> str:
    return report_vgm(
        Path(path_str), speed=speed, pcm=pcm,
        tick_samples=tick_samples, min_row=min_row,
    )


def report_files(
    files: list[Path],
    *,
    speed: int | None = None,
    pcm: bool = True,
    tick_samples: float | None = None,
    min_row: float | None = None,
    workers: int | None = None,
    log=print,
) -> Iterator[tuple[Path, str | None, str | None]]:
    """Analyze each file. Yields (src, report, None) in input order.

    One track per worker process, at low priority. The default count follows
    the logical CPUs (see `priority.default_workers`).
    """
    if not files:
        return
    n_workers, note = resolve_workers(workers, len(files))
    if n_workers <= 1:
        with low_priority():
            for f in files:
                try:
                    yield f, report_vgm(
                        f, speed=speed, pcm=pcm,
                        tick_samples=tick_samples, min_row=min_row,
                    ), None
                except Exception as exc:
                    yield f, None, str(exc)
        return
    log(f"workers: {n_workers} ({note})")
    with ProcessPoolExecutor(max_workers=n_workers, initializer=set_low_priority) as pool:
        futs = [
            pool.submit(_report_one, str(f), speed, pcm, tick_samples, min_row)
            for f in files
        ]
        for f, fut in zip(files, futs):
            try:
                yield f, fut.result(), None
            except Exception as exc:
                yield f, None, str(exc)


def dump_pcm(path: Path, out_dir: Path) -> dict:
    vgm = load_vgm(path)
    return dump_pcm_files(vgm, out_dir / path.stem)


def furnace_bpm(song) -> float:
    """The BPM Furnace displays: `calcBPM` with hilight 4 = 15 x rows/s."""
    return 15.0 * song.hz / song.speed


def _verify_runs(
    src_vgm, fur_path: Path, channels: int = 8,
    threshold_st: float = 0.25, min_run_ms: float = 60.0,
) -> int:
    """Pitch runs between the source and the .fur's Furnace export.

    The structural counters cannot see everything (retrigger/envelope wobble
    and sub-threshold drift), so `--verify` renders each candidate and counts
    the sustained divergences the `compare` command reports.  The threshold is
    finer than `compare`'s default (0.5 st / 100 ms), where a marginal variant
    can still hide.
    """
    from vgm2151fur.diag import export_fur, pitch_runs

    exp = fur_path.with_suffix(".verify.vgm")
    try:
        export_fur(fur_path, exp)
        other = load_vgm(exp)
        return sum(
            len(pitch_runs(src_vgm, other, ch, threshold_st=threshold_st,
                           min_run_ms=min_run_ms))
            for ch in range(channels)
        )
    finally:
        try:
            exp.unlink()
        except OSError:
            pass


def _emit(
    song, out_path: Path, *, pcm: bool, include_fm: bool, normalize: bool,
    warnings: list, stats: dict,
) -> tuple[bytes, float | None, float | None]:
    """Write one Song to `out_path`, optionally normalizing. (bytes, peak, gain)."""
    # A track with no FM key-ons must not carry an empty YM2151: the chip adds a
    # second device to the export, and VGM players rescale the mix for it, which
    # drops the sample chip's level.
    use_fm = include_fm and bool(song.fm_patches)
    data = write_fur(song, include_pcm=pcm, include_fm=use_fm, stats=stats)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    peak = gain = None
    if normalize:
        try:
            result = normalize_module(data, name=out_path.stem)
        except (FurnaceError, ValueError) as exc:
            text = str(exc)
            if "unrecognized system ID" in text:
                warnings.append(
                    "volume normalization skipped: this Furnace build cannot "
                    "load the module (unrecognized system ID)"
                )
            else:
                warnings.append(f"volume normalization skipped: {text.splitlines()[0]}")
        else:
            if result is not None:
                data = result.data
                peak, gain = result.peak, result.gain
                out_path.write_bytes(data)
    return data, peak, gain


def convert_vgm(
    path: Path,
    out_path: Path,
    *,
    speed: int | None = None,
    pcm: bool = True,
    tick_samples: float | None = None,
    min_row: float | None = None,
    condense: float = 1.0,
    include_fm: bool = True,
    normalize: bool = False,
    loop_find: bool = True,
) -> dict:
    vgm = load_vgm(path)
    song = analyze(vgm, speed=speed, pcm=pcm, tick_samples=tick_samples,
                   min_row=min_row, condense=condense)
    loop_note = None
    if loop_find:
        loop_note = apply_loop(song)
    warnings = list(song.warnings)
    stats: dict = {}
    data, peak, gain = _emit(
        song, Path(out_path), pcm=pcm, include_fm=include_fm,
        normalize=normalize, warnings=warnings, stats=stats,
    )
    return {
        "src": str(path),
        "dst": str(out_path),
        "bytes": len(data),
        "speed": song.speed,
        "hz": song.hz,
        "bpm": furnace_bpm(song),
        "rows_per_s": song.hz / song.speed,
        "condense": condense,
        "loss": dict(stats),
        "loss_total": loss_total(stats),
        "dev_max": dev_max(stats),
        "fm_patches": len(song.fm_patches) if (include_fm and song.fm_patches) else 0,
        "pcm_samples": sum(1 for s in song.pcm_samples if s.length >= 8) if pcm else 0,
        "oki_samples": len(song.oki_samples) if pcm else 0,
        "msm6258_samples": len(song.msm6258_samples) if pcm else 0,
        "segapcm_samples": len(song.segapcm_samples) if pcm else 0,
        "c140_samples": len(song.c140_samples) if pcm else 0,
        "c352_samples": len(song.c352_samples) if pcm else 0,
        "events": sum(1 for e in song.events if e.on),
        "warnings": warnings,
        "name": vgm.name,
        "peak": peak,
        "gain": gain,
        "loop": loop_note,
    }


def _optimised_dir(factor: float) -> str:
    """The per-factor subfolder: `default/`, `x2 optimised/`, `x3 optimised/`."""
    return "default" if factor == 1 else f"x{factor:g} optimised"


def default_factors(condense: float) -> tuple[float, ...]:
    """The 1x..Nx ladder with the half steps kept: 1, 1.5, 2, 2.5, ... N.

    A row is not a whole number of samples.  A unit that lands between two
    whole steps would otherwise be skipped.
    """
    top = max(1.0, float(condense))
    out: list[float] = []
    step = 1
    while step <= top:
        out.append(float(step))
        if step + 0.5 <= top:
            out.append(step + 0.5)
        step += 1
    return tuple(out)


def _place_dirs(results: list[dict], out_dir: Path, keep: set) -> None:
    """Move each kept factor into its subfolder; delete the ones that lost.

    The `keep` set is every factor the lossless gate accepted (always 1).
    """
    for r in results:
        path = Path(r["dst"])
        if r["factor"] in keep:
            dest = out_dir / _optimised_dir(r["factor"]) / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path != dest:
                path.replace(dest)
            r["dst"] = str(dest)
        elif path.exists():
            try:
                path.unlink()
            except OSError:
                pass


_MANIFEST = "condense.tsv"
_MANIFEST_HEADER = "track\tfactor\tfile\tbpm\tloss\tdev\tdiv\tlossless\tpick\tgrid\n"


def _manifest_line(src: Path, v: dict) -> str:
    div = "" if v.get("div_runs") is None else str(v["div_runs"])
    return "\t".join((
        src.stem, f"{v['factor']:g}", Path(v["dst"]).name,
        f"{v['bpm']:.1f}", str(v["loss_total"]), str(v["dev_max"]), div,
        "1" if v.get("lossless") else "0", "1" if v.get("pick") else "0",
        v.get("grid", ""),
    )) + "\n"


def convert_variants(
    path: Path,
    out_dir: Path,
    *,
    factors: Sequence[float] = (1, 2, 3, 4),
    mode: str = "all",
    speed: int | None = None,
    pcm: bool = True,
    min_row: float | None = None,
    include_fm: bool = True,
    normalize: bool = False,
    loop_find: bool = True,
    verify: bool = False,
    layout: str = "flat",
) -> list[dict]:
    """Write one .fur per condense factor, side by side, and pick one.

    factor 1 is the canonical `<stem>.fur`; factor N is `<stem> xN.fur`, so the
    canonical file is never overwritten by a condensed copy.  `mode` only
    decides the reported `pick`:
      none     -> 1x;
      lossless -> the largest factor whose structural loss does not exceed the
                  1x baseline (falls back to 1x);
      all      -> the largest factor.
    With `verify`, the lossless pick also renders each candidate and rejects
    any that adds a sustained pitch divergence over 1x.

    With `layout="dirs"` each accepted factor is moved to `<out_dir>/default/`
    or `<out_dir>/xN optimised/` and the rejected factors are deleted, so a
    finished folder holds only renditions that were judged lossless.
    """
    vgm = load_vgm(path)
    src = Path(path)
    out_dir = Path(out_dir)
    results: list[dict] = []
    for f in factors:
        factor = float(f)
        song = analyze(vgm, speed=speed, pcm=pcm, min_row=min_row, condense=factor)
        loop_note = apply_loop(song) if loop_find else None
        warnings = list(song.warnings)
        stats: dict = {}
        name = f"{src.stem}.fur" if factor == 1 else f"{src.stem} x{factor:g}.fur"
        out_path = out_dir / name
        data, peak, gain = _emit(
            song, out_path, pcm=pcm, include_fm=include_fm,
            normalize=normalize, warnings=warnings, stats=stats,
        )
        results.append({
            "factor": factor,
            "dst": str(out_path),
            "bytes": len(data),
            "speed": song.speed,
            "hz": song.hz,
            "bpm": furnace_bpm(song),
            "rows_per_s": song.hz / song.speed,
            "grid": song.grid_note,
            "loss": dict(stats),
            "loss_total": loss_total(stats),
            "dev_max": dev_max(stats),
            "warnings": warnings,
            "loop": loop_note,
            "peak": peak,
            "gain": gain,
            "div_runs": _verify_runs(vgm, out_path) if verify else None,
        })
    base = next((r for r in results if r["factor"] == 1), results[0])
    # Structural counters that change content and that the rendered pitch
    # comparison cannot see: a dropped note, a swallowed note-off, an evicted
    # sample or a lost effect.  The sweep/fade counters describe the pitch
    # model, which --verify measures directly. Under verify they are advisory:
    # a factor whose render matches 1x should not be blocked by a clipped
    # sweep step the render shows is inaudible.
    hard_keys = (
        "note_collision", "off_swallowed", "pcm_evicted", "pcm_unplaced",
        "fx_dropped", "orders_truncated",
    )
    tol = 6
    base_runs = base.get("div_runs")
    base_hard = sum(base["loss"].get(k, 0) for k in hard_keys)

    def _lossless(r: dict) -> bool:
        if verify:
            if sum(r["loss"].get(k, 0) for k in hard_keys) > base_hard:
                return False
            if base_runs is not None and r.get("div_runs") is not None:
                if r["div_runs"] > base_runs + 1:
                    return False
            return True
        # Without a render the analytic counters are all there is.
        if r["loss_total"] > base["loss_total"]:
            return False
        if r["dev_max"] > base["dev_max"] + tol:
            return False
        return True

    for r in results:
        r["lossless"] = _lossless(r)
    ok = [r for r in results if r["lossless"]]
    if mode == "none":
        pick = base
    elif mode == "lossless":
        pick = max(ok, key=lambda r: r["factor"])
    else:
        pick = max(results, key=lambda r: r["factor"])
    for r in results:
        r["pick"] = r is pick
    if layout == "dirs":
        keep = {r["factor"] for r in (results if mode == "all" else ok)}
        _place_dirs(results, out_dir, keep=keep)
    return results


_CFG: dict = {}


def _variant_info(
    src: Path, out_dir: Path, *, factors, mode, speed, pcm, min_row,
    include_fm, normalize, loop_find, verify, layout,
) -> dict:
    """Write the 1x/2x/3x/4x set and return the pick's info plus the set."""
    results = convert_variants(
        src, out_dir, factors=factors, mode=mode, speed=speed, pcm=pcm,
        min_row=min_row, include_fm=include_fm, normalize=normalize,
        loop_find=loop_find, verify=verify, layout=layout,
    )
    pick = next(r for r in results if r["pick"])
    info = dict(pick)
    info["variants"] = results
    return info


def _init_worker(cfg: dict) -> None:
    global _CFG
    _CFG = cfg
    set_low_priority()


def _worker(path_str: str) -> dict:
    src = Path(path_str)
    if _CFG.get("variants"):
        return _variant_info(
            src, Path(_CFG["out_dir"]),
            factors=_CFG["factors"], mode=_CFG["optimize"],
            speed=_CFG["speed"], pcm=_CFG["pcm"], min_row=_CFG["min_row"],
            include_fm=_CFG["include_fm"], normalize=_CFG["normalize"],
            loop_find=_CFG["loop_find"], verify=_CFG["verify"],
            layout=_CFG["layout"],
        )
    return convert_vgm(
        src,
        Path(_CFG["out_dir"]) / (src.stem + ".fur"),
        speed=_CFG["speed"],
        pcm=_CFG["pcm"],
        tick_samples=_CFG["tick_samples"],
        min_row=_CFG["min_row"],
        include_fm=_CFG["include_fm"],
        normalize=_CFG["normalize"],
        loop_find=_CFG["loop_find"],
    )


def _convert_serial(files, out_dir, *, speed, pcm, tick_samples, min_row,
                    variants, factors, optimize, include_fm, normalize, loop_find,
                    verify, layout):
    for f in files:
        try:
            if variants:
                info = _variant_info(
                    f, out_dir, factors=factors, mode=optimize,
                    speed=speed, pcm=pcm, min_row=min_row,
                    include_fm=include_fm, normalize=normalize, loop_find=loop_find,
                    verify=verify, layout=layout,
                )
            else:
                info = convert_vgm(
                    f,
                    out_dir / (f.stem + ".fur"),
                    speed=speed,
                    pcm=pcm,
                    tick_samples=tick_samples,
                    min_row=min_row,
                    include_fm=include_fm,
                    normalize=normalize,
                    loop_find=loop_find,
                )
        except Exception as exc:
            yield f, None, str(exc)
        else:
            yield f, info, None


def convert_folder(
    files: list[Path],
    out_dir: Path,
    *,
    speed: int | None = None,
    pcm: bool = True,
    tick_samples: float | None = None,
    min_row: float | None = None,
    variants: bool = False,
    condense: int = 4,
    factors: Sequence[float] | None = None,
    optimize: str = "all",
    include_fm: bool = True,
    normalize: bool = False,
    loop_find: bool = True,
    verify: bool = False,
    layout: str = "flat",
    workers: int | None = None,
    log=print,
) -> Iterator[tuple[Path, dict | None, str | None]]:
    """Convert every VGM/VGZ to `out_dir/<name>.fur`.

    Yields (src, info, None) as each file finishes, or (src, None, error) for a
    file that raised. The rest still convert. One track per worker process,
    at low priority. The default count follows the logical CPUs.

    With `variants` set, each file is written as the 1x..`condense`x set and
    `info` carries the whole set in `info["variants"]` plus the pick.  With
    `layout="dirs"` the accepted factors land in `default/` and `xN optimised/`
    subfolders and a `condense.tsv` manifest records every verdict.
    """
    try:
        if not files:
            return
        if factors is not None:
            factor_list = tuple(float(f) for f in factors)
        elif variants:
            factor_list = default_factors(condense)
        else:
            factor_list = (1,)
        manifest = (
            Path(out_dir) / _MANIFEST
            if variants and layout == "dirs" else None
        )
        if manifest is not None and not manifest.exists():
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(_MANIFEST_HEADER, encoding="utf-8")

        def _note(src: Path, info: dict | None) -> None:
            if manifest is None or not info or not info.get("variants"):
                return
            with manifest.open("a", encoding="utf-8") as fh:
                for v in info["variants"]:
                    fh.write(_manifest_line(src, v))

        n_workers, note = resolve_workers(workers, len(files))
        if n_workers <= 1:
            with low_priority():
                for result in _convert_serial(
                    files, out_dir, speed=speed, pcm=pcm,
                    tick_samples=tick_samples, min_row=min_row,
                    variants=variants, factors=factor_list, optimize=optimize,
                    include_fm=include_fm, normalize=normalize,
                    loop_find=loop_find, verify=verify, layout=layout,
                ):
                    _note(result[0], result[1])
                    yield result
            return
        log(f"workers: {n_workers} ({note})")
        cfg = {
            "out_dir": str(out_dir),
            "speed": speed,
            "pcm": pcm,
            "tick_samples": tick_samples,
            "min_row": min_row,
            "variants": variants,
            "factors": factor_list,
            "optimize": optimize,
            "include_fm": include_fm,
            "normalize": normalize,
            "loop_find": loop_find,
            "verify": verify,
            "layout": layout,
        }
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_worker,
            initargs=(cfg,),
        ) as pool:
            futs = {pool.submit(_worker, str(f)): f for f in files}
            for fut in as_completed(futs):
                f = futs[fut]
                try:
                    info = fut.result()
                except Exception as exc:
                    yield f, None, str(exc)
                else:
                    _note(f, info)
                    yield f, info, None
    finally:
        restore_console()
