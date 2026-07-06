"""Runtime proof for deterministic duplicate-prompt repair (audit G-5.3).

Two beats with byte-identical `flux_prompt` text hash to the SAME Flux cache
file within one content's cache directory — a surviving
`flux_prompt_exact_duplicate`/`flux_prompt_near_duplicate`/`near_duplicate_beat`
finding (all MINOR, so storyboard retry never fires for them) is a guaranteed
wasted/duplicate image. `repair_duplicate_flux_prompts()` deterministically
appends a composition-slot variation to the flagged beat before Flux ever
sees it — no randomness, no AI call. Existing media is preserved by default,
but callers that regenerate those beats in the same run can opt in.

Every test here drives the REAL `validate_storyboard()` →
`repair_duplicate_flux_prompts()` chain (and the real
`_repair_duplicate_prompts()` orchestrator wrapper) on plain beat dicts —
nothing internal is stubbed; this path has no external API to stub.
"""

import logging
import unittest
from pathlib import Path

from app.agents.agent4_visuals.services.visual_orchestrator import (
    _repair_duplicate_prompts,
)
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    _COMPOSITION_ROTATION,
    _DUPLICATE_REPAIR_CHECKS,
    composition_slot_variation,
    find_duplicate_prompt_beat_orders,
    repair_duplicate_flux_prompts,
    validate_storyboard,
)


def _beat(order: int, **overrides) -> dict:
    beat = {
        "beat_order": order,
        "section_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": f"narration {order}",
        "visual_intent": f"intent {order}",
        "visual_type": "b-roll",
        "visual_category": "object",
        "environment": "indoor_office",
        "flux_prompt": (
            f"Interior office desk clue {order}, brass handle, documentary "
            "photograph, sharp focus, no readable text"
        ),
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "object",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "",
        "media_type": "image",
    }
    beat.update(overrides)
    return beat


class TestExactDuplicateRepair(unittest.TestCase):
    def test_later_pending_duplicate_is_repaired_earlier_untouched(self):
        # Beats 1 and 2 deliberately carry DIFFERENT environment/motif/effect
        # metadata (only their flux_prompt text is identical) so exactly one
        # check — flux_prompt_exact_duplicate — fires between them; a
        # separate test below covers near_duplicate_beat's own metadata-only
        # convention in isolation.
        beats = [
            _beat(0, environment="forest_nature", motif="exterior",
                  flux_prompt="Ancient forest canopy, wide shot, photorealistic"),
            _beat(1, environment="urban_street", motif="room", effect="pan",
                  flux_prompt="Office desk with a brass lamp, documentary photograph"),
            _beat(2, environment="laboratory", motif="clock", effect="push_in",
                  flux_prompt="Office desk with a brass lamp, documentary photograph"),
        ]
        # Confirm the validator itself flags beat 2 (later occurrence) only.
        targets = find_duplicate_prompt_beat_orders(beats)
        self.assertEqual(targets, {2: "flux_prompt_exact_duplicate"})

        repaired, repairs = repair_duplicate_flux_prompts(beats)

        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["beat_order"], 2)
        self.assertEqual(repairs[0]["check"], "flux_prompt_exact_duplicate")
        self.assertIn(repairs[0]["variation"], _COMPOSITION_ROTATION)

        # Beat 1 (the original) is byte-identical to the input.
        self.assertEqual(repaired[1]["flux_prompt"], beats[1]["flux_prompt"])
        # Beat 2 (the duplicate) now differs and carries the appended variation.
        self.assertNotEqual(repaired[2]["flux_prompt"], beats[2]["flux_prompt"])
        self.assertTrue(repaired[2]["flux_prompt"].startswith(beats[2]["flux_prompt"]))
        self.assertIn(repairs[0]["variation"], repaired[2]["flux_prompt"])

        # Post-repair, the exact-duplicate finding for beat 2 is gone.
        post_targets = find_duplicate_prompt_beat_orders(repaired)
        self.assertNotIn(2, post_targets)

        # Original list is untouched (no in-place mutation).
        self.assertEqual(beats[2]["flux_prompt"], "Office desk with a brass lamp, documentary photograph")

    def test_no_duplicates_returns_same_list_unchanged(self):
        beats = [
            _beat(0, flux_prompt="Ancient forest canopy, wide shot, photorealistic",
                  environment="forest_nature"),
            _beat(1, flux_prompt="Empty laboratory bench, overhead light, photorealistic",
                  environment="laboratory"),
        ]
        repaired, repairs = repair_duplicate_flux_prompts(beats)
        self.assertEqual(repairs, [])
        self.assertIs(repaired, beats)


class TestReusedBeatsNeverTouched(unittest.TestCase):
    def test_already_resolved_duplicate_is_skipped_not_repaired(self):
        # Beat 1 already has a resolved media_url (deliberate parent-image
        # reuse on the child remap path) and duplicates pending beat 2's
        # prompt. The check flags beat 2 (the later occurrence) — since beat
        # 2 is pending, it gets repaired regardless of beat 1's reuse status.
        beats = [
            _beat(0, environment="forest_nature", motif="exterior",
                  flux_prompt="Ancient forest canopy, wide shot, photorealistic"),
            _beat(1, media_url="cache/parent-id/abc123.jpg",
                  flux_prompt="Office desk with a brass lamp, documentary photograph"),
            _beat(2, flux_prompt="Office desk with a brass lamp, documentary photograph"),
        ]
        repaired, repairs = repair_duplicate_flux_prompts(beats)
        self.assertEqual([r["beat_order"] for r in repairs], [2])
        # Beat 1 (reused) is never mutated, even though it shares beat 2's text.
        self.assertEqual(repaired[1]["flux_prompt"], beats[1]["flux_prompt"])
        self.assertEqual(repaired[1]["media_url"], "cache/parent-id/abc123.jpg")

    def test_flagged_beat_itself_already_resolved_is_left_alone_by_default(self):
        # If the FLAGGED beat_order itself already has media (e.g. a
        # flux-incomplete recovery pass where some beats already succeeded in
        # a prior attempt), it must not be rewritten — its image already
        # exists and its prompt is now purely descriptive metadata.
        beats = [
            _beat(0, environment="forest_nature", motif="exterior",
                  flux_prompt="Ancient forest canopy, wide shot, photorealistic"),
            _beat(1, environment="urban_street", motif="room", effect="pan",
                  flux_prompt="Office desk with a brass lamp, documentary photograph"),
            _beat(2, environment="laboratory", motif="clock", effect="push_in",
                  media_url="cache/content-id/def456.jpg",
                  flux_prompt="Office desk with a brass lamp, documentary photograph"),
        ]
        repaired, repairs = repair_duplicate_flux_prompts(beats)
        self.assertEqual(repairs, [])
        self.assertEqual(repaired[2]["flux_prompt"], beats[2]["flux_prompt"])

    def test_flagged_resolved_beat_can_be_repaired_when_regenerated_same_run(self):
        # Child Short portrait generation clears media_url and regenerates every
        # beat after this repair pass. In that path, a duplicate that is
        # currently "resolved" must still receive the append-only variation.
        beats = [
            _beat(0, environment="forest_nature", motif="exterior",
                  flux_prompt="Ancient forest canopy, wide shot, photorealistic"),
            _beat(1, environment="urban_street", motif="room", effect="pan",
                  flux_prompt="Office desk with a brass lamp, documentary photograph"),
            _beat(2, environment="laboratory", motif="clock", effect="push_in",
                  media_url="cache/content-id/def456.jpg",
                  flux_prompt="Office desk with a brass lamp, documentary photograph"),
        ]
        repaired, repairs = repair_duplicate_flux_prompts(beats, include_resolved=True)
        self.assertEqual([r["beat_order"] for r in repairs], [2])
        self.assertNotEqual(repaired[2]["flux_prompt"], beats[2]["flux_prompt"])
        self.assertIn(composition_slot_variation(0), repaired[2]["flux_prompt"])



class TestNearDuplicateBeatRepair(unittest.TestCase):

    def test_near_duplicate_scans_beyond_two_positions_for_short_wide_repeats(self):
        beats = [
            _beat(0, flux_prompt="Office desk first view",
                  environment="indoor_office", motif="object", effect="cut"),
            _beat(1, flux_prompt="Forest path",
                  environment="forest_nature", motif="exterior", effect="pan"),
            _beat(2, flux_prompt="Laboratory bench",
                  environment="laboratory", motif="clock", effect="push_in"),
            _beat(3, flux_prompt="Office desk distant repeat",
                  environment="indoor_office", motif="object", effect="cut"),
        ]
        targets = find_duplicate_prompt_beat_orders(beats)
        self.assertEqual(targets, {0: "near_duplicate_beat"})

    def test_metadata_near_duplicate_pair_gets_repaired(self):
        # near_duplicate_beat flags the EARLIER of a close pair sharing
        # environment+motif+effect (opposite convention from the flux_prompt
        # checks) — confirm the repair targets exactly what the check flags.
        beats = [
            _beat(0, flux_prompt="Office desk with scattered papers, photorealistic",
                  environment="indoor_office", motif="object", effect="cut"),
            _beat(1, flux_prompt="Office desk with a stapler, photorealistic",
                  environment="indoor_office", motif="object", effect="cut"),
        ]
        targets = find_duplicate_prompt_beat_orders(beats)
        self.assertEqual(targets, {0: "near_duplicate_beat"})

        repaired, repairs = repair_duplicate_flux_prompts(beats)
        self.assertEqual([r["beat_order"] for r in repairs], [0])
        self.assertNotEqual(repaired[0]["flux_prompt"], beats[0]["flux_prompt"])
        self.assertEqual(repaired[1]["flux_prompt"], beats[1]["flux_prompt"])


class TestDeterminismAndRotation(unittest.TestCase):
    def test_repeated_runs_produce_byte_identical_output(self):
        beats = [
            _beat(0, environment="forest_nature", motif="exterior",
                  flux_prompt="Ancient forest canopy, wide shot, photorealistic"),
            _beat(1, flux_prompt="Office desk with a brass lamp, documentary photograph"),
            _beat(2, flux_prompt="Office desk with a brass lamp, documentary photograph"),
        ]
        first_repaired, _ = repair_duplicate_flux_prompts(beats)
        second_repaired, _ = repair_duplicate_flux_prompts(beats)
        self.assertEqual(first_repaired[2]["flux_prompt"], second_repaired[2]["flux_prompt"])

    def test_multiple_duplicates_get_distinct_variations(self):
        # Four beats, distinct environment/motif/effect metadata (isolating
        # the flux_prompt-text checks from near_duplicate_beat): beats 1, 2,
        # and 3 all share one prompt with beat 1 as the first ("anchor")
        # occurrence. Beats 2 and 3 must receive DIFFERENT rotation phrases
        # from each other so they do not collide into a fresh duplicate.
        shared_prompt = "Office desk with a brass lamp, documentary photograph"
        beats = [
            _beat(0, environment="forest_nature", motif="exterior", effect="cut",
                  flux_prompt="Ancient forest canopy, wide shot, photorealistic"),
            _beat(1, environment="urban_street", motif="room", effect="pan",
                  flux_prompt=shared_prompt),
            _beat(2, environment="laboratory", motif="clock", effect="push_in",
                  flux_prompt=shared_prompt),
            _beat(3, environment="vehicle", motif="phone", effect="zoom_out",
                  flux_prompt=shared_prompt),
        ]
        repaired, repairs = repair_duplicate_flux_prompts(beats)
        self.assertEqual({r["beat_order"] for r in repairs}, {2, 3})
        variations = [r["variation"] for r in repairs]
        self.assertEqual(len(variations), len(set(variations)))
        self.assertNotEqual(repaired[2]["flux_prompt"], repaired[3]["flux_prompt"])

    def test_rotation_phrases_contain_no_forbidden_mood_words(self):
        # The repair must never reintroduce a forbidden_flux_word MAJOR.
        from app.agents.agent4_visuals.subagents.storyboard_validator import (
            FORBIDDEN_FLUX_WORDS,
        )
        for phrase in _COMPOSITION_ROTATION:
            words = set(phrase.lower().replace(",", " ").split())
            self.assertFalse(words & FORBIDDEN_FLUX_WORDS, phrase)


class TestOrchestratorWrapper(unittest.TestCase):
    """The shared `_repair_duplicate_prompts()` call site used by all three
    integration points (parent fresh generation, parent flux-incomplete
    recovery, child remap) — verified directly, without needing a DB/Content
    fixture, since it is a pure wrapper around the validator functions plus
    logging."""

    def test_wrapper_logs_and_returns_repaired_list(self):
        import uuid
        beats = [
            _beat(0, environment="forest_nature", motif="exterior",
                  flux_prompt="Ancient forest canopy, wide shot, photorealistic"),
            _beat(1, flux_prompt="Office desk with a brass lamp, documentary photograph"),
            _beat(2, flux_prompt="Office desk with a brass lamp, documentary photograph"),
        ]
        content_id = uuid.uuid4()
        with self.assertLogs(
            "app.agents.agent4_visuals.services.visual_orchestrator", level="WARNING"
        ) as logs:
            result = _repair_duplicate_prompts(beats, content_id=content_id, language="en")

        self.assertNotEqual(result[2]["flux_prompt"], beats[2]["flux_prompt"])
        self.assertTrue(
            any("DUPLICATE_PROMPT_REPAIRED" in m and str(content_id) in m and "beat_order=2" in m
                for m in logs.output),
            logs.output,
        )

    def test_wrapper_clean_case_logs_ok(self):
        import uuid
        beats = [
            _beat(0, flux_prompt="Ancient forest canopy, wide shot, photorealistic",
                  environment="forest_nature"),
            _beat(1, flux_prompt="Empty laboratory bench, overhead light, photorealistic",
                  environment="laboratory"),
        ]
        content_id = uuid.uuid4()
        with self.assertLogs(
            "app.agents.agent4_visuals.services.visual_orchestrator", level="INFO"
        ) as logs:
            result = _repair_duplicate_prompts(beats, content_id=content_id)
        self.assertEqual(result, beats)
        self.assertTrue(any("DUPLICATE_PROMPT_REPAIR_OK" in m for m in logs.output))


class TestStaticSurface(unittest.TestCase):
    def test_repair_checks_match_the_three_named_findings(self):
        self.assertEqual(
            _DUPLICATE_REPAIR_CHECKS,
            frozenset({
                "flux_prompt_exact_duplicate",
                "flux_prompt_near_duplicate",
                "near_duplicate_beat",
            }),
        )

    def test_rotation_has_24_distinct_combinations(self):
        self.assertEqual(len(_COMPOSITION_ROTATION), 24)
        self.assertEqual(len(set(_COMPOSITION_ROTATION)), 24)

    def test_orchestrator_call_sites_present(self):
        src = Path("app/agents/agent4_visuals/services/visual_orchestrator.py").read_text()
        self.assertEqual(src.count("_repair_duplicate_prompts("), 4)  # def + 3 call sites


if __name__ == "__main__":
    unittest.main()
