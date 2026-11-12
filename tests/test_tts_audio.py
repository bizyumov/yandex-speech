import io
import json
import math
import subprocess
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

def encoded_tone(frequency, duration=.3):
    frames = int(48000 * duration)
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / 48000)))
                   for i in range(frames))
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "s16le",
        "-ar", "48000", "-ac", "1", "-i", "pipe:0", "-c:a", "libopus",
        "-f", "ogg", "pipe:1"], input=pcm, capture_output=True, check=True, timeout=15).stdout


def decoded_pcm(data):
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0",
        "-ar", "48000", "-ac", "1", "-f", "s16le", "pipe:1"],
        input=data, capture_output=True, check=True, timeout=15).stdout
    return struct.unpack("<" + "h" * (len(raw) // 2), raw)

def measured_frame_samples(data):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_packets",
        "-show_entries", "packet=duration_time", "-of", "json", "pipe:0"],
        input=data, capture_output=True, check=True, timeout=15)
    packets = json.loads(result.stdout)["packets"]
    durations = [round(float(packet["duration_time"]) * 48000) for packet in packets]
    if not durations or min(durations) <= 0:
        raise AssertionError("fixture has no measurable codec-frame duration")
    return max(durations)


def tone_strength(samples, frequency):
    real = sum(value * math.cos(2 * math.pi * frequency * i / 48000)
               for i, value in enumerate(samples))
    imaginary = sum(value * math.sin(2 * math.pi * frequency * i / 48000)
                    for i, value in enumerate(samples))
    return real * real + imaginary * imaginary


class EncodedAssemblyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parts = [encoded_tone(440), encoded_tone(880)]

    def test_single_ogg_response_is_preserved(self):
        from yandex_speech.tts_audio import join_audio
        self.assertEqual(join_audio(self.parts[:1], "oggopus", 48000), self.parts[0])
        self.assertTrue(decoded_pcm(self.parts[0]))

    def test_multipart_ogg_keeps_both_tones_in_order_and_complete_tail(self):
        from yandex_speech.tts_audio import join_audio
        joined = join_audio(self.parts, "oggopus", 48000, timeout=15)
        pcm = decoded_pcm(joined)
        expected_frames = sum(len(decoded_pcm(part)) for part in self.parts)
        self.assertLessEqual(abs(len(pcm) - expected_frames),
                             max(measured_frame_samples(part) for part in self.parts))
        first, tail = pcm[2400:7200], pcm[-7200:-2400]
        self.assertGreater(tone_strength(first, 440), 10 * tone_strength(first, 880))
        self.assertGreater(tone_strength(tail, 880), 10 * tone_strength(tail, 440))
