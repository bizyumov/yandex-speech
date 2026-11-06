import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.transcription_provider import TranscriptionProvider, TranscriptionStreamSession
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tests.test_streaming import FakeStreamingRpc
from yandex_speech.transcription import SpeechTranscriber
from yandex_speech.errors import SpeechError


class HermesBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)

    def provider(self,**kwargs):
        from integrations.hermes.provider import YandexTranscriptionProvider
        return YandexTranscriptionProvider(self.home,_secret_resolver=lambda:"synthetic",**kwargs)

    def test_real_registration_and_spi_types(self):
        from integrations.hermes import register
        from agent.transcription_registry import get_provider
        manager=PluginManager(scope_key=str(self.home))
        ctx=PluginContext(PluginManifest(name="yandex-speech"),manager)
        with patch("hermes_cli.config.load_config_readonly",return_value={"stt":{}}):
            register(ctx)
        provider=get_provider("yandex",scope=str(self.home))
        self.assertIsInstance(provider,TranscriptionProvider)
        self.assertEqual(provider.workspace,self.home)

    def test_completed_stream_is_native_envelope_with_preview_separate(self):
        provider=self.provider(_client_factory=lambda **kw:SpeechTranscriber(_transport=FakeStreamingRpc(),**kw))
        self.assertTrue(provider.streaming_capable)
        session=provider.open_stream_session(prompt="ignored host hint")
        self.assertIsInstance(session,TranscriptionStreamSession)
        session.push_audio(b"\0"*3200)
        result=session.finalize()
        self.assertTrue(result["success"],result)
        self.assertIn("Контрольный хвост",result["transcript"])
        self.assertEqual(session.partial_transcript(),"")
        self.assertEqual(session.finalize(),result)

    def test_failed_stream_finalization_is_nonraising_error_envelope(self):
        provider=self.provider(_client_factory=lambda **kw:SpeechTranscriber(_transport=FakeStreamingRpc(),**kw))
        session=provider.open_stream_session()
        session.push_audio(b"\0")
        result=session.finalize()
        self.assertFalse(result["success"])
        self.assertEqual(result["transcript"],"")

    def test_host_finalization_does_not_raise_when_cleanup_itself_fails(self):
        from integrations.hermes.provider import YandexStreamSession
        from unittest.mock import Mock
        from yandex_speech.errors import SpeechError
        core=Mock()
        core.finalize.side_effect=SpeechError("PROVIDER","synthetic provider error")
        core.cancel.side_effect=SpeechError("STATE","synthetic cleanup error")
        result=YandexStreamSession(core).finalize()
        self.assertFalse(result["success"])
        self.assertEqual(result["transcript"],"")

    def test_rest_profile_preserves_file_availability_without_streaming(self):
        provider=self.provider(profile={"transport":"rest"})
        self.assertTrue(provider.is_available())
        self.assertFalse(provider.streaming_capable)
        self.assertIn("gRPC",provider.streaming_unavailable_reason)

    def test_user_file_remains_above_profile_and_host_language_defaults(self):
        path=self.home/".config/yandex-speech/config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"speech":{"language_code":"en-US"}}))
        provider=self.provider(profile={"language":"ru-RU"})
        client=provider._client(language="de-DE")
        self.assertEqual(client.settings.value("language_code"),"en-US")

    def test_file_provider_converts_errors_to_safe_envelope(self):
        result=self.provider().transcribe("/no/such/addressed/audio.wav",unknown_host_key="ignored")
        self.assertFalse(result["success"])
        self.assertEqual(result["transcript"],"")
        self.assertEqual(result["provider"],"yandex")

class HermesTTSBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.clients = []

    def provider(self, profile=None, *, fake_output=False):
        from integrations.hermes.tts_provider import YandexTTSProvider
        from yandex_speech import TextToSpeech
        def factory(**kwargs):
            client = TextToSpeech(**kwargs)
            self.clients.append(client)
            if fake_output:
                def synthesize(text, path, **options):
                    client.recorded = (text, options)
                    Path(path).write_bytes(b"synthetic adapter payload")
                    return [Path(path)]
                client.synthesize = synthesize
                client.stream_audio = lambda text, **options: iter([b"\0\0"])
            return client
        return YandexTTSProvider(self.home, profile=profile,
            _secret_resolver=lambda name: "synthetic" if name == "YANDEX_SPEECH_API_KEY" else None,
            _client_factory=factory)

    def test_real_plugin_registers_both_native_provider_types(self):
        from integrations.hermes import register
        from agent.tts_provider import TTSProvider
        from agent.tts_registry import get_provider
        manager = PluginManager(scope_key=str(self.home))
        ctx = PluginContext(PluginManifest(name="yandex-speech"), manager)
        with patch("hermes_cli.config.load_config_readonly", return_value={"stt": {}, "tts": {}}):
            register(ctx)
        provider = get_provider("yandex", scope=str(self.home))
        self.assertIsInstance(provider, TTSProvider)
        self.assertEqual(provider.workspace, self.home)
        self.assertTrue(provider.voice_compatible)

    def test_host_hints_and_role_remain_below_user_preferences(self):
        provider = self.provider({"voice": "ermil", "yandex": {"role": "good"}})
        self.assertEqual(provider._client()._options()[1]["_role"], "good")
        path = self.home / ".config/yandex-speech/config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"speech": {"tts": {
            "default_voice": "marina", "default_emotion": "friendly"}}}))
        settings, options, _ = provider._client(voice="ermil", model="general", speed=1.1)._options()
        self.assertEqual((options["default_voice"], options["_role"]), ("marina", "friendly"))
        self.assertEqual(settings.sources["tts.default_voice"], "config")
        self.assertEqual(settings.sources["tts.default_emotion"], "config")

    def test_foreign_ambient_defaults_and_credentials_are_not_read(self):
        with patch.dict(os.environ, {"YANDEX_SPEECH_API_KEY": "foreign-synthetic",
                                    "HERMES_TTS_VOICE": "ermil",
                                    "HERMES_TTS_LANG": "de-DE"}):
            client = self.provider()._client()
            self.assertEqual(client.api_key, "synthetic")
            self.assertEqual(client._options()[1]["default_voice"], "alena")
            self.assertEqual(client._options()[1]["default_lang"], "ru-RU")

    def test_output_extension_and_existing_empty_allocation(self):
        output = self.home / "speech.mp3"
        output.touch()
        provider = self.provider(fake_output=True)
        result = provider.synthesize("Текст.", str(output), format="mp3", ignored_host_field=True)
        self.assertNotEqual(result, str(output))
        self.assertEqual(output.read_bytes(), b"")
        self.assertEqual(Path(result).read_bytes(), b"synthetic adapter payload")
        other = provider.synthesize("Текст.", str(self.home / "unsupported.flac"), format="flac")
        self.assertTrue(other.endswith(".wav"))
        self.assertEqual(self.clients[-1].recorded[1]["format"], "wav")

    def test_nonempty_outputs_and_symlinks_are_preserved_before_requests(self):
        output = self.home / "speech.mp3"
        output.write_bytes(b"original")
        provider = self.provider(fake_output=True)
        with self.assertRaises(SpeechError):
            provider.synthesize("Текст.", str(output))
        link = self.home / "link.mp3"
        link.symlink_to(output)
        with self.assertRaises(SpeechError):
            provider.synthesize("Текст.", str(link))
        self.assertEqual(output.read_bytes(), b"original")
        self.assertEqual(self.clients, [])

    def test_an_allocation_populated_during_synthesis_is_not_overwritten(self):
        output = self.home / "speech.mp3"
        output.touch()
        provider = self.provider(fake_output=True)
        original = provider._client_factory
        def factory(**kwargs):
            client = original(**kwargs)
            synthesize = client.synthesize
            def race(text, path, **options):
                output.write_bytes(b"concurrent owner")
                return synthesize(text, path, **options)
            client.synthesize = race
            return client
        provider._client_factory = factory
        with self.assertRaises(SpeechError):
            provider.synthesize("Текст.", str(output))
        self.assertEqual(output.read_bytes(), b"concurrent owner")

    def test_base_rest_availability_and_pcm_eligibility_are_distinct(self):
        from yandex_speech.errors import SpeechError
        with patch("yandex_speech.tts_v3.grpc_sdk",
                   side_effect=SpeechError("PREREQUISITE", "synthetic missing SDK")):
            rest = self.provider({"voice": "ermil", "yandex": {"transport": "rest", "api_version": "1"}})
            self.assertTrue(rest.is_available())
            self.assertFalse(rest.streams_pcm)
            grpc = self.provider({"voice": "ermil", "yandex": {"transport": "grpc", "api_version": "3"}})
            self.assertFalse(grpc.is_available())
            self.assertFalse(grpc.streams_pcm)

    def test_pcm_client_is_created_before_iteration_and_is_not_shared(self):
        provider = self.provider({"voice": "ermil"}, fake_output=True)
        first = provider.stream("Первая.", format="pcm")
        second = provider.stream("Вторая.", format="pcm")
        self.assertEqual(len(self.clients), 2)
        self.assertIsNot(self.clients[0], self.clients[1])
        self.assertEqual(b"".join(first), b"\0\0")
        self.assertEqual(b"".join(second), b"\0\0")
        self.assertEqual(provider.stream_sample_rate, 24000)

    def test_concurrent_sentence_calls_use_distinct_core_clients(self):
        from concurrent.futures import ThreadPoolExecutor
        provider = self.provider(fake_output=True)
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(
                lambda index: provider.synthesize("Фраза.", str(self.home / f"sentence-{index}.mp3")),
                range(6)))
        self.assertEqual(len(set(results)), 6)
        self.assertEqual(len({id(client) for client in self.clients}), 6)
        self.assertTrue(all(Path(result).read_bytes() == b"synthetic adapter payload" for result in results))

    def test_a_foreign_active_secret_scope_is_rejected_before_secret_lookup(self):
        from integrations.hermes.provider import profile_secret
        with patch("agent.secret_scope.current_secret_scope_home", return_value=self.home / "other"):
            with patch("agent.secret_scope.get_secret_str") as lookup:
                with self.assertRaises(SpeechError) as error:
                    profile_secret(self.home, "YANDEX_SPEECH_API_KEY")
                self.assertEqual(error.exception.category, "AUTH")
                lookup.assert_not_called()

    def test_incompatible_livetts_controls_do_not_advertise_availability(self):
        provider = self.provider({"voice": "sergey", "model": "livetts", "speed": 1.5})
        self.assertFalse(provider.is_available())
        self.assertFalse(provider.streams_pcm)

    def test_default_model_is_the_effective_catalog_model(self):
        for voice, model in [("ermil", "general"), ("sergey", "livetts")]:
            provider = self.provider({"voice": voice})
            self.assertEqual(provider.default_model(), model)
            self.assertIn(model, {item["id"] for item in provider.list_models()})

    def test_a_late_writer_after_the_final_guard_is_never_overwritten(self):
        from yandex_speech import artifacts
        output = self.home / "speech.mp3"
        output.touch()
        provider = self.provider(fake_output=True)
        publish = artifacts.atomic_write
        def late_write(path, data, **kwargs):
            output.write_bytes(b"late concurrent owner")
            return publish(path, data, **kwargs)
        with patch("yandex_speech.artifacts.atomic_write", side_effect=late_write):
            result = Path(provider.synthesize("Текст.", str(output)))
        self.assertEqual(output.read_bytes(), b"late concurrent owner")
        self.assertNotEqual(result, output)
        self.assertEqual(result.suffix, ".mp3")
        self.assertEqual(result.read_bytes(), b"synthetic adapter payload")

    def test_two_calls_sharing_an_empty_allocation_return_distinct_owned_outputs(self):
        from concurrent.futures import ThreadPoolExecutor
        output = self.home / "speech.mp3"
        output.touch()
        provider = self.provider(fake_output=True)
        barrier = __import__("threading").Barrier(2)
        factory = provider._client_factory
        def paired_factory(**kwargs):
            client = factory(**kwargs)
            synthesize = client.synthesize
            def paired(text, path, **options):
                barrier.wait(timeout=2)
                return synthesize(text, path, **options)
            client.synthesize = paired
            return client
        provider._client_factory = paired_factory
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda text: Path(provider.synthesize(text, str(output))),
                                        ["Первая.", "Вторая."]))
        self.assertEqual(len(set(results)), 2)
        self.assertEqual(output.read_bytes(), b"")
        self.assertTrue(all(path.read_bytes() == b"synthetic adapter payload" for path in results))

    def test_compatible_general_and_livetts_pcm_are_advertised(self):
        for voice in ("ermil", "sergey"):
            with self.subTest(voice=voice):
                provider = self.provider({"voice": voice})
                self.assertTrue(provider.is_available())
                self.assertTrue(provider.streams_pcm)
