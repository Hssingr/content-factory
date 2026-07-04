"""Runtime proof for roadmap 5.3 Unicode-safe deterministic tokenizers.

No external APIs are called. The absent paid ElevenLabs SDK import is stubbed;
real script_checks, overlap detection, and TTS preparation logic run.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace


class _VoiceSettingsStub:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=_VoiceSettingsStub))

from app.agents.agent2_discovery.services import scripts
from app.agents.agent3_audio.services import tts
from app.services.script_checks import (
    check_hook_quality,
    check_retention_structure,
)


class UnicodeTokenizerTest(unittest.TestCase):
    def test_overlap_tokens_preserve_accents_and_casefold(self) -> None:
        tokens = scripts._normalize_overlap_tokens("Él volvió al café después del año nuevo.")

        self.assertEqual(tokens, ["él", "volvió", "al", "café", "después", "del", "año", "nuevo"])

    def test_parent_child_overlap_detects_accented_verbatim_run(self) -> None:
        parent = "Él volvió al café después del año nuevo con pruebas nuevas."
        child = "Él volvió al café después del año nuevo. Luego contó otra parte."

        result = scripts.detect_parent_child_overlap(child, parent, part_n=1, correction_round=0)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreater(result["overlap_ratio"], 0.15)
        self.assertEqual(result["issues"][0]["category"], "parent_child_overlap")
        self.assertIn("él volvió al café después del año nuevo", result["excerpts"][0])

    def test_hook_count_is_unicode_but_english_openers_are_source_scoped(self) -> None:
        spanish_intro = "[INTRO]\nIn agosto María volvió al café."  # Spanish/Italian-style "In" must not trip English opener.
        english_intro = "[INTRO]\nIn this story, Maria returned to the cafe."

        self.assertEqual(check_hook_quality(spanish_intro, "es"), [])
        english_issues = check_hook_quality(english_intro, "en")
        self.assertEqual(english_issues[0]["category"], "hook_quality")
        self.assertIn("forbidden opener", english_issues[0]["description"])

    def test_summary_starter_retention_check_is_english_scoped(self) -> None:
        script = "[INTRO]\nStart.\n\n[SECTION 1]\nA clue lands. In the end, nothing is resolved.\n\n[SECTION 2]\nA later clue appears.\n\n[OUTRO]\nDone."

        self.assertEqual(check_retention_structure(script, "es", "youtube_long"), [])
        english_issues = check_retention_structure(script, "en", "youtube_long")
        self.assertEqual(english_issues[0]["category"], "retention_structure")

    def test_tts_slow_open_detects_unicode_letter_before_period(self) -> None:
        prepared = tts.prepare_script_for_tts("Él llegó. The room went silent.", "es", "dramatic")

        self.assertIn("Él llegó... The room", prepared)


if __name__ == "__main__":
    unittest.main()
