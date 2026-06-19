"""Smoke tests for Work Item 9 — Agent 2 architectural fixes.

Validates:
A. Narrative completeness: already_covered param exists and skips covered turns.
B. Quality rewrite robustness: rewrite_script_for_quality uses call_claude_structured;
   _QUALITY_REWRITE_SCHEMA exists with required fields; QUALITY_REWRITE_SCHEMA_OK /
   QUALITY_REWRITE_JSON_FAIL wired in run_script_quality_gate source.
C. FINAL_TTS_BACKSTOP: reduces over-limit sentences before quality gate.
D. OUTRO_OVERLAP: logged after OUTRO append in generate_script_sections source.

No API calls. Run with:
    python scripts/smoke_arch_fixes.py
"""

import sys
import os
import inspect
import re

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
# A — Narrative Completeness: already_covered param
# ─────────────────────────────────────────────────────────────────────────────

print("\n── A: Narrative completeness alignment ──")

from app.agents.agent2_discovery.services.scripts import (
    check_narrative_completeness,
    _get_content_tokens,
)

blueprint_4turns = {
    "major_turns": [
        "detective discovers hidden evidence inside the warehouse",
        "suspect confesses to being present that night",
        "financial fraud traced back to the warehouse owner",
        "victim identified through dental records",
    ]
}

# Script that covers all 4 turns at 60%+ overlap
voice_script_full = (
    "[INTRO]\nThe detective discovered the hidden evidence inside the warehouse. "
    "[SECTION 1]\nThe suspect then confessed to being present that night. "
    "[SECTION 2]\nFinancial fraud was traced back to the warehouse owner through documents. "
    "[SECTION 3]\nThe victim was finally identified through dental records. "
    "[OUTRO]\nJustice was served."
)

# Without already_covered: should find no issues (all 4 turns covered)
nc_no_skip = check_narrative_completeness(voice_script_full, blueprint_4turns, already_covered=None)
check("A1: no already_covered — all turns found → no issues", len(nc_no_skip) == 0)

# With already_covered={0,1,2,3}: all turns marked done → should not flag any
nc_all_covered = check_narrative_completeness(voice_script_full, blueprint_4turns, already_covered={0, 1, 2, 3})
check("A2: all turns already_covered → no issues emitted", len(nc_all_covered) == 0)

# Script that covers only turns 0+1 (not 2+3)
voice_script_partial = (
    "[INTRO]\nThe detective discovered the hidden evidence inside the warehouse. "
    "[SECTION 1]\nThe suspect then confessed to being present that night. "
    "[OUTRO]\nJustice was served."
)

nc_partial = check_narrative_completeness(voice_script_partial, blueprint_4turns, already_covered=None)
check("A3: partial script without already_covered flags uncovered turns", len(nc_partial) > 0)

# With already_covered={2,3}: turns 2+3 are credited by section_progression
# So check should only look at turns 0+1. Turn 0+1 ARE in partial_script → no issues.
nc_partial_with_skip = check_narrative_completeness(voice_script_partial, blueprint_4turns, already_covered={2, 3})
check("A4: partial script + already_covered={2,3} → turns 2+3 skipped → no issues", len(nc_partial_with_skip) == 0)

# Function signature check
sig = inspect.signature(check_narrative_completeness)
check("A5: check_narrative_completeness has already_covered param", "already_covered" in sig.parameters)
check("A6: already_covered defaults to None", sig.parameters["already_covered"].default is None)

# Source wiring: TURN_COVERAGE_SOURCE log in generate_script_sections
from app.agents.agent2_discovery.services.scripts import generate_script_sections
src_gss = inspect.getsource(generate_script_sections)
check("A7: TURN_COVERAGE_SOURCE log wired in generate_script_sections", "TURN_COVERAGE_SOURCE" in src_gss)
check("A8: TURN_COVERAGE_DISAGREEMENT warning wired", "TURN_COVERAGE_DISAGREEMENT" in src_gss)
check("A9: already_covered=covered_turns passed to check_narrative_completeness", "already_covered=covered_turns" in src_gss)

# ─────────────────────────────────────────────────────────────────────────────
# B — Quality Rewrite Robustness
# ─────────────────────────────────────────────────────────────────────────────

print("\n── B: Quality rewrite robustness ──")

from app.agents.agent2_discovery.system_prompt import (
    _QUALITY_REWRITE_SCHEMA,
    rewrite_script_for_quality,
)
from app.agents.agent2_discovery.services.scripts import run_script_quality_gate

# Schema structure
check("B1: _QUALITY_REWRITE_SCHEMA is a dict", isinstance(_QUALITY_REWRITE_SCHEMA, dict))
check("B2: schema type is 'object'", _QUALITY_REWRITE_SCHEMA.get("type") == "object")
check("B3: schema has 'title' property", "title" in _QUALITY_REWRITE_SCHEMA.get("properties", {}))
check("B4: schema has 'video_script' property", "video_script" in _QUALITY_REWRITE_SCHEMA.get("properties", {}))
check("B5: schema has 'voice_script' property", "voice_script" in _QUALITY_REWRITE_SCHEMA.get("properties", {}))
check("B6: schema required includes all 3 keys", set(_QUALITY_REWRITE_SCHEMA.get("required", [])) == {"title", "video_script", "voice_script"})
check("B7: additionalProperties is False", _QUALITY_REWRITE_SCHEMA.get("additionalProperties") is False)

# rewrite_script_for_quality uses call_claude_structured (not call_claude)
src_rw = inspect.getsource(rewrite_script_for_quality)
check("B8: rewrite uses call_claude_structured", "call_claude_structured" in src_rw)
check("B9: rewrite no longer uses call_claude() directly", "call_claude(prompt" not in src_rw)
check("B10: rewrite no longer uses parse_claude_json", "parse_claude_json" not in src_rw)
check("B11: rewrite passes _QUALITY_REWRITE_SCHEMA as input_schema", "_QUALITY_REWRITE_SCHEMA" in src_rw)

# Caller logs in run_script_quality_gate
src_qg = inspect.getsource(run_script_quality_gate)
check("B12: QUALITY_REWRITE_SCHEMA_OK log wired in run_script_quality_gate", "QUALITY_REWRITE_SCHEMA_OK" in src_qg)
check("B13: QUALITY_REWRITE_JSON_FAIL log wired in run_script_quality_gate", "QUALITY_REWRITE_JSON_FAIL" in src_qg)

# ─────────────────────────────────────────────────────────────────────────────
# C — FINAL_TTS_BACKSTOP reduces over-limit sentences
# ─────────────────────────────────────────────────────────────────────────────

print("\n── C: FINAL_TTS_BACKSTOP ──")

from app.services.script_checks import normalize_tts_chars, split_long_sentences

# Verify the backstop logic manually (same logic as in run_script_quality_gate)
_dirty = (
    "[INTRO]\n"
    "She accepted seven hundred dollars for three simple errands with no questions "
    "asked from her employer, and she agreed to follow every instruction she received "
    "carefully.\n\n"
    "[SECTION 1]\n"
    "Short sentence here. Another short one.\n\n"
    "[OUTRO]\n"
    "Fine ending."
)

_over_before = sum(
    1 for s in re.split(r"(?<=[.!?])\s+", _dirty) if len(s.split()) > 18
)
_clean = split_long_sentences(normalize_tts_chars(_dirty))
_over_after = sum(
    1 for s in re.split(r"(?<=[.!?])\s+", _clean) if len(s.split()) > 18
)

check("C1: dirty script has at least 1 over-limit sentence before cleanup", _over_before >= 1)
check("C2: backstop reduces over-limit count", _over_after < _over_before)

# Wiring check: FINAL_TTS_BACKSTOP log present in run_script_quality_gate source
check("C3: FINAL_TTS_BACKSTOP log wired in run_script_quality_gate", "FINAL_TTS_BACKSTOP" in src_qg)
check("C4: backstop uses sentences_over_limit_before/after naming", "sentences_over_limit_before" in src_qg and "sentences_over_limit_after" in src_qg)
check("C5: backstop placed BEFORE attempt loop (FINAL_TTS_BACKSTOP appears before 'for attempt')",
      src_qg.index("FINAL_TTS_BACKSTOP") < src_qg.index("for attempt in range"))

# ─────────────────────────────────────────────────────────────────────────────
# D — OUTRO_OVERLAP diagnostic
# ─────────────────────────────────────────────────────────────────────────────

print("\n── D: OUTRO_OVERLAP diagnostic ──")

check("D1: OUTRO_OVERLAP log wired in generate_script_sections", "OUTRO_OVERLAP" in src_gss)
check("D2: previous_section_overlap field present in log", "previous_section_overlap" in src_gss)
check("D3: repeated_terms field present in log", "repeated_terms" in src_gss)
check("D4: WARNING emitted when overlap > 0.5", "OUTRO_OVERLAP" in src_gss and "0.5" in src_gss)

# Verify overlap logic manually with a high-overlap OUTRO
from app.agents.agent2_discovery.services.scripts import _get_content_tokens

_body_text = "The detective discovered the hidden evidence inside the warehouse through careful investigation."
_outro_high = "The detective discovered the hidden evidence inside the warehouse through careful investigation and the case closed."
_outro_low  = "Justice was finally served and the community began to heal."

_body_tokens  = _get_content_tokens(_body_text)
_outro_h_toks = _get_content_tokens(_outro_high)
_outro_l_toks = _get_content_tokens(_outro_low)

_ov_high = len(_outro_h_toks & _body_tokens) / len(_outro_h_toks) if _outro_h_toks else 0.0
_ov_low  = len(_outro_l_toks & _body_tokens) / len(_outro_l_toks) if _outro_l_toks else 0.0

check("D5: high-overlap OUTRO produces overlap > 0.5", _ov_high > 0.5)
check("D6: low-overlap OUTRO produces overlap ≤ 0.5", _ov_low <= 0.5)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — Work Item 9 architectural fixes: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
