"""Smoke test for hybrid media strategy.

Validates:
1. validate_storyboard() is importable
2. dark_contrast cover frame → MAJOR (cover_frame_dark_contrast)
3. text_card cover frame → MAJOR (cover_frame_text_card)
4. First two beats both text_card → MAJOR (opening_text_card_pair)
5. Forbidden flux word in flux_generated beat → MAJOR (forbidden_flux_word)
6. "dark" alone (no co-occurrence) does NOT trigger forbidden_flux_word
7. Clean fixture → empty issue list
8. Stock strategy override constant (_STOCK_STRATEGIES) is present in storyboard.py
9. _build_beat_section returns media_strategy and text_card_style keys
10. VideoSection model has media_strategy and text_card_style columns
11. remotion_builder._section_for_remotion returns text_card_style key
12. Alembic migration file for hybrid media migration exists
13. STORYBOARD_SCHEMA_VERSION is "6.0"

No API calls. No DB connections.

Run: python scripts/smoke_hybrid_media_strategy.py
"""

import ast
import importlib
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if not condition:
        failures.append(label)


# ── 1. Import ─────────────────────────────────────────────────────────────────

print("\n── Import ──────────────────────────────────────────────────────────────")
try:
    from app.agents.agent4_visuals.subagents.storyboard_validator import (
        validate_storyboard,
        StoryboardIssue,
        FORBIDDEN_FLUX_WORDS,
        _DARK_REQUIRES_COOCCURRENCE,
    )
    check("validate_storyboard importable", True)
except Exception as exc:
    check(f"validate_storyboard importable ({exc})", False)
    print(f"\n{FAIL}: fatal import error — cannot continue.\n")
    sys.exit(1)

# ── 2. MAJOR: dark_contrast cover frame ───────────────────────────────────────

print("\n── MAJOR checks ────────────────────────────────────────────────────────")

fixture_dark_contrast = [
    {
        "beat_order": 0,
        "color_grade": "dark_contrast",
        "media_strategy": "flux_generated",
        "flux_prompt": "A lit room with warm incandescent lamps, photorealistic",
        "environment": "indoor_domestic",
        "beat_intensity": "medium",
    },
    {
        "beat_order": 1,
        "color_grade": "neutral",
        "media_strategy": "flux_generated",
        "flux_prompt": "An office desk with scattered papers, morning light, photorealistic",
        "environment": "indoor_office",
        "beat_intensity": "medium",
    },
]
issues = validate_storyboard(fixture_dark_contrast)
major_checks = [i["check"] for i in issues if i["severity"] == "MAJOR"]
check("dark_contrast cover frame → MAJOR cover_frame_dark_contrast", "cover_frame_dark_contrast" in major_checks)

# ── 3. MAJOR: text_card cover frame ──────────────────────────────────────────

fixture_tc_cover = [
    {
        "beat_order": 0,
        "color_grade": "neutral",
        "media_strategy": "remotion_text_card",
        "flux_prompt": "",
        "environment": "other",
        "beat_intensity": "medium",
    },
    {
        "beat_order": 1,
        "color_grade": "neutral",
        "media_strategy": "flux_generated",
        "flux_prompt": "A city street corner at dawn, photorealistic",
        "environment": "urban_street",
        "beat_intensity": "medium",
    },
]
issues = validate_storyboard(fixture_tc_cover)
major_checks = [i["check"] for i in issues if i["severity"] == "MAJOR"]
check("text_card cover frame → MAJOR cover_frame_text_card", "cover_frame_text_card" in major_checks)

# ── 4. MAJOR: first two beats both text_card ──────────────────────────────────

fixture_pair_tc = [
    {
        "beat_order": 0,
        "color_grade": "neutral",
        "media_strategy": "remotion_text_card",
        "flux_prompt": "",
        "environment": "other",
        "beat_intensity": "medium",
    },
    {
        "beat_order": 1,
        "color_grade": "neutral",
        "media_strategy": "remotion_text_card",
        "flux_prompt": "",
        "environment": "other",
        "beat_intensity": "medium",
    },
    {
        "beat_order": 2,
        "color_grade": "neutral",
        "media_strategy": "flux_generated",
        "flux_prompt": "City street at dawn, photorealistic",
        "environment": "urban_street",
        "beat_intensity": "medium",
    },
]
issues = validate_storyboard(fixture_pair_tc)
major_checks = [i["check"] for i in issues if i["severity"] == "MAJOR"]
check("first two beats text_card → MAJOR opening_text_card_pair", "opening_text_card_pair" in major_checks)

# ── 5. MAJOR: forbidden flux word ─────────────────────────────────────────────

fixture_forbidden = [
    {
        "beat_order": 0,
        "color_grade": "neutral",
        "media_strategy": "flux_generated",
        "flux_prompt": "An atmospheric corridor with mysterious shadows, photorealistic",
        "environment": "corridor_interior",
        "beat_intensity": "medium",
    },
]
issues = validate_storyboard(fixture_forbidden)
major_checks = [i["check"] for i in issues if i["severity"] == "MAJOR"]
check("forbidden flux word → MAJOR forbidden_flux_word", "forbidden_flux_word" in major_checks)

# ── 6. "dark room" alone does NOT trigger forbidden_flux_word ─────────────────

fixture_dark_room = [
    {
        "beat_order": 0,
        "color_grade": "neutral",
        "media_strategy": "flux_generated",
        "flux_prompt": "A dark wooden door in a corridor, close-up, photorealistic, sharp focus",
        "environment": "corridor_interior",
        "beat_intensity": "medium",
    },
]
issues = validate_storyboard(fixture_dark_room)
major_checks = [i["check"] for i in issues if i["severity"] == "MAJOR"]
has_forbidden = "forbidden_flux_word" in major_checks
check('"dark room" alone does NOT trigger forbidden_flux_word', not has_forbidden)

# ── 7. Clean fixture → empty ──────────────────────────────────────────────────

fixture_clean = [
    {
        "beat_order": i,
        "color_grade": "neutral",
        "media_strategy": "flux_generated",
        "flux_prompt": f"A well-lit city street with parked cars, wide shot, photorealistic, beat {i}",
        "environment": "urban_street" if i % 3 != 0 else "indoor_office",
        "beat_intensity": "medium",
    }
    for i in range(4)
]
issues = validate_storyboard(fixture_clean)
majors = [i for i in issues if i["severity"] == "MAJOR"]
check("clean fixture → no MAJOR issues", len(majors) == 0)

# ── 8. _STOCK_STRATEGIES present in storyboard.py ────────────────────────────

print("\n── Python-side invariants ──────────────────────────────────────────────")

try:
    from app.agents.agent4_visuals.subagents.storyboard import _STOCK_STRATEGIES  # type: ignore[attr-defined]
    check("_STOCK_STRATEGIES present in storyboard.py", True)
    check("_STOCK_STRATEGIES contains stock_video and stock_image",
          "stock_video" in _STOCK_STRATEGIES and "stock_image" in _STOCK_STRATEGIES)
except ImportError as exc:
    check(f"_STOCK_STRATEGIES importable ({exc})", False)
    check("_STOCK_STRATEGIES contains stock_video and stock_image", False)

# ── 9. _build_beat_section returns media_strategy and text_card_style ─────────

try:
    from app.agents.agent4_visuals.subagents.storyboard import _build_beat_section  # type: ignore[attr-defined]
    import inspect as _inspect
    src = _inspect.getsource(_build_beat_section)
    check("_build_beat_section returns media_strategy", '"media_strategy"' in src)
    check("_build_beat_section returns text_card_style", '"text_card_style"' in src)
except Exception as exc:
    check(f"_build_beat_section source accessible ({exc})", False)
    check("_build_beat_section returns text_card_style", False)

# ── 10. VideoSection model has new columns ─────────────────────────────────────

try:
    from app.models.video_sections import VideoSection
    cols = [c.key for c in VideoSection.__table__.columns]
    check("VideoSection.media_strategy column present", "media_strategy" in cols)
    check("VideoSection.text_card_style column present", "text_card_style" in cols)
except Exception as exc:
    check(f"VideoSection importable ({exc})", False)
    check("VideoSection.text_card_style column present", False)

# ── 11. remotion_builder._section_for_remotion returns text_card_style ─────────

try:
    import app.agents.agent5_render.services.remotion_builder as rb
    src = inspect.getsource(rb._section_for_remotion)  # type: ignore[attr-defined]
    check("_section_for_remotion returns text_card_style", '"text_card_style"' in src)
except Exception as exc:
    check(f"remotion_builder._section_for_remotion source ({exc})", False)

# ── 12. Alembic migration file for hybrid media migration exists ─────────────────────────────

print("\n── DB migration ────────────────────────────────────────────────────────")

alembic_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alembic", "versions")
migration_files = os.listdir(alembic_dir)
has_migration = any("media_strategy" in f for f in migration_files)
check("Alembic migration for media_strategy exists", has_migration)

# ── 13. STORYBOARD_SCHEMA_VERSION is "6.0" ───────────────────────────────────

print("\n── Schema version ──────────────────────────────────────────────────────")

try:
    from app.agents.agent4_visuals.system_prompt import STORYBOARD_SCHEMA_VERSION
    check('STORYBOARD_SCHEMA_VERSION == "6.0"', STORYBOARD_SCHEMA_VERSION == "6.0")
except Exception as exc:
    check(f"STORYBOARD_SCHEMA_VERSION importable ({exc})", False)

# ── Summary ───────────────────────────────────────────────────────────────────

print()
if failures:
    print(f"SMOKE FAIL — {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  • {f}")
    sys.exit(1)
else:
    print("SMOKE PASS")
