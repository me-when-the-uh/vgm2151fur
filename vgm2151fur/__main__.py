"""Command line: convert, report, edit, compare, render.

A path on its own is enough. Quotes, globs, a pack folder, and a folder of
packs all work. With no arguments the menu opens.

    python -m vgm2151fur "Song.vgz"
    python -m vgm2151fur "Pack folder"
    python -m vgm2151fur convert "Song.vgz" -o out
    python -m vgm2151fur report "Song.vgz"
    python -m vgm2151fur edit "Pack folder" --chip all --factor 0.9
    python -m vgm2151fur compare "Song.vgz" "Song.fur" --channel 0
    python -m vgm2151fur render "Song.fur" --loops 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vgm2151fur import __version__
from vgm2151fur.convert import VGM_SUFFIXES, convert_folder, report_files
from vgm2151fur.edit import FurEditError, read_chips_file, set_chip_volume_file
from vgm2151fur.furnace import FurnaceError, render_wav
from vgm2151fur.packs import pack_tracks
from vgm2151fur.paths import clean_text, dedupe, expand_token, fur_targets, vgm_targets

COMMANDS = {"convert", "report", "edit", "compare", "render", "menu"}
_WORKERS_HELP = (
    "tracks at once, at low priority (default: logical CPUs minus one or two "
    "threads, capped at 16; requests cap at threads minus one, 31 max)"
)

EPILOG = """\
paths:
  A .vgm/.vgz file, a pack folder, or a folder of packs. Quotes around a
  pasted path are stripped. A glob the shell left unexpanded (*.vgz) is
  expanded here. edit and render also take a .fur file, a folder of them,
  or the pack folder that holds fur/.

  vgm2151fur "Song.vgz"
  vgm2151fur "Pack Folder" -o out
  vgm2151fur convert "Song.vgz" --no-normalize
  vgm2151fur edit "Pack Folder" --chip C140 --factor 0.9
  vgm2151fur render "Pack Folder" --loops 2
"""


class _Usage(Exception):
    def __init__(self, code: int):
        self.code = code


def _parser_exit(status: int = 0, message: str | None = None) -> None:
    if message:
        sys.stderr.write(message if message.endswith("\n") else message + "\n")
    raise _Usage(int(status or 0))


def _tokens(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _resolve(tokens: list[str]) -> tuple[list[Path], int]:
    """Existing paths from tokens. Missing ones are reported, not dropped silently."""
    found: list[Path] = []
    rc = 0
    if not tokens:
        print("ERROR: give a file or a folder", file=sys.stderr)
        return [], 2
    for token in tokens:
        matches = expand_token(token)
        if not matches:
            print(f"ERROR: no match for {clean_text(token)}", file=sys.stderr)
            rc = 1
            continue
        existing = [path for path in matches if path.exists()]
        if not existing:
            print(f"ERROR: {matches[0]} not found", file=sys.stderr)
            rc = 1
            continue
        found.extend(existing)
    return dedupe(found), rc


def _parse_targets(spec: str, chips) -> list[int]:
    if spec.strip().lower() in ("", "all", "*"):
        return list(range(len(chips)))
    out: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token or token.lower() == "all":
            out += range(len(chips))
            continue
        if token.isdigit():
            idx = int(token) - 1
            if not 0 <= idx < len(chips):
                raise SystemExit(f"chip {token} is out of range (1..{len(chips)})")
            out.append(idx)
            continue
        match = [c.index for c in chips if token.lower() in c.name.lower()]
        if not match:
            raise SystemExit(f"no chip matching {token!r}")
        out += match
    return sorted(set(out))


def _sample_counts(info: dict) -> str:
    samples = f"{info['pcm_samples']} PCM"
    for key, label in (
        ("segapcm_samples", "SEGA"),
        ("c140_samples", "C140"),
        ("c352_samples", "C352"),
        ("oki_samples", "OKI"),
        ("msm6258_samples", "ADPCM"),
    ):
        if info.get(key):
            samples += f"  {info[key]} {label}"
    return samples


def _convert_groups(
    paths: list[Path], out_root: Path | None
) -> tuple[list[tuple[list[Path], Path]], int]:
    """(files, dest dir) pairs. A folder of packs writes one folder per pack."""
    groups: list[tuple[list[Path], Path]] = []
    collections: list[tuple[list[Path], Path]] = []
    rc = 0
    for path in paths:
        direct, packs = vgm_targets(path, pack_limit=None)
        if direct:
            dest = out_root or ((path.parent if path.is_file() else path) / "fur")
            groups.append((direct, dest))
            continue
        if not packs:
            print(f"ERROR: no .vgm/.vgz under {path}", file=sys.stderr)
            rc = 1
            continue
        many = len(packs) > 1
        for pack in packs:
            tracks, _playlist = pack_tracks(pack)
            if not tracks:
                continue
            if out_root is None:
                dest = pack.path / "fur"
            elif many:
                dest = out_root / pack.name
            else:
                dest = out_root
            collections.append((tracks, dest))
    if len(collections) > 1:
        total = sum(len(tracks) for tracks, _dest in collections)
        print(f"{len(collections)} packs, {total} tracks")
    groups.extend(collections)
    return _merge_groups(groups), rc


def _merge_groups(groups: list[tuple[list[Path], Path]]) -> list[tuple[list[Path], Path]]:
    merged: dict[str, list[Path]] = {}
    order: list[Path] = []
    for files, dest in groups:
        key = str(dest).lower()
        if key not in merged:
            merged[key] = []
            order.append(dest)
        have = {str(path).lower() for path in merged[key]}
        for path in files:
            marker = str(path).lower()
            if marker not in have:
                merged[key].append(path)
                have.add(marker)
    return [(merged[str(dest).lower()], dest) for dest in order]


def _min_row_for_bpm(max_bpm: float | None) -> float | None:
    """Row length (samples) whose row rate makes Furnace show `max_bpm`.

    Furnace's BPM is 60*hz/(hilight*speed) with hilight 4, which is
    15 * rows/s = 15 * (44100 / row).  So row = 661500 / bpm.
    """
    if not max_bpm or max_bpm <= 0:
        return None
    return 15.0 * 44100.0 / max_bpm


def _convert(args) -> int:
    paths, rc = _resolve(_tokens(args.input))
    if not paths:
        return rc or 1
    out_root = Path(clean_text(args.out)) if args.out else None
    groups, group_rc = _convert_groups(paths, out_root)
    rc = rc or group_rc
    if not groups:
        return rc or 1
    pcm = not args.melody_only
    include_fm = not args.pcm_only
    grand_done = grand_total = 0
    for files, dest in groups:
        done = 0
        for src, info, error in convert_folder(
            files,
            dest,
            speed=args.speed,
            pcm=pcm,
            min_row=_min_row_for_bpm(args.max_bpm),
            include_fm=include_fm,
            normalize=not args.no_normalize,
            loop_find=not args.no_loop_find,
            workers=args.workers,
        ):
            if error is not None:
                print(f"ERROR {src.name}: {error}", file=sys.stderr)
                rc = 1
                continue
            done += 1
            extra = ""
            notes = [w for w in info["warnings"] if w != info.get("loop")]
            if notes:
                extra = "  (" + "; ".join(notes[:4]) + ")"
            vol = f"  vol {info['gain']:.2f}x" if info.get("gain") is not None else ""
            loop = f"  {info['loop']}" if info.get("loop") else ""
            print(
                f"{src.name} -> {info['dst']}  {info['bytes']} bytes  speed {info['speed']}  "
                f"{info['fm_patches']} FM ins  {_sample_counts(info)}  "
                f"{info['events']} notes{vol}{loop}{extra}"
            )
        print(f"{done}/{len(files)} converted to {dest}")
        grand_done += done
        grand_total += len(files)
    if len(groups) > 1:
        print(f"{grand_done}/{grand_total} converted")
    return rc


def _report(args) -> int:
    paths, rc = _resolve(_tokens(args.input))
    if not paths:
        return rc or 1
    wrote = False
    direct_all: list[Path] = []
    for path in paths:
        direct, packs = vgm_targets(path, pack_limit=None)
        if direct:
            direct_all.extend(direct)
            continue
        if packs:
            print(f"{len(packs)} packs in {path}")
            for pack in packs:
                label = f"  {pack.chip_label}" if pack.chip_label else ""
                print(f"  {len(pack.tracks):4}  {pack.name}{label}")
            print("pass a pack folder to report every track")
            wrote = True
            continue
        print(f"ERROR: no .vgm/.vgz under {path}", file=sys.stderr)
        rc = 1
    if direct_all:
        for src, text, error in report_files(
            dedupe(direct_all), speed=args.speed, workers=args.workers,
            min_row=_min_row_for_bpm(args.max_bpm),
        ):
            if error is not None:
                print(f"ERROR {src.name}: {error}", file=sys.stderr)
                rc = 1
                continue
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
        wrote = True
    if not wrote and rc == 0:
        return 1
    return rc


def _collect_furs(tokens: list[str]) -> tuple[list[Path], int]:
    paths, rc = _resolve(tokens)
    if not paths:
        return [], rc or 1
    found: list[Path] = []
    for path in paths:
        hits = fur_targets(path)
        if not hits:
            print(f"ERROR: no .fur under {path}", file=sys.stderr)
            rc = 1
            continue
        found.extend(hits)
    return dedupe(found), rc


def _edit(args) -> int:
    files, rc = _collect_furs(_tokens(args.input))
    if not files:
        return rc or 1
    if (args.factor is None) == (args.value is None) and not args.list:
        print("ERROR: give --factor F or --value V (or --list)", file=sys.stderr)
        return 2
    if len(files) > 1:
        print(f"{len(files)} files")
    for fur in files:
        try:
            chips = read_chips_file(fur)
            targets = _parse_targets(args.chip, chips) if not args.list else []
            if not args.list:
                set_chip_volume_file(
                    fur, factor=args.factor, value=args.value, targets=targets
                )
                chips = read_chips_file(fur)
            for chip in chips:
                mark = "*" if chip.index in targets else " "
                print(f"{mark} {fur.name}  {chip.index + 1}. {chip.name}  {chip.volume:.3f}")
        except (FurEditError, OSError, ValueError) as exc:
            print(f"ERROR {fur.name}: {exc}", file=sys.stderr)
            rc = 1
    return rc


def _compare(args) -> int:
    from vgm2151fur.diag import compare_fur_to_source

    source = Path(clean_text(str(args.source)))
    fur = Path(clean_text(str(args.fur)))
    if not source.is_file():
        print(f"ERROR: {source} not found", file=sys.stderr)
        return 1
    if not fur.is_file():
        print(f"ERROR: {fur} not found", file=sys.stderr)
        return 1
    try:
        runs, _exported = compare_fur_to_source(source, fur, args.channel)
    except FurnaceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not runs:
        print(f"channel {args.channel}: no sustained divergence above 0.5 st")
        return 0
    print(f"channel {args.channel}: {len(runs)} run(s) of divergence")
    for run in runs:
        print(" ", run)
    return 1


def _render(args) -> int:
    files, rc = _collect_furs(_tokens(args.input))
    if not files:
        return rc or 1
    if args.out and len(files) > 1:
        print("ERROR: -o takes a single .fur input", file=sys.stderr)
        return 2
    for fur in files:
        out = Path(clean_text(args.out)) if args.out else fur.with_suffix(".wav")
        try:
            render_wav(fur, out, loops=args.loops)
        except FurnaceError as exc:
            print(f"ERROR {fur.name}: {exc}", file=sys.stderr)
            rc = 1
            continue
        print(f"{fur.name} -> {out}  ({out.stat().st_size} bytes)")
    return rc


def _add_convert_flags(parser: argparse.ArgumentParser, *, bare: bool) -> None:
    parser.add_argument("-o", "--out", help="output folder (default: <input>/fur)")
    parser.add_argument("--speed", type=int, default=None, help="ticks per row (default: auto)")
    parser.add_argument(
        "--max-bpm", type=float, default=None,
        help="cap the tracker BPM (Furnace shows 15 x rows/s) by using the "
             "coarsest row grid under the cap; halving it halves the row rate. "
             "Slides change rate at most once per row, so a lower cap trades "
             "slide detail for readability",
    )
    parser.add_argument("--workers", type=int, default=None, help=_WORKERS_HELP)
    parser.add_argument("--melody-only", action="store_true", help="skip sample chips")
    parser.add_argument("--pcm-only", action="store_true", help="skip the YM2151")
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="skip the -1.5 dBFS volume fit (it renders each track once)",
    )
    parser.add_argument(
        "--no-loop-find",
        action="store_true",
        help="keep the VGM loop offset instead of moving it onto the bar",
    )
    if bare:
        parser.add_argument("--list", action="store_true", help="with a .fur: print chip volumes")
        parser.add_argument("--chip", default=None, help="with a .fur: chip number, name, or all")
        parser.add_argument("--factor", type=float, default=None, help="with a .fur: multiply volume")
        parser.add_argument("--value", type=float, default=None, help="with a .fur: set volume")
        parser.add_argument("--loops", type=int, default=None, help="with a .fur: render loops")
        parser.add_argument("--channel", type=int, default=None, help="compare: YM2151 channel")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vgm2151fur",
        description=(
            "Transcribe YM2151 + sample-chip (K007232, MSM6295, MSM6258, "
            "SegaPCM, C140, C352) VGMs into Furnace .fur files, and edit the result."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"vgm2151fur {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    convert = sub.add_parser("convert", help="convert VGM/VGZ files or folders")
    convert.add_argument(
        "input", nargs="+", metavar="PATH",
        help="VGM/VGZ file, pack folder, or folder of packs",
    )
    _add_convert_flags(convert, bare=False)

    report = sub.add_parser("report", help="print the analysis of a VGM without writing")
    report.add_argument("input", nargs="+", metavar="PATH", help="VGM/VGZ file or folder")
    report.add_argument("--speed", type=int, default=None)
    report.add_argument("--max-bpm", type=float, default=None)
    report.add_argument("--workers", type=int, default=None, help=_WORKERS_HELP)

    edit = sub.add_parser("edit", help="list or change .fur chip volumes")
    edit.add_argument("input", nargs="+", metavar="PATH", help=".fur file, folder, or pack folder")
    edit.add_argument("--list", action="store_true", help="print the chip table only")
    edit.add_argument("--chip", default="all", help="chip number (1-based), name, or 'all'")
    edit.add_argument("--factor", type=float, default=None, help="multiply the volume")
    edit.add_argument("--value", type=float, default=None, help="set the volume outright")

    compare = sub.add_parser("compare", help="pitch-check a .fur against its source VGM")
    compare.add_argument("source", help="source .vgm/.vgz")
    compare.add_argument("fur", help="converted .fur")
    compare.add_argument("--channel", type=int, default=0, help="YM2151 channel (default 0)")

    render = sub.add_parser("render", help="render .fur files to WAV with Furnace")
    render.add_argument("input", nargs="+", metavar="PATH", help=".fur file, folder, or pack folder")
    render.add_argument("-o", "--out", help="output WAV (single input only)")
    render.add_argument("--loops", type=int, default=1, help="song loops to render")

    sub.add_parser("menu", help="interactive menu")
    return ap


def _bare_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vgm2151fur",
        add_help=False,
        description="Convert VGMs, or edit and render .fur files, from the paths given.",
    )
    parser.exit = _parser_exit
    parser.add_argument("paths", nargs="+", metavar="PATH")
    _add_convert_flags(parser, bare=True)
    return parser


def _compare_paths(paths: list[Path], channel: int) -> int:
    source = fur = None
    for path in paths:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in VGM_SUFFIXES and source is None:
            source = path
        elif suffix == ".fur" and fur is None:
            fur = path
    if source is None or fur is None:
        print("ERROR: compare needs one .vgm/.vgz and one .fur", file=sys.stderr)
        return 2
    return _compare(argparse.Namespace(source=str(source), fur=str(fur), channel=channel))


def _peek(tokens: list[str]) -> list[Path]:
    """Existing paths, without printing. Callers report missing paths themselves."""
    found: list[Path] = []
    for token in tokens:
        found.extend(path for path in expand_token(token) if path.exists())
    return dedupe(found)


def _has_vgm(path: Path) -> bool:
    direct, packs = vgm_targets(path)
    return bool(direct or packs)


def _run_paths(argv: list[str]) -> int:
    try:
        ns = _bare_parser().parse_args(argv)
    except _Usage as exc:
        return exc.code
    wants_edit = bool(ns.list or ns.factor is not None or ns.value is not None)
    wants_render = ns.loops is not None
    wants_compare = ns.channel is not None
    existing = _peek(ns.paths)
    has_vgm = any(_has_vgm(path) for path in existing)
    has_fur = any(fur_targets(path) for path in existing)
    if wants_compare:
        return _compare_paths(existing, ns.channel)
    ns.input = ns.paths
    if wants_edit or (has_fur and not has_vgm and not wants_render):
        if ns.chip is None:
            ns.chip = "all"
        if ns.factor is None and ns.value is None:
            ns.list = True
        return _edit(ns)
    if wants_render:
        if ns.loops is None:
            ns.loops = 1
        return _render(ns)
    return _convert(ns)


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if not args_in or args_in == ["menu"]:
        if args_in == ["menu"] or (sys.stdin.isatty() and sys.stdout.isatty()):
            from vgm2151fur.menu import run_menu

            return run_menu()
        build_parser().print_help()
        return 0
    head = args_in[0]
    if head not in COMMANDS and not head.startswith("-"):
        return _run_paths(args_in)
    args = build_parser().parse_args(args_in)
    if args.cmd in (None, "menu"):
        if args.cmd == "menu" or (sys.stdin.isatty() and sys.stdout.isatty()):
            from vgm2151fur.menu import run_menu

            return run_menu()
        build_parser().print_help()
        return 0
    if args.cmd == "convert":
        return _convert(args)
    if args.cmd == "report":
        return _report(args)
    if args.cmd == "edit":
        return _edit(args)
    if args.cmd == "compare":
        return _compare(args)
    if args.cmd == "render":
        return _render(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
