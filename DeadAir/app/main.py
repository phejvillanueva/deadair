"""DeadAir local web server.

Standard library only (http.server) so setup is just "install Python + FFmpeg".
Binds to 127.0.0.1 and nothing is ever sent off the machine.

Run:  python -m app.main            (opens your browser)
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, silence, video
from .utils import (PROJECT_ROOT, ALLOWED_EXTENSIONS, find_binary, fmt_size, has_allowed_extension,
                    open_in_os, sanitize_filename, unique_path)

log = logging.getLogger("deadair")

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
MAX_JSON_BYTES = 64 * 1024
CHUNK = 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class VideoRecord:
    id: str
    original_name: str
    path: Path
    workdir: Path
    info: video.VideoInfo
    detection: silence.Plan | None = None
    detection_settings: silence.Settings | None = None


@dataclass
class Job:
    id: str
    kind: str
    state: str = "running"          # running | done | error
    progress: float = 0.0
    result: dict | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)


class AppState:
    """All mutable server state. One video + one job at a time keeps things simple and safe."""

    def __init__(self, output_dir: Path | None = None):
        self.output_dir = Path(output_dir or os.environ.get("DEADAIR_OUTPUT_DIR")
                               or PROJECT_ROOT / "output").resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_root = Path(tempfile.mkdtemp(prefix="deadair_"))
        self.lock = threading.Lock()
        self.videos: dict[str, VideoRecord] = {}
        self.jobs: dict[str, Job] = {}
        self.active_job: str | None = None
        self.last_output: Path | None = None

    # ---- housekeeping
    def cleanup(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def busy(self) -> bool:
        with self.lock:
            job = self.jobs.get(self.active_job) if self.active_job else None
            return bool(job and job.state == "running")

    # ---- videos
    def register_video(self, rec: VideoRecord) -> None:
        with self.lock:
            for old in self.videos.values():            # single active video: free the old copy
                shutil.rmtree(old.workdir, ignore_errors=True)
            self.videos = {rec.id: rec}

    def get_video(self, video_id: str) -> VideoRecord:
        with self.lock:
            rec = self.videos.get(video_id)
        if not rec:
            raise ApiError(404, "That video is no longer loaded. Please choose it again.")
        return rec

    # ---- jobs
    def start_job(self, kind: str, fn) -> Job:
        with self.lock:
            active = self.jobs.get(self.active_job) if self.active_job else None
            if active and active.state == "running":
                raise ApiError(409, "DeadAir is already working on something. Please wait for it to finish.")
            job = Job(id=uuid.uuid4().hex, kind=kind)
            self.jobs[job.id] = job
            self.active_job = job.id

        def runner():
            try:
                job.result = fn(job)
                job.progress = 1.0
                job.state = "done"
            except video.VideoError as exc:
                job.error, job.state = str(exc), "error"
            except ApiError as exc:
                job.error, job.state = exc.message, "error"
            except Exception:                                    # pragma: no cover - safety net
                log.exception("Unexpected job failure")
                job.error, job.state = "Something went wrong while processing. Please try again.", "error"

        threading.Thread(target=runner, daemon=True).start()
        return job

    def get_job(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs.get(job_id)
        if not job:
            raise ApiError(404, "Unknown job.")
        return job


# --------------------------------------------------------------------------- request helpers

def parse_settings(data: dict) -> silence.Settings:
    try:
        s = silence.Settings(
            threshold_db=float(data.get("threshold_db", -30)),
            min_silence=float(data.get("min_silence", 0.5)),
            padding=float(data.get("padding", 0.15)),
        )
        return s.validate()
    except (TypeError, ValueError) as exc:
        msg = str(exc)
        raise ApiError(400, msg if "must be between" in msg else "Detection settings must be numbers.")


def video_summary(rec: VideoRecord) -> dict:
    i = rec.info
    return {
        "id": rec.id, "filename": rec.original_name, "duration": i.duration,
        "size_bytes": i.size_bytes, "size_human": fmt_size(i.size_bytes),
        "width": i.width, "height": i.height, "fps": i.fps, "has_audio": i.has_audio,
        "video_codec": i.video_codec, "audio_codec": i.audio_codec,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"DeadAir/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> AppState:
        return self.server.app          # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # quiet by default
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- plumbing
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        """Reject requests whose Host/Origin isn't us (blocks DNS-rebinding / cross-site posts)."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in allowed}

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Bad request.")
        if length > MAX_JSON_BYTES:
            raise ApiError(413, "Request too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Request body must be valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        return data

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                raise ApiError(403, "Forbidden.")
            url = urlparse(self.path)
            route = (method, url.path)
            if route == ("GET", "/"):
                return self._send_file(TEMPLATE_DIR / "index.html")
            if method == "GET" and url.path.startswith("/static/"):
                return self._serve_static(url.path[len("/static/"):])
            if route == ("GET", "/api/status"):
                return self._send_json(200, self._status())
            if route == ("POST", "/api/upload"):
                return self._upload(parse_qs(url.query))
            if route == ("POST", "/api/detect"):
                return self._detect(self._read_json())
            if route == ("POST", "/api/export"):
                return self._export(self._read_json())
            if route == ("POST", "/api/open"):
                return self._open(self._read_json())
            if method == "GET" and url.path.startswith("/api/jobs/"):
                return self._job(url.path.rsplit("/", 1)[-1])
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("Unhandled error")
            self._send_json(500, {"error": "Internal error."})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    # ---- routes
    def _serve_static(self, rel: str) -> None:
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():   # no path traversal
            raise ApiError(404, "Not found.")
        self._send_file(target)

    def _status(self) -> dict:
        return {
            "version": __version__,
            "ffmpeg": bool(find_binary("ffmpeg")),
            "ffprobe": bool(find_binary("ffprobe")),
            "output_dir": str(self.app.output_dir),
            "extensions": sorted(ALLOWED_EXTENSIONS),
            "presets": silence.PRESETS,
            "busy": self.app.busy(),
        }

    def _drain_and_fail(self, status: int, message: str, length: int) -> None:
        """Reject an upload; swallow a bounded amount of the body so the client sees our reply."""
        remaining = min(length, 256 * 1024 * 1024)
        while remaining > 0:
            chunk = self.rfile.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        if length > 256 * 1024 * 1024:
            self.close_connection = True
        raise ApiError(status, message)

    def _upload(self, query: dict) -> None:
        """Raw-body upload: POST /api/upload?filename=clip.mp4 with the file bytes as body."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "Upload must include a Content-Length.")
        name = (query.get("filename") or [""])[0]
        if not has_allowed_extension(name):
            self._drain_and_fail(400, "Unsupported file type. Use MP4, MOV, MKV, WebM or AVI.", length)
        if length <= 0:
            raise ApiError(400, "That file is empty.")
        if self.app.busy():
            self._drain_and_fail(409, "DeadAir is busy. Please wait for the current job to finish.", length)
        if not (find_binary("ffmpeg") and find_binary("ffprobe")):
            self._drain_and_fail(500, "FFmpeg/FFprobe not found. See the README for setup.", length)

        vid = uuid.uuid4().hex
        workdir = self.app.temp_root / vid
        workdir.mkdir(parents=True)
        safe_name = sanitize_filename(name)
        dest = workdir / safe_name                        # uuid dir + sanitised name: no traversal possible
        received = 0
        try:
            with open(dest, "wb") as fh:
                while received < length:
                    chunk = self.rfile.read(min(CHUNK, length - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
            if received != length:
                self.close_connection = True
                raise ApiError(400, "Upload was interrupted. Please try again.")
            try:
                info = video.probe(dest)               # validates with ffprobe
            except video.VideoError as exc:
                raise ApiError(422, str(exc))
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        original = Path(name.replace("\\", "/")).name
        rec = VideoRecord(id=vid, original_name=original, path=dest, workdir=workdir, info=info)
        self.app.register_video(rec)
        self._send_json(200, video_summary(rec))

    def _detect(self, data: dict) -> None:
        rec = self.app.get_video(str(data.get("video_id", "")))
        settings = parse_settings(data)
        if not rec.info.has_audio:
            raise ApiError(422, "This video has no audio track, so there is no silence to detect.")

        def work(job: Job) -> dict:
            raw = video.detect_silence(rec.path, rec.info, settings, on_progress=lambda p: setattr(job, "progress", p))
            plan = silence.build_plan(raw, rec.info.duration, settings)
            rec.detection, rec.detection_settings = plan, settings
            result = plan.to_dict()
            result["settings"] = {"threshold_db": settings.threshold_db, "min_silence": settings.min_silence,
                                  "padding": settings.padding}
            return result

        rec.detection = None
        self._send_json(202, {"job_id": self.app.start_job("detect", work).id})

    def _export(self, data: dict) -> None:
        rec = self.app.get_video(str(data.get("video_id", "")))
        plan = rec.detection
        if plan is None:
            raise ApiError(409, "Run Detect Silence first.")
        if plan.fully_silent:
            raise ApiError(422, "The entire video was detected as silence, so there is nothing to export. "
                                "Try a lower threshold (e.g. -40 dB).")
        if not plan.has_changes:
            raise ApiError(409, "No silence was detected, so no changes are needed.")

        stem = Path(sanitize_filename(rec.original_name)).stem
        out_path = unique_path(self.app.output_dir, f"{stem}_deadair", ".mp4")

        def work(job: Job) -> dict:
            details = video.export(rec.path, rec.info, plan.keep, out_path,
                                   on_progress=lambda p: setattr(job, "progress", p))
            self.app.last_output = out_path
            final = details["final_duration"]
            return {
                "original_duration": rec.info.duration,
                "final_duration": final,
                "removed": max(rec.info.duration - final, 0.0),
                "output_name": out_path.name,
                "output_path": str(out_path),
                "output_dir": str(out_path.parent),
                "output_size_human": fmt_size(details["output_size"]),
                "warnings": details["warnings"],
            }

        self._send_json(202, {"job_id": self.app.start_job("export", work).id})

    def _job(self, job_id: str) -> None:
        job = self.app.get_job(job_id)
        self._send_json(200, {"id": job.id, "kind": job.kind, "state": job.state,
                              "progress": round(job.progress, 4), "result": job.result, "error": job.error})

    def _open(self, data: dict) -> None:
        """Open the most recent export (never an arbitrary client-supplied path)."""
        target = self.app.last_output
        if target is None or not target.exists():
            raise ApiError(404, "There's no exported file to open yet.")
        what = data.get("target")
        if what not in ("file", "folder"):
            raise ApiError(400, "target must be 'file' or 'folder'.")
        try:
            if what == "folder":
                open_in_os(target, select=True)     # Explorer with the file highlighted
            else:
                open_in_os(target)
        except OSError:
            raise ApiError(500, "Couldn't open it. The file is saved at: " + str(target))
        self._send_json(200, {"ok": True})


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # noqa: D401
        """Browsers reset connections all the time (closed tabs); don't print tracebacks for that."""
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(host: str = "127.0.0.1", port: int = 8765, output_dir: Path | None = None) -> ThreadingHTTPServer:
    server = QuietServer((host, port), Handler)
    server.daemon_threads = True
    server.app = AppState(output_dir)           # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="DeadAir - remove dead air, keep the story.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    server = None
    for port in range(args.port, args.port + 20):          # first free port starting at --port
        try:
            server = make_server(port=port)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"Couldn't bind to any port in {args.port}-{args.port + 19}.")

    atexit.register(server.app.cleanup)                    # type: ignore[attr-defined]
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if not (find_binary("ffmpeg") and find_binary("ffprobe")):
        print("WARNING: FFmpeg/FFprobe not found. Install FFmpeg (see README) and restart DeadAir.")
    print(f"DeadAir {__version__} is running at {url}")
    print(f"Exports are saved to: {server.app.output_dir}")   # type: ignore[attr-defined]
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping DeadAir...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
