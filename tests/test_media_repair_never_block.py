"""Runtime proof: pixel-quality media findings are FIXED and retried, never
content-blocking (operator policy — "never block the flow; if anything was
detected we fix and retry").

Real incident (content 41f7eeb8): the Tier 5 letterbox detector flagged 3
baked-in-border images as BLOCKING and the orchestrator's only response was
Content.status = "FAILED" — no repair path existed anywhere.

Covers:
1. detectors (real PIL images — letterboxed / near-black / clean),
2. flux_generator._reroll_if_letterboxed_once() — generation-time gate
   (one reroll with the full-bleed clause, then neighbor-fill hand-off),
3. visual_orchestrator._repair_flagged_media() — validation-time repair
   (regenerate → verify → neighbor fallback; updates the generation_prompt
   JSON, the ONLY place media_url lives),
4. the orchestrator decision: repairable-only findings never set FAILED;
   structural findings still do.

No live API calls (CLAUDE.md §19.1): the only stubbed boundaries are the
paid fal.ai call (generate_beat_image / the routing wrapper) and, in the decision
tests, the paid visual pass itself.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.services import media_validation as mv
from app.agents.agent4_visuals.services import visual_orchestrator as vo
from app.config import settings
from app.models import AudioFile, Channel, ChannelConfig, Content, Script, VideoSection


def _write_image(path: Path, kind: str, size=(640, 360)) -> None:
    """kind: 'good' (uniform mid-gray), 'letterboxed' (bright center, black
    horizontal bands), 'dark' (near-black full frame)."""
    w, h = size
    if kind == "good":
        img = Image.new("L", size, 140)
    elif kind == "dark":
        img = Image.new("L", size, 5)
    else:  # letterboxed
        img = Image.new("L", size, 0)
        band = round(h * 0.12)
        img.paste(140, (0, band, w, h - band))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, "JPEG", quality=90)


# ── 1. Detectors on real pixels ──────────────────────────────────────────────

class PixelDefectDetectorTest(unittest.TestCase):
    def test_letterboxed_image_detected(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "lb.jpg"
            _write_image(p, "letterboxed")
            found = mv.detect_image_letterbox(p)
            self.assertIsNotNone(found)
            self.assertEqual(found[0], "letterboxed_image")

    def test_clean_and_dark_full_frame_are_not_letterboxed(self):
        with TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.jpg"
            dark = Path(tmp) / "dark.jpg"
            _write_image(good, "good")
            _write_image(dark, "dark")
            self.assertIsNone(mv.detect_image_letterbox(good))
            # A uniformly dark frame has no bright center — near-black owns it.
            self.assertIsNone(mv.detect_image_letterbox(dark))

    def test_combined_detector_returns_each_code(self):
        with TemporaryDirectory() as tmp:
            for kind, expected in (("good", None), ("dark", "near_black_image"),
                                   ("letterboxed", "letterboxed_image")):
                p = Path(tmp) / f"{kind}.jpg"
                _write_image(p, kind)
                found = mv.detect_image_pixel_defect(p)
                if expected is None:
                    self.assertIsNone(found)
                else:
                    self.assertEqual(found[0], expected)


# ── 2. Unified generation-time health gate (letterbox class) ─────────────────

class LetterboxHealthGateTest(unittest.TestCase):
    def _beat(self):
        return {
            "beat_order": 7, "flux_prompt": "a stone bridge over a river",
            "visual_intent": "an old stone bridge spanning a rushing river",
            "environment": "open_landscape", "media_url": "",
        }

    def test_clean_image_passes_without_regeneration(self):
        with TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "cache" / "ok.jpg", "good")
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(flux_generator, "generate_beat_image") as mock_gen,
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    self._beat(), "cache/ok.jpg", "cid", width=1920, height=1080,
                )
        self.assertEqual(out, "cache/ok.jpg")
        mock_gen.assert_not_called()

    def test_letterboxed_image_healed_on_attempt_1_with_full_bleed_clause(self):
        with TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "cache" / "lb.jpg", "letterboxed")
            _write_image(Path(tmp) / "cache" / "fixed.jpg", "good")
            beat = self._beat()
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(
                    flux_generator, "generate_beat_image",
                    return_value="cache/fixed.jpg",
                ) as mock_gen,
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    beat, "cache/lb.jpg", "cid", width=1920, height=1080,
                )
        self.assertEqual(out, "cache/fixed.jpg")
        self.assertEqual(mock_gen.call_count, 1)
        prompt = mock_gen.call_args.args[0]
        self.assertIn("no black bars", prompt)
        self.assertIn("a stone bridge", prompt)
        self.assertIn("heal1:letterboxed_image", mock_gen.call_args.kwargs["cache_key_extra"])
        self.assertEqual(beat["flux_prompt"], prompt)

    def test_attempt_2_is_a_prompt_rewrite_not_the_same_prompt(self):
        """Operator preference: before neighbor duplication, try a genuinely
        DIFFERENT image request — visual_intent + composition variation +
        both corrective clauses."""
        with TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "cache" / "lb.jpg", "letterboxed")
            _write_image(Path(tmp) / "cache" / "lb2.jpg", "letterboxed")
            _write_image(Path(tmp) / "cache" / "fixed.jpg", "good")
            beat = self._beat()
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(
                    flux_generator, "generate_beat_image",
                    side_effect=["cache/lb2.jpg", "cache/fixed.jpg"],
                ) as mock_gen,
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    beat, "cache/lb.jpg", "cid", width=1920, height=1080,
                )
        self.assertEqual(out, "cache/fixed.jpg")
        self.assertEqual(mock_gen.call_count, 2)
        rewrite_prompt = mock_gen.call_args_list[1].args[0]
        self.assertIn("an old stone bridge", rewrite_prompt)   # visual_intent base
        self.assertIn("no black bars", rewrite_prompt)          # full-bleed clause
        self.assertIn("well-lit", rewrite_prompt)               # well-lit clause
        self.assertNotEqual(rewrite_prompt, mock_gen.call_args_list[0].args[0])
        self.assertIn("heal2:", mock_gen.call_args_list[1].kwargs["cache_key_extra"])

    def test_both_attempts_failing_hands_off_to_neighbor_fill(self):
        with TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "cache" / "lb.jpg", "letterboxed")
            _write_image(Path(tmp) / "cache" / "lb2.jpg", "letterboxed")
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(
                    flux_generator, "generate_beat_image",
                    return_value="cache/lb2.jpg",
                ) as mock_gen,
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    self._beat(), "cache/lb.jpg", "cid", width=1920, height=1080,
                )
        self.assertEqual(out, "", "must hand off to neighbor-fill, never ship letterbox")
        self.assertEqual(mock_gen.call_count, 2)

    def test_generation_failures_hand_off_to_neighbor_fill(self):
        with TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "cache" / "lb.jpg", "letterboxed")
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(flux_generator, "generate_beat_image", return_value=""),
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    self._beat(), "cache/lb.jpg", "cid", width=1920, height=1080,
                )
        self.assertEqual(out, "")

    def test_cross_defect_recheck_catches_a_dark_replacement(self):
        """The former gates never re-checked a replacement for OTHER defect
        classes — a letterbox heal that comes back near-black must not ship."""
        with TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "cache" / "lb.jpg", "letterboxed")
            _write_image(Path(tmp) / "cache" / "dark.jpg", "dark")
            _write_image(Path(tmp) / "cache" / "fixed.jpg", "good")
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(
                    flux_generator, "generate_beat_image",
                    side_effect=["cache/dark.jpg", "cache/fixed.jpg"],
                ) as mock_gen,
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    self._beat(), "cache/lb.jpg", "cid", width=1920, height=1080,
                )
        self.assertEqual(out, "cache/fixed.jpg")
        self.assertEqual(mock_gen.call_count, 2)


# ── Fake DB (established _FakeQuery/_FakeDb pattern) ─────────────────────────

class _FakeQuery:
    def __init__(self, table):
        self._table = table

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._table)


class _FakeDb:
    def __init__(self):
        self.video_sections: list[VideoSection] = []
        self.scripts: list[Script] = []
        self.audio_files: list[AudioFile] = []
        self.objects: dict = {}
        self.commit_count = 0

    def query(self, model):
        if model is VideoSection:
            return _FakeQuery(self.video_sections)
        if model is Script:
            return _FakeQuery(self.scripts)
        if model is AudioFile:
            return _FakeQuery(self.audio_files)
        return _FakeQuery([])

    def get(self, model, key):
        return self.objects.get((model, str(key)))

    def commit(self):
        self.commit_count += 1


def _section(content_id, language, order, media_url, prompt="ancient bridge") -> VideoSection:
    row = VideoSection(
        content_id=content_id, language=language, section_order=order,
        flux_prompt=prompt, beat_intensity="medium",
        generation_prompt=json.dumps({"media_url": media_url, "environment": "other"}),
    )
    return row


# ── 3. Validation-time repair pass ───────────────────────────────────────────

class RepairFlaggedMediaTest(unittest.TestCase):
    def _setup(self, tmp: Path, cid):
        db = _FakeDb()
        for lang in ("en", "__visual__"):
            for order, name in ((0, "a.jpg"), (1, "b.jpg"), (2, "bad.jpg"), (3, "c.jpg")):
                db.video_sections.append(_section(cid, lang, order, f"cache/{name}"))
        for name, kind in (("a.jpg", "good"), ("b.jpg", "good"),
                           ("bad.jpg", "letterboxed"), ("c.jpg", "good")):
            _write_image(tmp / "cache" / name, kind)
        return db

    def _issue(self, code="letterboxed_image", media="cache/bad.jpg"):
        return mv.MediaValidationIssue(
            severity="BLOCKING", code=code, section_order=2,
            language="en", message="test", media_path=media,
        )

    def test_regeneration_repairs_every_language_row(self):
        cid = uuid.uuid4()
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = self._setup(tmp, cid)
            _write_image(tmp / "cache" / "fixed.jpg", "good")
            content = SimpleNamespace(is_short_episode=False)
            with (
                patch.object(settings, "media_path", str(tmp)),
                patch.object(
                    vo, "generate_beat_image", return_value="cache/fixed.jpg",
                ) as mock_gen,
            ):
                repaired = vo._repair_flagged_media(cid, content, [self._issue()], db)

        self.assertEqual(repaired, 1)
        self.assertEqual(mock_gen.call_args.kwargs["width"], 1920)
        self.assertEqual(mock_gen.call_args.kwargs["height"], 1080)
        prompt = mock_gen.call_args.args[0]
        self.assertIn("no black bars", prompt)
        self.assertIn("media_repair:letterboxed_image", mock_gen.call_args.kwargs["cache_key_extra"])
        # BOTH language rows re-pointed, via the generation_prompt JSON.
        updated = [
            json.loads(r.generation_prompt)["media_url"]
            for r in db.video_sections if r.section_order == 2
        ]
        self.assertEqual(updated, ["cache/fixed.jpg", "cache/fixed.jpg"])
        # Untouched rows keep their media.
        kept = json.loads(db.video_sections[0].generation_prompt)["media_url"]
        self.assertEqual(kept, "cache/a.jpg")
        self.assertGreaterEqual(db.commit_count, 1)

    def test_still_defective_regeneration_falls_back_to_nearest_neighbor(self):
        cid = uuid.uuid4()
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = self._setup(tmp, cid)
            _write_image(tmp / "cache" / "still_bad.jpg", "letterboxed")
            content = SimpleNamespace(is_short_episode=False)
            with (
                patch.object(settings, "media_path", str(tmp)),
                patch.object(
                    vo, "generate_beat_image", return_value="cache/still_bad.jpg",
                ),
            ):
                repaired = vo._repair_flagged_media(cid, content, [self._issue()], db)

        self.assertEqual(repaired, 1)
        updated = {
            json.loads(r.generation_prompt)["media_url"]
            for r in db.video_sections if r.section_order == 2
        }
        # Nearest good section to 2 is 1 (earlier preferred on ties) → b.jpg.
        self.assertEqual(updated, {"cache/b.jpg"})

    def test_near_black_code_uses_well_lit_clause_and_short_uses_portrait(self):
        cid = uuid.uuid4()
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = self._setup(tmp, cid)
            _write_image(tmp / "cache" / "fixed.jpg", "good")
            content = SimpleNamespace(is_short_episode=True)
            with (
                patch.object(settings, "media_path", str(tmp)),
                patch.object(
                    vo, "generate_beat_image", return_value="cache/fixed.jpg",
                ) as mock_gen,
            ):
                vo._repair_flagged_media(
                    cid, content, [self._issue(code="near_black_image")], db,
                )
        prompt = mock_gen.call_args.args[0]
        self.assertIn("well-lit", prompt)
        self.assertEqual(mock_gen.call_args.kwargs["width"], 1080)
        self.assertEqual(mock_gen.call_args.kwargs["height"], 1920)


# ── 4. Orchestrator decision: repairable never blocks; structural still does ─

def _validation(passed, issues=(), checked=10):
    return SimpleNamespace(
        passed=passed, blocking_issues=list(issues), warnings=[], checked_count=checked,
    )


class NeverBlockDecisionTest(unittest.TestCase):
    def _drive(self, validations, repair_return=1):
        """Run the real run_visual_generation_for_content() decision path with
        the paid visual pass + validator + repair stubbed."""
        cid = uuid.uuid4()
        db = _FakeDb()
        channel_id = uuid.uuid4()
        content = Content(status="AUDIO_DONE", channel_id=channel_id, is_short_episode=False)
        channel = Channel(id=channel_id)
        db.objects[(Content, str(cid))] = content
        db.objects[(Channel, str(channel_id))] = channel
        db.scripts.append(Script(language="en", validated=True))

        with (
            patch.object(vo, "ensure_run_dirs"),
            patch.object(vo, "run_visual_generation", return_value={
                "status": "PARENT_VISUALS_DONE", "beats_by_lang": {"en": []},
            }),
            patch.object(vo, "validate_visual_media_assets", side_effect=validations),
            patch.object(vo, "_repair_flagged_media", return_value=repair_return) as mock_repair,
            patch.object(vo, "generate_visual_review_html", return_value="x"),
        ):
            ok = vo.run_visual_generation_for_content(cid, db)
        return ok, content, mock_repair

    def test_repairable_findings_are_repaired_and_content_proceeds(self):
        lb = mv.MediaValidationIssue("BLOCKING", "letterboxed_image", 2, "en", "m", "cache/x.jpg")
        ok, content, mock_repair = self._drive(
            [_validation(False, [lb]), _validation(True)],
        )
        self.assertTrue(ok)
        self.assertEqual(content.status, "PARENT_VISUALS_DONE")
        mock_repair.assert_called_once()

    def test_unrepaired_quality_findings_still_proceed_never_failed(self):
        lb = mv.MediaValidationIssue("BLOCKING", "letterboxed_image", 2, "en", "m", "cache/x.jpg")
        ok, content, _ = self._drive(
            [_validation(False, [lb]), _validation(False, [lb])],
        )
        self.assertTrue(ok, "pixel-quality findings must never block the flow")
        self.assertEqual(content.status, "PARENT_VISUALS_DONE")

    def test_structural_findings_still_fail_the_content(self):
        missing = mv.MediaValidationIssue("BLOCKING", "missing_file", 2, "en", "m", "cache/x.jpg")
        ok, content, mock_repair = self._drive([_validation(False, [missing])])
        self.assertFalse(ok)
        self.assertEqual(content.status, "FAILED")
        mock_repair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
