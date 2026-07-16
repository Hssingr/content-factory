"""Runtime prompt-construction proof for standalone Short cold opens."""

from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery import system_prompt


def _flat(text: str) -> str:
    return " ".join(text.casefold().split())


def _planned_parts() -> list[dict]:
    return [
        {
            "part": part,
            "goal": f"Goal {part}",
            "opening_hook": f"Specific subject faces crisis {part}.",
            "main_content_summary": f"Summary {part}",
            "main_reveal": f"Reveal {part}",
            "cliffhanger": f"Question {part}?",
        }
        for part in range(1, 4)
    ]


def test_prompt_version_bumped_for_cold_open_contract():
    assert tuple(map(int, system_prompt.PROMPT_VERSION.split("."))) >= (5, 0)


def test_real_shorts_planner_call_receives_cold_open_deletion_test():
    response = {"total_parts": 3, "parts": _planned_parts()}
    channel = SimpleNamespace(niche="history", tone="tense")

    with patch.object(
        system_prompt, "call_claude_structured", return_value=response,
    ) as paid_boundary:
        result = system_prompt.generate_shorts_plan(
            "A complete long-form narration.", {"hook": "A concrete hook."}, channel,
        )

    assert result == response
    prompt = _flat(paid_boundary.call_args.kwargs["system_prompt"])
    assert "cold-open deletion test" in prompt
    assert "previous part" in prompt
    assert "they weren't hypothetical" in prompt
    assert "while withholding the part's payoff" in prompt


def test_real_short_script_call_can_repair_context_dependent_plan_hook():
    response = {
        "title": "The Stakes Become Real",
        "voice_script": "The emperor's threats weren't hypothetical. The city saw proof.",
    }
    part_plan = {
        "part": 1,
        "_total_parts": 3,
        "goal": "Show the stakes",
        "opening_hook": "They weren't hypothetical.",
        "main_content_summary": "The threat becomes concrete.",
        "main_reveal": "The city sees proof.",
        "cliffhanger": "Who caused it?",
    }
    channel = SimpleNamespace(niche="history", tone="tense")
    voice = SimpleNamespace(tts_model="sonic-3.5", provider="cartesia")

    with patch.object(
        system_prompt, "call_claude_structured", return_value=response,
    ) as paid_boundary:
        result = system_prompt.generate_short_episode_script(
            part_plan=part_plan,
            long_voice_script="The complete parent narration.",
            blueprint={"final_payoff": "The ending."},
            channel=channel,
            channel_voice=voice,
        )

    assert result == response
    prompt = _flat(paid_boundary.call_args.kwargs["system_prompt"])
    assert "cold-open deletion test" in prompt
    assert "repair that missing context" in prompt
    assert "instead of copying the fragment" in prompt
    assert "they weren't hypothetical" in prompt


def test_real_child_short_adaptation_call_preserves_self_contained_cold_open():
    response = {"voice_script": "La menace de l'empereur était bien réelle."}

    with patch.object(
        system_prompt, "call_claude_structured", return_value=response,
    ) as paid_boundary:
        result = system_prompt.generate_native_script(
            voice_script="The emperor's threats weren't hypothetical.",
            target_language="fr",
            niche="history",
            tone="tense",
            content_kind="child_short",
        )

    assert result == response
    prompt = _flat(paid_boundary.call_args.kwargs["system_prompt"])
    assert "self-contained cold open" in prompt
    assert "first thing the viewer ever hears" in prompt
    assert "repair only the missing referent" in prompt
    assert "without adding recap or revealing the payoff" in prompt


def test_word_cap_and_overlap_remain_prompt_and_telemetry_only():
    source = _flat(system_prompt._SHORT_EPISODE_SYSTEM_PROMPT)
    assert "target 210–260 words" in source
    assert "remove the least essential sentences" in source
    assert "originality" in source
    assert "never lift a run of 6 or more consecutive words" in source
