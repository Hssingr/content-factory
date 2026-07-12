"""Telegram approval-loop chatbot handling + story-freshness harness fixes.

A post-implementation review of AI-Generated Story Discovery
(``code_report/ai_generated_story_discovery_design.md``, CLAUDE.md §9.5)
found two real gaps in ``test_pipeline/test_full_pipeline.py``, the
operator's real-money harness:

1. ``_run_telegram_approval()`` force-approved content regardless of what
   the operator actually typed back on Telegram — a CHANGE/feedback reply
   was silently discarded and the (rejected) story proceeded to script
   generation anyway. This module proves the fixed version actually
   branches on APPROVE vs. CHANGE, mirroring production's
   ``validation._handle_change()`` (reject, re-discover with the feedback
   threaded into ``rejected_stories``, re-send for approval, loop), capped
   by ``validation_max_revisions``.
2. ``_run_step_scripts()`` used to thread a captured discovery-time
   ``Story`` object through to ``generate_parent_source_script()``. That
   object goes stale the moment ``content.source_excerpt`` changes after
   discovery — guaranteed for ``script_source="ai_generated"`` (the premise
   Story predates ``_ensure_ai_story_expanded()``'s in-place expansion) and
   possible for any script_source after a CHANGE-driven re-discovery inside
   fix #1 above (a new ``content`` row, but the old ``story`` object). The
   fix (CLAUDE.md §9.5) is to never pass ``story=`` at all, statically
   proven here; the resulting data-flow correctness at
   ``generate_parent_source_script()`` itself is proven separately in
   ``tests/test_ai_generated_script_source.py``
   (``test_stale_premise_story_argument_is_discarded_after_expansion``).

Also covers ``build_telegram_message()``'s new deterministic story-preview
line (2-3 sentences from ``Content.source_excerpt``, no Claude call) — the
operator previously saw only a title and a source label, which for
``script_source="ai_generated"`` is a static string with nothing to click.

3. A further operator correction: the CHANGE flow above (reject the
   candidate, re-discover a DIFFERENT one) is right for ``"reddit"`` — there
   is no "premise" to iterate on for a real discovered post — but wrong for
   ``"ai_generated"``, where the Telegram exchange is meant to be a real,
   continuing conversation about the SAME story: the operator comments,
   Claude adjusts the SAME premise using the full conversation history, and
   this repeats until APPROVE. ``validation._handle_change()`` now branches
   on ``script_source``: ``"ai_generated"`` dispatches to
   ``_handle_change_ai_generated()`` (revises ``content.title``/
   ``source_excerpt`` in place, keeping the transcript in
   ``content.story_blueprint["ai_conversation"]``; ``content``/
   ``ContentValidation`` never change identity or get rejected), while every
   other ``script_source`` keeps the original reject-and-rediscover
   behavior. ``test_pipeline.test_full_pipeline._run_telegram_approval()``
   mirrors the same split for the synchronous operator harness.

Runtime proof (CLAUDE.md §19.4): the real
``test_pipeline.test_full_pipeline._run_telegram_approval()`` and the real
``validation._handle_change()``/``_handle_change_ai_generated()`` run
against fake DB and fake channel/content fixtures, with only the
external/paid boundaries stubbed — Telegram network I/O (``poll_telegram``,
``send_for_validation``, ``celery_app.send_task``) and the paid
``story_research``/``story_synthesis`` boundary (``run_discovery``,
``revise_story_premise``). Every other line — the APPROVE/CHANGE branch, the
``script_source`` dispatch, the conversation-transcript accumulation, the
revision-count bookkeeping, the max-revisions auto-approve, and the
loop-and-resend control flow — is the real, unmodified implementation.
"""

import unittest
import uuid
from unittest.mock import patch

import test_pipeline.test_full_pipeline as pipeline
from app.agents.agent2_discovery.services import discovery as discovery_mod
from app.agents.agent2_discovery.services import story_generator as story_generator_mod
from app.agents.agent2_discovery.services import validation as validation_mod
from app.agents.agent2_discovery.services.validation import _handle_change
from app.agents.agent2_discovery.system_prompt import build_telegram_message
from app.models import Channel, ChannelConfig, ChannelLanguage, Content, ContentValidation, User


# ═══════════════════════════════════════════════════════════════════════════
# 1: build_telegram_message() — deterministic story preview
# ═══════════════════════════════════════════════════════════════════════════

class TestTelegramPreview(unittest.TestCase):
    def test_preview_included_from_source_excerpt(self):
        msg = build_telegram_message(
            title="The Locked Attic",
            url="https://reddit.com/r/nosleep/abc",
            assessment=None,
            target_languages=None,
            user_language="en",
            source_excerpt=(
                "Sam found the attic door nailed shut from the outside. "
                "His sister said their father never went up there. "
                "That night, something knocked twice."
            ),
        )
        self.assertIn("*Preview:*", msg)
        self.assertIn("Sam found the attic door nailed shut from the outside.", msg)
        self.assertIn("His sister said their father never went up there.", msg)
        self.assertIn("That night, something knocked twice.", msg)

    def test_preview_capped_at_three_sentences(self):
        excerpt = " ".join(f"Sentence number {i}." for i in range(1, 10))
        msg = build_telegram_message(
            title="T", url="https://example.com/x", assessment=None,
            target_languages=None, user_language="en", source_excerpt=excerpt,
        )
        self.assertIn("Sentence number 1.", msg)
        self.assertIn("Sentence number 2.", msg)
        self.assertIn("Sentence number 3.", msg)
        self.assertNotIn("Sentence number 4.", msg)

    def test_preview_omitted_when_source_excerpt_empty(self):
        msg = build_telegram_message(
            title="T", url="https://example.com/x", assessment=None,
            target_languages=None, user_language="en", source_excerpt="",
        )
        self.assertNotIn("*Preview:*", msg)

    def test_preview_omitted_by_default(self):
        """Regression: source_excerpt defaults to '' — every pre-existing
        caller that doesn't pass it keeps producing the exact same message
        shape as before this change."""
        msg = build_telegram_message(
            title="T", url="https://example.com/x", assessment=None,
            target_languages=None, user_language="en",
        )
        self.assertNotIn("*Preview:*", msg)

    def test_preview_is_ai_generated_premise_itself_no_url_to_click(self):
        """The one case this fix specifically targets: script_source=
        'ai_generated' shows a static source label, not a link — the
        preview is the ONLY way the operator sees what the story is about."""
        msg = build_telegram_message(
            title="The Locked Attic",
            url=f"discovery://ai_generated/{uuid.uuid4()}/{uuid.uuid4()}",
            assessment=None, target_languages=None, user_language="en",
            source_excerpt="A teenager finds a door in their basement that wasn't there yesterday.",
        )
        self.assertIn("AI-generated original story premise (no external source)", msg)
        self.assertIn("A teenager finds a door in their basement that wasn't there yesterday.", msg)

    def test_long_sentences_truncated_with_ellipsis(self):
        excerpt = "This is a single very long sentence that goes on and on " * 10 + "."
        msg = build_telegram_message(
            title="T", url="https://example.com/x", assessment=None,
            target_languages=None, user_language="en", source_excerpt=excerpt,
        )
        preview_line = next(line for line in msg.splitlines() if line.startswith("*Preview:*"))
        self.assertTrue(preview_line.rstrip().endswith("…"))
        self.assertLessEqual(len(preview_line), len("*Preview:* ") + 400 + 1)

    def test_preview_localized_label_per_language(self):
        msg_fr = build_telegram_message(
            title="T", url="https://example.com/x", assessment=None,
            target_languages=None, user_language="fr", source_excerpt="Une phrase.",
        )
        self.assertIn("*Aperçu:*", msg_fr)


# ═══════════════════════════════════════════════════════════════════════════
# 2: _run_telegram_approval() — real APPROVE/CHANGE/timeout/max-revisions loop
# ═══════════════════════════════════════════════════════════════════════════

class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeApprovalDb:
    """Fake session for _run_telegram_approval()'s own query surface.

    send_for_validation() and run_discovery() are the external/paid
    boundaries and are stubbed out entirely (see each test) — this fake only
    supports the queries _run_telegram_approval() issues directly:
    ChannelConfig, ChannelLanguage, ContentValidation, Content. It models
    exactly one "current" content/validation pair at a time, matching how
    the function is actually used (strictly sequential: query right after
    the content just became current).
    """

    def __init__(self, config, languages, content):
        self.config = config
        self.languages = languages
        self.current_content = content
        self.current_validation = None
        self.commits = 0

    def get(self, model, pk):
        name = model.__name__
        if name == "ChannelConfig":
            return self.config
        if name == "Content":
            return self.current_content
        return None

    def query(self, model):
        name = model.__name__
        if name == "ChannelLanguage":
            return _FakeQuery(self.languages)
        if name == "ContentValidation":
            return _FakeQuery([self.current_validation] if self.current_validation else [])
        return _FakeQuery([])

    def commit(self):
        self.commits += 1


def _channel_fixture(*, max_revisions: int = 3, script_source: str = "reddit"):
    channel_id = uuid.uuid4()
    channel = Channel(
        id=channel_id, niche="history", tone="documentary",
        description="A channel about famous ancient military campaigns.", user_id=uuid.uuid4(),
    )
    config = ChannelConfig(
        channel_id=channel_id, validation_max_revisions=max_revisions, script_source=script_source,
    )
    languages = [ChannelLanguage(channel_id=channel_id, language="fr")]
    return channel, config, languages


def _content_fixture(channel, *, title="Story A", url="https://reddit.com/r/nosleep/a"):
    return Content(
        id=uuid.uuid4(), channel_id=channel.id, is_short_episode=False,
        source_language="en", status="PENDING_APPROVAL", title=title, source_url=url,
        source_excerpt="Some story body.",
    )


class TestTelegramApprovalLoop(unittest.TestCase):
    def _make_send_for_validation(self):
        """Records every call; sets db.current_validation with a fresh
        telegram_message_id, exactly like the real function's DB write."""
        calls: list[dict] = []

        def _fake(content, channel, db, assessment=None, target_languages=None):
            calls.append({
                "content_id": content.id, "title": content.title,
                "assessment": assessment, "target_languages": target_languages,
            })
            db.current_content = content
            db.current_validation = ContentValidation(
                content_id=content.id,
                telegram_message_id=f"msg-{len(calls)}",
                status="PENDING",
                revision_count=0,
            )

        return _fake, calls

    def test_straightforward_approve(self):
        channel, config, languages = _channel_fixture()
        content = _content_fixture(channel)
        db = _FakeApprovalDb(config, languages, content)
        send_fake, send_calls = self._make_send_for_validation()

        with (
            patch.object(pipeline, "poll_telegram", return_value=("APPROVE", "alice")) as poll_mock,
            patch("app.agents.agent2_discovery.services.validation.send_for_validation", side_effect=send_fake),
            patch.object(discovery_mod, "run_discovery") as discovery_mock,
        ):
            result_content, result_channel = pipeline._run_telegram_approval(content, channel, db)

        self.assertEqual(result_content.id, content.id)
        self.assertEqual(result_content.status, "APPROVED")
        self.assertEqual(len(send_calls), 1)
        poll_mock.assert_called_once()
        discovery_mock.assert_not_called()

    def test_timeout_auto_approves(self):
        channel, config, languages = _channel_fixture()
        content = _content_fixture(channel)
        db = _FakeApprovalDb(config, languages, content)
        send_fake, send_calls = self._make_send_for_validation()

        with (
            patch.object(pipeline, "poll_telegram", return_value=("APPROVE", "timeout")),
            patch("app.agents.agent2_discovery.services.validation.send_for_validation", side_effect=send_fake),
            patch.object(discovery_mod, "run_discovery") as discovery_mock,
        ):
            result_content, _ = pipeline._run_telegram_approval(content, channel, db)

        self.assertEqual(result_content.status, "APPROVED")
        self.assertEqual(len(send_calls), 1)
        discovery_mock.assert_not_called()

    def test_change_feedback_rejects_and_rediscovers_then_approves(self):
        """The money test: a CHANGE reply must not be silently discarded.

        First reply is feedback (not APPROVE) -> the ORIGINAL content is
        rejected (FAILED/REJECTED, feedback logged), run_discovery() is
        called with that feedback in rejected_stories, and the NEW candidate
        is sent for approval in turn. Second reply is APPROVE -> the NEW
        content (not the original) ends up APPROVED.
        """
        channel, config, languages = _channel_fixture(max_revisions=3)
        original = _content_fixture(channel, title="Story A", url="https://reddit.com/r/nosleep/a")
        replacement = _content_fixture(channel, title="Story B", url="https://reddit.com/r/nosleep/b")
        db = _FakeApprovalDb(config, languages, original)
        send_fake, send_calls = self._make_send_for_validation()

        discovery_calls: list[dict] = []

        def fake_run_discovery(channel_id, db_arg, rejected_stories=None):
            discovery_calls.append({"channel_id": channel_id, "rejected_stories": rejected_stories})
            db_arg.current_content = replacement
            return replacement, object(), {"overall_score": 80}

        poll_responses = iter([
            ("This isn't scary enough, find something creepier", "alice"),
            ("APPROVE", "alice"),
        ])

        with (
            patch.object(pipeline, "poll_telegram", side_effect=lambda *a, **k: next(poll_responses)),
            patch("app.agents.agent2_discovery.services.validation.send_for_validation", side_effect=send_fake),
            patch.object(discovery_mod, "run_discovery", side_effect=fake_run_discovery),
        ):
            result_content, _ = pipeline._run_telegram_approval(original, channel, db)

        # Two Telegram sends: the rejected original, then the replacement.
        self.assertEqual(len(send_calls), 2)
        self.assertEqual(send_calls[0]["content_id"], original.id)
        self.assertEqual(send_calls[1]["content_id"], replacement.id)

        # Discovery was re-run exactly once, with the operator's feedback
        # threaded into rejected_stories against the ORIGINAL story.
        self.assertEqual(len(discovery_calls), 1)
        rejected = discovery_calls[0]["rejected_stories"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["title"], "Story A")
        self.assertEqual(rejected[0]["url"], "https://reddit.com/r/nosleep/a")
        self.assertIn("creepier", rejected[0]["feedback"])

        # The original was rejected, not silently approved.
        self.assertEqual(original.status, "FAILED")

        # The FINAL result is the replacement, approved — not the original.
        self.assertEqual(result_content.id, replacement.id)
        self.assertEqual(result_content.status, "APPROVED")

    def test_ai_generated_change_revises_same_story_conversationally(self):
        """script_source='ai_generated' counterpart to the reddit money test
        above: two rounds of CHANGE feedback must revise the SAME content
        row conversationally (never reject/replace it with a different
        story), then APPROVE keeps that same row."""
        channel, config, languages = _channel_fixture(max_revisions=3, script_source="ai_generated")
        content = _content_fixture(channel, title="A General's Gamble", url=f"discovery://ai_generated/{channel.id}/{uuid.uuid4()}")
        original_content_id = content.id
        db = _FakeApprovalDb(config, languages, content)
        send_fake, send_calls = self._make_send_for_validation()

        revise_responses = iter([
            {"title": "Hannibal in Rome", "body": "First revision — mentions Hannibal."},
            {"title": "Hannibal: Carthage's War Against Rome",
             "body": "Second revision — mentions Hannibal and the elephants crossing the Alps."},
        ])
        revise_calls: list[dict] = []

        def fake_revise(channel, language, conversation):
            revise_calls.append({"conversation": list(conversation)})
            return next(revise_responses)

        poll_responses = iter([
            ("Be specific — this is about Hannibal", "alice"),
            ("Also mention the elephants crossing the Alps", "alice"),
            ("APPROVE", "alice"),
        ])

        with (
            patch.object(pipeline, "poll_telegram", side_effect=lambda *a, **k: next(poll_responses)),
            patch("app.agents.agent2_discovery.services.validation.send_for_validation", side_effect=send_fake),
            patch.object(discovery_mod, "run_discovery") as discovery_mock,
            patch.object(story_generator_mod, "revise_story_premise", side_effect=fake_revise),
        ):
            result_content, _ = pipeline._run_telegram_approval(content, channel, db)

        # run_discovery() must NEVER be called for this path — no rejection,
        # no search for a different story.
        discovery_mock.assert_not_called()

        # Exactly 3 Telegram sends: original, revision 1, revision 2.
        self.assertEqual(len(send_calls), 3)

        # SAME content row throughout — never replaced.
        self.assertEqual(result_content.id, original_content_id)
        self.assertEqual(result_content.status, "APPROVED")
        self.assertEqual(result_content.title, "Hannibal: Carthage's War Against Rome")
        self.assertIn("elephants", result_content.source_excerpt)

        # revise_story_premise() was called twice, and the SECOND call's
        # conversation includes the FIRST call's revision — a real,
        # continuing conversation, not two independent one-shot calls.
        self.assertEqual(len(revise_calls), 2)
        second_conversation = revise_calls[1]["conversation"]
        self.assertEqual(second_conversation[2], {
            "role": "assistant", "title": "Hannibal in Rome", "body": "First revision — mentions Hannibal.",
        })

        # The full transcript is persisted on the content row.
        persisted = result_content.story_blueprint["ai_conversation"]
        self.assertEqual(len(persisted), 5)  # orig + fb1 + rev1 + fb2 + rev2

    def test_max_revisions_reached_auto_approves_without_further_discovery(self):
        """validation_max_revisions=1: a single CHANGE reply immediately
        exhausts the budget and auto-approves that same candidate — mirrors
        _apply_limit_policy()'s default (auto_approve) — rather than calling
        run_discovery() again."""
        channel, config, languages = _channel_fixture(max_revisions=1)
        content = _content_fixture(channel)
        db = _FakeApprovalDb(config, languages, content)
        send_fake, send_calls = self._make_send_for_validation()

        with (
            patch.object(pipeline, "poll_telegram", return_value=("no, try again", "alice")),
            patch("app.agents.agent2_discovery.services.validation.send_for_validation", side_effect=send_fake),
            patch.object(discovery_mod, "run_discovery") as discovery_mock,
        ):
            result_content, _ = pipeline._run_telegram_approval(content, channel, db)

        discovery_mock.assert_not_called()
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(result_content.id, content.id)
        self.assertEqual(result_content.status, "APPROVED")
        self.assertEqual(db.current_validation.status, "APPROVED")

    def test_no_replacement_found_stops_without_approving(self):
        """run_discovery() returning None after a CHANGE reply (e.g. it fell
        through to the manual-fallback flow) must not silently approve the
        rejected original."""
        channel, config, languages = _channel_fixture(max_revisions=3)
        content = _content_fixture(channel)
        db = _FakeApprovalDb(config, languages, content)
        send_fake, _ = self._make_send_for_validation()

        with (
            patch.object(pipeline, "poll_telegram", return_value=("not interested", "alice")),
            patch("app.agents.agent2_discovery.services.validation.send_for_validation", side_effect=send_fake),
            patch.object(discovery_mod, "run_discovery", return_value=None) as discovery_mock,
        ):
            result_content, _ = pipeline._run_telegram_approval(content, channel, db)

        discovery_mock.assert_called_once()
        self.assertEqual(result_content.status, "FAILED")
        self.assertNotEqual(result_content.status, "APPROVED")


# ═══════════════════════════════════════════════════════════════════════════
# 2b: validation._handle_change() — script_source dispatch (conversational
#     revision for ai_generated vs. reject-and-rediscover for everything else)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeChangeDb:
    """Fake session for validation._handle_change()/_handle_change_ai_generated()'s
    own query surface: db.get(ChannelConfig, ...) and db.get(User, ...) only —
    everything else in the ai_generated branch is either mutated in place on
    the passed-in ORM objects or goes through the stubbed
    revise_story_premise()."""

    def __init__(self, config, user):
        self.config = config
        self.user = user
        self.commits = 0

    def get(self, model, _pk):
        name = model.__name__
        if name == "ChannelConfig":
            return self.config
        if name == "User":
            return self.user
        return None

    def commit(self):
        self.commits += 1


def _change_fixture(*, script_source: str, max_revisions: int = 3):
    channel_id = uuid.uuid4()
    channel = Channel(id=channel_id, niche="history", tone="documentary", user_id=uuid.uuid4())
    config = ChannelConfig(
        channel_id=channel_id, validation_max_revisions=max_revisions, script_source=script_source,
        validation_on_limit_reached="auto_approve",
    )
    user = User(id=channel.user_id, telegram_chat_id="12345", primary_language="en")
    content = Content(
        id=uuid.uuid4(), channel_id=channel_id, is_short_episode=False,
        source_language="en", status="PENDING_APPROVAL",
        title="A General's Gamble", source_url=f"discovery://ai_generated/{channel_id}/{uuid.uuid4()}",
        source_excerpt="A vague premise about a general.", story_blueprint=None,
    )
    validation = ContentValidation(
        content_id=content.id, telegram_message_id="msg-1", status="PENDING", revision_count=0,
    )
    db = _FakeChangeDb(config, user)
    return channel, content, validation, db


class TestHandleChangeAiGeneratedConversation(unittest.TestCase):
    """The money test for CLAUDE.md §9.5's "Conversational premise revision":
    a CHANGE reply for script_source="ai_generated" must revise the SAME
    content/validation row using the real revise_story_premise() call path,
    never reject/replace it — the opposite of the "reddit" CHANGE path."""

    def test_ai_generated_change_revises_same_row_in_place(self):
        channel, content, validation, db = _change_fixture(script_source="ai_generated")
        original_content_id = content.id

        with patch.object(
            story_generator_mod, "revise_story_premise",
            return_value={"title": "Hannibal: Carthage's War Against Rome",
                          "body": "Hannibal leads his army, elephants included, across the Alps to fight Rome."},
        ) as revise_mock:
            result = _handle_change(validation, content, channel, "Be specific — this is about Hannibal", db)

        self.assertIsNotNone(result)
        chat_id, message = result
        self.assertEqual(chat_id, "12345")
        self.assertIn("Hannibal", message)

        # SAME row throughout — never rejected, never replaced.
        self.assertEqual(content.id, original_content_id)
        self.assertEqual(content.status, "PENDING_APPROVAL")
        self.assertEqual(validation.status, "PENDING")
        self.assertEqual(validation.revision_count, 1)

        # The revised text actually landed on the content row.
        self.assertEqual(content.title, "Hannibal: Carthage's War Against Rome")
        self.assertIn("Hannibal", content.source_excerpt)

        # revise_story_premise() received the FULL conversation — seeded
        # with the ORIGINAL premise as turn 1, the feedback as turn 2.
        conversation = revise_mock.call_args.kwargs["conversation"]
        self.assertEqual(conversation[0], {
            "role": "assistant", "title": "A General's Gamble",
            "body": "A vague premise about a general.",
        })
        self.assertEqual(conversation[1], {
            "role": "operator", "feedback": "Be specific — this is about Hannibal",
        })

        # Conversation persisted for the next round, ending with the new revision.
        persisted = content.story_blueprint["ai_conversation"]
        self.assertEqual(len(persisted), 3)
        self.assertEqual(persisted[2]["title"], "Hannibal: Carthage's War Against Rome")

    def test_second_revision_sees_first_revision_in_transcript(self):
        """Two rounds of feedback: the SECOND call to revise_story_premise()
        must see the FIRST revision's title/body as its own turn 3, not just
        the original premise — this is what makes it a real conversation."""
        channel, content, validation, db = _change_fixture(script_source="ai_generated")

        with patch.object(
            story_generator_mod, "revise_story_premise",
            return_value={"title": "Hannibal in Rome", "body": "First revision body."},
        ):
            _handle_change(validation, content, channel, "Mention Hannibal", db)

        # Snapshot the conversation list AT CALL TIME (list(conversation)) —
        # _handle_change_ai_generated() appends the new assistant turn to
        # this SAME list object after the call returns, so inspecting
        # call_args after the fact would see that later mutation too.
        captured: dict = {}

        def fake_revise_2(channel, language, conversation):
            captured["conversation"] = list(conversation)
            return {"title": "Hannibal: Carthage's War Against Rome", "body": "Second revision body."}

        with patch.object(story_generator_mod, "revise_story_premise", side_effect=fake_revise_2):
            _handle_change(validation, content, channel, "Also mention the elephants", db)

        conversation = captured["conversation"]
        # turn0=original assistant, turn1=feedback1, turn2=revision1(assistant),
        # turn3=feedback2 (the one just given).
        self.assertEqual(len(conversation), 4)
        self.assertEqual(conversation[2], {
            "role": "assistant", "title": "Hannibal in Rome", "body": "First revision body.",
        })
        self.assertEqual(conversation[3], {"role": "operator", "feedback": "Also mention the elephants"})
        self.assertEqual(validation.revision_count, 2)

    def test_revision_failure_leaves_content_unchanged_but_logs_feedback(self):
        channel, content, validation, db = _change_fixture(script_source="ai_generated")
        original_title = content.title

        with patch.object(story_generator_mod, "revise_story_premise", return_value=None):
            result = _handle_change(validation, content, channel, "make it scarier", db)

        self.assertIsNotNone(result)
        _, message = result
        self.assertIn("Could not revise", message)
        self.assertEqual(content.title, original_title)
        self.assertEqual(content.status, "PENDING_APPROVAL")
        # The feedback turn is still persisted so the next attempt's
        # transcript reflects it.
        self.assertEqual(
            content.story_blueprint["ai_conversation"][-1],
            {"role": "operator", "feedback": "make it scarier"},
        )

    def test_max_revisions_reached_applies_limit_policy_not_a_new_revision(self):
        channel, content, validation, db = _change_fixture(script_source="ai_generated", max_revisions=1)

        with patch.object(story_generator_mod, "revise_story_premise") as revise_mock:
            result = _handle_change(validation, content, channel, "one more change", db)

        revise_mock.assert_not_called()
        self.assertIsNone(result)
        self.assertEqual(validation.status, "APPROVED")
        self.assertEqual(content.status, "APPROVED")

    def test_reddit_change_unaffected_still_rejects_and_rediscovers(self):
        """Regression: script_source='reddit' must take the ORIGINAL
        reject-and-rediscover path, never the ai_generated conversation path."""
        channel, content, validation, db = _change_fixture(script_source="reddit")

        with (
            patch.object(story_generator_mod, "revise_story_premise") as revise_mock,
            patch("app.scheduler.celery_app.send_task") as send_mock,
        ):
            result = _handle_change(validation, content, channel, "not interested", db)

        revise_mock.assert_not_called()
        send_mock.assert_called_once()
        self.assertEqual(content.status, "FAILED")
        self.assertEqual(validation.status, "REJECTED")
        self.assertIsNotNone(result)
        self.assertIn("Story rejected", result[1])


# ═══════════════════════════════════════════════════════════════════════════
# 3: _run_step_scripts() no longer threads a captured Story through
# ═══════════════════════════════════════════════════════════════════════════

class TestNoStaleStoryThreading(unittest.TestCase):
    """Static shape proof (matches the existing precedent in
    tests/test_full_pipeline_control_flow.py) that the harness never passes
    a captured discovery-time Story into generate_parent_source_script() —
    the actual data-flow correctness of *not* passing one is proven
    separately in tests/test_ai_generated_script_source.py."""

    def test_run_step_scripts_calls_generate_parent_source_script_with_no_story_kwarg(self):
        import inspect
        src = inspect.getsource(pipeline._run_step_scripts)
        self.assertIn("generate_parent_source_script(content, db)", src)
        self.assertNotIn("story=story", src)
        # Only the call site matters — the docstring above it legitimately
        # discusses story=None as the (now-only) behavior in prose.
        call_line = next(line for line in src.splitlines() if "generate_parent_source_script(" in line)
        self.assertNotIn("story=", call_line)

    def test_run_step_scripts_signature_has_no_story_parameter(self):
        import inspect
        sig = inspect.signature(pipeline._run_step_scripts)
        self.assertNotIn("story", sig.parameters)

    def test_main_run_function_never_passes_story_kwarg(self):
        import inspect
        src = inspect.getsource(pipeline.run)
        self.assertNotIn("story=story", src)
        # The unpacked Story from run_discovery()'s 3-tuple is intentionally
        # discarded (it is only ever used for the AI-generated-story
        # staleness/CHANGE-loop hazards documented in CLAUDE.md §9.5).
        self.assertIn("content, _, assessment = result", src)


if __name__ == "__main__":
    unittest.main()
