import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import make_wave
from yandex_speech.errors import SpeechError
from yandex_speech.media import AudioInputSpec, prepare_audio


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_compatible_wav_preserves_source_and_actual_metadata(self):
        path = make_wave(self.root / "wrong.extension")
        original = path.read_bytes()
        with prepare_audio(path) as audio:
            self.assertEqual(audio.path, path)
            self.assertEqual(audio.container, "WAV")
            self.assertEqual(audio.source_info["original"]["sample_rate_hertz"], 16000)
            self.assertAlmostEqual(audio.duration_ms, 100, delta=1)
        self.assertEqual(path.read_bytes(), original)

    def test_arbitrary_decodable_media_is_converted_automatically(self):
        source = make_wave(self.root / "stereo.wav", channels=2, rate=44100)
        for name, args in [
            ("input.flac", ["-c:a", "flac"]),
            ("input.m4a", ["-c:a", "aac"]),
            ("input.webm", ["-c:a", "libopus"]),
            ("input.mp3", ["-c:a", "libmp3lame"]),
            ("float.wav", ["-c:a", "pcm_f32le"]),
            ("24bit.wav", ["-c:a", "pcm_s24le"]),
        ]:
            with self.subTest(name=name):
                path = self.root / name
                subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                                "-i", str(source), *args, str(path)], check=True)
                with prepare_audio(path) as audio:
                    self.assertEqual(audio.channels, 1)
                    self.assertGreater(audio.duration_ms, 0)
                    self.assertTrue(audio.path.is_file())
                    self.assertTrue(audio.source_info["converted"])
                self.assertTrue(path.exists())

    def test_raw_source_metadata_alignment_and_no_fake_wav_header(self):
        path = self.root / "source.pcm"
        path.write_bytes(b"\0\0" * 1600)
        with prepare_audio(path, audio_input=AudioInputSpec(
                source_format="lpcm", sample_rate_hertz=16000, channels=1)) as audio:
            self.assertEqual(audio.container, "LPCM")
            self.assertEqual(audio.path.read_bytes(), path.read_bytes())
            self.assertEqual(audio.duration_ms, 100)
        path.write_bytes(b"\0")
        with self.assertRaises(SpeechError):
            with prepare_audio(path, audio_input=AudioInputSpec(
                    source_format="lpcm", sample_rate_hertz=16000, channels=1)):
                pass

    def test_no_audio_corrupt_and_missing_raw_metadata_fail(self):
        path = self.root / "broken.ogg"
        path.write_bytes(b"not audio")
        with self.assertRaises(SpeechError) as cm:
            with prepare_audio(path):
                pass
        self.assertEqual(cm.exception.category, "UNSUPPORTED_AUDIO")
        with self.assertRaises(SpeechError):
            with prepare_audio(path, audio_input=AudioInputSpec(source_format="lpcm")):
                pass

    def test_missing_converter_is_actionable_without_installing(self):
        path = make_wave(self.root / "stereo.wav", channels=2)
        with patch("yandex_speech.media.shutil.which", side_effect=lambda name: None):
            with self.assertRaises(SpeechError) as cm:
                with prepare_audio(path):
                    pass
        self.assertEqual(cm.exception.category, "PREREQUISITE")
