"""Automatic inline admission must not become storage-first after bucket setup."""
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_owned_storage import FakeS3
from tests.test_recording_partitions import Transport
from yandex_speech import SpeechError, SpeechTranscriber
from yandex_speech.config import RuntimeContext
from yandex_speech.jobs import JobHandle
from yandex_speech.storage import ObjectStorageUploader


class RouteTransport(Transport):
    def get_recognition(self, operation_id, timeout):
        for event in super().get_recognition(operation_id, timeout):
            event["channel_tag"] = "1"
            yield event


class ConfiguredStorageRouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "source.wav"
        with wave.open(str(self.path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(bytes(32000))
        self.context = RuntimeContext(user_home=self.root)
        self.backend = FakeS3()
        self.uploader = ObjectStorageUploader(bucket="configured-private",
            job_dir=self.root / "uploads", _context=self.context, _client=self.backend)
        self.transport = RouteTransport()

    def client(self, *, defaults="environment"):
        environment = {"YANDEX_SPEECH_S3_BUCKET": "configured-private"}
        adapter = {}
        if defaults == "file":
            environment = {}
            config = self.root / ".config/yandex-speech/config.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({"speech": {"object_storage": {"bucket": "configured-private"}}}))
        elif defaults == "adapter":
            environment = {}
            adapter = {"object_storage": {"bucket": "configured-private"}}
        return SpeechTranscriber(api_key="synthetic", job_dir=self.root / "jobs",
            _context=self.context, _environment=environment, _adapter_defaults=adapter,
            _uploader=self.uploader, _transport_factory=lambda version, transport: self.transport)

    def assert_inline(self, actual):
        self.assertEqual(actual["api_version"], "3")
        self.assertTrue(actual["input_complete"])
        self.assertEqual(actual["routing"]["attempts"][0]["input"], "inline")
        self.assertEqual(self.backend.puts, [])
        self.assertEqual(len(self.transport.submitted), 1)
        request = self.transport.submitted[0]
        if isinstance(request, bytes):
            self.assertIn("content", json.loads(request))
            self.assertNotIn("uri", json.loads(request))
        else:
            self.assertTrue(request.content)
            self.assertFalse(request.uri)

    def test_configured_bucket_keeps_small_ogg_inline_without_s3_put(self):
        ogg = self.root / "voice.ogg"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.path),
                        "-c:a", "libopus", str(ogg)], check=True, capture_output=True)
        actual = self.client().transcribe_auto(ogg)
        self.assert_inline(actual)

    def test_saved_bucket_does_not_force_storage(self):
        self.assert_inline(self.client(defaults="file").transcribe_auto(self.path))

    def test_adapter_bucket_does_not_force_storage(self):
        self.assert_inline(self.client(defaults="adapter").transcribe_auto(self.path))

    def test_inline_success_does_not_check_unused_storage_credentials(self):
        client = self.client()
        with patch("yandex_speech.recording_routes._storage_ready",
                   side_effect=SpeechError("AUTH", "unused upload credential")) as ready:
            actual = client.transcribe_auto(self.path, transport="rest", timestamps=True, diarize=True)
        self.assert_inline(actual)
        ready.assert_not_called()
        job = JobHandle.load(next((self.root / "jobs").glob("*/job.json")))
        self.assertTrue(job.data["parameters"]["diarize"])
        self.assertTrue(job.data["parameters"]["timestamps"])
        self.assertTrue(job.data["parameters"]["literature_text"])

    def test_explicit_storage_still_uploads_once(self):
        for options in ({"use_storage": True}, {"bucket": "configured-private"}):
            with self.subTest(options=options):
                self.backend.puts.clear()
                self.transport.submitted.clear()
                actual = self.client().transcribe_auto(self.path, transport="rest", **options)
                self.assertEqual(len(self.backend.puts), 1)
                self.assertEqual(len(self.transport.submitted), 1)
                self.assertEqual(actual["routing"]["attempts"][0]["input"], "storage")
                self.assertIn("uri", json.loads(self.transport.submitted[0]))

    def test_caller_uri_and_owned_receipt_are_not_uploaded_again(self):
        receipt = self.uploader.upload(self.path)
        for source in (receipt, receipt.uri):
            with self.subTest(receipt=isinstance(source, type(receipt))):
                actual = self.client().transcribe_auto(source, source_info=receipt.source_info, transport="rest")
                self.assertTrue(actual["input_complete"])
                self.assertEqual(actual["routing"]["attempts"][0]["input"], "uri")
        self.assertEqual(len(self.backend.puts), 1)

    def test_complete_rest_wire_limit_selects_storage_before_any_submission(self):
        with patch("yandex_speech.rest_stt.MAX_REST_BYTES", 1000):
            actual = self.client().transcribe_auto(self.path, transport="rest",
                                                  timestamps=True, diarize=True)
        self.assertEqual([item["input"] for item in actual["routing"]["attempts"]], ["inline", "storage"])
        self.assertEqual(len(self.backend.puts), 1)
        self.assertEqual(len(self.transport.submitted), 1)
        self.assertIn("uri", json.loads(self.transport.submitted[0]))
        self.assertTrue(actual["input_complete"])

    def test_explicit_inline_never_uploads_over_limit(self):
        with patch("yandex_speech.rest_stt.MAX_REST_BYTES", 1000):
            with self.assertRaises(SpeechError) as raised:
                self.client().transcribe_auto(self.path, transport="rest", use_storage=False)
        self.assertEqual(raised.exception.category, "LIMIT")
        self.assertEqual(self.backend.puts, [])
        self.assertEqual(self.transport.submitted, [])

    def test_non_size_admission_failures_do_not_make_storage_necessary(self):
        for category, message in (("PREREQUISITE", "optional gRPC unavailable"),
                                  ("LIMIT", "four-hour duration exceeded")):
            with self.subTest(category=category):
                client = self.client()
                with patch.object(client, "start_transcription",
                                  side_effect=SpeechError(category, message)) as start:
                    with patch("yandex_speech.recording_routes._storage_ready") as ready:
                        with self.assertRaises(SpeechError) as raised:
                            client.transcribe_auto(self.path, transport="grpc")
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(start.call_count, 1)
                ready.assert_not_called()
                self.assertEqual(self.backend.puts, [])

    def test_missing_grpc_uses_rest_inline_without_storage(self):
        with patch("yandex_speech.transcription.grpc_sdk",
                   side_effect=SpeechError("PREREQUISITE", "optional gRPC unavailable")):
            actual = self.client().transcribe_auto(self.path)
        self.assert_inline(actual)
        self.assertEqual(actual["transport"], "rest")

    def test_recognition_errors_never_trigger_storage_or_resubmission(self):
        for category in ("PERMISSION", "AUTH", "SUBMISSION_UNCERTAIN"):
            with self.subTest(category=category):
                self.transport.recognize_file = Mock(side_effect=SpeechError(category, "synthetic"))
                with self.assertRaises(SpeechError) as raised:
                    self.client().transcribe_auto(self.path, transport="rest")
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(self.transport.recognize_file.call_count, 1)
                job = JobHandle.load(raised.exception.receipt_path)
                self.assertEqual(job.status, "submission_uncertain" if category == "SUBMISSION_UNCERTAIN" else "failed")
                self.assertEqual(self.backend.puts, [])

    def test_accepted_result_failure_never_tries_storage(self):
        client = self.client()
        with patch.object(client, "poll_until_complete", side_effect=SpeechError("PERMISSION", "synthetic")):
            with self.assertRaises(SpeechError):
                client.transcribe_auto(self.path, transport="rest")
        self.assertEqual(len(self.transport.submitted), 1)
        self.assertEqual(self.backend.puts, [])
