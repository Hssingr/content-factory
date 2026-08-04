"""Runtime smoke proof for Flux failure logging.

No live API calls: the fal.ai client boundary is stubbed by patching the
ALREADY-IMPORTED ``app.services.flux_client`` module's ``fal_client``
attribute (``_call_fal()`` moved there — Agent 6 roadmap Phase A,
`code_report/agent6_metadata_roadmap.md`) — not by seeding ``sys.modules``
before import, which is order-dependent: when another test file imports
``flux_client`` first (any full-suite run), the module's globals already
hold the real ``fal_client`` and a late ``sys.modules`` swap does nothing,
letting ``client.run`` reach the real network. ``httpx.get`` is replaced
before any download can happen. The real ``_call_fal`` function is
exercised; only paid/external boundaries are stubbed.
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.agent4_visuals.services import flux_generator
from app.services import flux_client


class StubFalClientError(Exception):
    pass


class FailingSyncClient:
    def __init__(self, key: str | None = None) -> None:
        self.key = key

    def run(self, endpoint: str, arguments: dict, timeout: float) -> dict:
        raise StubFalClientError("stubbed fal outage")


_STUB_FAL_CLIENT = types.SimpleNamespace(
    SyncClient=FailingSyncClient,
    FalClientError=StubFalClientError,
)


class FluxFailureLoggingTest(unittest.TestCase):
    def test_call_fal_logs_exception_type_and_message_before_returning_none(self) -> None:
        def fail_if_downloaded(*args, **kwargs):
            raise AssertionError("httpx.get should not run after a fal client failure")

        stack = patch.object(flux_client, "fal_client", _STUB_FAL_CLIENT)
        stack.start()
        orig_httpx_get = flux_client.httpx.get
        orig_fal_key = flux_generator.settings.fal_key
        flux_client.httpx.get = fail_if_downloaded
        flux_generator.settings.fal_key = "stub-key-never-used"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                media_path = Path(tmpdir)
                cache_dir = media_path / "cache" / "content-1"
                cache_dir.mkdir(parents=True)

                with self.assertLogs(
                    "app.services.flux_client",
                    level="ERROR",
                ) as logs:
                    result = flux_client._call_fal(
                        "empty office desk, morning light",
                        cache_dir,
                        media_path,
                        model_key="schnell",
                    )
        finally:
            flux_client.httpx.get = orig_httpx_get
            flux_generator.settings.fal_key = orig_fal_key
            stack.stop()

        combined = "\n".join(logs.output)
        self.assertIsNone(result)
        self.assertIn("FAL_IMAGE_FAILED", combined)
        self.assertIn("model=schnell", combined)
        self.assertIn("endpoint=fal-ai/flux-1/schnell", combined)
        self.assertIn("exception_type=StubFalClientError", combined)
        self.assertIn("exception_message=stubbed fal outage", combined)


if __name__ == "__main__":
    unittest.main()
