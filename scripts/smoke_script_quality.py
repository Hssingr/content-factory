"""Smoke tests for script quality improvements.

Validates:
1. _SECTION_GENERATION_SYSTEM_PROMPT contains banned generic-phrase guidance.
2. _SECTION_GENERATION_SYSTEM_PROMPT contains channel-config-driven style rules.
3. _SECTION_GENERATION_SYSTEM_PROMPT contains the concrete-moment requirement.
4. OUTRO rules contain emotional resolution and comment-trigger build guidance.
5. detect_generic_documentary_phrases() flags known clichés.
6. detect_generic_documentary_phrases() does NOT flag clean concrete narration.
7. detect_generic_documentary_phrases() is importable as a public symbol.
8. GENERIC_PHRASE WARNING is wired into generate_script_sections source.

No API calls. Run with:
    python scripts/smoke_script_quality.py
"""

import sys
import os
import io
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0


def check(label: str, condition: bool) -> None:
    global _failures
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if not condition:
        _failures += 1


# ─────────────────────────────────────────────────────────────────────────────
# 1 — Prompt contains banned generic-phrase guidance
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 1: Banned generic phrases in system prompt ──")

from app.agents.agent2_discovery.system_prompt import _SECTION_GENERATION_SYSTEM_PROMPT

check(
    "prompt: 'this is not just' listed as banned",
    "this is not just" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'something far worse' listed as banned",
    "something far worse" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'what happened next' listed as banned",
    "what happened next" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'little did they know' listed as banned",
    "little did they know" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'brace yourself' listed as banned",
    "brace yourself" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'Banned generic phrases' heading present",
    "Banned generic phrases" in _SECTION_GENERATION_SYSTEM_PROMPT,
)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — Prompt contains channel-config-driven style rules (not hardcoded genre)
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 2: Channel-config-driven style rules ──")

check(
    "prompt: channel configuration mentioned as style source",
    "channel" in _SECTION_GENERATION_SYSTEM_PROMPT.lower(),
)
check(
    "prompt: tone-conditional rule (do not hardcode genre)",
    "channel tone" in _SECTION_GENERATION_SYSTEM_PROMPT.lower()
    or "configured tone" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: horror / thriller example present (as niche example, not hardcoded rule)",
    "horror" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: educational / documentary example present",
    "educational" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'Never impose a register' or equivalent anti-hardcode rule",
    "Never impose" in _SECTION_GENERATION_SYSTEM_PROMPT
    or "contradict" in _SECTION_GENERATION_SYSTEM_PROMPT,
)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — Prompt contains concrete-moment requirement for body sections
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 3: Concrete-moment requirement ──")

check(
    "prompt: 'concrete moment' or equivalent per-section requirement",
    "concrete moment" in _SECTION_GENERATION_SYSTEM_PROMPT
    or "at least one concrete" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'Abstract interpretation is not a substitute' rule",
    "Abstract interpretation is not a substitute" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'thematic essays' rule (only if tone requires analysis)",
    "thematic essays" in _SECTION_GENERATION_SYSTEM_PROMPT
    or "thematic essay" in _SECTION_GENERATION_SYSTEM_PROMPT,
)

# ─────────────────────────────────────────────────────────────────────────────
# 4 — OUTRO rules: emotional resolution + comment-trigger build
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 4: OUTRO emotional resolution rules ──")

check(
    "outro: 'emotionally' or equivalent emotional resolution instruction",
    "emotionally" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "outro: 'final 2' or '2-3' lines build into comment trigger",
    "final 2" in _SECTION_GENERATION_SYSTEM_PROMPT
    or "final 2–3" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "outro: 'do not repeat body facts' or equivalent rule",
    "Do not repeat body facts" in _SECTION_GENERATION_SYSTEM_PROMPT
    or "do not repeat" in _SECTION_GENERATION_SYSTEM_PROMPT.lower(),
)
check(
    "outro: 'adding a final consequence' exception present",
    "final consequence" in _SECTION_GENERATION_SYSTEM_PROMPT,
)

# ─────────────────────────────────────────────────────────────────────────────
# 5 — detect_generic_documentary_phrases() flags known clichés
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 5: Detector flags clichés ──")

from app.services.script_checks import detect_generic_documentary_phrases

# Script with multiple banned phrases
_CLICHE_SCRIPT = (
    "[INTRO]\n"
    "She took the envelope. But here's the thing — nobody saw it coming.\n\n"
    "[SECTION 1]\n"
    "This is not just a story about money. Little did they know what was inside.\n\n"
    "[SECTION 2]\n"
    "And that's when everything changed. It gets worse from here.\n\n"
    "[OUTRO]\n"
    "What really happened that night? Brace yourself for the answer.\n"
)

hits = detect_generic_documentary_phrases(_CLICHE_SCRIPT)
hit_phrases = [h["phrase"] for h in hits]

check("detector: returns non-empty list for cliché script", len(hits) > 0)
check("detector: flags 'but here's the thing'", "but here's the thing" in hit_phrases)
check("detector: flags 'this is not just'", "this is not just" in hit_phrases)
check("detector: flags 'little did they know'", "little did they know" in hit_phrases)
check("detector: flags 'brace yourself'", "brace yourself" in hit_phrases)
check("detector: each hit has 'phrase' key", all("phrase" in h for h in hits))
check("detector: each hit has 'sentence' key", all("sentence" in h for h in hits))
check("detector: sentence field is ≤150 chars", all(len(h["sentence"]) <= 150 for h in hits))

# ─────────────────────────────────────────────────────────────────────────────
# 6 — Detector does NOT flag clean concrete narration
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 6: Detector clean on concrete narration ──")

_CLEAN_SCRIPT = (
    "[INTRO]\n"
    "She accepted seven hundred dollars to deliver three sealed envelopes.\n\n"
    "[SECTION 1]\n"
    "The first delivery took her to a high-rise on Fifth Avenue. "
    "She rang the buzzer. A man in a grey suit collected the envelope without a word.\n\n"
    "[SECTION 2]\n"
    "The second address was a parking structure on the edge of the warehouse district. "
    "The man waiting there checked the seal twice before signing the receipt.\n\n"
    "[OUTRO]\n"
    "Three envelopes. Three strangers. And one question she kept asking herself: "
    "what was she really carrying?\n"
)

clean_hits = detect_generic_documentary_phrases(_CLEAN_SCRIPT)
check("detector: returns empty list for clean concrete script", len(clean_hits) == 0)

# ─────────────────────────────────────────────────────────────────────────────
# 7 — Public symbol import
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 7: Public symbol importable ──")

import importlib
mod = importlib.import_module("app.services.script_checks")
check(
    "detect_generic_documentary_phrases importable from script_checks",
    hasattr(mod, "detect_generic_documentary_phrases"),
)
check(
    "_GENERIC_DOCUMENTARY_PHRASES importable (for tuning)",
    hasattr(mod, "_GENERIC_DOCUMENTARY_PHRASES"),
)
check(
    "banned phrase list is non-empty",
    len(mod._GENERIC_DOCUMENTARY_PHRASES) > 0,
)

# ─────────────────────────────────────────────────────────────────────────────
# 8 — GENERIC_PHRASE WARNING wired into generate_script_sections source
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 8: WARNING wired in generate_script_sections ──")

import inspect
from app.agents.agent2_discovery.services.scripts import generate_script_sections

src = inspect.getsource(generate_script_sections)
check(
    "generate_script_sections: detect_generic_documentary_phrases called",
    "detect_generic_documentary_phrases" in src,
)
check(
    "generate_script_sections: GENERIC_PHRASE in WARNING log",
    "GENERIC_PHRASE" in src,
)
check(
    "generate_script_sections: detector is non-blocking (no raise/return after hit)",
    "for _hit in _phrase_hits:" in src,
)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — script quality improvements: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
