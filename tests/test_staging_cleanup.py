"""Registered local staging survives active work and is reclaimed after interruption."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yandex_speech.artifacts import cleanup_staging, staging_directory
from yandex_speech.errors import SpeechError


class StagingCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.unrelated = self.base / "unrelated.txt"
        self.unrelated.write_text("keep this file", encoding="utf-8")
        self.foreign = self.base / ".yandex-speech-partial-unregistered"
        self.foreign.mkdir()
        (self.foreign / "keep.txt").write_text("unregistered data", encoding="utf-8")

    def interrupted_stage(self):
        program = (
            "import os,sys; from pathlib import Path; "
            "from yandex_speech.artifacts import staging_directory; "
            "scope=staging_directory(Path(sys.argv[1])); "
            "stage=Path(scope.__enter__()); "
            "(stage/'part.bin').write_bytes(b'synthetic accepted part'); "
            "print(stage.name,flush=True); os._exit(19)"
        )
        result = subprocess.run(
            [sys.executable, "-c", program, str(self.base)],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 19, result.stderr)
        stage = self.base / result.stdout.strip()
        self.assertEqual(stage.parent, self.base)
        self.assertTrue((stage / "part.bin").is_file())
        return stage

    def assert_unrelated_survives(self):
        self.assertEqual(self.unrelated.read_text(encoding="utf-8"), "keep this file")
        self.assertEqual(
            (self.foreign / "keep.txt").read_text(encoding="utf-8"), "unregistered data")

    def test_process_interruption_reclaims_only_registered_inactive_stage(self):
        stage = self.interrupted_stage()
        cleanup_staging(self.base)
        self.assertFalse(stage.exists(), "interrupted registered stage was retained")
        self.assert_unrelated_survives()

    def test_active_stage_is_protected_until_its_owner_finishes(self):
        with staging_directory(self.base) as directory:
            stage = Path(directory)
            part = stage / "part.bin"
            part.write_bytes(b"active synthetic part")
            cleanup_staging(self.base)
            self.assertTrue(stage.is_dir(), "active staging lease was ignored")
            self.assertEqual(part.read_bytes(), b"active synthetic part")
            self.assert_unrelated_survives()
        self.assertFalse(stage.exists(), "finished operation retained its staging")
        self.assert_unrelated_survives()

    def test_failed_disposal_can_retry_without_removing_unrelated_data(self):
        stage = self.interrupted_stage()
        with patch("shutil.rmtree", side_effect=PermissionError(
                "synthetic disposal permission failure")) as remove:
            try:
                cleanup_staging(self.base)
            except (OSError, SpeechError):
                pass
            self.assertTrue(remove.called, "disposal failure fixture was not exercised")
        self.assertTrue(stage.is_dir(), "failed disposal lost its registered target")
        self.assertTrue((stage / "part.bin").is_file())
        self.assert_unrelated_survives()
        cleanup_staging(self.base)
        self.assertFalse(stage.exists(), "disposal did not retry the registered target")
        self.assert_unrelated_survives()

    def test_explicit_failure_retention_survives_later_reconciliation(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic terminal failure"):
            with staging_directory(self.base, retain_on_error=True) as directory:
                stage = Path(directory)
                (stage / "part.bin").write_bytes(b"supported retained part")
                raise RuntimeError("synthetic terminal failure")
        cleanup_staging(self.base)
        self.assertEqual((stage / "part.bin").read_bytes(), b"supported retained part")
        self.assert_unrelated_survives()
