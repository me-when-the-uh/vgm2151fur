"""Line menu. Numbered commands, and a pasted path does the obvious thing.

Not a full-screen interface. `run_menu(read, out, state_path)` is the testable
entry: every action calls the same functions the command line uses.

A path typed at the prompt can be quoted (Explorer's Copy as path), a file,
a pack folder, or a folder of packs. A file converts. A pack opens. A folder
of packs lists them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from vgm2151fur import __version__
from vgm2151fur.convert import VGM_SUFFIXES, convert_folder, report_files
from vgm2151fur.edit import FurEditError, read_chips_file, set_chip_volume_file
from vgm2151fur.furnace import FurnaceError, render_wav, restore_console
from vgm2151fur.packs import Pack, find_packs, pack_tracks, parse_selection, scan_pack_chips
from vgm2151fur.paths import (
    clean_text,
    dedupe,
    expand_token,
    folder_pack,
    fur_targets,
    vgm_targets,
)

FUR_FOLDER = "fur"
# A scan from a big collection still returns. The line under the list says so
# when this many packs came back.
PACK_SCAN_LIMIT = 400

_TONE = None  # set in run_menu; plain text when stdout is not a console


class _Tone:
    def __init__(self, on: bool):
        self.on = on

    def _wrap(self, code: str, text: str) -> str:
        if not self.on:
            return text
        return f"\033[{code}m{text}\033[0m"

    def title(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bad(self, text: str) -> str:
        return self._wrap("31", text)


def _vt_on() -> bool:
    """True when the console will actually render ANSI color."""
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def _banner(state: dict) -> str:
    lines = [
        _TONE.title(f"vgm2151fur {__version__}"),
        "  1  packs      scan a folder, then convert, volumes, render",
        "  2  convert    a file, a folder, or a glob",
        "  3  volumes    a .fur file or a folder of them",
        "  4  compare    a .fur against its source VGM",
        "  5  render     .fur to WAV",
        "  6  report     analysis, nothing written",
        _TONE.dim("  paste a path, or a number.   m menu    q quit"),
    ]
    last = state.get("browse_root")
    if last:
        lines.insert(1, f"  last folder  {last}")
    return "\n".join(lines) + "\n"


def run_menu(read=input, out=print, *, state_path: Path | None = None) -> int:
    global _TONE
    _TONE = _Tone(_vt_on())
    path = Path(state_path) if state_path else Path.home() / ".vgm2151fur.json"
    state = _load_state(path)
    out(_banner(state))
    while True:
        try:
            restore_console()
            raw = read("> ").strip()
        except EOFError:
            return 0
        if not raw:
            continue
        key = raw.lower()
        if key in ("q", "quit", "exit"):
            return 0
        if key in ("m", "menu", "?", "help"):
            out(_banner(state))
            continue
        try:
            result = _dispatch(read, out, state, path, raw, key)
        except KeyboardInterrupt:
            out("")
            continue
        except (OSError, ValueError) as exc:
            out(f"  ERROR: {exc}")
            continue
        if result == "quit":
            return 0
        if result == "menu":
            out(_banner(state))


def _dispatch(read, out, state, state_path, raw: str, key: str):
    if key == "1":
        return cmd_packs(read, out, state, state_path)
    if key == "2":
        return cmd_convert_file(read, out, state, state_path)
    if key == "3":
        cmd_edit(read, out)
        return None
    if key == "4":
        cmd_compare(read, out)
        return None
    if key == "5":
        cmd_render(read, out)
        return None
    if key == "6":
        cmd_report(read, out)
        return None
    return _open_typed_path(read, out, state, state_path, raw)


# ---------------------------------------------------------------- state

def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _remember(state, state_path, folder: Path) -> None:
    state["browse_root"] = str(folder)
    _save_state(state_path, state)


# ---------------------------------------------------------------- prompts

def _ask(read, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    restore_console()
    try:
        answer = read(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _ask_existing(read, out, prompt: str) -> list[Path] | None:
    """Paths that exist. Blank cancels. Quotes and globs are accepted."""
    while True:
        raw = _ask(read, prompt)
        if not raw:
            return None
        matches = expand_token(raw)
        existing = [p for p in matches if p.exists()]
        if existing:
            return existing
        shown = clean_text(raw)
        out(f"  no match for {shown}" if not matches else f"  not found: {shown}")
        out("  blank cancels. Quotes around a pasted path are fine.")


def _ask_file(read, out, prompt: str) -> Path | None:
    while True:
        found = _ask_existing(read, out, prompt)
        if found is None:
            return None
        files = [p for p in found if p.is_file()]
        if len(files) == 1:
            return files[0]
        if len(found) == 1 and found[0].is_dir():
            out("  that is a folder. Give the file.")
            continue
        out(f"  {len(found)} matches. Give one file.")


def _coerce_folder(out, raw: str) -> Path | None:
    """A folder, the parent of a file, or the common parent of a glob."""
    matches = [p for p in expand_token(raw) if p.exists()]
    if not matches:
        shown = clean_text(raw)
        out(f"  no match for {shown}" if not expand_token(raw) else f"  not found: {shown}")
        return None
    if len(matches) == 1:
        path = matches[0]
        return path.parent if path.is_file() else path
    parents = dedupe([p.parent if p.is_file() else p for p in matches])
    if len(parents) == 1:
        return parents[0]
    out(f"  {len(matches)} matches. Give one folder.")
    return None


def _looks_like_path(text: str) -> bool:
    cleaned = clean_text(text)
    if not cleaned:
        return False
    low = cleaned.lower()
    if low.endswith((".vgm", ".vgz", ".fur", ".wav", ".m3u")):
        return True
    return any(ch in cleaned for ch in "\\/*?[")


def _ask_factor(read) -> tuple[float | None, float | None]:
    raw = _ask(read, "volume factor (1.2 = +20%, 0.75 = -25%, =0.9 = set)")
    if not raw:
        return None, None
    if raw.startswith("="):
        return None, float(raw[1:])
    return float(raw), None


def _parse_targets(spec: str, chips) -> list[int]:
    targets: list[int] = []
    for token in spec.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token in ("all", "*"):
            targets += range(len(chips))
            continue
        if token.isdigit():
            idx = int(token) - 1
            if not 0 <= idx < len(chips):
                raise ValueError(f"chip {token} is outside 1..{len(chips)}")
            targets.append(idx)
        else:
            match = [c.index for c in chips if token in c.name.lower()]
            if not match:
                raise ValueError(f"no chip matching {token!r}")
            targets += match
    return sorted(set(targets))


def _print_chips(out, chips) -> None:
    for chip in chips:
        out(f"   {chip.index + 1}. {chip.name:<8} volume {chip.volume:.3f}")


def _apply_volume(files: list[Path], names: list[str], factor, value, out) -> int:
    """Apply a chip volume change by chip NAME, so indexes cannot drift."""
    changed = 0
    for fur in files:
        try:
            chips = read_chips_file(fur)
            targets = [c.index for c in chips if c.name in names]
            if not targets:
                out(f"   {fur.name}: no {', '.join(names)} here")
                continue
            set_chip_volume_file(fur, factor=factor, value=value, targets=targets)
            after = [c for c in read_chips_file(fur) if c.index in targets]
            parts = ", ".join(f"{c.name} {c.volume:.3f}" for c in after)
            out(f"   {fur.name}: {parts}")
            changed += 1
        except (FurEditError, OSError, ValueError) as exc:
            out(f"   ERROR {fur.name}: {exc}")
    return changed


def _ensure_chips(packs: list[Pack]) -> None:
    for pack in packs:
        if not pack.chips:
            pack.chips = scan_pack_chips(pack)


# ---------------------------------------------------------------- packs

def cmd_packs(read, out, state: dict, state_path: Path):
    root_raw = _ask(read, "folder to scan", str(state.get("browse_root") or Path.cwd()))
    if not root_raw:
        return None
    root = _coerce_folder(out, root_raw)
    if root is None:
        return None
    return _open_dir(read, out, state, state_path, root, remember=True)


def _open_dir(read, out, state, state_path, root: Path, *, remember: bool):
    pack = folder_pack(root)
    if pack is not None:
        _ensure_chips([pack])
        if remember:
            _remember(state, state_path, root)
        result = _pack_view(read, out, pack, state, state_path)
        return result if result in ("menu", "quit") else None
    packs = find_packs(root, limit=PACK_SCAN_LIMIT)
    if packs:
        _ensure_chips(packs)
        if remember:
            _remember(state, state_path, root)
        return _browse_packs(
            read, out, packs, state, state_path, root,
            truncated=len(packs) >= PACK_SCAN_LIMIT,
        )
    furs = fur_targets(root)
    if furs:
        _show_furs(out, furs)
        return None
    out(f"  no packs under {root}")
    out("  a pack is a folder with .vgm or .vgz files in it")
    return None


def _browse_packs(read, out, packs, state, state_path, root: Path, *, truncated: bool):
    def show() -> None:
        out(f"{len(packs)} packs in {root}")
        for i, pack in enumerate(packs, 1):
            chips = pack.chip_label or "no supported chip"
            playlist = ", playlist" if pack.playlist else ""
            out(f"  {i:3}  {len(pack.tracks):4} tracks  {pack.name}  ({chips}{playlist})")
        if truncated:
            out("  scan stopped here. Point at a narrower folder for the rest.")

    show()
    while True:
        answer = _ask(read, "pack number (m = list again, blank = main menu)")
        if not answer:
            return None
        if answer.lower() in ("q", "quit", "exit"):
            return "quit"
        if answer.lower() in ("m", "menu"):
            show()
            continue
        if not answer.isdigit() or not 1 <= int(answer) <= len(packs):
            out(f"  pick 1..{len(packs)}")
            continue
        pack = packs[int(answer) - 1]
        result = _pack_view(read, out, pack, state, state_path)
        if result in ("menu", "quit"):
            return result
        show()


def _fur_dir(pack: Pack, state: dict) -> Path:
    """Where this pack's .fur files went: remembered per pack."""
    remembered = state.get("out_dirs", {}).get(str(pack.path))
    return Path(remembered) if remembered else pack.path / FUR_FOLDER


def _pack_view(read, out, pack: Pack, state: dict, state_path: Path) -> str:
    tracks, from_playlist = pack_tracks(pack)
    while True:
        fur_dir = _fur_dir(pack, state)
        chips = f", {pack.chip_label}" if pack.chip_label else ""
        order = "playlist order" if from_playlist else "name order"
        out(f"pack {pack.path}  ({len(tracks)} tracks{chips}, {order})")
        out(f"output  {fur_dir}")
        for i, track in enumerate(tracks, 1):
            mark = "" if (fur_dir / (track.stem + ".fur")).is_file() else "  -"
            out(f"  {i:3}. {track.stem}{mark}")
        answer = _ask(
            read, "c convert, e volumes, r render, b back (a '-' means no .fur yet)", "b"
        ).lower()
        if answer in ("", "b", "back"):
            return "back"
        if answer in ("q", "quit", "exit"):
            return "quit"
        if answer in ("m", "menu"):
            return "menu"
        if answer == "c":
            _pack_convert(read, out, pack, tracks, fur_dir, state, state_path)
        elif answer == "e":
            _pack_volumes(read, out, tracks, fur_dir)
        elif answer == "r":
            _pack_render(read, out, tracks, fur_dir)
        else:
            out("  c, e, r or b")


def _run_convert(out, files: list[Path], dest: Path, speed: int | None) -> int:
    done = rc = 0
    for src, info, error in convert_folder(
        files, dest, speed=speed, normalize=True, log=lambda message: out(f"  {message}"),
    ):
        if error is not None:
            out(f"  ERROR {src.name}: {error}")
            rc = 1
            continue
        done += 1
        vol = f"  vol {info['gain']:.2f}x" if info.get("gain") is not None else ""
        loop = f"  {info['loop']}" if info.get("loop") else ""
        out(
            f"  {src.name} -> {Path(info['dst']).name}  {info['bytes']} bytes  "
            f"speed {info['speed']}  {info['events']} notes{vol}{loop}"
        )
        shown = 0
        for warning in info["warnings"]:
            if warning == info.get("loop"):
                continue
            out(f"     warning: {warning}")
            shown += 1
            if shown == 2:
                break
    out(f"  {done}/{len(files)} converted to {dest}")
    return rc


def _pack_convert(read, out, pack, tracks, default_dir: Path, state, state_path) -> None:
    spec = _ask(read, "tracks (all, 3, 2-6, 1,4)", "all")
    try:
        picks = parse_selection(spec, len(tracks))
    except ValueError as exc:
        out(f"  {exc}")
        return
    dst = Path(_ask(read, "output folder", str(default_dir)))
    _run_convert(out, [tracks[i] for i in picks], dst, None)
    state.setdefault("out_dirs", {})[str(pack.path)] = str(dst)
    state["pack"] = str(pack.path)
    _save_state(state_path, state)


def _pack_volumes(read, out, tracks, fur_dir: Path) -> None:
    spec = _ask(read, "tracks to adjust (all, 3, 1,4)", "all")
    try:
        picks = parse_selection(spec, len(tracks))
    except ValueError as exc:
        out(f"  {exc}")
        return
    furs = [fur_dir / (tracks[i].stem + ".fur") for i in picks]
    missing = [f for f in furs if not f.is_file()]
    if missing:
        out(f"  convert these first (c): {', '.join(m.name for m in missing[:4])}")
        return
    try:
        chips = read_chips_file(furs[0])
        _print_chips(out, chips)
        targets = _parse_targets(_ask(read, "chip number, name, or 'all'", "all"), chips)
        names = [c.name for c in chips if c.index in targets]
        factor, value = _ask_factor(read)
    except (FurEditError, OSError, ValueError) as exc:
        out(f"  {exc}")
        return
    if factor is None and value is None:
        out("  nothing changed")
        return
    if _apply_volume(furs, names, factor, value, out):
        answer = _ask(read, "render one to WAV now (track number, blank = no)")
        if answer:
            _pack_render(read, out, tracks, fur_dir, spec=answer)


def _pack_render(read, out, tracks, fur_dir: Path, *, spec: str | None = None) -> None:
    spec = spec if spec is not None else _ask(read, "track number", "1")
    try:
        picks = parse_selection(spec, len(tracks))
    except ValueError as exc:
        out(f"  {exc}")
        return
    loops = _ask(read, "loops", "1")
    try:
        nloops = int(loops or 1)
    except ValueError:
        out("  loops should be a number")
        return
    for i in picks:
        fur = fur_dir / (tracks[i].stem + ".fur")
        if not fur.is_file():
            out(f"  no .fur at {fur.name}, convert it first")
            continue
        out_path = fur.with_suffix(".wav")
        try:
            render_wav(fur, out_path, loops=nloops)
        except FurnaceError as exc:
            out(f"  {exc}")
            return
        out(f"  {out_path.name}  ({out_path.stat().st_size} bytes)")


# ---------------------------------------------------------------- direct commands

def _convert_now(out, files: list[Path], dest: Path, speed: int | None) -> int:
    return _run_convert(out, files, dest, speed)


def cmd_convert_file(read, out, state, state_path):
    raw = _ask(read, "VGM/VGZ file, folder, or glob")
    if not raw:
        return None
    matches = expand_token(raw)
    existing = [p for p in matches if p.exists()]
    if not existing:
        shown = clean_text(raw)
        out(f"  no match for {shown}" if not matches else f"  not found: {shown}")
        return None
    dirs = [p for p in existing if p.is_dir()]
    files = [p for p in existing if p.is_file()]
    if len(dirs) == 1 and not files:
        return _open_dir(read, out, state, state_path, dirs[0], remember=True)
    if dirs and not files:
        out(f"  {len(dirs)} folders. Give one.")
        for folder in dirs[:20]:
            out(f"  {folder}")
        return None
    vgms = [p for p in files if p.suffix.lower() in VGM_SUFFIXES]
    for folder in dirs:
        vgms.extend(vgm_targets(folder)[0])
    vgms = dedupe(vgms)
    if not vgms:
        out("  no .vgm/.vgz in that path")
        return None
    default_out = str(vgms[0].parent / FUR_FOLDER)
    dst = Path(_ask(read, "output folder", default_out))
    speed = _ask(read, "ticks per row (blank = auto)")
    return _convert_now(out, vgms, dst, int(speed) if speed.isdigit() else None)


def cmd_edit(read, out) -> None:
    while True:
        found = _ask_existing(read, out, ".fur file or folder")
        if found is None:
            return
        files = dedupe([fur for path in found for fur in fur_targets(path)])
        if not files:
            out("  no .fur under that path")
            return
        if len(files) > 1:
            out(f"  {len(files)} files here. The same change goes to each")
        try:
            chips = read_chips_file(files[0])
            _print_chips(out, chips)
            targets = _parse_targets(_ask(read, "chip number, name, or 'all'", "all"), chips)
            names = [c.name for c in chips if c.index in targets]
            factor, value = _ask_factor(read)
        except (FurEditError, OSError, ValueError) as exc:
            out(f"  {exc}")
            return
        if factor is None and value is None:
            out("  nothing changed")
        else:
            _apply_volume(files, names, factor, value, out)
        again = _ask(read, "adjust another file? (y/N)", "n")
        if again.lower() not in ("y", "yes"):
            return


def cmd_compare(read, out) -> None:
    from vgm2151fur.diag import compare_fur_to_source

    src = _ask_file(read, out, "source VGM/VGZ")
    if src is None:
        return
    fur = _ask_file(read, out, ".fur file")
    if fur is None:
        return
    ch = _ask(read, "YM2151 channel", "0")
    try:
        channel = int(ch or 0)
    except ValueError:
        out("  channel should be a number")
        return
    try:
        runs, exported = compare_fur_to_source(src, fur, channel)
    except FurnaceError as exc:
        out(f"  {exc}")
        return
    out(f"  export: {exported.name}")
    if not runs:
        out("  no sustained divergence above 0.5 st")
        return
    out(f"  {len(runs)} divergence run(s):")
    for run in runs:
        out(f"   {run}")


def cmd_render(read, out) -> None:
    found = _ask_existing(read, out, ".fur file or folder")
    if found is None:
        return
    files = dedupe([fur for path in found for fur in fur_targets(path)])
    if not files:
        out("  no .fur under that path")
        return
    one = ""
    if len(files) == 1:
        one = _ask(read, "output WAV", str(files[0].with_suffix(".wav")))
    else:
        out(f"  {len(files)} files, WAV written next to each")
    loops = _ask(read, "loops", "1")
    try:
        nloops = int(loops or 1)
    except ValueError:
        out("  loops should be a number")
        return
    for fur in files:
        dest = Path(one) if one else fur.with_suffix(".wav")
        try:
            render_wav(fur, dest, loops=nloops)
        except FurnaceError as exc:
            out(f"  {exc}")
            return
        out(f"  {fur.name} -> {dest}")


def cmd_report(read, out) -> None:
    found = _ask_existing(read, out, "VGM/VGZ file or folder")
    if found is None:
        return
    direct_all: list[Path] = []
    for path in found:
        direct, packs = vgm_targets(path)
        if direct:
            direct_all.extend(direct)
            continue
        if packs:
            out(f"{len(packs)} packs in {path}")
            for pack in packs:
                out(f"  {len(pack.tracks):4}  {pack.name}")
            out("  pass a pack folder to report every track")
            continue
        out(f"  no .vgm/.vgz under {path}")
    if not direct_all:
        return
    for src, text, error in report_files(
        dedupe(direct_all), log=lambda message: out(f"  {message}"),
    ):
        if error is not None:
            out(f"  ERROR {src.name}: {error}")
        else:
            out(text)


def _show_furs(out, files: list[Path]) -> None:
    out(f"{len(files)} .fur")
    for fur in files[:12]:
        try:
            chips = read_chips_file(fur)
        except (FurEditError, OSError, ValueError) as exc:
            out(f"  ERROR {fur.name}: {exc}")
            continue
        joined = ", ".join(f"{c.name} {c.volume:.3f}" for c in chips)
        out(f"  {fur.name}  {joined}")
    if len(files) > 12:
        out(f"  ... {len(files) - 12} more")
    out("  3 volumes, 5 render")


def _open_typed_path(read, out, state, state_path, raw: str):
    matches = expand_token(raw)
    existing = [p for p in matches if p.exists()]
    if not existing:
        shown = clean_text(raw)
        if _looks_like_path(raw):
            out(f"  no match for {shown}" if not matches else f"  not found: {shown}")
        else:
            out("  unknown command. 'm' lists them, or paste a path")
        return None
    dirs = [p for p in existing if p.is_dir()]
    files = [p for p in existing if p.is_file()]
    if len(existing) == 1 and existing[0].is_dir():
        return _open_dir(read, out, state, state_path, existing[0], remember=True)
    vgms = [p for p in files if p.suffix.lower() in VGM_SUFFIXES]
    furs = [p for p in files if p.suffix.lower() == ".fur"]
    for folder in dirs:
        direct, packs = vgm_targets(folder)
        if direct:
            vgms.extend(direct)
        elif packs:
            out(f"  {folder.name}: {len(packs)} packs. Paste that folder on its own.")
    if vgms and not furs:
        _convert_now(out, dedupe(vgms), vgms[0].parent / FUR_FOLDER, None)
        return None
    if furs and not vgms:
        _show_furs(out, dedupe(furs))
        return None
    if vgms and furs:
        out("  mixed VGM and .fur files. 2 converts, 3 edits volumes.")
        return None
    out("  nothing to convert or edit there")
    return None


if __name__ == "__main__":
    sys.exit(run_menu())
