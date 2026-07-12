"""Telegram CHANGE flow rework (fresh full-system audit §1.3).

Scripts are generated only AFTER approval, so the old revise-the-script CHANGE
flow was unreachable (no script ever exists while a validation is PENDING) and
silently swallowed the operator's feedback. A CHANGE reply is now story-level
feedback: reject the story, re-dispatch discovery with the feedback threaded
into the exclusion context, and reply honestly.

Runtime proof: the real ``_handle_change`` runs against a fake DB with only the
Celery dispatch boundary captured; the real ``fetch_batch`` exclusion-block
builder is exercised for the feedback line (Claude boundary stubbed).
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery.services import validation as validation_mod
from app.agents.agent2_discovery.services.validation import _handle_change


class _FakeDb:
    def __init__(self, objects_by_type=None):
        self.objects = objects_by_type or {}
        self.committed = 0

    def get(self, model, pk):
        return self.objects.get(model.__name__)

    def commit(self):
        self.committed += 1

    def flush(self):
        pass


def _fixture():
    channel = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), niche="horror", tone="suspenseful")
    content = SimpleNamespace(
        id=uuid.uuid4(), title="The Old Well", source_url="https://reddit.com/r/nosleep/x",
        status="PENDING_APPROVAL", story_blueprint=None,
    )
    validation = SimpleNamespace(
        revision_count=0, script_issues_log=None, status="PENDING",
    )
    user = SimpleNamespace(telegram_chat_id="12345", primary_language="en")
    config = SimpleNamespace(
        validation_max_revisions=3, validation_on_limit_reached="auto_approve", script_source="reddit",
    )
    db = _FakeDb({
        "ChannelConfig": config,
        "User": user,
    })
    return channel, content, validation, db


class TestStoryFeedbackChangeFlow(unittest.TestCase):
    def test_change_rejects_story_and_redispatches_discovery_with_feedback(self):
        channel, content, validation, db = _fixture()
        sent_tasks = []

        with patch.object(
            validation_mod, "_apply_limit_policy",
        ) as limit_mock, patch(
            "app.scheduler.celery_app.send_task",
            side_effect=lambda name, kwargs: sent_tasks.append((name, kwargs)),
        ):
            result = _handle_change(validation, content, channel, "too gory, want a slow burn", db)

        limit_mock.assert_not_called()
        self.assertEqual(validation.status, "REJECTED")
        self.assertEqual(content.status, "FAILED")
        self.assertEqual(validation.revision_count, 1)
        # Feedback persisted to the issues log
        self.assertEqual(validation.script_issues_log[-1]["feedback"], "too gory, want a slow burn")
        # Discovery re-dispatched with the rejected story + feedback
        self.assertEqual(len(sent_tasks), 1)
        name, kwargs = sent_tasks[0]
        self.assertEqual(name, "app.scheduler.tasks.run_agent2_for_channel")
        self.assertEqual(kwargs["channel_id"], str(channel.id))
        rejected = kwargs["rejected_stories"]
        self.assertEqual(rejected[0]["title"], "The Old Well")
        self.assertEqual(rejected[0]["feedback"], "too gory, want a slow burn")
        # The operator gets an honest reply — never silence
        self.assertIsNotNone(result)
        chat_id, message = result
        self.assertEqual(chat_id, "12345")
        self.assertIn("Story rejected", message)
        self.assertIn("too gory", message)

    def test_limit_reached_applies_policy_instead_of_searching_forever(self):
        channel, content, validation, db = _fixture()
        validation.revision_count = 2  # next CHANGE hits max_revisions=3

        with patch.object(validation_mod, "_apply_limit_policy") as limit_mock, \
             patch("app.scheduler.celery_app.send_task") as send_mock:
            result = _handle_change(validation, content, channel, "still not right", db)

        limit_mock.assert_called_once()
        send_mock.assert_not_called()
        self.assertIsNone(result)

    def test_fetcher_exclusion_block_carries_operator_feedback(self):
        """The real fetch_batch builds the exclusion block with the feedback
        line — only the Claude boundary is stubbed."""
        from app.agents.agent2_discovery.services import fetcher

        captured = {}

        def fake_tools_call(system_prompt, user_message, **kwargs):
            captured["user_message"] = user_message
            return "not json"  # force the parse to fail after capture

        with patch.object(fetcher, "call_claude_with_tools", side_effect=fake_tools_call), \
             patch.object(fetcher, "call_claude", return_value="still not json"):
            fetcher.fetch_batch(
                [("r/nosleep", "reddit", 1.0)],
                niche="horror",
                rejected_stories=[{
                    "title": "The Old Well", "url": "https://reddit.com/x",
                    "feedback": "too gory, want a slow burn",
                }],
            )

        self.assertIn("Operator feedback: too gory, want a slow burn", captured["user_message"])

    def test_revision_chain_symbols_are_gone_from_validation_module(self):
        self.assertFalse(hasattr(validation_mod, "generate_revised_scripts"))


if __name__ == "__main__":
    unittest.main()
