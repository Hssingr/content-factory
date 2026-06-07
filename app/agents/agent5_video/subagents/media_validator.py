"""Media Validation Agent — reviews fetched media against each storyboard beat's intent.

Runs after the initial stock fetch for storyboard beats. Claude decides KEEP / REPLACE
/ ADJUST per beat (creative judgment); Python executes the replacement deterministically:
``replacement_search_query`` → ``fallback_query`` → ``generated_visual`` placeholder,
then re-validates. Runs at most ``_MAX_VALIDATION_PASSES`` rounds — never loops forever.
"""

import logging

from app.agents.agent5_video.services.stock_fetcher import fetch_for_beat
from app.agents.agent5_video.system_prompt import validate_media_with_claude

logger = logging.getLogger(__name__)

_MAX_VALIDATION_PASSES = 2

# Enum sets — Python enforces, never trusts Claude's revision strings blindly
_VALID_EFFECTS     = {"slow_zoom", "zoom_out", "pan", "push_in", "shake", "cut", "fade_in", "parallax"}
_VALID_GRADES      = {"desaturated", "cold_blue", "warm_amber", "dark_contrast", "neutral"}
_VALID_TRANSITIONS = {"cut", "crossfade", "dip_to_black", "whip_pan", "zoom_blur", "match_cut", "none"}

_DARK_FALLBACK_URL      = "__dark_fallback__"
_GENERATED_PLACEHOLDER  = "__generated_pending__"


def validate_and_replace_media(
    beats: list[dict],
    channel_niche: str,
    channel_tone: str,
    script_format: str,
) -> list[dict]:
    """Validate fetched media against each beat's intent and replace weak matches.

    Steps per round:
      1. Ask Claude to review every beat's fetched media (KEEP / REPLACE / ADJUST).
      2. REPLACE → re-fetch via ``replacement_search_query``, then ``fallback_query``,
         then fall back to a ``generated_visual`` placeholder.
      3. ADJUST  → revise effect / color_grade / transition_to_next / overlay_text only.
      4. Stop early once Claude approves or no beat needed replacing this round.

    Args:
        beats:         Beat-section dicts already enriched with fetched media.
        channel_niche: Channel niche for context.
        channel_tone:  Channel tone for context.
        script_format: Format key — informs pacing/rhythm expectations.

    Returns:
        The same beats list, with REPLACE beats re-fetched and ADJUST beats revised
        in place. On Claude failure the current media is kept and logged.
    """
    for round_num in range(1, _MAX_VALIDATION_PASSES + 1):
        try:
            review = validate_media_with_claude(beats, channel_niche, channel_tone, script_format)
        except Exception as exc:
            logger.error(
                "Media validation Claude call failed (round %d): %s — keeping current media",
                round_num, exc,
            )
            return beats

        status = review.get("validation_status", "APPROVED")
        reviews_by_order = {
            r["beat_order"]: r for r in review.get("beat_reviews", []) if "beat_order" in r
        }

        replacements = 0
        for beat in beats:
            order = beat.get("beat_order", beat.get("section_order"))
            entry = reviews_by_order.get(order)
            if entry is None:
                continue

            decision = str(entry.get("decision", "KEEP")).upper()
            if decision == "REPLACE":
                _replace_beat_media(beat, entry)
                replacements += 1
            elif decision == "ADJUST":
                _apply_adjustments(beat, entry)

        logger.info(
            "Media validation round %d/%d: status=%s replacements=%d — %s",
            round_num, _MAX_VALIDATION_PASSES, status, replacements,
            review.get("overall_comment", ""),
        )

        if status == "APPROVED" or replacements == 0:
            break

    return beats


# ── Internal helpers ──────────────────────────────────────────────────────────

def _replace_beat_media(beat: dict, entry: dict) -> None:
    """Re-fetch a beat's media: replacement_search_query → fallback_query → placeholder."""
    order = beat.get("beat_order", beat.get("section_order", "?"))
    old_url = beat.get("media_url", "")

    candidates = [
        q.strip() for q in (entry.get("replacement_search_query", ""), beat.get("fallback_query", ""))
        if isinstance(q, str) and q.strip()
    ]

    for query in candidates:
        beat["search_query"] = query
        try:
            fetch_for_beat(beat)
        except Exception as exc:
            logger.error("Beat %s: replacement fetch failed for query=%r: %s", order, query, exc)
            continue

        new_url = beat.get("media_url", "")
        if new_url and new_url != _DARK_FALLBACK_URL and new_url != old_url:
            logger.info(
                "Beat %s: media replaced — query=%r old=%s new=%s",
                order, query, old_url[:60], new_url[:60],
            )
            _apply_adjustments(beat, entry)
            return

    # Both queries failed (or returned the same dark fallback) — generated_visual placeholder
    logger.warning(
        "Beat %s: replacement queries exhausted — using generated_visual placeholder", order,
    )
    beat["visual_type"]   = "generated_visual"
    beat["clips"]         = [{"url": _GENERATED_PLACEHOLDER, "thumb_url": "", "media_type": "image", "source": "generated"}]
    beat["media_url"]     = _GENERATED_PLACEHOLDER
    beat["media_thumb"]   = ""
    beat["media_type"]    = "image"
    beat["media_source"]  = "generated"
    _apply_adjustments(beat, entry)


def _apply_adjustments(beat: dict, entry: dict) -> None:
    """Apply Claude's revised effect/color_grade/transition_to_next/overlay_text in place.

    Each value is checked against its enum set before being applied — an invalid
    or missing revision leaves the beat's current value untouched.
    """
    effect = entry.get("effect")
    if isinstance(effect, str) and effect.strip().lower() in _VALID_EFFECTS:
        beat["effect"] = effect.strip().lower()

    grade = entry.get("color_grade")
    if isinstance(grade, str) and grade.strip().lower() in _VALID_GRADES:
        beat["color_grade"] = grade.strip().lower()

    transition = entry.get("transition_to_next")
    if isinstance(transition, str) and transition.strip().lower() in _VALID_TRANSITIONS:
        beat["transition_to_next"] = transition.strip().lower()

    overlay_text = entry.get("overlay_text")
    if isinstance(overlay_text, str):
        beat["overlay_text"] = overlay_text.strip()
