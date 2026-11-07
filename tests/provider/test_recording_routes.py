"""Opt-in real v1 fast-path/window fallback, with local async admission faults."""
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
from yandex_speech import SpeechError, SpeechTranscriber
from yandex_speech.config import RuntimeContext


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1", "explicit provider opt-in required")
class LiveRecordingRoutesTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.environ.get("YANDEX_SPEECH_API_KEY"), "select an existing speech credential")
        destination = Path(os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR",
                           str(Path.home() / ".local/state/yandex-speech/route-acceptance")))
        self.root = destination / ("routes-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True, mode=0o700)
        self.client = SpeechTranscriber(job_dir=self.root / "jobs", _context=RuntimeContext(user_home=self.root))

    def record(self, report):
        with os.fdopen(os.open(self.root / "acceptance.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(report, stream, indent=2)

    def test_short_raw_auto_has_one_direct_request_and_no_async_job(self):
        source = fixture_dir() / "single.wav"
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        from yandex_speech.grpc_stt import GrpcTransport
        from yandex_speech.rest_stt import RestTransport
        with patch.object(GrpcTransport, "recognize_file", side_effect=AssertionError("unexpected async gRPC")):
            with patch.object(RestTransport, "recognize_file", side_effect=AssertionError("unexpected async REST")):
                result = self.client.transcribe_auto(source, literature_text=False, deadline_seconds=120)
        self.assertEqual(result["api_version"], "1")
        self.assertEqual(result["mode"], "direct")
        self.assertTrue(result["input_complete"])
        self.assertIn("контрольный хвост", result["text"].casefold())
        self.assertFalse(list((self.root / "jobs").glob("*/job.json")))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.record({"case": self.id(), "api_version": "1", "mode": "direct", "tail_verified": True,
                     "async_submissions": 0, "source_unchanged": True,
                     "transcript_sha256": hashlib.sha256(result["text"].encode()).hexdigest()})

    def test_long_raw_auto_uses_windows_after_local_async_limits_and_cli_has_no_fake_job(self):
        source = Path(os.environ["YANDEX_SPEECH_TEST_FALLBACK_FIXTURE"])
        proof = json.loads((source.parent / "acceptance.json").read_text())
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(proof["source_sha256"], before)
        self.assertEqual(proof["source_seconds"], 56)
        self.assertTrue(proof["source"].startswith("synthetic"))
        from yandex_speech.grpc_stt import GrpcTransport
        from yandex_speech.rest_stt import RestTransport
        with patch("yandex_speech.grpc_stt.MAX_GRPC_BYTES", 320000), patch("yandex_speech.rest_stt.MAX_REST_BYTES", 320000):
            with patch.object(GrpcTransport, "recognize_file", side_effect=AssertionError("oversized async gRPC")):
                with patch.object(RestTransport, "recognize_file", side_effect=AssertionError("oversized async REST")):
                    result = self.client.transcribe_auto(source, literature_text=False, deadline_seconds=120)
                    with self.assertRaises(SpeechError):
                        self.client.transcribe_auto(source, timestamps=True, deadline_seconds=120)
        self.assertEqual(result["api_version"], "1")
        self.assertTrue(result["input_complete"])
        self.assertIn("контрольный хвост", result["text"].casefold())
        self.assertEqual([window["start_seconds"] for window in result["raw"]["windows"]], [0, 25, 50])
        self.assertTrue(any(reason["kind"] == "async-unavailable" for reason in result["routing"]["reasons"]))
        output = self.root / "cli.json"
        script = Path(__file__).resolve().parents[2] / "scripts/transcribe.py"
        program = ("import runpy,sys; import yandex_speech.grpc_stt as g, yandex_speech.rest_stt as r; "
                   "g.MAX_GRPC_BYTES=r.MAX_REST_BYTES=320000; "
                   "script=sys.argv[1];sys.argv=sys.argv[1:];runpy.run_path(script,run_name='__main__')")
        environment = {name: os.environ[name] for name in ("PATH", "VIRTUAL_ENV", "YANDEX_SPEECH_API_KEY") if name in os.environ}
        environment["HOME"] = str(self.root)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        cli = subprocess.run([sys.executable, "-c", program, str(script), str(source), "--no-literature-text",
            "--format", "json", "--output", str(output), "--deadline-seconds", "120"],
            env=environment, cwd=self.root, capture_output=True, text=True, timeout=150)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        cli_result = json.loads(output.read_text())
        self.assertEqual(cli_result["api_version"], "1")
        self.assertFalse(output.with_suffix(".job.json").exists())
        self.assertEqual(cli.stdout.strip(), str(output))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.record({"case": self.id(), "api_version": "1", "windows": 3, "tail_verified": True,
                     "async_submissions": 0, "local_async_budget": 320000, "hard_feature_refused": True,
                     "cli_success": True, "fake_job_pointer": False, "source_unchanged": True,
                     "transcript_sha256": hashlib.sha256(result["text"].encode()).hexdigest()})
