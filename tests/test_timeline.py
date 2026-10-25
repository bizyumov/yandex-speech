import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_wave
from tests.test_results import final
from yandex_speech.media import prepare_audio
from yandex_speech.results import ResultAccumulator


class TimelineTests(unittest.TestCase):
    def test_selected_audio_origin_survives_conversion_and_is_applied_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = make_wave(root / "original.wav", seconds=1)
            shifted = root / "shifted.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-itsoffset", "5",
                            "-i", str(original), "-c:a", "aac", str(shifted)], check=True)
            with prepare_audio(shifted) as prepared:
                info = prepared.source_info
                offset = info["timeline_offset_seconds"]
                self.assertGreater(offset, 4)
                self.assertIn("start_time_seconds", info["original"])
                acc = ResultAccumulator(literature_text=False, timestamps=True)
                event = final(0, "source time", start=100, end=900)
                acc.feed(event)
                a, b = acc.result(source_info=info), acc.result(source_info=info)
                self.assertAlmostEqual(a["segments"][0]["start"], offset + 0.1)
                self.assertAlmostEqual(a["segments"][0]["words"][0]["end"], offset + 0.9)
                self.assertEqual(a["segments"], b["segments"])
                self.assertEqual(a["raw"][0]["final"]["alternatives"][0]["start_time_ms"], 100)
