"""Runtime + static proofs for subtitles-only rendering (audit G-0/G-8).

Product rule: Remotion renders subtitles ONLY — never any other text. Every
beat is a Flux-generated image; text cards, overlay text, and the
suppress-window machinery are removed end to end.

Runtime proofs drive the real internal chain — the real props builders
writing real JSON files, the real `generate_all_beat_images` loop with only
the paid fal.ai wrapper stubbed, the real `_build_beat_section` normalization
— never the internal logic itself.
"""

import json
import logging
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.services.flux_generator import (
    fill_failed_beats_from_neighbors,
)
from app.agents.agent4_visuals.subagents.storyboard import (
    _build_beat_section,
    _parent_media_reusable,
)
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    validate_storyboard,
)
from app.agents.agent4_visuals.system_prompt import (
    PROMPT_VERSION,
    STORYBOARD_SCHEMA_VERSION,
    _BEAT_SCHEMA,
    _STORYBOARD_SYSTEM_PROMPT,
)
from app.agents.agent5_render.services import video
from app.agents.agent5_render.services.remotion_builder import (
    build_main_props,
    build_short_props,
)

_REMOTION_SRC = Path(__file__).resolve().parents[1] / "remotion" / "src"


def _beat(order: int, **overrides) -> dict:
    beat = {
        "beat_order": order,
        "section_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": f"narration {order}",
        "visual_intent": f"intent {order}",
        "visual_type": "b-roll",
        "environment": "forest_nature",
        "flux_prompt": f"concrete subject {order}, wide shot, photorealistic",
        "effect": "slow_zoom",
        "color_grade": "neutral",
        "transition_to_next": "cut",
        "motif": "exterior",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "",
        "media_type": "image",
    }
    beat.update(overrides)
    return beat


class TestPropsEmitNoTextFields(unittest.TestCase):
    """Runtime proof: the real props builders write JSON with zero text-layer keys."""

    def _sections(self):
        return [
            _beat(0, media_url="cache/abc/one.jpg"),
            # Legacy text-card row shapes that old DB data can still contain:
            _beat(1, media_url="__text_card__", visual_type="text_card",
                  media_strategy="remotion_text_card",
                  overlay_text="Do not touch the stairs.",
                  overlay_position="center", text_card_style="quote"),
            _beat(2, media_url="cache/abc/two.jpg",
                  overlay_text="THREE WEEKS LATER", overlay_position="center"),
        ]

    def _assert_clean(self, props: dict):
        for section in props["sections"]:
            self.assertNotIn("overlay_text", section)
            self.assertNotIn("overlay_position", section)
            self.assertNotIn("text_card_style", section)
            self.assertNotEqual(section.get("visual_type"), "text_card")
            self.assertNotEqual(section.get("media_url"), "__text_card__")

    def test_main_props_are_clean(self):
        with TemporaryDirectory() as tmp:
            with patch.object(settings, "media_path", tmp):
                path = build_main_props(
                    content_id="cid", language="en",
                    audio_file_path=f"{tmp}/audio.mp3", duration_ms=9000,
                    sections=self._sections(),
                    standard_subtitles=[{"text": "hello", "start_ms": 0, "end_ms": 8900}],
                    karaoke_subtitles=[],
                )
                props = json.loads(Path(path).read_text())
        self._assert_clean(props)
        # The legacy sentinel section carries no clips — dark fill, no text.
        self.assertEqual(props["sections"][1]["clips"], [])
        # Subtitles are present untouched — the only text layer.
        self.assertEqual(props["subtitles"]["captions"][0]["text"], "hello")

    def test_short_props_are_clean(self):
        with TemporaryDirectory() as tmp:
            with patch.object(settings, "media_path", tmp):
                path = build_short_props(
                    content_id="cid", language="en",
                    audio_file_path=f"{tmp}/audio.mp3",
                    short={"short_index": 0, "start_ms": 0, "end_ms": 9000,
                           "sections": self._sections(), "part_label": "",
                           "total_parts": 4},
                    karaoke_subtitles=[],
                )
                props = json.loads(Path(path).read_text())
        self._assert_clean(props)
        self.assertEqual(props["part_label"], "")

    def test_part_label_config_defaults_on(self):
        self.assertTrue(settings.short_part_label_enabled)


class _FakeVideoRenderQuery:
    """Minimal stand-in for `_is_rendered()`'s `db.query(VideoRender)...first()`
    lookup — always empty, so the fresh-build path is always taken."""

    def filter(self, *a, **k):
        return self

    def first(self):
        return None


class _FakeVideoDb:
    def query(self, model):
        return _FakeVideoRenderQuery()

    def get(self, model, key):
        return None

    def commit(self):
        pass


class TestPartLabelSuppressedForSingleParts(unittest.TestCase):
    """Runtime proof for Finding E (code_report/output_mode_shorts_only_and_
    youtube_long_only_roadmap.md, Phase B): a Short with only one part
    (``short_total_parts=1``, the shape a Solo Short always has) must never
    render a "Part 1 of 1" label, even though
    ``settings.short_part_label_enabled`` defaults to True. A real
    multi-part child-of-a-parent Short must still show its label,
    unaffected — the new condition is an AND on top of the existing
    enabled check, not a replacement for it.

    Exercises the REAL `video._process_language()` — not a reimplementation
    of its ternary — with only the deep render call (`_run_short_render`, a
    local Remotion subprocess — never invoked live, per CLAUDE.md §19.1) and
    the technical-blocker/props-sanity/subtitle-building steps (independent
    concerns with their own coverage elsewhere) stubbed. `build_short_props()`
    itself is real and writes a real JSON file, which is read back for the
    assertions — the same "real internal chain, only the paid/expensive
    boundary stubbed" pattern as `TestPropsEmitNoTextFields` above.
    """

    def _run(self, short_total_parts: int, part_label_enabled: bool = True) -> dict:
        content_id = uuid.uuid4()
        cid_str = str(content_id)
        script = SimpleNamespace(voice_script="Hello world, this is a short.")
        audio = SimpleNamespace(
            whisper_transcript=[], duration_ms=9000,
            file_path="audio/x/en.mp3",
        )
        # audio_end_ms must match audio.duration_ms exactly — build_short_props()
        # runs its own real _assert_timeline_alignment() check (unstubbed,
        # unrelated to this fix) and raises ValueError on drift >2%.
        beats = [_beat(0, media_url="cache/abc/one.jpg", audio_end_ms=9000)]

        with TemporaryDirectory() as tmp:
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(settings, "short_part_label_enabled", part_label_enabled),
                patch.object(video, "build_standard_subtitles", return_value=[]),
                patch.object(video, "build_karaoke_subtitles", return_value=[]),
                patch.object(video, "_collect_technical_blockers", return_value=[]),
                patch.object(video, "_check_props_sanity", return_value=(True, "")),
                patch.object(video, "_run_short_render", return_value={
                    "file_path": f"{tmp}/video/{cid_str}/en_short_0.mp4",
                    "duration_seconds": 9.0, "render_time_seconds": 1.0,
                }),
            ):
                ok = video._process_language(
                    content_id=content_id, language="en", script=script,
                    audio=audio, beats=beats, channel=None,
                    karaoke_color="#FFD700", db=_FakeVideoDb(),
                    is_short_episode=True, short_order=0,
                    short_total_parts=short_total_parts, proper_nouns=[],
                )
            self.assertTrue(ok)
            props_path = Path(tmp) / "remotion_props" / f"{cid_str}_en_short_0.json"
            return json.loads(props_path.read_text())

    def test_single_part_suppresses_part_label(self):
        props = self._run(short_total_parts=1)
        self.assertEqual(props["part_label"], "")
        self.assertEqual(props["total_parts"], 1)

    def test_multi_part_still_shows_label(self):
        props = self._run(short_total_parts=3)
        self.assertNotEqual(props["part_label"], "")
        self.assertIn("3", props["part_label"])  # e.g. "Part 1 of 3"
        self.assertEqual(props["total_parts"], 3)

    def test_single_part_suppressed_regardless_of_enabled_flag(self):
        # part_label_enabled=False already produced "" before this fix —
        # confirms the new total_parts>1 condition is additive (an AND),
        # not a replacement of the pre-existing enabled check.
        props = self._run(short_total_parts=1, part_label_enabled=False)
        self.assertEqual(props["part_label"], "")


class TestBeatBuildNormalization(unittest.TestCase):
    """_build_beat_section: every beat is flux_generated, no overlays derived."""

    def test_legacy_text_card_strategy_forced_to_flux(self):
        raw = _beat(0, media_strategy="remotion_text_card",
                    visual_type="text_card", text_card_style="quote")
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertEqual(out["media_strategy"], "flux_generated")
        self.assertNotEqual(out["visual_type"], "text_card")
        self.assertEqual(out["overlay_text"], "")
        self.assertEqual(out["overlay_position"], "none")

    def test_documentary_photograph_is_not_a_text_prop(self):
        # Regression pin: substring matching once made the keyword "document"
        # match inside "documentary photograph" — a phrase the storyboard
        # prompt itself recommends — silently rewriting valid prompts (and
        # mutating kept beats during segment-level retry re-mapping).
        raw = _beat(0, flux_prompt=(
            "Interior office drawer, brass handle, documentary photograph, "
            "sharp focus, no readable text"
        ))
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertEqual(out["flux_prompt"], raw["flux_prompt"])

    def test_text_prop_sanitized_without_overlay(self):
        # Elimination Mandate (D2.2/D2.3): the sanitizer no longer rewrites
        # the subject — it appends one no-readable-text clause to Claude's
        # own flux_prompt verbatim. A literal quote in the original prompt is
        # a separate concern owned by validate_storyboard() check 19
        # (ai_text_rendering_requested) and the storyboard prompt's own
        # quote-ban rule, not by this sanitizer.
        raw = _beat(0, flux_prompt="missing person poster on a pole",
                    visual_intent="a missing person poster on a pole")
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        # Prompt sanitization survives (Phase 14.7 prompt half)…
        self.assertIn("no readable text", out["flux_prompt"])
        self.assertIn("missing person poster on a pole", out["flux_prompt"])
        # …but the overlay-derivation half is gone: no "MISSING" label.
        self.assertEqual(out["overlay_text"], "")


class TestNeighborReuseFallback(unittest.TestCase):
    """Hard Flux failures reuse the nearest neighbour's image — never a text card."""

    def test_generate_all_fills_failed_beat_from_neighbor(self):
        beats = [_beat(0), _beat(1), _beat(2)]

        def fake_routing(beat, content_id, tier_counts, **kwargs):
            # Beat 1's generation hard-fails; the others succeed.
            return None if beat["beat_order"] == 1 else f"cache/cid/{beat['beat_order']}.jpg"

        with patch.object(flux_generator, "generate_beat_image_with_routing", side_effect=fake_routing), \
             patch.object(flux_generator, "generate_beat_image", return_value=None) as hard_retry, \
             patch.object(flux_generator.time, "sleep"):
            with self.assertLogs("app.agents.agent4_visuals.services.flux_generator",
                                 level=logging.WARNING) as logs:
                out = flux_generator.generate_all_beat_images(beats, "cid")

        # The safe retry with a fresh cache key ran for the failed beat…
        hard_retry.assert_called_once()
        self.assertEqual(hard_retry.call_args.kwargs.get("cache_key_extra"), "hard_retry:1")
        # …then the neighbour pass filled it from beat 0 (nearest, earlier wins).
        self.assertEqual(out[1]["media_url"], "cache/cid/0.jpg")
        self.assertNotEqual(out[1].get("visual_type"), "text_card")
        self.assertTrue(any("BEAT_IMAGE_NEIGHBOR_REUSED" in m for m in logs.output))
        self.assertFalse(any("__text_card__" in (b.get("media_url") or "") for b in out))

    def test_no_images_anywhere_fills_nothing(self):
        beats = [_beat(0), _beat(1)]
        filled = fill_failed_beats_from_neighbors(beats, "cid")
        self.assertEqual(filled, 0)
        self.assertTrue(all(not b["media_url"] for b in beats))

    def test_legacy_sentinel_counts_as_missing(self):
        beats = [_beat(0, media_url="cache/cid/0.jpg"),
                 _beat(1, media_url="__text_card__")]
        filled = fill_failed_beats_from_neighbors(beats, "cid")
        self.assertEqual(filled, 1)
        self.assertEqual(beats[1]["media_url"], "cache/cid/0.jpg")


class TestRemapReusePool(unittest.TestCase):
    """Parent text-card beats are excluded from the child reuse pool (G-4.1)."""

    def test_exclusions(self):
        self.assertTrue(_parent_media_reusable("cache/p/x.jpg", {}))
        self.assertFalse(_parent_media_reusable("", {}))
        self.assertFalse(_parent_media_reusable("__text_card__", {}))
        self.assertFalse(_parent_media_reusable("http://cdn/x.jpg", {}))
        self.assertFalse(_parent_media_reusable(
            "cache/p/x.jpg", {"media_strategy": "remotion_text_card"}))
        self.assertFalse(_parent_media_reusable(
            "cache/p/x.jpg", {"visual_type": "text_card"}))


class TestValidators(unittest.TestCase):
    """Retired checks stay silent. The legacy sentinel is now a MAJOR finding
    in media_validation.validate_visual_media_assets() (the single media
    validator since roadmap 6.4 — see test_agent4_media_validation.py's
    test_legacy_text_card_sentinel_blocks() for that runtime proof;
    storyboard_validator.validate_media_assets() no longer exists)."""

    def test_text_card_checks_retired(self):
        # A storyboard that would have fired checks 2/3/7 pre-removal.
        beats = [
            _beat(i, media_strategy="remotion_text_card", visual_type="text_card")
            for i in range(4)
        ]
        checks = {i["check"] for i in validate_storyboard(beats)}
        self.assertNotIn("cover_frame_text_card", checks)
        self.assertNotIn("opening_text_card_pair", checks)
        self.assertNotIn("text_card_saturation", checks)


class TestStaticSurface(unittest.TestCase):
    """Static smoke: schema, prompt, loaders, and Remotion sources are clean."""

    def test_beat_schema_has_no_text_fields(self):
        props = _BEAT_SCHEMA["properties"]
        for key in ("media_strategy", "stock_queries", "fallback_flux_prompt",
                    "text_card_style", "overlay_text", "overlay_position"):
            self.assertNotIn(key, props)
            self.assertNotIn(key, _BEAT_SCHEMA["required"])

    def test_prompt_has_no_text_card_instructions(self):
        self.assertNotIn("remotion_text_card", _STORYBOARD_SYSTEM_PROMPT)
        self.assertNotIn("overlay_text", _STORYBOARD_SYSTEM_PROMPT)
        self.assertIn("Every beat is a generated image", _STORYBOARD_SYSTEM_PROMPT)

    def test_versions_bumped(self):
        # Subtitles-only rendering landed at prompt v4.0 / schema v7.0 — later
        # phases may bump further (e.g. v4.1 prompt inversion), so pin
        # minimums, not exact values.
        self.assertGreaterEqual(float(PROMPT_VERSION), 4.0)
        self.assertGreaterEqual(float(STORYBOARD_SCHEMA_VERSION), 7.0)

    def test_loaders_normalize_legacy_rows(self):
        # Roadmap 6.5 / audit AR-2: the loader (and its legacy-row
        # normalization) is now shared, not duplicated per-file.
        src = (Path(__file__).resolve().parents[1] / "app/services/video_sections.py").read_text()
        self.assertIn("Legacy-row normalization", src)

    def test_remotion_sources_have_no_text_layers(self):
        self.assertFalse((_REMOTION_SRC / "components" / "TextCard.tsx").exists())
        for rel in ("components/MediaSection.tsx", "compositions/MainVideo.tsx",
                    "compositions/Short.tsx", "components/StandardSubtitles.tsx",
                    "components/KaraokeSubtitles.tsx"):
            # Strip comment lines — explanatory comments may name the retired
            # mechanisms; only actual code usage must be absent.
            src = "\n".join(
                line for line in (_REMOTION_SRC / rel).read_text().splitlines()
                if not line.lstrip().startswith(("//", "*", "/*"))
            )
            self.assertNotIn("TextOverlay", src, rel)
            self.assertNotIn("suppressWindows", src, rel)
            self.assertNotIn("<TextCard", src, rel)

    def test_props_builder_has_no_script_text_fallback(self):
        src = Path("app/agents/agent5_render/services/remotion_builder.py").read_text()
        self.assertNotIn('s.get("overlay_text", "") or s.get("script_text", "")', src)


if __name__ == "__main__":
    unittest.main()
