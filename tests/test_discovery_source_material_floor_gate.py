"""Discovery-time source-material floor gate (prompt engineering follow-up).

A real operator run showed ``run_discovery()`` sending a Telegram approval for
a story whose ``source_excerpt`` turned out to be a fandom-wiki summary PAGE
ABOUT an 8-part r/nosleep series (527 words), not the series itself — the
thinness was only caught later, post-approval, in
``generate_parent_source_script()``'s existing ``check_source_material_floor()``
call. By then the operator had already spent their attention approving a
story that could never produce a real script.

This module proves the fix: ``run_discovery()`` now runs the same
deterministic ``check_source_material_floor()`` check on every fetched
candidate, in both the targeted-retry loop and the nuclear-retry fallback,
BEFORE any ``Content``/Telegram approval is created. A too-thin candidate is
rejected exactly like a duplicate — added to the exclusion list (with
feedback) and retried — so only a candidate with enough material ever reaches
Telegram.

Runtime proof (CLAUDE.md §19.4): the real ``run_discovery()`` runs against a
fake DB and a stubbed ``fetch_batch()`` (the paid ``story_research`` Claude
boundary) and a stubbed ``score_story_for_gate()`` (the paid
``story_gate_scoring`` boundary) — every other function in the chain
(``_is_duplicate``, ``_fails_source_material_floor``, ``_nuclear_retry``,
``score_story_assessment``, ``decide_story_acceptance``, Content persistence)
is the real implementation.
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery.services import discovery as discovery_mod
from app.agents.agent2_discovery.services.discovery import (
    _fails_source_material_floor,
    run_discovery,
)
from app.agents.agent2_discovery.services.story import Story
from app.models import ChannelSource


def _story(title: str, url: str, word_count: int) -> Story:
    body = " ".join(["word"] * word_count)
    return Story(
        url=url, title=title, body=body, language="en",
        source_type="reddit", source_value="r/nosleep",
    )


# A real, well-formed score dict passing every gate (fresh full-system audit
# §2.5's 8-dimension schema) — used to stub the paid story_gate_scoring call
# so the REAL score_story_assessment()/decide_story_acceptance() run on it.
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
        self._rows = rows

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """Minimal fake session for run_discovery()'s query surface.

    ``content_rows`` seeds every duplicate-check query (both the URL check
    and the hash check in ``_is_duplicate``, and the nuclear-retry history
    query) — empty by default, so nothing is ever a duplicate unless a test
    explicitly seeds it.
    """

    def __init__(self, channel, config, sources, content_rows=None):
        self._by_type = {"Channel": channel, "ChannelConfig": config}
        self.sources = sources
        self.content_rows = content_rows or []
        self.added: list = []
        self.commits = 0

    def get(self, model, _pk):
        return self._by_type.get(model.__name__)

    def query(self, *cols):
        # ChannelSource is queried by the whole class; every Content query in
        # run_discovery() (dup checks, nuclear-retry history) is queried by
        # column attribute (Content.id, Content.title, ...), never the class
        # itself — an identity check on the first arg is enough to route
        # between the two without modeling real filter predicates.
        if cols and cols[0] is ChannelSource:
            return _FakeQuery(self.sources)
        return _FakeQuery(self.content_rows)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if not getattr(obj, "id", None):
                obj.id = uuid.uuid4()

    def commit(self):
        self.commits += 1

    def refresh(self, _obj):
        pass


def _channel_fixture():
    channel = SimpleNamespace(
        id=uuid.uuid4(), niche="horror", tone="suspenseful",
        active=True, user_id=uuid.uuid4(),
    )
    config = SimpleNamespace(
        validation_timeout_hours=24, script_format="youtube_long",
        script_source="reddit",
    )
    sources = [SimpleNamespace(source_value="r/nosleep", source_type="reddit", trust_score=1.0)]
    return channel, config, sources


class TestFailsSourceMaterialFloor(unittest.TestCase):
    """Unit coverage for the new helper itself."""

    def test_thin_story_fails(self):
        thin = _story("Thin", "https://example.com/thin", word_count=200)
        reason = _fails_source_material_floor(thin, "youtube_long")
        self.assertIsNotNone(reason)
        self.assertIn("below the 900-word floor", reason)

    def test_substantial_story_passes(self):
        rich = _story("Rich", "https://example.com/rich", word_count=1200)
        self.assertIsNone(_fails_source_material_floor(rich, "youtube_long"))

    def test_short_form_uses_lower_floor(self):
        # 500 words fails youtube_long's 900-word floor but clears the
        # 420-word floor used for any other script_format.
        mid = _story("Mid", "https://example.com/mid", word_count=500)
        self.assertIsNotNone(_fails_source_material_floor(mid, "youtube_long"))
        self.assertIsNone(_fails_source_material_floor(mid, "youtube_short"))


class TestRunDiscoveryFloorGate(unittest.TestCase):
    def test_thin_candidate_rejected_and_retried_with_feedback(self):
        """A too-thin candidate never reaches Content/Telegram — it is added
        to the exclusion list (with feedback) and fetch_batch is called again;
        the second, substantial candidate is the one actually accepted."""
        channel, config, sources = _channel_fixture()
        db = _FakeDb(channel, config, sources)

        thin = _story("Wiki Summary Page", "https://creepypasta.fandom.com/thin", word_count=527)
        rich = _story("The Real Post", "https://reddit.com/r/nosleep/rich", word_count=1200)
        fetch_calls: list[dict] = []

        def fake_fetch_batch(sources_list, niche, rejected_stories=None):
            fetch_calls.append({"rejected_stories": rejected_stories})
            return [thin] if len(fetch_calls) == 1 else [rich]

        with (
            patch.object(discovery_mod, "fetch_batch", side_effect=fake_fetch_batch),
            patch.object(discovery_mod, "score_story_for_gate", return_value=_PASSING_SCORES),
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNotNone(result)
        content, story, _assessment = result

        # fetch_batch was called twice — once for the thin candidate, once
        # more after it was rejected and excluded.
        self.assertEqual(len(fetch_calls), 2)
        self.assertIsNone(fetch_calls[0]["rejected_stories"])
        second_rejected = fetch_calls[1]["rejected_stories"]
        self.assertEqual(len(second_rejected), 1)
        self.assertEqual(second_rejected[0]["title"], thin.title)
        self.assertEqual(second_rejected[0]["url"], thin.url)
        self.assertIn("too thin", second_rejected[0]["feedback"])

        # The accepted story — and the persisted Content row — are the rich
        # candidate, never the thin one.
        self.assertEqual(story.title, rich.title)
        self.assertEqual(content.source_excerpt, rich.body)
        self.assertNotEqual(content.source_excerpt, thin.body)

    def test_all_candidates_thin_falls_through_to_manual_fallback(self):
        """Every targeted-retry AND nuclear-retry candidate is too thin —
        no Content/Telegram approval is ever created; the manual fallback
        path is what fires instead."""
        channel, config, sources = _channel_fixture()
        db = _FakeDb(channel, config, sources)

        thin_variants = [
            _story(f"Thin {i}", f"https://example.com/thin{i}", word_count=300)
            for i in range(10)
        ]
        calls = {"n": 0}

        def fake_fetch_batch(sources_list, niche, rejected_stories=None):
            i = calls["n"]
            calls["n"] += 1
            return [thin_variants[i]] if i < len(thin_variants) else []

        with (
            patch.object(discovery_mod, "fetch_batch", side_effect=fake_fetch_batch),
            patch.object(discovery_mod, "score_story_for_gate", return_value=_PASSING_SCORES),
            patch.object(discovery_mod, "_create_manual_fallback") as fallback_mock,
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNone(result)
        fallback_mock.assert_called_once()
        # No Content row for a thin story was ever added to the session.
        self.assertEqual(db.added, [])


if __name__ == "__main__":
    unittest.main()
