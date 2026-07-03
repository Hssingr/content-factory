import unittest

from app.agents.agent4_visuals.services.cinematic_prompts import (
    apply_cinematic_prompts_to_beats,
    build_cinematic_flux_prompt,
    inspect_flux_prompt_quality,
)


def bible():
    return {
        "config_context": {
            "visual_style": "documentary horror",
            "image_style": "cinematic photorealistic",
            "video_color_grade": "cool blue-gray",
        },
        "global_style": {
            "realism_level": "photorealistic",
            "visual_style": "documentary horror",
            "image_style": "cinematic photorealistic",
            "color_grade": "cool blue-gray",
            "lighting_rules": ["cold moonlight", "deep practical shadows"],
            "camera_language": ["slow push-in", "locked-off tense frame"],
            "lens_style": "35mm lens, shallow depth of field",
            "composition_rules": ["foreground obstruction", "clear silhouette"],
        },
        "characters": [{
            "name": "Eli",
            "role": "frightened teenage boy",
            "appearance": "pale face and dark curls",
            "clothing": "oversized gray hoodie",
            "body_language": "frozen hand near the door",
            "continuity_tags": ["eli-gray-hoodie"],
        }],
        "locations": [{
            "name": "upstairs hallway",
            "description": "narrow hallway with peeling wallpaper",
            "time_of_day": "night",
            "lighting": "cold blue moonlight through the bedroom door",
            "color_palette": "muted blue-gray",
            "recurring_details": ["half-open bedroom door"],
            "continuity_tags": ["upstairs-hallway-blue-gray"],
        }],
        "recurring_motifs": [{
            "name": "half-open door",
            "visual_description": "bedroom door open one hand width",
            "symbolic_role": "threat entering ordinary space",
            "usage_rule": "use at dread turns",
        }],
        "negative_prompt_rules": ["no readable text", "no logos"],
        "first_15_seconds_rules": ["open with a curiosity gap", "show the hallway geography"],
        "forbidden_generic_shots": ["generic screaming face", "random cemetery", "floating skull"],
    }


def beat(**extra):
    data = {
        "beat_order": 0,
        "audio_start_ms": 3000,
        "script_text": "Eli stands in the upstairs hallway beside the half-open door.",
        "visual_intent": "Eli frozen in dread near the half-open bedroom door",
        "visual_category": "person",
        "visual_type": "action",
        "environment": "upstairs hallway",
        "beat_intensity": "high",
    }
    data.update(extra)
    return data


class TestCinematicFluxPrompts(unittest.TestCase):
    def test_builds_cinematic_prompt_with_visual_bible(self):
        result = build_cinematic_flux_prompt(
            beat=beat(), narration_excerpt=beat()["script_text"], visual_bible=bible(),
            channel_config_context=bible()["config_context"], content_kind="parent_long_form",
            beat_index=0, total_beats=5,
        )
        prompt = result["flux_prompt"]
        metadata = result["prompt_metadata"]
        self.assertGreater(len(prompt.split()), 55)
        self.assertIn("documentary horror", prompt)
        self.assertIn("oversized gray hoodie", prompt)
        self.assertIn("cold blue moonlight", prompt)
        self.assertIn("eli-gray-hoodie", metadata["continuity_tags"])
        self.assertIn("upstairs-hallway-blue-gray", metadata["continuity_tags"])
        self.assertIn("character:Eli", metadata["visual_bible_refs"])
        self.assertIn("no readable text", result["negative_prompt"])

    def test_first_15_seconds_flag_and_guidance(self):
        result = build_cinematic_flux_prompt(
            beat=beat(audio_start_ms=0), narration_excerpt="Opening hallway dread", visual_bible=bible(),
            channel_config_context=bible()["config_context"], content_kind="parent_long_form",
            beat_index=0, total_beats=5,
        )
        self.assertTrue(result["prompt_metadata"]["is_first_15_seconds"])
        self.assertIn("First 15 seconds", result["flux_prompt"])

    def test_falls_back_without_character_match(self):
        result = build_cinematic_flux_prompt(
            beat=beat(script_text="A locked kitchen window rattles.", visual_intent="locked kitchen window rattling"),
            narration_excerpt="A locked kitchen window rattles.", visual_bible=bible(),
            channel_config_context=bible()["config_context"], content_kind="parent_long_form",
            beat_index=2, total_beats=5,
        )
        self.assertIn("documentary horror", result["flux_prompt"])
        self.assertNotIn("character:Eli", result["prompt_metadata"]["visual_bible_refs"])

    def test_quality_inspector_warns_on_weak_prompt(self):
        issues = inspect_flux_prompt_quality({
            "flux_prompt": "A scary hallway at night.",
            "negative_prompt": "",
            "prompt_metadata": {},
        })
        codes = {issue.code for issue in issues}
        self.assertIn("prompt_too_short", codes)
        self.assertIn("missing_subject", codes)
        self.assertIn("missing_negative_prompt", codes)
        self.assertIn("generic_horror_prompt", codes)

    def test_apply_enriches_beat_with_persistable_metadata_concepts(self):
        enriched = apply_cinematic_prompts_to_beats([beat()], visual_bible=bible(), content_kind="parent_long_form")
        for key in ("negative_prompt", "shot_type", "subject", "action", "emotion", "camera", "lighting", "composition", "continuity_tags", "visual_bible_refs", "is_first_15_seconds", "prompt_quality_warnings"):
            self.assertIn(key, enriched[0])
        self.assertIn("Avoid:", enriched[0]["flux_prompt"])

    def test_child_prompt_uses_parent_bible_continuity(self):
        result = build_cinematic_flux_prompt(
            beat=beat(script_text="Eli whispers from the hallway."), narration_excerpt="Eli whispers from the hallway.",
            visual_bible=bible(), channel_config_context={"source": "parent_visual_bible"},
            content_kind="child_short", beat_index=1, total_beats=3,
        )
        self.assertIn("eli-gray-hoodie", result["prompt_metadata"]["continuity_tags"])
        self.assertIn("documentary horror", result["flux_prompt"])


if __name__ == "__main__":
    unittest.main()
