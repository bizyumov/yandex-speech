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

    def test_native_ogg_extracts_the_selected_stream_without_downmixing(self):
        path = self.root / "tracks.ogg"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.1", "-f", "lavfi", "-i",
            "sine=frequency=880:duration=0.1", "-map", "0:a", "-map", "1:a",
            "-c:a", "libopus", str(path),
        ], check=True)
        original = path.read_bytes()
        for track in (0, 1):
            with self.subTest(track=track):
                expected = subprocess.check_output([
                    "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                    "-map", f"0:a:{track}", "-f", "s16le", "pipe:1",
                ])
                with prepare_audio(path, audio_track=track) as audio:
                    self.assertEqual(audio.container, "OGG_OPUS")
                    self.assertEqual(audio.source_info["original"]["track"], track)
                    actual = subprocess.check_output([
                        "ffmpeg", "-nostdin", "-v", "error", "-i", str(audio.path),
                        "-f", "s16le", "pipe:1",
                    ])
                    self.assertEqual(actual, expected)
        self.assertEqual(path.read_bytes(), original)

    def test_arbitrary_decodable_media_is_converted_automatically(self):
        source = make_wave(self.root / "stereo.wav", channels=2, rate=44100)
        for name, args in [
            ("input.flac", ["-c:a", "flac"]),
            ("input.m4a", ["-c:a", "aac"]),
            ("input.webm", ["-c:a", "libopus"]),
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

    def test_amr_and_selected_video_audio_track_are_decoded(self):
        amr=self.root/"input.amr"
        amr.write_bytes(b"#!AMR\n"+(b"\x04"+b"\0"*12)*20)
        with prepare_audio(amr) as audio:
            self.assertEqual(audio.container,"WAV")
            self.assertEqual(audio.sample_rate_hertz,16000)
            self.assertGreater(audio.duration_ms,300)
        first=make_wave(self.root/"first.wav",rate=8000)
        second=make_wave(self.root/"second.wav",rate=48000)
        video=self.root/"tracks.mkv"
        subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i",
            "color=size=16x16:rate=10:duration=0.1","-i",str(first),"-i",str(second),
            "-map","0:v","-map","1:a","-map","2:a","-c:v","ffv1","-c:a","pcm_s16le",str(video)],
            check=True)
        with prepare_audio(video,audio_track=1) as audio:
            self.assertEqual(audio.source_info["original"]["track"],1)
            self.assertEqual(audio.source_info["original"]["sample_rate_hertz"],48000)
            self.assertEqual(audio.channels,1)
        with self.assertRaises(SpeechError):
            with prepare_audio(video,audio_track=2):
                pass

    def test_decodable_pcm_at_unsupported_rate_is_converted(self):
        source=make_wave(self.root/"mono44100.wav",rate=44100)
        with prepare_audio(source) as audio:
            self.assertTrue(audio.source_info["converted"])
            self.assertEqual(audio.sample_rate_hertz,16000)

    def test_preparation_does_not_outlive_its_whole_deadline(self):
        import time
        import yandex_speech.media as media
        source=make_wave(self.root/"deadline.wav")
        original=media._probe
        def delayed(*args,**kwargs):
            value=original(*args,**kwargs)
            time.sleep(.02)
            return value
        with patch.object(media,"_probe",side_effect=delayed):
            with self.assertRaises(SpeechError) as cm:
                with prepare_audio(source,timeout=.001):
                    pass
        self.assertEqual(cm.exception.category,"TIMEOUT")

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
