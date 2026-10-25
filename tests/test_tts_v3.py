import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tts import TextToSpeech
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError


class Response:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, records):
        self.records = records

    def iter_lines(self):
        for record in self.records:
            yield json.dumps(record).encode()

    def close(self):
        pass


class V3SynthesisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = TextToSpeech(api_key="synthetic",
            _context=RuntimeContext(user_home=Path(self.tmp.name)))

    def options(self, **extra):
        return dict(api_version="3", transport="rest", model="livetts",
                    voice="sergey", fmt="mp3", **extra)

    def test_livetts_requests_final_mp3_directly_and_preserves_voice(self):
        records = [
            {"result": {"audioChunk": None, "textChunk": {"text": "Тест"}}},
            {"result": {"audioChunk": {"data": base64.b64encode(b"audio").decode()}}},
        ]
        with patch("scripts.tts.requests.post", return_value=Response(records)) as post:
            data = self.client.synthesize_bytes("Тест.", **self.options())
        self.assertEqual(data, b"audio")
        self.assertTrue(post.call_args.args[0].endswith("/tts/v3/utteranceSynthesis"))
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["model"], "livetts")
        self.assertEqual(request["hints"], [{"voice": "sergey"}])
        self.assertEqual(request["outputAudioSpec"]["containerAudio"]["containerAudioType"], "MP3")
        self.assertFalse(request["unsafeMode"])
        self.assertNotIn("folderId", request)
        self.assertNotIn("x-folder-id", post.call_args.kwargs["headers"])
        self.assertEqual(self.client.last_synthesis["transport"], "rest")
        self.assertEqual(self.client.last_synthesis["model"], "livetts")

    def test_all_records_are_drained_in_order(self):
        records = [{"result": {"audioChunk": {"data": base64.b64encode(x).decode()}}}
                   for x in [b"first", b"second", b"tail"]]
        with patch("scripts.tts.requests.post", return_value=Response(records)):
            data = self.client.synthesize_bytes("Тест.", **self.options())
        self.assertEqual(data, b"firstsecondtail")

    def test_embedded_error_after_audio_cannot_return_complete_prefix(self):
        records = [
            {"result": {"audioChunk": {"data": base64.b64encode(b"prefix").decode()}}},
            {"error": {"code": 13, "message": "sensitive provider payload"}},
        ]
        with patch("scripts.tts.requests.post", return_value=Response(records)):
            with self.assertRaises(SpeechError) as cm:
                self.client.synthesize_bytes("Тест.", **self.options())
        self.assertEqual(cm.exception.category, "OUTPUT_PARTIAL")
        self.assertNotIn("sensitive provider", str(cm.exception))

    def test_safe_request_above_250_characters_is_rejected_before_network(self):
        with patch("scripts.tts.requests.post") as post:
            with self.assertRaises(SpeechError) as cm:
                self.client.synthesize_bytes("я" * 251, **self.options())
        self.assertEqual(cm.exception.category, "LIMIT")
        post.assert_not_called()

    def test_livetts_incompatible_controls_fail_before_network(self):
        with patch("scripts.tts.requests.post") as post:
            with self.assertRaises(SpeechError):
                self.client.synthesize_bytes("Тест.", **self.options(speed=2))
            with self.assertRaises(SpeechError):
                self.client.synthesize_bytes("Тест.", **self.options(role="friendly"))
        post.assert_not_called()
