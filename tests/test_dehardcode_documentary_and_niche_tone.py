"""Runtime proof for the remaining two halves of roadmap Phase 4a (P1-9):
de-hardcoding "documentary" from Agent 4/5 runtime defaults, and the Agent 1
niche<->tone contradiction flag.
code_report/forensic_output_audit_borrasca_run.md.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals import system_prompt as agent4_prompt
from app.agents.agent5_render.services import remotion_builder
from app.agents.agent1_setup.services.niche_tone_check import detect_niche_tone_contradiction
from app.agents.agent1_setup.services.activation_readiness import check_activation_readiness

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_migration_module(filename: str):
    path = _REPO_ROOT / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(filename[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Agent 4: storyboard identity line + fallback defaults ─────────────────────

class TestAgent4NoLongerAssumesDocumentary(unittest.TestCase):
    def test_identity_line_does_not_assume_documentary(self):
        # The identity line itself must no longer name a specific format.
        first_paragraph = agent4_prompt._STORYBOARD_SYSTEM_PROMPT.split("\n\n")[0].lower()
        self.assertNotIn("documentary", first_paragraph)

    def test_storyboard_batch_defaults_to_story_driven_when_unconfigured(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return {"beats": []}, {"input_tokens": 10, "output_tokens": 10}

        with patch.object(agent4_prompt, "call_claude_structured_with_usage", side_effect=fake_structured):
            agent4_prompt.generate_storyboard_batch(
                segment_label="[INTRO]", segment_text="Something happens.",
                segment_index=1, segment_count=1,
                channel=SimpleNamespace(niche="horror", tone="tense"),
                visual_style="", image_style="not-empty-to-trigger-direction-lines",
            )

        self.assertIn("Global visual direction: story_driven", captured["user_message"])
        self.assertNotIn("documentary", captured["user_message"].lower())

    def test_storyboard_batch_respects_explicit_visual_style(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return {"beats": []}, {"input_tokens": 10, "output_tokens": 10}

        with patch.object(agent4_prompt, "call_claude_structured_with_usage", side_effect=fake_structured):
            agent4_prompt.generate_storyboard_batch(
                segment_label="[INTRO]", segment_text="Something happens.",
                segment_index=1, segment_count=1,
                channel=SimpleNamespace(niche="horror", tone="tense"),
                visual_style="noir", image_style="photorealistic",
            )

        self.assertIn("Global visual direction: noir", captured["user_message"])


# ── Agent 5: props builder / render fallback defaults ─────────────────────────

class TestAgent5NoLongerDefaultsToDocumentary(unittest.TestCase):
    def test_props_builders_no_longer_carry_channel_style_at_all(self):
        # Migration 009 (channel_config cleanup) removed video_style_type/
        # video_color_grade entirely — their consumer chain ended in a props
        # "config" key no Remotion component ever read. The de-hardcoding this
        # class originally proved is now moot: the parameters must not exist.
        import inspect
        for fn in (remotion_builder.build_main_props, remotion_builder.build_short_props):
            with self.subTest(fn=fn.__name__):
                params = inspect.signature(fn).parameters
                self.assertNotIn("channel_style", params)
                self.assertNotIn("channel_color_grade", params)

    def test_video_module_source_has_no_documentary_fallback(self):
        import inspect
        from app.agents.agent5_render.services import video as video_module
        source = inspect.getsource(video_module)
        self.assertNotIn('"documentary"', source)


# ── Agent 1: niche/tone contradiction detection ───────────────────────────────

class TestDetectNicheToneContradiction(unittest.TestCase):
    def test_the_audited_incident_is_flagged(self):
        findings = detect_niche_tone_contradiction(
            "Reddit horror story narration", "documentary",
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "niche_tone_contradiction")
        self.assertIn("horror", findings[0]["message"])

    def test_matching_tension_tone_is_not_flagged(self):
        findings = detect_niche_tone_contradiction(
            "Reddit horror story narration", "suspenseful",
        )
        self.assertEqual(findings, [])

    def test_non_tension_niche_is_never_flagged(self):
        findings = detect_niche_tone_contradiction(
            "Cooking recipes and food reviews", "documentary",
        )
        self.assertEqual(findings, [])

    def test_case_and_whitespace_insensitive_tone_match(self):
        findings = detect_niche_tone_contradiction(
            "true crime investigation channel", "  Documentary  ",
        )
        self.assertEqual(len(findings), 1)

    def test_empty_inputs_never_flagged(self):
        self.assertEqual(detect_niche_tone_contradiction("", "documentary"), [])
        self.assertEqual(detect_niche_tone_contradiction("horror stories", ""), [])
        self.assertEqual(detect_niche_tone_contradiction("", ""), [])


class TestActivationReadinessSurfacesContradictionAsWarningOnly(unittest.TestCase):
    """Real check_activation_readiness() call — a fully-ready channel fixture
    except for the niche/tone mismatch, proving the finding lands in
    `warnings` and never blocks `ready`."""

    def _ready_channel(self, *, niche: str, tone: str):
        config = SimpleNamespace(content_mode="single_story", script_source="reddit", output_mode="youtube_and_shorts")
        language = SimpleNamespace(language="en")
        voice = SimpleNamespace(language="en")
        source = SimpleNamespace()
        timing = SimpleNamespace()
        platform = SimpleNamespace(platform="youtube", language="en", verified=True)
        return SimpleNamespace(
            niche=niche, tone=tone, config=config,
            languages=[language], voices=[voice], sources=[source],
            publish_timings=[timing], platforms=[platform],
        )

    def test_contradiction_is_a_warning_and_activation_still_ready(self):
        channel = self._ready_channel(niche="Reddit horror story narration", tone="documentary")
        result = check_activation_readiness(channel)

        self.assertTrue(result["ready"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["severity"], "WARNING")
        self.assertEqual(result["warnings"][0]["code"], "niche_tone_contradiction")

    def test_no_contradiction_means_no_warnings(self):
        channel = self._ready_channel(niche="Reddit horror story narration", tone="suspenseful")
        result = check_activation_readiness(channel)

        self.assertTrue(result["ready"])
        self.assertEqual(result["warnings"], [])


# ── Migration 008 structural proof ────────────────────────────────────────────

class TestMigration008Structure(unittest.TestCase):
    def setUp(self):
        self.mod = _load_migration_module(
            "008_add_narration_pov_and_story_driven_default.py"
        )

    def test_revision_chain_links_to_007(self):
        self.assertEqual(self.mod.revision, "008")
        self.assertEqual(self.mod.down_revision, "007")

    def test_upgrade_and_downgrade_are_defined(self):
        self.assertTrue(callable(self.mod.upgrade))
        self.assertTrue(callable(self.mod.downgrade))

    def test_upgrade_adds_narration_pov_and_updates_style_defaults(self):
        calls = {"add_column": [], "alter_column": []}

        class _FakeOp:
            @staticmethod
            def add_column(table, column):
                calls["add_column"].append((table, column.name, column.server_default))

            @staticmethod
            def alter_column(table, column_name, **kwargs):
                calls["alter_column"].append((table, column_name, kwargs.get("server_default")))

        with patch.object(self.mod, "op", _FakeOp):
            self.mod.upgrade()

        self.assertEqual(len(calls["add_column"]), 1)
        table, name, _ = calls["add_column"][0]
        self.assertEqual((table, name), ("channel_config", "narration_pov"))

        altered = {(t, c): d for t, c, d in calls["alter_column"]}
        self.assertEqual(altered[("channel_config", "visual_style")], "story_driven")
        self.assertEqual(altered[("channel_config", "video_style_type")], "story_driven")


if __name__ == "__main__":
    unittest.main()
