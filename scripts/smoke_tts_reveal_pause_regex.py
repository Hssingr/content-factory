"""Smoke test for deterministic reveal-pause insertion.

No live Claude/TTS calls. This stubs only the paid Claude pause-review call
imported into Agent 3 TTS code and exercises real prepare_script_for_tts().
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


def prepare_with_echo_review(source: str) -> tuple[str, list[str]]:
    calls: list[str] = []
    original = tts.call_claude

    def echo_review(_system_prompt, user_message, **_kwargs):
        calls.append(user_message)
        return user_message

    tts.call_claude = echo_review
    try:
        return tts.prepare_script_for_tts(source, "en", "calm", tts_model="sonic-2"), calls
    finally:
        tts.call_claude = original


negative_then, negative_then_calls = prepare_with_echo_review(
    "She checked the lock. Then she walked into the room."
)
check("ordinary Then transition does not get reveal pause", "... Then she walked" not in negative_then)
check("ordinary Then still runs Haiku review once", len(negative_then_calls) == 1)

negative_but, negative_but_calls = prepare_with_echo_review(
    "She checked the hallway. But the hallway was empty."
)
check("ordinary But transition does not get reveal pause", "... But the hallway" not in negative_but)
check("ordinary But still runs Haiku review once", len(negative_but_calls) == 1)

negative_truth, negative_truth_calls = prepare_with_echo_review(
    "She sat down. The truth is, I was tired."
)
check("weak truth sentence does not get reveal pause", "... The truth is" not in negative_truth)
check("weak truth still runs Haiku review once", len(negative_truth_calls) == 1)

positive_discovery, discovery_calls = prepare_with_echo_review(
    "She checked the desk. Then I found the missing photo inside the locked drawer."
)
check("real discovery sentence gets reveal pause", "... Then I found" in positive_discovery)
check("discovery reviewer input includes deterministic pause", discovery_calls == [positive_discovery])

positive_reversal, reversal_calls = prepare_with_echo_review(
    "I played the recording. But the voice on the tape was mine."
)
check("real contradiction reversal gets reveal pause", "... But the voice" in positive_reversal)
check("reversal reviewer input includes deterministic pause", reversal_calls == [positive_reversal])

positive_realization, realization_calls = prepare_with_echo_review(
    "I tried the handle. That was when I realized the door had been locked from the outside."
)
check("real realization sentence gets reveal pause", "... That was when I realized" in positive_realization)
check("realization reviewer input includes deterministic pause", realization_calls == [positive_realization])

check(
    "Haiku reviewer remains second pass after deterministic logic",
    discovery_calls[0] == positive_discovery
    and reversal_calls[0] == positive_reversal
    and realization_calls[0] == positive_realization,
)

print(f"SMOKE PASS - {ASSERTIONS} checks")
print(f"ordinary_then={negative_then}")
print(f"ordinary_but={negative_but}")
print(f"weak_truth={negative_truth}")
print(f"discovery={positive_discovery}")
print(f"reversal={positive_reversal}")
print(f"realization={positive_realization}")
