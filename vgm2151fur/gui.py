"""Browser GUI: a local web front-end over the same core the menu uses.

`launch()` serves one page on 127.0.0.1 and opens the default browser. The
page scans pack folders, converts with every option the command line has,
edits chip volumes, renders .fur files to WAV (played in the page), runs
`report`, and pitch-compares a .fur against its source.

Every action calls the functions `menu.py` calls. Jobs run one at a time on
two background queues (heavy: convert and report; fast: render and compare);
the page polls `/api/jobs` instead of holding a socket stream, which is
simpler to keep correct. Settings live in the same `~/.vgm2151fur.json` the
menu uses, under a `gui` key. The lossy-clustering choice stays session-only
there too.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from vgm2151fur import __version__
from vgm2151fur.convert import VGM_SUFFIXES, convert_folder, report_files
from vgm2151fur.edit import FurEditError, read_chips_file, set_chip_volume_file
from vgm2151fur.furnace import find_furnace, restore_console
from vgm2151fur.menu import PACK_SCAN_LIMIT
from vgm2151fur.packs import Pack, find_packs, pack_tracks, scan_pack_chips
from vgm2151fur.paths import clean_text, expand_token, folder_pack

from vgm2151fur._gui_page import PAGE


# ------------------------------------------------------------------ options

DEFAULT_OPTIONS = {
    "dest": "",
    "speed": "",
    "max_bpm": "",
    "optimize": "all",
    "condense_max": 4,
    "factors": "",
    "dirs_layout": True,
    "lfo_loop": True,
    "loop_find": True,
    "lossy_samples": False,  # session-only: never saved
    "normalize": True,
    "verify": False,
    "chips": "all",
    "workers": "",
    "c352_quad": False,
    "render_loops": 1,
    "compare_channel": 0,
}

# Options that never persist to ~/.vgm2151fur.json: the dest belongs to a
# pack, and lossy clustering must default back off in a new session.
_SESSION_ONLY = ("dest", "lossy_samples")

_INT_OPTIONS = ("speed", "max_bpm", "condense_max", "render_loops",
                "compare_channel", "workers")


def _clean_options(raw: dict) -> dict:
    """Values the page sent, coerced. Unknown keys and bad numbers drop out."""
    opts: dict = {}
    for key, default in DEFAULT_OPTIONS.items():
        value = raw.get(key, default)
        if key in _INT_OPTIONS:
            if isinstance(value, bool):
                value = int(value)
            elif isinstance(value, str):
                text = value.strip()
                if not text:
                    # Blank means "auto" for the grid fields, default otherwise.
                    value = "" if key in ("speed", "max_bpm") else default
                else:
                    try:
                        number = float(text)
                    except ValueError:
                        raise ValueError(
                            f"{key} must be a number (got {text!r})"
                        ) from None
                    value = number if key == "max_bpm" else int(number)
            elif not isinstance(value, (int, float)):
                value = default
        elif isinstance(default, bool):
            value = bool(value)
        else:
            value = "" if value is None else str(value).strip()
        opts[key] = value
    # The pick select only knows these three.
    if opts["optimize"] not in ("none", "lossless", "all"):
        opts["optimize"] = "all"
    if opts["chips"] not in ("all", "melody-only", "pcm-only"):
        opts["chips"] = "all"
    factors = str(opts.get("factors") or "").strip()
    if factors:
        try:
            [float(x) for x in factors.split(",") if x.strip()]
        except ValueError:
            raise ValueError("factors must be a comma list, e.g. 1,1.5,2")
    return opts


def _str_number(opts: dict, key: str) -> int | float | None:
    """An option the page may have left blank: '' or a number."""
    value = opts.get(key, "")
    if isinstance(value, str):
        return None
    return value


def _min_row_for_bpm(max_bpm: float | None) -> float | None:
    """The CLI's BPM cap mapping, reused so the two never drift."""
    from vgm2151fur.__main__ import _min_row_for_bpm as cli_min_row

    return cli_min_row(max_bpm)


# ------------------------------------------------------------------ jobs

_JOB_LOCK = threading.Lock()
_NEXT_ID = 1
_LOG_LINES = 900


def _new_id() -> int:
    global _NEXT_ID
    with _JOB_LOCK:
        job_id = _NEXT_ID
        _NEXT_ID += 1
    return job_id


class Job:
    """One queued unit of work, with per-track rows and a log buffer."""

    def __init__(self, kind: str, label: str, files: list[Path],
                 request_options: dict):
        self.id = _new_id()
        self.kind = kind
        self.label = label
        self.created = time.time()
        self.status = "queued"
        self.error: str | None = None
        self.files = files
        self.request_options = request_options
        self.rows: dict[str, dict] = {}
        for path in files:
            self.rows[str(path)] = {"status": "queued"}
        self.log: deque[str] = deque(maxlen=_LOG_LINES)
        self.extra: dict = {}
        self.cancel_flag = threading.Event()

    # -- mutation (called from the job's own thread) --------------------

    def log_line(self, text: str = "") -> None:
        for line in str(text).splitlines() or [""]:
            self.log.append(line)

    def row(self, path: Path | str, **fields) -> None:
        entry = self.rows.get(str(path))
        if entry is not None:
            entry.update(fields)

    def fail(self, message: str) -> None:
        self.status = "error"
        self.error = message
        self.log_line(f"ERROR: {message}")

    # -- snapshot (read by HTTP threads) ----------------------------------

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "error": self.error,
            "created": self.created,
            "files": [str(f) for f in self.files],
            "rows": {k: dict(v) for k, v in self.rows.items()},
            "log": list(self.log),
            "extra": self.extra,
        }


class Worker(threading.Thread):
    """FIFO queue owner. One job at a time per worker."""

    def __init__(self, app: "GuiApp", name: str):
        super().__init__(name=f"vgm2151fur-{name}", daemon=True)
        self.app = app
        self.queue: queue.SimpleQueue[Job | None] = queue.SimpleQueue()

    def submit(self, job: Job) -> None:
        self.queue.put(job)

    def run(self) -> None:
        while True:
            job = self.queue.get()
            if job is None:
                return
            try:
                job.status = "running"
                RUNNERS[job.kind](self.app, job)
                if not job.cancel_flag.is_set():
                    job.status = "done"
            except Exception as exc:
                if job.cancel_flag.is_set():
                    job.status = "cancelled"
                else:
                    job.fail(str(exc))


RUNNERS: dict[str, object] = {}


# ------------------------------------------------------------------ runners

def _wav_sidecar(fur: Path) -> Path:
    return fur.with_suffix(".wav")


def _run_convert(app: "GuiApp", job: Job) -> None:
    opts = job.request_options
    files, dest = job.files, Path(opts["dest"])
    pcm = opts["chips"] != "melody-only"
    include_fm = opts["chips"] != "pcm-only"
    speed = _str_number(opts, "speed")
    min_row = _min_row_for_bpm(_str_number(opts, "max_bpm"))
    workers = _str_number(opts, "workers")
    common = dict(
        speed=speed, pcm=pcm, min_row=min_row,
        include_fm=include_fm, normalize=bool(opts["normalize"]),
        loop_find=bool(opts["loop_find"]), lfo_loop=bool(opts["lfo_loop"]),
        lossy_samples=bool(opts["lossy_samples"]), workers=workers,
        c352_quad=bool(opts["c352_quad"]), log=job.log_line,
    )
    if opts["optimize"] == "none":
        gen = convert_folder(files, dest, **common)
    else:
        factor_list: list[float] | None = None
        if str(opts["factors"]).strip():
            factor_list = [
                float(x) for x in str(opts["factors"]).split(",") if x.strip()
            ]
        gen = convert_folder(
            files, dest,
            variants=True,
            condense=int(opts["condense_max"]),
            factors=factor_list,
            optimize=opts["optimize"],
            verify=bool(opts["verify"]),
            layout="dirs" if opts["dirs_layout"] else "flat",
            **common,
        )
    try:
        for src, info, error in gen:
            if job.cancel_flag.is_set():
                break
            name = src.name
            if error is not None:
                job.row(src, status="error", detail=error)
                job.log_line(f"ERROR {name}: {error}")
                continue
            variants = [
                {
                    "factor": v["factor"],
                    "file": Path(v["dst"]).name,
                    "bpm": round(v["bpm"], 1),
                    "loss": v["loss_total"],
                    "keys": v.get("keyons"),
                    "miss": v.get("missed"),
                    "lossless": bool(v.get("lossless", True)),
                    "pick": bool(v.get("pick")),
                }
                for v in info.get("variants", [])
            ]
            notes = [
                w for w in info.get("warnings", []) if w != info.get("loop")
            ][:6]
            vol = (
                f"  volume fit {info['gain']:.2f}x"
                if info.get("gain") is not None else ""
            )
            # The plain path's info counts notes; a pick dict does not.
            counted = (
                f", {info['events']} notes"
                if info.get("events") is not None else ""
            )
            job.row(
                src, status="done", dst=str(info["dst"]),
                detail=(
                    f"{info['bytes']} bytes, speed {info['speed']}"
                    f"{counted}{vol}"
                ),
                variants=variants, warnings=notes,
            )
            if variants:
                pick = next((v for v in variants if v["pick"]), variants[0])
                job.log_line(
                    f"{name} -> {len(variants)} variants, picked x{pick['factor']:g}"
                )
            else:
                job.log_line(f"{name} -> {info['bytes']} bytes, speed {info['speed']}")
            for warning in notes[:2]:
                job.log_line(f"   warning: {warning}")
    finally:
        # Closing a suspended generator shuts the process pool down without
        # waiting for queued tracks, the same drop-the-queue Ctrl+C gets.
        try:
            gen.close()
        except Exception:
            pass
    if job.cancel_flag.is_set():
        job.status = "cancelled"


def _run_report(app: "GuiApp", job: Job) -> None:
    opts = job.request_options
    speed = _str_number(opts, "speed")
    min_row = _min_row_for_bpm(_str_number(opts, "max_bpm"))
    for src, text, error in report_files(
        job.files, speed=speed, min_row=min_row,
        workers=_str_number(opts, "workers"), log=job.log_line,
    ):
        if job.cancel_flag.is_set():
            break
        if error is not None:
            job.row(src, status="error", detail=error)
        else:
            job.row(src, status="done", text=text)
    if job.cancel_flag.is_set():
        job.status = "cancelled"


def _run_render(app: "GuiApp", job: Job) -> None:
    from vgm2151fur.furnace import FurnaceError, render_wav

    loops = int(job.request_options.get("render_loops") or 1)
    for fur in job.files:
        if job.cancel_flag.is_set():
            break
        out = _wav_sidecar(fur)
        try:
            render_wav(fur, out, loops=loops)
        except FurnaceError as exc:
            job.row(fur, status="error", detail=str(exc))
            job.log_line(f"ERROR {fur.name}: {exc}")
            continue
        token = app.register_wav(out)
        job.row(fur, status="done", detail=f"{out.stat().st_size} bytes", wav=token)
        job.log_line(f"{fur.name} -> {out.name}")
    if job.cancel_flag.is_set():
        job.status = "cancelled"


def _run_compare(app: "GuiApp", job: Job) -> None:
    from vgm2151fur.diag import compare_fur_to_source

    src = job.files[0]
    fur = Path(job.request_options["fur"])
    channel = int(job.request_options.get("channel") or 0)
    job.log_line(f"comparing {fur.name} channel {channel} against the source...")
    runs, exported = compare_fur_to_source(src, fur, channel)
    wav = _wav_sidecar(fur)
    token = app.register_wav(wav) if wav.is_file() else None
    detail = (
        "no sustained divergence above 0.5 st"
        if not runs else f"{len(runs)} divergence run(s)"
    )
    job.row(src, status="done", detail=detail, wav=token)
    job.extra = {
        "exported": str(exported),
        "runs": [
            {"text": str(run), "t0": run.t0, "t1": run.t1,
             "st": round(run.worst_st, 2)}
            for run in runs
        ],
    }
    job.log_line(detail)


RUNNERS.update(convert=_run_convert, report=_run_report,
               render=_run_render, compare=_run_compare)


# ------------------------------------------------------------------ app

def _load_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


class GuiApp:
    """State shared by the HTTP threads: settings, jobs, WAV tokens."""

    def __init__(self, state_path: Path | None = None):
        self.state_path = Path(state_path) if state_path else (
            Path.home() / ".vgm2151fur.json"
        )
        self.lock = threading.Lock()
        raw = _load_json(self.state_path)
        raw.pop("lossy_samples", None)  # stays session-only, as in the menu
        self.state = raw
        saved = raw.get("gui")
        if not isinstance(saved, dict):
            saved = {}
        normalize = saved.pop("normalize", None)
        if normalize is None:
            # Default on only when the level fit can actually run.
            normalize = find_furnace() is not None
        self.options: dict = {
            **DEFAULT_OPTIONS,
            **{k: v for k, v in saved.items() if k in DEFAULT_OPTIONS},
        }
        self.options["normalize"] = bool(normalize)
        self.jobs: list[Job] = []
        self.heavy = Worker(self, "heavy")
        self.fast = Worker(self, "fast")
        self.heavy.start()
        self.fast.start()
        self.wav_tokens: dict[int, Path] = {}
        self._wav_lock = threading.Lock()
        self._next_wav = 1

    # -- settings --------------------------------------------------------

    def persist(self) -> None:
        with self.lock:
            saved = {
                k: v for k, v in self.options.items() if k not in _SESSION_ONLY
            }
            self.state["gui"] = saved
            try:
                self.state_path.write_text(
                    json.dumps(self.state, indent=2) + "\n", encoding="utf-8"
                )
            except OSError:
                pass

    def remember_options(self, opts: dict) -> None:
        with self.lock:
            self.options.update(opts)
        self.persist()

    def remember_out_dir(self, pack: str | None, dest: Path) -> None:
        if not pack:
            return
        with self.lock:
            self.state.setdefault("out_dirs", {})[str(Path(pack))] = str(dest)
        self.persist()

    def remember_browse_root(self, root: Path) -> None:
        with self.lock:
            self.state["browse_root"] = str(root)
        self.persist()

    # -- jobs -------------------------------------------------------------

    def submit(
        self, kind: str, label: str, files: list[Path], options: dict | None = None,
        worker: str = "heavy",
    ) -> Job:
        merged = dict(self.options)
        merged.update(options or {})
        job = Job(kind, label, files, merged)
        with self.lock:
            self.jobs.append(job)
            if len(self.jobs) > 40:
                self.jobs = self.jobs[-40:]
        (self.heavy if worker == "heavy" else self.fast).submit(job)
        return job

    def cancel(self, job_id: int) -> bool:
        with self.lock:
            for job in self.jobs:
                if job.id == job_id and job.status in ("queued", "running"):
                    job.cancel_flag.set()
                    if job.status == "queued":
                        job.status = "cancelled"
                    return True
        return False

    def jobs_snapshot(self) -> list[dict]:
        with self.lock:
            return [job.snapshot() for job in reversed(self.jobs)]

    # -- WAV tokens ---------------------------------------------------------

    def register_wav(self, path: Path) -> int | None:
        """A short-lived handle; only these paths /api/wav will serve."""
        path = Path(path)
        if not path.is_file():
            return None
        with self._wav_lock:
            token = self._next_wav
            self._next_wav += 1
            self.wav_tokens[token] = path
        return token

    def wav_path(self, token: int) -> Path | None:
        with self._wav_lock:
            return self.wav_tokens.get(int(token))


# ------------------------------------------------------------------ scanning

def _fur_path(fur_dir: Path, stem: str) -> Path:
    """The 1x .fur for a track: `default/<stem>.fur` in the dir layout, else flat."""
    sub = fur_dir / "default" / (stem + ".fur")
    return sub if sub.is_file() else fur_dir / (stem + ".fur")


def _pack_view_response(app: GuiApp, pack: Pack) -> dict:
    tracks, from_playlist = pack_tracks(pack)
    if not pack.chips:
        pack.chips = scan_pack_chips(pack)
    with app.lock:
        remembered = app.state.get("out_dirs", {}).get(str(pack.path))
    fur_dir = Path(remembered) if remembered else pack.path / "fur"
    rows = []
    for track in tracks:
        fur = _fur_path(fur_dir, track.stem)
        wav_token = app.register_wav(_wav_sidecar(fur)) if fur.is_file() else None
        rows.append({
            "path": str(track),
            "name": track.name,
            "stem": track.stem,
            "fur": str(fur) if fur.is_file() else None,
            "wav": wav_token,
        })
    return {
        "kind": "pack",
        "path": str(pack.path),
        "name": pack.name,
        "fur_dir": str(fur_dir),
        "chips": pack.chip_label or "no supported chip",
        "playlist_order": from_playlist,
        "tracks": rows,
    }


# ------------------------------------------------------------------ handler

class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, fmt, *args) -> None:  # keep the console quiet
        pass

    # -- plumbing ---------------------------------------------------------

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("request body is not JSON")
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    # -- GET ----------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        app = self.server.app
        if path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            with app.lock:
                browse_root = app.state.get("browse_root", "")
            furnace = find_furnace()
            self._json({
                "version": __version__,
                "furnace": str(furnace) if furnace else None,
                "browse_root": browse_root,
                "options": {
                    k: v for k, v in app.options.items()
                    if k not in _SESSION_ONLY
                },
            })
        elif path == "/api/jobs":
            self._json({"jobs": app.jobs_snapshot()})
        elif path == "/api/wav":
            self._serve_wav(app)
        else:
            self._json({"error": "no such endpoint"}, 404)

    def _serve_wav(self, app: GuiApp) -> None:
        query = parse_qs(urlparse(self.path).query)
        token = (query.get("token") or [""])[0]
        target = app.wav_path(int(token)) if token.isdigit() else None
        if target is None or not target.is_file():
            self._json({"error": "no such WAV"}, 404)
            return
        data = target.read_bytes()
        start, end, status = 0, len(data) - 1, 200
        range_header = self.headers.get("Range") or ""
        if range_header.startswith("bytes="):
            try:
                first, _sep, last = range_header[6:].partition("-")
                start = int(first) if first else 0
                if last:
                    end = min(int(last), len(data) - 1)
                status = 206
            except ValueError:
                start, end, status = 0, len(data) - 1, 200
        if start >= len(data):
            self._json({"error": "range out of bounds"}, 416)
            return
        chunk = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    # -- POST ---------------------------------------------------------------

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        app = self.server.app
        try:
            body = self._body()
            if path == "/api/scan":
                out = _api_scan(app, body)
            elif path == "/api/pack":
                out = _api_pack(app, body)
            elif path == "/api/open_path":
                out = _api_open_path(app, body)
            elif path == "/api/convert":
                out = _api_convert(app, body)
            elif path == "/api/report":
                out = _api_report(app, body)
            elif path == "/api/render":
                out = _api_render(app, body)
            elif path == "/api/compare":
                out = _api_compare(app, body)
            elif path == "/api/volumes":
                out = _api_volumes(body)
            elif path == "/api/chips":
                out = _api_chips(body)
            elif path == "/api/cancel":
                job_id = int(body.get("id") or 0)
                out = {"cancelled": app.cancel(job_id)}
            elif path == "/api/save_options":
                app.remember_options(_clean_options(body))
                out = {"saved": True}
            else:
                self._json({"error": "no such endpoint"}, 404)
                return
            self._json(out)
        except (ValueError, OSError) as exc:
            self._json({"error": str(exc)}, 400)
        except BrokenPipeError:  # the page navigated away mid-response
            pass


# ------------------------------------------------------------------ endpoints

def _require_root(body: dict) -> Path:
    root = Path(clean_text(str(body.get("root") or "")))
    if not str(root):
        raise ValueError("give a folder to scan")
    if not root.is_dir():
        raise ValueError(f"not a folder: {root}")
    return root


def _api_scan(app: GuiApp, body: dict) -> dict:
    root = _require_root(body)
    app.remember_browse_root(root)
    pack = folder_pack(root)
    if pack is not None:
        return _pack_view_response(app, pack)
    packs = find_packs(root, limit=PACK_SCAN_LIMIT)
    for item in packs:
        if not item.chips:
            item.chips = scan_pack_chips(item)
    return {
        "kind": "packs",
        "root": str(root),
        "truncated": len(packs) >= PACK_SCAN_LIMIT,
        "packs": [
            {
                "path": str(p.path),
                "name": p.name,
                "tracks": len(p.tracks),
                "chips": p.chip_label or "no supported chip",
                "playlist": p.playlist is not None,
            }
            for p in packs
        ],
    }


def _api_pack(app: GuiApp, body: dict) -> dict:
    path = Path(clean_text(str(body.get("path") or "")))
    if not path.is_dir():
        raise ValueError(f"not a folder: {path}")
    pack = folder_pack(path)
    if pack is None:
        raise ValueError(f"no .vgm/.vgz directly inside {path.name}")
    return _pack_view_response(app, pack)


def _api_open_path(app: GuiApp, body: dict) -> dict:
    raw = clean_text(str(body.get("path") or ""))
    if not raw:
        raise ValueError("give a file, folder or glob")
    matches = [p for p in expand_token(raw) if p.exists()]
    if not matches:
        raise ValueError(f"no match for {raw}")
    if len(matches) == 1 and matches[0].is_dir():
        return _api_scan(app, {"root": str(matches[0])})
    vgms = [p for p in matches if p.suffix.lower() in VGM_SUFFIXES]
    furs = [p for p in matches if p.suffix.lower() == ".fur"]
    if not vgms and not furs:
        raise ValueError(f"no .vgm/.vgz or .fur among {len(matches)} matches")
    return {"kind": "paths",
            "vgms": [str(p) for p in vgms], "furs": [str(p) for p in furs]}


def _resolve_convert_files(raw_files) -> list[Path]:
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("select at least one track")
    files: list[Path] = []
    for path in raw_files:
        p = Path(clean_text(str(path)))
        if not p.is_file():
            raise ValueError(f"not found: {p}")
        if p.suffix.lower() not in VGM_SUFFIXES:
            raise ValueError(f"not a .vgm/.vgz file: {p.name}")
        files.append(p)
    return files


def _resolve_fur_files(raw_files) -> list[Path]:
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("select at least one .fur")
    files: list[Path] = []
    for path in raw_files:
        p = Path(clean_text(str(path)))
        if not p.is_file():
            raise ValueError(f"not found: {p}")
        if p.suffix.lower() != ".fur":
            raise ValueError(f"not a .fur file: {p.name}")
        files.append(p)
    return files


def _api_convert(app: GuiApp, body: dict) -> dict:
    files = _resolve_convert_files(body.get("files") or [])
    opts = _clean_options(body.get("options") or {})
    app.remember_options(opts)
    dest_raw = clean_text(str(body.get("dest") or opts.get("dest") or ""))
    if dest_raw:
        dest = Path(dest_raw)
    else:
        dest = files[0].parent / "fur"
    job_opts = dict(opts)
    job_opts["dest"] = str(dest)
    label = files[0].name if len(files) == 1 else f"{len(files)} tracks"
    job = app.submit("convert", label, files, job_opts)
    # Remembered per pack so the next visit opens at the same output folder.
    app.remember_out_dir(body.get("pack"), dest)
    return {"job": job.id}


def _api_report(app: GuiApp, body: dict) -> dict:
    files = _resolve_convert_files(body.get("files") or [])
    opts = {
        k: v for k, v in _clean_options(body.get("options") or {}).items()
        if k in ("speed", "max_bpm")
    }
    label = files[0].name if len(files) == 1 else f"{len(files)} tracks"
    job = app.submit("report", label, files, opts)
    return {"job": job.id}


def _api_render(app: GuiApp, body: dict) -> dict:
    files = _resolve_fur_files(body.get("files") or [])
    if not find_furnace():
        raise ValueError(
            "Furnace was not found next to the project (furnace/ or "
            "VGM2151FUR_FURNACE); rendering needs it"
        )
    label = files[0].name if len(files) == 1 else f"{len(files)} tracks"
    job = app.submit("render", label, files,
                     {"render_loops": body.get("loops") or 1}, worker="fast")
    return {"job": job.id}


def _api_compare(app: GuiApp, body: dict) -> dict:
    source = Path(clean_text(str(body.get("source") or "")))
    fur = Path(clean_text(str(body.get("fur") or "")))
    if not source.is_file() or source.suffix.lower() not in VGM_SUFFIXES:
        raise ValueError(f"give the source .vgm/.vgz (got {source.name})")
    if not fur.is_file() or fur.suffix.lower() != ".fur":
        raise ValueError(f"give a .fur file (got {fur.name})")
    if not find_furnace():
        raise ValueError(
            "Furnace was not found next to the project; comparing needs it"
        )
    job = app.submit(
        "compare", fur.name, [source],
        {"fur": str(fur), "channel": body.get("channel") or 0}, worker="fast",
    )
    return {"job": job.id}


def _chip_dicts(chips) -> list[dict]:
    return [
        {"index": c.index, "id": c.id, "name": c.name,
         "volume": round(c.volume, 4)}
        for c in chips
    ]


def _api_chips(body: dict) -> dict:
    files = [Path(clean_text(str(p))) for p in (body.get("files") or [])]
    if not files:
        raise ValueError("give a .fur file")
    out = []
    for path in files:
        if not path.is_file():
            raise ValueError(f"not found: {path}")
        try:
            out.append({"file": str(path), "chips": _chip_dicts(read_chips_file(path))})
        except (FurEditError, OSError) as exc:
            out.append({"file": str(path), "error": str(exc)})
    return {"files": out}


def _api_volumes(body: dict) -> dict:
    files = [Path(clean_text(str(p))) for p in (body.get("files") or [])]
    if not files:
        raise ValueError("give at least one .fur")
    factor = body.get("factor")
    value = body.get("value")
    if (factor is None) == (value is None):
        raise ValueError("give either factor (multiply) or value (set)")
    targets = body.get("targets")
    if targets is not None and (
        not isinstance(targets, list)
        or not all(isinstance(t, int) for t in targets)
    ):
        raise ValueError("targets must be a list of chip indexes")
    if factor is not None:
        factor = float(factor)
    if value is not None:
        value = float(value)
    results = []
    for path in files:
        if not path.is_file():
            raise ValueError(f"not found: {path}")
        try:
            set_chip_volume_file(path, factor=factor, value=value,
                                 targets=targets)
            results.append({
                "file": str(path),
                "chips": _chip_dicts(read_chips_file(path)),
            })
        except (FurEditError, OSError, ValueError) as exc:
            results.append({"file": str(path), "error": str(exc)})
    return {"results": results}


# ------------------------------------------------------------------ server

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, app: GuiApp):
        self.app = app
        super().__init__(address, _Handler)


def make_server(app: GuiApp | None = None, *, port: int = 0) -> _Server:
    """A bound server on 127.0.0.1, for `launch` and the tests."""
    return _Server(("127.0.0.1", port), app or GuiApp())


def launch(*, port: int | None = None, open_browser: bool = True,
           state_path: Path | None = None) -> int:
    """Serve the GUI until Ctrl+C. Returns an exit code."""
    restore_console()
    app = GuiApp(state_path)
    try:
        server = make_server(app, port=port or 0)
    except OSError as exc:
        print(f"ERROR: could not open the port ({exc})")
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    furnace = find_furnace()
    print(f"vgm2151fur {__version__}")
    print(f"  gui      {url}  (Ctrl+C quits; the server stays local)")
    if furnace:
        print(f"  furnace  {furnace}")
    else:
        print(
            "  furnace  not found: normalize, render and compare stay off. "
            "Put a build in furnace/ or point VGM2151FUR_FURNACE at it."
        )
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("\n  gui closed")
    return 0