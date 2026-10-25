import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

from tests.helpers import make_wave
from tests.test_results import final, refinement
from yandex_speech.errors import SpeechError
from yandex_speech.media import AudioInputSpec
from yandex_speech.streaming import TranscriptionSession


class FakeCall:
    def __init__(self, requests, *, acknowledge=True, prefix_only=False, blocked=False):
        self.requests = requests
        self.acknowledge, self.prefix_only, self.blocked = acknowledge, prefix_only, blocked
        self.cancelled = threading.Event()
        self.at_eou = threading.Event()
        self.kinds = []
        self.audio = bytearray()
        self.half_closed = False

    def cancel(self):
        self.cancelled.set()

    def __iter__(self):
        first = True
        for request in self.requests:
            if request.HasField("session_options"):
                self.kinds.append("options")
                self.options = request.session_options
            elif request.HasField("chunk"):
                self.kinds.append("audio")
                self.audio.extend(request.chunk.data)
                if first:
                    first = False
                    yield {"partial": {"alternatives": [{"text": "preview"}]}}
                    if self.prefix_only:
                        yield final(0, "prefix", end=50)
                        return
            elif request.HasField("eou"):
                self.kinds.append("eou")
                self.at_eou.set()
                if self.blocked:
                    self.cancelled.wait(10)
                    return
                yield final(0, "первая фраза", end=50)
                yield refinement(0, "Первая фраза.")
                yield final(1, "контрольный хвост", start=50, end=len(self.audio) / 32)
                yield refinement(1, "Контрольный хвост.")
                if self.acknowledge:
                    yield {"eou_update": {"time_ms": len(self.audio) / 32},
                           "audio_cursors": {"eou_time_ms": len(self.audio) / 32,
                                             "final_time_ms": len(self.audio) / 32}}
        self.half_closed = True


class FakeStreamingRpc:
    def __init__(self, **options):
        self.options = options
        self.calls = 0
        self.call = None
        self.closed = False

    def bind_cancel_event(self, event):
        self.event = event

    def open_stream(self, requests, timeout):
        self.calls += 1
        self.call = FakeCall(requests, **self.options)
        return self.call

    def cancel_all(self):
        if self.call:
            self.call.cancel()

    def close(self):
        self.closed = True
        self.cancel_all()


def native():
    return AudioInputSpec(source_format="lpcm", sample_rate_hertz=16000, channels=1)


class StreamingTests(unittest.TestCase):
    def session(self, rpc=None, **kwargs):
        rpc = rpc or FakeStreamingRpc()
        stream = TranscriptionSession(rpc, audio_input=kwargs.pop("audio_input", native()),
                                      **kwargs)
        self.addCleanup(stream.cancel)
        return stream, rpc

    def test_options_fifo_eou_halfclose_and_complete_tail(self):
        stream, rpc = self.session()
        stream.push_audio(b"\0")
        stream.push_audio(b"\0" * 3199)
        result = stream.finalize()
        self.assertTrue(result["input_complete"])
        self.assertEqual(result["completion_kind"], "streamed-utterance")
        self.assertEqual(result["text"], "Первая фраза. Контрольный хвост.")
        self.assertEqual(rpc.call.kinds[0], "options")
        self.assertEqual(rpc.call.kinds[-1], "eou")
        self.assertEqual(rpc.call.kinds.count("eou"), 1)
        self.assertEqual(bytes(rpc.call.audio), b"\0" * 3200)
        self.assertTrue(rpc.call.half_closed)
        self.assertTrue(rpc.call.options.eou_classifier.HasField("external_classifier"))
        self.assertTrue(rpc.call.options.recognition_model.text_normalization.literature_text)

    def test_missing_ack_and_prefix_eof_never_complete(self):
        for options in [{"acknowledge": False}, {"prefix_only": True}]:
            with self.subTest(options=options):
                stream, rpc = self.session(FakeStreamingRpc(**options))
                stream.push_audio(b"\0" * 3200)
                with self.assertRaises(SpeechError):
                    stream.finalize()

    def test_estimated_eou_matches_provider_cursor_not_raw_byte_duration(self):
        class EstimatedCall(FakeCall):
            def __iter__(self):
                for event in super().__iter__():
                    if "eou_update" in event:
                        estimate = len(self.audio) / 32 - 10
                        event = {"eou_update": {"time_ms": estimate},
                                 "audio_cursors": {"eou_time_ms": estimate,
                                                   "final_time_ms": estimate}}
                    yield event
        class EstimatedRpc(FakeStreamingRpc):
            def open_stream(self, requests, timeout):
                self.call = EstimatedCall(requests)
                return self.call
        stream, _ = self.session(EstimatedRpc())
        stream.push_audio(b"\0" * 3200)
        self.assertTrue(stream.finalize()["input_complete"])

    def test_acknowledgement_with_inconsistent_cursor_fails(self):
        class InconsistentCall(FakeCall):
            def __iter__(self):
                for event in super().__iter__():
                    if "eou_update" in event:
                        event["audio_cursors"]["eou_time_ms"] = 1
                    yield event
        class InconsistentRpc(FakeStreamingRpc):
            def open_stream(self, requests, timeout):
                self.call = InconsistentCall(requests)
                return self.call
        stream, _ = self.session(InconsistentRpc())
        stream.push_audio(b"\0" * 3200)
        with self.assertRaises(SpeechError):
            stream.finalize()

    def test_finalize_is_idempotent_and_late_push_is_state_error(self):
        stream, rpc = self.session()
        stream.push_audio(b"\0" * 3200)
        first = stream.finalize()
        self.assertEqual(stream.finalize(), first)
        self.assertEqual(rpc.calls, 1)
        self.assertEqual(rpc.call.kinds.count("eou"), 1)
        with self.assertRaises(SpeechError) as cm:
            stream.push_audio(b"\0\0")
        self.assertEqual(cm.exception.category, "STATE")

    def test_incomplete_raw_source_frame_never_sends_eou(self):
        stream, rpc = self.session()
        stream.push_audio(b"\0")
        with self.assertRaises(SpeechError) as cm:
            stream.finalize()
        self.assertEqual(cm.exception.category, "UNSUPPORTED_AUDIO")
        self.assertNotIn("eou", rpc.call.kinds)

    def test_encoded_decoder_flush_preserves_every_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path = make_wave(Path(directory) / "source.wav")
            with wave.open(str(path), "rb") as audio:
                expected = audio.readframes(audio.getnframes())
            data = path.read_bytes()
            stream, rpc = self.session(audio_input=AudioInputSpec())
            for offset in range(0, len(data), 37):
                stream.push_audio(data[offset:offset + 37])
            result = stream.finalize()
            self.assertTrue(result["input_complete"])
            self.assertEqual(bytes(rpc.call.audio), expected)

    def test_corrupt_encoded_source_fails_despite_provider_prefix(self):
        stream, rpc = self.session(audio_input=AudioInputSpec())
        stream.push_audio(b"not a decodable media stream")
        with self.assertRaises(SpeechError):
            stream.finalize()

    def test_cancel_wins_finalize_and_owned_workers_stop(self):
        stream, rpc = self.session(FakeStreamingRpc(blocked=True))
        stream.push_audio(b"\0" * 3200)
        errors = []
        def complete():
            try:
                stream.finalize()
            except SpeechError as error:
                errors.append(error.category)
        thread = threading.Thread(target=complete)
        thread.start()
        self.assertTrue(rpc.call.at_eou.wait(2))
        start = time.monotonic()
        stream.cancel()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertLessEqual(time.monotonic() - start, 2)
        self.assertEqual(errors, ["CANCELLED"])
        self.assertFalse(any(worker.is_alive() for worker in stream.workers))

    def test_utterance_limit_cancels_instead_of_dropping_audio(self):
        stream, rpc = self.session(_max_pcm_bytes=3200)
        with self.assertRaises(SpeechError) as cm:
            stream.push_audio(b"\0" * 3202)
        self.assertEqual(cm.exception.category, "LIMIT")
        with self.assertRaises(SpeechError):
            stream.finalize()

    def test_zero_audio_no_speech_does_not_open_provider(self):
        stream, rpc = self.session()
        result = stream.finalize()
        self.assertEqual(result["status"], "no_speech")
        self.assertTrue(result["input_complete"])
        self.assertEqual(rpc.calls, 0)
