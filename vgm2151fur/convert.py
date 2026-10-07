"""Convert VGMs to .fur files, one at a time or as a folder."""

from __future__ import annotations

import re
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from vgm2151fur.analyze import analyze, format_report
from vgm2151fur.furnace import FurnaceError, restore_console
from vgm2151fur.furwrite import write_fur
from vgm2151fur.levels import normalize_module
from vgm2151fur.loopfind import apply_loop
from vgm2151fur.pcm import dump_pcm as dump_pcm_files
from vgm2151fur.priority import low_priority, resolve_workers, set_low_priority
from vgm2151fur.vgm import load_vgm

# One less than the physical cores. `resolve_workers` is the cap a call uses.
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


def report_vgm(path: Path, *, speed: int | None = None, pcm: bool = True) -> str:
    vgm = load_vgm(path)
    song = analyze(vgm, speed=speed, pcm=pcm)
    return format_report(song)


def _report_one(path_str: str, speed: int | None, pcm: bool) -> str:
    return report_vgm(Path(path_str), speed=speed, pcm=pcm)


def report_files(
    files: list[Path],
    *,
    speed: int | None = None,
    pcm: bool = True,
    workers: int | None = None,
    log=print,
) -> Iterator[tuple[Path, str | None, str | None]]:
    """Analyze each file. Yields (src, report, None) in input order.

    One track per worker process, at low priority. The count stops at one
    less than the physical core count.
    """
    if not files:
        return
    n_workers, note = resolve_workers(workers, len(files))
    if n_workers <= 1:
        with low_priority():
            for f in files:
                try:
                    yield f, report_vgm(f, speed=speed, pcm=pcm), None
                except Exception as exc:
                    yield f, None, str(exc)
        return
    log(f"workers: {n_workers} ({note})")
    with ProcessPoolExecutor(max_workers=n_workers, initializer=set_low_priority) as pool:
        futs = [pool.submit(_report_one, str(f), speed, pcm) for f in files]
        for f, fut in zip(files, futs):
            try:
                yield f, fut.result(), None
            except Exception as exc:
                yield f, None, str(exc)


def dump_pcm(path: Path, out_dir: Path) -> dict:
    vgm = load_vgm(path)
    return dump_pcm_files(vgm, out_dir / path.stem)


def convert_vgm(
    path: Path,
    out_path: Path,
    *,
    speed: int | None = None,
    pcm: bool = True,
    include_fm: bool = True,
    normalize: bool = False,
    loop_find: bool = True,
) -> dict:
    vgm = load_vgm(path)
    song = analyze(vgm, speed=speed, pcm=pcm)
    loop_note = None
    if loop_find:
        loop_note = apply_loop(song)
    if not song.fm_patches:
        # A track with no FM key-ons must not carry an empty YM2151: the chip
        # adds a second device to the export, and VGM players rescale the mix
        # for it, which drops the sample chip's level (C352-only rips measured
        # 4-10x quieter). Sample-only modules are written without FM channels.
        include_fm = False
    data = write_fur(song, include_pcm=pcm, include_fm=include_fm)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    warnings = list(song.warnings)
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
    return {
        "src": str(path),
        "dst": str(out_path),
        "bytes": len(data),
        "speed": song.speed,
        "fm_patches": len(song.fm_patches) if include_fm else 0,
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


_CFG: dict = {}


def _init_worker(cfg: dict) -> None:
    global _CFG
    _CFG = cfg
    set_low_priority()


def _worker(path_str: str) -> dict:
    src = Path(path_str)
    return convert_vgm(
        src,
        Path(_CFG["out_dir"]) / (src.stem + ".fur"),
        speed=_CFG["speed"],
        pcm=_CFG["pcm"],
        include_fm=_CFG["include_fm"],
        normalize=_CFG["normalize"],
        loop_find=_CFG["loop_find"],
    )


def _convert_serial(files, out_dir, speed, pcm, include_fm, normalize, loop_find):
    for f in files:
        try:
            info = convert_vgm(
                f,
                out_dir / (f.stem + ".fur"),
                speed=speed,
                pcm=pcm,
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
    include_fm: bool = True,
    normalize: bool = False,
    loop_find: bool = True,
    workers: int | None = None,
    log=print,
) -> Iterator[tuple[Path, dict | None, str | None]]:
    """Convert every VGM/VGZ to `out_dir/<name>.fur`.

    Yields (src, info, None) as each file finishes, or (src, None, error) for a
    file that raised. The rest still convert. One track per worker process,
    at low priority. The count stops at one less than the physical core count.
    """
    try:
        if not files:
            return
        n_workers, note = resolve_workers(workers, len(files))
        if n_workers <= 1:
            with low_priority():
                yield from _convert_serial(
                    files, out_dir, speed=speed, pcm=pcm,
                    include_fm=include_fm, normalize=normalize,
                    loop_find=loop_find,
                )
            return
        log(f"workers: {n_workers} ({note})")
        cfg = {
            "out_dir": str(out_dir),
            "speed": speed,
            "pcm": pcm,
            "include_fm": include_fm,
            "normalize": normalize,
            "loop_find": loop_find,
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
                    yield f, info, None
    finally:
        restore_console()
