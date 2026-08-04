"""Runtime proof for the Agent 6 roadmap's Phase E
(code_report/agent6_metadata_roadmap.md) — status machine, scheduler wiring,
auto-approve, and the manual approve/edit API surface.

Nothing here calls a paid Claude/fal.ai boundary directly — the only thing
stubbed at that layer is ``metadata_orchestrator.run_metadata_generation_for_
content()`` itself (Phase D's own function, already fully covered by
``tests/test_agent6_metadata_generation.py``), the same "stub the layer
directly below the one under test" convention
``tests/test_solo_short_pipeline.py``'s ``TestSoloShortSchedulerHandoff``
already established. The PATCH-endpoint compositing test uses a real
(free, local, no-network) Pillow call for one specific assertion, mirroring
that same test file's own precedent for the identical kind of proof.

No live external API calls anywhere in this file (CLAUDE.md §19.1).
"""

from __future__ import annotations

import operator as _operator_module
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image
from sqlalchemy.sql.elements import False_, True_
from sqlalchemy.sql.operators import is_, isnot

from app.agents.agent6_metadata.routers import metadata as metadata_router_module
from app.agents.agent6_metadata.services import metadata_orchestrator, thumbnail
from app.config import settings
from app.models import Channel, Content, VideoMetadata
from app.schemas.video_metadata import VideoMetadataUpdate


# ── Real-filtering fake DB — same convention as
# tests/test_agent6_metadata_generation.py (a no-op filter would silently
# defeat the NEEDS_REVIEW-exclusion proof below), generalized to dispatch on
# the real SQLAlchemy comparison operator (eq/lt/... via Python's own
# `operator` module, `.is_()`/`.is_not()` special-cased since those compare
# against a True_/False_ singleton, not a plain literal) so it also supports
# check_metadata_auto_approve()'s `<` cutoff comparison, not just equality.

def _extract_value(node):
    if isinstance(node, True_):
        return True
    if isinstance(node, False_):
        return False
    return node.value


class _FakeQuery:
    def __init__(self, rows: list) -> None:
        self.rows = list(rows)

    def filter(self, *conditions) -> "_FakeQuery":
        rows = self.rows
        for cond in conditions:
            key = cond.left.key
            value = _extract_value(cond.right)
            op = cond.operator
            if op is is_:
                op = _operator_module.eq
            elif op is isnot:
                op = _operator_module.ne
            rows = [r for r in rows if op(getattr(r, key, None), value)]
        return _FakeQuery(rows)

    def order_by(self, column) -> "_FakeQuery":
        return _FakeQuery(sorted(self.rows, key=lambda r: getattr(r, column.key)))

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self) -> list:
        return list(self.rows)


class _FakeDb:
    def __init__(self) -> None:
        self.tables: dict = {}

    def get(self, model, key):
        for row in self.tables.get(model, []):
            if getattr(row, "id", None) == key:
                return row
        return None

    def query(self, model) -> _FakeQuery:
        return _FakeQuery(self.tables.get(model, []))

    def add(self, row) -> None:
        self.tables.setdefault(type(row), []).append(row)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def _write_real_jpeg(path: Path, *, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color=(60, 60, 60)).save(path, format="JPEG", quality=90)


def _bare_content(content_id: uuid.UUID, channel_id: uuid.UUID, status: str, **kw) -> Content:
    return Content(
        id=content_id, channel_id=channel_id, status=status,
        source_language="en", title="T", source_url="https://x", **kw,
    )


# ── _STALE_RECOVERY_MINUTES sanity check ────────────────────────────────────

class StaleRecoveryThresholdTest(unittest.TestCase):
    def test_generating_metadata_entry_exists_and_is_generous(self) -> None:
        from app.scheduler.tasks import _STALE_RECOVERY_MINUTES

        self.assertIn("GENERATING_METADATA", _STALE_RECOVERY_MINUTES)
        # Must clear the documented worst-case-legitimate-runtime estimate
        # (~40 min: up to 24 sequential platform_metadata_generation calls,
        # 6 languages x 4 platforms, plus one thumbnail generation — not the
        # "two calls" an earlier draft of this threshold's comment assumed)
        # with real margin, matching every other stage's own "must exceed
        # worst-case legitimate runtime" rule rather than a guess.
        self.assertGreaterEqual(_STALE_RECOVERY_MINUTES["GENERATING_METADATA"], 60)


# ── pickup_rendered_content() ───────────────────────────────────────────────

class PickupRenderedContentTest(unittest.TestCase):
    def _run(self, db: _FakeDb):
        from app.scheduler import tasks
        from app import database as database_module

        with (
            patch.object(database_module, "_get_session_factory", return_value=(lambda: db)),
            patch.object(tasks, "_stale_in_progress", return_value=[]),
            patch.object(tasks.run_agent6_metadata_for_content, "delay") as d,
        ):
            dispatched = tasks.pickup_rendered_content()
        return dispatched, d

    def test_rendered_content_dispatched(self) -> None:
        db = _FakeDb()
        content_id = uuid.uuid4()
        db.add(_bare_content(content_id, uuid.uuid4(), "RENDERED"))

        dispatched, d = self._run(db)
        self.assertEqual(dispatched, 1)
        d.assert_called_once_with(str(content_id))

    def test_needs_review_content_excluded(self) -> None:
        """Roadmap Check 2, Finding 2.3: a NEEDS_REVIEW content item must
        never be picked up, even though it can carry real VideoRender rows
        from whichever language did pass render verification."""
        db = _FakeDb()
        db.add(_bare_content(uuid.uuid4(), uuid.uuid4(), "NEEDS_REVIEW"))

        dispatched, d = self._run(db)
        self.assertEqual(dispatched, 0)
        d.assert_not_called()

    def test_already_processed_content_not_redispatched(self) -> None:
        db = _FakeDb()
        db.add(_bare_content(uuid.uuid4(), uuid.uuid4(), "METADATA_PENDING_APPROVAL"))

        dispatched, d = self._run(db)
        self.assertEqual(dispatched, 0)
        d.assert_not_called()


# ── run_agent6_metadata_for_content() ───────────────────────────────────────

class RunAgentSixMetadataForContentTest(unittest.TestCase):
    def test_success_moves_to_pending_approval_and_stamps_timestamp(self) -> None:
        from app.scheduler import tasks
        from app import database as database_module

        db = _FakeDb()
        content_id = uuid.uuid4()
        content = _bare_content(content_id, uuid.uuid4(), "RENDERED")
        db.add(content)

        before = datetime.now(timezone.utc)
        with (
            patch.object(database_module, "_get_session_factory", return_value=(lambda: db)),
            patch.object(
                metadata_orchestrator, "run_metadata_generation_for_content", return_value=True,
            ),
        ):
            tasks.run_agent6_metadata_for_content(str(content_id))

        self.assertEqual(content.status, "METADATA_PENDING_APPROVAL")
        self.assertIsNotNone(content.metadata_generated_at)
        self.assertGreaterEqual(content.metadata_generated_at, before)

    def test_zero_rows_produced_is_total_failure(self) -> None:
        """Explicit definition (operator review): FAILED means zero
        VideoMetadata rows produced for the whole content item — mirrors
        run_video_generation()'s `successful > 0` convention exactly, not a
        new threshold invented for Agent 6."""
        from app.scheduler import tasks
        from app import database as database_module

        db = _FakeDb()
        content_id = uuid.uuid4()
        content = _bare_content(content_id, uuid.uuid4(), "RENDERED")
        db.add(content)

        with (
            patch.object(database_module, "_get_session_factory", return_value=(lambda: db)),
            patch.object(
                metadata_orchestrator, "run_metadata_generation_for_content", return_value=False,
            ),
            self.assertLogs("app.scheduler.tasks", level="ERROR") as logs,
        ):
            tasks.run_agent6_metadata_for_content(str(content_id))

        self.assertEqual(content.status, "FAILED")
        self.assertIsNone(content.metadata_generated_at)
        self.assertTrue(any("METADATA_GENERATION_TOTAL_FAILURE" in line for line in logs.output))

    def test_status_is_generating_metadata_while_orchestrator_runs(self) -> None:
        from app.scheduler import tasks
        from app import database as database_module

        db = _FakeDb()
        content_id = uuid.uuid4()
        content = _bare_content(content_id, uuid.uuid4(), "RENDERED")
        db.add(content)

        seen = {}

        def _capture(cid, db_arg):
            seen["status"] = content.status
            return True

        with (
            patch.object(database_module, "_get_session_factory", return_value=(lambda: db)),
            patch.object(
                metadata_orchestrator, "run_metadata_generation_for_content", side_effect=_capture,
            ),
        ):
            tasks.run_agent6_metadata_for_content(str(content_id))

        self.assertEqual(seen["status"], "GENERATING_METADATA")

    def test_wrong_status_content_is_skipped(self) -> None:
        from app.scheduler import tasks
        from app import database as database_module

        db = _FakeDb()
        content_id = uuid.uuid4()
        db.add(_bare_content(content_id, uuid.uuid4(), "METADATA_APPROVED"))

        with (
            patch.object(database_module, "_get_session_factory", return_value=(lambda: db)),
            patch.object(
                metadata_orchestrator, "run_metadata_generation_for_content",
            ) as stub,
        ):
            tasks.run_agent6_metadata_for_content(str(content_id))

        stub.assert_not_called()


# ── check_metadata_auto_approve() ───────────────────────────────────────────

class CheckMetadataAutoApproveTest(unittest.TestCase):
    def test_fires_after_threshold_elapsed(self) -> None:
        """Time-manipulation strategy (CLAUDE.md §19.4) — inject an
        already-past timestamp directly, exactly the same technique
        check_validation_timeouts()'s own tests already use for
        ContentValidation.timeout_at. No real waiting anywhere."""
        from app.scheduler import tasks
        from app import database as database_module

        db = _FakeDb()
        content_id = uuid.uuid4()
        past = datetime.now(timezone.utc) - timedelta(
            seconds=settings.metadata_auto_approve_seconds + 1,
        )
        content = _bare_content(
            content_id, uuid.uuid4(), "METADATA_PENDING_APPROVAL", metadata_generated_at=past,
        )
        db.add(content)

        with patch.object(database_module, "_get_session_factory", return_value=(lambda: db)):
            count = tasks.check_metadata_auto_approve()

        self.assertEqual(count, 1)
        self.assertEqual(content.status, "METADATA_APPROVED")

    def test_does_not_fire_before_threshold_elapsed(self) -> None:
        from app.scheduler import tasks
        from app import database as database_module

        db = _FakeDb()
        content_id = uuid.uuid4()
        almost_now = datetime.now(timezone.utc) - timedelta(
            seconds=settings.metadata_auto_approve_seconds - 5,
        )
        content = _bare_content(
            content_id, uuid.uuid4(), "METADATA_PENDING_APPROVAL",
            metadata_generated_at=almost_now,
        )
        db.add(content)

        with patch.object(database_module, "_get_session_factory", return_value=(lambda: db)):
            count = tasks.check_metadata_auto_approve()

        self.assertEqual(count, 0)
        self.assertEqual(content.status, "METADATA_PENDING_APPROVAL")

    def test_wrong_status_content_never_touched(self) -> None:
        from app.scheduler import tasks
        from app import database as database_module

        db = _FakeDb()
        content_id = uuid.uuid4()
        past = datetime.now(timezone.utc) - timedelta(days=1)
        content = _bare_content(content_id, uuid.uuid4(), "RENDERED", metadata_generated_at=past)
        db.add(content)

        with patch.object(database_module, "_get_session_factory", return_value=(lambda: db)):
            count = tasks.check_metadata_auto_approve()

        self.assertEqual(count, 0)
        self.assertEqual(content.status, "RENDERED")


# ── Full scheduler handoff (mirrors TestSoloShortSchedulerHandoff exactly) ──

class FullSchedulerHandoffTest(unittest.TestCase):
    """Each stage's `.delay()` call is stubbed and asserted, then the next
    stage's precondition is set directly — the same convention
    TestSoloShortSchedulerHandoff (tests/test_solo_short_pipeline.py)
    established, since each task's own internal status-transition behavior
    already has dedicated unit coverage above."""

    def test_rendered_flows_through_every_stage_to_metadata_approved(self) -> None:
        from app.scheduler import tasks
        from app import database as database_module

        content_id = uuid.uuid4()
        content = _bare_content(content_id, uuid.uuid4(), "RENDERED")
        db = _FakeDb()
        db.add(content)

        with (
            patch.object(database_module, "_get_session_factory", return_value=(lambda: db)),
            patch.object(tasks, "_stale_in_progress", return_value=[]),
        ):
            # Stage 1: RENDERED -> Agent 6 metadata generation.
            with patch.object(tasks.run_agent6_metadata_for_content, "delay") as d1:
                dispatched = tasks.pickup_rendered_content()
            self.assertEqual(dispatched, 1)
            d1.assert_called_once_with(str(content_id))

            # Stage 2: metadata generation completed -> METADATA_PENDING_APPROVAL,
            # with the auto-approve window already elapsed (no real wait).
            content.status = "METADATA_PENDING_APPROVAL"
            content.metadata_generated_at = datetime.now(timezone.utc) - timedelta(
                seconds=settings.metadata_auto_approve_seconds + 1,
            )
            count = tasks.check_metadata_auto_approve()

        self.assertEqual(count, 1)
        self.assertEqual(content.status, "METADATA_APPROVED")


# ── Manual approve/edit API surface (Check 6) ───────────────────────────────

class ApproveEndpointTest(unittest.TestCase):
    def test_approves_pending_content(self) -> None:
        db = _FakeDb()
        user_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db.add(Channel(id=channel_id, user_id=user_id, niche="n", tone="t"))
        db.add(_bare_content(content_id, channel_id, "METADATA_PENDING_APPROVAL"))

        result = metadata_router_module.approve_metadata(content_id, user_id=user_id, db=db)
        self.assertEqual(result, {"status": "approved", "content_id": str(content_id)})

    def test_rejects_wrong_status(self) -> None:
        db = _FakeDb()
        user_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db.add(Channel(id=channel_id, user_id=user_id, niche="n", tone="t"))
        db.add(_bare_content(content_id, channel_id, "RENDERED"))

        with self.assertRaises(HTTPException) as ctx:
            metadata_router_module.approve_metadata(content_id, user_id=user_id, db=db)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_unowned_content(self) -> None:
        db = _FakeDb()
        owner_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db.add(Channel(id=channel_id, user_id=owner_id, niche="n", tone="t"))
        db.add(_bare_content(content_id, channel_id, "METADATA_PENDING_APPROVAL"))

        with self.assertRaises(HTTPException) as ctx:
            metadata_router_module.approve_metadata(content_id, user_id=other_user_id, db=db)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_rejects_missing_content(self) -> None:
        db = _FakeDb()
        with self.assertRaises(HTTPException) as ctx:
            metadata_router_module.approve_metadata(uuid.uuid4(), user_id=uuid.uuid4(), db=db)
        self.assertEqual(ctx.exception.status_code, 404)


class PatchVideoMetadataEndpointTest(unittest.TestCase):
    def _setup(self, db: _FakeDb, *, platform: str = "tiktok"):
        user_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db.add(Channel(id=channel_id, user_id=user_id, niche="n", tone="t"))
        db.add(_bare_content(content_id, channel_id, "METADATA_PENDING_APPROVAL"))
        row_id = uuid.uuid4()
        row = VideoMetadata(
            id=row_id, content_id=content_id, language="en", platform=platform,
            title="Original Title", description="Original description", hashtags=["#a"],
        )
        db.add(row)
        return user_id, content_id, row_id, row

    def test_oversized_title_is_truncated_not_persisted_raw(self) -> None:
        db = _FakeDb()
        user_id, content_id, row_id, row = self._setup(db, platform="tiktok")
        oversized_title = "word " * 60  # way over tiktok's 150-char cap

        result = metadata_router_module.update_video_metadata(
            row_id, VideoMetadataUpdate(title=oversized_title), user_id=user_id, db=db,
        )
        self.assertLessEqual(len(result.title), 150)
        self.assertNotEqual(result.title, oversized_title)
        self.assertEqual(row.description, "Original description")  # untouched field preserved

    def test_thumbnail_text_rejected_on_non_youtube_row(self) -> None:
        db = _FakeDb()
        user_id, content_id, row_id, row = self._setup(db, platform="tiktok")

        with self.assertRaises(HTTPException) as ctx:
            metadata_router_module.update_video_metadata(
                row_id, VideoMetadataUpdate(thumbnail_text="New Text"), user_id=user_id, db=db,
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_oversized_thumbnail_text_enforced_before_persisting(self) -> None:
        db = _FakeDb()
        user_id, content_id, row_id, row = self._setup(db, platform="youtube")
        raw = "way way too many words for one thumbnail overlay to ever show"

        with (
            patch.object(thumbnail, "composite_thumbnail_overlay", return_value=None),
        ):
            result = metadata_router_module.update_video_metadata(
                row_id, VideoMetadataUpdate(thumbnail_text=raw), user_id=user_id, db=db,
            )
        self.assertNotEqual(result.thumbnail_text, raw)
        self.assertLessEqual(len(result.thumbnail_text.split()), 5)

    def test_thumbnail_text_edit_triggers_exactly_one_recomposite_and_no_flux_call(self) -> None:
        db = _FakeDb()
        user_id, content_id, row_id, row = self._setup(db, platform="youtube")
        row.thumbnail_file_path = f"thumbnails/{content_id}/en.jpg"

        with tempfile.TemporaryDirectory() as tmp:
            base_relative = f"thumbnails/{content_id}/base.jpg"
            _write_real_jpeg(Path(tmp) / base_relative, width=1280, height=720)
            pre_edit_path = Path(tmp) / f"thumbnails/{content_id}/en.jpg"
            _write_real_jpeg(pre_edit_path, width=1280, height=720)
            pre_edit_bytes = pre_edit_path.read_bytes()

            with (
                patch.object(thumbnail.settings, "media_path", tmp),
                patch.object(
                    thumbnail.flux_client, "generate_beat_image",
                    side_effect=AssertionError("must not call Flux for a PATCH re-composite"),
                ),
                patch.object(
                    thumbnail, "composite_thumbnail_overlay",
                    wraps=thumbnail.composite_thumbnail_overlay,
                ) as composite_spy,
            ):
                result = metadata_router_module.update_video_metadata(
                    row_id, VideoMetadataUpdate(thumbnail_text="Brand New Text"),
                    user_id=user_id, db=db,
                )

            self.assertEqual(composite_spy.call_count, 1)
            self.assertEqual(result.thumbnail_file_path, f"thumbnails/{content_id}/en.jpg")
            final_bytes = (Path(tmp) / result.thumbnail_file_path).read_bytes()
            # Real Pillow render (not stubbed) — the recomposited file must
            # genuinely differ from the pre-edit one, not just report success.
            self.assertNotEqual(final_bytes, pre_edit_bytes)

    def test_rejects_unowned_row(self) -> None:
        db = _FakeDb()
        _owner_user_id, content_id, row_id, row = self._setup(db, platform="tiktok")
        other_user_id = uuid.uuid4()

        with self.assertRaises(HTTPException) as ctx:
            metadata_router_module.update_video_metadata(
                row_id, VideoMetadataUpdate(title="x"), user_id=other_user_id, db=db,
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_rejects_missing_row(self) -> None:
        db = _FakeDb()
        with self.assertRaises(HTTPException) as ctx:
            metadata_router_module.update_video_metadata(
                uuid.uuid4(), VideoMetadataUpdate(title="x"), user_id=uuid.uuid4(), db=db,
            )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
