"""Edit features for beat-synced edits (AMVs): effects, in-points, shot lists, beats.

Pure logic only - no ffmpeg. The renders themselves were checked by hand.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelforge import beats, effects, recipe, render
from reelforge.analyze import Analysis
from reelforge.shell import ToolError


class TestEffects(unittest.TestCase):
    def test_parse_forms(self):
        self.assertEqual(effects.parse_effects("zoom,flash"), ["zoom", "flash"])
        self.assertEqual(effects.parse_effects(["Shake", "shake"]), ["shake"])
        self.assertEqual(effects.parse_effects("none"), [])
        self.assertEqual(effects.parse_effects(None), [])

    def test_unknown_effect_is_named(self):
        with self.assertRaises(ToolError) as caught:
            effects.parse_effects("zoom,glitch")
        self.assertIn("glitch", str(caught.exception))

    def test_speed(self):
        self.assertEqual(effects.parse_speed("2x"), 2.0)
        self.assertEqual(effects.parse_speed(None), 1.0)
        with self.assertRaises(ToolError):
            effects.parse_speed("50x")

    def test_chain_keeps_frame_size(self):
        chain = effects.filter_chain(["zoom", "shake", "flash"], 1080, 1920, 1.0)
        # Every geometry step must crop back to the target size.
        self.assertEqual(chain.count("crop=1080:1920"), 2)
        self.assertTrue(chain.endswith("color=white"))
        self.assertEqual(effects.filter_chain([], 1080, 1920, 1.0), "")

    def test_atempo_chains_outside_its_range(self):
        self.assertEqual(effects.atempo_chain(4.0).count("atempo"), 2)
        self.assertEqual(effects.atempo_chain(0.25).count("atempo"), 2)
        self.assertEqual(effects.atempo_chain(1.5), "atempo=1.500000")


class TestShotList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for name in ("a.mp4", "b clip.mp4"):
            (self.dir / name).write_bytes(b"")
        self.clips = self.dir / "clips"
        self.clips.mkdir()
        (self.clips / "c.mp4").write_bytes(b"")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokens(self):
        shots = render.parse_shot_list(
            "# comment\n"
            "a.mp4 0:03.2 zoom flash\n"
            "'b clip.mp4' 12 - 0.5x\n"
            "...\n"
            "c.mp4\n",
            self.dir, [self.clips],
        )
        self.assertEqual(len(shots), 4)
        self.assertEqual(shots[0]["clip_start"], 3.2)
        self.assertEqual(shots[0]["effects"], ["zoom", "flash"])
        self.assertEqual(shots[1]["effects"], [])
        self.assertEqual(shots[1]["speed"], 0.5)
        self.assertEqual(shots[2], {})
        self.assertTrue(shots[3]["clip"].endswith("c.mp4"))
        self.assertNotIn("effects", shots[3])  # inherits the recipe's

    def test_missing_clip_names_the_line(self):
        with self.assertRaises(ToolError) as caught:
            render.parse_shot_list("a.mp4\nnope.mp4\n", self.dir)
        self.assertIn("line 2", str(caught.exception))

    def test_apply_pins_footage_in_order(self):
        data = {"segments": [
            {"index": 0, "fill": "footage"},
            {"index": 1, "fill": "asset"},
            {"index": 2, "fill": "footage", "effects": ["zoom"]},
        ]}
        notes = render.apply_shot_list(data, [{"clip": "x", "clip_start": 1.0}, {}])
        self.assertEqual(data["segments"][0]["clip"], "x")
        self.assertNotIn("clip", data["segments"][1])
        self.assertEqual(data["segments"][2]["effects"], ["zoom"])
        self.assertEqual(notes, [])


class TestInPoints(unittest.TestCase):
    def test_clip_start_forms(self):
        self.assertIsNone(render.clip_start_seconds({}))
        self.assertEqual(render.clip_start_seconds({"clip_start": 2}), 2.0)
        self.assertEqual(render.clip_start_seconds({"clip_start": "1:02.5"}), 62.5)
        with self.assertRaises(ToolError):
            render.clip_start_seconds({"index": 0, "clip_start": -1})

    def test_frame_snapping_does_not_drift(self):
        # 128 BPM beats at 30fps: 14.06 frames each. Rounded per segment that
        # loses a frame every ~16 cuts; snapped boundaries must not.
        period = 60 / 128
        segments = [{"start": round(i * period, 3), "duration": round(period, 3)}
                    for i in range(64)]
        render.snap_to_frames(segments, 30)
        total_frames = round(sum(s["_render_duration"] for s in segments) * 30)
        self.assertLessEqual(abs(total_frames - 64 * period * 30), 1)


def pulses(bpm: float, first: float, seconds: float, accent_every: int = 0,
           hop: float = beats.BEAT_HOP) -> list[float]:
    """A synthetic onset-strength curve: a spike on every beat."""
    curve = [0.0] * int(seconds / hop)
    period = 60.0 / bpm
    k = 0
    while first + k * period < seconds - hop:
        curve[int(round((first + k * period) / hop))] = (
            2.0 if accent_every and k % accent_every == 0 else 1.0)
        k += 1
    return curve


class TestBeatGrid(unittest.TestCase):
    def test_tempo_from_autocorrelation(self):
        self.assertAlmostEqual(beats.estimate_tempo(pulses(140, 0.37, 30)), 140, delta=2.5)
        self.assertIsNone(beats.estimate_tempo([0.0] * 3000))

    def test_refines_tempo_and_phase(self):
        tempo, phase = beats.fit_grid(pulses(128, 0.2, 30), 125.0)
        self.assertAlmostEqual(tempo, 128, delta=0.3)
        self.assertAlmostEqual(phase, 0.2, delta=0.015)

    def test_bar_start_follows_the_kick(self):
        period = 60 / 120
        # Kick on beats 1, 5, 9... (index 1 of the grid starting at 0.5s).
        low = pulses(30, 1.0, 20)
        grid = [0.5 + i * period for i in range(30)]
        offset = beats.guess_bar_offset(low, grid)
        self.assertEqual((1 + offset) % 4, 0)

    def test_grid_times_window(self):
        times = beats.grid_times(0.25, 0.5, 1.0, 2.6)
        self.assertEqual([round(t, 2) for t in times], [1.25, 1.75, 2.25])

    def _recipe(self, **kwargs):
        strength = pulses(120, 0.1, 30)
        with mock.patch.object(beats, "audio_duration", return_value=30.0), \
             mock.patch.object(beats, "onset_strengths",
                               return_value=(strength, [0.0] * len(strength))), \
             tempfile.NamedTemporaryFile(suffix=".mp3") as song:
            return beats.build_beat_recipe(Path(song.name), **kwargs)

    def test_recipe_cuts_on_the_beat(self):
        built = self._recipe(every=2, start=4.0, duration=8.0,
                             effects=["zoom"], accent_effects=["flash"])
        segments = built["segments"]
        # Starts on the first beat at/after 4.0s and ends on a whole step.
        self.assertAlmostEqual(built["audio"]["music_start"], 4.1, delta=0.02)
        self.assertTrue(all(abs(s["duration"] - 1.0) < 0.01 for s in segments))
        # Two-beat steps alternate bar starts (beats 0, 4, ...) with mid-bar cuts.
        self.assertEqual([s["effects"] for s in segments[:3]],
                         [["flash"], ["zoom"], ["flash"]])
        self.assertEqual(built["source"]["platform"], "song")

    def test_half_beats(self):
        built = self._recipe(every=0.5, duration=4.0)
        self.assertTrue(all(abs(s["duration"] - 0.25) < 0.01 for s in built["segments"]))

    def test_no_beat_asks_for_bpm(self):
        with mock.patch.object(beats, "audio_duration", return_value=10.0), \
             mock.patch.object(beats, "onset_strengths", return_value=([0.0] * 1000, [])), \
             tempfile.NamedTemporaryFile(suffix=".mp3") as song:
            with self.assertRaises(ToolError) as caught:
                beats.build_beat_recipe(Path(song.name))
        self.assertIn("--bpm", str(caught.exception))


class TestMinShot(unittest.TestCase):
    def test_fast_cuts_survive_a_lower_minimum(self):
        analysis = Analysis(probe={"duration": 2.0, "width": 1080, "height": 1920, "fps": 30},
                            cuts=[0.3, 0.6, 0.9, 1.2])
        kept = recipe.build_recipe(analysis, name="x", origin="x", platform="local", meta={})
        fast = recipe.build_recipe(analysis, name="x", origin="x", platform="local", meta={},
                                   min_segment=0.15)
        self.assertLess(len(kept["segments"]), 5)
        self.assertEqual(len(fast["segments"]), 5)


if __name__ == "__main__":
    unittest.main()
