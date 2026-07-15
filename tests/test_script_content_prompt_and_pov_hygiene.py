"""Offline runtime proofs for script-content prompts and POV hygiene.

All model boundaries are stubbed. No external API is called.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent2_discovery import system_prompt
from app.agents.agent2_discovery.services import script_workflow, story_generator
from app.agents.agent2_discovery.services.narration_pov import (
    VALID_NARRATION_POVS,
    normalize_narration_pov,
)
from app.models import Channel, ChannelConfig


def _channel() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        niche="ancient history",
        tone="tense",
        description="Accurate accounts of ancient military campaigns.",
    )


def _parts() -> list[dict]:
    return [
        {
            "part": number,
            "goal": "Advance the Alpine crossing",
            "opening_hook": "I led Carthage's army into the frozen pass.",
            "main_content_summary": "The army confronts the blocked route.",
            "main_reveal": "Mago identifies the only viable descent.",
            "cliffhanger": "Will Mago's route save the trapped army?",
        }
        for number in range(1, 4)
    ]


class ShortsPromptRuntimeTest(unittest.TestCase):
    def test_planner_receives_first_person_and_content_rules(self) -> None:
        response = {"total_parts": 3, "parts": _parts()}
        with patch.object(
            system_prompt, "call_claude_structured", return_value=response,
        ) as paid_boundary:
            result = system_prompt.generate_shorts_plan(
                "Complete grounded narration.", {"final_payoff": "The army survives."},
                _channel(), narration_pov="first_person_storytime",
            )

        self.assertEqual(result, response)
        kwargs = paid_boundary.call_args.kwargs
        prompt = " ".join(kwargs["system_prompt"].split())
        self.assertIn("Narration POV: first_person_storytime", kwargs["user_message"])
        self.assertIn("satisfy the cold-open identity requirement IN", prompt)
        self.assertIn("Never add a third-person naming sentence", prompt)
        self.assertIn("concrete unresolved threat, person, event, place, or object", prompt)
        self.assertIn("narrator's or character's rationalization", prompt)
        self.assertIn("140–170 words", prompt)

    def test_short_writer_receives_first_person_and_content_rules(self) -> None:
        response = {"title": "The Frozen Route", "voice_script": "I led the army onward."}
        with patch.object(
            system_prompt, "call_claude_structured", return_value=response,
        ) as paid_boundary:
            result = system_prompt.generate_short_episode_script(
                part_plan={**_parts()[0], "_total_parts": 3},
                long_voice_script="Complete grounded narration.",
                blueprint={"final_payoff": "The army survives."},
                channel=_channel(),
                channel_voice=SimpleNamespace(tts_model="sonic-2", provider="cartesia"),
                narration_pov="first_person_storytime",
            )

        self.assertEqual(result, response)
        kwargs = paid_boundary.call_args.kwargs
        prompt = " ".join(kwargs["system_prompt"].split())
        self.assertIn("Narration POV: first_person_storytime", kwargs["user_message"])
        self.assertIn("Never insert a third-person naming sentence", prompt)
        self.assertIn("concrete unresolved threat, person, event, place, or object", prompt)
        self.assertIn("character's rationalization", prompt)
        self.assertIn("Target 140–170 words", prompt)
        self.assertGreaterEqual(tuple(map(int, system_prompt.PROMPT_VERSION.split("."))), (5, 3))


class HistoricalAccuracyPromptRuntimeTest(unittest.TestCase):
    def test_accuracy_rules_reach_all_three_real_story_calls(self) -> None:
        captured: list[dict] = []

        def fake_boundary(**kwargs):
            captured.append(kwargs)
            if kwargs["schema_name"] == "story_expansion":
                return {"body": "A grounded historical narrative."}
            return {"title": "Hannibal Crosses the Alps", "body": "A grounded premise."}

        channel = _channel()
        with patch.object(story_generator, "call_claude_structured", side_effect=fake_boundary):
            story_generator.generate_story_premise(channel, "en")
            story_generator.expand_story_premise(
                "Hannibal crosses the Alps.", channel, "youtube_long", "en",
            )
            story_generator.revise_story_premise(
                channel, "en", [
                    {"role": "assistant", "title": "Hannibal", "body": "A premise."},
                    {"role": "operator", "feedback": "Include Mago accurately."},
                ],
            )

        self.assertEqual(len(captured), 3)
        for call in captured:
            with self.subTest(schema=call["schema_name"]):
                prompt = " ".join(call["system_prompt"].split())
                self.assertIn("Historical accuracy", prompt)
                self.assertIn("geography, distance, chronology, named people, roles, and outcomes", prompt)
                self.assertIn("later chroniclers claimed", prompt)
                self.assertIn("Introduce every named person by role or relationship", prompt)

    def test_expansion_target_is_ceiling_guidance_without_trim_or_retry(self) -> None:
        generated = " ".join(["word"] * 1205)
        with patch.object(
            story_generator, "call_claude_structured", return_value={"body": generated},
        ) as paid_boundary:
            result = story_generator.expand_story_premise(
                "Approved premise.", _channel(), "youtube_long", "en",
            )

        self.assertEqual(result, generated)
        paid_boundary.assert_called_once()
        self.assertIn(
            "Target length: 1200 words", paid_boundary.call_args.kwargs["user_message"],
        )
        self.assertIn("do not exceed it", paid_boundary.call_args.kwargs["user_message"])


class NarrationPovNormalizationTest(unittest.TestCase):
    def test_canonical_and_missing_values_pass_without_warning(self) -> None:
        self.assertEqual(normalize_narration_pov("third_person"), "third_person")
        self.assertEqual(
            normalize_narration_pov("first_person_storytime"),
            "first_person_storytime",
        )
        self.assertEqual(normalize_narration_pov(None), "third_person")

    def test_legacy_and_nearby_unknown_values_warn_and_choose_nearest(self) -> None:
        with self.assertLogs(
            "app.agents.agent2_discovery.services.narration_pov", level="WARNING",
        ) as logs:
            legacy = normalize_narration_pov("first_person", channel_id="hannibal")
            nearby = normalize_narration_pov("thirdperson", channel_id="other")
            misspelled = normalize_narration_pov("first_personn_storytime", channel_id="other")

        self.assertEqual(legacy, "first_person_storytime")
        self.assertEqual(nearby, "third_person")
        self.assertEqual(misspelled, "first_person_storytime")
        self.assertTrue(all(value in VALID_NARRATION_POVS for value in (legacy, nearby, misspelled)))
        self.assertTrue(any("NARRATION_POV_NORMALIZED" in line for line in logs.output))
        self.assertTrue(any("channel_id=hannibal" in line for line in logs.output))

    def test_workflow_context_normalizes_legacy_database_value(self) -> None:
        channel = Channel(id=uuid.uuid4(), niche="history", tone="tense")
        config = ChannelConfig(
            channel_id=channel.id, script_format="youtube_long", narration_pov="first_person",
        )

        class Query:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return None

        class Db:
            def get(self, model, key):
                return channel if model is Channel else config if model is ChannelConfig else None

            def query(self, model):
                self.last_model = model
                return Query()

        content = SimpleNamespace(
            id=uuid.uuid4(), channel_id=channel.id, source_language="en",
        )
        with self.assertLogs(
            "app.agents.agent2_discovery.services.narration_pov", level="WARNING",
        ):
            context = script_workflow._load_script_workflow_context(content, Db())

        self.assertEqual(context.narration_pov, "first_person_storytime")


class NarrationPovMigrationTest(unittest.TestCase):
    def test_migration_repairs_legacy_row_token(self) -> None:
        path = Path(__file__).resolve().parents[1] / "alembic/versions/014_normalize_legacy_narration_pov.py"
        spec = importlib.util.spec_from_file_location("migration_014", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        self.assertEqual(module.revision, "014")
        self.assertEqual(module.down_revision, "013")
        with patch.object(module.op, "execute") as execute:
            module.upgrade()

        sql = execute.call_args.args[0]
        self.assertIn("SET narration_pov = 'first_person_storytime'", sql)
        self.assertIn("lower(trim(narration_pov)) = 'first_person'", sql)


if __name__ == "__main__":
    unittest.main()
