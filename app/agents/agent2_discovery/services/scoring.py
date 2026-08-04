"""Story Scoring Gate — scores every discovered/synthesized candidate before
it reaches the operator for approval.

Replaces the old "relevance + engagement + substance" selection (which never
checked narrative tension, visual potential, or retention potential, and had
no scoring signal at all) with a deterministic scoring gate:

  Claude scores fixed performance dimensions plus an unweighted rights/IP risk
  signal for operator review. Python computes the weighted overall score and
  the gate verdict (``decide_story_acceptance()``) — Claude never decides
  PASSED/BELOW_FLOOR directly (CLAUDE.md determinism rules: business rules
  belong in Python, prompts only generate/classify content).

**The gate verdict is informational only and never blocks the pipeline**
(operator decision, superseding an earlier "auto-reject" design): every
candidate that clears dedup + the source-material floor is persisted as
``Content`` and sent to Telegram with its score attached
(``build_telegram_message()``, ``discovery.py``'s ``system_prompt.py``) —
the human operator decides via APPROVE/CHANGE, not an automated verdict.
``decide_story_acceptance()`` still computes a real accept/reject verdict;
``run_discovery()`` uses it only for logging (`gate_verdict=PASSED` /
`BELOW_FLOOR`) and to let the operator see the same call the gate would
have made, never to gate persistence or the Telegram send.

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

_RIGHTS_IP_RISK_DIMENSION = "rights_ip_risk"
_RIGHTS_IP_REVIEW_THRESHOLD = 70

# Weighted dimensions — must sum to 1.0.
# Slimmed 19 → 7 weighted dimensions + unweighted rights_ip_risk (fresh
# full-system audit §2.5): four near-duplicate pairs were merged into their
# strongest member, and the five ≤0.02-weight dimensions (which contributed at
# most 7 combined points while forcing the model to score them every call)
# were dropped. Each surviving dimension's prompt description absorbs its
# merged siblings' meaning (see system_prompt._SCORING_DIMENSIONS).
_DIMENSION_WEIGHTS: dict[str, float] = {
    "visual_storytelling_potential":   0.20,
    "scroll_stopper_potential":        0.15,
    "emotional_stakes":                0.15,
    "central_mystery":                 0.15,
    "social_media_clickability":       0.15,
    "conflict_or_contradiction":       0.10,
    "image_generation_feasibility":    0.10,
}

# Hard floors — a story can have a high weighted average and still be rejected
# if any of these named dimensions falls below its individual floor.
# Slimmed 12 → 5 pass conditions (audit §2.5): every floor is a chance for one
# noisy LLM dimension score to reject a usable story (a story scoring 80
# overall used to die on opening_scene_strength=49). The four kept floors are
# the non-negotiables: a story that cannot be shown, has no human stakes,
# cannot hook a scroller, or cannot be depicted as generated images is
# unusable regardless of its average.
_MIN_OVERALL_SCORE           = 65
_MIN_VISUAL_STORYTELLING     = 55
_MIN_EMOTIONAL_STAKES        = 55
_MIN_SCROLL_STOPPER          = 55
_MIN_IMAGE_GENERATION_FEASIBILITY = 40



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
          ``operator_review_flags``: list[str] — non-blocking flags for human approval,
                                including high rights/IP risk.
    """
    raw_scores = assessment.get("scores") or {}
    # Legacy key aliases (emotional_tension, controversy_or_debate_potential,
    # stock_media_feasibility) removed (fresh full-system audit §4): no
    # producer of those keys exists — every assessment comes fresh from
    # score_story_for_gate(), whose forced tool-use schema requires the
    # canonical dimension names.

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
    dimension_scores[_RIGHTS_IP_RISK_DIMENSION] = _extract(
        raw_scores.get(_RIGHTS_IP_RISK_DIMENSION)
    )

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
    _check("image_generation_feasibility",  _MIN_IMAGE_GENERATION_FEASIBILITY)

    operator_review_flags: list[str] = []
    rights_ip_risk = dimension_scores.get(_RIGHTS_IP_RISK_DIMENSION, 0)
    if rights_ip_risk >= _RIGHTS_IP_REVIEW_THRESHOLD:
        operator_review_flags.append(
            f"rights_ip_risk {rights_ip_risk} >= {_RIGHTS_IP_REVIEW_THRESHOLD} — operator decision required"
        )

    return {
        "overall_score": overall_score,
        "dimension_scores": dimension_scores,
        "failed_gates": failed_gates,
        "operator_review_flags": operator_review_flags,
    }


def decide_story_acceptance(story_score: dict) -> tuple[bool, str]:
    """Apply the fixed accept/reject gates to a computed story score.

    This is the ONLY place the accept/reject decision is made — Claude never
    returns a verdict directly. A story is accepted only if its weighted
    overall score clears ``_MIN_OVERALL_SCORE`` AND every per-dimension hard
    floor (visual storytelling, emotional stakes, scroll-stopper, image
    generation feasibility) is individually satisfied.

    Args:
        story_score: Output of ``score_story_assessment()``.

    Returns:
        ``(accepted, reason)`` — ``reason`` is a short, deterministic,
        human-readable string suitable for logging (e.g. ``"passed all gates
        (overall_score=72.5)"`` or ``"failed: overall_score 58.0 < 65; ..."``).
    """
    failed_gates = story_score.get("failed_gates", [])
    if not failed_gates:
        flags = story_score.get("operator_review_flags") or []
        if flags:
            return True, (
                f"passed all gates (overall_score={story_score['overall_score']}; "
                f"operator_review_flags={len(flags)})"
            )
        return True, f"passed all gates (overall_score={story_score['overall_score']})"
    return False, "failed: " + "; ".join(failed_gates)


