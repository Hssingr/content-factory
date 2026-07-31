"""Runtime proof for the Agent 1 research-ideas two-call reliability redesign
(app/agents/agent1_setup/system_prompt.py's research_channel_ideas()).

Background: the original single-call schema required ~30 leaf fields in one
forced-tool-use response and failed twice in real production runs with
FastAPI ResponseValidationError (Anthropic's forced tool-use does not
strictly enforce a JSON Schema's "required" list). In both real incidents,
editable_config itself was always complete and correct — what got dropped
was either most of the narrative fields (max_tokens truncation) or exactly
six fields that are literal semantic duplicates of editable_config's own
fields. This redesign splits the call in two: step 1
(task="channel_concept_validation") owns editable_config exclusively, step 2
(task="channel_research") owns narrative analysis only and is structurally
never given the six duplicate fields or best_script_source to fill in.
`_merge_research_steps()` derives them once, in Python, from step 1's
editable_config.

No live API calls (CLAUDE.md §19.1): `_merge_research_steps()` is tested as
a pure function with hand-built dicts (no Claude call at all); the
orchestration tests patch `system_prompt.call_claude_structured` directly
(the same level `scripts/smoke_agent1_research_ideas.py` mocks at) since the
generic `call_claude_structured()` truncation/shape behavior is already
covered by `tests/test_claude_structured_truncation.py` — this file tests
research_channel_ideas()'s own sequencing/merge logic, not the shared client.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.agents.agent1_setup import system_prompt
from app.schemas.research_ideas import ResearchIdeasResponse


def _concept(**overrides) -> dict:
    editable_config = {
        "channel_name": "Reel Legends",
        "description": "A deep dive into film history.",
        "niche": "film history",
        "tone": "documentary",
        "script_source": "reddit",
        "output_mode": "youtube_and_shorts",
        "visual_style": "documentary",
        "image_style": "cinematic_realism",
        "languages": ["en"],
        "platforms": ["youtube", "tiktok"],
        "videos_per_week": 4,
        "subreddits": ["r/movies"],
        "story_generation_prompt": None,
    }
    editable_config.update(overrides.pop("editable_config", {}))
    return {
        "description_issues": overrides.pop("description_issues", []),
        "assumption_note": overrides.pop("assumption_note", None),
        "editable_config": editable_config,
        **overrides,
    }


def _narrative(**overrides) -> dict:
    base = {
        "recommended_channel_concept": "Reel Legends: untold film history",
        "why_selected": "Strong retention, sourcing, and monetization fit.",
        "rpm_potential": "medium",
        "follower_growth_potential": "high",
        "platform_suitability": [
            {"platform": "youtube", "fit": "very_high", "reasoning": "Long-form narrated stories perform well."},
        ],
        "suggested_channel_names": ["Reel Legends", "Film Vault"],
        "example_video_ideas": ["The forgotten film that almost ruined a studio"],
        "risks_difficulty": ["Saturated niche"],
        "final_recommendation_summary": "A strong, feasible starting point.",
        "references_used": [],
    }
    base.update(overrides)
    return base


class MergeResearchStepsTest(unittest.TestCase):
    """_merge_research_steps() is a pure function — no Claude mocking needed."""

    def test_derived_fields_come_from_editable_config_not_narrative(self) -> None:
        concept = _concept()
        narrative = _narrative()
        merged = system_prompt._merge_research_steps(concept, narrative)
        rec = merged["primary_recommendation"]

        self.assertEqual(rec["best_script_source"], "reddit")
        self.assertEqual(rec["recommended_output_mode"], "youtube_and_shorts")
        self.assertEqual(rec["recommended_visual_style"], "documentary")
        self.assertEqual(rec["recommended_image_style"], "cinematic_realism")
        self.assertEqual(rec["recommended_tone"], "documentary")
        self.assertEqual(rec["recommended_target_languages"], ["en"])
        self.assertEqual(rec["recommended_platforms"], ["youtube", "tiktok"])
        self.assertEqual(rec["editable_config"], concept["editable_config"])

    def test_script_source_ai_generated_carries_through_unchanged(self) -> None:
        concept = _concept(editable_config={"script_source": "ai_generated", "subreddits": [], "story_generation_prompt": "A war-torn city under siege."})
        merged = system_prompt._merge_research_steps(concept, _narrative())
        self.assertEqual(merged["primary_recommendation"]["best_script_source"], "ai_generated")

    def test_output_mode_youtube_long_only_does_not_crash_validation(self) -> None:
        """Regression test for a real latent bug the design review caught:
        ResearchRecommendation.recommended_output_mode used to be a narrower
        Literal missing "youtube_long_only", which editable_config.output_mode
        already allows and CLAUDE.md documents as executable."""
        concept = _concept(editable_config={"output_mode": "youtube_long_only"})
        merged = system_prompt._merge_research_steps(concept, _narrative())
        validated = ResearchIdeasResponse.model_validate(merged)
        self.assertEqual(validated.primary_recommendation.recommended_output_mode, "youtube_long_only")

    def test_research_label_is_always_the_hardcoded_constant(self) -> None:
        merged = system_prompt._merge_research_steps(_concept(), _narrative())
        self.assertEqual(merged["research_label"], system_prompt._RESEARCH_LABEL)
        self.assertEqual(
            merged["research_label"],
            "AI market research estimate — not verified platform analytics",
        )

    def test_assumption_note_comes_from_concept_not_narrative(self) -> None:
        concept = _concept(assumption_note="Assumed a general English-speaking audience.")
        merged = system_prompt._merge_research_steps(concept, _narrative())
        self.assertEqual(
            merged["primary_recommendation"]["assumption_note"],
            "Assumed a general English-speaking audience.",
        )

    def test_merged_result_validates_against_response_schema(self) -> None:
        merged = system_prompt._merge_research_steps(_concept(), _narrative())
        validated = ResearchIdeasResponse.model_validate(merged)
        self.assertEqual(validated.primary_recommendation.editable_config.channel_name, "Reel Legends")
        self.assertFalse(hasattr(validated, "alternative_ideas"))

    def test_no_alternative_ideas_key_in_merged_output(self) -> None:
        merged = system_prompt._merge_research_steps(_concept(), _narrative())
        self.assertNotIn("alternative_ideas", merged)


class ResearchChannelIdeasOrchestrationTest(unittest.TestCase):
    """research_channel_ideas() calls step 1 then step 2, in order, and wires
    step 1's output into step 2's input context — patches
    system_prompt.call_claude_structured directly (same boundary
    scripts/smoke_agent1_research_ideas.py mocks at)."""

    def _stub(self, concept_response: dict, narrative_response: dict):
        calls = []

        def fake(**kwargs):
            calls.append(kwargs)
            if kwargs["task"] == "channel_concept_validation":
                return concept_response
            return narrative_response

        return calls, fake

    def test_calls_both_steps_in_order_with_correct_task_keys(self) -> None:
        calls, fake = self._stub(_concept(), _narrative())
        with patch.object(system_prompt, "call_claude_structured", fake):
            result = system_prompt.research_channel_ideas("A channel about old films", mode="validate")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["task"], "channel_concept_validation")
        self.assertEqual(calls[1]["task"], "channel_research")
        self.assertEqual(calls[0]["schema_name"], "channel_concept_validation")
        self.assertEqual(calls[1]["schema_name"], "channel_research_narrative")
        ResearchIdeasResponse.model_validate(result)

    def test_step2_receives_step1_finalized_editable_config(self) -> None:
        concept_response = _concept(editable_config={"channel_name": "Distinct Name For This Test"})
        calls, fake = self._stub(concept_response, _narrative())
        with patch.object(system_prompt, "call_claude_structured", fake):
            system_prompt.research_channel_ideas("desc", mode="validate")

        step2_context = json.loads(calls[1]["user_message"])
        self.assertEqual(
            step2_context["finalized_editable_config"]["channel_name"],
            "Distinct Name For This Test",
        )

    def test_step2_schema_never_includes_the_six_duplicate_fields_or_best_script_source(self) -> None:
        """Structural proof the second incident cannot recur: these fields
        are not merely unlikely to be omitted by step 2 — they are absent
        from its schema entirely, so omission is impossible."""
        forbidden = {
            "best_script_source", "recommended_output_mode",
            "recommended_visual_style", "recommended_image_style",
            "recommended_tone", "recommended_target_languages",
            "recommended_platforms", "editable_config",
        }
        schema_fields = set(system_prompt._RESEARCH_NARRATIVE_SCHEMA["properties"])
        self.assertEqual(schema_fields & forbidden, set())

    def test_step1_schema_is_exactly_the_shape_that_survived_both_incidents(self) -> None:
        required = set(system_prompt._CONCEPT_VALIDATION_SCHEMA["required"])
        self.assertEqual(required, {"description_issues", "editable_config"})
        editable_config_required = set(
            system_prompt._CONCEPT_VALIDATION_SCHEMA["properties"]["editable_config"]["required"]
        )
        self.assertEqual(
            editable_config_required,
            {
                "channel_name", "description", "niche", "tone", "script_source",
                "output_mode", "visual_style", "image_style", "languages",
                "platforms", "videos_per_week", "subreddits",
            },
        )

    def test_explore_mode_empty_description_reaches_step1_as_synthetic_brief(self) -> None:
        calls, fake = self._stub(_concept(), _narrative())
        with patch.object(system_prompt, "call_claude_structured", fake):
            system_prompt.research_channel_ideas("", mode="explore")

        step1_context = json.loads(calls[0]["user_message"])
        self.assertEqual(step1_context["mode"], "explore")
        self.assertIn("starting from scratch", step1_context["channel_description"])

    def test_validate_mode_empty_description_raises_before_any_claude_call(self) -> None:
        def fail_if_called(**kwargs):
            raise AssertionError("Claude must not be called for an empty validate-mode description")

        with patch.object(system_prompt, "call_claude_structured", fail_if_called):
            with self.assertRaises(ValueError):
                system_prompt.research_channel_ideas("   ", mode="validate")

    def test_dead_backfill_function_and_constant_are_removed(self) -> None:
        self.assertFalse(hasattr(system_prompt, "_backfill_recommendation_from_editable_config"))
        self.assertFalse(hasattr(system_prompt, "_RECOMMENDATION_EDITABLE_CONFIG_FALLBACKS"))


if __name__ == "__main__":
    unittest.main()
