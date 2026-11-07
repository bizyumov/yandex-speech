"""Capability-preserving recording routes and no-resubmission boundaries."""
import copy
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from yandex_speech import SpeechError, SpeechTranscriber
from yandex_speech.config import RuntimeContext
from yandex_speech.jobs import JobHandle


def result(version):
    return {"schema_version": 1, "status": "completed", "text": "synthetic",
        "segments": [], "raw": [], "uncertainties": [], "input_complete": True,
        "completion_kind": "recorded-file", "api_version": version,
        "transport": "rest" if version == "1" else "grpc", "mode": "direct" if version == "1" else "async"}


class RecordingRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "source.wav"
        self.make_wave(1)
        self.client = SpeechTranscriber(api_key="synthetic", job_dir=self.root / "jobs",
            _context=RuntimeContext(user_home=self.root), _environment={})

    def make_wave(self, seconds):
        with wave.open(str(self.path), "wb") as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(bytes(32000 * seconds))

    def accepted(self, transport="grpc"):
        job = JobHandle.create(self.root / "jobs", {"original_path": str(self.path), "duration_ms": 1000},
            parameters={"language": "ru-RU", "diarize": False, "timestamps": False, "literature_text": True},
            transport=transport)
        job.data["operation_ids"] = ["accepted-once"]
        job.data["parts"][0]["operation_id"] = "accepted-once"
        job.set_status("running")
        return job

    def test_short_raw_auto_uses_one_direct_request_without_async_submission(self):
        legacy = Mock(return_value=result("1"))
        with patch.object(self.client, "_legacy_call", legacy):
            with patch.object(self.client, "start_transcription") as start:
                actual = self.client.transcribe_auto(self.path, literature_text=False)
        self.assertEqual(actual["api_version"], "1")
        self.assertEqual(legacy.call_args.args[0], "transcribe_direct")
        self.assertTrue(actual["routing"]["reasons"])
        start.assert_not_called()

    def test_explicit_rest_or_fixed_v3_never_uses_the_short_v1_fast_path(self):
        for options in ({"transport": "rest"}, {"api_version": "3"}, {"timestamps": True}, {"diarize": True}):
            with self.subTest(options=options):
                job = self.accepted("rest" if options.get("transport") == "rest" else "grpc")
                with patch.object(self.client, "start_transcription", return_value=job) as start:
                    with patch.object(self.client, "poll_until_complete", return_value=result("3")):
                        with patch.object(self.client, "_legacy_call") as legacy:
                            actual = self.client.transcribe_auto(self.path, literature_text=False, **options)
                self.assertEqual(actual["api_version"], "3")
                self.assertEqual(start.call_count, 1)
                legacy.assert_not_called()

    def test_missing_grpc_and_unready_async_fall_back_to_complete_v1_windows_only_for_raw(self):
        self.make_wave(31)
        attempts = []
        def start(source, **options):
            attempts.append(options["transport"])
            raise SpeechError("PREREQUISITE" if options["transport"] == "grpc" else "LIMIT", "synthetic local admission")
        with patch.object(self.client, "start_transcription", side_effect=start):
            with patch.object(self.client, "_legacy_call", return_value=result("1")) as legacy:
                actual = self.client.transcribe_auto(self.path, literature_text=False)
        self.assertEqual(attempts, ["grpc", "rest"])
        self.assertEqual(legacy.call_args.args[0], "transcribe_chunked_direct")
        self.assertEqual(actual["api_version"], "1")
        self.assertGreaterEqual(len(actual["routing"]["reasons"]), 2)

    def test_hard_features_default_literary_and_fixed_versions_never_degrade_to_v1(self):
        self.make_wave(31)
        for options in ({}, {"timestamps": True, "literature_text": False},
                        {"diarize": True, "literature_text": False},
                        {"api_version": "3", "literature_text": False},
                        {"transport": "grpc", "literature_text": False}):
            with self.subTest(options=options):
                with patch.object(self.client, "start_transcription", side_effect=SpeechError("LIMIT", "local cap")):
                    with patch.object(self.client, "_legacy_call") as legacy:
                        with self.assertRaises(SpeechError):
                            self.client.transcribe_auto(self.path, **options)
                legacy.assert_not_called()

    def test_uncertain_submission_and_speech_auth_errors_never_try_another_tier(self):
        self.make_wave(31)
        for category in ("SUBMISSION_UNCERTAIN", "AUTH", "PERMISSION", "OUTPUT_PARTIAL"):
            with self.subTest(category=category):
                with patch.object(self.client, "start_transcription", side_effect=SpeechError(category, "synthetic")) as start:
                    with patch.object(self.client, "_legacy_call") as legacy:
                        with self.assertRaises(SpeechError) as raised:
                            self.client.transcribe_auto(self.path, literature_text=False)
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(start.call_count, 1)
                legacy.assert_not_called()

    def test_accepted_job_failure_is_recovery_and_never_new_submission(self):
        self.make_wave(31)
        job = self.accepted()
        with patch.object(self.client, "start_transcription", return_value=job) as start:
            with patch.object(self.client, "poll_until_complete", side_effect=SpeechError("TIMEOUT", "accepted wait")):
                with patch.object(self.client, "_legacy_call") as legacy:
                    with self.assertRaises(SpeechError):
                        self.client.transcribe_auto(self.path, literature_text=False)
        self.assertEqual(start.call_count, 1)
        legacy.assert_not_called()

    def test_fixed_v1_long_input_selects_windows_without_an_oversized_direct_attempt(self):
        self.make_wave(31)
        with patch.object(self.client, "_legacy_call", return_value=result("1")) as legacy:
            with patch.object(self.client, "start_transcription") as start:
                self.client.transcribe_auto(self.path, api_version="1")
        self.assertEqual(legacy.call_args.args[0], "transcribe_chunked_direct")
        self.assertEqual(legacy.call_count, 1)
        start.assert_not_called()

    def test_local_rpc_initialization_failure_can_change_transport_before_acceptance(self):
        self.make_wave(31)
        calls = []
        def start(source, **options):
            calls.append(options["transport"])
            if options["transport"] == "grpc":
                pending = JobHandle.create(self.root / "jobs", {"original_path": str(source)},
                    parameters={"language": "ru-RU", "diarize": False, "timestamps": False, "literature_text": False})
                raise SpeechError("PREREQUISITE", "local transport initialization",
                                  receipt_path=str(pending.manifest_path))
            return self.accepted("rest")
        with patch.object(self.client, "start_transcription", side_effect=start):
            with patch.object(self.client, "poll_until_complete", return_value=result("3")):
                actual = self.client.transcribe_auto(self.path, literature_text=False)
        self.assertEqual(calls, ["grpc", "rest"])
        self.assertEqual(actual["routing"]["requested_transport"], "auto")

    def test_explicit_unknown_options_fail_before_any_provider_or_probe(self):
        with patch.object(self.client, "start_transcription") as start:
            with self.assertRaises(SpeechError) as raised:
                self.client.transcribe_auto(self.path, imaginary_option=True)
        self.assertEqual(raised.exception.category, "CONFIG")
        start.assert_not_called()

    def test_unready_preferred_storage_leaves_inline_available_without_foreign_credentials(self):
        self.client.adapter_defaults["object_storage"] = {"bucket": "preferred-private"}
        uploader = Mock(_provided_client=None)
        uploader._credentials.side_effect = SpeechError("AUTH", "separate upload credential missing")
        self.client._uploader = uploader
        with patch.object(self.client, "start_transcription", return_value=self.accepted()) as start:
            with patch.object(self.client, "poll_until_complete", return_value=result("3")):
                actual = self.client.transcribe_auto(self.path)
        self.assertFalse(start.call_args.kwargs["use_storage"])
        self.assertEqual(actual["routing"]["reasons"][0]["kind"], "storage-unavailable")
        with patch.object(self.client, "start_transcription") as start:
            with self.assertRaises(SpeechError) as raised:
                self.client.transcribe_auto(self.path, use_storage=True)
        self.assertEqual(raised.exception.category, "AUTH")
        start.assert_not_called()

    def test_definite_storage_denial_can_exclude_only_preferred_upload(self):
        from tests.test_owned_storage import FakeS3, StorageError
        from yandex_speech.storage import ObjectStorageUploader
        backend = FakeS3()
        backend.put_object = Mock(side_effect=StorageError("AccessDenied"))
        self.client._uploader = ObjectStorageUploader(bucket="preferred-private", job_dir=self.root / "uploads",
            _context=RuntimeContext(user_home=self.root), _client=backend)
        self.client.adapter_defaults["object_storage"] = {"bucket": "preferred-private"}
        inputs = []
        def start(source, **options):
            inputs.append(options["use_storage"])
            if options["use_storage"]:
                self.client._uploader.upload(source)
            return self.accepted()
        with patch.object(self.client, "start_transcription", side_effect=start):
            with patch.object(self.client, "poll_until_complete", return_value=result("3")):
                actual = self.client.transcribe_auto(self.path)
        self.assertEqual(inputs, [True, False])
        self.assertTrue(any(reason["kind"] == "storage-denied" for reason in actual["routing"]["reasons"]))
        self.assertEqual(backend.put_object.call_count, 1)

    def test_prepared_callback_failure_preserves_job_before_any_request(self):
        from tests.test_rest_stt import Session
        from yandex_speech.rest_stt import RestTransport
        session = Session()
        self.client._transport_factory = lambda version, transport: RestTransport("synthetic", _session=session)
        def refuse(job):
            raise SpeechError("CONFIG", "protected output already exists")
        with self.assertRaises(SpeechError) as raised:
            self.client.start_transcription(self.path, transport="rest", _on_prepared=refuse)
        self.assertEqual(JobHandle.load(raised.exception.receipt_path).status, "prepared")
        self.assertEqual(session.calls, [])

    def test_accepted_callback_sees_saved_operation_and_never_changes_routes(self):
        from tests.test_rest_stt import Response, Session
        from yandex_speech.rest_stt import RestTransport
        session = Session(Response({"id": "accepted-callback"}))
        self.client._transport_factory = lambda version, transport: RestTransport("synthetic", _session=session)
        def refuse_after_acceptance(job):
            saved = JobHandle.load(job.manifest_path)
            self.assertEqual(saved.operation_id, "accepted-callback")
            self.assertEqual(saved.status, "running")
            raise SpeechError("CONFIG", "late output race")
        with patch.object(self.client, "_legacy_call") as legacy:
            with self.assertRaises(SpeechError) as raised:
                self.client.transcribe_auto(self.path, transport="rest", _on_accepted=refuse_after_acceptance)
        self.assertEqual(raised.exception.category, "CONFIG")
        self.assertEqual(len(session.calls), 1)
        legacy.assert_not_called()

    def test_cli_auto_fallback_has_no_fake_cloud_pointer(self):
        import contextlib, io
        from yandex_speech.cli import transcribe_main
        self.make_wave(31)
        with patch("yandex_speech.cli.SpeechTranscriber", return_value=self.client):
            with patch.object(self.client, "start_transcription", side_effect=SpeechError("LIMIT", "local cap")):
                with patch.object(self.client, "_legacy_call", return_value=result("1")):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        code = transcribe_main([str(self.path), "--no-literature-text",
                            "--output", str(self.root / "transcript.txt"), "--format", "txt"])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "transcript.txt").is_file())
        self.assertFalse((self.root / "transcript.job.json").exists())
