import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tts import TextToSpeech
from tests.test_tts_v3 import Response
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError


class SynthesisRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = TextToSpeech(api_key="synthetic",
            _context=RuntimeContext(user_home=Path(self.tmp.name)))

    def test_current_catalog_v3_only_general_voice_selects_v3(self):
        import base64
        with patch("scripts.tts.requests.post", return_value=Response([
                {"result":{"audioChunk":{"data":base64.b64encode(b"audio").decode()}}}])) as post:
            self.client.synthesize_bytes("Тест.", voice="dasha", transport="rest")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "general")

    def test_model_and_voice_conflicts_fail_before_billing(self):
        with patch("scripts.tts.requests.post") as post:
            for kwargs in [{"voice":"sergey","api_version":"1"},
                           {"voice":"sergey","model":"general","api_version":"3"},
                           {"voice":"filipp","model":"livetts","api_version":"3"},
                           {"voice":"ermis"}]:
                with self.subTest(kwargs=kwargs), self.assertRaises(SpeechError):
                    self.client.synthesize_bytes("Тест.", **kwargs)
        post.assert_not_called()

    def test_language_default_is_used_only_when_voice_is_builtin(self):
        with patch("scripts.tts.requests.post") as post:
            post.return_value.status_code=200
            post.return_value.headers={"Content-Type":"audio/mpeg"}
            post.return_value.content=b"audio"
            self.client.synthesize_bytes("Hello.", lang="en-US")
            self.assertEqual(post.call_args.kwargs["data"]["voice"], "john")
            self.client.synthesize_bytes("Hello.", lang="en-US",voice="ermil")
            self.assertEqual(post.call_args.kwargs["data"]["voice"],"ermil")
            self.assertEqual(post.call_args.kwargs["data"]["lang"],"en-US")

    def test_v1_form_body_budget_is_checked_before_network(self):
        with patch("scripts.tts.requests.post") as post:
            with self.assertRaises(SpeechError) as cm:
                self.client.synthesize_bytes("я"*4999, api_version="1")
        self.assertEqual(cm.exception.category,"LIMIT")
        post.assert_not_called()

    def test_v1_partial_pcm_sample_cannot_be_published(self):
        with patch("scripts.tts.requests.post") as post:
            post.return_value.status_code=200
            post.return_value.headers={"Content-Type":"audio/L16"}
            post.return_value.content=b"\0"
            with self.assertRaises(SpeechError) as cm:
                self.client.synthesize("Тест.",Path(self.tmp.name)/"out.pcm",
                    api_version="1",fmt="lpcm")
        self.assertEqual(cm.exception.category,"OUTPUT_PARTIAL")
        self.assertFalse((Path(self.tmp.name)/"out.pcm").exists())
