"""Turn a pasted path into files the commands can open.

Windows hands us quotes (Explorer's Copy as path, a drag onto the console),
a leading PowerShell ``&``, ``file://`` URIs, and globs that cmd.exe does not
expand. One cleaner feeds the menu and the command line.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from vgm2151fur.convert import VGM_SUFFIXES, iter_vgms
from vgm2151fur.packs import Pack, find_packs, pack_tracks

FUR_SUFFIX = ".fur"


def clean_text(text: str) -> str:
    """Strip the wrapping people paste around a path. The path itself stays."""
    s = text.strip().strip("\ufeff")
    if s.startswith("&"):
        s = s[1:].strip()
    changed = True
    while changed and len(s) >= 2:
        changed = False
        if s[0] == s[-1] and s[0] in "\"'":
            s = s[1:-1].strip()
            changed = True
    if len(s) >= 2 and s[-1] in "\"'" and s[0] not in "\"'":
        # A trailing backslash ate the closing quote on the way in: C:\dir"
        s = s[:-1].rstrip()
    if len(s) >= 2 and s[0] == "<" and s[-1] == ">":
        s = s[1:-1].strip()
    if s.lower().startswith("file:"):
        path = unquote(urlparse(s).path)
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        s = path
    return os.path.expanduser(s)


def expand_token(text: str) -> list[Path]:
    """One pasted token. A glob with no matches is an empty list.

    A path that does not exist is returned as a single Path so the caller
    can say it was not found. An existing path is returned as-is, not walked.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return []
    path = Path(cleaned)
    if path.exists():
        return [path]
    if any(ch in cleaned for ch in "*?["):
        return [Path(hit) for hit in glob.glob(cleaned, recursive=True)]
    return [path]


def expand_tokens(texts: list[str]) -> list[Path]:
    found: list[Path] = []
    for text in texts:
        found.extend(expand_token(text))
    return dedupe(found)


def dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = _key(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def ordered_vgms(folder: Path) -> list[Path]:
    """VGMs directly in `folder`, playlist order first when an .m3u names them."""
    direct = iter_vgms(folder)
    if not direct:
        return []
    playlists = sorted(p for p in folder.glob("*.m3u") if p.is_file())
    if not playlists:
        return direct
    tracks, _from_playlist = pack_tracks(
        Pack(path=folder, tracks=direct, playlist=playlists[0])
    )
    return dedupe(tracks)


def folder_pack(folder: Path) -> Pack | None:
    """A pack for this folder, or None when it holds no VGMs of its own."""
    tracks = iter_vgms(folder)
    if not tracks:
        return None
    playlists = sorted(p for p in folder.glob("*.m3u") if p.is_file())
    return Pack(
        path=folder,
        tracks=tracks,
        playlist=playlists[0] if playlists else None,
    )


def vgm_targets(path: Path, *, pack_limit: int | None = None) -> tuple[list[Path], list[Pack]]:
    """`(files, nested packs)`.

    A folder that itself holds VGMs is `files` (playlist order when there is
    an .m3u) and is not also returned as a pack. A folder of packs is
    `nested packs` and no files. `pack_limit` is passed to the nested scan;
    None walks the whole tree.
    """
    if not path.exists():
        return [], []
    if path.is_file():
        return ([path] if path.suffix.lower() in VGM_SUFFIXES else []), []
    direct = ordered_vgms(path)
    if direct:
        return direct, []
    nested = [pack for pack in find_packs(path, limit=pack_limit) if not _same(pack.path, path)]
    return [], nested


def fur_targets(path: Path) -> list[Path]:
    """A `.fur` file, the `.fur` files in a folder, or that folder's `fur/` child.

    When the folder itself has none, one level of subfolders is included, so
    a pack folder works after a convert and so does a folder of those packs.
    """
    if not path.exists():
        return []
    if path.is_file():
        return [path] if path.suffix.lower() == FUR_SUFFIX else []
    direct = dedupe(sorted(path.glob("*.fur"), key=lambda p: p.name.lower()))
    if direct:
        return direct
    child = path / "fur"
    if child.is_dir():
        nested = dedupe(sorted(child.glob("*.fur"), key=lambda p: p.name.lower()))
        if nested:
            return nested
    found: list[Path] = []
    try:
        children = sorted(
            (p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []
    for sub in children:
        if sub.name in {"__pycache__", "fur"}:
            continue
        found.extend(sub.glob("*.fur"))
        fur = sub / "fur"
        if fur.is_dir():
            found.extend(fur.glob("*.fur"))
    return dedupe(sorted(found, key=lambda p: str(p).lower()))


def _key(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve().samefile(b.resolve())
    except OSError:
        return _key(a).rstrip("\\/") == _key(b).rstrip("\\/")
