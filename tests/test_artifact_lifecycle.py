"""Operation-owned S3 cleanup must follow recognition without paid replay."""
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock

from tests.test_owned_storage import FakeS3, StorageError
from tests.test_recording_storage_routes import RouteTransport
from yandex_speech import SpeechError, SpeechTranscriber
from yandex_speech.config import RuntimeContext
from yandex_speech.jobs import JobHandle
from yandex_speech.storage import ObjectStorageUploader, UploadReceipt


class CleanupStorage(FakeS3):
    def __init__(self):
        super().__init__()
        self.deny_delete = False

    def delete_object(self, **request):
        if self.deny_delete:
            self.deletes.append(request)
            raise StorageError("AccessDenied")
        return super().delete_object(**request)


class CleanupTransport(RouteTransport):
    def __init__(self):
        super().__init__()
        self.after_results = None

    def get_recognition(self, operation_id, timeout):
        yield from super().get_recognition(operation_id, timeout)
        if self.after_results is not None:
            self.after_results()


class ArtifactLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "original.wav"
        with wave.open(str(self.source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(bytes(32000))
        self.original = self.source.read_bytes()
        self.context = RuntimeContext(user_home=self.root)
        self.storage = CleanupStorage()
        self.transport = CleanupTransport()
        self.uploader = ObjectStorageUploader(
            bucket="synthetic-private", job_dir=self.root / "uploads",
            _context=self.context, _client=self.storage)
        self.client = SpeechTranscriber(
            api_key="synthetic", job_dir=self.root / "jobs", _context=self.context,
            _environment={}, _uploader=self.uploader,
            _transport_factory=lambda version, transport: self.transport)

    def upload_for(self, job):
        return UploadReceipt.load(job.owned_uploads[0]["manifest_path"])

    def test_completed_and_no_speech_remove_only_owned_version_and_keep_result(self):
        for no_speech in (False, True):
            with self.subTest(no_speech=no_speech):
                if no_speech:
                    self.transport.events["accepted-" + str(len(self.transport.submitted))] = []
                replaced = {}
                def replace_after_results():
                    key = tuple(self.storage.puts[-1][field] for field in ("Bucket", "Key"))
                    version = self.storage.latest[key]
                    replaced["owned"] = (*key, version)
                    replaced["newer"] = (*key, "newer-unrelated")
                    self.storage.versions[replaced["newer"]] = {
                        "body": b"unrelated audio", "metadata": {}, "etag": '"newer"'}
                    self.storage.latest[key] = "newer-unrelated"
                self.transport.after_results = replace_after_results
                result = self.client.transcribe_auto(
                    self.source, transport="rest", use_storage=True)
                job = JobHandle.load(result["job_manifest_path"])
                self.assertEqual(result["status"], "no_speech" if no_speech else "completed")
                self.assertFalse(replaced["owned"] in self.storage.versions, "owned S3 version was retained")
                self.assertTrue(replaced["newer"] in self.storage.versions, "newer unrelated S3 version was deleted")
                self.assertEqual(self.upload_for(job).status, "deleted")
                self.assertEqual(job.cached_result()["text"], result["text"])
                self.assertTrue(self.source.read_bytes() == self.original, "local original changed")
                self.assertEqual(result["cleanup"]["status"], "deleted")
        self.assertEqual(len(self.storage.puts), 2)
        self.assertEqual(len(self.transport.submitted), 2)

    def test_local_cancellation_keeps_input_needed_by_accepted_cloud_job(self):
        job = self.client.start_transcription(
            self.source, transport="rest", use_storage=True)
        upload = self.upload_for(job)
        self.transport.get_operation = lambda operation_id, timeout: {
            "id": operation_id, "done": False}
        with self.assertRaises(SpeechError) as raised:
            self.client.poll_until_complete(
                job, poll_interval=0.001, progress=lambda event: self.client.cancel(job))
        self.assertEqual(raised.exception.category, "CANCELLED")
        self.assertTrue((upload.bucket, upload.key, upload.data["version_id"]) in self.storage.versions, "required S3 input was deleted")
        self.assertEqual(self.storage.deletes, [])
        self.assertEqual(len(self.transport.submitted), 1)

    def test_uncertain_submission_keeps_upload_without_reupload_or_replay(self):
        self.transport.recognize_file = Mock(side_effect=SpeechError(
            "SUBMISSION_UNCERTAIN", "synthetic uncertain acceptance"))
        with self.assertRaises(SpeechError) as raised:
            self.client.start_transcription(
                self.source, transport="rest", use_storage=True)
        job = JobHandle.load(raised.exception.receipt_path)
        upload = self.upload_for(job)
        self.assertEqual(job.status, "submission_uncertain")
        self.assertTrue((upload.bucket, upload.key, upload.data["version_id"]) in self.storage.versions, "required S3 input was deleted")
        self.assertEqual(self.storage.deletes, [])
        self.assertEqual(len(self.storage.puts), 1)
        self.assertEqual(self.transport.recognize_file.call_count, 1)

    def test_failed_cleanup_preserves_transcript_and_cached_poll_retries_only_deletion(self):
        self.storage.deny_delete = True
        result = self.client.transcribe_auto(
            self.source, transport="rest", use_storage=True)
        job = JobHandle.load(result["job_manifest_path"])
        upload = self.upload_for(job)
        owned = (upload.bucket, upload.key, upload.data["version_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["cleanup"]["status"], "pending")
        self.assertEqual(job.data["cleanup"]["status"], "pending")
        self.assertEqual(job.cached_result()["text"], result["text"])
        self.assertTrue(owned in self.storage.versions, "failed cleanup lost its S3 target")
        self.storage.deny_delete = False
        recovered = self.client.poll_until_complete(JobHandle.load(job.manifest_path))
        self.assertEqual(recovered["text"], result["text"])
        self.assertEqual(recovered["cleanup"]["status"], "deleted")
        self.assertEqual(JobHandle.load(job.manifest_path).data["cleanup"]["status"], "deleted")
        self.assertFalse(owned in self.storage.versions, "cleanup retry retained its S3 target")
        self.assertEqual(self.upload_for(job).status, "deleted")
        self.assertEqual(len(self.storage.puts), 1)
        self.assertEqual(len(self.transport.submitted), 1)
        self.assertTrue(self.source.read_bytes() == self.original, "local original changed")

    def test_explicit_cleanup_opt_out_retains_upload_and_reports_it(self):
        config = self.context.config_file
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"speech": {"object_storage": {"auto_cleanup": False}}}))
        result = self.client.transcribe_auto(self.source, transport="rest", use_storage=True)
        job = JobHandle.load(result["job_manifest_path"])
        upload = self.upload_for(job)
        self.assertEqual(result["cleanup"]["status"], "retained")
        self.assertIn((upload.bucket, upload.key, upload.data["version_id"]), self.storage.versions)
        self.assertEqual(self.storage.deletes, [])

    def test_confirmed_cloud_failure_releases_upload_but_preserves_primary_error(self):
        job = self.client.start_transcription(self.source, transport="rest", use_storage=True)
        upload = self.upload_for(job)
        self.transport.get_operation = lambda operation_id, timeout: {
            "id": operation_id, "done": True, "error": {"code": 13}}
        with self.assertRaises(SpeechError) as raised:
            self.client.poll_until_complete(job)
        self.assertEqual(raised.exception.category, "PROVIDER")
        self.assertEqual(UploadReceipt.load(upload.manifest_path).status, "deleted")
        self.assertTrue(self.source.exists())
        self.assertEqual(len(self.transport.submitted), 1)

    def test_definite_rejection_releases_upload_without_a_second_submission(self):
        self.transport.recognize_file = Mock(side_effect=SpeechError("PERMISSION", "definite rejection"))
        with self.assertRaises(SpeechError) as raised:
            self.client.start_transcription(self.source, transport="rest", use_storage=True)
        job = JobHandle.load(raised.exception.receipt_path)
        self.assertEqual(raised.exception.category, "PERMISSION")
        self.assertEqual(self.upload_for(job).status, "deleted")
        self.assertEqual(self.transport.recognize_file.call_count, 1)

    def test_second_accepted_consumer_prevents_deletion_until_it_finishes(self):
        first = self.client.start_transcription(self.source, transport="rest", use_storage=True)
        upload = self.upload_for(first)
        second = self.client.start_transcription(upload, transport="rest")
        result = self.client.poll_until_complete(first)
        self.assertEqual(result["cleanup"]["status"], "retained")
        self.assertEqual(UploadReceipt.load(upload.manifest_path).status, "uploaded")
        self.client.poll_until_complete(second)
        self.assertEqual(UploadReceipt.load(upload.manifest_path).status, "deleted")
        self.assertEqual(len(self.storage.puts), 1)
        self.assertEqual(len(self.transport.submitted), 2)

    def test_success_reply_without_actual_s3_deletion_stays_pending(self):
        self.storage.delete_object = lambda **request: {"ResponseMetadata": {"HTTPStatusCode": 204}}
        result = self.client.transcribe_auto(self.source, transport="rest", use_storage=True)
        self.assertEqual(result["cleanup"]["status"], "pending")
        self.assertEqual(result["status"], "completed")
