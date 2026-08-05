"""Runtime proof for Task 2e (code_report/TODO, 2026-08-05): the
forbidden_flux_word check no longer flags "cinematic" when it is part of the
operator-approved "cinematic_cartoon" image_style name — a real production
run measured this check flagging 38/38 beats MAJOR purely because every beat
opened with the style phrase system_prompt.py itself instructs Claude to use
("cinematic cartoon illustration of ..."), not because those prompts were
actually mood-only with no physical subject (the real defect this check
exists to catch).
"""

from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard


def _beat(order: int, flux_prompt: str) -> dict:
    return {
        "beat_order": order,
        "flux_prompt": flux_prompt,
        "media_url": "",
        "visual_type": "b-roll",
    }


def _forbidden_issues(beats):
    return [i for i in validate_storyboard(beats) if i["check"] == "forbidden_flux_word"]


def test_cinematic_cartoon_style_phrase_is_not_flagged():
    beats = [_beat(0, (
        "cinematic cartoon illustration of a rough hand-carved mine tunnel "
        "entrance in a reddish rock face, timber support beams framing the "
        "opening"
    ))]
    assert _forbidden_issues(beats) == []


def test_cinematic_cartoon_with_underscore_spelling_is_not_flagged():
    beats = [_beat(0, (
        "cinematic_cartoon illustration, simplified stylized rendering with "
        "bold clean outlines and flat rich color fills, a massive reddish "
        "conical mountain peak"
    ))]
    assert _forbidden_issues(beats) == []


def test_real_production_shaped_batch_is_not_flagged_38_of_38(caplog=None):
    """Reproduces the reported real-run shape: nearly every beat of a
    cinematic_cartoon-style channel opens with the style phrase."""
    beats = [
        _beat(i, f"cinematic cartoon illustration of physical scene detail {i}, sharp focus")
        for i in range(38)
    ]
    assert _forbidden_issues(beats) == []


def test_standalone_cinematic_as_a_pure_mood_word_is_still_flagged():
    # The check must still catch a genuinely mood-only prompt with no
    # physical subject and no connection to the approved style name.
    # (No comma immediately after "cinematic" — this check's word-match is
    # whitespace-split only, so "cinematic," would never have matched the
    # frozenset even before this fix; unrelated pre-existing crude-match
    # behavior, not something Task 2e's fix changes.)
    beats = [_beat(0, "a cinematic atmospheric shot with dramatic lighting")]
    issues = _forbidden_issues(beats)
    assert len(issues) == 1
    assert "cinematic" in issues[0]["description"]


def test_other_forbidden_mood_words_are_unaffected():
    beats = [_beat(0, "an eerie, haunting corridor with no physical subject named")]
    issues = _forbidden_issues(beats)
    assert len(issues) == 1
    assert "eerie" in issues[0]["description"] or "haunting" in issues[0]["description"]
