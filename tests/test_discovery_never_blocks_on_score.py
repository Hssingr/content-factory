"""Runtime proof: the story-gate score never blocks ``run_discovery()`` —
an explicit operator decision that supersedes the earlier "auto-reject"
design (and, before that, the earlier "notify but still block" design).

The score is computed exactly as before, still logged, still available for
the operator to see — but ``run_discovery()`` always persists ``Content`` +
``ContentValidation`` and returns normally for ANY candidate that clears
dedup + the source-material floor, regardless of the score-gate verdict.
The human decides via the existing Telegram APPROVE/CHANGE flow, which now
always has the score attached (``build_telegram_message()``).

Only the paid boundaries (``generate_story_premise``, ``score_story_for_gate``)
are stubbed — the real ``run_discovery()``, ``score_story_assessment()``,
``decide_story_acceptance()``, and ``build_telegram_message()`` run
unmodified against a fake DB (CLAUDE.md §19.4).

No live external API calls anywhere in this file (CLAUDE.md §19.1).
"""

from __future__ import annotations

import unittest
import uuid
from unittest.mock import patch

from app.agents.agent2_discovery.services import discovery as discovery_mod
from app.agents.agent2_discovery.services.discovery import run_discovery
from app.agents.agent2_discovery.services.story import Story
from app.agents.agent2_discovery.system_prompt import build_telegram_message
from app.models import Channel, ChannelConfig, ChannelLanguage

# Fails BOTH overall_score (65 floor) and emotional_stakes (55 floor) —
# mirrors the exact production shape reported (overall well under floor,
# not just one borderline dimension).
_LOW_SCORES = {
    "scores": {
        "visual_storytelling_potential": 60,
        "scroll_stopper_potential": 60,
        "emotional_stakes": 45,
        "central_mystery": 60,
        "conflict_or_contradiction": 60,
        "social_media_clickability": 60,
        "image_generation_feasibility": 60,
        "rights_ip_risk": 15,
    }
}

_PASSING_SCORES = {
    "scores": {
        "visual_storytelling_potential": 90,
        "scroll_stopper_potential": 90,
        "emotional_stakes": 90,
        "central_mystery": 90,
        "conflict_or_contradiction": 90,
        "social_media_clickability": 90,
        "image_generation_feasibility": 90,
        "rights_ip_risk": 10,
    }
}


class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        return _FakeQuery(self.rows[:n])

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _FakeDb:
    def __init__(self, channel, config, languages):
        self._by_type = {"Channel": channel, "ChannelConfig": config}
        self.languages = languages
        self.added: list = []

    def get(self, model, _pk):
        return self._by_type.get(model.__name__)

    def query(self, *cols):
        if cols and cols[0] is ChannelLanguage:
            return _FakeQuery(self.languages)
        return _FakeQuery([])

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if not getattr(obj, "id", None):
                obj.id = uuid.uuid4()

    def commit(self):
        pass

    def refresh(self, _obj):
        pass


def _fixture():
    channel_id = uuid.uuid4()
    channel = Channel(
        id=channel_id, user_id=uuid.uuid4(), name="Wealth Origins",
        description="Historical finance stories.",
        niche="historical finance / economic history", tone="suspenseful", active=True,
    )
    config = ChannelConfig(
        channel_id=channel_id, validation_timeout_hours=24,
        script_format="youtube_long", script_source="ai_generated",
    )
    languages = [ChannelLanguage(id=uuid.uuid4(), channel_id=channel_id, language="en", channel_name="Wealth Origins")]
    return channel, _FakeDb(channel, config, languages)


def _premise() -> Story:
    return Story(
        url=f"discovery://ai_generated/{uuid.uuid4()}/{uuid.uuid4()}",
        title="The Tulip Mania Crash of 1637",
        body="A short synthesized premise about the 1637 tulip bubble.",
        language="en", source_type="ai_generated", source_value="claude_synthesis",
    )


class NeverBlockOnScoreTest(unittest.TestCase):
    def test_below_floor_score_still_creates_content_and_returns_normally(self):
        channel, db = _fixture()
        premise = _premise()

        with (
            patch.object(discovery_mod, "generate_story_premise", return_value=[premise]),
            patch.object(discovery_mod, "score_story_for_gate", return_value=_LOW_SCORES),
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNotNone(result)
        content, story, story_score = result
        self.assertEqual(content.title, premise.title)
        self.assertEqual(content.status, "PENDING_APPROVAL")
        # A real Content + ContentValidation row were both added.
        from app.models import Content as ContentModel, ContentValidation as CV
        self.assertTrue(any(isinstance(o, ContentModel) for o in db.added))
        self.assertTrue(any(isinstance(o, CV) for o in db.added))
        # The gate verdict is still computed and visible, just non-blocking.
        self.assertLess(story_score["overall_score"], 65)
        self.assertTrue(story_score["failed_gates"])

    def test_passing_score_still_creates_content_as_before(self):
        """Regression: the normal accept path is unaffected."""
        channel, db = _fixture()
        premise = _premise()

        with (
            patch.object(discovery_mod, "generate_story_premise", return_value=[premise]),
            patch.object(discovery_mod, "score_story_for_gate", return_value=_PASSING_SCORES),
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNotNone(result)
        content, story, story_score = result
        self.assertEqual(content.status, "PENDING_APPROVAL")
        self.assertEqual(story_score["failed_gates"], [])

    def test_returned_third_value_is_the_weighted_story_score_not_raw_assessment(self):
        """run_discovery()'s 3rd return value must be the weighted dict
        (overall_score/dimension_scores/failed_gates) — not the raw
        {"scores": {...}} shape score_story_for_gate() returns — since that
        weighted shape is what build_telegram_message() now requires to
        show anything useful."""
        channel, db = _fixture()
        premise = _premise()

        with (
            patch.object(discovery_mod, "generate_story_premise", return_value=[premise]),
            patch.object(discovery_mod, "score_story_for_gate", return_value=_LOW_SCORES),
        ):
            result = run_discovery(channel.id, db)

        _content, _story, story_score = result
        self.assertIn("overall_score", story_score)
        self.assertIn("dimension_scores", story_score)
        self.assertIn("failed_gates", story_score)
        self.assertNotIn("scores", story_score)  # the raw shape's own key


class TelegramMessageShowsScoreTest(unittest.TestCase):
    """build_telegram_message() must render the weighted score, verdict, and
    failed gates — this is the ONE place the gate's opinion reaches the
    human, since the pipeline itself never blocks on it."""

    def test_below_floor_message_shows_warning_icon_and_failed_gates(self):
        message = build_telegram_message(
            title="The Tulip Mania Crash of 1637",
            url="discovery://ai_generated/x/y",
            assessment={
                "overall_score": 62.4,
                "dimension_scores": {"visual_storytelling_potential": 80, "rights_ip_risk": 15},
                "failed_gates": ["overall_score 62.4 < 65"],
                "operator_review_flags": [],
            },
            target_languages=["en"],
            user_language="en",
        )
        self.assertIn("62.4/100", message)
        self.assertIn("⚠️", message)
        self.assertIn("overall_score 62.4 < 65", message)

    def test_passing_message_shows_success_icon_and_no_failed_gates_line(self):
        message = build_telegram_message(
            title="A Great Story",
            url="discovery://ai_generated/x/y",
            assessment={
                "overall_score": 88.0,
                "dimension_scores": {"visual_storytelling_potential": 90, "rights_ip_risk": 10},
                "failed_gates": [],
                "operator_review_flags": [],
            },
            target_languages=["en"],
            user_language="en",
        )
        self.assertIn("88.0/100", message)
        self.assertIn("✅", message)
        self.assertNotIn("Below floor", message)


if __name__ == "__main__":
    unittest.main()
