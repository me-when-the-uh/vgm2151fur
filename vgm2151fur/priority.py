"""How many cores a batch may use, and how quiet those processes stay.

Workers are pinned to the logical CPUs. One thread stays free on small
CPUs and two on bigger ones (a 6 core/12 thread desktop runs 10 workers),
with a ceiling of 16; an explicit count may go up to one below the thread
count — every thread below eight — and 31 is the safety ceiling, because
past that the renders compete with each other and the batch runs slower.
Worker processes and Furnace renders run at low priority: Idle on Windows,
nice 10 elsewhere.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager

# Task Manager "Low". Child processes inherit it.
LOW_PRIORITY_CLASS = 0x00000040
# Rendering is one thread per worker; past this the renders fight each
# other more than they parallelize, and the batch goes slower.
WORKER_CEILING = 31


def visible_cpus() -> int:
    """CPUs this process may run on, narrowed by affinity where available."""
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def default_workers(threads: int) -> int:
    """One thread stays free below six, two at six or more; ceiling 16.

    The heavy part of a worker is a single-threaded Furnace render, so the
    count follows logical CPUs; the spare threads keep the desktop
    responsive. A 6 core 12 thread machine gets 10 workers, a 22 thread one
    gets 16.
    """
    if threads < 6:
        return max(1, threads - 1)
    return max(1, min(16, threads - 2))


def worker_limit() -> int:
    """The default worker count for this machine."""
    return default_workers(visible_cpus())


def worker_cap(threads: int) -> int:
    """Most workers an explicit `--workers` may use, and the default's ceiling.

    Below eight threads every thread may run a worker (a two-thread machine
    can use both). At eight or more one thread stays free, and 31 is the
    safety ceiling: past that the single-threaded renders fight over the
    CPU and the batch goes slower, not faster.
    """
    if threads < 8:
        return max(1, threads)
    return max(1, min(threads - 1, WORKER_CEILING))


def resolve_workers(requested: int | None, jobs: int) -> tuple[int, str]:
    """`(count, note)`. `requested` None uses the default, anything else is capped."""
    threads = visible_cpus()
    note = f"low priority, {threads} threads"
    if requested is None:
        chosen = default_workers(threads)
    else:
        asked = max(1, int(requested))
        cap = worker_cap(threads)
        chosen = min(asked, cap)
        if asked > cap:
            if cap >= WORKER_CEILING:
                note += (
                    f", capped at {cap} by the safety cap; past {WORKER_CEILING} workers "
                    "the renders compete and the converter runs slower, not faster"
                )
            elif threads < 8:
                note += f", capped at {cap} (all logical threads)"
            else:
                note += f", capped at {cap} (one thread stays free)"
    if jobs > 0:
        chosen = min(chosen, jobs)
    return max(1, chosen), note


def creationflags() -> int:
    """Flags for `subprocess` so Furnace starts at low priority."""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "IDLE_PRIORITY_CLASS", LOW_PRIORITY_CLASS)


def current_priority() -> int | None:
    if os.name != "nt":
        return None
    kernel = _kernel()
    handle = kernel.GetCurrentProcess()
    value = kernel.GetPriorityClass(handle)
    return int(value) if value else None


def set_priority(value: int) -> bool:
    if os.name != "nt":
        return False
    kernel = _kernel()
    return bool(kernel.SetPriorityClass(kernel.GetCurrentProcess(), value))


def set_low_priority() -> bool:
    """Drop this process to Low. Furnace children inherit it."""
    if os.name != "nt":
        try:
            os.nice(10)
        except (AttributeError, OSError):
            return False
        return True
    return set_priority(LOW_PRIORITY_CLASS)


@contextmanager
def low_priority():
    """Run the block at low priority, then restore what the process had."""
    previous = current_priority()
    set_low_priority()
    try:
        yield
    finally:
        if previous:
            set_priority(previous)


def _kernel():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
    kernel.GetPriorityClass.restype = wintypes.DWORD
    kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel.SetPriorityClass.restype = wintypes.BOOL
    return kernel
