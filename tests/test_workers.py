"""Worker count and low priority for convert and report."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _synth import raw_vgm  # noqa: E402
from vgm2151fur.convert import convert_folder, report_files  # noqa: E402
from vgm2151fur.priority import (  # noqa: E402
    LOW_PRIORITY_CLASS,
    creationflags,
    current_priority,
    physical_cores,
    resolve_workers,
    set_low_priority,
    set_priority,
    worker_limit,
)

_PRIORITY_CLASSES = {0x20, 0x40, 0x80, 0x100, 0x4000, 0x8000}


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + previous if previous else "")
    return env


class TestWorkerCount(unittest.TestCase):
    def test_cap_is_physical_cores_minus_one(self):
        cores = physical_cores()
        logical = os.cpu_count() or 1
        self.assertGreaterEqual(cores, 1)
        self.assertLessEqual(cores, logical)
        self.assertEqual(worker_limit(), max(1, cores - 1))

    def test_resolve_workers_clamps(self):
        cap = worker_limit()
        count, note = resolve_workers(None, 100)
        self.assertEqual(count, cap)
        self.assertIn("low priority", note)
        self.assertIn("physical cores", note)
        self.assertEqual(resolve_workers(1, 100)[0], 1)
        count, note = resolve_workers(10**6, 3)
        self.assertEqual(count, min(3, cap))
        if 10**6 > cap:
            self.assertIn("capped", note)


@unittest.skipUnless(os.name == "nt", "Windows priority classes")
class TestLowPriority(unittest.TestCase):
    def test_set_and_restore(self):
        before = current_priority()
        self.assertIn(before, _PRIORITY_CLASSES)
        try:
            self.assertTrue(set_low_priority())
            self.assertEqual(current_priority(), LOW_PRIORITY_CLASS)
        finally:
            if before:
                set_priority(before)
        self.assertEqual(current_priority(), before)

    def test_spawned_process_starts_low(self):
        flags = creationflags()
        self.assertEqual(flags, LOW_PRIORITY_CLASS)
        proc = subprocess.run(
            [
                sys.executable, "-c",
                "import ctypes\n"
                "from ctypes import wintypes\n"
                "k = ctypes.windll.kernel32\n"
                "k.GetCurrentProcess.restype = ctypes.c_void_p\n"
                "k.GetPriorityClass.argtypes = [ctypes.c_void_p]\n"
                "k.GetPriorityClass.restype = wintypes.DWORD\n"
                "print(k.GetPriorityClass(k.GetCurrentProcess()))\n",
            ],
            creationflags=flags,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "64")

    def test_python_worker_drops_itself(self):
        before = current_priority()
        proc = subprocess.run(
            [
                sys.executable, "-c",
                "from vgm2151fur.priority import current_priority, set_low_priority\n"
                "set_low_priority()\n"
                "print(current_priority())\n",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env=_child_env(),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "64")
        self.assertEqual(current_priority(), before)


class TestBatch(unittest.TestCase):
    def _two_files(self, root: Path) -> list[Path]:
        files = []
        for name in ("A Song.vgz", "B Song.vgz"):
            path = root / name
            path.write_bytes(raw_vgm(b""))
            files.append(path)
        return files

    def test_one_worker_keeps_order_and_restores_priority(self):
        before = current_priority()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = self._two_files(root)
            notes: list[str] = []
            rows = list(convert_folder(
                files, root / "out", workers=1, log=notes.append,
            ))
        self.assertEqual(notes, [])
        self.assertEqual([src for src, _info, _err in rows], files)
        self.assertTrue(all(err is None for _src, _info, err in rows))
        self.assertEqual(current_priority(), before)

    def test_two_workers_log_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = self._two_files(root)
            notes: list[str] = []
            rows = list(convert_folder(
                files, root / "out", workers=2, log=notes.append,
            ))
            reports = list(report_files(files, workers=2, log=notes.append))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(err is None for _src, _info, err in rows))
        self.assertEqual([src for src, _text, _err in reports], files)
        self.assertTrue(all(
            err is None and text is not None and "converter" in text
            for _src, text, err in reports
        ))
        if worker_limit() >= 2:
            self.assertTrue(any(line.startswith("workers:") and "low priority" in line for line in notes))
        else:
            self.assertEqual(notes, [])


class TestFurnaceConsole(unittest.TestCase):
    def test_render_does_not_take_the_console(self):
        import subprocess
        from unittest.mock import patch

        from vgm2151fur.furnace import _CREATE_NO_WINDOW, render_wav

        seen: dict = {}

        class _Proc:
            returncode = 0

            def communicate(self, timeout=None):
                return ("", None)

            def kill(self):
                pass

        def fake_popen(cmd, **kwargs):
            Path(cmd[-2]).write_bytes(b"RIFF")
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return _Proc()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fur = root / "Song.fur"
            fur.write_bytes(b"fur")
            with patch("vgm2151fur.furnace.subprocess.Popen", fake_popen), \
                 patch("vgm2151fur.furnace.find_furnace", return_value=root / "furnace.exe"):
                render_wav(fur, root / "Song.wav")
        self.assertIs(seen["kwargs"]["stdin"], subprocess.DEVNULL)
        if os.name == "nt":
            flags = seen["kwargs"]["creationflags"]
            self.assertTrue(flags & _CREATE_NO_WINDOW)
            self.assertTrue(flags & LOW_PRIORITY_CLASS)
        self.assertIn("-console", seen["cmd"])

    def test_restore_console_puts_line_input_back(self):
        if os.name != "nt":
            self.skipTest("Windows console")
        import ctypes
        from ctypes import wintypes

        from vgm2151fur.furnace import _CONSOLE_LINE, restore_console

        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.restype = ctypes.c_void_p
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel.GetStdHandle(-10)
        original = wintypes.DWORD()
        if not handle or not kernel.GetConsoleMode(handle, ctypes.byref(original)):
            self.skipTest("no console input")
        try:
            kernel.SetConsoleMode(handle, original.value & ~_CONSOLE_LINE)
            cleared = wintypes.DWORD()
            self.assertTrue(kernel.GetConsoleMode(handle, ctypes.byref(cleared)))
            self.assertEqual(cleared.value & _CONSOLE_LINE, 0)
            restore_console()
            restored = wintypes.DWORD()
            self.assertTrue(kernel.GetConsoleMode(handle, ctypes.byref(restored)))
            self.assertEqual(restored.value & _CONSOLE_LINE, _CONSOLE_LINE)
        finally:
            kernel.SetConsoleMode(handle, original.value)


if __name__ == "__main__":
    unittest.main()
