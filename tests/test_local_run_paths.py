import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.services.local_run_paths import RUN_SUBDIRS, ensure_run_dirs, get_run_root


class TestLocalRunPaths(unittest.TestCase):
    def test_directory_creation_and_manifest(self):
        with TemporaryDirectory() as tmp:
            with patch("app.services.local_run_paths.settings.media_path", tmp):
                dirs = ensure_run_dirs("content-123")
                run_root = get_run_root("content-123")

                self.assertEqual(run_root, Path(tmp).resolve() / "runs" / "content-123")
                self.assertEqual(set(dirs), set(RUN_SUBDIRS))
                for path in dirs.values():
                    self.assertTrue(path.is_dir())

                manifest_path = run_root / "run_manifest.json"
                self.assertTrue(manifest_path.is_file())
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["content_id"], "content-123")
                self.assertEqual(manifest["run_root"], str(run_root))
                self.assertEqual(set(manifest["directories"]), set(RUN_SUBDIRS))

    def test_idempotency_preserves_existing_files_and_created_at(self):
        with TemporaryDirectory() as tmp:
            with patch("app.services.local_run_paths.settings.media_path", tmp):
                dirs = ensure_run_dirs("content-123")
                sentinel = dirs["visuals"] / "keep.txt"
                sentinel.write_text("keep me", encoding="utf-8")

                manifest_path = get_run_root("content-123") / "run_manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["created_at"] = "2026-01-01T00:00:00+00:00"
                manifest["directories"] = {"script": manifest["directories"]["script"]}
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                ensure_run_dirs("content-123")
                updated = json.loads(manifest_path.read_text(encoding="utf-8"))

                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")
                self.assertEqual(updated["created_at"], "2026-01-01T00:00:00+00:00")
                self.assertEqual(set(updated["directories"]), set(RUN_SUBDIRS))

    def test_invalid_content_id(self):
        for content_id in (None, "", "   ", "../bad", "bad/path"):
            with self.subTest(content_id=content_id):
                with self.assertRaises(ValueError):
                    ensure_run_dirs(content_id)

    def test_invalid_media_root(self):
        with patch("app.services.local_run_paths.settings.media_path", ""):
            with self.assertRaises(ValueError):
                ensure_run_dirs("content-123")


if __name__ == "__main__":
    unittest.main()
