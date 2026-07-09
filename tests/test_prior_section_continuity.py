"""Runtime proof for threading full prior-section text into section generation.

A real production script shipped internally contradictory: blueprint turn[2]
framed the father as "his own daughter's killer" (murder), while a later
section said the daughter "was alive when firefighters broke through the
hatch" (imprisonment, not murder). Root cause: _append_generated_section()
only ever carried {label, summary, reveals, open_questions} forward into
state["prior_sections_summary"] for later generate_section() calls — the
literal script_text of a prior section (where the "killer" claim actually
appeared) was stored in state["sections"] for final assembly only and never
resent to Claude. Later sections had no way to see the specific words an
earlier section had committed to.

The fix threads a new prior_full_text parameter — assembled from
state["sections"] via the existing assemble_script() — through every
body-section and OUTRO generate_section() call, additive to (not a
replacement for) prior_sections_summary.

Only the paid Claude call (scripts.generate_section) is stubbed — every
other function in the real chain (_generate_intro_section,
_run_body_section_loop, _generate_outro_section, _generate_section_once,
_append_generated_section, assemble_script) is the real, unmocked
implementation. This proves the plumbing is correct; it cannot prove real
Claude output improves (no live API calls from this environment) — that is
verified by the operator's own test_full_pipeline.py --confirm run.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery.services import scripts


def _blueprint() -> dict:
    return {
        "hook": "A grinding metal sound never stops echoing from the mountain",
        "central_question": "What is making the sound?",
        # Both turns share the single content-token "turn" (_get_content_tokens
        # only keeps words >3 chars, so "one"/"two" are dropped) — deliberately
        # minimal, mirroring test_hook_by_construction.py's own blueprint. This
        # makes turn coverage collapse to a single shared token, which is fine
        # here since the fake sections below explicitly cover both turns'
        # literal text anyway.
        "major_turns": ["turn one", "turn two"],
        "final_payoff": "payoff",
        "comment_trigger": "What would you have done?",
        "midpoint_retention_trap": "",
        "suggested_section_count": 2,
        "suggested_title": "T",
    }


def _fake_section(script_text: str, reveals=None, suggests_outro: bool = False) -> dict:
    return {
        "script_text": script_text,
        "summary": "summary",
        "reveals": reveals or [],
        "open_questions": [],
        "suggests_outro": suggests_outro,
        "visual_intent": {},
    }


class TestPriorFullTextPropagation(unittest.TestCase):
    """Drives the real INTRO -> body loop -> OUTRO chain with only
    generate_section() stubbed, capturing every call's kwargs by label."""

    def _run_full_chain(self, fake_generate_section):
        blueprint = _blueprint()
        context = scripts._build_section_generation_context(channel_voice=None, blueprint=blueprint)
        state = scripts._create_section_loop_state()
        story = SimpleNamespace(title="T", body="story body")
        channel = SimpleNamespace(niche="horror", tone="tense")

        with patch.object(scripts, "generate_section", side_effect=fake_generate_section):
            scripts._generate_intro_section(story, blueprint, channel, "youtube_long", False, context, state)
            scripts._run_body_section_loop(story, blueprint, channel, "youtube_long", False, context, state)
            scripts._generate_outro_section(story, blueprint, channel, "youtube_long", False, context, state)

        return state

    def _standard_fake(self, calls: dict):
        """Records every call's kwargs by label; returns deterministic stubs
        that make the body loop stop after exactly 2 sections (both blueprint
        turns get covered by SECTION 1's reveals, but the loop still requires
        a minimum of 2 body sections before it's allowed to stop)."""

        def fake_generate_section(**kwargs):
            label = kwargs["label"]
            calls[label] = kwargs
            if label == "INTRO":
                return _fake_section("Someone moved to a quiet town.")
            if label == "SECTION 1":
                return _fake_section(
                    "The father is exposed as his own daughter's killer.",
                    reveals=["turn one revealed", "turn two revealed"],
                )
            if label == "SECTION 2":
                return _fake_section(
                    "The chase continues toward the tree line.",
                    reveals=["turn one revealed", "turn two revealed"],
                    suggests_outro=True,
                )
            if label == "OUTRO":
                return _fake_section("The story closes on the aftermath.")
            raise AssertionError(f"unexpected label: {label}")

        return fake_generate_section

    def test_intro_receives_empty_prior_full_text(self):
        calls: dict = {}
        self._run_full_chain(self._standard_fake(calls))

        self.assertIn("INTRO", calls)
        self.assertEqual(calls["INTRO"]["prior_full_text"], "")

    def test_section_1_receives_only_intro_full_text(self):
        calls: dict = {}
        state = self._run_full_chain(self._standard_fake(calls))

        self.assertIn("SECTION 1", calls)
        prior_text = calls["SECTION 1"]["prior_full_text"]
        self.assertEqual(prior_text, scripts.assemble_script(state["sections"][:1]))
        self.assertIn("Someone moved to a quiet town.", prior_text)
        # SECTION 1's own text must not be visible to itself.
        self.assertNotIn("exposed as his own daughter's killer", prior_text)

    def test_outro_receives_full_concatenated_intro_and_body_text(self):
        calls: dict = {}
        state = self._run_full_chain(self._standard_fake(calls))

        # Exactly 2 body sections were generated (loop stop condition proof).
        self.assertIn("SECTION 1", calls)
        self.assertIn("SECTION 2", calls)
        self.assertNotIn("SECTION 3", calls)

        self.assertIn("OUTRO", calls)
        prior_text = calls["OUTRO"]["prior_full_text"]
        # INTRO + SECTION 1 + SECTION 2, in order — locks the marker-convention
        # reuse decision: a future refactor that diverges the prompt-context
        # format from assemble_script()'s real output format fails this test.
        # state["sections"] now also holds OUTRO itself (appended after the
        # call was made), so compare against everything but the last entry.
        self.assertEqual(prior_text, scripts.assemble_script(state["sections"][:-1]))
        intro_pos = prior_text.index("Someone moved to a quiet town.")
        s1_pos = prior_text.index("exposed as his own daughter's killer")
        s2_pos = prior_text.index("The chase continues toward the tree line.")
        self.assertLess(intro_pos, s1_pos)
        self.assertLess(s1_pos, s2_pos)

    def test_planted_fact_string_propagates_into_later_calls(self):
        """The money test — reproduces the exact production defect shape: a
        fact stated in an early section (the father is a "killer") must be
        visible, verbatim, to every later generate_section() call."""
        calls: dict = {}
        self._run_full_chain(self._standard_fake(calls))

        planted_fact = "exposed as his own daughter's killer"
        self.assertIn(planted_fact, calls["SECTION 2"]["prior_full_text"])
        self.assertIn(planted_fact, calls["OUTRO"]["prior_full_text"])
        # Not visible to calls that happened before the fact was stated.
        self.assertNotIn(planted_fact, calls["INTRO"]["prior_full_text"])
        self.assertNotIn(planted_fact, calls["SECTION 1"]["prior_full_text"])

    def test_prior_sections_summary_mechanism_is_unchanged(self):
        """Regression guard: the existing summary/reveals mechanism passed to
        generate_section() (prior_sections_summary) must be untouched by this
        purely additive change — same shape, same entries, no leaked
        script_text key. (prior_summary_text, used separately inside
        _generate_section_once() for check_section_transition()'s
        recap-detection, is Python-side-only and was never forwarded to
        generate_section() before or after this change — not asserted here.)
        """
        calls: dict = {}
        self._run_full_chain(self._standard_fake(calls))

        for label in ("SECTION 2", "OUTRO"):
            summary_list = calls[label]["prior_sections_summary"]
            self.assertGreater(len(summary_list), 0)
            for entry in summary_list:
                self.assertEqual(
                    set(entry.keys()), {"label", "summary", "reveals", "open_questions"}
                )

        # OUTRO's summary list must reflect both body sections, not just the
        # most recent one — proves prior_sections_summary keeps accumulating
        # independently of the new prior_full_text mechanism.
        outro_labels = [entry["label"] for entry in calls["OUTRO"]["prior_sections_summary"]]
        self.assertEqual(outro_labels, ["INTRO", "SECTION 1", "SECTION 2"])


if __name__ == "__main__":
    unittest.main()
