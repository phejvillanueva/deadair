"""Tests that run real FFmpeg/FFprobe against synthetic videos with known silence."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import silence, video
from app.silence import Settings, build_plan
from app.video import VideoError
from tests.helpers import FFMPEG, make_silent_video, make_video, sync_mismatches

SILENCES = [(5, 7), (12, 13.5)]          # the example from the spec; 20 s total


def detect(path, **kw):
    s = Settings(kw.get("threshold_db", -30.0), kw.get("min_silence", 0.5), kw.get("padding", 0.15))
    info = video.probe(path)
    raw = video.detect_silence(path, info, s)
    return info, build_plan(raw, info.duration, s)


class VideoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="deadair_test_"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class ProbeTests(VideoTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mp4 = make_video(cls.tmp / "valid.mp4", 6, [(2, 3)])

    def test_valid_mp4_metadata(self):
        info = video.probe(self.mp4)
        self.assertAlmostEqual(info.duration, 6.0, delta=0.1)
        self.assertEqual((info.width, info.height), (320, 240))
        self.assertAlmostEqual(info.fps, 25.0, places=2)
        self.assertTrue(info.has_audio)
        self.assertTrue(info.cfr)
        self.assertEqual(info.video_codec, "h264")
        self.assertGreater(info.size_bytes, 1000)

    def test_unsupported_extension(self):
        p = self.tmp / "clip.txt"
        shutil.copy(self.mp4, p)
        with self.assertRaisesRegex(VideoError, "Unsupported"):
            video.probe(p)

    def test_text_file_renamed_mp4_is_rejected(self):
        p = self.tmp / "fake.mp4"
        p.write_text("this is definitely not a video")
        with self.assertRaises(VideoError):
            video.probe(p)

    def test_corrupt_random_bytes(self):
        p = self.tmp / "noise.mkv"
        p.write_bytes(bytes(range(256)) * 400)
        with self.assertRaises(VideoError):
            video.probe(p)

    def test_truncated_file(self):
        p = self.tmp / "trunc.mp4"
        p.write_bytes(self.mp4.read_bytes()[:300])
        with self.assertRaises(VideoError):
            video.probe(p)

    def test_missing_file(self):
        with self.assertRaises(VideoError):
            video.probe(self.tmp / "nope.mp4")

    def test_audio_only_file_has_no_video_stream(self):
        p = self.tmp / "audio_only.mkv"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=2", str(p)], check=True)
        with self.assertRaisesRegex(VideoError, "No video stream"):
            video.probe(p)

    def test_no_audio_stream(self):
        p = make_video(self.tmp / "silentfilm.mp4", 4, audio=False)
        info = video.probe(p)
        self.assertFalse(info.has_audio)
        with self.assertRaisesRegex(VideoError, "no audio"):
            video.detect_silence(p, info, Settings())

    def test_webm_without_header_duration(self):
        """Browser-recorded WebM often has no duration in the header; we measure it instead."""
        src = make_video(self.tmp / "dur_src.mp4", 5, [])
        p = self.tmp / "noduration.webm"
        # Writing to a pipe makes the output non-seekable, so no duration is stored in the header.
        data = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-c:v", "libvpx", "-c:a", "libvorbis",
                               "-f", "webm", "-"], check=True, capture_output=True).stdout
        p.write_bytes(data)
        self.assertIsNone(video._float(__import__("json").loads(subprocess.run(
            [video._require("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", str(p)],
            capture_output=True, text=True).stdout)["format"].get("duration")))
        info = video.probe(p)
        self.assertAlmostEqual(info.duration, 5.0, delta=0.3)


class DetectionTests(VideoTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.src = make_video(cls.tmp / "known.mp4", 20, SILENCES, noise_floor=True)   # -50 dB room tone

    def assertRanges(self, removed, expected, tol=0.06):
        self.assertEqual(len(removed), len(expected), f"{removed} vs {expected}")
        for (s, e), (es, ee) in zip(removed, expected):
            self.assertAlmostEqual(s, es, delta=tol)
            self.assertAlmostEqual(e, ee, delta=tol)

    def test_detects_known_ranges_from_real_audio(self):
        info, plan = detect(self.src, padding=0.0)
        self.assertRanges(plan.removed, SILENCES)
        self.assertAlmostEqual(plan.final_duration, 16.5, delta=0.1)
        self.assertEqual(len(plan.keep), 3)

    def test_padding_applied(self):
        _, plan = detect(self.src, padding=0.25)
        self.assertRanges(plan.removed, [(5.25, 6.75), (12.25, 13.25)])

    def test_threshold_handling(self):
        # Room tone is about -50 dB: below a -40 dB threshold it IS silence, above a -60 dB one it is NOT.
        _, quiet = detect(self.src, threshold_db=-40, padding=0)
        _, strict = detect(self.src, threshold_db=-60, padding=0)
        self.assertEqual(len(quiet.removed), 2)
        self.assertEqual(strict.removed, [])
        self.assertFalse(strict.has_changes)

    def test_minimum_duration_handling(self):
        _, p15 = detect(self.src, min_silence=1.5, padding=0)
        _, p18 = detect(self.src, min_silence=1.8, padding=0)
        _, p21 = detect(self.src, min_silence=2.1, padding=0)
        self.assertEqual(len(p15.removed), 2)
        self.assertRanges(p18.removed, [(5, 7)])
        self.assertEqual(p21.removed, [])

    def test_silence_at_beginning_and_end(self):
        p = make_video(self.tmp / "edges.mp4", 10, [(0, 2), (8, 11)])
        _, plan = detect(p, padding=0.15)
        self.assertEqual(len(plan.removed), 2)
        self.assertEqual(plan.removed[0][0], 0.0)
        self.assertAlmostEqual(plan.removed[0][1], 2 - 0.15, delta=0.08)
        self.assertAlmostEqual(plan.removed[1][0], 8 + 0.15, delta=0.08)
        self.assertEqual(plan.removed[1][1], 10)

    def test_no_silence(self):
        p = make_video(self.tmp / "talky.mp4", 6, [])
        _, plan = detect(p)
        self.assertFalse(plan.has_changes)
        self.assertEqual(plan.keep, [(0, plan.duration)])

    def test_completely_silent_video(self):
        p = make_silent_video(self.tmp / "dead.mp4", 5)
        info, plan = detect(p)
        self.assertTrue(plan.fully_silent)
        with self.assertRaisesRegex(VideoError, "entire video"):
            video.export(p, info, plan.keep, self.tmp / "never.mp4")
        self.assertFalse((self.tmp / "never.mp4").exists())

    def test_different_sample_rates(self):
        for rate in (22050, 48000):
            p = make_video(self.tmp / f"sr{rate}.mkv", 8, [(3, 5)], sample_rate=rate)
            _, plan = detect(p, padding=0)
            self.assertRanges(plan.removed, [(3, 5)], tol=0.1)

    def test_extremely_short_video(self):
        p = make_video(self.tmp / "tiny.mp4", 0.3, [])
        info, plan = detect(p)
        self.assertFalse(plan.has_changes)
        self.assertGreater(info.duration, 0.2)


class ExportTests(VideoTestCase):
    def export(self, src, plan_info, out_name):
        info, plan = plan_info
        out = self.tmp / out_name
        progress = []
        result = video.export(src, info, plan.keep, out, progress.append)
        return info, plan, out, result, progress

    def test_export_removes_timeline_sections_and_stays_in_sync(self):
        src = make_video(self.tmp / "e1.mp4", 20, SILENCES, noise_floor=True)
        info, plan, out, result, progress = self.export(src, detect(src, padding=0.0), "e1_out.mp4")

        self.assertTrue(out.exists())
        self.assertFalse(out.with_name("e1_out.partial.mp4").exists())
        out_info = video.probe(out)                                  # readable by ffprobe
        self.assertLess(out_info.duration, info.duration)
        self.assertAlmostEqual(out_info.duration, 16.5, delta=0.15)  # real removal, not muting
        self.assertAlmostEqual(result["final_duration"], out_info.duration, places=3)
        self.assertTrue(out_info.has_audio)
        self.assertEqual((out_info.width, out_info.height), (320, 240))
        self.assertAlmostEqual(out_info.fps, 25.0, places=2)
        self.assertAlmostEqual(out_info.audio_duration, out_info.video_duration, delta=0.1)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["segments"], 3)

        # Frame-count check: no frozen/duplicated/missing frames. 16.5 s * 25 fps = 412.5 frames.
        frames = int(subprocess.run(
            [video._require("ffprobe"), "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True).stdout.strip())
        self.assertAlmostEqual(frames, 412, delta=3)

        # Real progress: monotonic, ends at 1.0, strictly increasing at least once before the end.
        self.assertEqual(progress[-1], 1.0)
        self.assertEqual(progress, sorted(progress))

        # A/V sync: loud audio must still coincide with bright video after every cut.
        mism, compared = sync_mismatches(out, 25)
        self.assertGreater(compared, 150)
        self.assertEqual(mism, 0, f"{mism}/{compared} frames out of sync")

    def test_exported_video_contains_no_remaining_dead_air(self):
        src = make_video(self.tmp / "e2.mp4", 20, SILENCES, noise_floor=True)
        _, _, out, _, _ = self.export(src, detect(src), "e2_out.mp4")
        info = video.probe(out)
        raw = video.detect_silence(out, info, Settings(-30, 0.5, 0.15))
        self.assertEqual(build_plan(raw, info.duration, Settings()).removed, [])

    def test_export_with_padding_final_duration(self):
        src = make_video(self.tmp / "e3.mp4", 20, SILENCES, noise_floor=True)
        info, plan, out, _, _ = self.export(src, detect(src, padding=0.15), "e3_out.mp4")
        self.assertAlmostEqual(video.probe(out).duration, plan.final_duration, delta=0.15)
        self.assertAlmostEqual(plan.final_duration, 17.1, delta=0.1)

    def test_silence_at_start_and_end_export(self):
        src = make_video(self.tmp / "e4.mp4", 10, [(0, 2), (8, 11)])
        info, plan, out, _, _ = self.export(src, detect(src), "e4_out.mp4")
        self.assertAlmostEqual(video.probe(out).duration, plan.final_duration, delta=0.15)
        self.assertLess(video.probe(out).duration, 7)
        mism, compared = sync_mismatches(out, 25)
        self.assertEqual(mism, 0)

    def test_many_cuts(self):
        sil = [(i * 3 + 1.5, i * 3 + 2.4) for i in range(19)]          # 19 pauses in 60 s
        src = make_video(self.tmp / "many.mp4", 60, sil, noise_floor=True)
        info, plan, out, result, _ = self.export(src, detect(src, padding=0.1), "many_out.mp4")
        self.assertGreaterEqual(len(plan.removed), 18)
        out_dur = video.probe(out).duration
        # The export honours the frame-snapped plan exactly...
        self.assertAlmostEqual(out_dur, result["expected_duration"], delta=0.06)
        # ...and snapping moves each of the 2 boundaries per kept segment by at most half a frame.
        self.assertLessEqual(abs(out_dur - plan.final_duration), 0.02 * 2 * len(plan.keep))
        mism, compared = sync_mismatches(out, 25)
        self.assertLessEqual(mism, 2, f"{mism}/{compared} out of sync")

    def test_all_container_formats(self):
        for ext in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
            with self.subTest(container=ext):
                src = make_video(self.tmp / f"c{ext[1:]}{ext}", 8, [(3, 5)])
                info = video.probe(src)
                _, plan = detect(src, padding=0)
                self.assertEqual(len(plan.removed), 1)
                out = self.tmp / f"c{ext[1:]}_out.mp4"
                video.export(src, info, plan.keep, out)
                out_info = video.probe(out)
                self.assertAlmostEqual(out_info.duration, 6.0, delta=0.2)
                self.assertTrue(out_info.has_audio)

    def test_odd_dimensions_are_made_encodable(self):
        src = make_video(self.tmp / "odd.mkv", 8, [(3, 5)], size="321x241", vcodec="ffv1", pix_fmt="yuv444p")
        info = video.probe(src)
        self.assertEqual((info.width, info.height), (321, 241))
        _, plan = detect(src, padding=0)
        out = self.tmp / "odd_out.mp4"
        video.export(src, info, plan.keep, out)
        o = video.probe(out)
        self.assertEqual((o.width % 2, o.height % 2), (0, 0))
        self.assertAlmostEqual(o.duration, 6.0, delta=0.2)

    def test_variable_frame_rate_source(self):
        cfr = make_video(self.tmp / "vfr_src.mp4", 10, [(4, 6)])
        vfr = self.tmp / "vfr.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(cfr), "-vf", "select='lt(mod(n,5),3)'",
                        "-fps_mode", "vfr", "-c:v", "libx264", "-c:a", "copy", str(vfr)], check=True)
        info = video.probe(vfr)
        _, plan = detect(vfr, padding=0)
        out = self.tmp / "vfr_out.mp4"
        video.export(vfr, info, plan.keep, out)
        o = video.probe(out)
        self.assertAlmostEqual(o.duration, 8.0, delta=0.3)
        self.assertAlmostEqual(o.audio_duration, o.video_duration, delta=0.15)

    def test_filter_graph_structure(self):
        g = video.build_filter_graph([(0.0, 5.0), (7.0, None)], fix_odd_size=False)
        self.assertIn("trim=start=0.000000:end=5.000000", g)
        self.assertIn("atrim=start=7.000000", g)
        self.assertIn("concat=n=2:v=1:a=1", g)
        single = video.build_filter_graph([(1.0, None)], fix_odd_size=True)
        self.assertNotIn("split", single)
        self.assertIn("trunc(iw/2)*2", single)

    def test_snap_segments_whole_frames(self):
        info = video.VideoInfo(20, 0, 320, 240, 25.0, True, "h264", "aac", cfr=True)
        segs = video.snap_segments([(0, 5.016), (7.012, 12.005), (13.514, 20)], info)
        self.assertEqual(segs[0][0], 0.0)
        self.assertEqual(segs[-1][1], None)
        for s, e in segs[1:-1]:
            self.assertAlmostEqual((s * 25 + 0.5) % 1, 0, places=6)   # lands on half-frame boundary
        self.assertEqual(video.snap_segments([(1.0, 1.01)], info), [])  # sub-frame segment dropped


if __name__ == "__main__":
    unittest.main()
