"""Static proof for Task 2b (code_report/TODO, 2026-08-05): the storyboard
system prompt carries a channel-agnostic rule against depicting regime
insignia, flags, uniform emblems, or extremist/hate symbols of any era —
period/regime/conflict context must be conveyed through architecture, dress,
objects, and setting only.
"""

from app.agents.agent4_visuals.system_prompt import (
    PROMPT_VERSION,
    _STORYBOARD_SYSTEM_PROMPT,
)


def test_regime_insignia_rule_present():
    assert "regime insignia" in _STORYBOARD_SYSTEM_PROMPT
    assert "extremist" in _STORYBOARD_SYSTEM_PROMPT
    assert "hate symbol" in _STORYBOARD_SYSTEM_PROMPT.replace("hate symbols", "hate symbol")


def test_rule_is_explicitly_channel_and_side_agnostic():
    idx = _STORYBOARD_SYSTEM_PROMPT.index("regime insignia")
    surrounding = _STORYBOARD_SYSTEM_PROMPT[max(0, idx - 400):idx + 400]
    assert "any era" in surrounding
    assert "any side" in surrounding or "which side" in surrounding


def test_prompt_version_bumped_for_this_rule():
    assert PROMPT_VERSION == "5.5"
