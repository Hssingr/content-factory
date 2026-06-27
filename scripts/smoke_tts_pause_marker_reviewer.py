"""Smoke test for the TTS pause-marker reviewer.

No live Claude/TTS calls. This stubs only the paid Claude call imported into
Agent 3 TTS code and exercises real prepare_script_for_tts() logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.agent3_audio.services import tts

ASSERTIONS = 0


def check(label: str, condition: bool) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(label)


def deterministic_text(source: str, tone: str = "calm") -> str:
    original = tts.call_claude
    tts.call_claude = lambda *args, **kwargs: args[1]
    try:
        return tts.prepare_script_for_tts(source, "en", tone, tts_model="sonic-2")
    finally:
        tts.call_claude = original


def run_with_stub(source: str, stub, tone: str = "calm") -> tuple[str, list[dict]]:
    calls: list[dict] = []
    original = tts.call_claude

    def wrapped(system_prompt, user_message, max_tokens=1024, *, task, model_override=None):
        calls.append(
            {
                "system_prompt": system_prompt,
                "user_message": user_message,
                "max_tokens": max_tokens,
                "task": task,
                "model_override": model_override,
            }
        )
        return stub(system_prompt, user_message, max_tokens=max_tokens, task=task, model_override=model_override)

    tts.call_claude = wrapped
    try:
        return tts.prepare_script_for_tts(source, "en", tone, tts_model="sonic-2"), calls
    finally:
        tts.call_claude = original


SOURCE = "[INTRO]\nHe opened the box. Then I found the missing photo inside the locked drawer. The answer was inside."
BASE = deterministic_text(SOURCE)
check("deterministic pause insertion produced ellipsis before review", "... Then" in BASE)


def safe_punctuation_only(_system, user_message, **_kwargs):
    return user_message.replace(" ... Then", " Then")

accepted, accepted_calls = run_with_stub(SOURCE, safe_punctuation_only)
check("safe punctuation-only correction is accepted", accepted == BASE.replace(" ... Then", " Then"))
check("reviewer called exactly once for accepted case", len(accepted_calls) == 1)
check("reviewer uses pause_marker_review task", accepted_calls[0]["task"] == "pause_marker_review")
check("deterministic text is reviewer input", accepted_calls[0]["user_message"] == BASE)
check("prompt tells reviewer to keep every word", "Keep every word exactly the same" in accepted_calls[0]["system_prompt"])


def changes_word(_system, user_message, **_kwargs):
    return user_message.replace("silent", "quiet")

changed_word, changed_word_calls = run_with_stub(SOURCE, changes_word)
check("word change is rejected and falls back", changed_word == BASE)
check("word-change scenario called reviewer once", len(changed_word_calls) == 1)


def adds_word(_system, user_message, **_kwargs):
    return user_message.replace("room went", "room suddenly went")

added_word, added_word_calls = run_with_stub(SOURCE, adds_word)
check("added word is rejected and falls back", added_word == BASE)
check("added-word scenario called reviewer once", len(added_word_calls) == 1)


def removes_word(_system, user_message, **_kwargs):
    return user_message.replace("the room ", "")

removed_word, removed_word_calls = run_with_stub(SOURCE, removes_word)
check("removed words are rejected and fall back", removed_word == BASE)
check("removed-word scenario called reviewer once", len(removed_word_calls) == 1)


def raises_failure(*_args, **_kwargs):
    raise RuntimeError("stubbed Haiku failure")

failed, failed_calls = run_with_stub(SOURCE, raises_failure)
check("Haiku exception falls back", failed == BASE)
check("failure scenario called reviewer once", len(failed_calls) == 1)

print(f"SMOKE PASS - {ASSERTIONS} checks")
print(f"deterministic={BASE}")
print(f"accepted={accepted}")
print(f"fallback_word_change={changed_word}")
print(f"fallback_add={added_word}")
print(f"fallback_remove={removed_word}")
print(f"fallback_exception={failed}")
