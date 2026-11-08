"""Real provider requests consumed through the configured stock Hermes runtime host dispatch."""
import json
import os
import time
import unittest
import wave
from unittest.mock import patch

from tests.provider import test_recording


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1"
                     and os.environ.get("YANDEX_SPEECH_HOST_TESTS") == "1",
                     "explicit provider and host opt-in required")
class LiveHermesTests(unittest.TestCase):
    setUp=test_recording.LiveRecordingTests.setUp

    def test_real_host_file_and_arriving_turn(self):
        from agent.secret_scope import set_secret_scope,reset_secret_scope
        from hermes_cli.plugins import PluginContext,PluginManager,PluginManifest
        from hermes_cli.plugins_loader import _plugin_home_scope
        from tools.transcription_command import _dispatch_to_plugin_provider
        from tools.transcription_streaming import open_streaming_session
        from integrations.hermes import register
        config={"provider":"yandex","streaming":True,"yandex":{"language":"ru-RU"}}
        manager=PluginManager(scope_key=str(self.root))
        ctx=PluginContext(PluginManifest(name="yandex-speech"),manager)
        previews=[]
        # Isolate plugin discovery to this registered fixture. Speech calls, registry,
        # native interfaces and host consumption below are real.
        with _plugin_home_scope(self.root):
            token=set_secret_scope({"YANDEX_SPEECH_API_KEY":self.key},profile_home=str(self.root))
            try:
                with patch("hermes_cli.config.load_config_readonly",return_value={"stt":config}):
                    provider=register(ctx)
                with patch("hermes_cli.plugins._ensure_plugins_discovered"):
                    file_result=_dispatch_to_plugin_provider(str(self.fixtures/"single.wav"),
                                                             "yandex",config)
                    self.assertTrue(file_result["success"],file_result)
                    self.assertIn("контрольный хвост",file_result["transcript"].casefold())
                    session=open_streaming_session(on_partial=previews.append,stt_config=config)
                    self.assertIsNotNone(session,provider.streaming_unavailable_reason)
                    self.addCleanup(session.cancel)
                    with wave.open(str(self.fixtures/"single.wav"),"rb") as source:
                        while chunk:=source.readframes(320):
                            session.push_audio(chunk)
                            time.sleep(.02)
                    stream_result=session.finalize(timeout=10)
                    self.assertTrue(stream_result["success"],stream_result)
                    self.assertIn("контрольный хвост",stream_result["transcript"].casefold())
                    self.assertTrue(previews)
                    self.assertTrue(stream_result["speech_result"]["input_complete"])
                    session._worker.join(timeout=2)
                    self.assertFalse(session._worker.is_alive())
            finally:
                reset_secret_scope(token)
        (self.root/"acceptance.json").write_text(json.dumps({
            "case":self.id(),"result":"passed","host":"the configured stock Hermes runtime",
            "file_dispatch":"tools.transcription_command._dispatch_to_plugin_provider",
            "stream_dispatch":"tools.transcription_streaming.open_streaming_session",
            "file_tail_verified":True,"stream_tail_verified":True,
            "preview_observations":len(previews),"fixture_plugin_discovery_isolated":True,
        },indent=2))
