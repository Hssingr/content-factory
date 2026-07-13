"""Runtime proof for roadmap Phase C2/C3/C4 (operator video-output audit,
cross-validated against the operator's own frame-by-frame video review).

C2 — Character/style continuity: a real production run showed a story's
protagonist rendered as 6+ visually different women across beats, and ONE
video mixing photoreal-cinematic, flat cartoon, and anime-style renders with
nothing enforcing consistency. Agent 2's story blueprint now carries
locked `character_descriptors` (name/age/one-line physical description,
generated once at blueprint time, zero extra AI calls) and Agent 4 threads
them into every storyboard batch via the existing deterministic
`continuity_line` mechanism (Elimination Mandate D2.1 — no AI call, no
revert of the deleted visual bible). A deterministic per-`image_style`
negative-constraint line stops a beat's rendering drifting into a
neighboring style.

C3 — Era lock: the same run shipped modern anachronisms (a modern flag, a
modern city waterfront, contemporary clothing) inside a 6th-century
Byzantine story. Agent 2's blueprint now also carries `era_setting` (one
phrase naming the story's period/place), threaded the same deterministic
way.

C4 — Storyboard delivery tuning: real runs under-delivered ~65% of the
target beat count and logged 100%-invalid-hint-rate segments from
under-length start_hint/end_hint values. Both prompt rules are tightened
from soft ("aim for") to explicit requirements with stated rationale.

Only the paid `call_claude_structured`/`call_claude_structured_with_usage`
boundaries are stubbed — everything else (schema, message-building,
continuity-line formatting) is real, per CLAUDE.md §19.1/§19.4.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent2_discovery import system_prompt as agent2_system_prompt
from app.agents.agent4_visuals import system_prompt as agent4_system_prompt
from app.agents.agent4_visuals.subagents.storyboard import (
    build_continuity_line_from_blueprint,
    _format_character_descriptors_line,
    _format_era_setting_line,
)


class _Story:
    url = "https://reddit.com/r/nosleep/comments/abc/x"
    title = "A story"
    body = "Belisarius marched into Constantinople under a grey sky."
    language = "en"


class _Channel:
    niche = "history"
    tone = "documentary"


# ── C2a: blueprint schema/prompt gained character_descriptors + era_setting ──

class BlueprintSchemaGainedFieldsTest(unittest.TestCase):
    def test_schema_requires_both_new_fields(self):
        schema = agent2_system_prompt._STORY_BLUEPRINT_SCHEMA
        self.assertIn("character_descriptors", schema["properties"])
        self.assertIn("era_setting", schema["properties"])
        self.assertIn("character_descriptors", schema["required"])
        self.assertIn("era_setting", schema["required"])

    def test_character_descriptors_schema_shape(self):
        entry_schema = agent2_system_prompt._STORY_BLUEPRINT_SCHEMA["properties"][
            "character_descriptors"
        ]["items"]
        self.assertEqual(
            set(entry_schema["required"]), {"name", "age", "description"},
        )
        self.assertEqual(
            agent2_system_prompt._STORY_BLUEPRINT_SCHEMA["properties"][
                "character_descriptors"
            ]["maxItems"],
            5,
        )

    def test_prompt_documents_both_fields(self):
        prompt = agent2_system_prompt._STORY_BLUEPRINT_SYSTEM_PROMPT
        self.assertIn("character_descriptors", prompt)
        self.assertIn("era_setting", prompt)


class GenerateStoryBlueprintPropagatesNewFieldsTest(unittest.TestCase):
    """Runtime proof: generate_story_blueprint() (real function) forwards
    both new fields through untouched — only call_claude_structured stubbed."""

    def _fake_response(self, **overrides) -> dict:
        base = {
            "hook": "h", "central_question": "q",
            "major_turns": ["turn one", "turn two"],
            "final_payoff": "p", "comment_trigger": "c?",
            "midpoint_retention_trap": "trap",
            "suggested_section_count": 3, "suggested_title": "t",
            "character_descriptors": [
                {"name": "Belisarius", "age": "mid-30s", "description": "tall, dark beard, bronze cuirass"},
            ],
            "era_setting": "6th-century Byzantine Constantinople",
        }
        base.update(overrides)
        return base

    def test_character_descriptors_and_era_setting_survive_the_real_function(self):
        with patch.object(
            agent2_system_prompt, "call_claude_structured",
            side_effect=lambda **kwargs: self._fake_response(),
        ):
            result = agent2_system_prompt.generate_story_blueprint(_Story(), _Channel())

        self.assertEqual(result["era_setting"], "6th-century Byzantine Constantinople")
        self.assertEqual(result["character_descriptors"][0]["name"], "Belisarius")

    def test_empty_character_descriptors_is_tolerated(self):
        with patch.object(
            agent2_system_prompt, "call_claude_structured",
            side_effect=lambda **kwargs: self._fake_response(character_descriptors=[], era_setting=""),
        ):
            result = agent2_system_prompt.generate_story_blueprint(_Story(), _Channel())

        self.assertEqual(result["character_descriptors"], [])
        self.assertEqual(result["era_setting"], "")


# ── C2b: build_continuity_line_from_blueprint() extensions ──────────────────

class FormatCharacterDescriptorsLineTest(unittest.TestCase):
    def test_well_formed_entries_format_correctly(self):
        line = _format_character_descriptors_line([
            {"name": "Belisarius", "age": "mid-30s", "description": "tall, dark beard, bronze cuirass"},
            {"name": "Theodora", "age": "20s", "description": "petite, dark hair, purple robes"},
        ])
        self.assertIn("Belisarius, mid-30s — tall, dark beard, bronze cuirass", line)
        self.assertIn("Theodora, 20s — petite, dark hair, purple robes", line)
        self.assertTrue(line.startswith("Character identities"))

    def test_age_is_optional(self):
        line = _format_character_descriptors_line([
            {"name": "Belisarius", "age": "", "description": "tall, dark beard"},
        ])
        self.assertIn("Belisarius — tall, dark beard", line)
        self.assertNotIn("Belisarius,", line)

    def test_entry_missing_name_or_description_is_skipped(self):
        line = _format_character_descriptors_line([
            {"name": "", "age": "20s", "description": "tall"},
            {"name": "Marcus", "age": "20s", "description": ""},
        ])
        self.assertEqual(line, "")

    def test_malformed_entry_is_skipped_not_crashing(self):
        line = _format_character_descriptors_line(["not-a-dict", {"name": "Marcus", "age": "", "description": "short"}])
        self.assertIn("Marcus", line)

    def test_empty_or_none_returns_empty_string(self):
        self.assertEqual(_format_character_descriptors_line([]), "")
        self.assertEqual(_format_character_descriptors_line(None), "")

    def test_capped_at_five_entries(self):
        entries = [
            {"name": f"Person{i}", "age": "30s", "description": "tall"} for i in range(8)
        ]
        line = _format_character_descriptors_line(entries)
        for i in range(5):
            self.assertIn(f"Person{i}", line)
        for i in range(5, 8):
            self.assertNotIn(f"Person{i}", line)


class FormatEraSettingLineTest(unittest.TestCase):
    def test_formats_the_era_lock_line(self):
        line = _format_era_setting_line("6th-century Byzantine Constantinople")
        self.assertIn("6th-century Byzantine Constantinople", line)
        self.assertTrue(line.startswith("Era/setting lock"))
        self.assertIn("anachronistic", line)

    def test_empty_or_none_returns_empty_string(self):
        self.assertEqual(_format_era_setting_line(""), "")
        self.assertEqual(_format_era_setting_line(None), "")


class BuildContinuityLineCombinedTest(unittest.TestCase):
    def test_all_three_lines_present_when_blueprint_has_everything(self):
        blueprint = {
            "hook": "Belisarius arrived. Belisarius never retreated.",
            "final_payoff": "", "midpoint_retention_trap": "", "central_question": "",
            "major_turns": [],
            "character_descriptors": [
                {"name": "Belisarius", "age": "mid-30s", "description": "tall, dark beard"},
            ],
            "era_setting": "6th-century Byzantine Constantinople",
        }
        line = build_continuity_line_from_blueprint(blueprint)
        self.assertIn("Visual continuity:", line)
        self.assertIn("Character identities", line)
        self.assertIn("Era/setting lock", line)
        # Three distinct lines, newline-separated.
        self.assertEqual(len(line.split("\n")), 3)

    def test_legacy_blueprint_without_new_fields_matches_pre_existing_output(self):
        # A blueprint generated before roadmap Phase C2/C3 shipped simply
        # lacks these keys — output must degrade to exactly the old
        # single-line-or-empty behavior, no regeneration forced.
        blueprint = {
            "hook": "Sam hears it. Sam hears it again.",
            "final_payoff": "", "midpoint_retention_trap": "", "central_question": "",
            "major_turns": [],
        }
        line = build_continuity_line_from_blueprint(blueprint)
        self.assertEqual(
            line,
            "Visual continuity: these names recur throughout this story — give "
            "each a stable, consistent physical identity/appearance across beats: Sam.",
        )
        self.assertNotIn("Character identities", line)
        self.assertNotIn("Era/setting lock", line)

    def test_none_blueprint_returns_empty_string(self):
        self.assertEqual(build_continuity_line_from_blueprint(None), "")

    def test_only_era_setting_present(self):
        blueprint = {
            "hook": "", "final_payoff": "", "midpoint_retention_trap": "", "central_question": "",
            "major_turns": [], "character_descriptors": [],
            "era_setting": "1920s rural Midwest",
        }
        line = build_continuity_line_from_blueprint(blueprint)
        self.assertEqual(line, _format_era_setting_line("1920s rural Midwest"))


# ── C2c/C3: real generate_storyboard_batch() message threading ──────────────

class GenerateStoryboardBatchThreadsNewLinesTest(unittest.TestCase):
    def _capture_message(self, **call_kwargs) -> str:
        captured = {}

        def fake_structured(**kwargs):
            captured["user_message"] = kwargs["user_message"]
            return {"beats": []}, {"input_tokens": 10, "output_tokens": 10}

        with patch.object(agent4_system_prompt, "call_claude_structured_with_usage", side_effect=fake_structured):
            agent4_system_prompt.generate_storyboard_batch(
                segment_label="[INTRO]", segment_text="Belisarius marched north.",
                segment_index=1, segment_count=1, channel=_Channel(),
                **call_kwargs,
            )
        return captured["user_message"]

    def test_character_and_era_lines_reach_the_real_claude_boundary(self):
        continuity_line = build_continuity_line_from_blueprint({
            "hook": "", "final_payoff": "", "midpoint_retention_trap": "", "central_question": "",
            "major_turns": [],
            "character_descriptors": [
                {"name": "Belisarius", "age": "mid-30s", "description": "tall, dark beard, bronze cuirass"},
            ],
            "era_setting": "6th-century Byzantine Constantinople",
        })
        message = self._capture_message(continuity_line=continuity_line)
        self.assertIn("Character identities", message)
        self.assertIn("Belisarius, mid-30s — tall, dark beard, bronze cuirass", message)
        self.assertIn("Era/setting lock", message)
        self.assertIn("6th-century Byzantine Constantinople", message)

    def test_style_constraint_line_present_for_recognized_image_style(self):
        message = self._capture_message(image_style="cinematic_cartoon")
        self.assertIn("Style constraints (this image style must NOT look like):", message)
        self.assertIn("no photorealism", message)
        self.assertIn("no anime linework", message)

    def test_style_constraint_differs_per_image_style(self):
        photoreal_msg = self._capture_message(image_style="photorealistic")
        anime_msg = self._capture_message(image_style="anime")
        self.assertIn("no illustration, no painting, no cartoon, no anime", photoreal_msg)
        self.assertIn("no photorealistic photography, no live-action imagery", anime_msg)
        self.assertNotEqual(photoreal_msg, anime_msg)

    def test_every_documented_image_style_has_a_constraint_entry(self):
        # Keep this table in sync with app/ui/src/constants.js's
        # IMAGE_STYLE_OPTIONS (CLAUDE.md §8.4) — this test only proves
        # internal self-consistency (the storyboard prompt's own documented
        # vocabulary), not the frontend file itself.
        documented_styles = [
            "photorealistic", "cinematic_realism", "dark_realistic", "vintage_film",
            "digital_art", "cinematic_cartoon", "oil_painting", "watercolor", "anime",
        ]
        for style in documented_styles:
            with self.subTest(style=style):
                self.assertIn(style, agent4_system_prompt._IMAGE_STYLE_NEGATIVE_CONSTRAINTS)

    def test_unrecognized_image_style_gets_no_constraint_line_fail_open(self):
        message = self._capture_message(image_style="some_future_custom_style")
        self.assertNotIn("Style constraints", message)
        # But the direction lines themselves still appear (fail-open, not fail-closed).
        self.assertIn("Global image style: some_future_custom_style", message)

    def test_no_image_style_supplied_adds_no_constraint_line(self):
        message = self._capture_message()
        self.assertNotIn("Style constraints", message)


# ── C4: beat-count and hint-length prompt tightening ─────────────────────────

class BeatCountAndHintLengthPromptTighteningTest(unittest.TestCase):
    def test_strict_rules_state_beat_count_is_a_requirement(self):
        prompt = agent4_system_prompt._STORYBOARD_SYSTEM_PROMPT
        self.assertIn("REQUIREMENT, not a suggestion", prompt)
        self.assertIn("stop early or under-deliver", prompt)

    def test_hint_length_rule_states_the_floor_rationale(self):
        prompt = agent4_system_prompt._STORYBOARD_SYSTEM_PROMPT
        self.assertIn("never stop at 2-3 words", prompt)
        self.assertIn("frequently ambiguous", prompt)

    def test_count_line_in_real_message_states_requirement_not_suggestion(self):
        captured = {}

        def fake_structured(**kwargs):
            captured["user_message"] = kwargs["user_message"]
            return {"beats": []}, {"input_tokens": 10, "output_tokens": 10}

        with patch.object(agent4_system_prompt, "call_claude_structured_with_usage", side_effect=fake_structured):
            agent4_system_prompt.generate_storyboard_batch(
                segment_label="[INTRO]", segment_text="Belisarius marched north.",
                segment_index=1, segment_count=1, channel=_Channel(),
                target_beat_count=18,
            )

        self.assertIn("18 beats — a requirement, not a suggestion", captured["user_message"])


if __name__ == "__main__":
    unittest.main()
