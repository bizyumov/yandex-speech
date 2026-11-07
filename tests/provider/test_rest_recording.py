"""Opted-in real REST file recognition and recovery of a gRPC-created operation."""
import hashlib
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests.provider.fixtures import fixture_dir
from yandex_speech import SpeechTranscriber
from yandex_speech.config import RuntimeContext


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1", "explicit provider opt-in required")
class LiveRestRecordingTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.environ.get("YANDEX_SPEECH_API_KEY"), "select an existing speech credential")
        destination = Path(os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR",
                           str(Path.home() / ".local/state/yandex-speech/rest-acceptance")))
        self.root = destination / ("rest-recording-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True, mode=0o700)
        self.fixtures = fixture_dir()
        self.client = SpeechTranscriber(job_dir=self.root / "jobs", _context=RuntimeContext(user_home=self.root))

    def record(self, result, job, **facts):
        report = {"case": self.id(), "status": result["status"], "api_version": result["api_version"],
                  "transport": result["transport"], "job_manifest": str(job.manifest_path),
                  "segments": len(result["segments"]),
                  "words": sum(len(segment["words"]) for segment in result["segments"]),
                  "transcript_sha256": hashlib.sha256(result["text"].encode()).hexdigest(), **facts}
        with os.fdopen(os.open(self.root / "acceptance.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(report, stream, indent=2)

    def test_rest_inline_two_speakers_without_generated_sdk_and_cli_cache_recovery(self):
        source = self.root / "two-speaker-stereo.wav"
        converted = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-n",
            "-i", str(self.fixtures / "two-speaker.wav"), "-ac", "2", "-ar", "16000",
            "-c:a", "pcm_s16le", str(source)], capture_output=True, timeout=60)
        self.assertEqual(converted.returncode, 0, "synthetic stereo preparation failed")
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        with patch("yandex_speech.transcription.grpc_sdk", side_effect=AssertionError("gRPC was used")):
            with patch("yandex_speech.grpc_stt.sdk", side_effect=AssertionError("generated SDK was used")):
                job = self.client.start_transcription(source, transport="rest",
                    diarize=True, timestamps=True, deadline_seconds=120)
                result = self.client.poll_until_complete(job, transport="rest", deadline_seconds=120)
        self.assertTrue(result["input_complete"])
        self.assertEqual(job.source_info["original"]["channels"], 2)
        self.assertEqual(job.source_info["prepared"]["channels"], 1)
        self.assertEqual(result["transport"], "rest")
        self.assertEqual(len({segment["speaker"] for segment in result["segments"]}), 2)
        self.assertIn("последняя контрольная фраза", result["text"].casefold())
        self.assertTrue(all(segment["words"] and all(
            word["start"] is not None and word["end"] > word["start"] for word in segment["words"])
            for segment in result["segments"]))
        self.assertTrue(all("result" in event for event in result["raw"]))
        output = self.root / "cached-recovered.srt"
        command = Path(__file__).resolve().parents[2] / "scripts/transcribe.py"
        environment = {name: os.environ[name] for name in ("PATH", "VIRTUAL_ENV") if name in os.environ}
        environment["HOME"] = str(self.root)
        recovered = subprocess.run([sys.executable, str(command), "--resume", str(job.manifest_path),
            "--transport", "rest", "--api-version", "3", "--format", "srt", "--output", str(output)],
            env=environment, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)
        self.record(result, job, generated_sdk_calls=0, source_unchanged=True,
                    native_speakers=2, source_channels=2, prepared_channels=1,
                    tail_verified=True, external_cli_cache_recovery=True)

    def test_accepted_grpc_operation_recovers_through_rest_without_another_submission(self):
        from yandex_speech.grpc_stt import GrpcTransport
        from yandex_speech.rest_stt import RestTransport
        job = self.client.start_transcription(self.fixtures / "single.wav", transport="grpc",
                                             timestamps=True, deadline_seconds=120)
        with patch.object(GrpcTransport, "recognize_file", side_effect=AssertionError("resubmitted gRPC")):
            with patch.object(RestTransport, "recognize_file", side_effect=AssertionError("resubmitted REST")):
                result = self.client.poll_until_complete(job.manifest_path, transport="rest", deadline_seconds=120)
        self.assertEqual(result["operation_id"], job.operation_id)
        self.assertEqual(result["transport"], "rest")
        self.assertIn("контрольный хвост", result["text"].casefold())
        self.assertEqual(result["metadata_availability"]["timing"], "provider")
        self.record(result, job, submissions=1, recovery_transport="rest", tail_verified=True)
