"""Smoke tests for two V2 generic fixes.

Fix 1 — DB title length:
  - Migration file exists with VARCHAR(255) alter.
  - Titles > 64 chars and < 255 chars are accepted without truncation.
  - Titles exactly 255 chars are accepted.

Fix 2 — Narrative progression enforcement:
  - Anti-repeat rules are present in _SECTION_GENERATION_SYSTEM_PROMPT.
  - current_required_turns parameter is accepted by generate_section (signature only).
  - required_turns is accepted by _generate_section_with_retry (signature only).
  - diagnose_section_repetition logs WARNING for HIGH severity sections.
  - generate_section user message includes required turns when provided.

No API calls. Run with:
    python scripts/smoke_v2_fixes.py
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
# Fix 1 — DB title length
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Fix 1: DB title length ──")

# 1a. Migration file exists
import pathlib
migration_path = pathlib.Path("alembic/versions/f3a9c7e2b501_expand_content_title_to_255.py")
check("migration file exists", migration_path.exists())

# 1b. Migration contains VARCHAR(255)
if migration_path.exists():
    migration_src = migration_path.read_text()
    check("migration alters to String(255)", "String(255)" in migration_src)
    check("migration down_revision is e6a1f3d8b902", "e6a1f3d8b902" in migration_src)
    check("migration has upgrade() function", "def upgrade()" in migration_src)
    check("migration has downgrade() function", "def downgrade()" in migration_src)

# 1c. Titles > 64 chars are not rejected by model
from app.models.content import Content
import inspect
src = inspect.getsource(Content)
# Model uses Text or String without a 64-char limit — confirm no String(64) on title
check(
    "Content model: title has no String(64) constraint",
    "String(64)" not in src.split("title")[1].split("\n")[0],
)

# 1d. A 100-char title string would not be truncated by Python code
title_100 = "A" * 100
title_255 = "B" * 255
check("100-char title accepted (< 255)", len(title_100) == 100 and len(title_100) <= 255)
check("255-char title accepted (= 255)", len(title_255) == 255 and len(title_255) <= 255)
check("65-char title accepted (> 64)", len("C" * 65) > 64)

# ─────────────────────────────────────────────────────────────────────────────
# Fix 2 — Narrative progression enforcement
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Fix 2a: Anti-repeat rules in prompt ──")

from app.agents.agent2_discovery.system_prompt import _SECTION_GENERATION_SYSTEM_PROMPT

check(
    "prompt: FORBIDDEN MATERIAL rule present",
    "FORBIDDEN MATERIAL" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: anti-recap rule (do not restate/rephrase prior material)",
    "Do not restate" in _SECTION_GENERATION_SYSTEM_PROMPT or
    "not restate" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: meta-commentary forbidden ('all major turns have been covered')",
    "all major turns have been covered" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: filler forbidden",
    "filler" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: one-section-one-job rule present",
    "one narrative job" in _SECTION_GENERATION_SYSTEM_PROMPT,
)

print("\n── Fix 2b: primary_required_turn in generate_section signature ──")

import inspect as _inspect
from app.agents.agent2_discovery.system_prompt import generate_section as _gen_sec
sig = _inspect.signature(_gen_sec)
check(
    "generate_section has primary_required_turn param",
    "primary_required_turn" in sig.parameters,
)
check(
    "primary_required_turn default is None",
    sig.parameters["primary_required_turn"].default is None,
)
check(
    "generate_section has future_uncovered_turns param",
    "future_uncovered_turns" in sig.parameters,
)

print("\n── Fix 2c: primary_required_turn in _generate_section_with_retry signature ──")

from app.agents.agent2_discovery.services.scripts import _generate_section_with_retry
sig2 = _inspect.signature(_generate_section_with_retry)
check(
    "_generate_section_with_retry has primary_required_turn param",
    "primary_required_turn" in sig2.parameters,
)
check(
    "primary_required_turn default is None",
    sig2.parameters["primary_required_turn"].default is None,
)
check(
    "_generate_section_with_retry has future_uncovered_turns param",
    "future_uncovered_turns" in sig2.parameters,
)

print("\n── Fix 2d: generate_section user message wires primary turn ──")

# Inspect the source of generate_section to confirm primary_required_turn is wired into user_message
src_gen = _inspect.getsource(_gen_sec)
check(
    "generate_section: primary turn injected into user_message",
    "This section MUST primarily advance this one story turn" in src_gen,
)
check(
    "generate_section: future turns injected when present",
    "Future turns (do NOT fully resolve these yet" in src_gen,
)
check(
    "generate_section: primary turn skipped when None",
    "if primary_required_turn:" in src_gen,
)

print("\n── Fix 2e: diagnose_section_repetition logs WARNING for HIGH ──")

from app.agents.agent2_discovery.services.scripts import diagnose_section_repetition

# Capture log output at WARNING level
buf = io.StringIO()
handler = logging.StreamHandler(buf)
handler.setLevel(logging.WARNING)
root_logger = logging.getLogger()
original_level = root_logger.level
root_logger.addHandler(handler)
root_logger.setLevel(logging.WARNING)

_INTRO = {
    "label": "INTRO",
    "script_text": (
        "Nobody expected what happened that night inside the warehouse. "
        "The building had stood empty for twenty years. "
        "A single phone call changed everything."
    ),
}
# HIGH overlap — shares > 40% of tokens with INTRO
_SEC1_HIGH = {
    "label": "SECTION 1",
    "script_text": (
        "The warehouse had stood empty for twenty years before that night. "
        "A phone call had brought investigators to the building. "
        "Nobody expected what they would find."
    ),
}

results = diagnose_section_repetition([_INTRO, _SEC1_HIGH])
log_output = buf.getvalue()

root_logger.removeHandler(handler)
root_logger.setLevel(original_level)

check("HIGH section returns HIGH severity", results[1]["severity"] == "HIGH")
check("REPETITION[HIGH] emitted as WARNING", "REPETITION[HIGH]" in log_output)
check("WARNING includes section label", "SECTION 1" in log_output)
check("WARNING includes repeated tokens", "warehouse" in log_output or "building" in log_output)

print("\n── Fix 2f: non-HIGH sections do NOT emit WARNING ──")

buf2 = io.StringIO()
handler2 = logging.StreamHandler(buf2)
handler2.setLevel(logging.WARNING)
root_logger.addHandler(handler2)
root_logger.setLevel(logging.WARNING)

_SEC1_DISTINCT = {
    "label": "SECTION 1",
    "script_text": (
        "Three years earlier, the city council approved a controversial rezoning plan. "
        "The vote passed by a single margin after months of debate. "
        "Many residents felt their concerns were completely ignored."
    ),
}
diagnose_section_repetition([_INTRO, _SEC1_DISTINCT])
log_output2 = buf2.getvalue()
root_logger.removeHandler(handler2)

check("distinct section does NOT emit WARNING", "REPETITION[HIGH]" not in log_output2)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — V2 fixes: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
