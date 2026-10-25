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
