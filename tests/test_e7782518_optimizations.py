"""Offline regression coverage for the e7782518 quality optimizations."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent2_discovery import system_prompt
from app.agents.agent2_discovery.services import scripts, story_generator
from app.agents.agent3_audio.services import tts


class PromptAndBudgetTest(unittest.TestCase):
    def test_ai_premise_is_built_for_gate_strength_without_becoming_a_teaser(self):
        captured = {}
        channel = SimpleNamespace(
            id="channel", niche="history", tone="dramatic",
            description="Historical biography storytelling",
        )
        with patch.object(
            story_generator,
            "call_claude_structured",
            side_effect=lambda **kwargs: captured.update(kwargs) or {
                "title": "A direct title", "body": "A direct premise.",
            },
        ):
            story_generator.generate_story_premise(channel, "en")
        prompt = captured["system_prompt"]
        self.assertIn("performance-ready STORY", prompt)
        self.assertIn("concrete high-stakes or counterintuitive moment", prompt)
        self.assertIn("personal consequence for a named person", prompt)
        self.assertIn("visually distinct settings/actions", prompt)
        self.assertIn("write the premise itself as a viewer hook or vague teaser", prompt)

    def test_ai_gate_scores_hook_source_moment_not_plain_pitch_rhetoric(self):
        captured = {}
        scores = {dimension: 70 for dimension in system_prompt._SCORING_DIMENSIONS}
        story = SimpleNamespace(
            source_type="ai_generated", title="Ada's Notes", url="discovery://story",
            body="Ada publishes notes that describe a machine no one has built.",
        )
        channel = SimpleNamespace(niche="history", tone="dramatic")
        with patch.object(
            system_prompt,
            "call_claude_structured",
            side_effect=lambda **kwargs: captured.update(kwargs) or {"scores": scores},
        ):
            system_prompt.score_story_for_gate(story, channel)
        message = captured["user_message"]
        self.assertIn("operator-facing plain-language pitch", message)
        self.assertIn("script could open on", message)
        self.assertIn("do not invent an unmentioned moment", message)

    def test_native_adaptation_receives_gender_and_agreement_rule(self):
        captured = {}
        with patch.object(
            system_prompt,
            "call_claude_structured",
            side_effect=lambda **kwargs: captured.update(kwargs) or {"voice_script": "ok"},
        ):
            system_prompt.generate_native_script(
                "I was accused.", "fr", "history", "dramatic",
                protagonist_gender="feminine",
            )
        self.assertIn(
            "Narrator gender (for grammatical agreement): feminine",
            captured["user_message"],
        )
        self.assertIn("Past participles and adjectives", captured["system_prompt"])
        self.assertIn("natural spoken French", captured["system_prompt"])

    def test_section_call_receives_explicit_target_only_budget(self):
        captured = {}
        story = SimpleNamespace(body="Grounded source material.")
        channel = SimpleNamespace(niche="history", tone="dramatic")
        with patch.object(
            system_prompt,
            "call_claude_structured",
            side_effect=lambda **kwargs: captured.update(kwargs) or {
                "script_text": "A section.",
                "summary": "Summary.",
                "reveals": [],
                "open_questions": [],
                "suggests_outro": False,
                "visual_intent": {
                    "section_goal": "advance",
                    "primary_visual_focus": "subject",
                    "avoid_repeating": [],
                },
            },
        ):
            system_prompt.generate_section(
                "SECTION 1", story, {}, [], {"avoid_repeating": []}, channel,
                total_word_target=1550, planned_section_count=5,
            )
        self.assertIn("target 1550 words across 5 planned sections", captured["user_message"])
        self.assertIn("target about 310 words for this section", captured["user_message"])
        self.assertIn("writing target only", captured["user_message"])

    def test_long_form_source_is_richer_than_script_target(self):
        self.assertEqual(story_generator._EXPANSION_WORD_TARGETS["youtube_long"], 2200)
        context = scripts._build_section_generation_context(
            None, {"major_turns": [], "suggested_section_count": 4},
            script_format="youtube_long",
        )
        self.assertEqual(context["total_word_target"], 1550)
        self.assertEqual(context["planned_section_count"], 6)

    def test_short_writer_sees_sibling_reveals_as_forbidden(self):
        captured = {}
        part = {
            "part": 2,
            "_total_parts": 4,
            "main_reveal": "the trial turns",
            "other_parts_main_reveals": ["the Varennes recognition", "the prison escape"],
        }
        with patch.object(
            system_prompt,
            "call_claude_structured",
            side_effect=lambda **kwargs: captured.update(kwargs) or {
                "title": "Part 2", "voice_script": "Narration.",
            },
        ):
            system_prompt.generate_short_episode_script(
                part, "Long source.", {}, SimpleNamespace(niche="history", tone="dramatic"),
                None,
            )
        self.assertIn("other_parts_main_reveals", captured["user_message"])
        self.assertIn("forbidden", captured["system_prompt"])
        self.assertIn("sibling territory", captured["system_prompt"])
        self.assertIn("do not continue into the next part", captured["system_prompt"])


class OfflineAudioPolishTest(unittest.TestCase):
    def test_loudness_arc_has_conservative_role_offsets_and_runs_offline(self):
        self.assertEqual(
            tts._SECTION_LOUDNESS_GAIN_DB,
            {
                "intro": -1.0,
                "early_buildup": 0.0,
                "late_buildup": 1.0,
                "climax_title": 1.5,
                "outro": -1.5,
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp3"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2",
                    "-c:a", "libmp3lame", "-b:a", "192k", str(source),
                ],
                check=True,
                capture_output=True,
            )
            original = source.read_bytes()
        result = tts._apply_section_loudness_arc(
            original,
            [{"start_ms": 0, "end_ms": 2000}],
            [{"delivery_reason": "climax_title"}],
        )
        self.assertTrue(result)
        self.assertNotEqual(result, original)
        self.assertAlmostEqual(
            tts._measure_mp3_bytes_duration_ms(result), 2000, delta=100,
        )


if __name__ == "__main__":
    unittest.main()
