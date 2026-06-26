"""Story Scoring Gate — rejects boring or visually weak stories before scripting.

Replaces the old "relevance + engagement + substance" selection (which never
checked narrative tension, visual potential, or retention potential, and had
no reject path) with a deterministic scoring gate:

  Claude scores eighteen fixed dimensions of a candidate story (with justifications).
  Python computes the weighted overall score and makes the accept/reject call —
  Claude never decides ACCEPTED/REJECTED directly (CLAUDE.md determinism rules:
  business rules belong in Python, prompts only generate/classify content).

Rejected candidates are never persisted as ``Content`` and never sent to
Telegram — only a story that clears every gate proceeds to script generation.

Fetch + score + gate orchestration for one candidate at a time lives in
``run_discovery()`` (``discovery.py``), which calls ``score_story_for_gate()``
directly and then the two functions below. The standalone single-candidate
fetch+score+gate wrapper that used to live in this module
(``run_story_scoring_gate()``) was removed in Phase 10A-0 as dead code — it
had no callers; ``run_discovery()``'s own dedup-retry/nuclear-retry/manual-
fallback fetch flow fully supersedes the simple single-fetch path it used.
"""

import logging

logger = logging.getLogger(__name__)

# Weighted dimensions — must sum to 1.0.
# opening_scene_strength, social_media_clickability, thumbnail_strength,
# visual_storytelling_potential, and viral_clip_count are the highest-priority
# dimensions: they gate YouTube retention and platform performance directly.
# central_mystery and conflict_or_contradiction are the primary narrative-quality
# gates: a story with no mystery, conflict, or contradiction cannot hold a viewer.
_DIMENSION_WEIGHTS: dict[str, float] = {
    "visual_storytelling_potential":   0.14,
    "social_media_clickability":       0.12,
    "opening_scene_strength":          0.10,
    "thumbnail_strength":              0.09,
    "scroll_stopper_potential":        0.08,
    "emotional_stakes":                0.08,
    "viral_clip_count":                0.07,
    "central_mystery":                 0.06,
    "curiosity_gap":                   0.05,
    "conflict_or_contradiction":       0.05,
    "emotional_specificity":           0.04,
    "title_thumbnail_potential":       0.03,
    "visual_range":                    0.03,
    "stock_media_feasibility":         0.02,
    "short_form_clip_potential":       0.01,
    "comment_section_potential":       0.01,
    "series_potential":                0.01,
    "episode_two_potential":           0.01,
}

# Hard floors — a story can have a high weighted average and still be rejected
# if any of these named dimensions falls below its individual floor.
# Dimension floors:
_MIN_OVERALL_SCORE           = 65
_MIN_VISUAL_STORYTELLING     = 55
_MIN_EMOTIONAL_STAKES        = 55
_MIN_SCROLL_STOPPER          = 55
_MIN_CENTRAL_MYSTERY         = 45
_MIN_CONFLICT_CONTRADICTION  = 45
_MIN_SOCIAL_CLICKABILITY     = 50
_MIN_TITLE_THUMBNAIL         = 50
_MIN_OPENING_SCENE           = 50
_MIN_THUMBNAIL_STRENGTH      = 50
_MIN_VISUAL_RANGE            = 35
_MIN_STOCK_FEASIBILITY       = 40



def _clamp_score(value) -> int:
    """Coerce a Claude-returned dimension score to an int clamped to [0, 100]."""
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        score = 0
    return max(0, min(100, score))


def score_story_assessment(assessment: dict) -> dict:
    """Compute the deterministic weighted overall score from Claude's per-dimension scores.

    Claude only judges each dimension (with a justification); this function owns
    all the math — clamping, weighting, and checking the hard floors — so the
    same assessment always yields the same score and the same gate result.

    Args:
        assessment: Raw dict from ``score_stories_comparatively()`` — must contain a
                    ``scores`` mapping of dimension name to either a plain integer
                    0–100 (new batch schema) or ``{"score": <0-100>}`` (legacy).

    Returns:
        Dict with:
          ``overall_score``:    float — weighted average, clamped to 0-100
          ``dimension_scores``: dict[str, int] — each dimension clamped to 0-100
          ``failed_gates``:     list[str] — human-readable description of every
                                hard-floor check that failed (empty if all passed)
    """
    raw_scores = assessment.get("scores") or {}
    # Support legacy key aliases from older single-candidate Claude responses
    if "emotional_tension" in raw_scores and "emotional_stakes" not in raw_scores:
        raw_scores["emotional_stakes"] = raw_scores["emotional_tension"]
    if "controversy_or_debate_potential" in raw_scores and "conflict_or_contradiction" not in raw_scores:
        raw_scores["conflict_or_contradiction"] = raw_scores["controversy_or_debate_potential"]

    def _extract(val) -> int:
        """Accept either plain int (new batch schema) or {"score": int} (legacy)."""
        if isinstance(val, (int, float)):
            return _clamp_score(val)
        if isinstance(val, dict):
            return _clamp_score(val.get("score"))
        return 0

    dimension_scores: dict[str, int] = {
        dimension: _extract(raw_scores.get(dimension))
        for dimension in _DIMENSION_WEIGHTS
    }

    overall_score = round(
        sum(dimension_scores[dimension] * weight for dimension, weight in _DIMENSION_WEIGHTS.items()),
        1,
    )

    failed_gates: list[str] = []
    if overall_score < _MIN_OVERALL_SCORE:
        failed_gates.append(f"overall_score {overall_score} < {_MIN_OVERALL_SCORE}")

    def _check(dim: str, floor: int) -> None:
        val = dimension_scores.get(dim, 0)
        if val < floor:
            failed_gates.append(f"{dim} {val} < {floor}")

    _check("visual_storytelling_potential", _MIN_VISUAL_STORYTELLING)
    _check("emotional_stakes",              _MIN_EMOTIONAL_STAKES)
    _check("scroll_stopper_potential",      _MIN_SCROLL_STOPPER)
    _check("central_mystery",              _MIN_CENTRAL_MYSTERY)
    _check("conflict_or_contradiction",    _MIN_CONFLICT_CONTRADICTION)
    _check("social_media_clickability",    _MIN_SOCIAL_CLICKABILITY)
    _check("title_thumbnail_potential",    _MIN_TITLE_THUMBNAIL)
    _check("opening_scene_strength",       _MIN_OPENING_SCENE)
    _check("thumbnail_strength",           _MIN_THUMBNAIL_STRENGTH)
    _check("visual_range",                 _MIN_VISUAL_RANGE)
    _check("stock_media_feasibility",      _MIN_STOCK_FEASIBILITY)

    return {
        "overall_score": overall_score,
        "dimension_scores": dimension_scores,
        "failed_gates": failed_gates,
    }


def decide_story_acceptance(story_score: dict) -> tuple[bool, str]:
    """Apply the fixed accept/reject gates to a computed story score.

    This is the ONLY place the accept/reject decision is made — Claude never
    returns a verdict directly. A story is accepted only if its weighted overall
    score clears the bar AND all three named hard floors (narrative_tension,
    visual_potential, youtube_retention) are individually satisfied.

    Args:
        story_score: Output of ``score_story_assessment()``.

    Returns:
        ``(accepted, reason)`` — ``reason`` is a short, deterministic,
        human-readable string suitable for logging (e.g. ``"passed all gates
        (overall_score=72.5)"`` or ``"failed: overall_score 58.0 < 65; ..."``).
    """
    failed_gates = story_score.get("failed_gates", [])
    if not failed_gates:
        return True, f"passed all gates (overall_score={story_score['overall_score']})"
    return False, "failed: " + "; ".join(failed_gates)


