"""Runtime proof for Task 3 (code_report/TODO, 2026-08-05): the
forbidden_flux_word check exempts a forbidden word only when it is a token of
the CHANNEL'S OWN configured image_style/visual_style value, and that value's
own phrase genuinely appears in the prompt — generalized from Task 2e's
single hardcoded "cinematic cartoon" exemption (which a real production run
needed after it flagged MAJOR on 38/38 beats) to every configured style
preset, driven by the real config value rather than a fixed string pair.

There is no literal "canonical string Python prepends" to flux_prompt in this
codebase — every flux_prompt is entirely Claude-authored text; Python only
injects "Global visual direction:"/"Global image style:" INSTRUCTION lines
into the storyboard generation prompt (system_prompt.py), never writes into
flux_prompt itself. The exemption therefore compares against the channel's
own configured image_style/visual_style VALUE, checked at validation time,
not a hypothetical Python-authored prefix string.
"""

from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard


def _beat(order: int, flux_prompt: str) -> dict:
    return {
        "beat_order": order,
        "flux_prompt": flux_prompt,
        "media_url": "",
        "visual_type": "b-roll",
    }


def _forbidden_issues(beats, image_style="", visual_style=""):
    return [
        i for i in validate_storyboard(beats, image_style=image_style, visual_style=visual_style)
        if i["check"] == "forbidden_flux_word"
    ]


def test_cinematic_cartoon_image_style_exempts_cinematic():
    beats = [_beat(0, (
        "cinematic cartoon illustration of a rough hand-carved mine tunnel "
        "entrance in a reddish rock face, timber support beams framing the "
        "opening"
    ))]
    assert _forbidden_issues(beats, image_style="cinematic_cartoon") == []


def test_cinematic_realism_image_style_exempts_cinematic():
    # A DIFFERENT preset than cinematic_cartoon — the original Task 2e fix
    # hardcoded only "cinematic cartoon"/"cinematic_cartoon" and would still
    # have flagged this one; the generalized version must not.
    beats = [_beat(0, (
        "cinematic realism photograph of a weathered stone bridge at dusk, "
        "shallow depth of field, graded color"
    ))]
    assert _forbidden_issues(beats, image_style="cinematic_realism") == []


def test_bare_cinematic_visual_style_exempts_cinematic():
    # visual_style="cinematic" is a single-token preset with no underscore —
    # the exemption must still work for it, not just multi-word presets.
    beats = [_beat(0, "cinematic wide shot of a courtroom hallway, deliberate low angle")]
    assert _forbidden_issues(beats, visual_style="cinematic") == []


def test_style_word_elsewhere_in_prompt_without_the_configured_phrase_still_flags():
    # The word "cinematic" appears, and image_style is genuinely
    # "cinematic_cartoon", but NEITHER the "cinematic_cartoon" nor
    # "cinematic cartoon" phrase actually appears in this prompt — the
    # exemption requires the real configured phrase to be present, not just
    # the bare word floating anywhere.
    beats = [_beat(0, "a moody atmospheric hallway with cinematic feel and no concrete subject")]
    issues = _forbidden_issues(beats, image_style="cinematic_cartoon")
    assert len(issues) == 1
    assert "cinematic" in issues[0]["description"]


def test_unrelated_channel_style_gets_no_free_pass_on_cinematic():
    # A channel configured with a style that has NOTHING to do with
    # "cinematic" (e.g. anime) must not get an exemption just because some
    # OTHER channel's preset happens to collide with this forbidden word.
    beats = [_beat(0, "a cinematic atmospheric shot with dramatic lighting")]
    issues = _forbidden_issues(beats, image_style="anime", visual_style="documentary")
    assert len(issues) == 1
    assert "cinematic" in issues[0]["description"]


def test_no_style_configured_behaves_exactly_as_before_the_exemption_existed():
    beats = [_beat(0, "a cinematic atmospheric shot with dramatic lighting")]
    issues = _forbidden_issues(beats)  # image_style/visual_style default to ""
    assert len(issues) == 1


def test_real_production_shaped_batch_is_not_flagged_38_of_38():
    """Reproduces the reported real-run shape: nearly every beat of a
    cinematic_cartoon-style channel opens with the style phrase."""
    beats = [
        _beat(i, f"cinematic cartoon illustration of physical scene detail {i}, sharp focus")
        for i in range(38)
    ]
    assert _forbidden_issues(beats, image_style="cinematic_cartoon") == []


def test_other_forbidden_mood_words_are_unaffected_by_style_exemption():
    beats = [_beat(0, "an eerie, haunting corridor with no physical subject named")]
    issues = _forbidden_issues(beats, image_style="cinematic_cartoon")
    assert len(issues) == 1
    assert "eerie" in issues[0]["description"] or "haunting" in issues[0]["description"]
