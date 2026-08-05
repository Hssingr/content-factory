"""Runtime proof for Python-owned verbatim recurring-character descriptors."""

from unittest.mock import patch

from app.agents.agent2_discovery import system_prompt as agent2_prompt
from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.services.visual_orchestrator import (
    _inject_character_descriptors,
)
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    repair_duplicate_flux_prompts,
)


MARA = {
    "name": "Mara Voss",
    "apparent_age_band": "mid-30s",
    "build": "lean athletic build",
    "hair_color": "black",
    "hair_length": "chin-length",
    "hair_style": "side-parted waves",
    "facial_hair": "none",
    "distinguishing_facial_features": "a narrow face and a scar through the left eyebrow",
    "clothing_garment": "wool field coat",
    "clothing_colors": "forest green and charcoal",
    "signature_accessory": "a brass compass on a brown leather cord",
}
BLUEPRINT = {"character_descriptors": [MARA]}
CANONICAL = flux_generator.canonical_character_descriptor(MARA)


def _person(order: int, action: str) -> dict:
    return {
        "beat_order": order,
        "visual_category": "person",
        "visual_intent": f"Mara Voss, young woman with cropped hair, {action}",
        "flux_prompt": (
            "cinematic cartoon illustration, bold clean outlines, Mara Voss, "
            f"young woman with cropped blond hair and a red dress, {action}"
        ),
        "environment": "other",
    }


def test_schema_requires_the_complete_fixed_feature_set():
    item = agent2_prompt._STORY_BLUEPRINT_SCHEMA["properties"]["character_descriptors"]["items"]
    assert set(item["required"]) == {
        "name", "apparent_age_band", "build", "hair_color", "hair_length", "hair_style",
        "facial_hair", "distinguishing_facial_features", "clothing_garment",
        "clothing_colors", "signature_accessory",
    }


def test_multiple_person_beats_receive_byte_identical_canonical_substrings():
    beats = _inject_character_descriptors(
        [_person(0, "opens the archive door"), _person(1, "runs across the courtyard")],
        BLUEPRINT,
    )
    assert all(beat["flux_prompt"].count(CANONICAL) == 1 for beat in beats)
    assert all(beat["flux_prompt"].startswith("cinematic cartoon illustration, bold clean outlines,") for beat in beats)
    assert all("cropped blond hair" not in beat["flux_prompt"] for beat in beats)
    assert beats[0][flux_generator.CANONICAL_CHARACTER_DESCRIPTOR_KEY][0].encode() == \
           beats[1][flux_generator.CANONICAL_CHARACTER_DESCRIPTOR_KEY][0].encode()


def test_descriptor_survives_sanitizer_defect_rewrite_and_duplicate_repair():
    beat = _person(2, "sorts a case file")
    beat["motif"] = "document"
    beat = _inject_character_descriptors([beat], BLUEPRINT)[0]

    sanitized = flux_generator.derive_text_prop_prompt(beat)
    assert CANONICAL in sanitized

    rewritten = flux_generator._build_defect_rewrite_prompt(beat, 2)
    assert CANONICAL in rewritten
    assert rewritten.startswith("cinematic cartoon illustration, bold clean outlines,")

    beat["flux_prompt"] = sanitized
    repaired, repairs = repair_duplicate_flux_prompts(
        [beat],
        issues=[{"check": "flux_prompt_exact_duplicate", "beat_order": 2}],
    )
    assert repairs
    assert repaired[0]["flux_prompt"].count(CANONICAL) == 1


def test_no_recurring_character_injects_nothing():
    original = _person(0, "opens the archive door")
    before = dict(original)
    result = _inject_character_descriptors([original], {"character_descriptors": []})
    assert result[0] == before
    assert flux_generator.CANONICAL_CHARACTER_DESCRIPTOR_KEY not in result[0]


def test_every_person_beat_generates_its_own_image_after_anchor_retirement():
    beats = _inject_character_descriptors(
        [_person(0, "opens a door"), _person(1, "crosses a room"), _person(2, "raises a lamp")],
        BLUEPRINT,
    )
    calls = []

    def generate(beat, *_args, **_kwargs):
        calls.append(beat["beat_order"])
        return f"cache/cid/{beat['beat_order']}.jpg"

    with (
        patch.object(flux_generator, "generate_beat_image_with_routing", side_effect=generate),
        patch.object(flux_generator, "_dedupe_generated_image_once", side_effect=lambda _b, path, *_a, **_k: path),
        patch.object(flux_generator, "_ensure_beat_image_healthy", side_effect=lambda _b, path, *_a, **_k: path),
        patch.object(flux_generator, "fill_failed_beats_from_neighbors", return_value=0),
        patch.object(flux_generator.time, "sleep"),
    ):
        generated = flux_generator.generate_all_beat_images(beats, "cid", width=1080, height=1920)

    assert calls == [0, 1, 2]
    assert len({beat["media_url"] for beat in generated}) == 3
