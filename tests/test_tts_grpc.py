import concurrent.futures
import unittest

import grpc
from yandex.cloud.ai.tts.v3 import tts_pb2 as p, tts_service_pb2_grpc as service


class SynthesisGrpcTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        captured = self.requests
        class Provider(service.SynthesizerServicer):
            def UtteranceSynthesis(self, request, context):
                captured.append((request, dict(context.invocation_metadata())))
                yield p.UtteranceSynthesisResponse(audio_chunk=p.AudioChunk(data=b"first"))
                yield p.UtteranceSynthesisResponse(text_chunk=p.TextChunk(text="metadata"))
                yield p.UtteranceSynthesisResponse(audio_chunk=p.AudioChunk(data=b"tail"))
        self.server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        service.add_SynthesizerServicer_to_server(Provider(), self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.addCleanup(lambda:self.server.stop(0).wait(2))
        self.channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(self.channel.close)

    def test_actual_generated_unary_response_stream_is_fully_drained(self):
        from yandex_speech.tts_v3 import grpc_synthesize
        body={"model":"livetts","text":"Тест.","hints":[{"voice":"sergey"}],
              "outputAudioSpec":{"containerAudio":{"containerAudioType":"MP3"}},"unsafeMode":False}
        output = grpc_synthesize("synthetic",body,_channel=self.channel)
        self.assertEqual(output,b"firsttail")
        request,metadata=self.requests[0]
        self.assertEqual(request.model,"livetts")
        self.assertEqual(request.hints[0].voice,"sergey")
        self.assertEqual(metadata["authorization"],"Api-Key synthetic")
        self.assertNotIn("x-folder-id",metadata)

    def test_missing_grpc_dependency_is_actionable_without_installation(self):
        import builtins
        from unittest.mock import patch
        from yandex_speech.errors import SpeechError
        from yandex_speech.tts_v3 import grpc_sdk
        original=builtins.__import__
        def blocked(name,*args,**kwargs):
            if name=="grpc":
                raise ImportError("optional profile unavailable")
            return original(name,*args,**kwargs)
        with patch("builtins.__import__",side_effect=blocked):
            with self.assertRaises(SpeechError) as cm:
                grpc_sdk()
        self.assertEqual(cm.exception.category,"PREREQUISITE")
