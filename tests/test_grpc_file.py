import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import grpc
from tests.helpers import make_wave
from tests.test_results import final, refinement
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.grpc_stt import build_file_request, MAX_GRPC_BYTES
from yandex_speech.media import prepare_audio
from yandex_speech.transcription import SpeechTranscriber


class FakeRpc:
    def __init__(self):
        self.submissions = []
        self.polls = []
        self.events = [final(0, "контрольный хвост"),
                       refinement(0, "Контрольный хвост.")]
        self.failure = None

    def recognize_file(self, request, timeout):
        self.submissions.append(request)
        if self.failure:
            raise self.failure
        return {"id": "operation-test", "done": False}

    def get_operation(self, operation_id, timeout):
        self.polls.append(operation_id)
        return {"id": operation_id, "done": True}

    def get_recognition(self, operation_id, timeout):
        return iter(self.events)

    def close(self):
        pass


class UncertainRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.DEADLINE_EXCEEDED

    def details(self):
        return "synthetic-credential-and-transcript-must-not-leak"


class GrpcFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = make_wave(self.root / "sample.wav")
        self.rpc = FakeRpc()
        self.client = SpeechTranscriber(api_key="synthetic-key",
            job_dir=self.root / "jobs", _context=RuntimeContext(user_home=self.root),
            _transport=self.rpc)

    def test_actual_protobuf_fields_and_effective_policy(self):
        with prepare_audio(self.path) as audio:
            request = build_file_request(audio, language="ru-RU", diarize=True,
                                         literature_text=True)
        model = request.recognition_model
        self.assertEqual(model.model, "general:rc")
        self.assertTrue(model.text_normalization.literature_text)
        self.assertFalse(model.text_normalization.profanity_filter)
        self.assertEqual(model.text_normalization.text_normalization, 1)
        self.assertEqual(model.text_normalization.phone_formatting_mode, 1)
        self.assertEqual(request.speaker_labeling.speaker_labeling, 1)
        self.assertEqual(request.WhichOneof("AudioSource"), "content")
        self.assertLessEqual(request.ByteSize(), MAX_GRPC_BYTES)

    def test_serialized_envelope_not_only_audio_size_is_admitted(self):
        with prepare_audio(self.path) as audio:
            with patch("yandex_speech.grpc_stt.MAX_GRPC_BYTES", audio.size_bytes):
                with self.assertRaises(SpeechError) as cm:
                    build_file_request(audio, language="ru-RU")
        self.assertEqual(cm.exception.category, "LIMIT")

    def test_submit_save_resume_getrecognition_no_resubmission(self):
        job = self.client.start_transcription(self.path, timestamps=True)
        self.assertEqual(job.operation_id, "operation-test")
        receipt = json.loads(job.manifest_path.read_text())
        self.assertEqual(receipt["api_version"], "3")
        self.assertNotIn("synthetic-key", job.manifest_path.read_text())
        recovered = SpeechTranscriber(api_key="synthetic-key", _transport=self.rpc,
            _context=RuntimeContext(user_home=self.root), job_dir=self.root / "jobs")
        result = recovered.poll_until_complete(job.manifest_path)
        self.assertEqual(result["text"], "Контрольный хвост.")
        self.assertTrue(result["input_complete"])
        self.assertEqual(len(self.rpc.submissions), 1)
        self.assertEqual(result["completion_kind"], "recorded-file")

    def test_uncertain_submission_is_saved_and_not_retried(self):
        self.rpc.failure = UncertainRpcError()
        with self.assertRaises(SpeechError) as cm:
            self.client.start_transcription(self.path)
        self.assertEqual(cm.exception.category, "SUBMISSION_UNCERTAIN")
        self.assertEqual(len(self.rpc.submissions), 1)
        receipts = list((self.root / "jobs").glob("*/job.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())["status"], "submission_uncertain")
        self.assertNotIn("synthetic-credential", str(cm.exception))

    def test_fixed_version_does_not_fall_back_or_reinterpret_resume(self):
        for version in ["1", "2"]:
            with self.subTest(version=version):
                with self.assertRaises(SpeechError):
                    self.client.start_transcription(self.path, api_version=version)
        self.assertEqual(self.rpc.submissions, [])
        job = self.client.start_transcription(self.path)
        with self.assertRaises(SpeechError):
            self.client.poll_until_complete(job, api_version="2")
