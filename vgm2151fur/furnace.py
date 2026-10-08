"""Run the Furnace console build: render .fur to WAV, export to VGM.

Furnace is not vendored in the package. The tools look for the console
build in this order: VGM2151FUR_FURNACE (an exe or a folder holding it), a
`furnace` folder next to the project (an unpacked release or a source
checkout with `build/furnace.exe`), the bundled headless build in
`third_party/furnace-console/` (the patched C352-capable console, ~4 MB),
a `third_party/furnace` drop-in, the same folders one level up, then PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from vgm2151fur.priority import creationflags

# Furnace's -console path clears line input on the console it is attached to.
# Launch it detached, with stdin closed, so the menu keeps that console.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_CONSOLE_LINE = 0x0001 | 0x0002 | 0x0004  # Ctrl+C, line input, echo


class FurnaceError(RuntimeError):
    """Furnace exited with an error or wrote nothing."""


def restore_console() -> None:
    """Turn line input, echo, and Ctrl+C back on for this console."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.restype = ctypes.c_void_p
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel.GetStdHandle(-10)
        if not handle:
            return
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        if (mode.value & _CONSOLE_LINE) != _CONSOLE_LINE:
            kernel.SetConsoleMode(handle, mode.value | _CONSOLE_LINE)
    except (AttributeError, OSError):
        return


def binary_name() -> str:
    """The console build's file name on this platform."""
    return "furnace.exe" if os.name == "nt" else "furnace"


def _candidates() -> list[Path]:
    here = Path(__file__).resolve().parents[1]
    exe = binary_name()
    out: list[Path] = []
    env = os.environ.get("VGM2151FUR_FURNACE")
    if env:
        p = Path(env)
        if p.is_dir():
            out.append(p / exe)
        else:
            out.append(p)
    out += [
        here.parent / "furnace" / exe,
        here.parent / "furnace" / "build" / exe,
        here / "third_party" / "furnace-console" / exe,
        here.parent / "third_party" / "furnace-console" / exe,
        here / "third_party" / "furnace" / exe,
        here.parent / "third_party" / "furnace" / exe,
    ]
    return out


def find_furnace() -> Path | None:
    for candidate in _candidates():
        if candidate.is_file():
            return candidate
    which = shutil.which("furnace")
    return Path(which) if which else None


def _run(fur: Path, out: Path, extra: list[str]) -> Path:
    exe = find_furnace()
    if exe is None:
        raise FurnaceError(
            f"{binary_name()} not found. Put a Furnace build next to the project as "
            "furnace/, drop it in third_party/furnace/, or point "
            "VGM2151FUR_FURNACE at it"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    flags = creationflags()
    if os.name == "nt":
        flags |= _CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [str(exe), "-console", *extra, str(out.resolve()), str(fur.resolve())],
        cwd=out.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        **({"creationflags": flags} if flags else {}),
    )
    try:
        stdout, _err = proc.communicate()
    except BaseException:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise
    finally:
        restore_console()
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        tail = "\n".join((stdout or "").splitlines()[-6:])
        raise FurnaceError(f"{fur.name}: furnace exit {proc.returncode}\n{tail}")
    return out


def render_wav(fur: Path, out: Path, *, loops: int = 1, outformat: str | None = None) -> Path:
    extra = ["-noreport", "-loops", str(loops)]
    if outformat:
        extra += ["-outformat", outformat]
    extra.append("-output")
    return _run(fur, out, extra)


def export_vgm(fur: Path, out: Path) -> Path:
    return _run(fur, out, ["-vgmout"])
