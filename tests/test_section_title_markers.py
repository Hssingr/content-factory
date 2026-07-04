"""Runtime proof for roadmap 4.6 titled section markers.

No external APIs are called. These tests exercise the real script assembly
helpers and the real deterministic script completeness regexes.
"""

from __future__ import annotations

import unittest

from app.agents.agent2_discovery.services import scripts
from app.services.script_checks import _SECTION_NUM_RE, check_completeness


class SectionTitleMarkersTest(unittest.TestCase):
    def test_assemble_script_emits_body_section_titles_only(self) -> None:
        voice_script = scripts.assemble_script([
            {"label": "INTRO", "script_text": "A family hears a sound."},
            {
                "label": "SECTION 1",
                "title": "The locked basement door finally opens during the storm",
                "script_text": "Mara reaches the basement door. It opens by itself.",
            },
            {
                "label": "SECTION 2",
                "title": "The receipt reveals who paid for the room",
                "script_text": "The receipt names a person nobody expected.",
            },
            {"label": "OUTRO", "script_text": "Then the last clue changes everything."},
        ])

        self.assertIn("[INTRO]", voice_script)
        self.assertIn("[SECTION 1: The locked basement door finally opens during]", voice_script)
        self.assertIn("[SECTION 2: The receipt reveals who paid for the]", voice_script)
        self.assertIn("[OUTRO]", voice_script)


    def test_append_generated_section_preserves_title_for_assembly(self) -> None:
        state = scripts._create_section_loop_state()
        section = {
            "script_text": "Mara reaches the basement door. It opens by itself.",
            "title": scripts._section_title_from_text("The basement door opens during the storm"),
            "reveals": [],
            "summary": "Mara reaches the basement door.",
            "open_questions": [],
        }

        matched = scripts._append_generated_section(
            state,
            "SECTION 1",
            section,
            major_turns=["The basement door opens during the storm"],
        )
        voice_script = scripts.assemble_script(state["sections"])

        self.assertEqual(matched, {0})
        self.assertEqual(state["sections"][0]["title"], "The basement door opens during the storm")
        self.assertIn("[SECTION 1: The basement door opens during the storm]", voice_script)

    def test_titled_markers_still_pass_existing_completeness_regexes(self) -> None:
        voice_script = scripts.assemble_script([
            {"label": "INTRO", "script_text": "A family hears a sound."},
            {
                "label": "SECTION 1",
                "title": "The storm clue",
                "script_text": "Mara reaches the basement door. It opens by itself.",
            },
            {
                "label": "SECTION 2",
                "title": "The final receipt",
                "script_text": "The receipt names a person nobody expected.",
            },
            {"label": "OUTRO", "script_text": "Then the last clue changes everything."},
        ])

        self.assertEqual([int(n) for n in _SECTION_NUM_RE.findall(voice_script)], [1, 2])
        self.assertEqual(check_completeness(voice_script, "en"), [])

    def test_title_sanitizer_removes_brackets_and_limits_words(self) -> None:
        title = scripts._section_title_from_text(
            "[REVEAL] The hidden camera footage exposes the impossible locked-room alibi",
            max_words=7,
        )

        self.assertEqual(title, "REVEAL The hidden camera footage exposes the")
        self.assertNotIn("[", title)
        self.assertNotIn("]", title)


if __name__ == "__main__":
    unittest.main()
