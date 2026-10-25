"""Paced synthetic audio exercises the real full-utterance protocol."""
import json
import os
import time
import unittest
import wave

from tests.provider.test_recording import LiveRecordingTests
from yandex_speech.media import AudioInputSpec


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1",
                     "explicit provider opt-in required")
class LiveStreamingTests(unittest.TestCase):
    setUp = LiveRecordingTests.setUp

    def test_real_paced_stream_retains_full_tail_and_finishes(self):
        with wave.open(str(self.fixtures / "single.wav"), "rb") as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()),
                             (16000, 1, 2))
            pcm = audio.readframes(audio.getnframes())
        descriptor = AudioInputSpec(source_format="lpcm", sample_format="s16le",
                                    sample_rate_hertz=16000, channels=1)
        previews = []
        began = time.monotonic()
        with self.client.open_stream(audio_input=descriptor, timestamps=True,
                                     deadline_seconds=120) as session:
            for offset in range(0, len(pcm), 640):
                session.push_audio(pcm[offset:offset+640])
                preview = session.partial_transcript()
                if preview:
                    previews.append(preview)
                time.sleep(0.02)
            result = session.finalize()
            self.assertEqual(session.finalize(), result)
        self.assertTrue(result["input_complete"])
        self.assertIn("контрольный хвост", result["text"].casefold())
        self.assertIn("23", result["text"])
        self.assertIn("сука", result["text"].casefold())
        self.assertTrue(previews, "no actual provider preview was observed while audio arrived")
        (self.root / "acceptance.json").write_text(json.dumps({
            "case": self.id(), "result": "passed", "tail_verified": True,
            "input_bytes": len(pcm), "preview_observations": len(previews),
            "elapsed_seconds": time.monotonic() - began,
            "input_complete": result["input_complete"],
            "completion_kind": result["completion_kind"],
        }, indent=2))
