"""How many cores a batch may use, and how quiet those processes stay.

Physical cores set the count. One core stays free. Worker processes and
Furnace renders run at the Windows priority Task Manager calls Low.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from ctypes import wintypes

# Task Manager "Low". Child processes inherit it.
LOW_PRIORITY_CLASS = 0x00000040
_RELATION_PROCESSOR_CORE = 0


def physical_cores() -> int:
    """Physical cores the process can see. Falls back to half the logical count."""
    logical = os.cpu_count() or 1
    if os.name != "nt":
        return logical
    try:
        counted = _windows_physical_cores()
    except OSError:
        counted = None
    if not counted:
        return max(1, logical // 2)
    return counted


def worker_limit() -> int:
    """1 .. N-1 physical cores. A one-core machine still gets a single worker."""
    return max(1, physical_cores() - 1)


def resolve_workers(requested: int | None, jobs: int) -> tuple[int, str]:
    """`(count, note)`. `requested` None uses the limit. Above the limit is capped."""
    cap = worker_limit()
    cores = physical_cores()
    note = f"low priority, {cores} physical cores"
    if requested is None:
        chosen = cap
    else:
        asked = max(1, int(requested))
        chosen = min(asked, cap)
        if asked > cap:
            note += f", capped at {cap}"
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

    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
    kernel.GetPriorityClass.restype = wintypes.DWORD
    kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel.SetPriorityClass.restype = wintypes.BOOL
    return kernel


def _windows_physical_cores() -> int | None:
    """Count RelationProcessorCore records. Each record is one physical core."""
    import ctypes

    class _Cache(ctypes.Structure):
        _fields_ = [
            ("Level", ctypes.c_ubyte),
            ("Associativity", ctypes.c_ubyte),
            ("LineSize", wintypes.WORD),
            ("Size", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    class _Union(ctypes.Union):
        _fields_ = [
            ("ProcessorCore", ctypes.c_ubyte),
            ("NumaNode", wintypes.DWORD),
            ("Cache", _Cache),
            ("Reserved", ctypes.c_ulonglong * 2),
        ]

    class _Info(ctypes.Structure):
        _fields_ = [
            ("ProcessorMask", ctypes.c_void_p),
            ("Relationship", wintypes.DWORD),
            ("u", _Union),
        ]

    kernel = ctypes.windll.kernel32
    query = kernel.GetLogicalProcessorInformation
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    returned = wintypes.DWORD(0)
    query(None, ctypes.byref(returned))
    if returned.value == 0:
        return None
    buf = ctypes.create_string_buffer(returned.value)
    if not query(buf, ctypes.byref(returned)):
        return None
    stride = ctypes.sizeof(_Info)
    if stride <= 0 or returned.value % stride != 0:
        return None
    count = 0
    for index in range(returned.value // stride):
        entry = _Info.from_buffer_copy(buf.raw[index * stride:(index + 1) * stride])
        if entry.Relationship == _RELATION_PROCESSOR_CORE:
            count += 1
    return count or None
