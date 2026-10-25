import tempfile
import unittest
from pathlib import Path

from tests.test_streaming import FakeStreamingRpc, native
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.transcription import SpeechTranscriber


class StreamApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.context = RuntimeContext(user_home=Path(self.tmp.name))

    def client(self):
        return SpeechTranscriber(api_key="synthetic", _context=self.context,
                                  _transport=FakeStreamingRpc())

    def test_public_client_opens_complete_session(self):
        with self.client().open_stream(audio_input=native()) as stream:
            stream.push_audio(b"\0" * 3200)
            result = stream.finalize()
        self.assertTrue(result["input_complete"])

    def test_explicit_incompatible_stream_features_fail_before_open(self):
        for options in [{"transport": "rest"}, {"api_version": "1"}, {"diarize": True}]:
            with self.subTest(options=options):
                with self.assertRaises(SpeechError):
                    self.client().open_stream(audio_input=native(), **options)

    def test_process_operation_limit_releases_after_cancel(self):
        first = self.client().open_stream(audio_input=native())
        second = self.client().open_stream(audio_input=native())
        self.addCleanup(first.cancel)
        self.addCleanup(second.cancel)
        with self.assertRaises(SpeechError) as cm:
            self.client().open_stream(audio_input=native())
        self.assertEqual(cm.exception.category, "RATE_LIMIT")
        first.cancel()
        third = self.client().open_stream(audio_input=native())
        third.cancel()
        second.cancel()

    def test_failed_push_closes_transport_and_releases_operation_slot(self):
        import time
        failed = []
        for _ in range(2):
            client = self.client()
            session = client.open_stream(audio_input=native())
            failed.append((session, client._transport))
            self.addCleanup(session.cancel)
            with self.assertRaises(SpeechError):
                session.push_audio(b"\0" * 64002)
        deadline = time.monotonic() + 2
        while not all(rpc.closed and stream._closed for stream,rpc in failed):
            if time.monotonic() >= deadline:
                self.fail("fatal push retained owned transport or operation slot")
            time.sleep(.01)
        third = self.client().open_stream(audio_input=native())
        third.cancel()

    def test_expired_empty_stream_cannot_report_no_speech_success(self):
        import time
        session = self.client().open_stream(audio_input=native(), deadline_seconds=.01)
        self.addCleanup(session.cancel)
        time.sleep(.03)
        with self.assertRaises(SpeechError) as cm:
            session.finalize()
        self.assertEqual(cm.exception.category, "TIMEOUT")
