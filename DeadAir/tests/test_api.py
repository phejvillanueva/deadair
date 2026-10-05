"""API tests: a real DeadAir server on a random port, real uploads, real FFmpeg."""

import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app import main as app_main
from app import video
from tests.helpers import make_silent_video, make_video

SILENCES = [(5, 7), (12, 13.5)]


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="deadair_api_"))
        cls.out_dir = cls.tmp / "output"
        cls.server = app_main.make_server(port=0, output_dir=cls.out_dir)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.talky = make_video(cls.tmp / "talky.mp4", 20, SILENCES, noise_floor=True)
        cls.nosilence = make_video(cls.tmp / "nosilence.mp4", 6, [])
        cls.dead = make_silent_video(cls.tmp / "dead.mp4", 5)
        cls.nosound = make_video(cls.tmp / "nosound.mp4", 4, audio=False)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.app.cleanup()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- tiny client
    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return resp.status, data

    def post_json(self, path, payload):
        return self.request("POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json"})

    def upload(self, file: Path, filename: str | None = None):
        data = file.read_bytes() if isinstance(file, Path) else file
        name = filename or file.name
        from urllib.parse import quote
        return self.request("POST", f"/api/upload?filename={quote(name)}", data,
                            {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))})

    def wait_job(self, job_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            status, job = self.request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            if job["state"] != "running":
                return job
            time.sleep(0.1)
        self.fail("job timed out")

    def detect(self, vid, **settings):
        status, data = self.post_json("/api/detect", {"video_id": vid, **settings})
        self.assertEqual(status, 202, data)
        return self.wait_job(data["job_id"])

    def load(self, file):
        status, info = self.upload(file)
        self.assertEqual(status, 200, info)
        return info


class PageTests(ApiTestCase):
    def test_index_and_assets_served(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"DeadAir", body)
        self.assertIn(b"Remove dead air. Keep the story.", body)
        for needle in (b"Detect Silence", b"Export Clean Video", b"Open Folder", b"Drop your video here"):
            self.assertIn(needle, body)
        self.assertEqual(self.request("GET", "/static/app.js")[0], 200)
        self.assertEqual(self.request("GET", "/static/app.css")[0], 200)

    def test_static_path_traversal_blocked(self):
        for path in ("/static/../main.py", "/static/%2e%2e/main.py", "/static/..%2fmain.py",
                     "/static/../../README.md", "/static//etc/passwd"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_status(self):
        status, data = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["ffmpeg"] and data["ffprobe"])
        self.assertEqual(data["presets"]["balanced"]["threshold_db"], -30)

    def test_wrong_host_header_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/status", headers={"Host": "evil.example.com"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_cross_origin_post_rejected(self):
        status, _ = self.request("POST", "/api/detect", b"{}", {"Origin": "http://evil.example.com"})
        self.assertEqual(status, 403)

    def test_unknown_route(self):
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)


class UploadTests(ApiTestCase):
    def test_valid_upload_returns_metadata(self):
        status, info = self.upload(self.talky)
        self.assertEqual(status, 200)
        self.assertEqual(info["filename"], "talky.mp4")
        self.assertAlmostEqual(info["duration"], 20, delta=0.1)
        self.assertEqual((info["width"], info["height"]), (320, 240))
        self.assertAlmostEqual(info["fps"], 25, places=1)
        self.assertGreater(info["size_bytes"], 0)
        self.assertTrue(info["size_human"].endswith(("KB", "MB")))
        self.assertTrue(info["has_audio"])

    def test_unsupported_extension(self):
        status, data = self.upload(b"hello", "notes.txt")
        self.assertEqual(status, 400)
        self.assertIn("Unsupported", data["error"])

    def test_fake_video_rejected_by_ffprobe(self):
        status, data = self.upload(b"definitely not a video" * 100, "fake.mp4")
        self.assertEqual(status, 422)
        self.assertIn("error", data)

    def test_empty_upload(self):
        self.assertEqual(self.upload(b"", "empty.mp4")[0], 400)

    def test_missing_content_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest("POST", "/api/upload?filename=a.mp4")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"0\r\n\r\n")
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()

    def test_hostile_filename_stays_inside_temp_dir(self):
        status, info = self.upload(self.talky, "../../../evil name?.mp4")
        self.assertEqual(status, 200)
        rec = self.server.app.get_video(info["id"])
        self.assertEqual(rec.path.parent, rec.workdir)
        self.assertIn(self.server.app.temp_root, rec.path.parents)
        self.assertEqual(rec.path.name, "evil name_.mp4")
        self.assertEqual(info["filename"], "evil name?.mp4")
        self.assertFalse((self.tmp.parent / "evil name_.mp4").exists())

    def test_new_upload_cleans_old_temp_copy(self):
        a = self.load(self.nosilence)
        old_dir = self.server.app.get_video(a["id"]).workdir
        self.load(self.talky)
        self.assertFalse(old_dir.exists())

    def test_no_audio_video_loads_but_cannot_detect(self):
        info = self.load(self.nosound)
        self.assertFalse(info["has_audio"])
        status, data = self.post_json("/api/detect", {"video_id": info["id"]})
        self.assertEqual(status, 422)
        self.assertIn("no audio", data["error"])


class DetectTests(ApiTestCase):
    def test_detect_returns_real_timestamps(self):
        info = self.load(self.talky)
        job = self.detect(info["id"], threshold_db=-30, min_silence=0.5, padding=0)
        self.assertEqual(job["state"], "done", job)
        r = job["result"]
        self.assertEqual(len(r["silences"]), 2)
        for got, (es, ee) in zip(r["silences"], SILENCES):
            self.assertAlmostEqual(got["start"], es, delta=0.06)
            self.assertAlmostEqual(got["end"], ee, delta=0.06)
        self.assertAlmostEqual(r["total_silence"], 3.5, delta=0.1)
        self.assertAlmostEqual(r["final_duration"], 16.5, delta=0.1)
        self.assertTrue(r["has_changes"])

    def test_detect_defaults_when_settings_omitted(self):
        info = self.load(self.talky)
        job = self.detect(info["id"])
        self.assertEqual(job["result"]["settings"], {"threshold_db": -30.0, "min_silence": 0.5, "padding": 0.15})

    def test_invalid_settings(self):
        info = self.load(self.talky)
        for bad in ({"threshold_db": "loud"}, {"threshold_db": 5}, {"min_silence": -1}, {"padding": 99}):
            status, data = self.post_json("/api/detect", {"video_id": info["id"], **bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("error", data)

    def test_unknown_video(self):
        self.assertEqual(self.post_json("/api/detect", {"video_id": "nope"})[0], 404)

    def test_bad_json(self):
        status, data = self.request("POST", "/api/detect", b"{not json", {})
        self.assertEqual(status, 400)

    def test_no_silence_reports_no_changes(self):
        info = self.load(self.nosilence)
        r = self.detect(info["id"])["result"]
        self.assertFalse(r["has_changes"])
        self.assertEqual(r["silences"], [])

    def test_silent_video_reported(self):
        info = self.load(self.dead)
        r = self.detect(info["id"])["result"]
        self.assertTrue(r["fully_silent"])


class ExportTests(ApiTestCase):
    def test_export_before_detect_is_refused(self):
        info = self.load(self.talky)
        status, data = self.post_json("/api/export", {"video_id": info["id"]})
        self.assertEqual(status, 409)
        self.assertIn("Detect", data["error"])

    def test_export_refused_when_no_silence(self):
        info = self.load(self.nosilence)
        self.detect(info["id"])
        status, data = self.post_json("/api/export", {"video_id": info["id"]})
        self.assertEqual(status, 409)
        self.assertIn("no changes", data["error"].lower())

    def test_export_refused_when_entire_video_silent(self):
        info = self.load(self.dead)
        self.detect(info["id"])
        status, data = self.post_json("/api/export", {"video_id": info["id"]})
        self.assertEqual(status, 422)
        self.assertIn("entire video", data["error"])
        self.assertEqual(list(self.out_dir.glob("dead_deadair*")), [])     # no broken zero-length file

    def test_full_workflow_and_open_buttons(self):
        info = self.load(self.talky)
        self.assertEqual(self.post_json("/api/open", {"target": "folder"})[0] in (404, 200), True)
        self.detect(info["id"], threshold_db=-30, min_silence=0.5, padding=0.15)
        status, data = self.post_json("/api/export", {"video_id": info["id"]})
        self.assertEqual(status, 202, data)

        # progress is real and monotonic
        seen = []
        end = time.time() + 120
        while time.time() < end:
            _, job = self.request("GET", f"/api/jobs/{data['job_id']}")
            seen.append(job["progress"])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(seen, sorted(seen))

        r = job["result"]
        out = Path(r["output_path"])
        self.assertTrue(out.exists())
        self.assertEqual(out.parent, self.out_dir.resolve())
        self.assertEqual(out.name, "talky_deadair.mp4")
        self.assertAlmostEqual(r["original_duration"], 20, delta=0.1)
        self.assertAlmostEqual(r["final_duration"], 17.1, delta=0.15)
        self.assertAlmostEqual(r["removed"], 2.9, delta=0.15)
        self.assertLess(r["final_duration"], r["original_duration"])
        self.assertEqual(r["warnings"], [])
        info_out = video.probe(out)                               # readable by ffprobe, audio + video present
        self.assertTrue(info_out.has_audio)
        self.assertAlmostEqual(info_out.duration, r["final_duration"], places=3)

        with mock.patch.object(app_main, "open_in_os") as opener:
            self.assertEqual(self.post_json("/api/open", {"target": "file"})[0], 200)
            self.assertEqual(self.post_json("/api/open", {"target": "folder"})[0], 200)
            self.assertEqual(self.post_json("/api/open", {"target": "../../etc"})[0], 400)
        self.assertEqual(opener.call_args_list[0], mock.call(out))
        self.assertEqual(opener.call_args_list[1], mock.call(out, select=True))

    def test_second_export_does_not_overwrite(self):
        info = self.load(self.talky)
        self.detect(info["id"])
        names = []
        for _ in range(2):
            status, data = self.post_json("/api/export", {"video_id": info["id"]})
            self.assertEqual(status, 202)
            names.append(self.wait_job(data["job_id"])["result"]["output_name"])
        self.assertEqual(len(set(names)), 2)

    def test_failed_ffmpeg_gives_clear_error_and_no_output(self):
        info = self.load(self.talky)
        self.detect(info["id"])
        before = set(self.out_dir.glob("*"))
        rec = self.server.app.get_video(info["id"])
        rec.path.write_bytes(b"corrupted after upload")           # simulate the file breaking mid-session
        status, data = self.post_json("/api/export", {"video_id": info["id"]})
        job = self.wait_job(data["job_id"])
        self.assertEqual(job["state"], "error")
        self.assertIn("FFmpeg", job["error"])
        self.assertEqual(set(self.out_dir.glob("*")), before)     # no partial files left behind

    def test_open_before_any_export(self):
        fresh = app_main.AppState(self.tmp / "o2")
        self.assertIsNone(fresh.last_output)
        fresh.cleanup()


class BusyTests(ApiTestCase):
    def test_duplicate_processing_is_prevented(self):
        info = self.load(self.talky)
        release = threading.Event()
        real = video.detect_silence

        def slow(*a, **kw):
            release.wait(30)
            return real(*a, **kw)

        with mock.patch.object(video, "detect_silence", slow):       # only to hold the job open deterministically
            status, first = self.post_json("/api/detect", {"video_id": info["id"]})
            self.assertEqual(status, 202)
            status, data = self.post_json("/api/detect", {"video_id": info["id"]})
            self.assertEqual(status, 409)
            self.assertIn("already working", data["error"])
            self.assertEqual(self.upload(self.talky)[0], 409)          # uploads are blocked too
            self.assertTrue(self.request("GET", "/api/status")[1]["busy"])
            release.set()
            self.assertEqual(self.wait_job(first["job_id"])["state"], "done")
        self.assertFalse(self.request("GET", "/api/status")[1]["busy"])


class CleanupTests(unittest.TestCase):
    def test_cleanup_removes_temp_root(self):
        d = Path(tempfile.mkdtemp())
        state = app_main.AppState(d / "out")
        (state.temp_root / "x").mkdir()
        state.cleanup()
        self.assertFalse(state.temp_root.exists())
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
