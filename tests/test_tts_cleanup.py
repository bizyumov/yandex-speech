"""Temporary synthesis data is disposed without removing the returned final audio."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tts import TextToSpeech
from tests.test_tts_audio import encoded_tone
from tests.test_tts_selection import RUNTIME
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError


class TTSCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audio = encoded_tone(440)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_default_sdk_terminal_multipart_failure_discards_owned_parts(self):
        client = TextToSpeech(
            api_key="synthetic", _context=RuntimeContext(user_home=self.root))
        output = self.root / "failed.ogg"
        primary = SpeechError("SUBMISSION_UNCERTAIN", "synthetic middle failure")
        with patch("yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME), patch.object(
                client, "_synthesize_request", side_effect=[self.audio, primary]) as requests:
            with self.assertRaises(SpeechError) as raised:
                client.synthesize("a" * 600, output, file_method="utterance",
                    api_version="3", transport="rest", fmt="oggopus")
        self.assertIs(raised.exception, primary)
        self.assertEqual(requests.call_count, 2)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".yandex-speech-partial-*")), [])
        self.assertEqual(client.last_synthesis["status"], "incomplete")

    def provider(self, *, fail):
        from integrations.hermes.tts_provider import YandexTTSProvider
        def factory(**options):
            client = TextToSpeech(**options)
            def synthesize(text, path, **request):
                staged = Path(path)
                staged.write_bytes(b"synthetic final audio")
                if fail:
                    partial = staged.parent / ".yandex-speech-partial-owned"
                    partial.mkdir()
                    (partial / "part-000000.mp3").write_bytes(b"synthetic accepted part")
                    error = SpeechError("PROVIDER", "synthetic terminal failure")
                    error.partial_dir = partial
                    raise error
                return [staged]
            client.synthesize = synthesize
            return client
        return YandexTTSProvider(
            self.root, _client_factory=factory,
            _secret_resolver=lambda name: "synthetic" if name == "YANDEX_SPEECH_API_KEY" else None)

    def test_default_native_failure_discards_its_stage_and_nested_parts(self):
        output = self.root / "failed.mp3"
        with self.assertRaises(SpeechError) as raised:
            self.provider(fail=True).synthesize("synthetic speech", output, format="mp3")
        self.assertEqual(raised.exception.category, "PROVIDER")
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".yandex-native-tts-*")), [])
        self.assertEqual(list(self.root.rglob(".yandex-speech-partial-*")), [])

    def test_successful_native_audio_survives_staging_disposal(self):
        allocation = self.root / "reply.mp3"
        allocation.touch()
        delivered = Path(self.provider(fail=False).synthesize(
            "synthetic speech", allocation, format="mp3"))
        self.assertNotEqual(delivered, allocation)
        self.assertEqual(delivered.read_bytes(), b"synthetic final audio")
        self.assertEqual(allocation.read_bytes(), b"")
        self.assertEqual(list(self.root.glob(".yandex-native-tts-*")), [])
