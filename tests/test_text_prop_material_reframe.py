"""Phase A3.1: text-bearing concepts become physical-detail prompts."""

import pytest

from app.agents.agent4_visuals.services.flux_generator import derive_text_prop_prompt


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("signed document", "wax seal"),
        ("price board", "chalk dust"),
        ("street sign", "mounting hardware"),
        ("phone screen", "screen glow"),
        ("newspaper headline", "newsprint texture"),
        ("map caption", "creased map paper"),
    ],
)
def test_text_object_classes_are_reframed_with_one_consolidated_clause(subject, expected):
    result = derive_text_prop_prompt({
        "flux_prompt": f"cinematic cartoon illustration, bold clean outlines, Straight-on view of a {subject}",
        "visual_intent": subject,
        "motif": "document",
    })
    assert expected in result
    assert result.startswith("cinematic cartoon illustration, bold clean outlines,")


@pytest.mark.parametrize("subject", ["checklist", "book cover", "storefront signage", "banknote"])
def test_added_object_classes_are_materially_reframed(subject):
    result = derive_text_prop_prompt({"flux_prompt": subject, "visual_intent": subject})
    assert result != "material texture, edges, and hands interacting with the object in close-up"


def test_rule_vocabulary_cannot_leak_into_rewritten_flux_prompt():
    result = derive_text_prop_prompt({
        "beat_order": 4,
        "flux_prompt": "watercolor illustration, muted palette, a document, title card, no readable text",
        "visual_intent": "Make a title card with legible words; prompt rule says no readable text",
    })
    lowered = result.lower()
    for forbidden in ("title card", "title-card", "no readable text", "legible words", "prompt rule"):
        assert forbidden not in lowered
    assert result.startswith("watercolor illustration, muted palette,")


def test_material_variant_rotation_is_deterministic_and_varied():
    prompts = [derive_text_prop_prompt({
        "beat_order": order,
        "flux_prompt": "oil painting, signed document",
        "visual_intent": "document",
    }) for order in range(3)]
    assert len(set(prompts)) == 3
    assert prompts == [derive_text_prop_prompt({
        "beat_order": order,
        "flux_prompt": "oil painting, signed document",
        "visual_intent": "document",
    }) for order in range(3)]
