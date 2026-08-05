"""Phase A2: Solo Short protagonist anchors avoid Flux calls deterministically."""

from unittest.mock import patch

from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.services.visual_orchestrator import (
    _prepend_primary_character_fragment,
    _primary_character_continuity_fragment,
)


PRIMARY = "Mara, adult — short black hair, green coat"


def _person_beat(order: int, prompt: str = PRIMARY):
    return {
        "beat_order": order,
        "visual_category": "person",
        "flux_prompt": f"{prompt}, visible action in a room",
        "environment": "indoor_domestic",
    }


def test_anchor_reuses_twice_then_regenerates_and_cover_is_fresh():
    beats = [_person_beat(i) for i in range(5)]
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
        result = flux_generator.generate_all_beat_images(
            beats, "cid", width=1080, height=1920,
            person_anchor_continuity_line=PRIMARY,
        )

    assert calls == [0, 3]
    assert result[0]["media_url"] == "cache/cid/0.jpg"
    assert result[1]["media_url"] == result[0]["media_url"]
    assert result[2]["media_url"] == result[0]["media_url"]
    assert result[3]["media_url"] == "cache/cid/3.jpg"
    assert result[4]["media_url"] == result[3]["media_url"]


def test_empty_character_descriptors_disable_all_reuse():
    assert _primary_character_continuity_fragment({"character_descriptors": []}) == ""
    beats = [_person_beat(i) for i in range(3)]
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
        flux_generator.generate_all_beat_images(beats, "cid", width=1080, height=1920)

    assert calls == [0, 1, 2]


def test_primary_character_fragment_matches_prompt_contract():
    blueprint = {"character_descriptors": [{
        "name": "Mara", "age": "adult", "description": "short black hair, green coat",
    }]}
    assert _primary_character_continuity_fragment(blueprint) == PRIMARY


def test_fragment_is_prepended_only_to_matching_person_beats():
    blueprint = {"character_descriptors": [{
        "name": "Mara", "age": "adult", "description": "short black hair, green coat",
    }]}
    beats = [
        {"visual_category": "person", "visual_intent": "Mara opens the door", "flux_prompt": "woman opening a door"},
        {"visual_category": "person", "visual_intent": "Jon waits", "flux_prompt": "man waiting"},
        {"visual_category": "place", "visual_intent": "Mara's house", "flux_prompt": "house exterior"},
    ]
    fragment = _prepend_primary_character_fragment(beats, blueprint)
    assert fragment == PRIMARY
    assert beats[0]["flux_prompt"].startswith(f"{PRIMARY}, ")
    assert PRIMARY not in beats[1]["flux_prompt"]
    assert PRIMARY not in beats[2]["flux_prompt"]

