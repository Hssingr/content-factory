"""Story Scoring Gate — rejects boring or visually weak stories before scripting.

Replaces the old "relevance + engagement + substance" selection (which never
checked narrative tension, visual potential, or retention potential, and had
no reject path) with a deterministic scoring gate:

  Claude scores eighteen fixed dimensions of a candidate story (with justifications).
  Python computes the weighted overall score and makes the accept/reject call —
  Claude never decides ACCEPTED/REJECTED directly (CLAUDE.md determinism rules:
  business rules belong in Python, prompts only generate/classify content).

Up to ``_MAX_CANDIDATES_PER_RUN`` candidates are fetched and scored per run.
Rejected candidates are never persisted as ``Content`` and never sent to
Telegram — only a story that clears every gate proceeds to script generation.
"""

import logging

from app.agents.agent2_discovery.services.fetcher import fetch_batch
from app.agents.agent2_discovery.services.story import Story
from app.agents.agent2_discovery.system_prompt import score_story_for_gate

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


def run_story_scoring_gate(
    sources: list[tuple[str, str, float]],
    niche: str,
    channel,
    script_format: str = "youtube_long",
) -> tuple[Story, dict] | None:
    """Fetch the highest-engagement story then score and gate it in two Claude calls.

    Phase 1 — Fetch: ``fetch_batch`` runs one web_search pass (story_research / Sonnet)
    and returns the single most engaged, highest-signal story from the configured sources.

    Phase 2 — Score: ``score_story_for_gate`` calls Claude once (story_gate_scoring /
    Sonnet, structured schema) to produce 18 dimension scores. No comparative scoring —
    there is only one candidate.

    Phase 3 — Gate: Python applies the weighted threshold and all hard floors.

    The gate fails closed: if either the fetch or the scoring call fails, ``None`` is
    returned and nothing is persisted.

    Args:
        sources:       List of ``(source_value, source_type, trust_score)`` tuples.
        niche:         Channel niche description, passed to the fetcher.
        channel:       Channel ORM object (provides niche/tone context to the scorer).
        script_format: Format key from ``channel_config.script_format``.

    Returns:
        ``(story, assessment)`` where ``story`` is the accepted ``Story`` and
        ``assessment`` is its scoring dict (for downstream Telegram message).
        Returns ``None`` if the story didn't clear the gate or no story was fetched.
    """
    # Phase 1: fetch single highest-engagement story
    stories: list[Story] = fetch_batch(sources, niche=niche)
    if not stories:
        logger.info(
            "Story Scoring Gate: fetch returned no story — exiting cleanly, "
            "no Content created, no Telegram message sent"
        )
        return None
    story = stories[0]
    logger.info(
        "Story Scoring Gate: fetched story (title=%r url=%s)", story.title[:80], story.url
    )

    # Phase 2: score the single story
    try:
        assessment = score_story_for_gate(
            story=story,
            channel=channel,
            script_format=script_format,
        )
    except Exception as exc:
        logger.error(
            "Story Scoring Gate: scoring failed for %r: %s — "
            "exiting cleanly, no Content created",
            story.title[:80], exc,
        )
        return None

    # Phase 3: apply gate
    try:
        story_score = score_story_assessment(assessment)
    except Exception as exc:
        logger.error(
            "Story Scoring Gate: score_story_assessment failed for %r — skipping: %s",
            story.title[:80], exc,
        )
        return None

    accepted, reason = decide_story_acceptance(story_score)
    decision = "ACCEPTED" if accepted else "REJECTED"

    logger.info(
        "Story Scoring Gate: title=%r url=%s overall_score=%.1f decision=%s reason=%s",
        story.title[:80], story.url, story_score["overall_score"], decision, reason,
    )
    top5 = dict(
        sorted(story_score["dimension_scores"].items(), key=lambda x: x[1], reverse=True)[:5]
    )
    logger.info("Story Scoring Gate top-5 dimensions: %s", top5)

    if accepted:
        return story, assessment

    logger.info(
        "Story Scoring Gate: story rejected (title=%r) — exiting cleanly, no Content created",
        story.title[:80],
    )
    return None
