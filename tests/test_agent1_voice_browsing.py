"""Runtime proof for Agent 1's provider voice-catalog browsing
(app/agents/agent1_setup/services/cartesia_voices.py and
.../elevenlabs_voices.py) — paginated, filtered voice search used by the
setup UI's VoicePicker so an operator can find a real Voice ID instead of
pasting one blindly (CLAUDE.md §8/§8.4).

No live API calls (CLAUDE.md §19.1): the Cartesia REST boundary is stubbed
by patching the already-imported `cartesia_voices` module's `httpx.get`
attribute; the ElevenLabs SDK boundary is stubbed by patching
`elevenlabs_voices.elevenlabs_client.get_client` to return a fake client
object exposing the same `.voices.get_shared(...)` call shape the real SDK
uses (verified directly against the installed `elevenlabs==2.51.0` package's
`elevenlabs/voices/client.py`).
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from app.agents.agent1_setup.services import cartesia_voices, elevenlabs_voices


class _FakeHttpxResponse:
    def __init__(self, json_body: dict, status_code: int = 200) -> None:
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json_body


class CartesiaVoiceBrowsingTest(unittest.TestCase):
    def test_missing_api_key_fails_open_without_a_network_call(self) -> None:
        def fail_if_called(*args, **kwargs):
            raise AssertionError("httpx.get should not be called with no API key")

        with patch.object(cartesia_voices.settings, "cartesia_api_key", ""), \
             patch.object(cartesia_voices.httpx, "get", fail_if_called):
            result = cartesia_voices.list_voices(search="ann")

        self.assertEqual(result, {"voices": [], "has_more": False, "next_cursor": None})

    def test_successful_response_is_normalized_and_query_params_are_correct(self) -> None:
        captured = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return _FakeHttpxResponse({
                "data": [
                    {
                        "id": "voice-123",
                        "name": "Annie",
                        "description": "",
                        "tagline": "Warm narrator",
                        "gender": "feminine",
                        "language": "en",
                        "preview_file_url": "https://cdn.example/annie.mp3",
                    },
                ],
                "has_more": True,
                "next_page": "voice-123",
            })

        with patch.object(cartesia_voices.settings, "cartesia_api_key", "test-key"), \
             patch.object(cartesia_voices.httpx, "get", fake_get):
            result = cartesia_voices.list_voices(
                search="ann", gender="feminine", language="en",
                page_size=10, cursor="voice-000",
            )

        self.assertEqual(captured["url"], cartesia_voices._LIST_URL)
        self.assertEqual(captured["headers"]["X-API-Key"], "test-key")
        self.assertEqual(
            captured["headers"]["Cartesia-Version"],
            cartesia_voices.DEFAULT_CARTESIA_VERSION,
        )
        self.assertEqual(captured["params"]["limit"], 10)
        self.assertEqual(captured["params"]["q"], "ann")
        self.assertEqual(captured["params"]["gender"], "feminine")
        self.assertEqual(captured["params"]["language"], "en")
        self.assertEqual(captured["params"]["starting_after"], "voice-000")
        self.assertEqual(captured["params"]["expand[]"], "preview_file_url")

        self.assertEqual(result["has_more"], True)
        self.assertEqual(result["next_cursor"], "voice-123")
        self.assertEqual(len(result["voices"]), 1)
        voice = result["voices"][0]
        self.assertEqual(voice["voice_id"], "voice-123")
        self.assertEqual(voice["name"], "Annie")
        # empty description falls back to tagline
        self.assertEqual(voice["description"], "Warm narrator")
        self.assertEqual(voice["gender"], "feminine")
        self.assertEqual(voice["language"], "en")
        self.assertEqual(voice["preview_url"], "https://cdn.example/annie.mp3")
        self.assertEqual(voice["provider"], "cartesia")

    def test_request_failure_fails_open(self) -> None:
        def fake_get(*args, **kwargs):
            raise RuntimeError("simulated network failure")

        with patch.object(cartesia_voices.settings, "cartesia_api_key", "test-key"), \
             patch.object(cartesia_voices.httpx, "get", fake_get):
            result = cartesia_voices.list_voices()

        self.assertEqual(result, {"voices": [], "has_more": False, "next_cursor": None})

    def test_page_size_is_clamped_to_provider_maximum(self) -> None:
        captured = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            captured["params"] = params
            return _FakeHttpxResponse({"data": [], "has_more": False, "next_page": None})

        with patch.object(cartesia_voices.settings, "cartesia_api_key", "test-key"), \
             patch.object(cartesia_voices.httpx, "get", fake_get):
            cartesia_voices.list_voices(page_size=500)

        self.assertEqual(captured["params"]["limit"], cartesia_voices._MAX_PAGE_SIZE)

    def _bare_list_voices(self):
        return [
            {"id": "v-annie", "name": "Annie", "gender": "feminine", "language": "en", "description": "warm"},
            {"id": "v-ben", "name": "Ben", "gender": "masculine", "language": "en", "description": "deep"},
            {"id": "v-camille", "name": "Camille", "gender": "feminine", "language": "fr", "description": "clear"},
            {"id": "v-diane", "name": "Diane", "gender": "feminine", "language": "en-GB", "description": "crisp"},
        ]

    def test_bare_list_response_regression_the_real_live_shape(self) -> None:
        """The exact shape a real live call returned and crashed on before this
        fix: `response.json()` is a bare list, not `{"data": [...]}`."""
        def fake_get(url, headers=None, params=None, timeout=None):
            return _FakeHttpxResponse(self._bare_list_voices())

        with patch.object(cartesia_voices.settings, "cartesia_api_key", "test-key"), \
             patch.object(cartesia_voices.httpx, "get", fake_get):
            result = cartesia_voices.list_voices(language="fr", gender="feminine", page_size=20)

        self.assertEqual(len(result["voices"]), 1)
        self.assertEqual(result["voices"][0]["voice_id"], "v-camille")
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])

    def test_bare_list_client_side_gender_and_language_filtering(self) -> None:
        def fake_get(url, headers=None, params=None, timeout=None):
            return _FakeHttpxResponse(self._bare_list_voices())

        with patch.object(cartesia_voices.settings, "cartesia_api_key", "test-key"), \
             patch.object(cartesia_voices.httpx, "get", fake_get):
            feminine_en = cartesia_voices.list_voices(gender="feminine", language="en")

        # "en" matches both the exact "en" voice and the "en-GB" voice (prefix
        # match), but not the "fr" feminine voice or the masculine "en" voice.
        ids = {v["voice_id"] for v in feminine_en["voices"]}
        self.assertEqual(ids, {"v-annie", "v-diane"})

    def test_bare_list_client_side_search_matches_name_and_description(self) -> None:
        def fake_get(url, headers=None, params=None, timeout=None):
            return _FakeHttpxResponse(self._bare_list_voices())

        with patch.object(cartesia_voices.settings, "cartesia_api_key", "test-key"), \
             patch.object(cartesia_voices.httpx, "get", fake_get):
            result = cartesia_voices.list_voices(search="deep")

        self.assertEqual([v["voice_id"] for v in result["voices"]], ["v-ben"])

    def test_bare_list_client_side_pagination_and_cursor_round_trip(self) -> None:
        def fake_get(url, headers=None, params=None, timeout=None):
            return _FakeHttpxResponse(self._bare_list_voices())

        with patch.object(cartesia_voices.settings, "cartesia_api_key", "test-key"), \
             patch.object(cartesia_voices.httpx, "get", fake_get):
            page1 = cartesia_voices.list_voices(page_size=3)
            self.assertEqual(len(page1["voices"]), 3)
            self.assertTrue(page1["has_more"])
            self.assertEqual(page1["next_cursor"], "3")

            page2 = cartesia_voices.list_voices(page_size=3, cursor=page1["next_cursor"])
            self.assertEqual(len(page2["voices"]), 1)
            self.assertFalse(page2["has_more"])
            self.assertIsNone(page2["next_cursor"])

        # No overlap between the two pages.
        ids_page1 = {v["voice_id"] for v in page1["voices"]}
        ids_page2 = {v["voice_id"] for v in page2["voices"]}
        self.assertEqual(ids_page1 & ids_page2, set())
        self.assertEqual(ids_page1 | ids_page2, {v["id"] for v in self._bare_list_voices()})

    def test_bare_list_malformed_cursor_falls_back_to_first_page(self) -> None:
        def fake_get(url, headers=None, params=None, timeout=None):
            return _FakeHttpxResponse(self._bare_list_voices())

        with patch.object(cartesia_voices.settings, "cartesia_api_key", "test-key"), \
             patch.object(cartesia_voices.httpx, "get", fake_get):
            result = cartesia_voices.list_voices(cursor="not-a-number")

        self.assertEqual(len(result["voices"]), 4)


class _FakeVoice:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeSharedVoicesResponse:
    def __init__(self, voices, has_more) -> None:
        self.voices = voices
        self.has_more = has_more


class _FakeVoicesClient:
    def __init__(self, response: _FakeSharedVoicesResponse, capture: dict) -> None:
        self._response = response
        self._capture = capture

    def get_shared(self, **kwargs):
        self._capture.update(kwargs)
        return self._response


class ElevenLabsVoiceBrowsingTest(unittest.TestCase):
    def test_missing_api_key_fails_open_without_a_network_call(self) -> None:
        def raise_missing_key():
            raise RuntimeError("ELEVENLABS_API_KEY is not set")

        with patch.object(elevenlabs_voices.elevenlabs_client, "get_client", raise_missing_key):
            result = elevenlabs_voices.list_shared_voices(search="ann")

        self.assertEqual(result, {"voices": [], "has_more": False, "next_cursor": None})

    def test_successful_response_is_normalized_and_params_forwarded(self) -> None:
        fake_voice = _FakeVoice(
            voice_id="el-voice-1", name="Rachel", description=None,
            descriptive="calm", gender="female", language="en",
            accent="american", age="young", preview_url="https://cdn.example/rachel.mp3",
        )
        capture: dict = {}
        fake_client = types.SimpleNamespace(
            voices=_FakeVoicesClient(_FakeSharedVoicesResponse([fake_voice], has_more=True), capture)
        )

        with patch.object(elevenlabs_voices.elevenlabs_client, "get_client", lambda: fake_client):
            result = elevenlabs_voices.list_shared_voices(
                search="ann", gender="female", age="young", accent="american",
                language="en", use_case="narration", page_size=15, cursor="2",
            )

        self.assertEqual(capture["page_size"], 15)
        self.assertEqual(capture["page"], 2)
        self.assertEqual(capture["search"], "ann")
        self.assertEqual(capture["gender"], "female")
        self.assertEqual(capture["age"], "young")
        self.assertEqual(capture["accent"], "american")
        self.assertEqual(capture["language"], "en")
        self.assertEqual(capture["use_cases"], ["narration"])

        self.assertEqual(result["has_more"], True)
        self.assertEqual(result["next_cursor"], "3")
        voice = result["voices"][0]
        self.assertEqual(voice["voice_id"], "el-voice-1")
        self.assertEqual(voice["name"], "Rachel")
        # None description falls back to descriptive
        self.assertEqual(voice["description"], "calm")
        self.assertEqual(voice["gender"], "female")
        self.assertEqual(voice["accent"], "american")
        self.assertEqual(voice["age"], "young")
        self.assertEqual(voice["provider"], "elevenlabs")

    def test_no_more_pages_yields_null_next_cursor(self) -> None:
        fake_client = types.SimpleNamespace(
            voices=_FakeVoicesClient(_FakeSharedVoicesResponse([], has_more=False), {})
        )
        with patch.object(elevenlabs_voices.elevenlabs_client, "get_client", lambda: fake_client):
            result = elevenlabs_voices.list_shared_voices()

        self.assertEqual(result["has_more"], False)
        self.assertIsNone(result["next_cursor"])

    def test_malformed_cursor_falls_back_to_first_page(self) -> None:
        capture: dict = {}
        fake_client = types.SimpleNamespace(
            voices=_FakeVoicesClient(_FakeSharedVoicesResponse([], has_more=False), capture)
        )
        with patch.object(elevenlabs_voices.elevenlabs_client, "get_client", lambda: fake_client):
            elevenlabs_voices.list_shared_voices(cursor="not-a-number")

        self.assertEqual(capture["page"], 0)

    def test_request_failure_fails_open(self) -> None:
        class RaisingVoicesClient:
            def get_shared(self, **kwargs):
                raise RuntimeError("simulated API failure")

        fake_client = types.SimpleNamespace(voices=RaisingVoicesClient())
        with patch.object(elevenlabs_voices.elevenlabs_client, "get_client", lambda: fake_client):
            result = elevenlabs_voices.list_shared_voices()

        self.assertEqual(result, {"voices": [], "has_more": False, "next_cursor": None})


if __name__ == "__main__":
    unittest.main()
