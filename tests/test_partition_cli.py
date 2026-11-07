"""Exercise the public explicit split CLI and durable recovery pointer."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_partition_audio import wav
from tests.test_recording_partitions import Transport
from yandex_speech import SpeechTranscriber
from yandex_speech.cli import transcribe_main
from yandex_speech.config import RuntimeContext
from yandex_speech.jobs import JobHandle
from yandex_speech.partition_jobs import PartitionJob


class PartitionCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "recording.wav"
        wav(self.source, frames=8 * 16000)
        self.transport = Transport()
        self.client = SpeechTranscriber(api_key="synthetic-key", job_dir=self.root / "jobs",
            _context=RuntimeContext(user_home=self.root),
            _transport_factory=lambda version, transport: self.transport)
        for patcher in (
            patch("yandex_speech.partition_audio.inline_frame_capacity", return_value=6 * 16000),
            patch("yandex_speech.cli.SpeechTranscriber", return_value=self.client),
            patch("yandex_speech.cli.RuntimeContext",
                  side_effect=lambda **kw: RuntimeContext(user_home=self.root, selected_file=kw.get("selected_file")))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = transcribe_main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_explicit_cli_split_publishes_parent_pointer_and_resumes_another_format_from_cache(self):
        output = self.root / "transcript.json"
        code, stdout, stderr = self.invoke([str(self.source), "--mode", "async", "--api-version", "3",
            "--transport", "rest", "--split-inline", "--timestamps", "--output", str(output)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, str(output) + "\n")
        result = json.loads(output.read_text())
        self.assertTrue(result["input_complete"])
        self.assertEqual(len(result["parts"]), 3)
        pointer = json.loads(output.with_suffix(".job.json").read_text())
        parent = JobHandle.load(pointer["manifest_path"])
        self.assertIsInstance(parent, PartitionJob)
        self.assertIn(str(parent.manifest_path), stderr)
        self.source.unlink()
        for part in parent.parts:
            Path(part["audio_path"]).unlink()
            Path(part["child_manifest_path"]).unlink()
        self.client.api_key = None
        recovered = self.root / "recovered.srt"
        with patch.object(self.client, "_rpc", side_effect=AssertionError("cached resume must not call cloud")):
            code, stdout, stderr = self.invoke(["--resume", str(output.with_suffix(".job.json")),
                "--format", "srt", "--output", str(recovered)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, str(recovered) + "\n")
        self.assertIn("Tail accepted-2.", recovered.read_text())
        self.assertEqual(len(self.transport.submitted), 3)

    def test_auto_with_explicit_split_does_not_enter_text_only_fallback(self):
        output = self.root / "auto.txt"
        with patch("yandex_speech.recording_routes.transcribe_recording",
                   side_effect=AssertionError("explicit partitioning must retain its own receipt")):
            code, stdout, stderr = self.invoke([str(self.source), "--split-inline", "--transport", "rest",
                "--output", str(output), "--format", "txt"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, str(output) + "\n")
        self.assertEqual(len(self.transport.submitted), 3)

    def test_conflicting_modes_versions_and_storage_do_not_start_paid_work(self):
        variants = [["--api-version", "1"], ["--api-version", "2"], ["--mode", "direct"],
                    ["--bucket", "explicit-bucket"]]
        for index, options in enumerate(variants):
            with self.subTest(options=options):
                code, stdout, stderr = self.invoke([str(self.source), "--split-inline",
                    "--transport", "rest", "--output", str(self.root / f"out-{index}.json"), *options])
                self.assertEqual(code, 2, stderr)
                self.assertEqual(stdout, "")
        self.assertEqual(self.transport.submitted, [])
