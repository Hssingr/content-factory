"""Section Validator — validates and enriches sections for video production.

For each section Claude checks:
  - Duration fit (< 3s or > 60s = MAJOR)
  - Search query quality (too generic = MINOR)
  - Visual type correctness (b-roll / text_overlay / action)
  - Effect (slow_zoom / fade_in / cut / pan / zoom_out)
  - Color grade (desaturated / cold_blue / warm_amber / dark_contrast / neutral)

Python enforces:
  - Runway Decision: visual_source="runway" is only accepted when channel_config.runway_enabled=True
    AND section duration ≤ 5s AND the section is marked as critical (MAJOR without stock).
    At this stage (no media fetched yet) we only enforce runway_enabled + duration.
  - Max 3 correction rounds per batch of MAJOR sections.
  - After 3 rounds: best attempt wins, best_attempt_used=True.
  - Enum validation: invalid effect/color_grade/source → replaced with safe defaults.
"""

import copy
import logging

from app.agents.agent5_video.system_prompt import validate_sections_with_claude

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 3

# Allowed enum values — Python enforces, not just the prompt
_VALID_SOURCES = {"pexels", "unsplash", "runway"}
_VALID_EFFECTS  = {"slow_zoom", "fade_in", "cut", "pan", "zoom_out"}
_VALID_GRADES   = {"desaturated", "cold_blue", "warm_amber", "dark_contrast", "neutral"}

_DEFAULT_EFFECT = "slow_zoom"
_DEFAULT_GRADE  = "desaturated"
_DEFAULT_SOURCE = "pexels"


def validate_sections(
    sections: list[dict],
    channel_niche: str,
    channel_tone: str,
    runway_enabled: bool = False,
) -> list[dict]:
    """Validate and enrich all sections, running up to 3 correction rounds for MAJOR issues.

    Args:
        sections:       Sections from ``section_splitter`` (with search_query + suggested_visual).
        channel_niche:  Channel niche passed to Claude for context.
        channel_tone:   Channel tone for tone-match checking.
        runway_enabled: Whether Runway API is enabled for this channel
                        (channel_config.runway_enabled).

    Returns:
        Fully validated sections with: visual_source, search_query, effect, color_grade,
        validation_status, subagent_rounds, best_attempt_used, issues.
    """
    working = copy.deepcopy(sections)
    best_attempts: dict[int, dict] = {}   # order → best validated section so far

    for round_num in range(1, _MAX_ROUNDS + 1):
        # Only validate sections that still have outstanding MAJOR issues
        # (on round 1, validate everything)
        to_validate = [
            s for s in working
            if round_num == 1 or s.get("validation_status") == "MAJOR"
        ]
        if not to_validate:
            break

        logger.info("Section validator round %d/%d — %d section(s)", round_num, _MAX_ROUNDS, len(to_validate))

        try:
            results = validate_sections_with_claude(to_validate, channel_niche, channel_tone)
        except Exception as exc:
            logger.error("Section validation Claude call failed (round %d): %s", round_num, exc)
            break

        # Merge results back into working list
        results_by_order = {r.get("section_order"): r for r in results if "section_order" in r}

        for s in working:
            order = s["section_order"]
            r = results_by_order.get(order)
            if r is None:
                continue

            # ── Apply Claude's answer ────────────────────────────────────────
            s["validation_status"] = r.get("validation_status", "PASS")
            s["search_query"]      = r.get("search_query", s.get("search_query", ""))
            s["effect"]            = _coerce(r.get("effect"), _VALID_EFFECTS, _DEFAULT_EFFECT)
            s["color_grade"]       = _coerce(r.get("color_grade"), _VALID_GRADES, _DEFAULT_GRADE)
            s["visual_source"]     = _coerce(r.get("visual_source"), _VALID_SOURCES, _DEFAULT_SOURCE)
            s["issues"]            = r.get("issues", [])
            s["subagent_rounds"]   = round_num

            # ── Runway Decision (Python enforces, not Claude) ─────────────────
            if s["visual_source"] == "runway":
                if not _runway_allowed(s, runway_enabled):
                    logger.info(
                        "Section %d: runway rejected (enabled=%s, duration=%.1fs) → pexels",
                        order, runway_enabled, s.get("duration_sec", 0),
                    )
                    s["visual_source"] = _DEFAULT_SOURCE
                    if s["validation_status"] == "PASS":
                        s["issues"].append("runway not allowed — reverted to pexels")

            # ── Track best attempt ────────────────────────────────────────────
            if s["validation_status"] != "MAJOR":
                best_attempts[order] = copy.deepcopy(s)
            elif order not in best_attempts:
                best_attempts[order] = copy.deepcopy(s)   # record first attempt as baseline

    # After all rounds: apply best attempt to sections still MAJOR
    for s in working:
        order = s["section_order"]
        if s.get("validation_status") == "MAJOR" and order in best_attempts:
            best = best_attempts[order]
            s.update(best)
            s["best_attempt_used"] = True
            logger.warning(
                "Section %d still MAJOR after %d rounds — using best attempt",
                order, _MAX_ROUNDS,
            )
        else:
            s.setdefault("best_attempt_used", False)

    logger.info(
        "Section validation complete: %d section(s) | PASS=%d MINOR=%d MAJOR=%d best_attempt=%d",
        len(working),
        sum(1 for s in working if s.get("validation_status") == "PASS"),
        sum(1 for s in working if s.get("validation_status") == "MINOR"),
        sum(1 for s in working if s.get("validation_status") == "MAJOR"),
        sum(1 for s in working if s.get("best_attempt_used")),
    )
    return working


# ── Helpers ───────────────────────────────────────────────────────────────────

def _coerce(value: str | None, valid: set, default: str) -> str:
    """Return value if it's in the valid set, otherwise return default."""
    if value and value.strip().lower() in valid:
        return value.strip().lower()
    if value:
        logger.warning("Invalid enum value %r — using default %r", value, default)
    return default


def _runway_allowed(section: dict, runway_enabled: bool) -> bool:
    """Enforce Runway conditions 3 and 4 (conditions 1 and 2 are checked in Step 5).

    Condition 3: section duration ≤ 5 seconds
    Condition 4: runway_enabled = True in channel_config
    """
    if not runway_enabled:
        return False
    duration_sec = section.get("duration_sec", 999)
    return duration_sec <= 5.0
