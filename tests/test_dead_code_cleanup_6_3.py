"""Runtime proof for roadmap 6.3 dead-code cleanup (audit §7 table).

Covers the two behavior-affecting changes (everything else in 6.3 is pure
deletion, verified by import/static checks, not runtime behavior):

  1. check_retention_structure() is now wired into the real quality gate
     (_collect_quality_gate_issues()) as MINOR telemetry only — it must
     never be converted to a HIGH-severity rewrite-triggering issue, and
     must never block the gate.
  2. Storyboard generation failure (split_into_beats() returns None) now
     always fails loud — the removed legacy section-splitter fallback
     branch is gone, so allow_legacy_fallback=True must behave identically
     to allow_legacy_fallback=False at that point (both return None, 0).

Also proves the deleted modules/functions/keys are genuinely gone (not just
unreferenced) via direct import/attribute checks.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent2_discovery.services import scripts
from app.services import script_checks


class TestRunDeterministicChecksDeleted(unittest.TestCase):
    def test_function_no_longer_exists(self):
        self.assertFalse(hasattr(script_checks, "run_deterministic_checks"))


class TestSectionSplitterModuleDeleted(unittest.TestCase):
    def test_section_splitter_module_is_gone(self):
        with self.assertRaises(ModuleNotFoundError):
            import importlib
            importlib.import_module("app.agents.agent4_visuals.subagents.section_splitter")

    def test_enrich_sections_with_visuals_removed_from_system_prompt(self):
        from app.agents.agent4_visuals import system_prompt
        self.assertFalse(hasattr(system_prompt, "enrich_sections_with_visuals"))

    def test_legacy_section_fallback_removed_from_orchestrator(self):
        from app.agents.agent4_visuals.services import visual_orchestrator
        self.assertFalse(hasattr(visual_orchestrator, "_legacy_section_fallback"))
        self.assertFalse(hasattr(visual_orchestrator, "split_into_sections"))


class TestDeadModelRoutingKeysRemoved(unittest.TestCase):
    def test_dead_task_keys_absent(self):
        from app.services.model_routing import MODEL_ROUTING
        for key in ("media_scoring", "visual_reinterpretation", "script_validation", "section_splitting"):
            self.assertNotIn(key, MODEL_ROUTING)

    def test_resolve_model_raises_for_removed_keys(self):
        from app.services.model_routing import resolve_model
        for key in ("media_scoring", "visual_reinterpretation", "script_validation", "section_splitting"):
            with self.assertRaises(ValueError):
                resolve_model(key)


class TestDeadConfigKeysRemoved(unittest.TestCase):
    def test_dead_settings_fields_absent(self):
        from app.config import Settings
        loaded = Settings(_env_file=None)
        for key in (
            "runway_api_key", "brightdata_username", "brightdata_password",
            "generate_required_frame", "localize_media_before_render",
            "agent3_minor_timeout_minutes",
        ):
            self.assertFalse(hasattr(loaded, key))


class TestRetentionStructureWiredAsTelemetryOnly(unittest.TestCase):
    """Runtime proof: the real _collect_quality_gate_issues() (scripts.py)
    calls the real check_retention_structure() (script_checks.py) and
    surfaces its findings only as telemetry — never blocking, never
    converted to a rewrite-triggering HIGH issue."""

    def _script_with_summary_pattern_ending(self) -> str:
        return (
            "[INTRO]\n"
            "A quiet street hides a secret nobody expected.\n"
            "[SECTION 1]\n"
            "The witness saw a light flicker in the window. "
            "So the mystery deepened.\n"
            "[SECTION 2]\n"
            "Detectives found a torn note under the porch.\n"
            "[OUTRO]\n"
            "The truth finally came out and everyone was shocked.\n"
        )

    def test_retention_minor_surfaces_in_retention_det_only(self):
        voice_script = self._script_with_summary_pattern_ending()
        review = {"status": "PASSED", "issues": []}
        current = {"voice_script": voice_script}

        issue_group = scripts._collect_quality_gate_issues(
            review=review, current=current, language="source", script_format="youtube_long",
        )

        retention_minors = [
            i for i in issue_group["retention_det"] if i["severity"] == "MINOR"
        ]
        self.assertTrue(retention_minors, "expected at least one retention_structure MINOR finding")
        self.assertEqual(
            {i["category"] for i in retention_minors}, {"retention_structure"}
        )

        # Telemetry only: never promoted into all_issues (which drives the
        # rewrite loop), and status stays PASSED.
        all_categories = {i.get("category") for i in issue_group["all_issues"]}
        self.assertNotIn("retention_structure", all_categories)
        self.assertEqual(issue_group["status"], "PASSED")
        self.assertEqual(issue_group["converted_det"], [])

    def test_clean_script_has_no_retention_findings(self):
        voice_script = (
            "[INTRO]\nA quiet street hides a secret nobody expected.\n"
            "[SECTION 1]\nThe witness saw a light flicker and called the police immediately.\n"
            "[OUTRO]\nThe truth finally came out and everyone was shocked.\n"
        )
        review = {"status": "PASSED", "issues": []}
        current = {"voice_script": voice_script}

        issue_group = scripts._collect_quality_gate_issues(
            review=review, current=current, language="source", script_format="youtube_long",
        )
        self.assertEqual(issue_group["retention_det"], [])


class TestStoryboardFailureAlwaysFailsLoud(unittest.TestCase):
    """Runtime proof: with the legacy splitter fallback branch removed,
    a storyboard generation failure (split_into_beats returning None)
    returns None regardless of allow_legacy_fallback's value — there is no
    remaining code path that treats True differently from False here."""

    def test_allow_legacy_fallback_true_still_fails_loud_on_storyboard_failure(self):
        from app.agents.agent4_visuals.services import visual_orchestrator as vo

        content_id = "content-1"
        source_script = SimpleNamespace(voice_script="[SECTION 1]\nSome narration.")
        source_audio = SimpleNamespace(
            duration_ms=10_000, whisper_transcript=[{"word": "hi", "start": 0.0, "end": 0.4}],
        )
        channel = SimpleNamespace(niche="horror", tone="tense", id="chan-1")

        with (
            patch.object(vo, "split_into_beats", return_value=None),
            patch.object(vo, "load_visual_bible_for_content", return_value=None),
        ):
            beats, tokens = vo._run_visual_pass(
                content_id=content_id,
                scripts_by_lang={"en": source_script},
                audio_by_lang={"en": source_audio},
                channel=channel,
                script_format="youtube_long",
                allow_legacy_fallback=True,
                db=None,
            )

        self.assertIsNone(beats)
        self.assertEqual(tokens, 0)


if __name__ == "__main__":
    unittest.main()
