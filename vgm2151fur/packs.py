"""Find VGM packs and read their playlists.

A pack is a folder with .vgm/.vgz files directly inside. Many packs ship an
.m3u playlist that fixes the track order, sometimes different from the
alphabetical order. The browser lists packs, and a pack view lists its tracks
in playlist order when there is one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from vgm2151fur.vgm import chips_in_head, head_bytes

VGM_SUFFIXES = (".vgm", ".vgz")

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "output", "third_party", "soundcheck",
    "fur", "docs", "tests",
}

# Display order and names for the header-visible chips.
CHIP_LABELS = (
    ("ym2151", "YM2151"),
    ("k007232", "K007232"),
    ("segapcm", "SegaPCM"),
    ("c140", "C140"),
    ("c352", "C352"),
    ("c219", "C219 (unsupported)"),
    ("msm6295", "MSM6295"),
    ("msm6258", "MSM6258"),
)


@dataclass
class Pack:
    path: Path
    tracks: list[Path]
    playlist: Path | None = None
    chips: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def chip_label(self) -> str:
        names = [label for kind, label in CHIP_LABELS if kind in self.chips]
        return ", ".join(names)


def find_packs(root: Path, *, max_depth: int = 5, limit: int | None = 80) -> list[Pack]:
    """Folders under `root` that hold .vgm/.vgz files directly.

    `limit` caps the list so a scan from a huge root still returns. None
    walks the whole tree.
    """
    root = Path(root)
    packs: list[Pack] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= max_depth:
            dirnames.clear()
        tracks = sorted(
            (Path(dirpath) / f for f in filenames if f.lower().endswith(VGM_SUFFIXES)),
            key=lambda p: p.name.lower(),
        )
        if not tracks:
            continue
        playlists = sorted(Path(dirpath).glob("*.m3u"))
        packs.append(
            Pack(
                path=Path(dirpath),
                tracks=tracks,
                playlist=playlists[0] if playlists else None,
            )
        )
        if limit is not None and len(packs) >= limit:
            break
    return packs


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def read_playlist(path: Path) -> list[Path]:
    """Existing tracks named by an .m3u, in playlist order.

    A line that points into a missing subdirectory still matches a file of
    the same name sitting in the pack folder.
    """
    out: list[Path] = []
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    for line in text.splitlines():
        line = line.strip().strip('"').strip("'")
        if not line or line.startswith("#"):
            continue
        folder = path.parent
        candidate = folder / line
        if not candidate.is_file():
            candidate = folder / Path(line).name
        if candidate.is_file():
            out.append(candidate)
    return out


def pack_tracks(pack: Pack) -> tuple[list[Path], bool]:
    """(tracks, from_playlist). Playlist order, then files the playlist skipped."""
    if pack.playlist is not None:
        listed = read_playlist(pack.playlist)
        if listed:
            seen = {_path_key(p) for p in listed}
            rest = [p for p in pack.tracks if _path_key(p) not in seen]
            return listed + rest, True
    return pack.tracks, False


def scan_pack_chips(pack: Pack, *, sample: int = 6) -> set[str]:
    """Chip kinds seen in the headers of up to `sample` tracks."""
    kinds: set[str] = set()
    for track in pack.tracks[:sample]:
        try:
            kinds |= chips_in_head(head_bytes(track, 0x200))
        except OSError:
            continue
    return kinds


def parse_selection(spec: str, count: int) -> list[int]:
    """'all', '3', '2-6', '1,3-5' -> sorted 0-based track indexes.

    Raises ValueError when a token is not a track or lands outside 1..count.
    """
    spec = spec.strip().lower()
    if spec in ("", "all", "*"):
        return list(range(count))
    chosen: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            low, _sep, high = token.partition("-")
            if not (low.strip().isdigit() and high.strip().isdigit()):
                raise ValueError(f"cannot read {token!r} as a track range")
            first, last = int(low), int(high)
            if not 1 <= first <= last <= count:
                raise ValueError(f"range {token!r} is outside 1..{count}")
            chosen.update(range(first - 1, last))
        elif token.isdigit():
            value = int(token)
            if not 1 <= value <= count:
                raise ValueError(f"track {value} is outside 1..{count}")
            chosen.add(value - 1)
        else:
            raise ValueError(f"cannot read {token!r} as a track number")
    return sorted(chosen)
