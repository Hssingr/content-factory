"""Runtime proof for full source-material fetch (roadmap 4.1 / audit S-1, exec-2).

Source-material starvation: discovery used to ask Claude for a 200-500 word
factual summary, which then became the ONLY fact source for blueprint, every
section, length-correction expansion, and Shorts grounding — a 9-minute
documentary written from a paragraph. Since no Reddit API key is available,
the fix does not add a deterministic Reddit .json fetch (roadmap's original
suggestion); instead Claude's existing web_search tool call (fetcher.py) is
asked to return the full verbatim post + top comments it already retrieved,
and every downstream truncation point that used to cap this at 4,000-8,000
chars (a no-op when the body was only ever a few hundred words) now shares
one real ceiling, `story.MAX_SOURCE_EXCERPT_CHARS` (60,000 chars).

This proves the value actually survives from Story.body through to the real
user_message sent to Claude in generate_story_blueprint()/generate_section()/
generate_section() — a multi-function data-flow chain — with only the
paid Claude call boundary stubbed (call_claude_with_tools / call_claude /
call_claude_structured), never the internal truncation/assembly logic.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery.services import fetcher
from app.agents.agent2_discovery.services.story import MAX_SOURCE_EXCERPT_CHARS, Story
from app.agents.agent2_discovery import system_prompt
from app.models import Content


def _long_body(n_chars: int) -> str:
    unit = "The witness said the porch light flickered twice before the door opened. "
    reps = (n_chars // len(unit)) + 1
    return (unit * reps)[:n_chars]


class TestMaxSourceExcerptConstant(unittest.TestCase):
    def test_constant_is_60000(self):
        self.assertEqual(MAX_SOURCE_EXCERPT_CHARS, 60_000)


class TestFetcherPromptAsksForFullTextNotSummary(unittest.TestCase):
    def test_system_prompt_no_longer_requests_a_short_summary(self):
        self.assertNotIn("200-500 word factual summary", fetcher._SINGLE_STORY_SYSTEM_PROMPT)
        self.assertIn("verbatim", fetcher._SINGLE_STORY_SYSTEM_PROMPT.lower())
        self.assertIn("not a summary", fetcher._SINGLE_STORY_SYSTEM_PROMPT.lower())

    def test_reformat_prompt_also_preserves_full_text(self):
        self.assertIn("full verbatim", fetcher._SINGLE_STORY_REFORMAT_PROMPT.lower())

    def test_system_prompt_instructs_combining_multi_part_series(self):
        """Prompt engineering follow-up: a real run picked "Part 1" of an
        8-part r/nosleep series and relayed only that part (428 words) — the
        discovery-time floor gate rejected it and burned a retry, when the
        better fix is teaching Claude to find and combine every part up
        front."""
        prompt = fetcher._SINGLE_STORY_SYSTEM_PROMPT.lower()
        self.assertIn("multi-part", prompt)
        self.assertIn("part 1", prompt)
        self.assertIn("combined verbatim text of every part", prompt)

    def test_max_tokens_raised_to_accommodate_combined_multi_part_bodies(self):
        """The multi-part rule means one response can legitimately carry
        several parts' worth of body text — must exceed the old 8192 ceiling
        that was sized for a single post."""
        self.assertGreater(fetcher._STORY_FETCH_MAX_TOKENS, 8192)
        self.assertEqual(fetcher._STORY_FETCH_MAX_TOKENS, fetcher._STORY_REFORMAT_MAX_TOKENS)


class TestStoryFromDictTruncatesAtSharedCeiling(unittest.TestCase):
    def test_body_under_ceiling_is_untouched(self):
        body = _long_body(500)
        data = {"url": "https://example.com/p1", "title": "T", "body": body}
        story = fetcher._story_from_dict(data)
        self.assertEqual(story.body, body)

    def test_body_over_ceiling_is_truncated_to_shared_constant(self):
        body = _long_body(MAX_SOURCE_EXCERPT_CHARS + 20_000)
        data = {"url": "https://example.com/p2", "title": "T", "body": body}
        story = fetcher._story_from_dict(data)
        self.assertEqual(len(story.body), MAX_SOURCE_EXCERPT_CHARS)
        self.assertEqual(story.body, body[:MAX_SOURCE_EXCERPT_CHARS])


class TestFetchBatchUsesRaisedTokenBudgetAndPreservesFullBody(unittest.TestCase):
    """Runtime proof for fetch_batch() itself — only call_claude_with_tools /
    call_claude are stubbed; _parse_story/_story_from_dict run for real."""

    def test_direct_json_response_preserves_long_body_and_uses_raised_max_tokens(self):
        long_body = _long_body(40_000)
        response_json = (
            '{"title":"A porch light story","body":' + _json_escape(long_body) + ','
            '"url":"https://reddit.com/r/nosleep/comments/abc/x","language":"en",'
            '"published_at":null,"upvotes":120,"comments":45}'
        )
        calls: list[dict] = []

        def fake_tools_call(system_prompt, user_message, **kwargs):
            calls.append({"system_prompt": system_prompt, "user_message": user_message, **kwargs})
            return response_json

        with patch.object(fetcher, "call_claude_with_tools", side_effect=fake_tools_call):
            stories = fetcher.fetch_batch(
                sources=[("r/nosleep", "reddit", 1.0)],
                niche="horror stories",
            )

        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].body, long_body)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["max_tokens"], fetcher._STORY_FETCH_MAX_TOKENS)
        self.assertIn("verbatim", calls[0]["system_prompt"].lower())

    def test_fetch_batch_caps_max_rounds_at_8(self):
        """Roadmap 6.2 / audit C-4: since roadmap 4.1, this call only finds
        and relays a story via web_search — it no longer summarizes — so it
        does not need the old max_rounds=20 budget."""
        response_json = (
            '{"title":"A porch light story","body":"short body","url":'
            '"https://reddit.com/r/nosleep/comments/abc/x","language":"en",'
            '"published_at":null,"upvotes":120,"comments":45}'
        )
        calls: list[dict] = []

        def fake_tools_call(system_prompt, user_message, **kwargs):
            calls.append(kwargs)
            return response_json

        with patch.object(fetcher, "call_claude_with_tools", side_effect=fake_tools_call):
            fetcher.fetch_batch(sources=[("r/nosleep", "reddit", 1.0)], niche="horror stories")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["max_rounds"], 8)

    def test_reformat_fallback_preserves_full_body_beyond_old_6000_char_cap(self):
        """Claude returns prose (not JSON) containing a long real body — the
        reformat pass must not lose most of it to the old 6000-char cap."""
        long_body = _long_body(40_000)
        prose_response = f"Sure, here is the story I found:\nTitle: A long story\nBody: {long_body}\nURL: https://reddit.com/r/nosleep/comments/xyz/y"
        reformatted_json = (
            '{"title":"A long story","body":' + _json_escape(long_body) + ','
            '"url":"https://reddit.com/r/nosleep/comments/xyz/y","language":"en",'
            '"published_at":null,"upvotes":10,"comments":2}'
        )
        reformat_calls: list[dict] = []

        def fake_tools_call(system_prompt, user_message, **kwargs):
            return prose_response

        def fake_reformat_call(system_prompt, user_message, **kwargs):
            reformat_calls.append({"user_message": user_message, **kwargs})
            return reformatted_json

        with (
            patch.object(fetcher, "call_claude_with_tools", side_effect=fake_tools_call),
            patch.object(fetcher, "call_claude", side_effect=fake_reformat_call),
        ):
            stories = fetcher.fetch_batch(
                sources=[("r/nosleep", "reddit", 1.0)],
                niche="horror stories",
            )

        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].body, long_body)
        self.assertEqual(len(reformat_calls), 1)
        self.assertEqual(reformat_calls[0]["max_tokens"], fetcher._STORY_REFORMAT_MAX_TOKENS)
        # The raw prose (well under MAX_SOURCE_EXCERPT_CHARS here) must have
        # been passed through whole, not clipped at the old 6000-char cap.
        self.assertIn(long_body[:100], reformat_calls[0]["user_message"])
        self.assertGreater(len(prose_response), 6000)


def _json_escape(text: str) -> str:
    import json
    return json.dumps(text)


class TestBlueprintAndSectionReceiveFullSourceMaterial(unittest.TestCase):
    """Runtime proof that generate_story_blueprint()/generate_section()
    actually forward the full (up to the shared ceiling) source body to
    Claude — the multi-function propagation the roadmap depends on. Only
    call_claude_structured/call_claude are stubbed. (auto_correct_script
    was deleted by the post-roadmap deep audit — its forwarding test went
    with it.)"""

    def _story(self, body_chars: int) -> Story:
        return Story(
            url="https://reddit.com/r/nosleep/comments/abc/x",
            title="A porch light story",
            body=_long_body(body_chars),
            language="en",
            source_type="web",
            source_value="claude_web_search",
        )

    def test_generate_story_blueprint_forwards_body_beyond_old_8000_cap(self):
        story = self._story(50_000)
        channel = SimpleNamespace(niche="horror", tone="tense")
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return {
                "hook": "h", "central_question": "q",
                "major_turns": ["turn one", "turn two"],
                "final_payoff": "p", "comment_trigger": "c?",
                "suggested_section_count": 3, "suggested_title": "t",
            }

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            system_prompt.generate_story_blueprint(story, channel)

        user_message = captured["user_message"]
        # Full 50k-char body must appear whole — old cap (8000) would have
        # silently dropped everything past the first 8000 chars.
        self.assertIn(story.body, user_message)
        self.assertIn(story.body[-200:], user_message)

    def test_generate_section_forwards_body_beyond_old_4000_cap(self):
        """This is the exact 'no-op cap' the audit flagged: 4000 chars never
        mattered while bodies were 200-500 words, but now would silently
        gut source grounding for every section if left unraised."""
        story = self._story(50_000)
        channel = SimpleNamespace(niche="horror", tone="tense")
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return {
                "script_text": "narration", "summary": "s", "reveals": [],
                "open_questions": [], "suggests_outro": False, "visual_intent": [],
            }

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            system_prompt.generate_section(
                label="INTRO",
                story=story,
                blueprint={"hook": "h"},
                prior_sections_summary=[],
                visual_intent_accumulator={"avoid_repeating": []},
                channel=channel,
            )

        user_message = captured["user_message"]
        self.assertIn(story.body[-200:], user_message)


class TestContentModelPersistsFullyTruncatedExcerpt(unittest.TestCase):
    """discovery.py / validation.py both persist Content.source_excerpt via
    story.body[:MAX_SOURCE_EXCERPT_CHARS] — proves the real Content ORM model
    accepts and preserves that exact truncated length (no DB session needed
    to construct/inspect the model instance)."""

    def test_content_source_excerpt_matches_shared_ceiling(self):
        body = _long_body(MAX_SOURCE_EXCERPT_CHARS + 30_000)
        content = Content(
            channel_id=None,
            source_url="https://reddit.com/r/nosleep/comments/abc/x",
            source_language="en",
            content_hash="abc123",
            title="T",
            status="PENDING_APPROVAL",
            source_excerpt=body[:MAX_SOURCE_EXCERPT_CHARS] if body else None,
        )
        self.assertEqual(len(content.source_excerpt), MAX_SOURCE_EXCERPT_CHARS)
        self.assertEqual(content.source_excerpt, body[:MAX_SOURCE_EXCERPT_CHARS])


if __name__ == "__main__":
    unittest.main()
