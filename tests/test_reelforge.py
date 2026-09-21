"""Unit tests for the pure logic - no ffmpeg, no network."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelforge import analyze, captions, ingest, recipe, render, sheet, visuals


class TestUrlClassification(unittest.TestCase):
    def test_instagram_reel(self):
        # Share links carry a tracking query string; it must not leak into the
        # post id or the project slug.
        platform, post = ingest.classify_url(
            "https://www.instagram.com/reel/Ab1cD2eF3gH/?stkn=EXAMPLETOKEN=="
        )
        self.assertEqual(platform, "instagram")
        self.assertEqual(post, "Ab1cD2eF3gH")

    def test_instagram_post_and_tiktok_and_youtube(self):
        cases = [
            ("https://instagram.com/p/ABC123xyz/", "instagram", "ABC123xyz"),
            ("https://www.tiktok.com/@someone/video/7412345678901234567",
             "tiktok", "7412345678901234567"),
            ("https://youtube.com/shorts/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
        ]
        for url, platform, post in cases:
            with self.subTest(url=url):
                self.assertEqual(ingest.classify_url(url), (platform, post))

    def test_unknown_host_falls_back_to_hostname(self):
        platform, post = ingest.classify_url("https://vimeo.com/123456")
        self.assertEqual(platform, "vimeo-com")
        self.assertIsNone(post)

    def test_is_url(self):
        self.assertTrue(ingest.is_url("https://x.com/a"))
        self.assertFalse(ingest.is_url("/Users/me/clip.mp4"))

    def test_slugify_strips_unsafe_characters(self):
        self.assertEqual(ingest.slugify("my reel!! (final)/v2"), "my-reel-final-v2")


class TestSegmentMerging(unittest.TestCase):
    def test_slivers_are_folded_into_neighbours(self):
        # 0.2s and 0.1s gaps are detector noise, not editorial cuts.
        spans = recipe._merge_short_segments([0.2, 0.3, 4.0, 4.1], 10.0)
        for start, end in spans:
            self.assertGreaterEqual(end - start, recipe.MIN_SEGMENT)
        self.assertAlmostEqual(spans[0][0], 0.0)
        self.assertAlmostEqual(spans[-1][1], 10.0)

    def test_no_cuts_yields_one_span(self):
        self.assertEqual(recipe._merge_short_segments([], 8.0), [(0.0, 8.0)])

    def test_spans_are_contiguous_and_cover_duration(self):
        spans = recipe._merge_short_segments([2.0, 5.0, 7.5], 10.0)
        self.assertEqual(spans[0][0], 0.0)
        self.assertEqual(spans[-1][1], 10.0)
        for earlier, later in zip(spans, spans[1:]):
            self.assertEqual(earlier[1], later[0])


class TestRhythm(unittest.TestCase):
    def test_rhythm_buckets(self):
        self.assertEqual(recipe._rhythm(0.8), "fast")
        self.assertEqual(recipe._rhythm(2.0), "medium")
        self.assertEqual(recipe._rhythm(4.0), "slow")


class TestCaptionSeeding(unittest.TestCase):
    def test_asset_segments_start_with_no_caption(self):
        # The supplied diagram carries its own words already.
        self.assertEqual(recipe._starting_text("HOW IT WORKS", True, True), "")

    def test_short_overlay_is_inherited(self):
        self.assertEqual(recipe._starting_text("STOP SCROLLING", False, True),
                         "STOP SCROLLING")

    def test_paragraph_is_not_treated_as_a_caption(self):
        paragraph = " ".join(f"word{i}" for i in range(20))
        self.assertEqual(recipe._starting_text(paragraph, False, True), "")

    def test_blank_text_opt_out(self):
        self.assertEqual(recipe._starting_text("STOP SCROLLING", False, False), "")


class TestOcrCleaning(unittest.TestCase):
    HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"

    def _row(self, block, line, conf, text):
        return f"5\t1\t{block}\t1\t{line}\t1\t0\t0\t10\t10\t{conf}\t{text}"

    def test_low_confidence_words_are_dropped(self):
        tsv = "\n".join([
            self.HEADER,
            self._row(1, 1, 95, "STOP"),
            self._row(1, 1, 91, "SCROLLING"),
            self._row(2, 1, 12, "bas"),     # hallucinated from noise
            self._row(2, 1, 8, "ied"),
        ])
        self.assertEqual(analyze._clean_ocr_tsv(tsv), "STOP SCROLLING")

    def test_punctuation_only_words_are_dropped(self):
        tsv = "\n".join([self.HEADER, self._row(1, 1, 90, "---"),
                         self._row(1, 1, 90, "REAL")])
        self.assertEqual(analyze._clean_ocr_tsv(tsv), "REAL")

    def test_empty_input(self):
        self.assertEqual(analyze._clean_ocr_tsv(""), "")
        self.assertEqual(analyze._clean_ocr_tsv(self.HEADER), "")

    def test_separate_lines_are_joined(self):
        tsv = "\n".join([self.HEADER, self._row(1, 1, 90, "ONE"),
                         self._row(1, 2, 90, "TWO")])
        self.assertEqual(analyze._clean_ocr_tsv(tsv), "ONE TWO")


class TestTextBeatGrouping(unittest.TestCase):
    def test_jittered_repeats_form_one_beat(self):
        observations = [
            (0.0, "STOP SCROLLING"),
            (0.5, "STOP SCROLLlNG"),   # OCR jitter on the same caption
            (1.0, "STOP SCROLLING"),
            (1.5, ""),
        ]
        beats = analyze._group_text_beats(observations, 0.5, 2.0)
        self.assertEqual(len(beats), 1)
        self.assertAlmostEqual(beats[0].start, 0.0)
        self.assertAlmostEqual(beats[0].end, 1.5)

    def test_distinct_captions_split(self):
        observations = [(0.0, "FIRST ONE"), (0.5, "SECOND THING ENTIRELY")]
        beats = analyze._group_text_beats(observations, 0.5, 1.0)
        self.assertEqual(len(beats), 2)


class TestTempo(unittest.TestCase):
    def test_half_second_onsets_are_120_bpm(self):
        onsets = [i * 0.5 for i in range(12)]
        self.assertAlmostEqual(analyze._estimate_tempo(onsets), 120.0, places=1)

    def test_folds_into_musical_range(self):
        onsets = [i * 0.25 for i in range(20)]  # 240 BPM raw -> folds to 120
        tempo = analyze._estimate_tempo(onsets)
        self.assertGreaterEqual(tempo, 60.0)
        self.assertLessEqual(tempo, 180.0)
        self.assertAlmostEqual(tempo, 120.0, places=1)

    def test_intervals_below_the_onset_floor_are_ignored(self):
        # The onset detector enforces a 0.18s gap, so anything faster is noise.
        self.assertIsNone(analyze._estimate_tempo([i * 0.1 for i in range(20)]))

    def test_too_few_onsets(self):
        self.assertIsNone(analyze._estimate_tempo([0.0, 1.0]))


class TestAssetSlots(unittest.TestCase):
    def _recipe(self):
        return {
            "version": recipe.RECIPE_VERSION,
            "target": {"width": 1080, "height": 1920, "fps": 30, "duration": 6.0},
            "assets": {"required": 1, "footage_slots": 2, "slots": [
                {"segment": 1, "kind": "diagram", "confidence": 0.9,
                 "placement": "full", "start": 2.0, "duration": 2.0,
                 "source_text": "HOW IT WORKS", "reference_frame": "frames/f.jpg"},
            ]},
            "captions": {"enabled": False},
            "audio": {"mode": "silent"},
            "segments": [
                {"index": 0, "role": "hook", "start": 0.0, "end": 2.0, "duration": 2.0,
                 "fill": "footage", "text": "", "clip": None, "asset": None},
                {"index": 1, "role": "body", "start": 2.0, "end": 4.0, "duration": 2.0,
                 "fill": "asset", "text": "", "clip": None, "asset": None,
                 "visual": {"kind": "diagram"}, "source_text": "HOW IT WORKS"},
                {"index": 2, "role": "cta", "start": 4.0, "end": 6.0, "duration": 2.0,
                 "fill": "footage", "text": "", "clip": None, "asset": None},
            ],
        }

    def test_assets_fill_only_asset_slots(self):
        data = self._recipe()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "a.mp4"
            clip.touch()
            image = Path(tmp) / "d.png"
            image.touch()
            render.assign_sources(data, [clip], [image])

        self.assertEqual(data["segments"][1]["asset"], str(image.resolve()))
        self.assertIsNone(data["segments"][0]["asset"])
        self.assertEqual(data["segments"][0]["clip"], str(clip.resolve()))
        self.assertEqual(data["segments"][2]["clip"], str(clip.resolve()))

    def test_short_asset_list_is_reported_not_recycled(self):
        data = self._recipe()
        data["segments"].append({
            "index": 3, "role": "body", "start": 6.0, "end": 8.0, "duration": 2.0,
            "fill": "asset", "text": "", "clip": None, "asset": None,
            "visual": {"kind": "screenshot"}, "source_text": "",
        })
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "d.png"
            image.touch()
            render.assign_sources(data, [], [image])

        _, missing = render.missing_sources(data)
        self.assertEqual([s["index"] for s in missing], [3])

    def test_pinned_assignments_are_preserved(self):
        data = self._recipe()
        data["segments"][1]["asset"] = "/pinned/diagram.png"
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "other.png"
            image.touch()
            render.assign_sources(data, [], [image])
        self.assertEqual(data["segments"][1]["asset"], "/pinned/diagram.png")

    def test_reuse_index_staggers_repeated_clips(self):
        data = self._recipe()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "a.mp4"
            clip.touch()
            image = Path(tmp) / "d.png"
            image.touch()
            render.assign_sources(data, [clip], [image])
        self.assertEqual(data["segments"][0]["_reuse_index"], 0)
        self.assertEqual(data["segments"][2]["_reuse_index"], 1)

    def test_segment_source_picks_the_right_field(self):
        data = self._recipe()
        data["segments"][0]["clip"] = "/x/clip.mp4"
        data["segments"][1]["asset"] = "/x/img.png"
        self.assertEqual(render.segment_source(data["segments"][0]), "/x/clip.mp4")
        self.assertEqual(render.segment_source(data["segments"][1]), "/x/img.png")


class TestRecipeIo(unittest.TestCase):
    def test_private_keys_are_not_persisted(self):
        data = {
            "version": recipe.RECIPE_VERSION,
            "segments": [{"index": 0, "clip": "/a.mp4", "_reuse_index": 3}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipe.json"
            recipe.save_recipe(data, path)
            written = json.loads(path.read_text())
        self.assertNotIn("_reuse_index", written["segments"][0])

    def test_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipe.json"
            path.write_text(json.dumps({"version": 999, "segments": []}))
            with self.assertRaises(ValueError):
                recipe.load_recipe(path)

    def test_non_recipe_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipe.json"
            path.write_text(json.dumps({"hello": "world"}))
            with self.assertRaises(ValueError):
                recipe.load_recipe(path)

    def test_round_trip(self):
        data = {"version": recipe.RECIPE_VERSION, "name": "x", "segments": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipe.json"
            recipe.save_recipe(data, path)
            self.assertEqual(recipe.load_recipe(path)["name"], "x")


class TestVisualSummary(unittest.TestCase):
    def test_title_card_is_not_an_asset(self):
        self.assertNotIn("title_card", visuals.ASSET_KINDS)
        self.assertIn("diagram", visuals.ASSET_KINDS)

    def test_majority_graphic_wins(self):
        verdicts = [
            visuals.FrameVerdict(0.0, "diagram", 0.9),
            visuals.FrameVerdict(0.5, "diagram", 0.95),
            visuals.FrameVerdict(1.0, "footage", 0.4),
        ]
        summary = visuals.summarize_segment(verdicts, 0.0, 1.5)
        self.assertEqual(summary.kind, "diagram")

    def test_majority_footage_wins(self):
        verdicts = [
            visuals.FrameVerdict(0.0, "footage", 0.9),
            visuals.FrameVerdict(0.5, "footage", 0.9),
            visuals.FrameVerdict(1.0, "diagram", 0.95),
        ]
        summary = visuals.summarize_segment(verdicts, 0.0, 1.5)
        self.assertEqual(summary.kind, "footage")

    def test_title_card_segment_reported_as_title_card(self):
        verdicts = [
            visuals.FrameVerdict(0.0, "title_card", 0.8),
            visuals.FrameVerdict(0.5, "title_card", 0.85),
        ]
        summary = visuals.summarize_segment(verdicts, 0.0, 1.0)
        self.assertEqual(summary.kind, "title_card")

    def test_segment_between_samples_uses_nearest_frame(self):
        verdicts = [visuals.FrameVerdict(0.0, "diagram", 0.9)]
        summary = visuals.summarize_segment(verdicts, 0.4, 0.45)
        self.assertEqual(summary.kind, "diagram")


class TestSheetMaps(unittest.TestCase):
    def test_slot_numbers_follow_timeline_order(self):
        data = {
            "assets": {"slots": [{"segment": 1}, {"segment": 3}]},
            "segments": [
                {"index": 0, "start": 0.0, "end": 1.0, "fill": "footage",
                 "visual": {"kind": "footage"}},
                {"index": 1, "start": 1.0, "end": 2.0, "fill": "asset",
                 "visual": {"kind": "diagram"}},
                {"index": 2, "start": 2.0, "end": 3.0, "fill": "footage",
                 "visual": {"kind": "title_card"}},
                {"index": 3, "start": 3.0, "end": 4.0, "fill": "asset",
                 "visual": {"kind": "screenshot"}},
            ],
        }
        frames = [(t / 2, Path(f"f{t}.jpg")) for t in range(8)]
        slots, kinds = sheet.expand_maps(data, frames)

        self.assertEqual(slots[1.0], 1)
        self.assertEqual(slots[3.0], 2)
        self.assertNotIn(0.0, slots)
        self.assertEqual(kinds[2.0], "title_card")
        self.assertEqual(kinds[3.5], "screenshot")


class TestCaptionStyle(unittest.TestCase):
    def test_colors(self):
        self.assertEqual(captions.parse_color("white"), (255, 255, 255))
        self.assertEqual(captions.parse_color("#ff8800"), (255, 136, 0))
        self.assertEqual(captions.parse_color("nonsense"), (255, 255, 255))

    def test_font_size_scales_with_frame_height(self):
        data = {"target": {"height": 1920}, "captions": {"font_size_ratio": 0.05}}
        style = captions.style_from_recipe(data, "/dev/null")
        self.assertEqual(style.font_size, 96)


class TestClipCollection(unittest.TestCase):
    def test_directories_expand_and_unknown_types_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.mp4").touch()
            (root / "a.png").touch()
            (root / "notes.txt").touch()

            found = render.collect_clips([str(root)])
            self.assertEqual([p.name for p in found], ["a.png", "b.mp4"])

            with self.assertRaises(Exception):
                render.collect_clips([str(root / "notes.txt")])

    def test_empty_selection_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                render.collect_clips([tmp])


if __name__ == "__main__":
    unittest.main()
