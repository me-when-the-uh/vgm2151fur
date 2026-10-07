"""Locate and run the reference VGM renderer (vgm2wav-mute).

The renderer ships with the VGM tools and lives next to the project folder,
not inside it. VGM2151FUR_VGM2WAV overrides the search.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class RendererError(RuntimeError):
    """vgm2wav-mute is missing or failed."""


def find_vgm2wav() -> Path | None:
    env = os.environ.get("VGM2151FUR_VGM2WAV")
    here = Path(__file__).resolve().parent
    candidates = [
        Path(env) if env else None,
        here.parent.parent / "vgm2wav-mute.exe",
        here.parent.parent.parent / "vgm2wav-mute.exe",
        shutil.which("vgm2wav-mute"),
        shutil.which("vgm2wav"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def render(src: Path, out: Path, mute: list[str] | None = None) -> str:
    """Render `src` to `out`; returns the renderer's stdout (Dev lines included)."""
    exe = find_vgm2wav()
    if exe is None:
        raise RendererError("vgm2wav-mute.exe not found (set VGM2151FUR_VGM2WAV)")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe)]
    for spec in mute or []:
        cmd += ["--mute", spec]
    cmd += [str(src), str(out)]
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    )
    if proc.returncode != 0 or not out.is_file():
        raise RendererError(f"vgm2wav-mute failed rc={proc.returncode} for {src.name}")
    return proc.stdout
