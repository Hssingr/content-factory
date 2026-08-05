"""Phase A3/A4 prompt and telemetry contracts; no API calls."""

import logging

from app.agents.agent2_discovery import system_prompt as agent2_prompt
from app.agents.agent2_discovery.services import scripts
from app.agents.agent4_visuals import system_prompt as agent4_prompt


def test_phase_a_prompt_pack_is_present():
    assert tuple(map(int, agent2_prompt.PROMPT_VERSION.split("."))) >= (5, 7)
    assert tuple(map(int, agent4_prompt.PROMPT_VERSION.split("."))) >= (5, 2)
    assert "HARD CEILING: voice_script must never exceed 270 words" in agent2_prompt._SOLO_SHORT_SYSTEM_PROMPT
    assert "historians disagree" in agent2_prompt._SOLO_SHORT_SYSTEM_PROMPT
    assert "first approximately 6 seconds with 2–3" in agent4_prompt._STORYBOARD_SYSTEM_PROMPT
    assert "Never make a text-bearing object" in agent4_prompt._STORYBOARD_SYSTEM_PROMPT
    assert "When narration describes an action or consequence" in agent4_prompt._STORYBOARD_SYSTEM_PROMPT


def test_solo_short_log_prefix_and_current_wpm_are_not_stale(caplog):
    too_long = " ".join(["word"] * (scripts._MAX_SHORT_WORDS + 1))
    with caplog.at_level(logging.WARNING):
        issues = scripts._collect_short_script_major_issues(
            too_long, "en", part_n=1, correction_round=1,
            caller_label="solo_short",
        )
    assert any(issue["category"] == "script_too_long" for issue in issues)
    assert "solo_short: part 1 attempt 1 word count" in caplog.text
    assert "~175 wpm Short rate" in next(
        issue["description"] for issue in issues
        if issue["category"] == "script_too_long"
    )
    assert "~120 wpm" not in caplog.text

