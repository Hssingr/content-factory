"""Phase 5 P3-1..3 proof: hold-cap logging, Short title telemetry, CTA prompt diversity."""

from __future__ import annotations

import inspect
import logging
import unittest

from app.agents.agent2_discovery import system_prompt
from app.agents.agent2_discovery.services import scripts
from app.agents.agent4_visuals.services import visual_orchestrator
from app.agents.agent4_visuals.subagents import storyboard


class Phase5P3ShortTelemetryTest(unittest.TestCase):
    def test_static_contracts_are_present(self):
        split_sig = inspect.signature(storyboard.split_into_beats)
        self.assertIn("content_id", split_sig.parameters)
        split_source = inspect.getsource(storyboard.split_into_beats)
        self.assertIn("content_id=content_id", split_source)
        orchestrator_source = inspect.getsource(visual_orchestrator._run_visual_pass)
        self.assertIn("content_id=str(content_id)", orchestrator_source)

        hold_cap_source = inspect.getsource(storyboard._apply_visual_hold_cap)
        self.assertIn("%s_APPLIED content_id=%s", hold_cap_source)
        self.assertIn("content_id or \"unknown\"", hold_cap_source)
        self.assertIn("SHORT_TITLE_SPOILER_TELEMETRY", inspect.getsource(scripts._collect_short_title_spoiler_issue))
        self.assertIn("must not copy blueprint.comment_trigger", system_prompt._SHORT_EPISODE_SYSTEM_PROMPT)
        self.assertIn("must not copy blueprint.comment_trigger", system_prompt._SHORTS_PLANNER_SYSTEM_PROMPT)
        self.assertIn("story-specific", system_prompt._STORY_BLUEPRINT_SYSTEM_PROMPT)
        self.assertIn("unique to this story", system_prompt._SECTION_GENERATION_SYSTEM_PROMPT)

    def test_parent_hold_cap_log_includes_content_id(self):
        sections = [
            {"audio_start_ms": 0, "audio_end_ms": 12_000, "duration_sec": 12.0},
            {"audio_start_ms": 12_000, "audio_end_ms": 18_000, "duration_sec": 6.0},
        ]
        with self.assertLogs(storyboard.logger.name, level=logging.INFO) as logs:
            capped = storyboard._apply_visual_hold_cap(
                sections,
                max_hold_ms=9_000,
                content_id="content-123",
                language="en",
                log_prefix="PARENT_VISUAL_HOLD_CAP",
            )

        self.assertEqual(capped[0]["audio_end_ms"], 9_000)
        self.assertEqual(capped[1]["audio_start_ms"], 9_000)
        self.assertTrue(
            any("PARENT_VISUAL_HOLD_CAP_APPLIED content_id=content-123" in line for line in logs.output),
            logs.output,
        )

    def test_short_title_spoiler_telemetry_is_log_only(self):
        calls = []
        generated = {
            "title": "They Found Whitney Alive",
            "voice_script": " ".join(["Whitney looks toward the mine."] + [f"detail{i}" for i in range(200)]),
        }

        def fake_generate_short_episode_script(**kwargs):
            calls.append(kwargs)
            return dict(generated)

        original = scripts.generate_short_episode_script
        scripts.generate_short_episode_script = fake_generate_short_episode_script
        try:
            with self.assertLogs(scripts.logger.name, level=logging.WARNING) as logs:
                result = scripts._generate_short_script(
                    part_plan={
                        "part": 4,
                        "_total_parts": 4,
                        "opening_hook": "Whitney looks toward the mine.",
                        "main_reveal": "They found Whitney alive inside the mountain.",
                        "cliffhanger": "Would you tell anyone Whitney was alive?",
                    },
                    part_n=4,
                    voice_script="The parent script uses different words for the wider story.",
                    blueprint={
                        "final_payoff": "Whitney is found alive inside the mountain.",
                        "comment_trigger": "Would you go back into that mountain knowing what waited inside?",
                    },
                    channel=type("Channel", (), {"niche": "horror", "tone": "dread"})(),
                    channel_voice=None,
                    source_language="en",
                )
        finally:
            scripts.generate_short_episode_script = original

        self.assertEqual(len(calls), 1)
        self.assertEqual(result, generated)
        self.assertTrue(
            any("SHORT_TITLE_SPOILER_TELEMETRY part=4" in line for line in logs.output),
            logs.output,
        )
        self.assertTrue(any("action=log_only" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
