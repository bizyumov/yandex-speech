import io
import struct
import unittest
import wave

from yandex_speech.errors import SpeechError


class WavFinalizationTests(unittest.TestCase):
    def wav(self, frames=4800):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(48000)
            audio.writeframes(b"\0\0" * frames)
        return stream.getvalue()

    def test_unknown_stream_lengths_become_actual_file_lengths_without_reencoding(self):
        from yandex_speech.tts_audio import finalize_wav
        original = bytearray(self.wav())
        struct.pack_into("<I", original, 4, 0xFFFFFFFF)
        struct.pack_into("<I", original, 40, 0xFFFFFFFF)
        fixed = finalize_wav(bytes(original))
        self.assertEqual(struct.unpack_from("<I", fixed, 4)[0], len(fixed) - 8)
        self.assertEqual(struct.unpack_from("<I", fixed, 40)[0], len(fixed) - 44)
        self.assertEqual(fixed[44:], original[44:])
        with wave.open(io.BytesIO(fixed), "rb") as audio:
            self.assertEqual(audio.getnframes(), 4800)
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), .1)

    def test_valid_complete_wav_is_unchanged(self):
        from yandex_speech.tts_audio import finalize_wav
        original = self.wav()
        self.assertEqual(finalize_wav(original), original)

    def test_known_truncation_is_not_disguised_as_a_complete_stream(self):
        from yandex_speech.tts_audio import finalize_wav
        with self.assertRaises(SpeechError):
            finalize_wav(self.wav()[:-4])

    def test_partial_pcm_frame_is_rejected(self):
        from yandex_speech.tts_audio import finalize_wav
        original = bytearray(self.wav())
        struct.pack_into("<I", original, 4, 0xFFFFFFFF)
        struct.pack_into("<I", original, 40, 0xFFFFFFFF)
        with self.assertRaises(SpeechError):
            finalize_wav(bytes(original[:-1]))
