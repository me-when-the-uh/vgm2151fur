"""Menu, pack browsing, and command line wiring, against synthetic files."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _synth import PATCH, raw_vgm  # noqa: E402
from vgm2151fur import __main__ as cli  # noqa: E402
from vgm2151fur.convert import iter_vgms  # noqa: E402
from vgm2151fur.edit import read_chips_file  # noqa: E402
from vgm2151fur.menu import _parse_targets, run_menu  # noqa: E402
from vgm2151fur.packs import (  # noqa: E402
    find_packs, pack_tracks, parse_selection, scan_pack_chips,
)
from vgm2151fur.paths import clean_text  # noqa: E402


class Reader:
    """input() stand-in: pops scripted answers, records prompts."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class _Interrupter(Reader):
    """Reader that raises KeyboardInterrupt for the 'INT' answer."""

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if answer == "INT":
            raise KeyboardInterrupt
        return answer


def _music_vgm() -> bytes:
    cmds = bytearray()
    for reg, val in PATCH:
        cmds += bytes([0x54, reg, val])
    for _ in range(24):
        cmds += bytes([0x54, 0x08, 0x78])
        cmds += bytes([0x61]) + (5000).to_bytes(2, "little")
    return raw_vgm(bytes(cmds))


def _make_pack(root: Path, tracks: int = 3) -> Path:
    pack = root / "Test Pack (Arcade)"
    pack.mkdir(parents=True, exist_ok=True)
    names = [f"0{i + 1} Song {i + 1}.vgz" for i in range(tracks)]
    for name in names:
        (pack / name).write_bytes(_music_vgm())
    # The playlist fixes the order: last track first.
    (pack / "Test Pack.m3u").write_text(
        "\n".join(reversed(names)) + "\n", encoding="utf-8"
    )
    return pack


def _run_menu(answers, tmp: Path) -> str:
    log = io.StringIO()
    reader = Reader(answers)
    with redirect_stdout(log):
        rc = run_menu(read=reader, out=print, state_path=tmp / "state.json")
    assert rc == 0, rc
    return log.getvalue()


class TestCli(unittest.TestCase):
    def test_convert_writes_a_fur(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Song.vgz"
            src.write_bytes(_music_vgm())
            out = Path(tmp) / "fur"
            with redirect_stdout(io.StringIO()) as log:
                rc = cli.main(["convert", str(src), "-o", str(out)])
            self.assertEqual(rc, 0)
            self.assertTrue((out / "Song.fur").is_file())
            self.assertIn("Song.fur", log.getvalue())

    def test_edit_factor_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Song.vgm"
            src.write_bytes(_music_vgm())
            cli.main(["convert", str(src), "-o", tmp])
            fur = Path(tmp) / "Song.fur"
            with redirect_stdout(io.StringIO()):
                rc = cli.main(["edit", str(fur), "--chip", "YM2151", "--factor", "0.5"])
            self.assertEqual(rc, 0)
            self.assertAlmostEqual(read_chips_file(fur)[0].volume, 0.5, places=3)

    def test_edit_needs_a_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Song.vgm"
            src.write_bytes(_music_vgm())
            cli.main(["convert", str(src), "-o", tmp])
            with redirect_stdout(io.StringIO()):
                rc = cli.main(["edit", str(Path(tmp) / "Song.fur")])
            self.assertEqual(rc, 2)

    def test_missing_input_reports_cleanly(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["report", "no-such-file.vgm"]), 1)

    def test_bare_and_quoted_paths_convert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "My Song.vgz"
            src.write_bytes(_music_vgm())
            with redirect_stdout(io.StringIO()):
                rc = cli.main([f'"{src}"', "--no-normalize", "--workers", "1"])
            self.assertEqual(rc, 0)
            self.assertTrue((root / "fur" / "My Song.fur").is_file())

    def test_parent_directory_converts_the_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            with redirect_stdout(io.StringIO()) as log:
                rc = cli.main(
                    ["convert", str(root), "--workers", "1", "--no-normalize"]
                )
            self.assertEqual(rc, 0)
            self.assertTrue((pack / "fur" / "01 Song 1.fur").is_file())
            self.assertTrue((pack / "fur" / "03 Song 3.fur").is_file())
            self.assertIn("3/3 converted", log.getvalue())

    def test_glob_converts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "A.vgz").write_bytes(_music_vgm())
            (root / "B.vgz").write_bytes(_music_vgm())
            out = root / "out"
            with redirect_stdout(io.StringIO()):
                rc = cli.main(
                    ["convert", str(root / "*.vgz"), "-o", str(out),
                     "--workers", "1", "--no-normalize"]
                )
            self.assertEqual(rc, 0)
            self.assertTrue((out / "A.fur").is_file())
            self.assertTrue((out / "B.fur").is_file())

    def test_edit_finds_fur_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "Song.vgz"
            src.write_bytes(_music_vgm())
            with redirect_stdout(io.StringIO()):
                cli.main(["convert", str(src), "-o", str(root / "fur"), "--no-normalize"])
                rc = cli.main(["edit", str(root), "--chip", "YM2151", "--factor", "0.5"])
            self.assertEqual(rc, 0)
            fur = root / "fur" / "Song.fur"
            self.assertAlmostEqual(read_chips_file(fur)[0].volume, 0.5, places=3)

    def test_bare_fur_lists_chips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "Song.vgz"
            src.write_bytes(_music_vgm())
            with redirect_stdout(io.StringIO()):
                cli.main(["convert", str(src), "-o", str(root), "--no-normalize"])
            fur = root / "Song.fur"
            with redirect_stdout(io.StringIO()) as log:
                rc = cli.main([str(fur)])
            self.assertEqual(rc, 0)
            self.assertIn("YM2151", log.getvalue())
            self.assertAlmostEqual(read_chips_file(fur)[0].volume, 1.0, places=3)

    def test_report_on_a_collection_lists_packs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_pack(root)
            with redirect_stdout(io.StringIO()) as log:
                rc = cli.main(["report", str(root)])
            self.assertEqual(rc, 0)
            self.assertIn("Test Pack", log.getvalue())
            self.assertIn("pass a pack folder", log.getvalue())


class TestPacks(unittest.TestCase):
    def test_parse_selection(self):
        self.assertEqual(parse_selection("", 4), [0, 1, 2, 3])
        self.assertEqual(parse_selection("all", 4), [0, 1, 2, 3])
        self.assertEqual(parse_selection("2", 4), [1])
        self.assertEqual(parse_selection("2-3", 4), [1, 2])
        self.assertEqual(parse_selection("1,3-4", 4), [0, 2, 3])
        self.assertEqual(parse_selection("4,2", 4), [1, 3])
        for bad in ("0", "5", "3-2", "x", "1-"):
            with self.assertRaises(ValueError, msg=bad):
                parse_selection(bad, 4)

    def test_find_packs_and_playlist_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            found = find_packs(root)
            self.assertEqual([p.path for p in found], [pack])
            tracks, from_playlist = pack_tracks(found[0])
            self.assertTrue(from_playlist)
            self.assertEqual(tracks[0].name, "03 Song 3.vgz")
            self.assertEqual(len(tracks), 3)

    def test_pack_chips_from_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_pack(root)
            pack = find_packs(root)[0]
            self.assertIn("ym2151", scan_pack_chips(pack))
            pack.chips = scan_pack_chips(pack)
            self.assertIn("YM2151", pack.chip_label)

    def test_playlist_keeps_a_track_it_names_by_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            # The line's directory is not there. The file in the pack still counts,
            # and the two tracks the playlist never names stay on the list.
            (pack / "Test Pack.m3u").write_text(
                "nested/03 Song 3.vgz\n", encoding="utf-8"
            )
            found = find_packs(root)[0]
            tracks, from_playlist = pack_tracks(found)
            self.assertTrue(from_playlist)
            self.assertEqual(tracks[0].name, "03 Song 3.vgz")
            self.assertEqual(
                {p.name for p in tracks},
                {"01 Song 1.vgz", "02 Song 2.vgz", "03 Song 3.vgz"},
            )


class TestPaths(unittest.TestCase):
    def test_clean_text_strips_paste_wrapping(self):
        self.assertEqual(clean_text('"C:\\Music\\Song.vgz"'), "C:\\Music\\Song.vgz")
        self.assertEqual(clean_text("& 'C:\\Music\\Song.vgz'"), "C:\\Music\\Song.vgz")
        self.assertEqual(clean_text("file:///C:/Music/My%20Song.vgz"), "C:/Music/My Song.vgz")
        self.assertEqual(Path(clean_text("~")), Path.home())

    def test_iter_vgms_skips_a_fur_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fur = Path(tmp) / "Song.fur"
            fur.write_bytes(b"not a vgm")
            self.assertEqual(iter_vgms(fur), [])

    def test_parse_targets_all(self):
        chip = type("Chip", (), {"index": 0, "name": "YM2151"})()
        self.assertEqual(_parse_targets("all", [chip]), [0])


class TestMenu(unittest.TestCase):
    def test_pack_convert_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            log = _run_menu(
                ["1", str(root), "1", "c", "all", "", "", "n", "b", "", "q"], root
            )
            self.assertTrue((pack / "fur" / "default" / "01 Song 1.fur").is_file())
            self.assertTrue((pack / "fur" / "default" / "03 Song 3.fur").is_file())
            self.assertIn("3/3 converted", log)
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["browse_root"], str(root))
            self.assertEqual(state["pack"], str(pack))

    def test_pack_convert_a_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            # The playlist order is 3, 2, 1, so tracks 2-3 are "02 Song 2"
            # and "01 Song 1".
            _run_menu(
                ["1", str(root), "1", "c", "2-3", "", "", "n", "b", "", "q"], root
            )
            self.assertFalse((pack / "fur" / "default" / "03 Song 3.fur").exists())
            self.assertTrue((pack / "fur" / "default" / "02 Song 2.fur").is_file())
            self.assertTrue((pack / "fur" / "default" / "01 Song 1.fur").is_file())

    def test_pack_volume_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            cli.main(["convert", str(pack / "01 Song 1.vgz"), "-o", str(pack / "fur")])
            # Playlist order is 3, 2, 1, so "01 Song 1" is track 3.
            log = _run_menu(
                ["1", str(root), "1", "e", "3", "1", "0.5", "", "b", "", "q"], root
            )
            fur = pack / "fur" / "01 Song 1.fur"
            self.assertAlmostEqual(read_chips_file(fur)[0].volume, 0.5, places=3)
            self.assertIn("YM2151 0.500", log)

    def test_direct_edit_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "Song.vgm"
            src.write_bytes(_music_vgm())
            cli.main(["convert", str(src), "-o", str(root)])
            fur = root / "Song.fur"
            _run_menu(["3", f'"{fur}"', "1", "0.8", "", "q"], root)
            self.assertAlmostEqual(read_chips_file(fur)[0].volume, 0.8, places=3)

    def test_unknown_command_then_quit(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _run_menu(["nope", "q"], Path(tmp))
            self.assertIn("unknown command", log)

    def test_paste_quoted_file_converts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "My Song.vgz"
            src.write_bytes(_music_vgm())
            log = _run_menu([f'"{src}"', "q"], root)
            self.assertTrue((root / "fur" / "default" / "My Song.fur").is_file())
            self.assertIn("1/1 converted", log)

    def test_paste_pack_folder_opens_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            log = _run_menu([f'"{pack}"', "b", "q"], root)
            self.assertIn("01 Song 1", log)
            self.assertIn("playlist order", log)

    def test_q_inside_a_pack_quits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            reader = Reader(["1", str(root), "1", "q"])
            log = io.StringIO()
            with redirect_stdout(log):
                rc = run_menu(read=reader, out=print, state_path=root / "state.json")
            self.assertEqual(rc, 0)
            self.assertIn(pack.name, log.getvalue())
            self.assertNotIn("c, e, r or b", log.getvalue())
            self.assertIn("c convert", reader.prompts[-1])

    def test_quoted_collection_lists_packs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _make_pack(root)
            log = _run_menu(["1", f'"{root}"', "1", "b", "", "q"], root)
            self.assertIn(pack.name, log)

    def test_eof_exits_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = Reader([])
            with redirect_stdout(io.StringIO()):
                rc = run_menu(read=reader, out=print, state_path=Path(tmp) / "state.json")
            self.assertEqual(rc, 0)

    def test_ctrl_c_at_the_prompt_quits(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = io.StringIO()
            with redirect_stdout(log):
                rc = run_menu(
                    read=_Interrupter(["nope", "INT"]),
                    out=print,
                    state_path=Path(tmp) / "state.json",
                )
            self.assertEqual(rc, 130)
            self.assertIn("unknown command", log.getvalue())
            self.assertIn("quit (Ctrl+C)", log.getvalue())

    def test_ctrl_c_in_a_command_returns_to_the_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_pack(root)
            log = io.StringIO()
            with redirect_stdout(log):
                rc = run_menu(
                    read=_Interrupter(["1", str(root), "1", "INT", "q"]),
                    out=print,
                    state_path=root / "state.json",
                )
            self.assertEqual(rc, 0)
            self.assertIn("cancelled", log.getvalue())

    def test_ctrl_c_in_a_cli_run_exits_130(self):
        from unittest import mock

        with mock.patch.object(cli, "_main", side_effect=KeyboardInterrupt):
            with redirect_stderr(io.StringIO()) as err:
                rc = cli.main(["convert", "whatever.vgm"])
        self.assertEqual(rc, 130)
        self.assertIn("cancelled", err.getvalue())


if __name__ == "__main__":
    unittest.main()
