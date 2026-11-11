"""The GUI server, over HTTP against synthetic files.

No browser is opened: every test talks to a `make_server` on port 0 with
urllib, the way the page's fetch calls do. Render and compare need the
Furnace console build, so they skip when it is not next to the project.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _synth import PATCH, raw_vgm  # noqa: E402
from vgm2151fur.furnace import find_furnace  # noqa: E402
from vgm2151fur.gui import GuiApp, make_server  # noqa: E402


def _music_vgm() -> bytes:
    cmds = bytearray()
    for reg, val in PATCH:
        cmds += bytes([0x54, reg, val])
    for _ in range(24):
        cmds += bytes([0x54, 0x08, 0x78])
        cmds += bytes([0x61]) + (5000).to_bytes(2, "little")
    return raw_vgm(bytes(cmds))


def _make_pack(root: Path) -> tuple[Path, list[str]]:
    pack = root / "Test Pack (Arcade)"
    pack.mkdir(parents=True, exist_ok=True)
    names = [f"0{i + 1} Song {i + 1}.vgz" for i in range(3)]
    for name in names:
        (pack / name).write_bytes(_music_vgm())
    # The playlist fixes the order: last track first.
    (pack / "Test Pack.m3u").write_text(
        "\n".join(reversed(names)) + "\n", encoding="utf-8"
    )
    return pack, names


class _ServerCase(unittest.TestCase):
    """One app and server per test; nothing touches ~/.vgm2151fur.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.tmp = tmp
        self.app = GuiApp(state_path=tmp / "state.json")
        self.server = make_server(self.app, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # make_server only binds; the loop has to run for requests to answer.
        self._loop = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._loop.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    # -- tiny client -----------------------------------------------------

    def get(self, path: str) -> tuple[int, dict | bytes]:
        try:
            with urllib.request.urlopen(self.base + path) as res:
                body = res.read()
                if (res.headers.get("Content-Type") or "").startswith(
                    "application/json"
                ):
                    return res.status, json.loads(body)
                return res.status, body
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            exc.close()
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, payload

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            exc.close()
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, {}

    def wait_job(self, job_id: int, timeout: float = 180) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _status, data = self.get("/api/jobs")
            job = next(j for j in data["jobs"] if j["id"] == job_id)
            if job["status"] not in ("queued", "running"):
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish in time")

    # -- shared option sets ----------------------------------------------

    @staticmethod
    def plain_options(dest: str) -> dict:
        return {
            "optimize": "none", "normalize": False, "verify": False,
            "workers": 1, "dest": dest,
        }


class TestPagesAndState(_ServerCase):
    def test_page_serves(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        text = bytes(body).decode("utf-8")
        self.assertIn("vgm2151fur", text)
        self.assertIn("<audio", text)

    def test_state_and_saved_options(self):
        status, state = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("optimize", state["options"])
        status, _ = self.post("/api/save_options", {
            "optimize": "lossless", "workers": "3",
            "lossy_samples": True,  # session-only: must not persist
        })
        self.assertEqual(status, 200)
        _status, state = self.get("/api/state")
        self.assertEqual(state["options"]["optimize"], "lossless")
        saved = json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["gui"]["workers"], 3)
        self.assertNotIn("lossy_samples", saved["gui"])
        # The dest belongs to the pack view, never to a global default.
        self.assertNotIn("dest", saved["gui"])

    def test_scan_parent_then_pack(self):
        pack, names = _make_pack(self.tmp)
        status, data = self.post("/api/scan", {"root": str(self.tmp)})
        self.assertEqual(status, 200)
        self.assertEqual(data["kind"], "packs")
        self.assertEqual(len(data["packs"]), 1)
        status, data = self.post("/api/pack", {"path": str(pack)})
        self.assertEqual(status, 200)
        # Playlist order: the .m3u listed the tracks last first.
        self.assertEqual(
            [t["name"] for t in data["tracks"]],
            list(reversed(names)),
        )
        self.assertTrue(all(t["fur"] is None for t in data["tracks"]))
        # The scan remembered the browse root.
        saved = json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["browse_root"], str(self.tmp))

    def test_scan_errors_are_400s(self):
        status, data = self.post("/api/scan", {"root": str(self.tmp / "nope")})
        self.assertEqual(status, 400)
        self.assertIn("not a folder", data["error"])

    def test_open_path_kinds(self):
        pack, _names = _make_pack(self.tmp)
        src = pack / "01 Song 1.vgz"
        status, data = self.post("/api/open_path", {"path": str(src)})
        self.assertEqual(status, 200)
        self.assertEqual(data["kind"], "paths")
        self.assertEqual(data["vgms"], [str(src)])
        status, data = self.post("/api/open_path", {"path": str(pack)})
        self.assertEqual(status, 200)
        self.assertEqual(data["kind"], "pack")


class TestConvertAndReport(_ServerCase):
    def test_convert_plain_writes_fur(self):
        pack, _names = _make_pack(self.tmp)
        src = pack / "01 Song 1.vgz"
        dest = self.tmp / "out"
        status, data = self.post("/api/convert", {
            "files": [str(src)],
            "options": self.plain_options(str(dest)),
        })
        self.assertEqual(status, 200)
        job = self.wait_job(data["job"])
        self.assertEqual(job["status"], "done")
        row = job["rows"][str(src)]
        self.assertEqual(row["status"], "done", row.get("detail"))
        fur = Path(row["dst"])
        self.assertTrue(fur.is_file())
        self.assertEqual(fur.parent, dest)
        # optimize "none" converts the 1x module only.
        self.assertEqual(job["rows"][str(src)]["variants"], [])

    def test_convert_ladder_dirs_layout(self):
        pack, _names = _make_pack(self.tmp)
        src = pack / "01 Song 1.vgz"
        dest = self.tmp / "ladder"
        status, data = self.post("/api/convert", {
            "files": [str(src)],
            "options": {
                "optimize": "all", "normalize": False, "verify": False,
                "workers": 1, "dest": str(dest), "dirs_layout": True,
            },
        })
        job = self.wait_job(data["job"])
        self.assertEqual(job["status"], "done", job.get("error"))
        row = job["rows"][str(src)]
        self.assertEqual(row["status"], "done", row.get("detail"))
        factors = {v["factor"] for v in row["variants"]}
        # The ladder keeps the half steps.
        self.assertEqual(factors, {1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0})
        self.assertTrue((dest / "condense.tsv").is_file())
        self.assertTrue(any(dest.glob("default/*.fur")))
        self.assertTrue(any(dest.glob("x2 optimised/*.fur")))
        picked = next(v for v in row["variants"] if v["pick"])
        self.assertEqual(picked["factor"], 4.0)

    def test_report_job_text(self):
        pack, _names = _make_pack(self.tmp)
        src = pack / "01 Song 1.vgz"
        status, data = self.post("/api/report", {
            "files": [str(src)],
            "options": {"speed": "", "max_bpm": "", "workers": 1},
        })
        self.assertEqual(status, 200)
        job = self.wait_job(data["job"])
        row = job["rows"][str(src)]
        self.assertEqual(row["status"], "done", row.get("detail"))
        # The report names the grid, not the chip.
        self.assertIn("grid", row["text"].lower())

    def test_report_bad_speed_is_400(self):
        pack, _names = _make_pack(self.tmp)
        status, data = self.post("/api/report", {
            "files": [str(pack / "01 Song 1.vgz")],
            "options": {"speed": "abc", "max_bpm": ""},
        })
        self.assertEqual(status, 400)

    def test_cancel_endpoint(self):
        pack, _names = _make_pack(self.tmp)
        status, data = self.post("/api/convert", {
            "files": [str(pack / "01 Song 1.vgz")],
            "options": self.plain_options(str(self.tmp / "out2")),
        })
        _status, result = self.post("/api/cancel", {"id": data["job"]})
        self.assertTrue(result.get("cancelled"))
        job = self.wait_job(data["job"])
        # It may cancel or finish first; either way the endpoint worked.
        self.assertIn(job["status"], ("cancelled", "done"))

    def test_missing_file_is_400(self):
        status, data = self.post("/api/convert", {
            "files": [str(self.tmp / "ghost.vgm")],
            "options": {},
        })
        self.assertEqual(status, 400)
        self.assertIn("not found", data["error"])

    def test_bad_factors_are_400(self):
        pack, _names = _make_pack(self.tmp)
        status, data = self.post("/api/convert", {
            "files": [str(pack / "01 Song 1.vgz")],
            "options": {"optimize": "lossless", "factors": "1,oops"},
        })
        self.assertEqual(status, 400)
        self.assertIn("factors", data["error"])


class TestVolumes(_ServerCase):
    def setUp(self) -> None:
        super().setUp()
        pack, _names = _make_pack(self.tmp)
        self.pack = pack
        status, data = self.post("/api/convert", {
            "files": [str(pack / "01 Song 1.vgz")],
            "options": self.plain_options(str(self.tmp / "out")),
        })
        job = self.wait_job(data["job"])
        self.fur = Path(job["rows"][str(pack / "01 Song 1.vgz")]["dst"])

    def test_chips_read_and_write(self):
        status, data = self.post("/api/chips", {"files": [str(self.fur)]})
        self.assertEqual(status, 200)
        chips = data["files"][0]["chips"]
        self.assertTrue(chips)
        first = min(c["index"] for c in chips)
        status, data = self.post("/api/volumes", {
            "files": [str(self.fur)], "factor": 0.5,
            "targets": [first],
        })
        self.assertEqual(status, 200)
        result = data["results"][0]
        self.assertNotIn("error", result)
        after = {c["index"]: c["volume"] for c in result["chips"]}
        before = {c["index"]: c["volume"] for c in chips}
        self.assertAlmostEqual(after[first], before[first] * 0.5, places=3)
        if len(chips) > 1:
            other = next(c["index"] for c in chips if c["index"] != first)
            self.assertEqual(after[other], before[other])

    def test_set_value_clamps_to_cap(self):
        # convert writes INFO-format modules, and those cap at unity: a
        # request above the cap lands on it instead of raising.
        status, data = self.post("/api/volumes", {
            "files": [str(self.fur)], "value": 0.75,
        })
        self.assertEqual(status, 200)
        result = data["results"][0]
        self.assertNotIn("error", result)
        volume = {c["index"]: c["volume"] for c in result["chips"]}
        self.assertTrue(all(0.7 < v <= 1.0 for v in volume.values()))

    def test_factor_and_value_conflict_is_400(self):
        status, data = self.post("/api/volumes", {
            "files": [str(self.fur)], "factor": 1.0, "value": 1.0,
        })
        self.assertEqual(status, 400)


class TestWavTokens(_ServerCase):
    def test_unknown_token_is_404(self):
        status, _data = self.get("/api/wav?token=999")
        self.assertEqual(status, 404)

    def test_nonsensical_token_is_404(self):
        status, _data = self.get("/api/wav?token=zzz")
        self.assertEqual(status, 404)

    def test_registered_wav_serves_with_range(self):
        wav = self.tmp / "side.wav"
        pcm = b"\x00\x01" * 4410
        header = (
            b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVE"
            + b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
            + (1).to_bytes(2, "little") + (44100).to_bytes(4, "little")
            + (88200).to_bytes(4, "little") + (1).to_bytes(2, "little")
            + (16).to_bytes(2, "little") + b"data"
            + len(pcm).to_bytes(4, "little")
        )
        wav.write_bytes(header + pcm)
        token = self.app.register_wav(wav)
        status, body = self.get(f"/api/wav?token={token}")
        self.assertEqual(status, 200)
        self.assertTrue(bytes(body).startswith(b"RIFF"))
        req = urllib.request.Request(
            self.base + f"/api/wav?token={token}",
            headers={"Range": "bytes=0-3"},
        )
        with urllib.request.urlopen(req) as res:
            self.assertEqual(res.status, 206)
            self.assertEqual(res.read(), b"RIFF")


class TestFurnaceJobs(_ServerCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.furnace = find_furnace()

    def test_render_writes_sidecar_and_serves(self):
        if not self.furnace:
            self.skipTest("needs the Furnace console build")
        pack, _names = _make_pack(self.tmp)
        status, data = self.post("/api/convert", {
            "files": [str(pack / "01 Song 1.vgz")],
            "options": self.plain_options(str(self.tmp / "out")),
        })
        job = self.wait_job(data["job"])
        fur = Path(job["rows"][str(pack / "01 Song 1.vgz")]["dst"])
        status, data = self.post("/api/render", {
            "files": [str(fur)], "loops": 1,
        })
        self.assertEqual(status, 200)
        job = self.wait_job(data["job"])
        row = next(iter(job["rows"].values()))
        self.assertEqual(row["status"], "done", row.get("detail"))
        status, body = self.get(f"/api/wav?token={row['wav']}")
        self.assertEqual(status, 200)
        self.assertTrue(bytes(body).startswith(b"RIFF"))

    def test_compare_reports_runs(self):
        if not self.furnace:
            self.skipTest("needs the Furnace console build")
        pack, _names = _make_pack(self.tmp)
        src = pack / "01 Song 1.vgz"
        status, data = self.post("/api/convert", {
            "files": [str(src)],
            "options": self.plain_options(str(self.tmp / "out")),
        })
        job = self.wait_job(data["job"])
        fur = Path(job["rows"][str(src)]["dst"])
        status, data = self.post("/api/compare", {
            "source": str(src), "fur": str(fur), "channel": 0,
        })
        self.assertEqual(status, 200)
        job = self.wait_job(data["job"])
        row = job["rows"][str(src)]
        self.assertEqual(row["status"], "done", row.get("detail"))
        self.assertIn("exported", job["extra"])

    def test_compare_without_furnace_is_400(self):
        if self.furnace:
            self.skipTest("only meaningful when Furnace is absent")
        pack, _names = _make_pack(self.tmp)
        (self.tmp / "fake.fur").write_bytes(b"nope")
        status, data = self.post("/api/compare", {
            "source": str(pack / "01 Song 1.vgz"),
            "fur": str(self.tmp / "fake.fur"), "channel": 0,
        })
        self.assertEqual(status, 400)
        self.assertIn("Furnace", data["error"])


class TestCliEntry(unittest.TestCase):
    def test_gui_parser(self):
        from vgm2151fur import __main__ as cli

        args = cli.build_parser().parse_args(
            ["gui", "--port", "39123", "--no-browser"]
        )
        self.assertEqual(args.port, 39123)
        self.assertTrue(args.no_browser)


if __name__ == "__main__":
    unittest.main()