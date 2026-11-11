"""Unit tests for the pure logic - no ffmpeg, no network."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelforge import analyze, captions, ingest, recipe, render, sheet, speakers, sync, visuals


class TestUrlClassification(unittest.TestCase):
    def test_instagram_reel(self):
        # Share links carry a tracking query string; it must not leak into the
        # post id or the project slug.
        info = ingest.classify_url(
            "https://www.instagram.com/reel/Ab1cD2eF3gH/?stkn=EXAMPLETOKEN=="
        )
        self.assertEqual(info.platform, "instagram")
        self.assertEqual(info.post_id, "Ab1cD2eF3gH")

    def test_instagram_post_and_youtube(self):
        cases = [
            ("https://instagram.com/p/ABC123xyz/", "instagram", "ABC123xyz"),
            ("https://youtube.com/shorts/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
        ]
        for url, platform, post in cases:
            with self.subTest(url=url):
                info = ingest.classify_url(url)
                self.assertEqual((info.platform, info.post_id), (platform, post))
                self.assertFalse(info.provisional)

    def test_unknown_host_falls_back_to_hostname(self):
        info = ingest.classify_url("https://vimeo.com/123456")
        self.assertEqual(info.platform, "vimeo-com")
        self.assertIsNone(info.post_id)

    def test_is_url(self):
        self.assertTrue(ingest.is_url("https://x.com/a"))
        self.assertFalse(ingest.is_url("/Users/me/clip.mp4"))

    def test_slugify_strips_unsafe_characters(self):
        self.assertEqual(ingest.slugify("my reel!! (final)/v2"), "my-reel-final-v2")


class TestTikTokUrls(unittest.TestCase):
    VIDEO_ID = "7412345678901234567"

    def test_canonical_forms_yield_the_real_post_id(self):
        urls = [
            f"https://www.tiktok.com/@someone/video/{self.VIDEO_ID}",
            f"https://www.tiktok.com/@someone/video/{self.VIDEO_ID}"
            "?is_from_webapp=1&sender_device=pc",
            f"https://www.tiktok.com/@some.user_1/video/{self.VIDEO_ID}",
            f"https://m.tiktok.com/v/{self.VIDEO_ID}.html",
            f"https://www.tiktok.com/embed/{self.VIDEO_ID}",
            f"https://www.tiktok.com/embed/v2/{self.VIDEO_ID}",
        ]
        for url in urls:
            with self.subTest(url=url):
                info = ingest.classify_url(url)
                self.assertEqual(info.platform, "tiktok")
                self.assertEqual(info.post_id, self.VIDEO_ID)
                self.assertFalse(info.provisional)

    def test_photo_carousels_are_recognised(self):
        info = ingest.classify_url(
            f"https://www.tiktok.com/@someone/photo/{self.VIDEO_ID}")
        self.assertEqual((info.platform, info.post_id), ("tiktok", self.VIDEO_ID))

    def test_short_links_are_flagged_provisional(self):
        urls = [
            "https://vm.tiktok.com/ZMhKJq8Yx/",
            "https://vt.tiktok.com/ZSJ8k2mQd/",
            "https://www.tiktok.com/t/ZTd9aBcDe/",
        ]
        for url in urls:
            with self.subTest(url=url):
                info = ingest.classify_url(url)
                self.assertEqual(info.platform, "tiktok")
                self.assertTrue(
                    info.provisional,
                    "a short-link code is not the post id and must be revisited")
                self.assertIsNotNone(info.post_id)

    def test_query_string_never_leaks_into_the_id(self):
        info = ingest.classify_url(
            f"https://www.tiktok.com/@someone/video/{self.VIDEO_ID}?lang=en&q=1")
        self.assertEqual(info.post_id, self.VIDEO_ID)


class TestProjectRename(unittest.TestCase):
    def test_short_link_project_is_renamed_to_the_real_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "tiktok-ZMhKJq8Yx"
            project.mkdir()
            video = project / "source.mp4"
            video.write_bytes(b"x")

            new_dir, new_video = ingest._rename_project(
                project, video, root, "tiktok-7412345678901234567")

            self.assertEqual(new_dir.name, "tiktok-7412345678901234567")
            self.assertTrue(new_video.exists())
            self.assertFalse(project.exists())

    def test_rename_is_skipped_when_the_target_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "tiktok-ZMhKJq8Yx"
            project.mkdir()
            video = project / "source.mp4"
            video.write_bytes(b"x")
            (root / "tiktok-999").mkdir()

            new_dir, new_video = ingest._rename_project(
                project, video, root, "tiktok-999")

            self.assertEqual(new_dir, project)
            self.assertEqual(new_video, video)
            self.assertTrue(video.exists())

    def test_rename_to_the_same_name_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "tiktok-abc"
            project.mkdir()
            video = project / "source.mp4"
            video.write_bytes(b"x")

            new_dir, new_video = ingest._rename_project(
                project, video, root, "tiktok-abc")
            self.assertEqual((new_dir, new_video), (project, video))



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
        paragraph = " ".join(f"word{i}" for i in range(40))
        self.assertEqual(recipe._starting_text(paragraph, False, True), "")

    def test_a_two_line_lyric_caption_survives(self):
        # Nine words across two lines. The old eight-word cap silently dropped
        # this, leaving a reel whose captions were plainly visible on screen
        # with no captions at all in its recipe.
        lyric = "It's simple and it's plain The choice is yours."
        self.assertEqual(recipe._starting_text(lyric, False, True), lyric)

    def test_blank_text_opt_out(self):
        self.assertEqual(recipe._starting_text("STOP SCROLLING", False, False), "")

    def test_platform_watermarks_are_not_seeded_as_captions(self):
        # OCR reads TikTok's drifting "TikTok @handle" stamp as on-screen text;
        # burning another creator's handle into the render would be wrong.
        for watermark in ("Gorillo TikTok @gorilloyt", "TikTok", "@gorilloyt",
                          "Instagram", "YouTube"):
            with self.subTest(watermark=watermark):
                self.assertTrue(recipe._looks_like_watermark(watermark))
                self.assertEqual(recipe._starting_text(watermark, False, True), "")

    def test_real_captions_survive_the_watermark_filter(self):
        for caption in ("how many frogs can you find", "STOP SCROLLING",
                        "3 things I wish I knew"):
            with self.subTest(caption=caption):
                self.assertFalse(recipe._looks_like_watermark(caption))
                self.assertEqual(recipe._starting_text(caption, False, True), caption)

    def test_a_sentence_mentioning_a_platform_is_not_a_watermark(self):
        sentence = "I built this on TikTok last year and it changed everything"
        self.assertFalse(recipe._looks_like_watermark(sentence))


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


class TestDarkFootageIsNotAGraphic(unittest.TestCase):
    """Dim footage is genuinely flat and used to be classified as a diagram."""

    def _write(self, image, tmp: str, name: str) -> Path:
        path = Path(tmp) / name
        image.save(path)
        return path

    def _dark_room(self):
        """A dim room: mostly shadow, but with real objects in it.

        Uniform noise alone would be a bad proxy - with no large-scale
        structure every row reads as uniform, which real footage never does.
        The lit monitor and the chair silhouette are what break the rows up.
        """
        from PIL import Image, ImageDraw
        import random
        random.seed(7)
        image = Image.new("RGB", (360, 640), (14, 12, 18))
        draw = ImageDraw.Draw(image)
        # A lit monitor, its glow, and a chair back in front of it.
        draw.rectangle([70, 180, 290, 330], fill=(40, 70, 120))
        draw.rectangle([90, 200, 270, 250], fill=(70, 110, 170))
        draw.ellipse([110, 300, 250, 470], fill=(26, 24, 30))
        draw.rectangle([0, 470, 360, 640], fill=(20, 18, 24))
        pixels = image.load()
        for y in range(640):
            for x in range(360):
                r, g, b = pixels[x, y]
                jitter = random.randint(-7, 7)
                pixels[x, y] = (max(0, min(255, r + jitter)),
                                max(0, min(255, g + jitter)),
                                max(0, min(255, b + jitter)))
        return image

    def _light_chart(self):
        """A light background with ruled lines and filled bars - a chart."""
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (360, 640), (250, 250, 252))
        draw = ImageDraw.Draw(image)
        for y in range(120, 560, 60):
            draw.line([(30, y), (330, y)], fill=(200, 205, 214), width=3)
        # Enough filled area to register as distinct color regions.
        draw.rectangle([40, 180, 150, 540], fill=(66, 133, 244))
        draw.rectangle([160, 260, 250, 540], fill=(52, 168, 83))
        draw.rectangle([260, 340, 330, 540], fill=(234, 67, 53))
        return image

    @unittest.skipUnless(visuals.PILLOW_AVAILABLE, "needs Pillow")
    def test_dark_low_contrast_frame_is_footage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(self._dark_room(), tmp, "dark.png")
            metrics = visuals._metrics(path)
            # It really does look flat - that is the whole trap.
            self.assertGreater(metrics["flat_ratio"], 0.7)
            self.assertLess(metrics["near_white"], 0.05)
            self.assertLessEqual(metrics["ruled_ratio"], visuals.RULED_LINE_RATIO)

            verdict = visuals.classify_frame(path, "")
            self.assertEqual(verdict.kind, "footage")

    @unittest.skipUnless(visuals.PILLOW_AVAILABLE, "needs Pillow")
    def test_light_ruled_frame_is_still_a_graphic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(self._light_chart(), tmp, "chart.png")
            verdict = visuals.classify_frame(path, "Revenue Signups Churn")
            self.assertIn(verdict.kind, visuals.ASSET_KINDS)

    @unittest.skipUnless(visuals.PILLOW_AVAILABLE, "needs Pillow")
    def test_structural_evidence_is_required(self):
        # A light background alone is enough; so are ruled lines. Neither
        # present -> footage, however flat the frame is.
        self.assertGreater(visuals.LIGHT_BACKGROUND_RATIO, 0.0)
        self.assertLess(visuals.LIGHT_BACKGROUND_RATIO, 1.0)


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


def _speechy(n: int, seed: int) -> list[float]:
    """Bursty, envelope-like noise: syllables with gaps, never repeating."""
    import random
    rng = random.Random(seed)
    out, level = [], 0.0
    for _ in range(n):
        if rng.random() < 0.08:
            level = rng.random() if rng.random() < 0.7 else 0.0
        out.append(max(0.0, level + rng.gauss(0, 0.05)))
    return out


class TestSync(unittest.TestCase):
    RATE = sync.ENVELOPE_RATE

    def test_fft_fallback_matches_naive_correlation(self):
        saved = sync._np
        sync._np = None
        try:
            track = _speechy(300, 1)
            snip = _speechy(40, 2)
            got = sync._raw_xcorr(snip, track)
        finally:
            sync._np = saved
        want = [sum(s * track[lag + i] for i, s in enumerate(snip))
                for lag in range(len(track) - len(snip) + 1)]
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, places=6)

    def test_best_lag_is_gain_invariant(self):
        track = _speechy(3000, 3)
        snip = [x * 0.2 + 0.05 for x in track[1234:1234 + 500]]  # quieter mic, DC
        lag, score = sync.best_lag(snip, track)
        self.assertEqual(lag, 1234)
        self.assertGreater(score, 0.99)

    def test_match_recovers_offset_with_head_the_take_misses(self):
        take = _speechy(60 * self.RATE, 4)
        # video: 10s the recorder never heard, then take[20s:50s] -> offset +10
        video = _speechy(10 * self.RATE, 5) + take[20 * self.RATE:50 * self.RATE]
        offset, score, agreeing, probes = sync.match_envelopes(
            video, take, snippet=8, probes=3)
        self.assertEqual(len(probes), 3)
        self.assertGreaterEqual(agreeing, 2)
        self.assertAlmostEqual(offset, 10.0, places=1)
        self.assertGreater(score, 0.9)

    def test_unrelated_audio_is_rejected(self):
        offset, _score, agreeing, _ = sync.match_envelopes(
            _speechy(40 * self.RATE, 6), _speechy(120 * self.RATE, 7),
            snippet=10, probes=3)
        self.assertIsNone(offset)
        self.assertLess(agreeing, 2)

    def test_coverage(self):
        # recorder started 20s into the video and ran past its end
        self.assertEqual(sync.coverage(-20.0, 100.0, 500.0), (20.0, 100.0))
        # recorder stopped 30s before the video ended
        self.assertEqual(sync.coverage(100.0, 100.0, 170.0), (0.0, 70.0))
        self.assertIsNone(sync.coverage(-200.0, 100.0, 50.0))

    def test_probe_positions(self):
        self.assertEqual(sync.probe_positions(20, 30, 3), [0.0])
        pos = sync.probe_positions(130, 30, 3)
        self.assertEqual(len(pos), 3)
        self.assertTrue(all(0 <= p <= 100 for p in pos))
        self.assertLess(pos[0], pos[1])

    def test_probe_status_marks_probe_past_take_end(self):
        # camera ran 4 min past the recorder: the last probe has no take audio
        probes = [sync.Probe(80.0, -22.0, 0.9), sync.Probe(420.0, -22.1, 0.88),
                  sync.Probe(815.0, 310.0, 0.12)]
        sync.classify_probes(probes, -22.05, snippet=30, take_duration=670,
                             video_duration=935)
        self.assertEqual([p.status for p in probes],
                         ["agrees", "agrees", sync.OUTSIDE])
        m = sync.Match(video="v", take="t", probes=probes)
        self.assertEqual(sync.probe_note(m), "1 outside take")

    def test_probe_status_weak_and_disagrees(self):
        probes = [sync.Probe(10.0, 5.0, 0.9), sync.Probe(50.0, 5.2, 0.8),
                  sync.Probe(90.0, 40.0, 0.7), sync.Probe(130.0, 5.0, 0.1)]
        sync.classify_probes(probes, 5.1, snippet=30, take_duration=1000,
                             video_duration=200)
        self.assertEqual([p.status for p in probes],
                         ["agrees", "agrees", "disagrees", "weak"])

    def test_expand_falls_back_to_audio_when_dir_has_no_video(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("a.wav", "b.wav", ".hidden.wav", "notes.txt"):
                Path(d, n).write_bytes(b"")
            got = sync.expand([d], sync.VIDEO_EXTS, fallback=sync.AUDIO_EXTS)
            self.assertEqual([p.name for p in got], ["a.wav", "b.wav"])
            Path(d, "c.mp4").write_bytes(b"")
            got = sync.expand([d], sync.VIDEO_EXTS, fallback=sync.AUDIO_EXTS)
            self.assertEqual([p.name for p in got], ["c.mp4"])


class TestSpeakers(unittest.TestCase):
    def test_parse_mic_and_anchor(self):
        m = speakers.parse_mic("David/Papa=/x/MIC3.WAV")
        self.assertEqual((m.names, m.label, m.path.name), (["David", "Papa"], "David/Papa", "MIC3.WAV"))
        self.assertEqual(speakers.parse_mic("Max=MIC1.WAV").names, ["Max"])
        with self.assertRaises(ValueError):
            speakers.parse_mic("MIC1.WAV")
        with self.assertRaises(ValueError):
            speakers.parse_mic("A/A=MIC1.WAV")
        self.assertEqual(speakers.parse_anchor("Papa=28.5"), ("Papa", 28.5))
        self.assertEqual(speakers.parse_anchor("Papa=1:02:03"), ("Papa", 3723.0))

    def test_parse_both_whisper_formats(self):
        cpp = {"transcription": [
            {"offsets": {"from": 500, "to": 2400}, "text": " Hi there."},
            {"offsets": {"from": 2400, "to": 2400}, "text": "zero length"},
            {"offsets": {"from": 3000, "to": 4000}, "text": "  "}]}
        py = {"segments": [{"start": 0.5, "end": 2.4, "text": " Hi there."}]}
        for data in (cpp, py):
            segs = speakers.parse_transcript(data)
            self.assertEqual([(s.start, s.end, s.text) for s in segs], [(0.5, 2.4, "Hi there.")])
        with self.assertRaises(ValueError):
            speakers.parse_transcript({"text": "x"})

    def test_loudest_mic_wins(self):
        rate = speakers.ENERGY_RATE
        a = [1.0] * (2 * rate) + [0.01] * (2 * rate)      # A talks 0-2s
        b = [0.02] * (2 * rate) + [0.8] * (2 * rate)      # B talks 2-4s
        segs = [speakers.Segment(0.2, 1.8, "one"), speakers.Segment(2.1, 3.9, "two")]
        speakers.assign_loudest(segs, {"A": a, "B": b})
        self.assertEqual([s.mic for s in segs], ["A", "B"])

    def test_text_merges_turns_and_srt_shifts_into_video_time(self):
        segs = [speakers.Segment(0.5, 2.0, "Hi.", "Max"),
                speakers.Segment(2.0, 3.0, "I'm Max.", "Max"),
                speakers.Segment(65.0, 66.5, "Hey.", "David")]
        txt = speakers.to_text(segs, ["Title"])
        self.assertEqual(txt, "Title\n\n[0:00:00] Max: Hi. I'm Max.\n\n[0:01:05] David: Hey.\n")
        # take_time = video_time + offset; offset -6.5 -> video is 6.5s later
        srt = speakers.to_srt(segs, shift=-6.5, duration=70.0)
        self.assertIn("00:00:07,000 --> 00:00:08,500\nMax: Hi.", srt)
        self.assertNotIn("David", srt)        # 71.5s is past the 70s video
        # positive offset drops cues from before the video started
        self.assertNotIn("Hi.", speakers.to_srt(segs, shift=10.0))

    def test_matches_for_take_by_stem(self):
        report = [{"video": "/a/C0001.MP4", "take": "/x/2026_1108_1938.WAV", "offset": -6.5},
                  {"video": "/a/C0002.MP4", "take": "/y/2026_1108_1938.wav", "offset": -5.2},
                  {"video": "/a/C0003.MP4", "take": None, "offset": None}]
        got = speakers.matches_for_take(report, Path("/z/2026_1108_1938.WAV"))
        self.assertEqual([Path(r["video"]).name for r in got], ["C0001.MP4", "C0002.MP4"])

    @unittest.skipIf(sync._np is None, "numpy not installed")
    def test_anchored_kmeans_names_clusters_by_seed(self):
        import random
        rng = random.Random(1)
        def near(c):
            return [x + rng.gauss(0, 0.15) for x in c]
        a, b = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
        vecs = [near(a) for _ in range(20)] + [near(b) for _ in range(8)]
        labels = speakers.anchored_kmeans(vecs, [vecs[25], vecs[3]])  # seed order: b, a
        self.assertEqual(labels[:20], [1] * 20)
        self.assertEqual(labels[20:], [0] * 8)


if __name__ == "__main__":
    unittest.main()
