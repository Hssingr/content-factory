import json
import argparse
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.local_quality.common import (
    LocalQualityReport,
    LocalQualityUsageError,
    aggregate_status,
    artifact_info,
    check,
    collect_messages,
    require_external,
    write_report,
)


class TestLocalQualityScripts(unittest.TestCase):
    def test_report_writer_creates_valid_json(self):
        with TemporaryDirectory() as tmp:
            report = LocalQualityReport(
                status="PASS",
                content_id="content-123",
                agent="agent4",
                started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:01+00:00",
                duration_sec=1.0,
                checks=[{"name": "ok", "status": "PASS"}],
                warnings=[],
                errors=[],
                artifact_paths={"visual_review": "review.html"},
            )
            with patch("app.services.local_run_paths.settings.media_path", tmp):
                path = write_report("content-123", "agent4_visual_report.json", report)
                payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["agent"], "agent4")
        self.assertTrue(str(path).endswith("media/runs/content-123/review/agent4_visual_report.json") or path.name == "agent4_visual_report.json")

    def test_status_aggregation(self):
        self.assertEqual(aggregate_status(["PASS", "PASS_WITH_WARNINGS"]), "PASS_WITH_WARNINGS")
        self.assertEqual(aggregate_status(["PASS", "FAIL"]), "FAIL")
        self.assertEqual(aggregate_status([]), "NOT_RUN")

    def test_collect_messages_splits_warnings_and_errors(self):
        warnings, errors = collect_messages([
            check("soft", False, severity="WARNING"),
            check("hard", False),
            check("ok", True),
        ])
        self.assertEqual(warnings, ["soft"])
        self.assertEqual(errors, ["hard"])

    def test_external_guard_rejects_execution_without_flags(self):
        args = argparse.Namespace(inspect_only=False, allow_external=False, real_ai=False)
        with self.assertRaises(LocalQualityUsageError):
            require_external(args, "real_ai")

    def test_external_guard_allows_inspect_only(self):
        args = argparse.Namespace(inspect_only=True, allow_external=False, real_ai=False)
        require_external(args, "real_ai")

    def test_external_guard_allows_explicit_flag(self):
        args = argparse.Namespace(inspect_only=False, allow_external=False, real_ai=True)
        require_external(args, "real_ai")

    def test_artifact_info(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.txt"
            path.write_text("hello", encoding="utf-8")
            info = artifact_info(path)
        self.assertTrue(info["exists"])
        self.assertEqual(info["size_bytes"], 5)

    def test_scripts_import_and_have_help(self):
        modules = [
            "scripts.local_quality.run_agent1_setup_check",
            "scripts.local_quality.run_agent2_script_test",
            "scripts.local_quality.run_agent3_audio_test",
            "scripts.local_quality.run_agent4_visual_test",
            "scripts.local_quality.run_agent5_render_test",
            "scripts.local_quality.run_full_local_quality_test",
            "scripts.local_quality.run_quality_suite",
        ]
        for module_name in modules:
            module = __import__(module_name, fromlist=["build_parser"])
            self.assertIn("usage:", module.build_parser().format_help())


if __name__ == "__main__":
    unittest.main()
