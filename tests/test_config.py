import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yandex_speech.config import RuntimeContext, resolve_settings
from yandex_speech.errors import SpeechError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.context = RuntimeContext(user_home=self.home)
        self.config = self.home / ".config/yandex-speech/config.json"

    def put(self, data, path=None):
        path = path or self.config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_defaults_are_effective_and_nonsecret(self):
        settings = resolve_settings(_context=self.context, _environment={})
        self.assertTrue(settings.value("literature_text"))
        self.assertFalse(settings.value("profanity_filter"))
        self.assertEqual(settings.value("api_version"), "auto")
        self.assertEqual(settings.path, self.config)

    def test_call_file_adapter_builtin_precedence(self):
        self.put({"speech": {"language_code": "en-US",
                            "tts": {"default_voice": "configured"},
                            "object_storage": {"bucket": "configured"}}})
        env = {"HERMES_TTS_VOICE": "legacy", "YANDEX_SPEECH_S3_BUCKET": "legacy"}
        settings = resolve_settings(_context=self.context, _environment=env,
                                    _adapter={"language_code": "ru-RU",
                                              "tts": {"default_voice": "profile"}})
        self.assertEqual(settings.value("tts.default_voice"), "configured")
        self.assertEqual(settings.value("object_storage.bucket"), "configured")
        self.assertEqual(settings.value("language_code"), "en-US")
        self.assertEqual(settings.sources["tts.default_voice"], "config")
        explicit = resolve_settings(_context=self.context, _environment=env,
                                    _explicit={"literature_text": False,
                                               "tts": {"default_voice": "call"}})
        self.assertFalse(explicit.value("literature_text"))
        self.assertEqual(explicit.value("tts.default_voice"), "call")

    def test_only_explicit_cli_file_selects_another_path(self):
        self.put({"speech": {"language_code": "ru-RU"}})
        other = self.put({"speech": {"language_code": "en-US"}}, self.home / "other.json")
        env = {"YANDEX_SPEECH_CONFIG": str(other), "XDG_CONFIG_HOME": str(self.home / "xdg")}
        self.assertEqual(resolve_settings(_context=self.context, _environment=env).value("language_code"), "ru-RU")
        selected = RuntimeContext(user_home=self.home, selected_file=other)
        self.assertEqual(resolve_settings(_context=selected, _environment=env).value("language_code"), "en-US")

    def test_workspace_scope_isolation_does_not_mutate_home(self):
        a, b = self.home / "a", self.home / "b"
        self.put({"speech": {"language_code": "en-US"}}, a / ".config/yandex-speech/config.json")
        self.put({"speech": {"language_code": "kk-KZ"}}, b / ".config/yandex-speech/config.json")
        original = os.environ.get("HOME")
        self.assertEqual(resolve_settings(_context=RuntimeContext(workspace=a), _environment={}).value("language_code"), "en-US")
        self.assertEqual(resolve_settings(_context=RuntimeContext(workspace=b), _environment={}).value("language_code"), "kk-KZ")
        self.assertEqual(os.environ.get("HOME"), original)

    def test_missing_explicit_invalid_and_unknown_files_fail(self):
        with self.assertRaises(SpeechError) as cm:
            resolve_settings(_context=RuntimeContext(selected_file=self.home / "absent"), _environment={})
        self.assertEqual(cm.exception.category, "CONFIG")
        for data in [{"speech": {"api_key": "not-a-secret"}}, {"speech": {"literature_text": 1}},
                     {"speech": {"api_version": 3}}, {"speech": {"profanity_filter": True}},
                     {"speech": {"tts": {"default_speed": float("nan")}}}]:
            with self.subTest(data=data):
                self.put(data)
                with self.assertRaises(SpeechError):
                    resolve_settings(_context=self.context, _environment={})

    def test_profile_defaults_precede_environment_only_within_adapter_layer(self):
        settings = resolve_settings(_context=self.context,
                                    _environment={"HERMES_TTS_LANG": "en-US"},
                                    _adapter={"tts": {"default_lang": "kk-KZ"}})
        self.assertEqual(settings.value("tts.default_lang"), "kk-KZ")
        self.assertEqual(settings.sources["tts.default_lang"], "adapter")

    def test_forbidden_secret_does_not_appear_in_error(self):
        self.put({"speech": {"api_key": "synthetic-sensitive-sentinel"}})
        with self.assertRaises(SpeechError) as cm:
            resolve_settings(_context=self.context, _environment={})
        self.assertNotIn("synthetic-sensitive-sentinel", str(cm.exception))
