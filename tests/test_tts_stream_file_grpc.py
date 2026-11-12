"""gRPC-profile tests for complete-file collection, distinct from PCM streaming."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from yandex.cloud.ai.tts.v3 import tts_pb2 as p
from yandex_speech.config import DEFAULTS
from yandex_speech.errors import SpeechError
from yandex_speech.tts_stream_file import collect_stream_file, synthesis_options
from yandex_speech.tts_voices import resolve_voice
from tests.test_tts_audio import encoded_tone, decoded_pcm


def options(fmt="oggopus", voice="ermil"):
    return resolve_voice(dict(DEFAULTS["tts"], default_voice=voice, default_format=fmt,
        api_version="3", transport="grpc"), {})


class Transport:
    def __init__(self, data=b"", fail=False, consume=True, block=False, cancel=None):
        self.data, self.fail, self.consume, self.block = data, fail, consume, block
        self.requests, self.closed = [], threading.Event()
        self.cancel = cancel
        self.calls = 0

    def open_stream(self, requests, timeout):
        self.calls += 1
        if self.consume:
            self.requests = list(requests)
        def responses():
            yield p.StreamSynthesisResponse(text_chunk=p.TextChunk(text="synthetic"))
            if self.block:
                if self.cancel is not None:
                    self.cancel.set()
                self.closed.wait(2)
            for start in range(0, len(self.data), 37):
                yield p.StreamSynthesisResponse(audio_chunk=p.AudioChunk(data=self.data[start:start+37]))
            if self.fail:
                raise RuntimeError("synthetic interrupted EOF")
        return responses()

    def close(self):
        self.closed.set()


class StreamFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ogg = encoded_tone(440)

    def collect(self, transport, **kwargs):
        return collect_stream_file("Текст " * 100, options(), api_key="synthetic",
            _transport=transport, **kwargs)

    def test_transport_fragments_become_one_complete_container_without_joiner(self):
        transport = Transport(self.ogg)
        with patch("shutil.which", return_value=None):
            result = self.collect(transport)
        self.assertEqual(result.data, self.ogg)
        self.assertTrue(decoded_pcm(result.data))
        self.assertGreater(result.audio_records, 1)
        self.assertGreater(result.response_records, result.audio_records)
        self.assertTrue(transport.closed.is_set())
        self.assertEqual(transport.calls, 1)
        inputs = [request.synthesis_input.text for request in transport.requests if request.HasField("synthesis_input")]
        self.assertEqual(inputs, ["Текст " * 100])
        self.assertEqual(transport.requests[0].options.model, "general")
        self.assertTrue(transport.requests[-1].HasField("force_synthesis"))

    def test_failed_eof_and_truncation_never_return_complete_and_retain_prefix(self):
        for transport in (Transport(self.ogg, fail=True), Transport(self.ogg[:-10]), Transport(b"")):
            with tempfile.TemporaryDirectory() as root:
                evidence = Path(root) / "partial"
                with self.subTest(bytes=len(transport.data), fail=transport.fail), self.assertRaises(SpeechError):
                    self.collect(transport, partial_directory=evidence)
                self.assertEqual(transport.calls, 1)
                self.assertTrue(transport.closed.is_set())
                if transport.data:
                    self.assertEqual((evidence / "incomplete-provider-audio.bin").read_bytes(), transport.data)

    def test_request_early_eof_is_rejected(self):
        with self.assertRaises(SpeechError) as caught:
            self.collect(Transport(self.ogg, consume=False))
        self.assertEqual(caught.exception.category, "OUTPUT_PARTIAL")

    def test_cancellation_closes_blocked_producer(self):
        cancel = threading.Event()
        transport = Transport(self.ogg, block=True, cancel=cancel)
        with self.assertRaises(SpeechError) as caught:
            self.collect(transport, cancel_event=cancel)
        self.assertEqual(caught.exception.category, "CANCELLED")
        self.assertTrue(transport.closed.is_set())

    def test_deadline_closes_transport_without_replay(self):
        transport = Transport(self.ogg)
        original = transport.open_stream
        def slow(requests, timeout):
            responses = original(requests, timeout)
            for response in responses:
                time.sleep(.005)
                yield response
        transport.open_stream = slow
        with self.assertRaises(SpeechError) as caught:
            self.collect(transport, deadline_seconds=.001)
        self.assertEqual(caught.exception.category, "TIMEOUT")
        self.assertEqual(transport.calls, 1)
        self.assertTrue(transport.closed.is_set())

    def test_deadline_interrupts_blocked_response_read(self):
        transport = Transport(self.ogg, block=True)
        started = time.monotonic()
        with self.assertRaises(SpeechError) as caught:
            self.collect(transport, deadline_seconds=.08)
        self.assertEqual(caught.exception.category, "TIMEOUT")
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(transport.closed.is_set())

    def test_readiness_time_is_subtracted_before_rpc_deadline(self):
        from types import SimpleNamespace
        from yandex_speech.tts_stream import GrpcStreamingTransport
        transport = object.__new__(GrpcStreamingTransport)
        transport.closed = threading.Event()
        transport.channel = object()
        transport.metadata = ()
        transport.ready, transport.call = None, None
        class Future:
            def result(self, timeout):
                time.sleep(.04)
        observed = []
        transport.grpc = SimpleNamespace(channel_ready_future=lambda channel: Future(),
                                         FutureTimeoutError=type("FutureTimeoutError", (Exception,), {}))
        transport.stub = SimpleNamespace(StreamSynthesis=lambda requests, **kw: observed.append(kw["timeout"]))
        transport.open_stream(iter(()), .1)
        self.assertGreater(observed[0], 0)
        self.assertLess(observed[0], .08)

    def test_existing_retention_directory_blocks_before_rpc(self):
        with tempfile.TemporaryDirectory() as root:
            transport = Transport(self.ogg, fail=True)
            with self.assertRaises(SpeechError) as caught:
                self.collect(transport, partial_directory=Path(root))
            self.assertEqual(caught.exception.category, "CONFIG")
            self.assertEqual(transport.calls, 0)

    def test_retention_and_cleanup_failures_preserve_original_cause(self):
        class BrokenClose(Transport):
            def close(self):
                super().close()
                raise OSError("synthetic cleanup")
        with tempfile.TemporaryDirectory() as root, patch(
                "yandex_speech.tts_stream_file.atomic_write", side_effect=OSError("synthetic retention")):
            transport = BrokenClose(self.ogg, fail=True)
            with self.assertRaises(SpeechError) as caught:
                self.collect(transport, partial_directory=Path(root) / "new")
            self.assertEqual(caught.exception.category, "OUTPUT_PARTIAL")
            self.assertEqual(caught.exception.partial_retention_error, "OSError")
            self.assertEqual(caught.exception.cleanup_error, "OSError")
            self.assertEqual(transport.calls, 1)

    def test_exact_general_and_livetts_identity_controls(self):
        general = synthesis_options(dict(options(), _role="good", default_speed=1.2,
                                        volume=-17, pitch_shift=10))
        self.assertEqual((general.model, general.voice, general.role), ("general", "ermil", "good"))
        self.assertEqual((general.speed, general.volume, general.pitch_shift), (1.2, -17, 10))
        live = synthesis_options(dict(options(voice="sergey"), _role="sales"))
        self.assertEqual((live.model, live.voice, live.role), ("livetts", "sergey", "sales"))
        self.assertEqual(live.speed, 0)
        self.assertEqual(live.output_audio_spec.container_audio.container_audio_type, p.ContainerAudio.OGG_OPUS)
