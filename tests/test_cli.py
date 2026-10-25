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
            root=Path(directory)
            config=root/"chosen.json"
            config.write_text('{"speech":{"language_code":"en-US"}}')
            job=Mock()
            with patch("yandex_speech.cli.SpeechTranscriber") as cls:
                client=cls.return_value
                client.start_transcription.return_value=job
                client.poll_until_complete.return_value={"text":"test","input_complete":True,"segments":[]}
                result=transcribe_main(["input.wav","--config",str(config),"--no-literature-text",
                    "--output",str(root/"out.txt"),"--format","txt"])
                self.assertEqual(result,0)
                self.assertEqual(cls.call_args.kwargs["_context"].selected_file,config)
                self.assertFalse(client.start_transcription.call_args.kwargs["literature_text"])
                self.assertIsNone(client.start_transcription.call_args.kwargs["language_code"])

    def test_default_recording_receipt_and_documented_error_codes(self):
        from yandex_speech.cli import transcribe_main
        import contextlib,io
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            job=Mock(manifest_path=root/"durable"/"job.json")
            with patch("yandex_speech.cli.SpeechTranscriber") as cls:
                cls.return_value.start_transcription.return_value=job
                cls.return_value.poll_until_complete.return_value={"text":"x","input_complete":True}
                with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(transcribe_main(["audio.wav","--output",str(root/"meeting.txt")]),0)
            pointer=json.loads((root/"meeting.job.json").read_text())
            self.assertEqual(pointer["manifest_path"],str(job.manifest_path))
            for category,expected in [("AUTH",4),("PERMISSION",4),("PREREQUISITE",3),
                    ("LIMIT",3),("UNSUPPORTED_AUDIO",3),("TIMEOUT",6),
                    ("OUTPUT_PARTIAL",7),("SUBMISSION_UNCERTAIN",7),("PROVIDER",5)]:
                with self.subTest(category=category):
                    with patch("yandex_speech.cli.SpeechTranscriber",side_effect=SpeechError(category,"synthetic")):
                        with contextlib.redirect_stderr(io.StringIO()):
                            result=transcribe_main(["audio.wav","--output",str(root/(category+".txt"))])
                    self.assertEqual(result,expected)

    def test_explicit_rest_recovery_and_new_submission_flags_are_rejected(self):
        from yandex_speech.cli import transcribe_main
        import contextlib,io
        for extra in [["--transport","rest"],["--mode","async"],["--diarize"],
                      ["--lang","ru-RU"],["--literature-text"]]:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                with patch("yandex_speech.cli.SpeechTranscriber") as cls:
                    with contextlib.redirect_stderr(io.StringIO()):
                        code=transcribe_main(["--operation-id","existing","--api-version","3",
                                             "--output",str(Path(directory)/"out.txt"),*extra])
                    self.assertEqual(code,2)
                    cls.assert_not_called()
