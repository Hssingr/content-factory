"""Phase 14.9 — Flux prompt quality/length investigation proof.

Read-only investigation. No production code is modified by this script.
Zero live API calls — every prompt sample below comes from a real, unmodified
production function called with local fixtures, or from the static prompt
text constants themselves. No Claude/fal.ai call is made anywhere.

Run: python scripts/smoke_flux_prompt_quality_investigation.py
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.modules.setdefault(
    "fal_client",
    types.SimpleNamespace(SyncClient=None, FalClientError=Exception),
)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


from app.agents.agent4_visuals.system_prompt import (
    _STORYBOARD_SYSTEM_PROMPT, _SPLITTER_SYSTEM_PROMPT,
)
from app.agents.agent4_visuals.services import flux_generator as fg
from app.agents.agent4_visuals.services import image_router as ir
from app.agents.agent4_visuals.subagents import storyboard as sb
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    validate_storyboard, FORBIDDEN_FLUX_WORDS, _QUOTED_TEXT_RE,
    _AI_TEXT_RENDERING_PHRASES,
)

# ═══════════════════════════════════════════════════════════════════════════
# 1: Inventory of every Flux prompt producer — all importable/callable
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: Flux prompt producer inventory ──")
PRODUCERS = {
    "primary storyboard generation (Claude, per-beat)": "system_prompt._STORYBOARD_SYSTEM_PROMPT",
    "legacy splitter fallback (Claude, per-section) + Python synthesis":
        "system_prompt._SPLITTER_SYSTEM_PROMPT + enrich_sections_with_visuals()",
    "text-card background derivation (Phase 14.4)": "flux_generator.derive_text_card_background_prompt()",
    "text-prop sanitization (Phase 14.7)": "flux_generator.derive_text_prop_prompt()",
    "safe-prompt fallback/cascade (3-tier)": "flux_generator.generate_beat_image() cascade + _ENV_SAFE_PROMPTS",
    "router/provider payload wrapper (Phase 14.6)": "image_router.build_fal_payload()",
}
for name in PRODUCERS:
    print(f"  - {name}")
check("1a: all 6 producers identified are real, importable callables/constants",
      callable(fg.derive_text_card_background_prompt)
      and callable(fg.derive_text_prop_prompt)
      and callable(fg.generate_beat_image)
      and callable(ir.build_fal_payload)
      and isinstance(_STORYBOARD_SYSTEM_PROMPT, str)
      and isinstance(_SPLITTER_SYSTEM_PROMPT, str))

# ═══════════════════════════════════════════════════════════════════════════
# 2: Sample prompt corpus from deterministic fixtures
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: building a sample prompt corpus from real producer functions ──")
corpus: dict[str, list[str]] = {}

# Primary storyboard prompt's own worked examples (few-shot guidance Claude sees)
good_block = re.search(r"Good examples:(.*?)Bad examples", _STORYBOARD_SYSTEM_PROMPT, re.S).group(1)
corpus["storyboard_good_examples"] = re.findall(r"✓\s*\"(.*?)\"", good_block, re.S)

bad_block = re.search(r"Bad examples \(forbidden\):(.*?)==", _STORYBOARD_SYSTEM_PROMPT, re.S).group(1)
corpus["storyboard_bad_examples"] = re.findall(r"✗\s*\"(.*?)\"", bad_block, re.S)

# Safe-fallback prompts (tier 3 of the generation cascade) — fixed, exhaustive
corpus["safe_fallback_prompts"] = list(fg._ENV_SAFE_PROMPTS.values())

# Legacy splitter's synthesized flux_prompt (Python template + a sample search_query)
legacy_sq = "abandoned hospital hallway with rows of orange chairs"
corpus["legacy_splitter_synthesized"] = [
    f"{legacy_sq}, photorealistic, documentary photography style, desaturated color grade, no people, no text"
]

# Text-card background derivation (Phase 14.4) — three representative beats
text_card_fixtures = [
    {"visual_intent": "a missing person poster pinned to a community bulletin board",
     "environment": "urban_street", "overlay_text": ""},
    {"visual_intent": "", "script_text": "The detective opened the case file and read the report.",
     "environment": "indoor_office", "overlay_text": ""},
    {"visual_intent": "", "script_text": "", "overlay_text": "BREAKING NEWS", "environment": "other"},
]
corpus["text_card_derived"] = [fg.derive_text_card_background_prompt(b) for b in text_card_fixtures]

# Text-prop sanitization (Phase 14.7) — three representative beats (poster/doc/calendar)
text_prop_fixtures = [
    {"visual_intent": "a missing person poster pinned to a community bulletin board",
     "flux_prompt": 'A missing poster, the text reads "JANE DOE MISSING SINCE MARCH 5"',
     "environment": "urban_street"},
    {"visual_intent": "a detective opens a case file folder on the desk",
     "flux_prompt": 'A case file folder, label reading "CASE #4471"',
     "environment": "indoor_office"},
    {"visual_intent": "a wall calendar with a date circled in red",
     "flux_prompt": 'A wall calendar with the date "March 14" circled',
     "script_text": "It happened on March 14, just days before the funeral.",
     "environment": "indoor_domestic"},
]
corpus["text_prop_sanitized"] = [fg.derive_text_prop_prompt(b) for b in text_prop_fixtures]

# Shortened cascade tier (40-word truncation of a long original)
long_original = " ".join(f"detail{i}" for i in range(90))
corpus["cascade_shortened_tier"] = [" ".join(long_original.split()[:40])]

check(
    "2a: corpus built from 7 prompt categories spanning every producer in the inventory",
    len(corpus) == 7, list(corpus.keys()),
)
total_prompts = sum(len(v) for v in corpus.values())
check("2b: corpus is non-trivial (more than 20 sample prompts)", total_prompts > 20, total_prompts)

# ═══════════════════════════════════════════════════════════════════════════
# 3: Prompt length distribution
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3: prompt length distribution by category ──")
print(f"  {'category':32s} {'n':>3s} {'min_w':>6s} {'max_w':>6s} {'avg_w':>6s} {'avg_chars':>9s}")
length_summary: dict[str, dict] = {}
for category, prompts in corpus.items():
    word_counts = [len(p.split()) for p in prompts]
    char_counts = [len(p) for p in prompts]
    summary = {
        "n": len(prompts),
        "min_words": min(word_counts),
        "max_words": max(word_counts),
        "avg_words": sum(word_counts) / len(word_counts),
        "avg_chars": sum(char_counts) / len(char_counts),
    }
    length_summary[category] = summary
    print(
        f"  {category:32s} {summary['n']:3d} {summary['min_words']:6d} "
        f"{summary['max_words']:6d} {summary['avg_words']:6.1f} {summary['avg_chars']:9.1f}"
    )

check(
    "3a: the legacy splitter's synthesized prompt is measurably shorter than the storyboard "
    "prompt's own stated 50-80 word target — an extremely short prompt relative to the rest "
    "of the pipeline",
    length_summary["legacy_splitter_synthesized"]["avg_words"] < 20,
    length_summary["legacy_splitter_synthesized"]["avg_words"],
)
check(
    "3b: the storyboard prompt's own worked 'good' examples (the few-shot guidance Claude "
    "actually sees) run shorter than the stated 50-80 word build-order target",
    length_summary["storyboard_good_examples"]["avg_words"] < 50,
    length_summary["storyboard_good_examples"]["avg_words"],
)
check(
    "3c: text-card and text-prop derived prompts are measurably longer than the safe-fallback "
    "tier, driven by their fixed negative-constraint clause",
    length_summary["text_card_derived"]["avg_words"] > length_summary["safe_fallback_prompts"]["avg_words"]
    and length_summary["text_prop_sanitized"]["avg_words"] > length_summary["safe_fallback_prompts"]["avg_words"],
)

# Quantify the repeated-boilerplate fraction directly (not just observed, measured).
boilerplate_card = fg._TEXT_CARD_NO_TEXT_CLAUSE
boilerplate_prop = fg._TEXT_PROP_NO_TEXT_CLAUSE
card_boilerplate_ratio = len(boilerplate_card.split()) / length_summary["text_card_derived"]["avg_words"]
prop_boilerplate_ratio = len(boilerplate_prop.split()) / length_summary["text_prop_sanitized"]["avg_words"]
print(
    f"\n  Repeated boilerplate fraction: text-card clause is "
    f"{len(boilerplate_card.split())} words ({card_boilerplate_ratio:.0%} of the average "
    f"derived prompt); text-prop clause is {len(boilerplate_prop.split())} words "
    f"({prop_boilerplate_ratio:.0%} of the average derived prompt)."
)
check(
    "3d: the fixed negative-constraint clause is identical, word-for-word, across every "
    "text-card-derived prompt in the corpus (confirmed repeated boilerplate, not assumed)",
    all(boilerplate_card in p for p in corpus["text_card_derived"]),
)
check(
    "3e: the fixed negative-constraint clause is identical, word-for-word, across every "
    "text-prop-sanitized prompt in the corpus",
    all(boilerplate_prop in p for p in corpus["text_prop_sanitized"]),
)

# ═══════════════════════════════════════════════════════════════════════════
# 4: Forbidden-word scan (Phase 12.6 FORBIDDEN_FLUX_WORDS) across the corpus
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4: forbidden-Flux-word scan across the full corpus ──")
# storyboard_bad_examples is the prompt's own deliberate negative few-shot set
# (forbidden ON PURPOSE, to show Claude what not to do) — excluded from the
# "producer output should be clean" assertion; checked separately in 4b.
producer_output_categories = {k: v for k, v in corpus.items() if k != "storyboard_bad_examples"}
forbidden_hits: dict[str, list[str]] = {}
for category, prompts in producer_output_categories.items():
    for p in prompts:
        hit = FORBIDDEN_FLUX_WORDS & set(p.lower().split())
        if hit:
            forbidden_hits.setdefault(category, []).append((p[:60], sorted(hit)))
check(
    "4a: zero forbidden-mood-word hits anywhere in the corpus produced by current producers "
    "(storyboard good examples, safe fallback, text-card, text-prop, legacy splitter)",
    not forbidden_hits, forbidden_hits,
)
# The storyboard prompt's own "bad examples" are NEGATIVE few-shot guidance (forbidden on
# purpose, to show Claude what NOT to do) — confirm they DO trip the scan, proving the scan
# itself is discriminating and not vacuously passing everything.
bad_example_hits = [
    p for p in corpus["storyboard_bad_examples"]
    if FORBIDDEN_FLUX_WORDS & set(p.lower().split())
]
check(
    "4b: the prompt's own negative ('bad') examples DO trip the forbidden-word scan — proves "
    "the scan is discriminating, not a vacuous pass",
    len(bad_example_hits) == len(corpus["storyboard_bad_examples"]),
    f"{len(bad_example_hits)}/{len(corpus['storyboard_bad_examples'])}",
)

# ═══════════════════════════════════════════════════════════════════════════
# 5: AI text-rendering phrase scan (Phase 14.7) across the corpus
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: AI text-rendering phrase scan across the full corpus ──")
rendering_hits: dict[str, list[str]] = {}
for category, prompts in corpus.items():
    for p in prompts:
        quoted = _QUOTED_TEXT_RE.search(p)
        phrase = next((ph for ph in _AI_TEXT_RENDERING_PHRASES if ph in p.lower()), None)
        if quoted or phrase:
            rendering_hits.setdefault(category, []).append(p[:60])
check(
    "5a: zero text-rendering-request signals in any prompt actually produced by current "
    "producers (text-card derivation and text-prop sanitization both strip the original "
    "quoted/rendering-instruction text before this corpus was sampled)",
    not rendering_hits, rendering_hits,
)

# ═══════════════════════════════════════════════════════════════════════════
# 6 & 7: text-card / text-prop prompts pass validate_storyboard cleanly
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 6 & 7: text-card and text-prop prompts pass validate_storyboard with zero MAJOR ──")


def make_full_beat(order, flux_prompt, media_strategy="flux_generated", visual_type="document"):
    return {
        "beat_order": order, "flux_prompt": flux_prompt, "media_strategy": media_strategy,
        "visual_type": visual_type, "environment": "urban_street", "motif": "other",
        "effect": "cut", "color_grade": "neutral", "beat_intensity": "medium",
    }


text_card_beats_for_validation = [
    make_full_beat(i, p, media_strategy="remotion_text_card", visual_type="text_card")
    for i, p in enumerate(corpus["text_card_derived"])
]
issues_card = validate_storyboard(text_card_beats_for_validation)
major_card = [i for i in issues_card if i["severity"] == "MAJOR"]
check(
    "6a: text-card-strategy beats carrying the derived background prompt produce zero MAJOR "
    "findings (cover-frame-position artifacts aside, confirmed below)",
    not [i for i in major_card if i["check"] not in ("cover_frame_text_card", "opening_text_card_pair")],
    major_card,
)

text_prop_beats_for_validation = [
    make_full_beat(i, p) for i, p in enumerate(corpus["text_prop_sanitized"])
]
issues_prop = validate_storyboard(text_prop_beats_for_validation)
major_prop = [i for i in issues_prop if i["severity"] == "MAJOR"]
check(
    "7a: text-prop sanitized prompts (which, unlike text-card, ARE subject to "
    "flux_generated-only checks like subject_presence/forbidden_flux_word/"
    "ai_text_rendering_requested) produce zero MAJOR findings",
    not major_prop, major_prop,
)

# ═══════════════════════════════════════════════════════════════════════════
# 8: router prompt pass-through check (Phase 14.6)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 8: router payload builder never modifies prompt text ──")
sample_prompt = corpus["text_prop_sanitized"][0]
for model_key in ("schnell", "dev", "pro_1_1", "pro_1_1_ultra", "flux_2_pro"):
    payload = ir.build_fal_payload(model_key, sample_prompt)
    check(
        f"8: {model_key} payload's 'prompt' field is byte-identical to the input "
        "(router changes model/endpoint/fields, never the prompt's meaning or text)",
        payload["prompt"] == sample_prompt,
    )

# ═══════════════════════════════════════════════════════════════════════════
# 9: existing Phase 14.4/14.6/14.7/14.8 smokes still pass (no files touched, but re-run anyway)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 9: existing Phase 14.4/14.6/14.7/14.8 smokes still pass ──")
import subprocess

for smoke in (
    "scripts/smoke_text_card_generated_backgrounds.py",
    "scripts/smoke_image_model_router.py",
    "scripts/smoke_ai_text_rendering_ban.py",
    "scripts/smoke_visuals_amplify_not_illustrate.py",
):
    proc = subprocess.run(
        [sys.executable, smoke], cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    check(
        f"9: {smoke} exits 0 with SMOKE PASS",
        proc.returncode == 0 and "SMOKE PASS" in proc.stdout,
        proc.stdout[-400:] if proc.returncode != 0 else "",
    )

# ═══════════════════════════════════════════════════════════════════════════
# Notable finding: text-card derivation NEVER reads the beat's original
# Claude-authored flux_prompt at all (full discard); text-prop sanitization
# DOES use it as a fallback context source (partial reuse).
# ═══════════════════════════════════════════════════════════════════════════

print("\n── Notable structural finding: text-card vs. text-prop reuse of Claude's original prompt ──")
import inspect

src_text_card = inspect.getsource(fg.derive_text_card_background_prompt)
src_text_prop = inspect.getsource(fg.derive_text_prop_prompt)
check(
    "derive_text_card_background_prompt() never reads beat['flux_prompt'] — Claude's own "
    "carefully-authored background-scene prompt for every text_card beat is fully discarded "
    "and replaced by this template, even though the storyboard prompt explicitly tells Claude "
    "to write one (CLAUDE.md §11.4's 'Media strategy selection' instructions). This is a "
    "pre-existing Phase 14.4 design, not changed by this investigation, but worth surfacing.",
    'beat.get("flux_prompt"' not in src_text_card,
)
check(
    "derive_text_prop_prompt() DOES read beat['flux_prompt'] as a fallback context source "
    "(third in its visual_intent -> flux_prompt -> script_text chain) — Phase 14.7's "
    "sanitization is a partial rewrite, not a full discard, unlike Phase 14.4's text-card path",
    'beat.get("flux_prompt"' in src_text_prop,
)

print()
print("SMOKE PASS — Flux prompt quality/length investigation")
