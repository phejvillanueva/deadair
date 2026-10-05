"""Pure-logic tests: silencedetect parsing, thresholds, min duration, padding, inversion."""

import unittest
from pathlib import Path

from app import silence
from app.silence import Settings, build_plan, invert_intervals, merge_intervals, parse_silencedetect
from app.utils import fmt_clock, fmt_size, sanitize_filename, unique_path

import tempfile

FFMPEG_LOG = """\
[silencedetect @ 0x55d0c8a2b6c0] silence_start: 5.01551
[silencedetect @ 0x55d0c8a2b6c0] silence_end: 7.01243 | silence_duration: 1.99692
size=N/A time=00:00:20.00 bitrate=N/A speed= 400x
[silencedetect @ 0x55d0c8a2b6c0] silence_start: 12.0047
[silencedetect @ 0x55d0c8a2b6c0] silence_end: 13.514 | silence_duration: 1.5093
[Parsed_volume_0 @ 0x1] some unrelated line silence_start: 99
"""


def no_pad(**kw):
    return Settings(kw.get("threshold_db", -30.0), kw.get("min_silence", 0.5), 0.0)


class ParseTests(unittest.TestCase):
    def test_parses_pairs_and_ignores_noise(self):
        self.assertEqual(parse_silencedetect(FFMPEG_LOG), [(5.01551, 7.01243), (12.0047, 13.514)])

    def test_unterminated_silence_runs_to_end(self):
        log = "[silencedetect @ 0x1] silence_start: 8.5\n"
        self.assertEqual(parse_silencedetect(log), [(8.5, None)])

    def test_negative_and_scientific_numbers(self):
        log = ("[silencedetect @ 0x1] silence_start: -0.0123\n"
               "[silencedetect @ 0x1] silence_end: 1.5e0 | silence_duration: 1.51\n")
        self.assertEqual(parse_silencedetect(log), [(-0.0123, 1.5)])

    def test_empty(self):
        self.assertEqual(parse_silencedetect(""), [])


class IntervalTests(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(merge_intervals([(5, 6), (1, 2), (1.5, 3), (6, 7)]), [(1, 3), (5, 7)])

    def test_invert(self):
        self.assertEqual(invert_intervals([(5, 7), (12, 13.5)], 20), [(0, 5), (7, 12), (13.5, 20)])
        self.assertEqual(invert_intervals([], 10), [(0, 10)])
        self.assertEqual(invert_intervals([(0, 10)], 10), [])


class PlanTests(unittest.TestCase):
    def test_spec_example(self):
        """0-5 speech, 5-7 silence, 7-12 speech, 12-13.5 silence, 13.5-20 speech -> 16.5 s."""
        plan = build_plan([(5, 7), (12, 13.5)], 20, no_pad())
        self.assertEqual(plan.removed, [(5, 7), (12, 13.5)])
        self.assertEqual(plan.keep, [(0, 5), (7, 12), (13.5, 20)])
        self.assertAlmostEqual(plan.final_duration, 16.5)
        self.assertAlmostEqual(plan.total_removed, 3.5)

    def test_multiple_intervals(self):
        plan = build_plan([(1, 2), (4, 5), (8, 9.5)], 12, no_pad(min_silence=0.5))
        self.assertEqual(len(plan.removed), 3)
        self.assertAlmostEqual(plan.final_duration, 12 - 3.5)

    def test_silence_at_beginning(self):
        plan = build_plan([(0, 2)], 10, Settings(-30, 0.5, 0.15))
        self.assertEqual(plan.removed, [(0, 2 - 0.15)])      # no padding at the file edge
        self.assertEqual(plan.keep[0][0], 2 - 0.15)

    def test_silence_at_end_unterminated(self):
        plan = build_plan([(8, None)], 10, Settings(-30, 0.5, 0.15))
        self.assertAlmostEqual(plan.removed[0][0], 8.15)
        self.assertEqual(plan.removed[0][1], 10)
        self.assertEqual(plan.keep, [(0, 8.15)])

    def test_silence_in_middle_with_padding(self):
        plan = build_plan([(5, 7)], 20, Settings(-30, 0.5, 0.15))
        self.assertAlmostEqual(plan.removed[0][0], 5.15)
        self.assertAlmostEqual(plan.removed[0][1], 6.85)

    def test_minimum_duration_filter(self):
        plan = build_plan([(1, 1.4), (3, 4)], 10, no_pad(min_silence=0.5))
        self.assertEqual(plan.removed, [(3, 4)])

    def test_padding_can_swallow_short_silences(self):
        # 0.6 s pause, 0.3 s kept each side -> nothing left to remove
        plan = build_plan([(5, 5.6)], 10, Settings(-30, 0.5, 0.3))
        self.assertFalse(plan.has_changes)
        self.assertEqual(plan.keep, [(0, 10)])

    def test_overlapping_raw_intervals_merge(self):
        plan = build_plan([(2, 4), (3, 6), (6, 7)], 20, no_pad())
        self.assertEqual(plan.removed, [(2, 7)])

    def test_padding_with_close_silences_never_overlaps(self):
        raw = [(1, 1.6), (1.8, 2.5), (2.7, 3.4), (3.5, 6)]
        for pad in (0.0, 0.1, 0.15, 0.25, 0.5):
            plan = build_plan(raw, 10, Settings(-30, 0.3, pad))
            last_end = -1
            for s, e in plan.removed:
                self.assertGreater(e, s)
                self.assertGreaterEqual(s, last_end)
                last_end = e
            # keep + removed always tile the whole file
            self.assertAlmostEqual(plan.final_duration + plan.total_removed, 10)

    def test_tiny_keep_sliver_is_absorbed(self):
        plan = build_plan([(1, 3), (3.02, 5)], 10, no_pad())      # 20 ms gap between silences
        self.assertEqual(plan.removed, [(1, 5)])

    def test_no_silence(self):
        plan = build_plan([], 12, Settings())
        self.assertFalse(plan.has_changes)
        self.assertFalse(plan.fully_silent)
        self.assertEqual(plan.keep, [(0, 12)])
        self.assertEqual(plan.final_duration, 12)

    def test_all_silence(self):
        plan = build_plan([(0, None)], 10, Settings())
        self.assertTrue(plan.fully_silent)
        self.assertEqual(plan.keep, [])
        self.assertEqual(plan.final_duration, 0)

    def test_all_silence_with_ffmpeg_slop(self):
        plan = build_plan([(-0.002, 10.004)], 10, Settings())     # clamped to the file
        self.assertTrue(plan.fully_silent)

    def test_extremely_short_video_does_not_break(self):
        plan = build_plan([], 0.02, Settings())
        self.assertEqual(plan.keep, [(0, 0.02)])
        self.assertEqual(build_plan([], 0, Settings()).keep, [])

    def test_to_dict(self):
        d = build_plan([(5, 7)], 20, no_pad()).to_dict()
        self.assertEqual(d["silences"][0], {"start": 5, "end": 7, "duration": 2})
        self.assertTrue(d["has_changes"])


class SettingsTests(unittest.TestCase):
    def test_defaults_match_balanced_preset(self):
        d = Settings()
        b = silence.PRESETS["balanced"]
        self.assertEqual((d.threshold_db, d.min_silence, d.padding), (b["threshold_db"], b["min_silence"], b["padding"]))

    def test_presets(self):
        self.assertEqual(silence.PRESETS["natural"], {"threshold_db": -35.0, "min_silence": 0.7, "padding": 0.25})
        self.assertEqual(silence.PRESETS["aggressive"], {"threshold_db": -25.0, "min_silence": 0.3, "padding": 0.10})

    def test_options_cover_spec(self):
        self.assertEqual(silence.THRESHOLD_OPTIONS, [-20, -25, -30, -35, -40])
        self.assertEqual(silence.MIN_SILENCE_OPTIONS, [0.3, 0.5, 0.7, 1.0, 1.5, 2.0])
        self.assertEqual(silence.PADDING_OPTIONS, [0, 0.1, 0.15, 0.25, 0.5])

    def test_validation(self):
        for bad in (Settings(-3, 0.5, 0.1), Settings(-200, 0.5, 0.1), Settings(-30, 0, 0.1),
                    Settings(-30, 0.5, -1), Settings(-30, 0.5, 99)):
            with self.assertRaises(ValueError):
                bad.validate()
        Settings(-40, 2.0, 0.5).validate()


class UtilsTests(unittest.TestCase):
    def test_sanitize_blocks_traversal_and_bad_chars(self):
        self.assertEqual(sanitize_filename("../../etc/passwd.mp4"), "passwd.mp4")
        self.assertEqual(sanitize_filename("..\\..\\windows\\evil.mov"), "evil.mov")
        self.assertEqual(sanitize_filename('a<b>c:d"e|f?g*.mkv'), "a_b_c_d_e_f_g_.mkv")
        self.assertEqual(sanitize_filename("   ...   .mp4"), "video.mp4")
        self.assertTrue(sanitize_filename("CON.mp4").startswith("_"))
        self.assertTrue(sanitize_filename("nul").startswith("_"))
        self.assertLessEqual(len(sanitize_filename("x" * 500 + ".mp4")), 105)
        for nasty in ("../x.mp4", "/abs/x.mp4", "C:\\x.mp4", "x\x00.mp4", "-rf.mp4"):
            out = sanitize_filename(nasty)
            self.assertNotIn("/", out)
            self.assertNotIn("\\", out)
            self.assertNotIn(":", out)

    def test_fmt_clock(self):
        self.assertEqual(fmt_clock(5.2), "00:05.20")
        self.assertEqual(fmt_clock(74.5), "01:14.50")
        self.assertEqual(fmt_clock(3723.5), "1:02:03.50")

    def test_fmt_size(self):
        self.assertEqual(fmt_size(512), "512 B")
        self.assertEqual(fmt_size(1536), "1.5 KB")

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p1 = unique_path(d, "clip_deadair", ".mp4")
            p1.write_text("x")
            self.assertEqual(unique_path(d, "clip_deadair", ".mp4").name, "clip_deadair_2.mp4")


if __name__ == "__main__":
    unittest.main()
