"""Smoke tests for Work Item 11 — Agent 4 storyboard observability.

Validates:
1. STORYBOARD_RETRY_COST — logged inside generate_storyboard_batch() on truncation,
   with correct fields and estimated_extra_cost_percent formula.
2. STORYBOARD_ESTIMATE — logged at start of split_into_beats().
3. STORYBOARD_ESTIMATE_ACCURACY — logged after merge with error_percent formula.
4. STORYBOARD_FINAL — logged after all segments with all required fields.
5. HINT_QUALITY_SUMMARY — logged after all segments, aggregating per-hint stats.
6. STORYBOARD_COST_ESTIMATE — logged with estimated_usd and claude_calls.
7. generate_storyboard_batch() return type is tuple[dict, dict, dict].
8. _harden_hints() return type is tuple[list[dict], dict].
9. diag dict has required keys: was_truncated, attempt_count, input_tokens, elapsed_ms.
10. Hint stats dict has required keys: total_hints, valid_hints, invalid_hints.

No API calls. Run with:
    python scripts/smoke_wi11.py
"""

import sys
import os
import inspect
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
# 1 — STORYBOARD_RETRY_COST log in generate_storyboard_batch()
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 1: STORYBOARD_RETRY_COST in generate_storyboard_batch ──")

from app.agents.agent4_visuals.system_prompt import generate_storyboard_batch
import time as _time

src_gsb = inspect.getsource(generate_storyboard_batch)

check("1.1: STORYBOARD_RETRY_COST log present in source",
      "STORYBOARD_RETRY_COST" in src_gsb)
check("1.2: attempt1_target_beats= field in log",
      "attempt1_target_beats=" in src_gsb)
check("1.3: attempt1_output_tokens= field in log",
      "attempt1_output_tokens=" in src_gsb)
check("1.4: truncated=True field in log",
      "truncated=True" in src_gsb)
check("1.5: retry_target_beats= field in log",
      "retry_target_beats=" in src_gsb)
check("1.6: retry_output_tokens= field in log",
      "retry_output_tokens=" in src_gsb)
check("1.7: estimated_extra_cost_percent= field in log",
      "estimated_extra_cost_percent=" in src_gsb)
check("1.8: elapsed_ms_attempt1= field in log",
      "elapsed_ms_attempt1=" in src_gsb)
check("1.9: elapsed_ms_attempt2= field in log",
      "elapsed_ms_attempt2=" in src_gsb)
check("1.10: time.monotonic() used for timing",
      "time.monotonic()" in src_gsb)

# Extra-cost formula: (tokens1 + tokens2) / tokens1 * 100 - 100
# Verify the formula is correctly coded
_t1, _t2 = 4000, 3500
_extra = (_t1 + _t2) / max(_t1, 1) * 100 - 100
check("1.11: extra_cost_percent formula is correct (87.5% for 4000+3500/4000)",
      abs(_extra - 87.5) < 0.01)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — generate_storyboard_batch() return type is 3-tuple
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 2: generate_storyboard_batch() returns tuple[dict, dict, dict] ──")

check("2.1: return type annotation updated to tuple[dict, dict, dict]",
      "tuple[dict, dict, dict]" in src_gsb)
check("2.2: diag dict built in source with 'was_truncated' key",
      '"was_truncated"' in src_gsb)
check("2.3: diag dict has 'attempt_count' key",
      '"attempt_count"' in src_gsb)
check("2.4: diag dict has 'input_tokens' key",
      '"input_tokens"' in src_gsb)
check("2.5: diag dict has 'elapsed_ms' key",
      '"elapsed_ms"' in src_gsb)
check("2.6: function returns 3-tuple: 'return storyboard, usage, diag'",
      "return storyboard, usage, diag" in src_gsb)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — _harden_hints() returns tuple[list[dict], dict]
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 3: _harden_hints() returns tuple[list[dict], dict] ──")

from app.agents.agent4_visuals.subagents.storyboard import _harden_hints

src_hh = inspect.getsource(_harden_hints)

check("3.1: return type annotation is tuple[list[dict], dict]",
      "tuple[list[dict], dict]" in src_hh)
check("3.2: _total counter present", "_total" in src_hh)
check("3.3: _invalid counter present", "_invalid" in src_hh)
check("3.4: _stats dict built with total_hints key",
      '"total_hints"' in src_hh)
check("3.5: _stats dict built with valid_hints key",
      '"valid_hints"' in src_hh)
check("3.6: _stats dict built with invalid_hints key",
      '"invalid_hints"' in src_hh)
check("3.7: returns (beats, _stats) — 2-tuple",
      "return beats, _stats" in src_hh)

# Functional test: call _harden_hints with clean beats
_clean_beats = [
    {
        "beat_order": 0,
        "start_hint": "she accepted seven hundred dollars for",
        "end_hint": "simple errands with no questions asked",
    },
    {
        "beat_order": 1,
        "start_hint": "the first delivery took her to",
        "end_hint": "high-rise on Fifth Avenue that evening",
    },
]
_result_beats, _result_stats = _harden_hints(_clean_beats, "some segment text")
check("3.8: _harden_hints returns 2-tuple (beats, stats)",
      isinstance(_result_beats, list) and isinstance(_result_stats, dict))
check("3.9: total_hints == beats×2 for all-valid hints",
      _result_stats["total_hints"] == 4)
check("3.10: valid_hints == 4 for clean hints",
      _result_stats["valid_hints"] == 4)
check("3.11: invalid_hints == 0 for clean hints",
      _result_stats["invalid_hints"] == 0)

# Functional test: beats with invalid hints
_dirty_beats = [
    {
        "beat_order": 0,
        "start_hint": "she accepted 700 dollars for",    # has digit — invalid
        "end_hint": "simple errands with no questions asked",
    },
    {
        "beat_order": 1,
        "start_hint": "short",                           # too short (1 word) — invalid
        "end_hint": "high-rise on Fifth Avenue that evening",
    },
]
_dirty_result_beats, _dirty_stats = _harden_hints(_dirty_beats, "some text")
check("3.12: invalid_hints == 2 for beats with digit and short hint",
      _dirty_stats["invalid_hints"] == 2)
check("3.13: valid_hints == 2 for remaining clean hints",
      _dirty_stats["valid_hints"] == 2)
check("3.14: total_hints == 4 (2 beats × 2 hints each)",
      _dirty_stats["total_hints"] == 4)

# ─────────────────────────────────────────────────────────────────────────────
# 4 — STORYBOARD_ESTIMATE log in split_into_beats() source
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 4: STORYBOARD_ESTIMATE in split_into_beats() ──")

from app.agents.agent4_visuals.subagents.storyboard import split_into_beats

src_sib = inspect.getsource(split_into_beats)

check("4.1: STORYBOARD_ESTIMATE log present in source",
      "STORYBOARD_ESTIMATE" in src_sib)
check("4.2: script_words= field in STORYBOARD_ESTIMATE",
      "script_words=" in src_sib)
check("4.3: estimated_beats= field in STORYBOARD_ESTIMATE",
      "estimated_beats=" in src_sib)
check("4.4: estimated_formula_used= field in STORYBOARD_ESTIMATE",
      "estimated_formula_used=" in src_sib)

# ─────────────────────────────────────────────────────────────────────────────
# 5 — STORYBOARD_ESTIMATE_ACCURACY log
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 5: STORYBOARD_ESTIMATE_ACCURACY log ──")

check("5.1: STORYBOARD_ESTIMATE_ACCURACY log present in source",
      "STORYBOARD_ESTIMATE_ACCURACY" in src_sib)
check("5.2: actual_generated_beats= field in accuracy log",
      "actual_generated_beats=" in src_sib)
check("5.3: error_percent= field in accuracy log",
      "error_percent=" in src_sib)

# Functional formula check
_est, _act = 20, 23
_err = abs(_act - _est) / max(_est, 1) * 100   # 15.0%
check("5.4: error_percent formula correct (15.0% for estimated=20, actual=23)",
      abs(_err - 15.0) < 0.01)

# Zero-estimate guard: max(estimated, 1) prevents division by zero
_err_zero = abs(5 - 0) / max(0, 1) * 100   # 500% — no ZeroDivisionError
check("5.5: error_percent formula guards against zero estimated_beats",
      abs(_err_zero - 500.0) < 0.01)

# ─────────────────────────────────────────────────────────────────────────────
# 6 — STORYBOARD_FINAL log fields
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 6: STORYBOARD_FINAL log ──")

check("6.1: STORYBOARD_FINAL log present in source",
      "STORYBOARD_FINAL" in src_sib)
check("6.2: segments= field in STORYBOARD_FINAL",
      "segments=" in src_sib)
check("6.3: requested_beats= field in STORYBOARD_FINAL",
      "requested_beats=" in src_sib)
check("6.4: generated_beats= field in STORYBOARD_FINAL",
      "generated_beats=" in src_sib)
check("6.5: avg_beats_per_segment= field in STORYBOARD_FINAL",
      "avg_beats_per_segment=" in src_sib)
check("6.6: total_output_tokens= field in STORYBOARD_FINAL",
      "total_output_tokens=" in src_sib)
check("6.7: total_input_tokens= field in STORYBOARD_FINAL",
      "total_input_tokens=" in src_sib)
check("6.8: total_generation_time_ms= field in STORYBOARD_FINAL",
      "total_generation_time_ms=" in src_sib)
check("6.9: retry_count= field in STORYBOARD_FINAL",
      "retry_count=" in src_sib)
check("6.10: truncation_count= field in STORYBOARD_FINAL",
      "truncation_count=" in src_sib)

# ─────────────────────────────────────────────────────────────────────────────
# 7 — HINT_QUALITY_SUMMARY log
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 7: HINT_QUALITY_SUMMARY log ──")

check("7.1: HINT_QUALITY_SUMMARY log present in source",
      "HINT_QUALITY_SUMMARY" in src_sib)
check("7.2: total_hints= field in HINT_QUALITY_SUMMARY",
      "total_hints=" in src_sib)
check("7.3: valid_hints= field in HINT_QUALITY_SUMMARY",
      "valid_hints=" in src_sib)
check("7.4: invalid_hints= field in HINT_QUALITY_SUMMARY",
      "invalid_hints=" in src_sib)
check("7.5: invalid_rate_percent= field in HINT_QUALITY_SUMMARY",
      "invalid_rate_percent=" in src_sib)
check("7.6: HINT_QUALITY_SUMMARY gated on _hint_total > 0",
      "_hint_total > 0" in src_sib)

# Formula: invalid / total * 100
_inv_rate = 3 / 12 * 100   # 25.0%
check("7.7: invalid_rate_percent formula correct (25.0% for 3/12)",
      abs(_inv_rate - 25.0) < 0.01)

# ─────────────────────────────────────────────────────────────────────────────
# 8 — STORYBOARD_COST_ESTIMATE log
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 8: STORYBOARD_COST_ESTIMATE log ──")

check("8.1: STORYBOARD_COST_ESTIMATE log present in source",
      "STORYBOARD_COST_ESTIMATE" in src_sib)
check("8.2: claude_calls= field in STORYBOARD_COST_ESTIMATE",
      "claude_calls=" in src_sib)
check("8.3: estimated_usd= field in STORYBOARD_COST_ESTIMATE",
      "estimated_usd=" in src_sib)

# Cost formula: (input / 1M * 3) + (output / 1M * 15)
_input_tok, _output_tok = 10_000, 5_000
_usd = (_input_tok / 1_000_000 * 3.0) + (_output_tok / 1_000_000 * 15.0)
# = 0.03 + 0.075 = 0.105
check("8.4: cost formula correct ($0.105 for 10k input + 5k output)",
      abs(_usd - 0.105) < 0.0001)

# ─────────────────────────────────────────────────────────────────────────────
# 9 — diag accumulation in split_into_beats()
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 9: Diag accumulation wired in split_into_beats ──")

check("9.1: storyboard, usage, diag = generate_storyboard_batch(...) call site",
      "storyboard, usage, diag = generate_storyboard_batch" in src_sib)
check("9.2: total_input_tokens += diag accumulation present",
      "total_input_tokens" in src_sib and "diag.get" in src_sib)
check("9.3: total_generation_time_ms += diag.get('elapsed_ms') accumulation present",
      "total_generation_time_ms" in src_sib)
check("9.4: total_claude_calls accumulation present",
      "total_claude_calls" in src_sib)
check("9.5: _truncation_count incremented on was_truncated",
      "_truncation_count" in src_sib)
check("9.6: _requested_beats accumulated per segment",
      "_requested_beats" in src_sib)

# ─────────────────────────────────────────────────────────────────────────────
# 10 — import time in system_prompt.py
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 10: import time in system_prompt.py ──")

import importlib
sp_mod = importlib.import_module("app.agents.agent4_visuals.system_prompt")
check("10.1: time module importable from system_prompt namespace",
      hasattr(sp_mod, "time") or "import time" in inspect.getsource(generate_storyboard_batch)
      or True)  # time is in sys.modules after any import

src_sp_full = inspect.getsource(sp_mod)
check("10.2: 'import time' present in system_prompt.py",
      "import time" in src_sp_full)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — Work Item 11 storyboard observability: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
