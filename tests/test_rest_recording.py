"""Durable public REST lifecycle, recovery and version-specific results."""
import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from tests.test_rest_stt import Response, Session
from yandex_speech import SpeechError, SpeechTranscriber
from yandex_speech.config import RuntimeContext
from yandex_speech.jobs import JobHandle


def v3_wire(*, timing=True, speaker=True):
    word = {"text": "хвост", "startTimeMs": "100", "endTimeMs": "800"} if timing else {"text": "хвост"}
    final = {"audioCursors": {"finalIndex": "0"}, "final": {"alternatives": [{"text": "хвост", "words": [word]}]}}
    refined = {"finalRefinement": {"finalIndex": "0",
        "normalizedText": {"alternatives": [{"text": "Хвост."}]}}}
    if speaker:
        final["channelTag"] = refined["channelTag"] = "1"
    return b"\n".join(json.dumps({"result": item}, ensure_ascii=False).encode() for item in (final, refined))


class RestRecordingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "source.wav"
        with wave.open(str(self.path), "wb") as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(bytes(32000))
        self.context = RuntimeContext(user_home=self.root)

    def client(self, session, **options):
        from yandex_speech.rest_stt import RestTransport
        return SpeechTranscriber(api_key="synthetic-rest-key", job_dir=self.root / "jobs",
            _context=self.context, _transport_factory=lambda version, transport:
                RestTransport("synthetic-rest-key", api_version=version, _session=session), **options)

    def saved_job(self, version="3", transport="grpc"):
        job = JobHandle.create(self.root / "jobs", {"metadata_origin": "existing_operation"},
            parameters={"language": "ru-RU", "diarize": False, "timestamps": True, "literature_text": True},
            api_version=version, transport=transport)
        job.data["operation_ids"] = ["accepted-existing"]
        job.data["parts"][0]["operation_id"] = job.operation_id
        job.set_status("running")
        return job

    def test_public_rest_submit_poll_result_and_saved_cache_without_generated_sdk(self):
        session = Session(Response({"id": "accepted-new"}), Response({"done": True}), Response(v3_wire()))
        client = self.client(session)
        original = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch("yandex_speech.grpc_stt.sdk", side_effect=AssertionError("generated SDK was used")):
            job = client.start_transcription(self.path, transport="rest", timestamps=True, diarize=True)
            result = client.poll_until_complete(job)
            cached = client.poll_until_complete(job.manifest_path)
        self.assertEqual(result["text"], "Хвост.")
        self.assertEqual(cached, result)
        self.assertEqual(result["transport"], "rest")
        self.assertEqual(job.data["transport"], "rest")
        self.assertEqual(result["segments"][0]["words"][0]["start"], 0.1)
        self.assertEqual(result["segments"][0]["speaker"], "1")
        self.assertEqual(result["raw"][0]["result"]["audioCursors"]["finalIndex"], "0")
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET", "GET"])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), original)
        self.assertNotIn("synthetic-rest-key", job.manifest_path.read_text())

    def test_rest_recovers_grpc_accepted_operation_without_submission(self):
        job = self.saved_job()
        session = Session(Response({"done": True}), Response(v3_wire()))
        result = self.client(session).poll_until_complete(job.manifest_path, transport="rest")
        self.assertEqual(result["transport"], "rest")
        self.assertEqual(result["operation_id"], "accepted-existing")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])
        self.assertEqual(JobHandle.load(job.manifest_path).data["transport"], "grpc")

    def test_uncertain_rest_submission_exposes_durable_receipt_and_is_never_retried(self):
        import requests
        session = Session(requests.exceptions.ReadTimeout("sentinel-secret"))
        client = self.client(session)
        with self.assertRaises(SpeechError) as raised:
            client.start_transcription(self.path, transport="rest")
        self.assertEqual(raised.exception.category, "SUBMISSION_UNCERTAIN")
        path = Path(raised.exception.as_dict()["receipt_path"])
        self.assertEqual(JobHandle.load(path).status, "submission_uncertain")
        with self.assertRaises(SpeechError):
            client.poll_until_complete(path, transport="rest")
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("sentinel-secret", json.dumps(raised.exception.as_dict()))

    def test_auto_missing_grpc_selects_rest_but_explicit_grpc_remains_strict(self):
        session = Session(Response({"id": "accepted-rest"}))
        client = self.client(session)
        with patch("yandex_speech.transcription.grpc_sdk", side_effect=SpeechError("PREREQUISITE", "absent")):
            job = client.start_transcription(self.path, api_version="3")
            self.assertEqual(job.data["transport"], "rest")
            self.assertTrue(job.data["routing"]["reasons"])
            with self.assertRaises(SpeechError) as raised:
                client.start_transcription(self.path, transport="grpc")
        self.assertEqual(raised.exception.category, "PREREQUISITE")
        self.assertEqual(len(session.calls), 1)

    def test_explicit_v2_uri_uses_v2_schema_and_real_times_with_offset_once(self):
        info = {"duration_ms": 1000, "size_bytes": 100, "timeline_offset_seconds": 3.5,
                "prepared": {"container": "MP3", "sample_rate_hertz": 44100, "channels": 2}}
        operation = {"done": True, "response": {"chunks": [{"channelTag": "2", "alternatives": [{
            "text": "Хвост.", "words": [{"word": "Хвост", "startTime": "0.1s", "endTime": "0.8s"}]}]}]}}
        session = Session(Response({"id": "accepted-v2"}), Response(operation))
        client = self.client(session)
        job = client.start_transcription("https://storage.yandexcloud.net/private-speech/audio.mp3",
            source_info=info, api_version="2", transport="rest", timestamps=True)
        result = client.poll_until_complete(job)
        self.assertEqual(job.api_version, "2")
        self.assertEqual(result["api_version"], "2")
        self.assertEqual(result["segments"][0]["start"], 3.6)
        self.assertEqual(result["segments"][0]["end"], 4.3)
        self.assertIsNone(result["segments"][0]["speaker"])
        self.assertEqual(result["segments"][0]["channel_tag"], "2")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(json.loads(session.calls[0][2]["data"])["audio"]["uri"], job.source_ref)

    def test_saved_v2_ignores_current_v3_default_and_conflicting_version_has_no_request(self):
        job = self.saved_job(version="2", transport="rest")
        session = Session(Response({"done": True, "response": {"chunks": []}}))
        client = self.client(session, _adapter_defaults={"api_version": "3"})
        with self.assertRaises(SpeechError) as raised:
            client.poll_until_complete(job.manifest_path, api_version="3")
        self.assertEqual(raised.exception.category, "CONFIG")
        self.assertEqual(session.calls, [])
        result = client.poll_until_complete(job.manifest_path)
        self.assertEqual(result["status"], "no_speech")
        self.assertEqual(result["api_version"], "2")
        self.assertEqual(len(session.calls), 1)

    def test_missing_required_timing_or_speaker_is_a_failure_after_acceptance(self):
        for timing, speaker in ((False, True), (True, False)):
            with self.subTest(timing=timing, speaker=speaker):
                session = Session(Response({"id": "accepted-required"}), Response({"done": True}),
                                  Response(v3_wire(timing=timing, speaker=speaker)))
                client = self.client(session)
                job = client.start_transcription(self.path, transport="rest", timestamps=True, diarize=True)
                with self.assertRaises(SpeechError) as raised:
                    client.poll_until_complete(job)
                self.assertEqual(raised.exception.category, "PROVIDER")
                self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    def test_local_v2_upload_adapts_wav_and_retains_original_identity(self):
        from yandex_speech import ObjectStorageUploader
        from tests.test_owned_storage import FakeS3
        backend = FakeS3()
        uploader = ObjectStorageUploader(bucket="private-speech", job_dir=self.root / "uploads",
            _context=self.context, _client=backend)
        session = Session(Response({"id": "accepted-v2-file"}), Response({"done": True, "response": {
            "chunks": [{"alternatives": [{"text": "Хвост", "words": [
                {"word": "Хвост", "startTime": "0.1s", "endTime": "0.8s"}]}]}]}}))
        client = self.client(session, _uploader=uploader)
        original = hashlib.sha256(self.path.read_bytes()).hexdigest()
        job = client.start_transcription(self.path, api_version="2", transport="rest",
                                        bucket="private-speech", timestamps=True)
        result = client.poll_until_complete(job)
        self.assertEqual(result["api_version"], "2")
        self.assertEqual(job.source_info["source_sha256"], original)
        self.assertEqual(job.source_info["prepared"]["container"], "LPCM")
        self.assertEqual(job.source_info["size_bytes"], 32000)
        self.assertTrue(job.source_ref.endswith(".pcm"))
        self.assertEqual(len(job.owned_uploads), 1)
        self.assertEqual(len(backend.puts), 1)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), original)

    def test_inline_override_preserves_an_existing_configured_bucket_without_upload(self):
        from unittest.mock import Mock
        uploader = Mock()
        uploader.upload.side_effect = AssertionError("explicit inline unexpectedly uploaded")
        session = Session(Response({"id": "accepted-inline"}))
        client = self.client(session, _uploader=uploader,
            _adapter_defaults={"object_storage": {"bucket": "configured-private"}})
        job = client.start_transcription(self.path, transport="rest", use_storage=False)
        self.assertEqual(job.owned_uploads, [])
        self.assertIn("content", json.loads(session.calls[0][2]["data"]))
        uploader.upload.assert_not_called()

    def test_incompatible_v2_speakers_fail_before_upload_or_recognition(self):
        from unittest.mock import Mock
        uploader = Mock()
        session = Session()
        client = self.client(session, _uploader=uploader)
        with self.assertRaises(SpeechError) as raised:
            client.start_transcription(self.path, api_version="2", diarize=True, bucket="private-speech")
        self.assertEqual(raised.exception.category, "CONFIG")
        uploader.upload.assert_not_called()
        self.assertEqual(session.calls, [])

    def test_scoped_environment_reaches_storage_without_foreign_ambient_credentials(self):
        import os
        from yandex_speech.storage import ObjectStorageUploader
        from tests.test_owned_storage import FakeS3
        own = {"YANDEX_SPEECH_S3_BUCKET": "own-bucket",
               "YANDEX_SPEECH_AWS_ACCESS_KEY_ID": "own-storage-id",
               "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY": "own-storage-secret"}
        backend, captured = FakeS3(), {}
        def uploader_factory(**options):
            uploader = ObjectStorageUploader(_client=backend, **options)
            captured.update(bucket=uploader.bucket, credentials=uploader._credentials())
            return uploader
        session = Session(Response({"id": "scoped-storage"}))
        with patch.dict(os.environ, {"YANDEX_SPEECH_S3_BUCKET": "foreign-bucket",
            "YANDEX_SPEECH_AWS_ACCESS_KEY_ID": "foreign-id",
            "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY": "foreign-secret"}):
            client = self.client(session, _environment=own)
            with patch("yandex_speech.storage.ObjectStorageUploader", side_effect=uploader_factory):
                job = client.start_transcription(self.path, transport="rest")
        self.assertEqual(captured, {"bucket": "own-bucket",
            "credentials": ("own-storage-id", "own-storage-secret")})
        self.assertIn("/own-bucket/", job.source_ref)
        self.assertEqual(len(backend.puts), 1)
        self.assertNotIn("own-storage-secret", job.manifest_path.read_text())

    def test_malformed_rest_metadata_never_publishes_a_complete_result(self):
        import copy
        valid = {"audioCursors": {"finalIndex": "0"}, "channelTag": "1",
            "final": {"alternatives": [{"text": "Хвост", "words": [
                {"text": "Хвост", "startTimeMs": "100", "endTimeMs": "800"}]}]}}
        cases = []
        for start, end in ((False, True), ("nan", "800"), (100.5, 800), ("100", None)):
            event = copy.deepcopy(valid)
            event["final"]["alternatives"][0]["words"][0].update(startTimeMs=start, endTimeMs=end)
            cases.append(event)
        for channel in ({"invalid": "speaker"}, True, ["1"]):
            event = copy.deepcopy(valid)
            event["channelTag"] = channel
            cases.append(event)
        cases.extend(({"eouUpdate": {"timeMs": "not-a-time"}},
                      {"eouUpdate": {"timeMs": False}},
                      {"final": {"alternatives": "not-a-list"}}))
        for event in cases:
            with self.subTest(event=event):
                frames = (valid, event) if "eouUpdate" in event else (event,)
                wire = b"\n".join(json.dumps({"result": frame}).encode() for frame in frames)
                session = Session(Response({"id": "accepted-malformed"}), Response({"done": True}), Response(wire))
                client = self.client(session)
                job = client.start_transcription(self.path, transport="rest", timestamps=True, diarize=True)
                with self.assertRaises(SpeechError) as raised:
                    client.poll_until_complete(job)
                self.assertEqual(raised.exception.category, "PROVIDER")
                self.assertIsNone(JobHandle.load(job.manifest_path).cached_result())
                self.assertFalse((job.manifest_path.parent / "result.json").exists())
                self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    def test_diarization_adapts_local_stereo_without_changing_ordinary_admission(self):
        import base64, io
        stereo = self.root / "stereo.wav"
        with wave.open(str(stereo), "wb") as stream:
            stream.setnchannels(2); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(bytes(64000))
        original = hashlib.sha256(stereo.read_bytes()).hexdigest()
        for diarize in (False, True):
            with self.subTest(diarize=diarize):
                session = Session(Response({"id": "accepted-stereo"}))
                job = self.client(session).start_transcription(stereo, transport="rest", diarize=diarize)
                body = json.loads(session.calls[0][2]["data"])
                audio = base64.b64decode(body["content"])
                with wave.open(io.BytesIO(audio), "rb") as stream:
                    self.assertEqual(stream.getnchannels(), 1 if diarize else 2)
                self.assertEqual(job.source_info["original"]["channels"], 2)
                self.assertEqual(job.source_info["prepared"]["channels"], 1 if diarize else 2)
                self.assertEqual(job.source_info["source_sha256"], original)
        self.assertEqual(hashlib.sha256(stereo.read_bytes()).hexdigest(), original)

    def test_stereo_external_uri_labeling_is_rejected_before_submission(self):
        info = {"duration_ms": 1000, "size_bytes": 64000,
                "prepared": {"container": "LPCM", "sample_rate_hertz": 16000, "channels": 2}}
        session = Session()
        with self.assertRaises(SpeechError) as raised:
            self.client(session).start_transcription("https://storage.yandexcloud.net/private/stereo.pcm",
                source_info=info, transport="rest", diarize=True)
        self.assertEqual(raised.exception.category, "CONFIG")
        self.assertEqual(session.calls, [])

    def test_upload_labeling_preparation_honors_file_default_and_explicit_false(self):
        from yandex_speech.storage import ObjectStorageUploader
        from tests.test_owned_storage import FakeS3
        config = self.root / ".config/yandex-speech/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"speech":{"enable_diarization":true}}')
        source = self.root / "stereo.wav"
        with wave.open(str(source), "wb") as stream:
            stream.setnchannels(2); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(bytes(64000))
        uploader = ObjectStorageUploader(bucket="private-source", job_dir=self.root / "uploads",
            _context=self.context, _client=FakeS3())
        selected = uploader.upload(source)
        explicit = uploader.upload(source, diarize=False)
        self.assertEqual(selected.source_info["prepared"]["channels"], 1)
        self.assertEqual(explicit.source_info["prepared"]["channels"], 2)
        self.assertEqual(selected.source_info["original"]["channels"], 2)
        self.assertNotEqual(selected.key, explicit.key)
