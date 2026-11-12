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

    def test_native_storage_defaults_and_separate_keys_use_only_own_profile_scope(self):
        from integrations.hermes.provider import YandexTranscriptionProvider
        own = {"YANDEX_SPEECH_API_KEY": "own-speech", "YANDEX_SPEECH_S3_BUCKET": "own-bucket",
               "YANDEX_SPEECH_AWS_ACCESS_KEY_ID": "own-storage-id",
               "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY": "own-storage-secret"}
        foreign = {"YANDEX_SPEECH_S3_BUCKET": "foreign-bucket",
                   "YANDEX_SPEECH_AWS_ACCESS_KEY_ID": "foreign-id",
                   "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY": "foreign-secret"}
        with patch.dict(os.environ, foreign):
            with patch("integrations.hermes.provider.profile_secret", side_effect=lambda workspace, name: own.get(name)):
                client = YandexTranscriptionProvider(self.home)._client()
        self.assertEqual(client.api_key, "own-speech")
        self.assertEqual(client.settings.value("object_storage.bucket"), "own-bucket")
        self.assertEqual(client._environment["YANDEX_SPEECH_AWS_ACCESS_KEY_ID"], "own-storage-id")
        self.assertNotIn("foreign-secret", json.dumps(client._environment))

    def test_custom_speech_resolver_never_inherits_ambient_storage_defaults(self):
        with patch.dict(os.environ, {"YANDEX_SPEECH_S3_BUCKET": "foreign-bucket",
                                    "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY": "foreign-secret"}):
            client = self.provider()._client()
        self.assertIsNone(client.settings.value("object_storage.bucket"))
        self.assertNotIn("foreign-secret", json.dumps(client._environment))

    def test_native_explicit_rest_v3_file_uses_the_new_recording_core(self):
        import wave
        from tests.test_rest_stt import Response, Session
        from tests.test_rest_recording import v3_wire
        from yandex_speech.rest_stt import RestTransport
        source = self.home / "source.wav"
        with wave.open(str(source), "wb") as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(bytes(32000))
        session = Session(Response({"id": "native-rest"}), Response({"done": True}), Response(v3_wire()))
        def factory(**options):
            return SpeechTranscriber(**options, job_dir=self.home / "jobs",
                _transport_factory=lambda version, transport: RestTransport("synthetic", _session=session))
        provider = self.provider(profile={"transport": "rest", "api_version": "3"}, _client_factory=factory)
        with patch("yandex_speech.transcription.grpc_sdk", side_effect=AssertionError("gRPC was used")):
            result = provider.transcribe(source)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["speech_result"]["transport"], "rest")
        self.assertEqual(result["transcript"], "Хвост.")

    def test_native_file_availability_honors_strict_transport_and_version(self):
        for version, transport, expected in (("3", "grpc", False), ("3", "rest", True),
                                             ("auto", "auto", True), ("2", "grpc", False),
                                             ("2", "rest", False), ("1", "grpc", False)):
            with self.subTest(version=version, transport=transport):
                provider = self.provider(profile={"api_version": version, "transport": transport})
                with patch("yandex_speech.transcription.grpc_sdk",
                           side_effect=SpeechError("PREREQUISITE", "missing optional SDK")):
                    self.assertEqual(provider.is_available(), expected)

    def test_preferred_storage_without_keys_preserves_native_inline_file_availability(self):
        config = self.home / ".config/yandex-speech/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"speech":{"transport":"rest","object_storage":{"bucket":"preferred-private"}}}')
        self.assertTrue(self.provider().is_available())
        self.assertFalse(self.provider().streaming_capable)

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

    def test_live_push_failure_is_immediate_and_keeps_primary_cause(self):
        from integrations.hermes.provider import YandexStreamSession
        from unittest.mock import Mock
        from yandex_speech.errors import SpeechError
        core = Mock()
        failure = SpeechError("RATE_LIMIT", "bounded stream queue is full", code="INPUT_QUEUE_FULL")
        core.push_audio.side_effect = failure
        core.cancel.side_effect = RuntimeError("private-token-fixture")
        session = YandexStreamSession(core)
        with self.assertRaises(SpeechError) as caught:
            session.push_audio(bytes(640))
        self.assertIs(caught.exception, failure)
        with self.assertRaises(SpeechError) as repeated:
            session.push_audio(bytes(640))
        self.assertIs(repeated.exception, failure)
        core.push_audio.assert_called_once()
        session.cancel()
        result = session.finalize()
        self.assertFalse(result["success"])
        self.assertIn("bounded stream queue is full", result["error"])
        self.assertEqual(result["stream_failure"]["category"], "RATE_LIMIT")
        self.assertEqual(result["stream_failure"]["code"], "INPUT_QUEUE_FULL")
        self.assertTrue(result["stream_failure"]["cleanup_failed"])
        self.assertNotIn("private-token-fixture", str(result))
        self.assertIs(session.finalize(), result)
        core.finalize.assert_not_called()

    def test_async_stream_failure_surfaces_during_preview_before_source_eof(self):
        from integrations.hermes.provider import YandexStreamSession
        from unittest.mock import Mock
        from yandex_speech.errors import SpeechError
        core = Mock()
        failure = SpeechError("PROVIDER", "stream transport failed", code="UNAVAILABLE")
        core.partial_transcript.side_effect = failure
        session = YandexStreamSession(core)
        session.push_audio(bytes(640))
        with self.assertRaises(SpeechError) as caught:
            session.partial_transcript()
        self.assertIs(caught.exception, failure)
        result = session.finalize()
        self.assertFalse(result["success"])
        self.assertEqual(result["stream_failure"]["phase"], "preview")
        self.assertEqual(result["stream_failure"]["code"], "UNAVAILABLE")
        core.finalize.assert_not_called()

    def test_unknown_stream_exception_is_safe_in_live_error_log_and_envelope(self):
        import logging
        from integrations.hermes.provider import YandexStreamSession
        from unittest.mock import Mock
        from yandex_speech.errors import SpeechError
        core = Mock()
        core.push_audio.side_effect = RuntimeError("private-token-fixture")
        session = YandexStreamSession(core)
        with self.assertLogs("integrations.hermes.provider", level=logging.WARNING) as logs:
            with self.assertRaises(SpeechError) as caught:
                session.push_audio(bytes(640))
        result = session.finalize()
        self.assertNotIn("private-token-fixture", str(caught.exception))
        self.assertNotIn("private-token-fixture", str(result))
        self.assertNotIn("private-token-fixture", str(logs.output))
        self.assertEqual(result["stream_failure"]["category"], "PROVIDER")

    def test_healthy_source_end_finalizes_same_core_without_cancellation(self):
        from integrations.hermes.provider import YandexStreamSession
        from unittest.mock import Mock
        from yandex_speech.errors import SpeechError
        core = Mock()
        core.finalize.return_value = {"input_complete": True, "text": "complete control tail"}
        session = YandexStreamSession(core)
        session.push_audio(bytes(640))
        result = session.finalize()
        self.assertTrue(result["success"])
        self.assertEqual(result["transcript"], "complete control tail")
        self.assertIs(session.finalize(), result)
        core.finalize.assert_called_once()
        core.cancel.assert_not_called()
        with self.assertRaises(SpeechError):
            session.push_audio(bytes(640))
        core.push_audio.assert_called_once()

    def test_cancel_racing_native_finalize_prevents_success_commit(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from integrations.hermes.provider import YandexStreamSession
        from unittest.mock import Mock
        entered, release = threading.Event(), threading.Event()
        core = Mock()
        def completing():
            entered.set()
            if not release.wait(2):
                raise AssertionError("cancel did not release finalization")
            return {"input_complete": True, "text": "late success"}
        core.finalize.side_effect = completing
        core.cancel.side_effect = release.set
        session = YandexStreamSession(core)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(session.finalize)
            self.assertTrue(entered.wait(2))
            session.cancel()
            result = future.result(2)
        self.assertFalse(result["success"])
        self.assertEqual(result["stream_failure"]["category"], "CANCELLED")

    def test_real_core_cleanup_failure_preserves_provider_cause_in_all_live_paths(self):
        from integrations.hermes.provider import YandexStreamSession
        from tests.test_streaming import FakeStreamingRpc, native
        from unittest.mock import Mock
        from yandex_speech.errors import SpeechError
        from yandex_speech.streaming import TranscriptionSession
        rpc = FakeStreamingRpc()
        original_close = rpc.close
        rpc.close = Mock(side_effect=RuntimeError("private-cleanup-fixture"))
        core = TranscriptionSession(rpc, audio_input=native())
        primary = SpeechError("PROVIDER", "safe provider failure", code="UNAVAILABLE")
        try:
            core._fail(primary)
            for worker in list(core.workers):
                worker.join(2)
                self.assertFalse(worker.is_alive())
            result = YandexStreamSession(core).finalize()
            self.assertEqual(result["stream_failure"]["category"], "PROVIDER")
            self.assertEqual(result["stream_failure"]["code"], "UNAVAILABLE")
            self.assertTrue(result["stream_failure"]["cleanup_failed"])
            self.assertNotIn("private-cleanup-fixture", str(result))
            for operation in ("push_audio", "partial_transcript"):
                session = YandexStreamSession(core)
                with self.subTest(operation=operation), self.assertRaises(SpeechError) as caught:
                    if operation == "push_audio":
                        session.push_audio(bytes(640))
                    else:
                        session.partial_transcript()
                self.assertIs(caught.exception, primary)
                self.assertEqual(session.finalize()["stream_failure"]["code"], "UNAVAILABLE")
        finally:
            rpc.close = original_close
            core.cancel()

    def test_later_cancel_does_not_hide_unobserved_core_provider_failure(self):
        from integrations.hermes.provider import YandexStreamSession
        from tests.test_streaming import FakeStreamingRpc, native
        from yandex_speech.errors import SpeechError
        from yandex_speech.streaming import TranscriptionSession
        core = TranscriptionSession(FakeStreamingRpc(), audio_input=native())
        self.addCleanup(core.cancel)
        primary = SpeechError("PROVIDER", "safe provider failure", code="UNAVAILABLE")
        core._fail(primary)
        for worker in list(core.workers):
            worker.join(2)
            self.assertFalse(worker.is_alive())
        session = YandexStreamSession(core)
        session.cancel()
        result = session.finalize()
        self.assertEqual(result["stream_failure"]["category"], "PROVIDER")
        self.assertEqual(result["stream_failure"]["code"], "UNAVAILABLE")
        self.assertIs(core.failure, primary)

    def test_cached_failure_updates_when_concurrent_cleanup_finishes(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from integrations.hermes.provider import YandexStreamSession
        from tests.test_streaming import FakeStreamingRpc, native
        from yandex_speech.streaming import TranscriptionSession
        entered, release = threading.Event(), threading.Event()
        rpc = FakeStreamingRpc()
        original_close = rpc.close
        def blocked_close():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("cleanup did not receive its release")
            raise RuntimeError("private-cleanup-fixture")
        rpc.close = blocked_close
        core = TranscriptionSession(rpc, audio_input=native())
        session = YandexStreamSession(core)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(session.cancel)
                self.assertTrue(entered.wait(2))
                result = session.finalize()
                self.assertFalse(result["success"])
                self.assertTrue(result["stream_failure"].get("cleanup_pending", False))
                release.set()
                pending.result(2)
            self.assertTrue(core.cleanup_failed)
            self.assertTrue(result["stream_failure"]["cleanup_failed"])
            self.assertFalse(result["stream_failure"]["cleanup_pending"])
            self.assertIs(session.finalize(), result)
            self.assertNotIn("private-cleanup-fixture", str(result))
        finally:
            release.set()
            rpc.close = original_close
            core.cancel()

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

    def test_native_method_defaults_stay_below_user_file_and_false_is_preserved(self):
        provider = self.provider({"voice": "ermil", "yandex": {
            "file_method": "stream", "unsafe_mode": True}})
        path = self.home / ".config/yandex-speech/config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"speech": {"tts": {
            "file_method": "utterance", "unsafe_mode": False}}}))
        settings, options, _ = provider._client()._options()
        self.assertEqual(options["file_method"], "utterance")
        self.assertFalse(options["unsafe_mode"])
        self.assertEqual(settings.sources["tts.file_method"], "config")

    def test_file_stream_readiness_checks_selected_interface_and_keeps_pcm_distinct(self):
        from tests.test_tts_selection import RUNTIME, MISSING, READY
        from yandex_speech.tts_capabilities import RuntimeCapabilities
        provider = self.provider({"voice": "ermil", "yandex": {"file_method": "stream"}})
        with patch("yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME):
            self.assertTrue(provider.is_available())
        with patch("yandex_speech.tts_selection.probe_runtime",
                   return_value=RuntimeCapabilities(READY, READY, READY, MISSING)):
            self.assertFalse(provider.is_available())
        voices = {voice["id"]: voice for voice in provider.list_voices()}
        self.assertEqual(voices["alena"]["display"], "alena")

    def test_native_receipt_exposes_actual_route_without_credentials_or_text(self):
        provider = self.provider(fake_output=True)
        client = provider._client()
        client.last_synthesis = {"api_version": "3", "transport": "grpc", "method": "stream",
            "voice": "ermil", "model": "general", "role": "good", "parts": 2,
            "api_key": "private credential sentinel", "text": "private transcript sentinel"}
        with patch.object(provider, "_client", return_value=client), self.assertLogs(
                "integrations.hermes.tts_provider", level="INFO") as logs:
            result = provider.synthesize("synthetic", self.home / "receipt.ogg", format="ogg")
        message = logs.output[-1].split("Yandex native TTS receipt ", 1)[1]
        record = json.loads(message)
        self.assertEqual((record["api_version"], record["transport"], record["method"]), ("3", "grpc", "stream"))
        self.assertEqual(record["parts"], 2)
        self.assertEqual(record["output"], result)
        self.assertEqual(record["input_characters"], len("synthetic"))
        self.assertNotIn("sentinel", message)
        self.assertNotIn("api_key", record)
        self.assertGreaterEqual(record["elapsed_seconds"], 0)

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
        from tests.test_tts_selection import RUNTIME
        for voice in ("ermil", "sergey"):
            with self.subTest(voice=voice), patch(
                    "yandex_speech.tts_capabilities.probe_runtime", return_value=RUNTIME), patch(
                    "yandex_speech.tts_v3.grpc_sdk", return_value=(None, None, None)):
                provider = self.provider({"voice": voice})
                self.assertTrue(provider.is_available())
                self.assertTrue(provider.streams_pcm)
