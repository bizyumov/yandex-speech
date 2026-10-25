import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from scripts.tts import TextToSpeech
from tests.test_tts_v3 import Response
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.tts_text import v1_fits,v1_form


class SynthesisBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client=TextToSpeech(api_key="synthetic",
            _context=RuntimeContext(user_home=Path(self.tmp.name)))

    def test_249_250_251_codepoint_safe_utterances(self):
        for length in [249,250]:
            with patch("scripts.tts.requests.post",return_value=Response([
                    {"result":{"audioChunk":{"data":base64.b64encode(b"audio").decode()}}}])):
                self.assertEqual(self.client.synthesize_bytes("я"*length,api_version="3",
                    transport="rest",model="livetts",voice="sergey"),b"audio")
        with patch("scripts.tts.requests.post") as post:
            with self.assertRaises(SpeechError):
                self.client.synthesize_bytes("я"*251,api_version="3",
                    transport="rest",model="livetts",voice="sergey")
            post.assert_not_called()

    def test_v1_4999_5000_5001_and_exact_encoded_body(self):
        for length in [4999,5000]:
            with patch("scripts.tts.requests.post") as post:
                post.return_value.status_code=200
                post.return_value.headers={"Content-Type":"audio/mpeg"}
                post.return_value.content=b"audio"
                self.client.synthesize_bytes("a"*length,api_version="1")
        with patch("scripts.tts.requests.post") as post:
            with self.assertRaises(SpeechError):
                self.client.synthesize_bytes("a"*5001,api_version="1")
            post.assert_not_called()
        _,options,_=self.client._options(api_version="1")
        overhead=len(urlencode(v1_form("",options)).encode())
        target=15000-overhead
        text="!"*(target//3)+"a"*(target%3)
        self.assertLessEqual(len(text),5000)
        self.assertEqual(len(urlencode(v1_form(text,options)).encode()),15000)
        self.assertTrue(v1_fits(text,options))
        self.assertFalse(v1_fits(text+"a",options))

    def test_unsupported_nondefault_livetts_controls_are_not_dropped(self):
        for controls in [{"speed":2},{"pitch_shift":100},{"duration_ms":1000}]:
            with self.subTest(controls=controls),patch("scripts.tts.requests.post") as post:
                with self.assertRaises(SpeechError):
                    self.client.synthesize_bytes("Тест.",api_version="3",transport="rest",
                        model="livetts",voice="sergey",**controls)
                post.assert_not_called()

    def test_native_stream_factory_preserves_fixed_version_transport_and_format(self):
        for options in [{"api_version":"1"},{"transport":"rest"},{"text_format":"ssml"},
                        {"fmt":"mp3"},{"sample_rate":16000}]:
            with self.subTest(options=options),self.assertRaises(SpeechError):
                self.client.stream_audio("Тест.",voice="sergey",model="livetts",**options)

    def test_format_alias_and_method_specific_sample_rate_are_preflighted(self):
        with patch("scripts.tts.requests.post") as post:
            for options in [{"fmt":"mp3","format":"wav"},
                            {"api_version":"3","transport":"rest","voice":"sergey",
                             "fmt":"mp3","sample_rate":16000}]:
                with self.subTest(options=options),self.assertRaises(SpeechError):
                    self.client.synthesize_bytes("Тест.",**options)
            post.assert_not_called()
