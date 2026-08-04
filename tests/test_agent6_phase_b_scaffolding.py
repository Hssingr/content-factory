"""Runtime proof for the Agent 6 roadmap's Phase B
(code_report/agent6_metadata_roadmap.md) — scaffolding only: migration,
settings, model routing, and the agent6_metadata package skeleton.

Migration correctness is proven two ways, matching this repo's existing
convention (e.g. tests/test_script_content_prompt_and_pov_hygiene.py's
NarrationPovMigrationTest for migration 014): a mocked-``op`` structural
test here (works even without a live DB or the ``mako`` package alembic's
CLI needs), plus a real upgrade/downgrade/upgrade cycle already run by hand
against the live dev database as part of this phase's verification gate
(see the phase's own report — not repeated here as an automated test since
CLAUDE.md §19.1 prefers this suite stay live-service-independent).

No live external API calls anywhere in this file.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_migration_017():
    path = _REPO_ROOT / "alembic/versions/017_add_agent6_metadata_fields.py"
    spec = importlib.util.spec_from_file_location("migration_017", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Migration017StructureTest(unittest.TestCase):
    def test_revision_chain(self) -> None:
        module = _load_migration_017()
        self.assertEqual(module.revision, "017")
        self.assertEqual(module.down_revision, "016")
        self.assertIsNone(module.branch_labels)
        self.assertIsNone(module.depends_on)

    def test_upgrade_adds_exactly_three_columns(self) -> None:
        module = _load_migration_017()
        with patch.object(module.op, "add_column") as add_column:
            module.upgrade()

        self.assertEqual(add_column.call_count, 3)
        calls_by_table: dict[str, list] = {}
        for call in add_column.call_args_list:
            table_name, column = call.args
            calls_by_table.setdefault(table_name, []).append(column)

        self.assertEqual(set(calls_by_table), {"content", "video_metadata"})
        self.assertEqual(len(calls_by_table["content"]), 1)
        self.assertEqual(len(calls_by_table["video_metadata"]), 2)

        content_col = calls_by_table["content"][0]
        self.assertEqual(content_col.name, "metadata_generated_at")
        self.assertTrue(content_col.nullable)

        vm_cols = {c.name: c for c in calls_by_table["video_metadata"]}
        self.assertEqual(set(vm_cols), {"generated_at", "thumbnail_text"})
        self.assertFalse(vm_cols["generated_at"].nullable)
        self.assertIsNotNone(vm_cols["generated_at"].server_default)
        self.assertTrue(vm_cols["thumbnail_text"].nullable)
        self.assertEqual(vm_cols["thumbnail_text"].type.length, 64)

    def test_downgrade_drops_the_same_three_columns_in_reverse(self) -> None:
        module = _load_migration_017()
        with patch.object(module.op, "drop_column") as drop_column:
            module.downgrade()

        calls = [call.args for call in drop_column.call_args_list]
        self.assertEqual(
            calls,
            [
                ("video_metadata", "thumbnail_text"),
                ("video_metadata", "generated_at"),
                ("content", "metadata_generated_at"),
            ],
        )


class ModelFieldsTest(unittest.TestCase):
    """The ORM must already know about migration 017's columns (this
    codebase's convention: migration and model land in the same change
    set) — checked structurally, no DB connection needed."""

    def test_content_has_metadata_generated_at(self) -> None:
        from app.models import Content

        self.assertIn("metadata_generated_at", Content.__table__.columns)
        col = Content.__table__.columns["metadata_generated_at"]
        self.assertTrue(col.nullable)

    def test_video_metadata_has_new_columns(self) -> None:
        from app.models import VideoMetadata

        cols = VideoMetadata.__table__.columns
        self.assertIn("thumbnail_text", cols)
        self.assertIn("generated_at", cols)
        self.assertTrue(cols["thumbnail_text"].nullable)
        self.assertEqual(cols["thumbnail_text"].type.length, 64)
        self.assertFalse(cols["generated_at"].nullable)


class SettingsTest(unittest.TestCase):
    def test_metadata_auto_approve_seconds_default(self) -> None:
        from app.config import Settings

        # Fresh instance (not the possibly-.env-overridden module singleton)
        # so this test is honest about the *default*, not whatever a local
        # .env happens to set.
        defaults = Settings.model_fields["metadata_auto_approve_seconds"]
        self.assertEqual(defaults.default, 3600)


class ModelRoutingTest(unittest.TestCase):
    def test_thumbnail_prompt_generation_resolves(self) -> None:
        from app.services.model_routing import resolve_model, MODEL_ROUTING, PRIMARY_MODEL

        self.assertEqual(MODEL_ROUTING["thumbnail_prompt_generation"], PRIMARY_MODEL)
        # Does not raise — the whole point of this smoke check.
        resolve_model("thumbnail_prompt_generation")

    def test_platform_metadata_generation_resolves(self) -> None:
        from app.services.model_routing import resolve_model, MODEL_ROUTING, PRIMARY_MODEL

        self.assertEqual(MODEL_ROUTING["platform_metadata_generation"], PRIMARY_MODEL)
        resolve_model("platform_metadata_generation")


class AgentSixPackageSkeletonTest(unittest.TestCase):
    """Originally proved every service function was a real, callable stub
    (Phase B). Phases C and D have since replaced every one of them with a
    real implementation (thumbnail.py + system_prompt.py's
    thumbnail_prompt_generation in Phase C; metadata_orchestrator.py,
    platform_limits.py, and system_prompt.py's platform_metadata_generation
    in Phase D) — real coverage for all of them lives in
    tests/test_agent6_thumbnail_base_image.py and
    tests/test_agent6_metadata_generation.py. What remains useful here is
    the plain "does the whole package still import cleanly" check below —
    kept because a circular-import or module-load crash anywhere in the
    package would otherwise go undetected by any single module's own tests."""

    def test_package_and_submodules_import_cleanly(self) -> None:
        import app.agents.agent6_metadata  # noqa: F401
        import app.agents.agent6_metadata.system_prompt  # noqa: F401
        import app.agents.agent6_metadata.services  # noqa: F401
        import app.agents.agent6_metadata.services.metadata_orchestrator  # noqa: F401
        import app.agents.agent6_metadata.services.thumbnail  # noqa: F401
        import app.agents.agent6_metadata.services.platform_limits  # noqa: F401

    # test_thumbnail_stub_raises intentionally removed: Phase C
    # (code_report/agent6_metadata_roadmap.md) replaced thumbnail.py's stub
    # with the real base-image implementation, so
    # generate_thumbnail_base_image() no longer raises NotImplementedError —
    # it now requires real (content_id, db) arguments and does real work.
    # Real coverage lives in tests/test_agent6_thumbnail_base_image.py.
    #
    # test_metadata_orchestrator_stub_raises and test_platform_limits_stubs_raise
    # intentionally removed the same way: Phase D replaced both
    # metadata_orchestrator.py's and platform_limits.py's stubs with real
    # implementations. Real coverage lives in
    # tests/test_agent6_metadata_generation.py. Only system_prompt.py's
    # thumbnail_prompt_generation/platform_metadata_generation Claude-call
    # functions and thumbnail.py's stage 1-5 pipeline remain covered by this
    # file's earlier tests (settings/model-routing/font-asset checks above),
    # none of which were ever stubs to begin with.


class FontAssetTest(unittest.TestCase):
    """The font asset is real, bundled, and genuinely loadable — not a
    placeholder or a mislabeled substitute (roadmap Finding 3.5 / the
    resolved Open Decision naming Archivo Black as the default)."""

    def test_font_file_exists_and_loads(self) -> None:
        from PIL import ImageFont

        font_path = (
            _REPO_ROOT
            / "app/agents/agent6_metadata/assets/fonts/ArchivoBlack-Regular.ttf"
        )
        self.assertTrue(font_path.is_file())
        font = ImageFont.truetype(str(font_path), 48)
        family, style = font.getname()
        self.assertEqual(family, "Archivo Black")

    def test_license_file_present(self) -> None:
        ofl_path = _REPO_ROOT / "app/agents/agent6_metadata/assets/fonts/OFL.txt"
        self.assertTrue(ofl_path.is_file())
        self.assertIn("SIL OPEN FONT LICENSE", ofl_path.read_text().upper())


if __name__ == "__main__":
    unittest.main()
