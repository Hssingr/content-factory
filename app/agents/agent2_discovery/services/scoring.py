"""Story Scoring Gate — rejects boring or visually weak stories before scripting.

Replaces the old "relevance + engagement + substance" selection (which never
checked narrative tension, visual potential, or retention potential, and had
no reject path) with a deterministic scoring gate:

  Claude scores nine fixed dimensions of a candidate story (with justifications).
  Python computes the weighted overall score and makes the accept/reject call —
  Claude never decides ACCEPTED/REJECTED directly (CLAUDE.md determinism rules:
  business rules belong in Python, prompts only generate/classify content).

Up to ``_MAX_CANDIDATES_PER_RUN`` candidates are fetched and scored per run.
Rejected candidates are never persisted as ``Content`` and never sent to
Telegram — only a story that clears every gate proceeds to script generation.
"""

import logging

from app.agents.agent2_discovery.services.fetcher import fetch as claude_fetch
from app.agents.agent2_discovery.services.story import Story
from app.agents.agent2_discovery.system_prompt import assess_story_quality

logger = logging.getLogger(__name__)

# Weighted dimensions — must sum to 1.0. visual_potential and narrative_tension/
# youtube_retention are weighted heaviest because they are the named non-negotiable
# axes (see _MIN_* floors below); the rest round out overall documentary quality.
_DIMENSION_WEIGHTS: dict[str, float] = {
    "visual_potential": 0.20,
    "narrative_tension": 0.15,
    "youtube_retention": 0.15,
    "emotional_impact": 0.10,
    "curiosity_gap": 0.10,
    "documentary_potential": 0.10,
    "stock_media_availability": 0.10,
    "shorts_potential": 0.05,
    "visual_diversity": 0.05,
}

# Hard floors — a story can have a high weighted average and still be rejected
# if any one of these named dimensions is weak ("strong narrative tension, strong
# visual potential, and high viewer-retention potential" are non-negotiable).
_MIN_OVERALL_SCORE = 65
_MIN_NARRATIVE_TENSION = 60
_MIN_VISUAL_POTENTIAL = 60
_MIN_YOUTUBE_RETENTION = 60

_MAX_CANDIDATES_PER_RUN = 3


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
        assessment: Raw dict from ``assess_story_quality()`` — must contain a
                    ``scores`` mapping of dimension name to
                    ``{"score": <0-100>, "justification": "..."}``.

    Returns:
        Dict with:
          ``overall_score``:    float — weighted average, clamped to 0-100
          ``dimension_scores``: dict[str, int] — each dimension clamped to 0-100
          ``failed_gates``:     list[str] — human-readable description of every
                                hard-floor check that failed (empty if all passed)
    """
    raw_scores = assessment.get("scores") or {}
    dimension_scores: dict[str, int] = {
        dimension: _clamp_score((raw_scores.get(dimension) or {}).get("score"))
        for dimension in _DIMENSION_WEIGHTS
    }

    overall_score = round(
        sum(dimension_scores[dimension] * weight for dimension, weight in _DIMENSION_WEIGHTS.items()),
        1,
    )

    failed_gates: list[str] = []
    if overall_score < _MIN_OVERALL_SCORE:
        failed_gates.append(f"overall_score {overall_score} < {_MIN_OVERALL_SCORE}")
    if dimension_scores["narrative_tension"] < _MIN_NARRATIVE_TENSION:
        failed_gates.append(
            f"narrative_tension {dimension_scores['narrative_tension']} < {_MIN_NARRATIVE_TENSION}"
        )
    if dimension_scores["visual_potential"] < _MIN_VISUAL_POTENTIAL:
        failed_gates.append(
            f"visual_potential {dimension_scores['visual_potential']} < {_MIN_VISUAL_POTENTIAL}"
        )
    if dimension_scores["youtube_retention"] < _MIN_YOUTUBE_RETENTION:
        failed_gates.append(
            f"youtube_retention {dimension_scores['youtube_retention']} < {_MIN_YOUTUBE_RETENTION}"
        )

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
) -> Story | None:
    """Fetch and score candidate stories; return the first one that clears every gate.

    Flow per attempt (up to ``_MAX_CANDIDATES_PER_RUN``):
      1. Ask Claude to fetch a candidate story (excluding already-rejected ones).
      2. Ask Claude to score it across nine fixed dimensions
         (``assess_story_quality``).
      3. Compute the deterministic weighted score (``score_story_assessment``).
      4. Apply the fixed accept/reject gates (``decide_story_acceptance``).

    If a candidate is accepted, it is returned immediately — no further
    candidates are fetched. If scoring itself fails (Claude error, malformed
    JSON), that candidate is rejected and the loop continues — the gate fails
    closed (an unscored story never proceeds). If no candidate clears the bar
    after ``_MAX_CANDIDATES_PER_RUN`` attempts, ``None`` is returned and the
    caller exits cleanly: nothing is persisted, no Telegram message is sent.

    Args:
        sources:       List of ``(source_value, source_type, trust_score)`` tuples.
        niche:         Channel niche description, passed to the fetcher.
        channel:       Channel ORM object (provides niche/tone context to the scorer).
        script_format: Format key from ``channel_config.script_format``.

    Returns:
        The first ``Story`` that is ACCEPTED, or ``None`` if no candidate
        cleared the bar (or the fetcher produced nothing) within the attempt limit.
    """
    rejected: list[dict] = []

    for attempt in range(1, _MAX_CANDIDATES_PER_RUN + 1):
        story = claude_fetch(sources, niche=niche, exclude=rejected or None)
        if story is None:
            logger.info(
                "Story Scoring Gate: fetcher returned no candidate on attempt %d/%d",
                attempt, _MAX_CANDIDATES_PER_RUN,
            )
            break

        try:
            assessment = assess_story_quality(
                story, channel, script_format=script_format, rejected_candidates=rejected or None,
            )
            story_score = score_story_assessment(assessment)
        except Exception as exc:
            logger.error(
                "Story Scoring Gate: scoring failed for candidate %d/%d "
                "(title=%r url=%s) — rejecting and trying next: %s",
                attempt, _MAX_CANDIDATES_PER_RUN, story.title[:80], story.url, exc,
            )
            rejected.append({"title": story.title, "url": story.url})
            continue

        accepted, reason = decide_story_acceptance(story_score)
        decision = "ACCEPTED" if accepted else "REJECTED"

        logger.info(
            "Story Scoring Gate candidate %d/%d: title=%r url=%s overall_score=%.1f "
            "dimension_scores=%s decision=%s reason=%s",
            attempt, _MAX_CANDIDATES_PER_RUN, story.title[:80], story.url,
            story_score["overall_score"], story_score["dimension_scores"], decision, reason,
        )
        logger.info(
            "Story Scoring Gate candidate %d/%d details: central_tension=%r "
            "best_possible_hook=%r concrete_visual_elements=%s",
            attempt, _MAX_CANDIDATES_PER_RUN,
            assessment.get("central_tension", ""), assessment.get("best_possible_hook", ""),
            assessment.get("concrete_visual_elements", []),
        )

        if accepted:
            return story

        rejected.append({"title": story.title, "url": story.url})

    logger.info(
        "Story Scoring Gate: no candidate cleared the bar after %d attempt(s) — "
        "exiting cleanly, no Content created, no Telegram message sent",
        len(rejected) if rejected else _MAX_CANDIDATES_PER_RUN,
    )
    return None
