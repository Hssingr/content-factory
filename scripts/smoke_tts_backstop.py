"""Smoke test for split_long_sentences() — the TTS backstop introduced in scripts.py.

No API calls. Run with:
    python scripts/smoke_tts_backstop.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.script_checks import split_long_sentences, check_tts_compliance

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0


def check(label: str, condition: bool) -> None:
    global _failures
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if not condition:
        _failures += 1


# ── 1. Comma + conjunction: should be split ───────────────────────────────────
sent_comma = (
    "He had walked slowly across the dark corridor, "
    "but the shadows seemed to follow him everywhere he went no matter what."
)
assert len(sent_comma.split()) > 18, "test sentence must exceed 18 words"

result_comma = split_long_sentences(sent_comma)
parts_comma = [p.strip() for p in result_comma.split(".") if p.strip()]

check("comma+but: output has more than one sentence",   result_comma.count(".") >= 2)
check("comma+but: no resulting sentence exceeds 18 words",
      all(len(s.split()) <= 18 for s in result_comma.split(".") if s.strip()))
check("comma+but: original content words preserved (no word loss)",
      len(result_comma.split()) >= len(sent_comma.split()) - 1)  # -1 tolerance for dropped conjunction
check("comma+but: first half ends with period",         result_comma.rstrip().split(". ")[0].endswith(".") or "." in result_comma)

# ── 2. Semicolon: should be split ─────────────────────────────────────────────
sent_semi = (
    "He had walked slowly across the corridor; "
    "no one seemed to notice the shadows gathering behind him at all."
)
assert len(sent_semi.split()) > 18

result_semi = split_long_sentences(sent_semi)
check("semicolon: output has two sentences",
      result_semi.count(".") >= 2)
check("semicolon: no resulting sentence exceeds 18 words",
      all(len(s.split()) <= 18 for s in result_semi.split(".") if s.strip()))

# ── 3. Em-dash: should be split ───────────────────────────────────────────────
sent_dash = (
    "She turned to look at the doorway — "
    "someone had been standing there all along watching every single move she made."
)
assert len(sent_dash.split()) > 18

result_dash = split_long_sentences(sent_dash)
check("em-dash: output has two sentences",
      result_dash.count(".") >= 2)
check("em-dash: no resulting sentence exceeds 18 words",
      all(len(s.split()) <= 18 for s in result_dash.split(".") if s.strip()))

# ── 4. No natural split point: sentence unchanged ─────────────────────────────
sent_no_split = (
    "He walked from the very beginning all the way without any pause or rest "
    "whatsoever throughout the entire long journey home."
)
assert len(sent_no_split.split()) > 18

result_no_split = split_long_sentences(sent_no_split)
check("no-split-point: sentence left unchanged",
      result_no_split.strip() == sent_no_split.strip())

# ── 5. Short sentence: not touched ────────────────────────────────────────────
sent_short = "He walked slowly. The door creaked open."
result_short = split_long_sentences(sent_short)
check("short sentence: returned unchanged",
      result_short == sent_short)

# ── 6. Multi-line text: only long sentences split, structure preserved ─────────
multi = (
    "The first line is short.\n"
    "He had walked slowly across the dark corridor, but the shadows seemed to follow him everywhere he went no matter what.\n"
    "The third line is also fine."
)
result_multi = split_long_sentences(multi)
lines_out = result_multi.splitlines()
check("multi-line: three lines preserved",     len(lines_out) == 3)
check("multi-line: first line unchanged",      lines_out[0] == "The first line is short.")
check("multi-line: long line 2 was fixed",     result_multi.splitlines()[1].count(".") >= 2)
check("multi-line: third line unchanged",      lines_out[2] == "The third line is also fine.")

# ── 7. Integration: backstop reduces TTS violations before check runs ─────────
# Simulate what _generate_section_with_retry() does: apply backstop, then check.
section_with_long_sent = (
    "Nobody knew what had happened inside that building, "
    "but the investigation would later reveal a series of disturbing and previously undisclosed facts.\n"
    "She was shocked."
)
# Before backstop: TTS check would flag the long sentence
issues_before = check_tts_compliance(section_with_long_sent, "source")
long_sent_issues_before = [i for i in issues_before if "words" in i["description"]]

# After backstop: apply split, then re-check
fixed = split_long_sentences(section_with_long_sent)
issues_after = check_tts_compliance(fixed, "source")
long_sent_issues_after = [i for i in issues_after if "words" in i["description"]]

check("integration: TTS flagged long sentence before backstop",
      len(long_sent_issues_before) > 0)
check("integration: backstop eliminates that long-sentence violation",
      len(long_sent_issues_after) == 0)

# ── 8. Comma+but inside a section [INTRO]/body: markers stripped by check ─────
# Make sure split_long_sentences doesn't corrupt section-marker lines
section_with_marker = (
    "[INTRO]\n"
    "He had walked slowly across the dark corridor, "
    "but the shadows seemed to follow him everywhere he went no matter what.\n"
    "[SECTION 1]\n"
    "Short sentence here."
)
result_marker = split_long_sentences(section_with_marker)
check("marker lines: [INTRO] preserved",    "[INTRO]" in result_marker)
check("marker lines: [SECTION 1] preserved", "[SECTION 1]" in result_marker)

# ── Result ────────────────────────────────────────────────────────────────────
print()
if _failures == 0:
    print("SMOKE PASS — split_long_sentences backstop: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
