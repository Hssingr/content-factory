"""Storyboard quote-escaping prompt rule smoke test — zero API calls, zero DB access.

Verifies the preventive fix for a real, recurring production failure: across
two separate full-pipeline runs (4 Claude calls total: 2 attempts x 2 runs),
generate_storyboard_batch() for [SECTION 2] consistently failed with
json.JSONDecodeError "Expecting ',' delimiter" at nearly the same offset in
the manually-stringified 'beats' field. The recurring offset (not random)
indicated the model was embedding a literal, unescaped `"` character (from
quoted dialogue in the narration) inside a JSON string value while manually
serializing the beats array as a string — breaking the JSON structure at that
exact point every time the same segment content was generated.

This cannot be safely repaired after the fact (heuristically re-escaping
stray quotes risks silently corrupting beat content, which CLAUDE.md's
"no silent fallbacks" / "no unvalidated AI output" rules forbid). The fix is
preventive: an explicit system-prompt rule telling Claude never to copy
literal quotation marks into any field value, and never to emit `beats` as a
string at all.

Verifies:
  1. _STORYBOARD_SYSTEM_PROMPT explicitly forbids literal quotation marks in
     any field value.
  2. _STORYBOARD_SYSTEM_PROMPT explicitly forbids emitting 'beats' as a
     JSON-encoded string instead of a native array.
  3. The hint exact-copy rule carries an explicit, documented exception for
     quotation marks (so this new rule does not silently contradict the
     existing "copied EXACTLY" requirement for start_hint/end_hint).
  4. PROMPT_VERSION is still a valid identifier (every system prompt must
     expose a version per CLAUDE.md §21.1) -- this smoke test does not
     require bumping it, but confirms the field still exists and is non-empty.

Run: python scripts/smoke_storyboard_quote_escaping_rule.py
Expected output: all lines prefixed with PASS, then SMOKE PASS
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]")


from app.agents.agent4_visuals.system_prompt import _STORYBOARD_SYSTEM_PROMPT, PROMPT_VERSION

assert_ok("imports", True)

prompt = _STORYBOARD_SYSTEM_PROMPT

assert_ok(
    "prompt forbids literal quotation marks in field values",
    "literal quotation mark" in prompt and 'character (")' in prompt,
    "expected explicit no-literal-quote-marks instruction not found",
)

assert_ok(
    "prompt forbids emitting 'beats' as a JSON-encoded string",
    "native JSON array" in prompt and "never a JSON-encoded string" in prompt,
    "expected native-array-not-string instruction not found",
)

assert_ok(
    "exact-copy hint rule documents an explicit quotation-mark exception",
    "EXCEPT" in prompt and "quotation mark" in prompt,
    "expected EXCEPT clause for quotation marks in the hint copy rule not found",
)

assert_ok(
    "PROMPT_VERSION is set and non-empty",
    isinstance(PROMPT_VERSION, str) and len(PROMPT_VERSION) > 0,
    f"got {PROMPT_VERSION!r}",
)

print("\nSMOKE PASS")
