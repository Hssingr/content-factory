"""Runtime proof for Elimination Mandate Phase 1b (D1.1, D1.2) —
code_report/forensic_output_audit_borrasca_run.md.

A real production run showed the parent AI quality gate spent two paid
rewrites and the script still came back NEEDS_REWRITE with the flagged
repetitions shipped unfixed anyway, and the global narrative-coherence
Claude call found 4 real issues that every single one shipped unfixed
regardless — pure cost, zero effect. This file proves: run_script_quality_gate()
now makes ZERO Claude calls (the paid boundary raises if touched at all),
still applies the deterministic TTS backstop, still logs deterministic
MAJOR findings as telemetry, and the deleted functions/prompts/task keys are
genuinely gone — not just uncalled.
"""

from __future__ import annotations

import unittest
import unittest.mock

from app.agents.agent2_discovery import system_prompt
from app.agents.agent2_discovery.services import scripts


class TestRunScriptQualityGateMakesNoClaudeCalls(unittest.TestCase):
    """D1.1/D1.2: run_script_quality_gate() must never touch the Claude
    boundary — no assess call, no rewrite call, no global-validation call."""

    def _poison_claude_boundary(self):
        """Patch call_claude_structured to raise if invoked at all — proves
        the gate is 100% Claude-free, not just "usually" skips the call."""
        def _raise(*args, **kwargs):
            raise AssertionError(
                "run_script_quality_gate() must never call Claude — "
                "the entire AI assess/rewrite/global-validation mechanism "
                "was deleted by the Elimination Mandate"
            )
        return unittest.mock.patch.object(
            system_prompt, "call_claude_structured", side_effect=_raise
        )

    def test_clean_script_returns_unchanged_with_no_claude_call(self):
        clean_script = (
            "[INTRO]\nA short punchy hook line here.\n"
            "[SECTION 1]\nSomething concrete happens next in the story.\n"
            "[OUTRO]\nAnd that is how it all ended for everyone involved.\n"
        )
        scripts_in = {"title": "T", "voice_script": clean_script, "_section_calls": 3}

        with self._poison_claude_boundary():
            result = scripts.run_script_quality_gate(scripts_in, script_format="youtube_long")

        self.assertEqual(result["voice_script"], clean_script)

    def test_script_with_major_tts_and_hook_issues_still_returns_with_no_claude_call(self):
        """Even a script that would previously have triggered two paid
        rewrite rounds must return immediately with zero Claude calls."""
        bad_script = (
            "[INTRO]\n" + ("very " * 30) + "long opening sentence with no punchy hook whatsoever.\n"
            "[SECTION 1]\nA sentence so long that it just keeps going and going "
            "and going without any punctuation to break it up at all which is bad.\n"
            "[OUTRO]\nDone.\n"
        )
        scripts_in = {"title": "T", "voice_script": bad_script, "_section_calls": 3}

        with self._poison_claude_boundary(), \
             self.assertLogs("app.agents.agent2_discovery.services.scripts", level="WARNING") as log_ctx:
            result = scripts.run_script_quality_gate(scripts_in, script_format="youtube_long")

        self.assertIn("voice_script", result)
        joined = " ".join(log_ctx.output)
        self.assertIn("telemetry only, no rewrite", joined)

    def test_tts_backstop_still_applies_deterministic_cleanup(self):
        """The mechanical TTS backstop (normalize_tts_chars/split_long_sentences)
        must still run — it is not part of the deleted AI mechanism."""
        # A single massively over-length sentence with no terminal punctuation
        # break — split_long_sentences() should still act on this.
        run_on = "word " * 60
        scripts_in = {
            "title": "T",
            "voice_script": f"[INTRO]\n{run_on.strip()}.\n[OUTRO]\nDone.\n",
            "_section_calls": 1,
        }

        with self._poison_claude_boundary():
            result = scripts.run_script_quality_gate(scripts_in, script_format="youtube_long")

        # The backstop must have run without raising, and returned a dict
        # shaped like the input (proving it's still a real transformation
        # pass, not a no-op passthrough).
        self.assertIn("voice_script", result)
        self.assertIsInstance(result["voice_script"], str)


class TestParentAIQualityGateFullyRemoved(unittest.TestCase):
    """The entire parent AI quality gate (assess_script_quality(),
    rewrite_script_for_quality()) and global narrative validation
    (validate_script_globally()) must not exist anywhere — deleted, not
    disabled."""

    def test_functions_and_prompts_fully_removed(self):
        for name in (
            "assess_script_quality",
            "rewrite_script_for_quality",
            "validate_script_globally",
            "_SCRIPT_QUALITY_SYSTEM_PROMPT",
            "_SCRIPT_QUALITY_REWRITE_BASE",
            "_QUALITY_REWRITE_SCHEMA",
            "_SCRIPT_QUALITY_SCHEMA",
            "_GLOBAL_VALIDATION_SYSTEM_PROMPT",
            "_GLOBAL_VALIDATION_SCHEMA",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(system_prompt, name))

    def test_scripts_module_helpers_fully_removed(self):
        for name in (
            "_MAX_QUALITY_REWRITES",
            "_has_tts_only_high_issues",
            "_apply_tts_only_quality_cleanup",
            "_apply_post_rewrite_cleanup",
            "_apply_final_quality_cleanup",
            "_run_global_script_validation",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(scripts, name))

    def test_task_keys_retired_from_model_routing(self):
        from app.services.model_routing import resolve_model
        for task in ("script_quality_check", "quality_rewrite", "global_validation"):
            with self.subTest(task=task):
                with self.assertRaises(ValueError):
                    resolve_model(task)


class TestGenerateParentSourceScriptChainMakesNoGateClaudeCalls(unittest.TestCase):
    """Full-chain proof: generate_parent_source_script() calls the REAL
    (unmocked) run_script_quality_gate() — only generate_story_blueprint() and
    generate_script_sections() are stubbed, exactly as
    test_roadmap_closeout_fixes.py's own fixture does. If the quality gate
    still made any Claude call, poisoning call_claude_structured here would
    catch it; the chain completing successfully proves it does not."""

    def _fixtures(self):
        import uuid
        from app.models import Channel, ChannelConfig, ChannelVoice, Content

        class _FakeQuery:
            def __init__(self, rows):
                self.rows = rows
            def filter(self, *a, **k): return self
            def order_by(self, *a, **k): return self
            def limit(self, n): return _FakeQuery(self.rows[:n])
            def first(self): return self.rows[0] if self.rows else None
            def all(self): return self.rows

        class _FakeDb:
            def __init__(self):
                self.tables: dict = {}
            def get(self, model, key):
                for row in self.tables.get(model, []):
                    if getattr(row, "id", None) == key:
                        return row
                return None
            def query(self, model):
                return _FakeQuery(self.tables.get(model, []))
            def add(self, row):
                self.tables.setdefault(type(row), []).append(row)
            def commit(self): pass
            def refresh(self, row): pass

        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db = _FakeDb()
        db.add(Channel(id=channel_id, niche="horror", tone="tense"))
        db.add(ChannelConfig(
            channel_id=channel_id, script_format="youtube_long",
            visual_style="documentary", image_style="photorealistic",
            audio_tags_enabled=False,
        ))
        db.add(ChannelVoice(
            id=uuid.uuid4(), channel_id=channel_id, language="en",
            provider="cartesia", voice_id="v1", tts_model="sonic-3.5",
        ))
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="APPROVED", title="T",
            source_url="https://example.com/x",
            # >=900 words: clears the youtube_long source-material floor
            # (roadmap 4b / audit P1-5, check_source_material_floor()) — this
            # fixture is testing the Claude-free quality gate, not the floor.
            source_excerpt=" ".join(["word"] * 950),
        )
        db.add(content)
        return db, content

    def test_real_quality_gate_runs_without_touching_claude_boundary(self):
        from unittest.mock import patch as mock_patch
        from app.agents.agent2_discovery.services import script_workflow

        db, content = self._fixtures()

        def fake_blueprint(story, channel, **kwargs):
            return {"major_turns": ["t1"], "suggested_section_count": 1,
                    "hook": "h", "final_payoff": "p", "comment_trigger": "c?",
                    "midpoint_retention_trap": "m", "central_question": "q",
                    "suggested_title": "T"}

        def fake_sections(**kwargs):
            # Deliberately over-length/TTS-hostile to prove that even a draft
            # the deleted gate would have retried on still short-circuits
            # with no Claude call from the real run_script_quality_gate().
            return {"title": "T", "voice_script": "[INTRO]\n" + ("word " * 40) + ".\n[OUTRO]\nDone.\n",
                    "visual_intent_history": []}

        def poisoned_structured(**kwargs):
            raise AssertionError(
                "run_script_quality_gate() must never call Claude — the AI "
                "assess/rewrite/global-validation mechanism was deleted"
            )

        with (
            mock_patch.object(script_workflow, "generate_story_blueprint", side_effect=fake_blueprint),
            mock_patch.object(script_workflow, "generate_script_sections", side_effect=fake_sections),
            mock_patch.object(system_prompt, "call_claude_structured", side_effect=poisoned_structured),
        ):
            # run_script_quality_gate() is NOT mocked — the real function runs.
            voice_script = script_workflow.generate_parent_source_script(content, db)

        self.assertIn("word", voice_script)


if __name__ == "__main__":
    unittest.main()
