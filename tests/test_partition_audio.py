"""Complete sample coverage and actual inline-wire budgets for async partitions."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

from yandex_speech.errors import SpeechError
from yandex_speech.media import AudioInputSpec, PreparedAudio
from yandex_speech import rest_stt
from yandex_speech.partition_audio import frame_windows, inline_frame_capacity, prepare_parts


def wav(path, *, frames, rate=16000, channels=1):
    with path.open("xb") as stream:
        with wave.open(stream, "wb") as audio:
            audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
            audio.writeframes((bytes.fromhex("0100") * channels) * frames)


class PartitionGeometryTests(unittest.TestCase):
    def test_all_frames_with_five_second_context_and_no_redundant_tail(self):
        rate = 16000
        for seconds, expected in (
                (30, [(0, 30)]), (55, [(0, 30), (25, 55)]),
                (56, [(0, 30), (25, 55), (50, 56)])):
            with self.subTest(seconds=seconds):
                windows = frame_windows(seconds * rate, 30 * rate, rate)
                self.assertEqual([(x.start // rate, x.end // rate) for x in windows], expected)
                self.assertEqual(windows[-1].end, seconds * rate)
                self.assertTrue(all(x.end - x.start <= 30 * rate for x in windows))
        self.assertEqual(frame_windows(0, 30 * rate, rate), [])
        self.assertEqual(frame_windows(30 * rate + 1, 30 * rate, rate)[-1].end, 30 * rate + 1)

    def test_low_budget_reduces_context_without_losing_progress(self):
        windows = frame_windows(100, 20, 16000)
        self.assertEqual((windows[0].start, windows[-1].end), (0, 100))
        self.assertTrue(all(right.start <= left.end and right.start > left.start
                            for left, right in zip(windows, windows[1:])))

    def test_invalid_geometry_and_excessive_part_count_fail_locally(self):
        for values in ((True, 20, 16000), (-1, 20, 16000), (100, 0, 16000), (100, 20, False)):
            with self.subTest(values=values), self.assertRaises(SpeechError):
                frame_windows(*values)
        with self.assertRaises(SpeechError) as caught:
            frame_windows(100000, 1, 16000)
        self.assertEqual(caught.exception.category, "LIMIT")


class PartitionWireTests(unittest.TestCase):
    def test_rest_budget_uses_exact_utf8_envelope_and_base64_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(rest_stt, "MAX_REST_BYTES", 4096):
                count = inline_frame_capacity(
                    ("rest",), root, language="ru-RU", diarize=False,
                    literature_text=True, channels=2)
                path = root / "at-budget.wav"
                wav(path, frames=count, channels=2)
                audio = PreparedAudio(path, "WAV", 16000, 2, count / 16, {"converted": True})
                body = rest_stt.build_rest_request(audio)
                self.assertLessEqual(len(body), rest_stt.MAX_REST_BYTES)
                over = root / "above-budget.wav"
                wav(over, frames=count + 1, channels=2)
                with self.assertRaises(SpeechError) as caught:
                    rest_stt.build_rest_request(PreparedAudio(
                        over, "WAV", 16000, 2, (count + 1) / 16, {"converted": True}))
                self.assertEqual(caught.exception.category, "LIMIT")
                message = str(caught.exception)
                self.assertIn("4096 bytes", message)
                self.assertIn("Yandex Object Storage (S3-compatible storage)", message)
                self.assertIn("--bucket EXISTING_BUCKET", message)
                self.assertIn("--split-inline", message)
                with patch.object(rest_stt, "MAX_REST_BYTES", len(body)):
                    self.assertEqual(len(rest_stt.build_rest_request(audio)), len(body))
                with patch.object(rest_stt, "MAX_REST_BYTES", len(body) - 1):
                    with self.assertRaises(SpeechError) as exact:
                        rest_stt.build_rest_request(audio)
                    self.assertIn(f"{len(body) - 1} bytes", str(exact.exception))


class PartitionMediaTests(unittest.TestCase):
    def test_complete_stereo_media_coverage_and_private_owned_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "addressed.wav"
            wav(source, frames=44100 * 12, rate=44100, channels=2)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = root / "caller"
            target.mkdir(mode=0o775)
            os.chmod(target, 0o775)
            options = {"language": "ru-RU", "diarize": False, "literature_text": True}
            prototype = root / "six-seconds.wav"
            wav(prototype, frames=16000 * 6, channels=2)
            maximum = len(rest_stt.build_rest_request(PreparedAudio(
                prototype, "WAV", 16000, 2, 6000, {"converted": True}), **options))
            with patch.object(rest_stt, "MAX_REST_BYTES", maximum):
                info, parts = prepare_parts(source, target, transports=("rest",), **options)
                self.assertGreater(len(parts), 1)
                self.assertEqual(parts[-1]["end_frame"], 16000 * 12)
                self.assertEqual(info["source_sha256"], digest)
                self.assertEqual(info["prepared"]["channels"], 2)
                decoded = []
                for part in parts:
                    path = Path(part["audio_path"])
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    with wave.open(str(path), "rb") as audio:
                        self.assertEqual((audio.getnchannels(), audio.getframerate()), (2, 16000))
                        self.assertEqual(audio.getnframes(), part["end_frame"] - part["start_frame"])
                        decoded.append(audio.readframes(audio.getnframes()))
                    request = rest_stt.build_rest_request(PreparedAudio(
                        path, "WAV", 16000, 2, part["duration_ms"], {"converted": True}), **options)
                    self.assertLessEqual(len(request), maximum)
                for index, (left, right) in enumerate(zip(parts, parts[1:])):
                    overlap = left["end_frame"] - right["start_frame"]
                    self.assertEqual(decoded[index][-overlap * 4:], decoded[index + 1][:overlap * 4])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertEqual(target.stat().st_mode & 0o777, 0o775)

    def test_cancelled_preparation_never_starts_decoder_or_recognition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "audio.wav"
            wav(source, frames=1600)
            cancelled = threading.Event()
            cancelled.set()
            with patch("yandex_speech.partition_audio.subprocess.Popen") as process:
                with self.assertRaises(SpeechError) as caught:
                    prepare_parts(source, root, transports=("rest",), cancel_event=cancelled)
            self.assertEqual(caught.exception.category, "CANCELLED")
            process.assert_not_called()
