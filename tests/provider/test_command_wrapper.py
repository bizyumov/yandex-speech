"""Opt-in checks of the active consumer's actual command-STT contract."""
import json
import os
import pwd
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from ruamel.yaml import YAML


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1"
                     and os.environ.get("YANDEX_SPEECH_HOST_TESTS") == "1",
                     "explicit consumer/provider opt-in required")
class LiveCommandWrapperTests(unittest.TestCase):
    def setUp(self):
        self.profile = Path(os.environ["HERMES_HOME"])
        self.assertEqual(os.geteuid(), self.profile.stat().st_uid,
                         "run as the active profile's actual Unix principal")
        self.assertEqual(Path(sys.prefix).resolve(), Path(os.environ["VIRTUAL_ENV"]).resolve())
        self.assertTrue(os.environ.get("YANDEX_SPEECH_API_KEY"),
                        "supply the selected profile's authorized runtime credential")
        config = YAML(typ="safe").load(self.profile / "config.yaml")
        self.stt = config["stt"]
        self.provider = self.stt["provider"]
        self.command = self.stt["providers"][self.provider]
        self.assertEqual(self.command["type"], "command")
        self.tmp = tempfile.TemporaryDirectory(prefix="speech-command-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def transcribe(self, source):
        from tools.transcription_command import _transcribe_command_stt
        result = _transcribe_command_stt(str(source), self.provider,
                                         self.command, self.stt)
        destination = os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR")
        if destination:
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            record = {"case": self.id(), "principal": pwd.getpwuid(os.geteuid()).pw_name,
                      "success": result.get("success"),
                      "transcript_empty": not result.get("transcript"),
                      "pathname_leaked": "hermes-cmd-stt-" in result.get("transcript", ""),
                      "transcript": result.get("transcript", "")}
            (directory / (self._testMethodName + ".json")).write_text(
                json.dumps(record, ensure_ascii=False, indent=2))
        return result

    def test_silence_never_becomes_a_pathname_user_message(self):
        source = self.root / "silence.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0" * 32000)
        result = self.transcribe(source)
        self.assertEqual(result.get("transcript", ""), "",
                         "silence must not become the temporary transcript pathname")

    def test_spoken_input_returns_transcript_text(self):
        fixture = Path(os.environ["YANDEX_SPEECH_TEST_FIXTURE_DIR"]) / "single.wav"
        self.assertTrue(fixture.is_file())
        result = self.transcribe(fixture)
        self.assertTrue(result.get("success"), "actual command-STT execution failed")
        text = result["transcript"]
        self.assertIn("контрольный хвост", text.casefold())
        self.assertNotIn("hermes-cmd-stt-", text)
