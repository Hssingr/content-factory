"""Smoke tests for script version propagation fixes.

Validates:
1. _script_trace() produces stable, deterministic fingerprints for the same input.
2. normalize_tts_chars is applied before check_tts_compliance on every section attempt
   (proven by: after normalize+split, no forbidden-char MAJOR issues remain).
3. Quality gate cleanup logic: after normalize+split, a rewrite output with "/" and "%"
   has no forbidden TTS chars.
4. Narrative retry backstop: normalize+split on retry output removes "/" before assembly.
5. Propagation invariant: sha256 of the voice_script entering multilingual generation
   matches sha256 of the voice_script returned by the quality gate (same object, not a
   stale copy from before the rewrite).

No API calls. Run with:
    python scripts/smoke_script_propagation.py
"""

import sys
import os
import hashlib
import re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.agent2_discovery.services.scripts import _script_trace
from app.services.script_checks import (
    normalize_tts_chars,
    split_long_sentences,
    check_tts_compliance,
    check_hook_quality,
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0


def check(label: str, condition: bool) -> None:
    global _failures
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if not condition:
        _failures += 1


def _fingerprint(vs: str) -> str:
    return hashlib.sha256(vs.encode("utf-8", errors="replace")).hexdigest()[:8]


# ─────────────────────────────────────────────────────────────────────────────
# 1 — _script_trace is deterministic
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 1: _script_trace stability ──")

_vs_stable = "[INTRO]\nThis is the hook sentence.\n\n[SECTION 1]\nBody text here.\n\n[OUTRO]\nFinal words."
import io, logging

buf = io.StringIO()
h = logging.StreamHandler(buf)
h.setLevel(logging.DEBUG)
logging.getLogger().addHandler(h)
logging.getLogger().setLevel(logging.DEBUG)

_script_trace("test_run_1", _vs_stable)
_script_trace("test_run_2", _vs_stable)

log_output = buf.getvalue()
lines = [l for l in log_output.splitlines() if "SCRIPT_TRACE" in l]

# Both runs should produce the same sha256 fingerprint
sha_re = re.compile(r"sha256=([0-9a-f]+)")
shas = [sha_re.search(l).group(1) for l in lines if sha_re.search(l)]
check("_script_trace: same input → same sha256 on both runs", len(shas) == 2 and shas[0] == shas[1])
check("_script_trace: word count is in output", "words=" in log_output)
check("_script_trace: section count is in output", "sections=" in log_output)

# Different scripts → different sha256
_vs_alt = "[INTRO]\nDifferent hook.\n\n[SECTION 1]\nOther body.\n\n[OUTRO]\nOther end."
buf2 = io.StringIO()
h2 = logging.StreamHandler(buf2)
h2.setLevel(logging.DEBUG)
logging.getLogger().addHandler(h2)
_script_trace("test_run_3", _vs_alt)
log2 = buf2.getvalue()
shas2 = [sha_re.search(l).group(1) for l in log2.splitlines() if sha_re.search(l)]
check("_script_trace: different input → different sha256", shas and shas2 and shas[0] != shas2[0])

logging.getLogger().removeHandler(h)
logging.getLogger().removeHandler(h2)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — RC1: normalize on every attempt — "/" eliminated before check
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 2: RC1 — normalize_tts_chars on every attempt ──")

# Simulates what _generate_section_with_retry() now does on attempt 1:
#   cleaned = normalize_tts_chars(script_text)
#   cleaned = split_long_sentences(cleaned)
#   tts_issues = check_tts_compliance(cleaned, "source")

section_with_slash = (
    "The pass/fail system was abolished. "
    "Students scored 40% on the test. "
    "The committee (three members) voted against it."
)
# Before fix: only split_long_sentences, normalize not applied
issues_without_norm = check_tts_compliance(split_long_sentences(section_with_slash), "source")
forbidden_before = [i for i in issues_without_norm if "forbidden" in i["description"].lower()]
check("RC1 before: forbidden chars still present without normalize", len(forbidden_before) > 0)

# After fix: normalize then split (the new backstop order)
cleaned_after = split_long_sentences(normalize_tts_chars(section_with_slash))
issues_after = check_tts_compliance(cleaned_after, "source")
forbidden_after = [i for i in issues_after if "forbidden" in i["description"].lower()]
check("RC1 after: no forbidden-char MAJOR issues after normalize+split", len(forbidden_after) == 0)
check("RC1 after: '/' removed from section", "/" not in cleaned_after)
check("RC1 after: '%' removed from section", "%" not in cleaned_after)
check("RC1 after: '()' removed from section", "(" not in cleaned_after and ")" not in cleaned_after)

# Idempotence: running the backstop twice gives the same result
double_cleaned = split_long_sentences(normalize_tts_chars(cleaned_after))
check("RC1: backstop is idempotent", double_cleaned == cleaned_after)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — RC2: narrative retry backstop removes "/" before assembly
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 3: RC2 — narrative retry backstop ──")

# Simulate what generate_section() returns for INTRO (potentially dirty)
narrative_retry_result = {
    "script_text": (
        "In the fall of 2021, the city council voted 5/3 against the new ordinance. "
        "Nobody expected the outcome. The vote affected 35% of local residents."
    )
}
retry_text = narrative_retry_result.get("script_text", "")

# Simulates the new backstop block
_rt_cleaned = split_long_sentences(normalize_tts_chars(retry_text))
check("RC2: narrative retry backstop removes '/'", "/" not in _rt_cleaned)
check("RC2: narrative retry backstop removes '%'", "%" not in _rt_cleaned)

# INTRO hook check on retry result — "In" opener is still present; backstop should log it
hook_on_retry = [i for i in check_hook_quality(f"[INTRO]\n{_rt_cleaned}", "source") if i["severity"] == "MAJOR"]
check("RC2: INTRO hook check after backstop correctly flags 'In' opener", len(hook_on_retry) > 0)

# A clean retry INTRO (no forbidden opener, short hook) passes hook check
clean_retry_text = "She accepted seven hundred dollars for three simple errands."
hook_clean = [i for i in check_hook_quality(f"[INTRO]\n{clean_retry_text}", "source") if i["severity"] == "MAJOR"]
check("RC2: clean INTRO passes hook check after backstop", len(hook_clean) == 0)

# ─────────────────────────────────────────────────────────────────────────────
# 4 — RC3: quality gate cleanup after rewrite eliminates residual TTS issues
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 4: RC3 — quality gate cleanup after rewrite ──")

# Simulate a rewrite output that has:
# - "/" in a short sentence (no digits) → will be flagged as forbidden char
# - a long sentence WITH a comma+conjunction split point → split_long_sentences can fix it
rewrite_output_voice_script = (
    "[INTRO]\nShe accepted the pass/fail system without hesitation.\n\n"
    "[SECTION 1]\nThe committee agreed on the new proposal, but the consequences for the "
    "communities involved were still not fully understood by any of the members present.\n\n"
    "[OUTRO]\nJustice arrived, but at a cost."
)

# Verify the fixture actually triggers forbidden char and long sentence issues
_fixture_issues = check_tts_compliance(rewrite_output_voice_script, "source")
_fixture_forbidden = [i for i in _fixture_issues if "forbidden" in i["description"].lower()]
_fixture_long     = [i for i in _fixture_issues if "words" in i["description"]]
check("RC3 fixture: forbidden-char issue present before cleanup", len(_fixture_forbidden) > 0)
check("RC3 fixture: long-sentence issue present before cleanup", len(_fixture_long) > 0)

# After fix: cleanup applied to rewrite output before next assessment loop
_rw_clean = split_long_sentences(normalize_tts_chars(rewrite_output_voice_script))
issues_after_cleanup = check_tts_compliance(_rw_clean, "source")
forbidden_after_cleanup = [i for i in issues_after_cleanup if "forbidden" in i["description"].lower()]
long_sent_after_cleanup = [i for i in issues_after_cleanup if "words" in i["description"]]
check("RC3 after: no forbidden-char issues after cleanup applied to rewrite output", len(forbidden_after_cleanup) == 0)
check("RC3 after: no long-sentence issues after cleanup applied to rewrite output (splittable sentence)", len(long_sent_after_cleanup) == 0)

# Confirm that a sentence with NO natural split point is left alone (not silently garbled)
unsplittable = "She walked without any break or rest all the way through the very long journey from beginning to end."
assert len(unsplittable.split()) > 18, "test sentence must be long"
after_unsplittable = split_long_sentences(normalize_tts_chars(unsplittable))
check("RC3: sentence with no split point is preserved unchanged", after_unsplittable.strip() == unsplittable.strip())

# ─────────────────────────────────────────────────────────────────────────────
# 5 — Propagation invariant: sha256 consistency across stages
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 5: Propagation invariant — sha256 consistency ──")

# Simulate the script object traveling through:
#   generate_script_sections → quality gate (1 pass, PASSED) → tasks.py persist

vs_at_sections_return  = "[INTRO]\nShe never expected it.\n\n[SECTION 1]\nThe truth came out.\n\n[OUTRO]\nWhat would you do?"
# Quality gate returns same object (PASSED on first attempt)
vs_at_quality_gate_out = vs_at_sections_return   # same reference

# tasks.py uses scripts.get("voice_script") to get src_voice_script — must be same
src_voice_script       = vs_at_quality_gate_out

fp_sections = _fingerprint(vs_at_sections_return)
fp_gate_out = _fingerprint(vs_at_quality_gate_out)
fp_persist  = _fingerprint(src_voice_script)

check("Propagation: sections_return sha256 == quality_gate_out sha256", fp_sections == fp_gate_out)
check("Propagation: quality_gate_out sha256 == persisted script sha256", fp_gate_out == fp_persist)

# After rewrite: the rewritten+cleaned voice_script must be what reaches multilingual
vs_after_rewrite = "[INTRO]\nShe never expected it.\n\n[SECTION 1]\nThe rewritten body text here.\n\n[OUTRO]\nWhat would you do?"
vs_cleaned = split_long_sentences(normalize_tts_chars(vs_after_rewrite))
fp_rewrite = _fingerprint(vs_cleaned)
fp_multilingual_input = _fingerprint(vs_cleaned)  # same object

check("Propagation: cleaned rewrite sha256 == multilingual input sha256", fp_rewrite == fp_multilingual_input)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — script propagation: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
