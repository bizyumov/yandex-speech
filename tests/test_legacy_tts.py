import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tts import TextToSpeech, TtsError
from yandex_speech.config import RuntimeContext


class LegacyTtsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / ".config/yandex-speech/config.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"speech":{"tts":{"default_voice":"ermil",
                                                          "default_lang":"ru-RU"}}}))

    def test_service_account_does_not_need_or_transmit_folder(self):
        with patch("scripts.tts.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.headers = {"Content-Type":"audio/mpeg"}
            post.return_value.content = b"audio"
            client = TextToSpeech(api_key="synthetic", _context=RuntimeContext(user_home=self.root))
            self.assertEqual(client.synthesize_bytes("hello"), b"audio")
            self.assertNotIn("folderId",post.call_args.kwargs["data"])

    def test_file_overrides_legacy_environment_and_explicit_voice_wins(self):
        with patch.dict("os.environ", {"HERMES_TTS_VOICE":"alena"}):
            client = TextToSpeech(api_key="synthetic",_context=RuntimeContext(user_home=self.root))
            with patch("scripts.tts.requests.post") as post:
                post.return_value.status_code=200
                post.return_value.headers={"Content-Type":"audio/mpeg"}
                post.return_value.content=b"audio"
                client.synthesize_bytes("hello")
                self.assertEqual(post.call_args.kwargs["data"]["voice"],"ermil")
                client.synthesize_bytes("hello",voice="filipp")
                self.assertEqual(post.call_args.kwargs["data"]["voice"],"filipp")

    def test_profile_adapter_voice_stays_below_user_file(self):
        client=TextToSpeech(api_key="synthetic",_context=RuntimeContext(user_home=self.root),
                            _adapter_defaults={"tts":{"default_voice":"filipp"}})
        with patch("scripts.tts.requests.post") as post:
            post.return_value.status_code=200
            post.return_value.headers={"Content-Type":"audio/mpeg"}
            post.return_value.content=b"audio"
            client.synthesize_bytes("hello")
            self.assertEqual(post.call_args.kwargs["data"]["voice"],"ermil")

    def test_errors_do_not_echo_provider_payload_or_key(self):
        with patch("scripts.tts.requests.post") as post:
            post.return_value.status_code=403
            post.return_value.headers={}
            post.return_value.text="sentinel-secret-401"
            with self.assertRaises(TtsError) as cm:
                TextToSpeech(api_key="sentinel-secret-401").synthesize_bytes("hello")
            self.assertNotIn("sentinel-secret-401",str(cm.exception))

    def test_existing_output_is_preserved(self):
        output=self.root/"reply.mp3"
        output.write_bytes(b"sentinel")
        with patch.object(TextToSpeech,"synthesize_bytes",return_value=b"new"):
            client=TextToSpeech(api_key="synthetic",_context=RuntimeContext(user_home=self.root))
            with self.assertRaises(Exception):
                client.synthesize("hello",output)
        self.assertEqual(output.read_bytes(),b"sentinel")

    def test_tts_cli_cannot_replace_its_text_source_even_with_overwrite(self):
        import sys,contextlib,io
        from scripts.tts import main
        source=self.root/"text.txt"
        source.write_text("keep the addressed text")
        with patch.object(sys,"argv",["tts.py","--input-file",str(source),
                                     "--output",str(source),"--overwrite"]):
            with patch("scripts.tts.TextToSpeech") as cls:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertNotEqual(main(),0)
                cls.assert_not_called()
        self.assertEqual(source.read_text(),"keep the addressed text")
