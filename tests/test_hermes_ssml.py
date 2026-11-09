"""Explicit SSML uses a scoped plugin tool, never the lossy native text cleaner."""
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from integrations.hermes.ssml_tool import HermesSSMLTool, register_ssml_tool
from integrations.hermes.tts_provider import YandexTTSProvider
from scripts.tts import TextToSpeech


SSML = '<speak><sub alias="рэст">REST</sub>. <phoneme alphabet="ipa" ph="məlɐko">молоко</phoneme>.</speak>'


class HermesSSMLToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssml profile ")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.provider = YandexTTSProvider(
            self.home, profile={"voice": "ermil", "yandex": {"role": "good"}},
            _secret_resolver=lambda name: "synthetic" if name == "YANDEX_SPEECH_API_KEY" else None)
        self.tool = HermesSSMLTool(self.provider)

    @staticmethod
    def response(post):
        post.return_value.status_code = 200
        post.return_value.headers = {"Content-Type": "audio/ogg"}
        post.return_value.content = b"OggS-synthetic-unit-test-payload"

    def test_explicit_markup_and_preferences_reach_ssml_request(self):
        with patch("scripts.tts.requests.post") as post:
            self.response(post)
            result = json.loads(self.tool.handle({"ssml": SSML}))
        self.assertTrue(result["success"], result)
        data = post.call_args.kwargs["data"]
        self.assertNotIn("text", data)
        root = ET.fromstring(data["ssml"])
        self.assertEqual(root.find("sub").attrib["alias"], "рэст")
        self.assertEqual(root.find("phoneme").attrib["ph"], "məlɐko")
        self.assertEqual((data["voice"], data["emotion"]), ("ermil", "good"))
        self.assertEqual((result["api_version"], result["transport"], result["text_format"]),
                         ("1", "rest", "ssml"))
        self.assertEqual(result["delivery_status"], "awaiting_host_delivery")
        self.assertTrue(Path(result["file_path"]).is_file())

    def test_explicit_voice_and_role_override_saved_defaults_without_rewriting_them(self):
        config = self.home / ".config/yandex-speech/config.json"
        config.parent.mkdir(parents=True)
        original = '{"speech":{"tts":{"default_voice":"ermil","default_emotion":"good"}}}'
        config.write_text(original)
        with patch("scripts.tts.requests.post") as post:
            self.response(post)
            result = json.loads(self.tool.handle({"ssml": SSML, "voice": "marina", "role": "friendly"}))
        self.assertTrue(result["success"], result)
        self.assertEqual(post.call_args.kwargs["data"]["voice"], "marina")
        self.assertEqual(post.call_args.kwargs["data"]["emotion"], "friendly")
        self.assertEqual(config.read_text(), original)

    def test_invalid_or_unsupported_markup_never_calls_provider(self):
        cases = [
            {"ssml": "<speak>broken"},
            {"ssml": '<!DOCTYPE speak [<!ENTITY x "synthetic">]><speak>&x;</speak>'},
            {"ssml": '<speak><audio src="external"/></speak>'},
            {"ssml": "plain text"},
            {"ssml": SSML, "output_path": "/unowned/output.ogg"},
            {"ssml": SSML, "voice": "alexander"},
            {"ssml": SSML, "voice": "sergey", "model": "livetts"},
            {"ssml": SSML, "speed": True},
        ]
        with patch("scripts.tts.requests.post") as post:
            for args in cases:
                with self.subTest(args=args):
                    result = json.loads(self.tool.handle(args))
                    self.assertFalse(result["success"], result)
                    self.assertNotIn("media_tag", result)
        post.assert_not_called()

    def test_media_directive_uses_normal_final_response_voice_protocol(self):
        from gateway.platforms.base import BasePlatformAdapter
        with patch("scripts.tts.requests.post") as post:
            self.response(post)
            result = json.loads(self.tool.handle({"ssml": SSML}))
        self.assertTrue(result["success"], result)
        media, remaining = BasePlatformAdapter.extract_media(result["media_tag"])
        self.assertEqual(media, [(result["file_path"], True)])
        self.assertEqual(remaining, "")
        # Returning JSON is not a substitute for emitting its media_tag field value.
        self.assertEqual(BasePlatformAdapter.extract_media(json.dumps(result))[0], [])

    def test_concurrent_calls_keep_distinct_owned_artifacts(self):
        from types import SimpleNamespace
        texts = ["<speak>Первая реплика.</speak>", "<speak>Вторая реплика.</speak>"]
        def response(*unused, **kwargs):
            payload = kwargs["data"]["ssml"].encode()
            return SimpleNamespace(status_code=200, headers={"Content-Type": "audio/ogg"},
                                   content=b"OggS" + payload)
        with patch("scripts.tts.requests.post", side_effect=response):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda text: json.loads(self.tool.handle({"ssml": text})), texts))
        self.assertTrue(all(r["success"] for r in results), results)
        paths = [Path(r["file_path"]) for r in results]
        self.assertEqual(len(set(paths)), 2)
        for path, text in zip(paths, texts):
            self.assertTrue(path.is_relative_to(self.home) and path.is_file())
            self.assertEqual("".join(ET.fromstring(path.read_bytes()[4:]).itertext()),
                             "".join(ET.fromstring(text).itertext()))

    def test_passive_availability_makes_no_request(self):
        with patch("scripts.tts.requests.post") as post:
            self.assertTrue(self.tool.is_available())
        post.assert_not_called()

    def test_unknown_failure_does_not_expose_internal_error_or_publish_partial_audio(self):
        with patch.object(self.provider, "_client", side_effect=RuntimeError("synthetic-hidden-token")):
            result = json.loads(self.tool.handle({"ssml": SSML}))
        self.assertFalse(result["success"])
        self.assertNotIn("synthetic-hidden-token", json.dumps(result))
        self.assertNotIn("media_tag", result)

    def test_final_modified_xml_is_used_even_when_native_normalization_collides(self):
        from tools.tts_text_normalize import prepare_spoken_text
        original = "<speak>Документ /private/original.txt готов.</speak>"
        modified = "<speak>Документ /private/redacted.txt готов.</speak>"
        self.assertEqual(prepare_spoken_text(original), prepare_spoken_text(modified))
        with patch("scripts.tts.requests.post") as post:
            self.response(post)
            first = json.loads(self.tool.handle({"ssml": original}))
            second = json.loads(self.tool.handle({"ssml": modified}))
        self.assertTrue(first["success"] and second["success"])
        supplied = [call.kwargs["data"]["ssml"] for call in post.call_args_list]
        self.assertIn("/private/original.txt", supplied[0])
        self.assertIn("/private/redacted.txt", supplied[1])
        self.assertNotIn("/private/original.txt", supplied[1])

    def test_foreign_active_profile_is_rejected_before_secret_lookup(self):
        provider = YandexTTSProvider(self.home)
        tool = HermesSSMLTool(provider)
        with patch("agent.secret_scope.current_secret_scope_home", return_value=self.home / "other"):
            with patch("agent.secret_scope.get_secret_str") as lookup:
                with patch("scripts.tts.requests.post") as post:
                    result = json.loads(tool.handle({"ssml": SSML}))
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "AUTH")
        lookup.assert_not_called()
        post.assert_not_called()

    def test_symlinked_cache_does_not_escape_profile_or_call_provider(self):
        with tempfile.TemporaryDirectory() as foreign:
            (self.home / "cache").symlink_to(Path(foreign), target_is_directory=True)
            with patch("scripts.tts.requests.post") as post:
                result = json.loads(self.tool.handle({"ssml": SSML}))
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["category"], "CONFIG")
            self.assertEqual(list(Path(foreign).iterdir()), [])
            post.assert_not_called()

    def test_failed_multipart_synthesis_retains_owned_artifacts_without_delivery_tag(self):
        from types import SimpleNamespace
        response = SimpleNamespace(status_code=200, headers={"Content-Type": "audio/ogg"},
                                   content=b"OggS-synthetic-first-part")
        text = "<speak>" + "word " * 2000 + "</speak>"
        with patch("scripts.tts.requests.post", side_effect=[response, RuntimeError("synthetic-hidden-token")]):
            result = json.loads(self.tool.handle({"ssml": text}))
        self.assertFalse(result["success"])
        self.assertNotIn("media_tag", result)
        self.assertNotIn("file_paths", result)
        self.assertNotIn("synthetic-hidden-token", json.dumps(result))
        artifacts = Path(result["artifact_dir"])
        self.assertTrue(artifacts.is_relative_to(self.home))
        manifests = list(artifacts.glob(".yandex-speech-partial-*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "incomplete")

    def test_native_profile_defaults_refresh_without_rewriting_the_registered_provider(self):
        current = {"voice": "ermil", "yandex": {"role": "good"}}
        tool = HermesSSMLTool(self.provider, _profile_loader=lambda: current)
        with patch("scripts.tts.requests.post") as post:
            self.response(post)
            first = json.loads(tool.handle({"ssml": SSML}))
            current = {"voice": "marina", "yandex": {"role": "friendly"}}
            second = json.loads(tool.handle({"ssml": SSML}))
        self.assertTrue(first["success"] and second["success"])
        requests = [call.kwargs["data"] for call in post.call_args_list]
        self.assertEqual([(data["voice"], data["emotion"]) for data in requests],
                         [("ermil", "good"), ("marina", "friendly")])
        self.assertEqual(self.provider.profile["voice"], "ermil")
        self.assertEqual(self.provider.profile["yandex"]["role"], "good")

    def test_nullable_optional_arguments_preserve_saved_preferences(self):
        args = {"ssml": SSML, "voice": None, "role": None, "language": None,
                "model": None, "speed": None}
        with patch("scripts.tts.requests.post") as post:
            self.response(post)
            result = json.loads(self.tool.handle(args))
        self.assertTrue(result["success"], result)
        self.assertEqual(post.call_args.kwargs["data"]["voice"], "ermil")
        self.assertEqual(post.call_args.kwargs["data"]["emotion"], "good")

    def test_registration_is_scoped_and_preserves_builtin(self):
        from hermes_cli.plugins import PluginManager, PluginContext, PluginManifest
        from tools.registry import registry
        import tools.tts_tool
        manager = PluginManager(scope_key=str(self.home))
        ctx = PluginContext(PluginManifest(name="yandex-speech"), manager)
        original = registry.get_entry("text_to_speech", scope=str(self.home))
        register_ssml_tool(ctx, self.provider)
        added = registry.get_entry("yandex_speech_ssml", scope=str(self.home))
        self.assertIsNotNone(added)
        self.assertIs(registry.get_entry("text_to_speech", scope=str(self.home)), original)
        self.assertIsNone(registry.get_entry("yandex_speech_ssml", scope=str(self.home / "other")))
        with patch("hermes_constants.get_hermes_home", return_value=self.home):
            with patch("hermes_cli.config.load_config_readonly",
                       return_value={"tts": self.provider.profile}):
                with patch("scripts.tts.requests.post") as post:
                    self.response(post)
                    reply = ctx.dispatch_tool("yandex_speech_ssml", {"ssml": SSML})
        self.assertIsInstance(reply, str)
        self.assertTrue(json.loads(reply)["success"], reply)
        self.assertIn("ssml", post.call_args.kwargs["data"])
