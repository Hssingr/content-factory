"""Media Validation Agent — incremental, stateful, bounded validation.

Each beat carries a validation state that persists across passes:
  PENDING   — not yet reviewed
  APPROVED  — Claude returned KEEP (will not be re-reviewed unless a neighbour changed)
  DIRTY     — needs (re-)validation: initial default or neighbour was replaced
  REPLACED  — media was swapped this pass; triggers DIRTY on immediate neighbours
  FAILED    — replacement attempts exhausted or oscillation detected; keeps last media

Pass 1 validates all beats and classifies them. Passes 2-N target only DIRTY and
REPLACED beats plus a context window around changed beats so Claude can check visual
repetition with neighbours.

Hard limits prevent runaway behaviour:
  max_passes              — default 5 (configurable per call)
  max_replacements_per_beat — default 2
  max_total_replacement_ratio — default 40 % of total beats
"""

import logging
from collections import Counter
from typing import TypedDict

from app.agents.agent5_video.services.stock_fetcher import fetch_for_beat
from app.agents.agent5_video.system_prompt import validate_media_with_claude_batched

logger = logging.getLogger(__name__)

# ── Beat validation state constants ───────────────────────────────────────────
PENDING  = "PENDING"
APPROVED = "APPROVED"
DIRTY    = "DIRTY"
REPLACED = "REPLACED"
FAILED   = "FAILED"

_DIRTY_STATES: frozenset[str] = frozenset({PENDING, DIRTY, REPLACED})

# ── Config defaults ─────────────────────────────────────────────────────────
_MAX_MEDIA_VALIDATION_PASSES  = 5
_MAX_REPLACEMENTS_PER_BEAT    = 2
_MAX_TOTAL_REPLACEMENT_RATIO  = 0.40

# ── Stop reason constants ──────────────────────────────────────────────────
_STOP_ALL_APPROVED   = "ALL_APPROVED"
_STOP_MAX_PASSES     = "MAX_PASSES"
_STOP_MAX_REPLACEMENTS = "MAX_REPLACEMENTS"
_STOP_CLAUDE_ERROR   = "CLAUDE_ERROR"

# ── Media URL sentinels ────────────────────────────────────────────────────
_DARK_FALLBACK_URL     = "__dark_fallback__"
_GENERATED_PLACEHOLDER = "__generated_pending__"
_PLACEHOLDER_URLS: frozenset[str] = frozenset({_DARK_FALLBACK_URL, _GENERATED_PLACEHOLDER})

# ── Enum sets — Python enforces, never trusts Claude's revision strings ────
_VALID_EFFECTS     = {"slow_zoom", "zoom_out", "pan", "push_in", "shake", "cut", "fade_in", "parallax"}
_VALID_GRADES      = {"desaturated", "cold_blue", "warm_amber", "dark_contrast", "neutral"}
_VALID_TRANSITIONS = {"cut", "crossfade", "dip_to_black", "whip_pan", "zoom_blur", "match_cut", "none"}


class MediaValidationState(TypedDict):
    """Summary of the media validation run returned alongside the beat list."""
    stop_reason:        str   # one of _STOP_* constants
    passes_run:         int
    total_replacements: int
    approved_count:     int
    failed_count:       int
    dirty_remaining:    int


def validate_and_replace_media(
    beats: list[dict],
    channel_niche: str,
    channel_tone: str,
    script_format: str,
    max_passes: int = _MAX_MEDIA_VALIDATION_PASSES,
    max_replacements_per_beat: int = _MAX_REPLACEMENTS_PER_BEAT,
    max_replacement_ratio: float = _MAX_TOTAL_REPLACEMENT_RATIO,
) -> tuple[list[dict], MediaValidationState]:
    """Validate and incrementally repair each beat's fetched media.

    Incremental behaviour:
      - Pass 1: validate all beats → KEEP→APPROVED, REPLACE/ADJUST→DIRTY.
      - Passes 2-N: only DIRTY + REPLACED beats are sent for review.
      - Each successful replacement marks the beat REPLACED and marks its
        immediate neighbours DIRTY so Claude can check visual rhythm locally.
      - APPROVED beats are never re-reviewed unless a neighbour changes.

    Oscillation guard (per beat):
      - If a replacement returns a URL already in that beat's history → FAILED.
      - If the replacement count for a beat reaches max_replacements_per_beat → FAILED.

    Global cap:
      - Once total_replacements reaches ``max_replacement_ratio * len(beats)``
        the loop stops regardless of remaining DIRTY beats.

    Args:
        beats:                    Beat-section dicts enriched by the stock fetcher.
        channel_niche:            Channel niche for Claude context.
        channel_tone:             Channel tone for Claude context.
        script_format:            Format key — informs pacing expectations.
        max_passes:               Hard limit on validation passes.
        max_replacements_per_beat: Max times a single beat may be replaced.
        max_replacement_ratio:    Fraction of total beats that may be replaced in total.

    Returns:
        ``(beats, state)`` — ``beats`` mutated in place with best available media;
        ``state`` summarises the run (stop_reason, counts).
    """
    n = len(beats)
    if n == 0:
        return beats, _empty_state(_STOP_ALL_APPROVED)

    max_total: int = max(1, int(n * max_replacement_ratio))

    # Per-beat tracking (indexed by list position = beat_order after renumbering)
    beat_states:        dict[int, str]        = {i: PENDING for i in range(n)}
    replacement_counts: dict[int, int]        = {i: 0 for i in range(n)}
    url_history:        dict[int, list[str]]  = {
        i: [beats[i].get("media_url", "")] for i in range(n)
    }

    # beat_order → list index (beat_order may differ from list index on re-entrant runs)
    _order_to_idx: dict[int, int] = {
        beats[i].get("beat_order", beats[i].get("section_order", i)): i
        for i in range(n)
    }

    total_replacements = 0
    stop_reason        = _STOP_MAX_PASSES
    passes_run         = 0

    for _pass in range(1, max_passes + 1):

        # Determine which beats need (re-)validation this pass
        target_indices = [i for i in range(n) if beat_states[i] in _DIRTY_STATES]
        if not target_indices:
            stop_reason = _STOP_ALL_APPROVED
            break
        if total_replacements >= max_total:
            stop_reason = _STOP_MAX_REPLACEMENTS
            break

        passes_run += 1

        try:
            review = validate_media_with_claude_batched(
                beats, channel_niche, channel_tone, script_format,
                target_indices=target_indices,
            )
        except Exception as exc:
            logger.error(
                "Media validation pass %d/%d failed (Claude error): %s — keeping current media",
                _pass, max_passes, exc,
            )
            stop_reason = _STOP_CLAUDE_ERROR
            break

        reviews_by_order: dict[int, dict] = {
            r["beat_order"]: r
            for r in review.get("beat_reviews", [])
            if "beat_order" in r
        }

        # ── Decision distribution log ──────────────────────────────────────
        decision_dist = Counter(
            str(r.get("decision", "KEEP")).upper()
            for r in reviews_by_order.values()
        )
        logger.info(
            "Media validation pass %d/%d — target=%d coverage=%d/%d decisions=%s",
            _pass, max_passes,
            len(target_indices), len(reviews_by_order), len(target_indices),
            dict(decision_dist),
        )

        replace_ok     = 0
        replace_failed = 0
        adjustments    = 0

        for idx in target_indices:
            beat      = beats[idx]
            beat_order = beat.get("beat_order", beat.get("section_order", idx))
            entry      = reviews_by_order.get(beat_order)

            if entry is None:
                # Claude returned no review for this beat — treat as implicitly approved
                beat_states[idx] = APPROVED
                continue

            decision = str(entry.get("decision", "KEEP")).upper()

            if decision == "KEEP":
                beat_states[idx] = APPROVED

            elif decision == "ADJUST":
                _apply_adjustments(beat, entry)
                beat_states[idx] = APPROVED
                adjustments += 1

            elif decision == "REPLACE":
                # ── Per-beat replacement cap ───────────────────────────────
                if replacement_counts[idx] >= max_replacements_per_beat:
                    logger.warning(
                        "Beat %s: hit replacement cap (%d/%d) — marking FAILED",
                        beat_order, replacement_counts[idx], max_replacements_per_beat,
                    )
                    beat_states[idx] = FAILED
                    replace_failed += 1
                    continue

                # ── Global replacement cap ────────────────────────────────
                if total_replacements >= max_total:
                    beat_states[idx] = FAILED
                    replace_failed += 1
                    continue

                old_url = beat.get("media_url", "")
                _replace_beat_media(beat, entry)
                new_url = beat.get("media_url", "")

                # ── Oscillation guard ─────────────────────────────────────
                if not new_url or new_url == old_url:
                    # Fetch returned same or empty URL
                    beat_states[idx] = FAILED
                    replace_failed += 1
                    continue

                if new_url in url_history[idx]:
                    # Returned to a previously seen URL — oscillating
                    logger.warning(
                        "Beat %s: oscillation detected (url already in history) — FAILED",
                        beat_order,
                    )
                    beat_states[idx] = FAILED
                    replace_failed += 1
                    continue

                # ── Successful replacement ────────────────────────────────
                url_history[idx].append(new_url)
                replacement_counts[idx] += 1
                total_replacements += 1
                beat_states[idx] = REPLACED
                replace_ok += 1

                # Mark immediate neighbours DIRTY so Claude can re-check local rhythm
                for neighbour in (idx - 1, idx + 1):
                    if 0 <= neighbour < n and beat_states[neighbour] == APPROVED:
                        beat_states[neighbour] = DIRTY

        # ── Pass summary log ──────────────────────────────────────────────
        approved_count   = sum(1 for s in beat_states.values() if s == APPROVED)
        dirty_remaining  = sum(1 for s in beat_states.values() if s in _DIRTY_STATES)
        failed_count     = sum(1 for s in beat_states.values() if s == FAILED)
        placeholders_now = sum(
            1 for b in beats if b.get("media_url", "") in _PLACEHOLDER_URLS
        )

        _pass_stop = (
            _STOP_MAX_REPLACEMENTS if total_replacements >= max_total else "continue"
        )
        logger.info(
            "Media validation pass %d/%d: "
            "target=%d approved=%d dirty=%d failed=%d "
            "replace_ok=%d replace_failed=%d adjustments=%d "
            "total_replacements=%d/%d placeholders=%d stop=%s",
            _pass, max_passes,
            len(target_indices), approved_count, dirty_remaining, failed_count,
            replace_ok, replace_failed, adjustments,
            total_replacements, max_total, placeholders_now, _pass_stop,
        )

        if total_replacements >= max_total:
            stop_reason = _STOP_MAX_REPLACEMENTS
            break

    # ── Final summary ──────────────────────────────────────────────────────
    approved_count  = sum(1 for s in beat_states.values() if s == APPROVED)
    dirty_remaining = sum(1 for s in beat_states.values() if s in _DIRTY_STATES)
    failed_count    = sum(1 for s in beat_states.values() if s == FAILED)

    state: MediaValidationState = {
        "stop_reason":        stop_reason,
        "passes_run":         passes_run,
        "total_replacements": total_replacements,
        "approved_count":     approved_count,
        "failed_count":       failed_count,
        "dirty_remaining":    dirty_remaining,
    }

    logger.info(
        "Media validation complete: stop=%s passes=%d total_replacements=%d "
        "approved=%d failed=%d dirty_remaining=%d",
        stop_reason, passes_run, total_replacements,
        approved_count, failed_count, dirty_remaining,
    )
    return beats, state


# ── Internal helpers ───────────────────────────────────────────────────────────

def _empty_state(stop_reason: str) -> MediaValidationState:
    return {
        "stop_reason":        stop_reason,
        "passes_run":         0,
        "total_replacements": 0,
        "approved_count":     0,
        "failed_count":       0,
        "dirty_remaining":    0,
    }


def _replace_beat_media(beat: dict, entry: dict) -> None:
    """Re-fetch a beat's media: replacement_search_query → fallback_query → placeholder."""
    order   = beat.get("beat_order", beat.get("section_order", "?"))
    old_url = beat.get("media_url", "")

    candidates = [
        q.strip()
        for q in (entry.get("replacement_search_query", ""), beat.get("fallback_query", ""))
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
                "Beat %s: media replaced — query=%r old=…%s new=…%s",
                order, query, old_url[-40:], new_url[-40:],
            )
            _apply_adjustments(beat, entry)
            return

    # Both queries failed — fall back to generated_visual placeholder
    logger.warning(
        "Beat %s: replacement queries exhausted — using generated_visual placeholder", order,
    )
    beat["visual_type"]  = "generated_visual"
    beat["clips"]        = [{"url": _GENERATED_PLACEHOLDER, "thumb_url": "", "media_type": "image", "source": "generated"}]
    beat["media_url"]    = _GENERATED_PLACEHOLDER
    beat["media_thumb"]  = ""
    beat["media_type"]   = "image"
    beat["media_source"] = "generated"
    _apply_adjustments(beat, entry)


def _apply_adjustments(beat: dict, entry: dict) -> None:
    """Apply Claude's revised effect/color_grade/transition_to_next/overlay_text.

    Each value is validated against its enum set; an invalid or missing value
    leaves the beat's current value untouched.
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
