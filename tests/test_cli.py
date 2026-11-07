import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yandex_speech.errors import SpeechError


class CliTests(unittest.TestCase):
    def test_version_works_from_external_directory_without_credentials(self):
        for script in ["transcribe.py","tts.py"]:
            path=Path("scripts",script).resolve()
            with tempfile.TemporaryDirectory() as directory:
                result=subprocess.run([sys.executable,str(path),"--version"],cwd=directory,
                    env={"HOME":directory,"PATH":"/usr/bin:/bin"},capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),Path("VERSION").read_text().strip())

    def test_invalid_conflicting_input_fails_before_provider(self):
        from yandex_speech.cli import transcribe_main
        with patch("yandex_speech.cli.SpeechTranscriber") as client:
            with self.assertRaises(SystemExit) as cm:
                transcribe_main(["audio.wav","--resume","job.json","--output","out.json"])
            self.assertEqual(cm.exception.code,2)
            client.assert_not_called()

    def test_explicit_output_survives_preflight_without_submission(self):
        from yandex_speech.cli import transcribe_main
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"out.json"
            path.write_text("sentinel")
            with patch("yandex_speech.cli.SpeechTranscriber") as client:
                self.assertNotEqual(transcribe_main(["input.wav","--output",str(path)]),0)
                client.assert_not_called()
            self.assertEqual(path.read_text(),"sentinel")

    def test_cli_only_selector_and_explicit_false_reach_core(self):
        from yandex_speech.cli import transcribe_main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "chosen.json"
            config.write_text('{"speech":{"language_code":"en-US"}}')
            with patch("yandex_speech.cli.SpeechTranscriber") as cls:
                client = cls.return_value
                client.transcribe_auto.return_value = {"text": "test", "input_complete": True, "segments": []}
                code = transcribe_main(["input.wav", "--config", str(config), "--no-literature-text",
                    "--output", str(root / "out.txt"), "--format", "txt"])
                self.assertEqual(code, 0)
                self.assertEqual(cls.call_args.kwargs["_context"].selected_file, config)
                self.assertFalse(client.transcribe_auto.call_args.kwargs["literature_text"])
                self.assertIsNone(client.transcribe_auto.call_args.kwargs["language_code"])
                client.start_transcription.assert_not_called()
                self.assertFalse((root / "out.job.json").exists())

    def test_default_recording_receipt_and_documented_error_codes(self):
        from yandex_speech.cli import transcribe_main
        import contextlib, io
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = Mock(manifest_path=root / "durable/job.json")
            def complete(source, **options):
                options["_on_prepared"](job)
                options["_on_accepted"](job)
                return {"text": "x", "input_complete": True, "job_manifest_path": str(job.manifest_path)}
            with patch("yandex_speech.cli.SpeechTranscriber") as cls:
                cls.return_value.transcribe_auto.side_effect = complete
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(transcribe_main(["audio.wav", "--output", str(root / "meeting.txt")]), 0)
            pointer = json.loads((root / "meeting.job.json").read_text())
            self.assertEqual(pointer["manifest_path"], str(job.manifest_path))
            for category, expected in [("AUTH", 4), ("PERMISSION", 4), ("PREREQUISITE", 3),
                    ("LIMIT", 3), ("UNSUPPORTED_AUDIO", 3), ("TIMEOUT", 6),
                    ("OUTPUT_PARTIAL", 7), ("SUBMISSION_UNCERTAIN", 7), ("PROVIDER", 5)]:
                with self.subTest(category=category):
                    with patch("yandex_speech.cli.SpeechTranscriber", side_effect=SpeechError(category, "synthetic")):
                        with contextlib.redirect_stderr(io.StringIO()):
                            code = transcribe_main(["audio.wav", "--output", str(root / (category + ".txt"))])
                    self.assertEqual(code, expected)

    def test_recovery_rejects_new_submission_flags(self):
        from yandex_speech.cli import transcribe_main
        import contextlib,io
        for extra in [["--mode","async"],["--diarize"],
                      ["--lang","ru-RU"],["--literature-text"]]:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                with patch("yandex_speech.cli.SpeechTranscriber") as cls:
                    with contextlib.redirect_stderr(io.StringIO()):
                        code=transcribe_main(["--operation-id","existing","--api-version","3",
                                             "--output",str(Path(directory)/"out.txt"),*extra])
                    self.assertEqual(code,2)
                    cls.assert_not_called()

    def test_rest_v2_and_v3_recovery_reach_core_without_new_submission(self):
        import contextlib, io
        from yandex_speech.cli import transcribe_main
        for version in ("2", "3"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                with patch("yandex_speech.cli.SpeechTranscriber") as cls:
                    cls.return_value.poll_until_complete.return_value = {
                        "schema_version": 1, "text": "Recovered.", "segments": [],
                        "status": "completed", "input_complete": True, "uncertainties": []}
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        code = transcribe_main(["--operation-id", "existing", "--api-version", version,
                            "--transport", "rest", "--output", str(Path(directory) / "out.txt"), "--format", "txt"])
                    self.assertEqual(code, 0)
                    cls.return_value.poll_until_complete.assert_called_once_with("existing",
                        api_version=version, transport="rest", deadline_seconds=1800)
                    cls.return_value.start_transcription.assert_not_called()

    def test_async_rest_bucket_and_v2_requests_publish_recoverable_jobs(self):
        import contextlib, io
        from yandex_speech.cli import transcribe_main
        for version in ("2", "3"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch("yandex_speech.cli.SpeechTranscriber") as cls:
                    cls.return_value.start_transcription.return_value.manifest_path = root / "private/job.json"
                    cls.return_value.poll_until_complete.return_value = {
                        "text": "Completed.", "segments": [], "status": "completed",
                        "input_complete": True, "uncertainties": []}
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        code = transcribe_main([str(root / "source.wav"), "--mode", "async",
                            "--api-version", version, "--transport", "rest", "--bucket", "explicit-private",
                            "--output", str(root / "out.txt"), "--format", "txt"])
                    self.assertEqual(code, 0)
                    options = cls.return_value.start_transcription.call_args.kwargs
                    self.assertEqual(options["api_version"], version)
                    self.assertEqual(options["transport"], "rest")
                    self.assertEqual(options["bucket"], "explicit-private")
                    pointer = json.loads((root / "out.job.json").read_text())
                    self.assertEqual(pointer["manifest_path"], str(root / "private/job.json"))
