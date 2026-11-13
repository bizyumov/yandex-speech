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

    def test_v1_exact_complete_form_matrix_preserves_cyrillic_markup_and_fields(self):
        from types import SimpleNamespace
        for fmt in ("oggopus", "lpcm", "wav"):
            for text_format in ("plain", "ssml"):
                kwargs = dict(api_version="1", transport="rest", voice="ermil", model="general",
                    role="good", lang="ru-RU", speed=1.25, fmt=fmt, text_format=text_format)
                if fmt in {"lpcm", "wav"}:
                    kwargs["sample_rate"] = 16000
                _, options, _ = self.client._options(**kwargs)
                prefix, suffix = (('Привет & + "<>! ', '') if text_format == "plain" else
                    ('<speak><p><sub alias="слово &amp; ещё">метка</sub></p><p>Привет &amp; ', '</p></speak>'))
                base = len(urlencode(v1_form(prefix + suffix, options)).encode())
                for size in (14999, 15000, 15001):
                    remaining = size - base
                    text = prefix + "я" * (remaining // 6) + "a" * (remaining % 6) + suffix
                    with self.subTest(format=fmt, text_format=text_format, bytes=size):
                        self.assertLess(len(text), 5000)
                        payload = v1_form(text, options)
                        self.assertEqual(len(urlencode(payload).encode()), size)
                        expected = {"lang", "voice", "format", "speed", "emotion",
                                    "ssml" if text_format == "ssml" else "text"}
                        if fmt in {"lpcm", "wav"}:
                            expected.add("sampleRateHertz")
                        self.assertEqual(set(payload), expected)
                        self.assertEqual(v1_fits(text, options), size <= 15000)
                        with patch("scripts.tts.requests.post", return_value=SimpleNamespace(
                                status_code=200, headers={"Content-Type": "audio/L16"},
                                content=b"\0\0" * 16)) as post:
                            if size <= 15000:
                                self.client.synthesize_bytes(text, **kwargs)
                                self.assertEqual(post.call_args.kwargs["data"], payload)
                            else:
                                with self.assertRaises(SpeechError):
                                    self.client.synthesize_bytes(text, **kwargs)
                                post.assert_not_called()

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

    def test_stream_dependency_absence_has_a_project_error_after_option_validation(self):
        import builtins
        original_import = builtins.__import__

        def without_sdk(name, *args, **kwargs):
            if name.split(".")[0] in {"google", "grpc", "yandex"}:
                raise ModuleNotFoundError("optional SDK unavailable", name=name)
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_sdk):
            for options, category in [
                ({}, "PREREQUISITE"),
                ({"api_version": "1"}, "CONFIG"),
                ({"transport": "rest"}, "CONFIG"),
            ]:
                with self.subTest(options=options):
                    with self.assertRaises(SpeechError) as failure:
                        self.client.stream_audio("Тест.", voice="sergey", model="livetts", **options)
                    self.assertEqual(failure.exception.category, category)
