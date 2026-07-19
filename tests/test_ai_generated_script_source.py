"""AI-Generated Story Discovery (``ChannelConfig.script_source="ai_generated"``).

Runtime proof and unit coverage for the two-stage premise/expansion design —
see ``code_report/ai_generated_story_discovery_design.md`` for the full
design record and CLAUDE.md §9.5 for the runtime architecture this
proves.

Covers:
  - Agent 1 executability + activation-readiness niche/tone gate (pure
    functions, no stubbing needed).
  - ``generate_story_premise()``/``expand_story_premise()`` unit behavior
    with the paid Claude boundary (``call_claude_structured``) stubbed.
  - A runtime proof (CLAUDE.md §19.4) driving the real ``run_discovery()``
    and ``generate_parent_source_script()`` chains against a fake DB, with
    only the paid Claude boundaries (``generate_story_premise``,
    ``expand_story_premise``, ``score_story_for_gate``,
    ``generate_story_blueprint``, ``generate_script_sections``) stubbed —
    every other function in the chain (``_resolve_ai_story_language``,
    ``_seed_ai_generated_exclusions``, ``_is_duplicate``,
    ``_create_manual_fallback`` branching, ``_ensure_ai_story_expanded``,
    ``_passes_source_material_floor``) is the real, unmodified
    implementation.
  - A regression proof that the unmodified reddit path is unaffected.
"""

from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent1_setup.services.activation_readiness import check_activation_readiness
from app.agents.agent1_setup.services.v3_config_rules import (
    is_executable_script_source,
    validate_v3_channel_config,
)
from app.agents.agent2_discovery.services import discovery as discovery_mod
from app.agents.agent2_discovery.services import script_workflow as script_workflow_mod
from app.agents.agent2_discovery.services import story_generator as story_generator_mod
from app.agents.agent2_discovery.services.discovery import run_discovery
from app.agents.agent2_discovery.services.script_workflow import ScriptWorkflowContext
from app.agents.agent2_discovery.services.story import Story
from app.models import Channel, ChannelConfig, ChannelLanguage, ChannelSource, Content

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


def _channel_fixture(script_source: str = "ai_generated"):
    channel_id = uuid.uuid4()
    channel = Channel(
        id=channel_id, user_id=uuid.uuid4(), name="Test Channel",
        description="A horror storytelling channel.",
        niche="horror", tone="suspenseful", active=True,
    )
    config = ChannelConfig(
        channel_id=channel_id, validation_timeout_hours=24,
        script_format="youtube_long", script_source=script_source,
    )
    languages = [
        ChannelLanguage(id=uuid.uuid4(), channel_id=channel_id, language="en", channel_name="Test Channel"),
    ]
    return channel, config, languages


# ═══════════════════════════════════════════════════════════════════════════
# 1: Agent 1 — executability + activation readiness (pure functions)
# ═══════════════════════════════════════════════════════════════════════════

class TestExecutabilityAndActivation(unittest.TestCase):
    def test_ai_generated_is_executable_for_single_story(self):
        self.assertTrue(is_executable_script_source("single_story", "ai_generated"))
        self.assertTrue(is_executable_script_source("single_story", "claude_generated"))

    def test_ai_generated_not_executable_outside_single_story(self):
        self.assertFalse(is_executable_script_source("limited_series", "ai_generated"))

    def test_user_provided_and_hybrid_remain_not_executable(self):
        self.assertFalse(is_executable_script_source("single_story", "user_provided"))
        self.assertFalse(is_executable_script_source("single_story", "hybrid"))

    def test_validate_v3_channel_config_ai_generated_is_fully_executable(self):
        result = validate_v3_channel_config({
            "content_mode": "single_story", "script_source": "ai_generated",
            "output_mode": "youtube_and_shorts",
        })
        self.assertEqual(result, {"executable": True, "supported": True, "issues": []})

    def _readiness_channel(self, niche: str, tone: str, script_source: str = "ai_generated"):
        return SimpleNamespace(
            id=uuid.uuid4(), niche=niche, tone=tone, description=None,
            config=SimpleNamespace(
                content_mode="single_story", script_source=script_source,
                output_mode="youtube_and_shorts",
            ),
            languages=[SimpleNamespace(language="en")],
            voices=[SimpleNamespace(language="en")],
            sources=[],
            publish_timings=[SimpleNamespace()],
            platforms=[SimpleNamespace(platform="youtube", language="en", verified=True)],
        )

    def test_activation_blocks_ai_generated_channel_with_empty_niche(self):
        channel = self._readiness_channel(niche="", tone="suspenseful")
        result = check_activation_readiness(channel)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("ai_generated_missing_niche", codes)
        self.assertFalse(result["ready"])

    def test_activation_blocks_ai_generated_channel_with_empty_tone(self):
        channel = self._readiness_channel(niche="horror", tone="   ")
        result = check_activation_readiness(channel)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("ai_generated_missing_tone", codes)

    def test_activation_does_not_require_channel_source_for_ai_generated(self):
        channel = self._readiness_channel(niche="horror", tone="suspenseful")
        result = check_activation_readiness(channel)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertNotIn("no_sources_for_reddit_mode", codes)
        self.assertTrue(result["ready"], result["issues"])

    def test_reddit_channel_unaffected_by_new_niche_tone_check(self):
        """The new check 10 is scoped to script_source='ai_generated' only —
        a reddit channel with empty niche/tone (already impossible given
        Channel.niche/tone are NOT NULL, but checked here for the code path)
        must not trip the new ai_generated-only codes."""
        channel = self._readiness_channel(niche="horror", tone="suspenseful", script_source="reddit")
        channel.sources = [SimpleNamespace()]
        result = check_activation_readiness(channel)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertNotIn("ai_generated_missing_niche", codes)
        self.assertNotIn("ai_generated_missing_tone", codes)


# ═══════════════════════════════════════════════════════════════════════════
# 2: generate_story_premise() / expand_story_premise() — Claude boundary
#    stubbed, everything else real
# ═══════════════════════════════════════════════════════════════════════════

class TestGenerateStoryPremise(unittest.TestCase):
    def _channel(self):
        return SimpleNamespace(id=uuid.uuid4(), niche="horror", tone="suspenseful", description="A desc.")

    def test_returns_story_on_success(self):
        channel = self._channel()
        with patch.object(
            story_generator_mod, "call_claude_structured",
            return_value={"title": "The Locked Attic", "body": "A short concrete premise about a locked attic."},
        ) as mock_call:
            result = story_generator_mod.generate_story_premise(channel, language="en")

        self.assertEqual(len(result), 1)
        story = result[0]
        self.assertIsInstance(story, Story)
        self.assertEqual(story.title, "The Locked Attic")
        self.assertEqual(story.source_type, "ai_generated")
        self.assertEqual(story.source_value, "claude_synthesis")
        self.assertEqual(story.language, "en")
        self.assertTrue(story.url.startswith(f"discovery://ai_generated/{channel.id}/"))
        mock_call.assert_called_once()
        self.assertEqual(mock_call.call_args.kwargs["task"], "story_synthesis")

    def test_empty_body_returns_empty_list(self):
        channel = self._channel()
        with patch.object(story_generator_mod, "call_claude_structured", return_value={"title": "T", "body": ""}):
            result = story_generator_mod.generate_story_premise(channel, language="en")
        self.assertEqual(result, [])

    def test_empty_title_returns_empty_list(self):
        channel = self._channel()
        with patch.object(story_generator_mod, "call_claude_structured", return_value={"title": "", "body": "Body."}):
            result = story_generator_mod.generate_story_premise(channel, language="en")
        self.assertEqual(result, [])

    def test_claude_exception_returns_empty_list_never_raises(self):
        channel = self._channel()
        with patch.object(story_generator_mod, "call_claude_structured", side_effect=RuntimeError("boom")):
            result = story_generator_mod.generate_story_premise(channel, language="en")
        self.assertEqual(result, [])

    def test_rejected_stories_threaded_into_user_message(self):
        channel = self._channel()
        captured: dict = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return {"title": "New Premise", "body": "A different situation entirely."}

        with patch.object(story_generator_mod, "call_claude_structured", side_effect=fake_call):
            story_generator_mod.generate_story_premise(
                channel, language="en",
                rejected_stories=[{"title": "Old One", "url": "discovery://x", "feedback": "too similar"}],
            )

        self.assertIn("Old One", captured["user_message"])
        self.assertIn("too similar", captured["user_message"])

    def test_no_rejected_stories_omits_avoid_block(self):
        channel = self._channel()
        captured: dict = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return {"title": "T", "body": "Body text."}

        with patch.object(story_generator_mod, "call_claude_structured", side_effect=fake_call):
            story_generator_mod.generate_story_premise(channel, language="en", rejected_stories=None)

        self.assertNotIn("Avoid similar", captured["user_message"])

    def test_two_calls_generate_unique_urls(self):
        channel = self._channel()
        with patch.object(
            story_generator_mod, "call_claude_structured",
            return_value={"title": "T", "body": "Body text here."},
        ):
            r1 = story_generator_mod.generate_story_premise(channel, language="en")
            r2 = story_generator_mod.generate_story_premise(channel, language="en")
        self.assertNotEqual(r1[0].url, r2[0].url)

    def test_long_premise_logs_warning_but_still_returns_story(self):
        """Elimination Mandate: an ignored length instruction is telemetry
        only, never a re-roll."""
        channel = self._channel()
        long_body = " ".join(["word"] * 200)
        with (
            patch.object(story_generator_mod, "call_claude_structured",
                         return_value={"title": "T", "body": long_body}),
            self.assertLogs("app.agents.agent2_discovery.services.story_generator", level="WARNING") as log_ctx,
        ):
            result = story_generator_mod.generate_story_premise(channel, language="en")
        self.assertEqual(len(result), 1)
        self.assertIn("longer than expected", " ".join(log_ctx.output))


class TestExpandStoryPremise(unittest.TestCase):
    def _channel(self):
        return SimpleNamespace(
            id=uuid.uuid4(), niche="horror", tone="suspenseful",
            description="A channel about small-town urban legends.",
        )

    def test_returns_expanded_body_on_success(self):
        channel = self._channel()
        expanded_text = "word " * 1300
        with patch.object(story_generator_mod, "call_claude_structured", return_value={"body": expanded_text}):
            result = story_generator_mod.expand_story_premise(
                premise="A short premise.", channel=channel,
                script_format="youtube_long", language="en",
            )
        self.assertEqual(result, expanded_text.strip())

    def test_word_target_scales_with_script_format(self):
        channel = self._channel()
        captured: dict = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return {"body": "expanded"}

        with patch.object(story_generator_mod, "call_claude_structured", side_effect=fake_call):
            story_generator_mod.expand_story_premise(
                premise="p", channel=channel, script_format="youtube_long", language="en",
            )
        self.assertIn("2200 words", captured["user_message"])

        captured.clear()
        with patch.object(story_generator_mod, "call_claude_structured", side_effect=fake_call):
            story_generator_mod.expand_story_premise(
                premise="p", channel=channel, script_format="tiktok", language="en",
            )
        self.assertIn("600 words", captured["user_message"])

    def test_premise_text_included_verbatim_in_user_message(self):
        channel = self._channel()
        captured: dict = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return {"body": "expanded"}

        with patch.object(story_generator_mod, "call_claude_structured", side_effect=fake_call):
            story_generator_mod.expand_story_premise(
                premise="A very specific approved premise about a hollow tree.",
                channel=channel, script_format="youtube_long", language="en",
            )
        self.assertIn("A very specific approved premise about a hollow tree.", captured["user_message"])

    def test_empty_body_returns_none(self):
        channel = self._channel()
        with patch.object(story_generator_mod, "call_claude_structured", return_value={"body": ""}):
            result = story_generator_mod.expand_story_premise(
                premise="p", channel=channel, script_format="youtube_long", language="en",
            )
        self.assertIsNone(result)

    def test_claude_exception_returns_none_never_raises(self):
        channel = self._channel()
        with patch.object(story_generator_mod, "call_claude_structured", side_effect=RuntimeError("boom")):
            result = story_generator_mod.expand_story_premise(
                premise="p", channel=channel, script_format="youtube_long", language="en",
            )
        self.assertIsNone(result)


class TestReviseStoryPremise(unittest.TestCase):
    """revise_story_premise() — the conversational-revision function (CLAUDE.md
    §9.5's "Conversational premise revision"): a CHANGE reply for
    script_source="ai_generated" must revise the SAME premise using the full
    prior conversation, not discard it for an unrelated new one."""

    def _channel(self):
        return SimpleNamespace(
            id=uuid.uuid4(), niche="history", tone="documentary",
            description="A channel about famous ancient military campaigns.",
        )

    def test_returns_revised_title_and_body_on_success(self):
        channel = self._channel()
        conversation = [
            {"role": "assistant", "title": "A General's Gamble", "body": "Vague description."},
            {"role": "operator", "feedback": "Be specific — this is about Hannibal crossing the Alps."},
        ]
        with patch.object(
            story_generator_mod, "call_claude_structured",
            return_value={"title": "Hannibal: Carthage's War Against Rome",
                          "body": "Hannibal leads his army, elephants included, across the Alps to fight Rome."},
        ):
            result = story_generator_mod.revise_story_premise(
                channel=channel, language="en", conversation=conversation,
            )
        self.assertEqual(result["title"], "Hannibal: Carthage's War Against Rome")
        self.assertIn("Hannibal", result["body"])

    def test_full_conversation_transcript_included_in_user_message(self):
        """The money test: every prior premise AND every operator reply must
        reach Claude, in order — not just the latest feedback — so a later
        revision can never contradict an earlier one."""
        channel = self._channel()
        conversation = [
            {"role": "assistant", "title": "Title A", "body": "Body A."},
            {"role": "operator", "feedback": "Make the setting ancient Rome."},
            {"role": "assistant", "title": "Title B", "body": "Body B, now set in Rome."},
            {"role": "operator", "feedback": "Now make the title mention Hannibal specifically."},
        ]
        captured: dict = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return {"title": "Hannibal in Rome", "body": "Revised body."}

        with patch.object(story_generator_mod, "call_claude_structured", side_effect=fake_call):
            story_generator_mod.revise_story_premise(channel=channel, language="en", conversation=conversation)

        msg = captured["user_message"]
        self.assertIn("Title A", msg)
        self.assertIn("Body A.", msg)
        self.assertIn("Make the setting ancient Rome.", msg)
        self.assertIn("Title B", msg)
        self.assertIn("Body B, now set in Rome.", msg)
        self.assertIn("Now make the title mention Hannibal specifically.", msg)
        # Order matters: turn 1 must appear before turn 4 in the transcript.
        self.assertLess(msg.index("Title A"), msg.index("Now make the title mention Hannibal"))

    def test_empty_title_or_body_returns_none(self):
        channel = self._channel()
        conversation = [
            {"role": "assistant", "title": "T", "body": "B"},
            {"role": "operator", "feedback": "f"},
        ]
        with patch.object(story_generator_mod, "call_claude_structured", return_value={"title": "", "body": "x"}):
            result = story_generator_mod.revise_story_premise(channel=channel, language="en", conversation=conversation)
        self.assertIsNone(result)

    def test_claude_exception_returns_none_never_raises(self):
        channel = self._channel()
        conversation = [
            {"role": "assistant", "title": "T", "body": "B"},
            {"role": "operator", "feedback": "f"},
        ]
        with patch.object(story_generator_mod, "call_claude_structured", side_effect=RuntimeError("boom")):
            result = story_generator_mod.revise_story_premise(channel=channel, language="en", conversation=conversation)
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════
# 3: run_discovery() — ai_generated runtime proof (real chain, fake DB)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def join(self, *a, **k):
        # Needed by script_estimator.compute_measured_wpm()'s calibration
        # query (db.query(AudioFile).filter(...).join(Content, ...)) when
        # generate_parent_source_script() -> _persist_source_script() calls
        # estimate_duration_sec(..., db=db, is_short_episode=False). Since no
        # AudioFile rows are ever seeded in these fakes, the join is a no-op
        # passthrough — calibration always fails open to the static fallback.
        return self

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


class _FakeDiscoveryDb:
    """Fake session covering run_discovery()'s full ai_generated query
    surface.

    ``duplicate_rows`` (queried via ``Content.id``) is kept deliberately
    separate from ``history_rows`` (queried via ``Content.title,
    Content.source_url`` — used by both ``_seed_ai_generated_exclusions()``
    and ``_nuclear_retry()``): the shared ``_FakeQuery.filter()`` below is a
    no-op, so if both query shapes read the same table, a non-empty history
    would make ``_is_duplicate()`` report every candidate as a duplicate.
    """

    def __init__(self, channel, config, sources=None, languages=None,
                 history_rows=None, duplicate_rows=None):
        self._by_type = {"Channel": channel, "ChannelConfig": config}
        self.sources = sources or []
        self.languages = languages or []
        self.history_rows = history_rows or []
        self.duplicate_rows = duplicate_rows or []
        self.added: list = []
        self.commits = 0

    def get(self, model, _pk):
        return self._by_type.get(model.__name__)

    def query(self, *cols):
        if cols and cols[0] is ChannelSource:
            return _FakeQuery(self.sources)
        if cols and cols[0] is ChannelLanguage:
            return _FakeQuery(self.languages)
        if cols == (Content.id,):
            return _FakeQuery(self.duplicate_rows)
        return _FakeQuery(self.history_rows)

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


class TestRunDiscoveryAiGeneratedPath(unittest.TestCase):
    def test_zero_channel_source_channel_succeeds(self):
        """ai_generated channels have no ChannelSource rows at all — unlike
        the reddit path, run_discovery() must not reject them for that."""
        channel, config, languages = _channel_fixture()
        db = _FakeDiscoveryDb(channel, config, sources=[], languages=languages)

        premise = Story(
            url=f"discovery://ai_generated/{channel.id}/{uuid.uuid4()}",
            title="The Locked Attic", body="A short premise about a locked attic that hums at night.",
            language="en", source_type="ai_generated", source_value="claude_synthesis",
        )

        with (
            patch.object(discovery_mod, "generate_story_premise", return_value=[premise]) as mock_premise,
            patch.object(discovery_mod, "score_story_for_gate", return_value=_PASSING_SCORES),
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNotNone(result)
        content, story, _assessment = result
        self.assertEqual(story.source_type, "ai_generated")
        self.assertEqual(content.source_excerpt, premise.body)
        mock_premise.assert_called_once()
        self.assertEqual(mock_premise.call_args.kwargs["language"], "en")

    def test_exclusions_seeded_before_first_premise_call(self):
        """The channel's recent content history is proactively threaded into
        the very FIRST generate_story_premise() call — not only after an
        in-loop rejection — since synthetic URLs mean _is_duplicate() can
        never grow the list on its own."""
        channel, config, languages = _channel_fixture()
        history = [SimpleNamespace(title="Already Published One", source_url="discovery://ai_generated/x/1")]
        db = _FakeDiscoveryDb(channel, config, sources=[], languages=languages, history_rows=history)

        premise = Story(
            url=f"discovery://ai_generated/{channel.id}/{uuid.uuid4()}",
            title="New One", body="A brand new premise, unrelated to anything before.",
            language="en", source_type="ai_generated", source_value="claude_synthesis",
        )
        calls: list = []

        def fake_generate(channel_arg, language, rejected_stories=None):
            calls.append(rejected_stories)
            return [premise]

        with (
            patch.object(discovery_mod, "generate_story_premise", side_effect=fake_generate),
            patch.object(discovery_mod, "score_story_for_gate", return_value=_PASSING_SCORES),
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0])
        self.assertEqual(calls[0][0]["title"], "Already Published One")

    def test_nuclear_retry_never_invoked_for_ai_generated(self):
        """Every attempt exhausts with no candidate at all; _nuclear_retry
        must never be called — manual fallback fires instead with
        is_ai_generated=True."""
        channel, config, languages = _channel_fixture()
        db = _FakeDiscoveryDb(channel, config, sources=[], languages=languages)

        with (
            patch.object(discovery_mod, "generate_story_premise", return_value=[]),
            patch.object(discovery_mod, "_nuclear_retry") as nuclear_mock,
            patch.object(discovery_mod, "_create_manual_fallback") as fallback_mock,
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNone(result)
        nuclear_mock.assert_not_called()
        fallback_mock.assert_called_once()
        self.assertTrue(fallback_mock.call_args.kwargs["is_ai_generated"])

    def test_floor_check_never_fires_on_short_premise_at_discovery_time(self):
        """A deliberately thin premise (well under the 900/420-word floor)
        must still be accepted at discovery time — the source-material floor
        check is skipped entirely for ai_generated; it applies only
        post-approval, in _ensure_ai_story_expanded()'s caller."""
        channel, config, languages = _channel_fixture()
        db = _FakeDiscoveryDb(channel, config, sources=[], languages=languages)

        thin_premise = Story(
            url=f"discovery://ai_generated/{channel.id}/{uuid.uuid4()}",
            title="Thin", body="Only a dozen words make up this deliberately short premise text here.",
            language="en", source_type="ai_generated", source_value="claude_synthesis",
        )

        with (
            patch.object(discovery_mod, "generate_story_premise", return_value=[thin_premise]),
            patch.object(discovery_mod, "score_story_for_gate", return_value=_PASSING_SCORES),
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNotNone(result)
        content, story, _assessment = result
        self.assertEqual(story.title, "Thin")


class TestRedditPathRegression(unittest.TestCase):
    """The unmodified reddit path must behave exactly as before this change."""

    def test_reddit_channel_without_sources_still_returns_none(self):
        channel, config, languages = _channel_fixture(script_source="reddit")
        db = _FakeDiscoveryDb(channel, config, sources=[], languages=languages)

        with patch.object(discovery_mod, "generate_story_premise") as premise_mock:
            result = run_discovery(channel.id, db)

        self.assertIsNone(result)
        premise_mock.assert_not_called()

    def test_reddit_channel_calls_fetch_batch_not_generate_story_premise(self):
        channel, config, languages = _channel_fixture(script_source="reddit")
        sources = [ChannelSource(
            id=uuid.uuid4(), channel_id=channel.id, source_value="r/nosleep",
            source_type="reddit", trust_score=1.0, language="en",
        )]
        db = _FakeDiscoveryDb(channel, config, sources=sources, languages=languages)

        rich = Story(
            url="https://reddit.com/r/nosleep/rich", title="Rich Story",
            body=" ".join(["word"] * 1200), language="en",
            source_type="reddit", source_value="r/nosleep",
        )

        with (
            patch.object(discovery_mod, "fetch_batch", return_value=[rich]) as fetch_mock,
            patch.object(discovery_mod, "generate_story_premise") as premise_mock,
            patch.object(discovery_mod, "score_story_for_gate", return_value=_PASSING_SCORES),
        ):
            result = run_discovery(channel.id, db)

        self.assertIsNotNone(result)
        fetch_mock.assert_called_once()
        premise_mock.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 4: _ensure_ai_story_expanded() — idempotency and failure path
# ═══════════════════════════════════════════════════════════════════════════

class _FakeCommitOnlyDb:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _script_workflow_context(channel, config, script_format="youtube_long"):
    return ScriptWorkflowContext(
        channel=channel, config=config, script_format=script_format,
        audio_tags_enabled=False, source_voice=None,
        tts_model="sonic-3.5", tts_provider="cartesia",
        visual_style="story_driven", image_style="photorealistic",
        narration_pov="third_person",
    )


class TestEnsureAiStoryExpanded(unittest.TestCase):
    def _content(self, source_excerpt):
        return SimpleNamespace(
            id=uuid.uuid4(), source_excerpt=source_excerpt, source_language="en",
            status="GENERATING_SCRIPTS",
        )

    def test_idempotent_noop_when_already_sufficient(self):
        """A stuck-state-recovery re-entry whose source_excerpt already
        clears the floor must not call expand_story_premise() again."""
        channel, config, _langs = _channel_fixture()
        context = _script_workflow_context(channel, config)
        long_body = " ".join(["word"] * 1200)
        content = self._content(long_body)
        db = _FakeCommitOnlyDb()

        with patch.object(script_workflow_mod, "expand_story_premise") as expand_mock:
            result = script_workflow_mod._ensure_ai_story_expanded(content, context, db)

        self.assertTrue(result)
        expand_mock.assert_not_called()
        self.assertEqual(content.source_excerpt, long_body)
        self.assertEqual(db.commits, 0)

    def test_expands_and_persists_when_premise_too_thin(self):
        channel, config, _langs = _channel_fixture()
        context = _script_workflow_context(channel, config)
        premise = "A short premise about a locked attic."
        content = self._content(premise)
        db = _FakeCommitOnlyDb()
        expanded_body = " ".join(["word"] * 1300)

        with patch.object(
            script_workflow_mod, "expand_story_premise", return_value=expanded_body,
        ) as expand_mock:
            result = script_workflow_mod._ensure_ai_story_expanded(content, context, db)

        self.assertTrue(result)
        expand_mock.assert_called_once()
        self.assertEqual(expand_mock.call_args.kwargs["premise"], premise)
        self.assertEqual(content.source_excerpt, expanded_body)
        self.assertEqual(db.commits, 1)
        self.assertEqual(content.status, "GENERATING_SCRIPTS")  # untouched on success

    def test_failure_sets_status_failed(self):
        channel, config, _langs = _channel_fixture()
        context = _script_workflow_context(channel, config)
        content = self._content("A short premise.")
        db = _FakeCommitOnlyDb()

        with patch.object(script_workflow_mod, "expand_story_premise", return_value=None):
            result = script_workflow_mod._ensure_ai_story_expanded(content, context, db)

        self.assertFalse(result)
        self.assertEqual(content.status, "FAILED")
        self.assertEqual(db.commits, 1)

    def test_still_too_short_after_expansion_is_not_re_rolled(self):
        """Elimination Mandate: a real-but-still-short expansion is NOT
        re-rolled by this function — it returns True and lets the caller's
        _passes_source_material_floor() catch it."""
        channel, config, _langs = _channel_fixture()
        context = _script_workflow_context(channel, config)
        content = self._content("A short premise.")
        db = _FakeCommitOnlyDb()
        still_short = " ".join(["word"] * 50)

        with patch.object(
            script_workflow_mod, "expand_story_premise", return_value=still_short,
        ) as expand_mock:
            result = script_workflow_mod._ensure_ai_story_expanded(content, context, db)

        self.assertTrue(result)
        expand_mock.assert_called_once()
        self.assertEqual(content.source_excerpt, still_short)


# ═══════════════════════════════════════════════════════════════════════════
# 5: generate_parent_source_script() — full chain, persist-then-recheck
#    ordering proof (CLAUDE.md §19.4)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeWorkflowDb:
    def __init__(self):
        self.tables: dict = {}
        self.commits = 0

    def get(self, model, key):
        for row in self.tables.get(model, []):
            # ChannelConfig's real primary key is channel_id, not id — fall
            # back to it so db.get(ChannelConfig, channel_id) resolves
            # correctly (a row with no .id attribute at all, e.g.
            # ChannelConfig, must not silently match via getattr(...,
            # None) == key for an unset id).
            pk = getattr(row, "id", None)
            if pk is None:
                pk = getattr(row, "channel_id", None)
            if pk == key:
                return row
        return None

    def query(self, model):
        return _FakeQuery(self.tables.get(model, []))

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)

    def commit(self):
        self.commits += 1

    def refresh(self, row):
        pass


def _fake_blueprint(story, channel, **kwargs):
    return {
        "major_turns": ["t1"], "suggested_section_count": 1,
        "hook": "h", "final_payoff": "p", "comment_trigger": "c?",
        "midpoint_retention_trap": "m", "central_question": "q",
        "suggested_title": "T",
    }


def _fake_sections(**kwargs):
    return {
        "title": "T",
        "voice_script": "[INTRO]\nSome narration here.\n[OUTRO]\nDone.\n",
        "visual_intent_history": [],
    }


class TestGenerateParentSourceScriptAiGeneratedChain(unittest.TestCase):
    def _fixtures(self, *, premise: str):
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db = _FakeWorkflowDb()
        db.add(Channel(id=channel_id, niche="horror", tone="tense"))
        db.add(ChannelConfig(
            channel_id=channel_id, script_format="youtube_long", script_source="ai_generated",
            visual_style="story_driven", image_style="photorealistic", audio_tags_enabled=False,
        ))
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="APPROVED", title="T",
            source_url=f"discovery://ai_generated/{channel_id}/{uuid.uuid4()}",
            source_excerpt=premise,
        )
        db.add(content)
        return db, content

    def _poisoned(self, name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"{name}() must never be called before expansion+floor check pass")
        return _raise

    def test_premise_expanded_before_floor_check_then_proceeds(self):
        db, content = self._fixtures(premise="A short premise about a locked attic.")
        expanded = " ".join(["word"] * 1300)

        with (
            patch.object(script_workflow_mod, "expand_story_premise", return_value=expanded) as expand_mock,
            patch.object(script_workflow_mod, "generate_story_blueprint", side_effect=_fake_blueprint),
            patch.object(script_workflow_mod, "generate_script_sections", side_effect=_fake_sections),
        ):
            result = script_workflow_mod.generate_parent_source_script(content, db)

        self.assertIsNotNone(result)
        expand_mock.assert_called_once()
        # The persisted source_excerpt is the EXPANDED body, not the original
        # premise — the floor check judges the updated value, not a stale one.
        self.assertEqual(content.source_excerpt, expanded)
        self.assertEqual(content.status, "GENERATING_SCRIPTS")

    def test_expansion_still_too_short_fails_at_floor_check_not_expansion(self):
        db, content = self._fixtures(premise="A short premise.")
        still_short = " ".join(["word"] * 50)

        with (
            patch.object(script_workflow_mod, "expand_story_premise", return_value=still_short) as expand_mock,
            patch.object(script_workflow_mod, "generate_story_blueprint",
                         side_effect=self._poisoned("generate_story_blueprint")),
        ):
            result = script_workflow_mod.generate_parent_source_script(content, db)

        self.assertIsNone(result)
        expand_mock.assert_called_once()
        self.assertEqual(content.status, "FAILED")
        self.assertEqual(content.source_excerpt, still_short)

    def test_expansion_outright_failure_sets_failed_without_reaching_floor_check(self):
        db, content = self._fixtures(premise="A short premise.")

        with (
            patch.object(script_workflow_mod, "expand_story_premise", return_value=None) as expand_mock,
            patch.object(script_workflow_mod, "generate_story_blueprint",
                         side_effect=self._poisoned("generate_story_blueprint")),
        ):
            result = script_workflow_mod.generate_parent_source_script(content, db)

        self.assertIsNone(result)
        expand_mock.assert_called_once()
        self.assertEqual(content.status, "FAILED")

    def test_already_sufficient_premise_skips_expansion_entirely(self):
        """Stuck-state-recovery re-entry: source_excerpt already clears the
        floor (a prior run already expanded it) — expand_story_premise must
        not be called again."""
        already_long = " ".join(["word"] * 1300)
        db, content = self._fixtures(premise=already_long)

        with (
            patch.object(script_workflow_mod, "expand_story_premise") as expand_mock,
            patch.object(script_workflow_mod, "generate_story_blueprint", side_effect=_fake_blueprint),
            patch.object(script_workflow_mod, "generate_script_sections", side_effect=_fake_sections),
        ):
            result = script_workflow_mod.generate_parent_source_script(content, db)

        expand_mock.assert_not_called()
        self.assertIsNotNone(result)

    def test_stale_premise_story_argument_is_discarded_after_expansion(self):
        """Regression: a caller may pass a pre-expansion premise Story (this
        is exactly what test_full_pipeline.py's fresh-discovery path used to
        do, before it was fixed to stop passing story= at all — see CLAUDE.md
        §9.5). For script_source="ai_generated" that argument must never
        reach blueprint/section generation: it predates the real expanded
        story _ensure_ai_story_expanded() just wrote to
        content.source_excerpt, and would silently ground the script on a
        3-6 sentence premise instead of the real story."""
        premise = "A short premise about a locked attic."
        db, content = self._fixtures(premise=premise)
        expanded = " ".join(["word"] * 1300)

        stale_story = Story(
            url=content.source_url, title="Stale Premise Title", body=premise,
            language="en", source_type="ai_generated", source_value="claude_synthesis",
            upvotes=0, comments=0,
        )

        captured: dict = {}

        def _capturing_blueprint(story, channel, **kwargs):
            captured["story"] = story
            return _fake_blueprint(story, channel, **kwargs)

        with (
            patch.object(script_workflow_mod, "expand_story_premise", return_value=expanded),
            patch.object(script_workflow_mod, "generate_story_blueprint", side_effect=_capturing_blueprint),
            patch.object(script_workflow_mod, "generate_script_sections", side_effect=_fake_sections),
        ):
            result = script_workflow_mod.generate_parent_source_script(content, db, story=stale_story)

        self.assertIsNotNone(result)
        # The story actually sent to blueprint generation must carry the
        # EXPANDED body — never the stale pre-expansion premise the caller
        # passed in.
        self.assertEqual(captured["story"].body, expanded)
        self.assertNotEqual(captured["story"].body, premise)
        self.assertEqual(content.source_excerpt, expanded)

    def test_reddit_content_unaffected_expand_never_called(self):
        """Regression: a reddit-sourced content (script_source='reddit')
        must never call expand_story_premise() at all."""
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db = _FakeWorkflowDb()
        db.add(Channel(id=channel_id, niche="horror", tone="tense"))
        db.add(ChannelConfig(
            channel_id=channel_id, script_format="youtube_long", script_source="reddit",
            visual_style="story_driven", image_style="photorealistic", audio_tags_enabled=False,
        ))
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="APPROVED", title="T",
            source_url="https://reddit.com/r/nosleep/x",
            source_excerpt=" ".join(["word"] * 1200),
        )
        db.add(content)

        with (
            patch.object(script_workflow_mod, "expand_story_premise") as expand_mock,
            patch.object(script_workflow_mod, "generate_story_blueprint", side_effect=_fake_blueprint),
            patch.object(script_workflow_mod, "generate_script_sections", side_effect=_fake_sections),
        ):
            result = script_workflow_mod.generate_parent_source_script(content, db)

        expand_mock.assert_not_called()
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
