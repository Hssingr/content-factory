"""Runtime proof for Elimination Mandate Phase 1c (D2.1-D2.4) —
code_report/forensic_output_audit_borrasca_run.md.

A real production run showed the AI-generated visual bible return 0
locations, 0 recurring motifs, and 0 first-15 rules — a total no-op that
still cost a Claude call and tokens on every storyboard batch, plus paid for
a cinematic-prompt-enrichment pass and a first-15 enhancement/validation pass
that had nothing real to enrich or validate. This file proves:

1. ``build_continuity_line_from_blueprint()`` extracts recurring proper nouns
   deterministically (no AI call) and degrades safely to "" when nothing
   recurs.
2. ``generate_storyboard_batch()`` threads a plain ``continuity_line`` string
   into the user message — no compaction, no JSON block.
3. ``split_into_beats()`` threads the same line through to the real Claude
   boundary, with only ``call_claude_structured_with_usage`` stubbed.
4. The visual-bible layer, cinematic-prompt enrichment, first15
   enhancement/validation, and the final-prompt re-validation passes are
   genuinely gone — not just unused.
5. ``derive_text_prop_prompt()`` never rewrites the subject: Claude's own
   prompt survives verbatim, plus exactly one no-readable-text clause.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.subagents.storyboard import (
    build_continuity_line_from_blueprint,
    split_into_beats,
)
from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.services import visual_bible
from app.agents.agent4_visuals.services import visual_orchestrator


class _Channel:
    niche = "Reddit horror story narration"
    tone = "documentary"


class TestBuildContinuityLineFromBlueprint(unittest.TestCase):
    def test_recurring_names_extracted_deterministically(self):
        blueprint = {
            "hook": "Sam hears a grinding sound from Borrasca every night.",
            "major_turns": [
                "Sam finds Whitney's name carved into the tree.",
                "Sam and Kyle go into Borrasca together.",
            ],
            "final_payoff": "Sam finds Whitney alive inside Borrasca.",
        }
        line = build_continuity_line_from_blueprint(blueprint)
        self.assertIn("Sam", line)
        self.assertIn("Borrasca", line)
        self.assertIn("Whitney", line)
        # Deterministic: calling it again with the same input gives the same line.
        self.assertEqual(line, build_continuity_line_from_blueprint(blueprint))

    def test_names_appearing_only_once_are_not_included(self):
        blueprint = {
            "hook": "Sam hears a grinding sound from the mountain.",
            "major_turns": ["A stranger named Prescott signed the form once."],
            "final_payoff": "Sam finds the truth.",
        }
        line = build_continuity_line_from_blueprint(blueprint)
        self.assertNotIn("Prescott", line)  # appears exactly once — not "recurring"
        self.assertIn("Sam", line)  # appears twice (hook + final_payoff)

    def test_empty_blueprint_returns_empty_string(self):
        self.assertEqual(build_continuity_line_from_blueprint(None), "")
        self.assertEqual(build_continuity_line_from_blueprint({}), "")

    def test_blueprint_with_no_recurring_entities_returns_empty_string(self):
        blueprint = {"hook": "A quiet town.", "major_turns": [], "final_payoff": "The end."}
        self.assertEqual(build_continuity_line_from_blueprint(blueprint), "")

    def test_sentence_starter_stopwords_never_counted_as_entities(self):
        blueprint = {
            "hook": "The sound never stops. The house is empty. The truth waits.",
            "major_turns": ["The night comes. The fear grows."],
            "final_payoff": "The end arrives.",
        }
        line = build_continuity_line_from_blueprint(blueprint)
        self.assertEqual(line, "")  # "The" repeats but is stopworded


class TestGenerateStoryboardBatchThreadsContinuityLine(unittest.TestCase):
    def test_continuity_line_appears_in_user_message_no_compaction(self):
        captured = {}

        def fake_structured(**kwargs):
            captured["user_message"] = kwargs["user_message"]
            return {"beats": []}, {"input_tokens": 10, "output_tokens": 10}

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_structured):
            system_prompt.generate_storyboard_batch(
                segment_label="[INTRO]",
                segment_text="Sam hears the sound again.",
                segment_index=1,
                segment_count=1,
                channel=_Channel(),
                continuity_line="Visual continuity: Sam, Borrasca.",
            )

        self.assertIn("Visual continuity: Sam, Borrasca.", captured["user_message"])
        # No JSON/compact-bible artifact of any kind.
        self.assertNotIn("compact JSON", captured["user_message"])

    def test_empty_continuity_line_adds_nothing(self):
        captured = {}

        def fake_structured(**kwargs):
            captured["user_message"] = kwargs["user_message"]
            return {"beats": []}, {"input_tokens": 10, "output_tokens": 10}

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_structured):
            system_prompt.generate_storyboard_batch(
                segment_label="[INTRO]",
                segment_text="Sam hears the sound again.",
                segment_index=1,
                segment_count=1,
                channel=_Channel(),
                continuity_line="",
            )

        self.assertNotIn("Visual continuity", captured["user_message"])


class TestSplitIntoBeatsThreadsContinuityLineToRealClaudeBoundary(unittest.TestCase):
    def test_continuity_line_reaches_generate_storyboard_batch_call(self):
        transcript_words = "Sam hears the sound again tonight".split()
        transcript = [
            {"word": w, "start": i * 0.4, "end": (i + 1) * 0.4}
            for i, w in enumerate(transcript_words)
        ]
        captured_messages: list[str] = []

        def fake_structured(**kwargs):
            captured_messages.append(kwargs["user_message"])
            return (
                {
                    "beats": [{
                        "beat_order": 0,
                        "start_hint": "Sam hears",
                        "end_hint": "again tonight",
                        "visual_intent": "Sam listening in the dark",
                        "visual_type": "b-roll",
                        "visual_category": "person",
                        "environment": "indoor_domestic",
                        "flux_prompt": "A man listening intently in a dim room, photorealistic",
                        "effect": "slow_zoom",
                        "color_grade": "desaturated",
                        "transition_to_next": "cut",
                        "motif": "other",
                        "beat_intensity": "medium",
                        "suggested_duration_sec": 2.4,
                    }],
                },
                {"input_tokens": 10, "output_tokens": 10},
            )

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_structured):
            beats = split_into_beats(
                voice_script="[INTRO]\nSam hears the sound again tonight",
                duration_ms=2400,
                channel=_Channel(),
                script_format="youtube_long",
                whisper_transcript=transcript,
                continuity_line="Visual continuity: Sam, Borrasca.",
            )

        self.assertIsNotNone(beats)
        self.assertTrue(any("Visual continuity: Sam, Borrasca." in msg for msg in captured_messages))


class TestVisualBibleLayerFullyRemoved(unittest.TestCase):
    """The AI-generating layer is deleted — not disabled. Read-only helpers
    survive only so the local-debug review page can still show an old bible
    file left on disk by a run from before this change."""

    def test_generation_function_and_schema_gone(self):
        for name in (
            "generate_visual_bible_for_content",
            "validate_visual_bible",
            "VisualBibleIssue",
            "VISUAL_BIBLE_TASK",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(visual_bible, name))

    def test_read_only_helpers_survive(self):
        self.assertTrue(hasattr(visual_bible, "get_visual_bible_path"))
        self.assertTrue(hasattr(visual_bible, "load_visual_bible_for_content"))
        self.assertIsNone(visual_bible.load_visual_bible_for_content("nonexistent-content-id"))

    def test_cinematic_prompts_module_deleted(self):
        with self.assertRaises(ModuleNotFoundError):
            import importlib
            importlib.import_module("app.agents.agent4_visuals.services.cinematic_prompts")

    def test_first15_validator_module_deleted(self):
        with self.assertRaises(ModuleNotFoundError):
            import importlib
            importlib.import_module("app.agents.agent4_visuals.services.first15_validator")

    def test_final_prompt_issues_checker_removed_from_orchestrator(self):
        self.assertFalse(hasattr(visual_orchestrator, "_check_final_prompt_issues"))
        self.assertFalse(hasattr(visual_orchestrator, "_FINAL_PROMPT_VALIDATION_CHECKS"))


class TestTextPropSanitizerMaterialReframe(unittest.TestCase):
    """Roadmap A3.1: text objects become physical material details."""

    def test_reframes_subject_without_repeating_text_object(self):
        beat = {
            "flux_prompt": "A weathered case file left open on a warehouse desk, dust in the air",
            "visual_intent": "evidence of the cover-up",
            "motif": "document",
        }
        self.assertTrue(flux_generator.is_text_prop_beat(beat))
        result = flux_generator.derive_text_prop_prompt(beat)
        self.assertTrue(result.startswith("paper texture"))
        self.assertNotIn("case file", result)
        self.assertNotIn("no readable text", result)
        # No old framing/negative-wall artifacts.
        self.assertNotIn("blank and unmarked", result)
        self.assertNotIn("illegible or blank surface only", result)

    def test_reframes_visual_intent_when_prompt_empty(self):
        beat = {"flux_prompt": "", "visual_intent": "a case file on a desk", "motif": "document"}
        result = flux_generator.derive_text_prop_prompt(beat)
        self.assertTrue(result.startswith("paper texture"))
        self.assertNotIn("case file", result)

    def test_helper_functions_for_old_rewrite_mechanism_are_gone(self):
        for name in (
            "_select_text_prop_framing",
            "_detect_text_prop_label",
            "_compact_context",
            "_ENVIRONMENT_SCENE_HINTS",
            "_TEXT_PROP_FRAMING_CLAUSES",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(flux_generator, name))


if __name__ == "__main__":
    unittest.main()
