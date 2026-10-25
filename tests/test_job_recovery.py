import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yandex_speech.errors import SpeechError
from yandex_speech.jobs import JobHandle


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job = JobHandle.create(self.root, {"duration_ms": 1000},
            parameters={"language": "ru-RU", "diarize": False,
                        "timestamps": True, "literature_text": True})
        self.result = {"schema_version": 1, "status": "completed", "input_complete": True,
                       "completion_kind": "recorded-file", "text": "Complete.",
                       "segments": [], "job_id": self.job.job_id}

    def test_crash_after_publication_recovers_without_provider_or_overwrite(self):
        real_save = self.job.save
        def interrupted_save():
            if (self.job.manifest_path.parent / "result.json").exists():
                raise SpeechError("PREREQUISITE", "simulated receipt write interruption")
            return real_save()
        with patch.object(self.job, "save", side_effect=interrupted_save):
            with self.assertRaises(SpeechError):
                self.job.store_result(self.result)
        recovered = JobHandle.load(self.job.manifest_path)
        self.assertEqual(recovered.cached_result(), self.result)
        persisted = json.loads(self.job.manifest_path.read_text())
        self.assertEqual(persisted["status"], "completed")
        self.assertIn("result_sha256", persisted)
        self.assertNotIn("pending_result_sha256", persisted)
        self.assertEqual(persisted["parts"][0]["status"], "completed")

    def test_crash_before_result_publication_can_retry(self):
        import yandex_speech.jobs as jobs
        real_write = jobs.atomic_write
        def interrupted_write(path, data, **options):
            if Path(path).name == "result.json":
                raise SpeechError("PREREQUISITE", "simulated publication interruption")
            return real_write(path, data, **options)
        with patch("yandex_speech.jobs.atomic_write", side_effect=interrupted_write):
            with self.assertRaises(SpeechError):
                self.job.store_result(self.result)
        recovered = JobHandle.load(self.job.manifest_path)
        self.assertIsNone(recovered.cached_result())
        recovered.store_result(self.result)
        self.assertEqual(recovered.cached_result(), self.result)

    def test_unowned_existing_result_is_preserved(self):
        path = self.job.manifest_path.parent / "result.json"
        path.write_text("sentinel")
        with self.assertRaises(SpeechError):
            self.job.store_result(self.result)
        self.assertEqual(path.read_text(), "sentinel")
        self.assertNotIn("pending_result_sha256", json.loads(self.job.manifest_path.read_text()))

    def test_preloaded_handles_refresh_authoritative_state_after_lock(self):
        first = JobHandle.load(self.job.manifest_path)
        second = JobHandle.load(self.job.manifest_path)
        cancellation = second.cancel_event
        cancellation.set()
        with first.lock():
            first.store_result(self.result)
        with second.lock():
            self.assertEqual(second.cached_result(), self.result)
        self.assertIs(second.cancel_event, cancellation)
        self.assertTrue(second.cancel_event.is_set())

    def test_tampered_committed_result_fails(self):
        self.job.store_result(self.result)
        (self.job.manifest_path.parent / "result.json").write_text("{}")
        with self.assertRaises(SpeechError):
            JobHandle.load(self.job.manifest_path).cached_result()
