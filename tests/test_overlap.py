"""REST-v1 source windows and adjacent raw-text overlap reconciliation (#19)."""

import array
import struct
import tempfile
import unittest
import wave
from pathlib import Path


class OverlapTests(unittest.TestCase):
    def test_sample_exact_geometry_and_no_redundant_tail(self):
        from yandex_speech.overlap import pcm_windows
        for seconds, spans in [
            (29, [(0, 29)]), (30, [(0, 30)]),
            (31, [(0, 30), (25, 31)]), (55, [(0, 30), (25, 55)]),
            (56, [(0, 30), (25, 55), (50, 56)]),
        ]:
            with self.subTest(seconds=seconds):
                windows = list(pcm_windows(seconds * 16000))
                self.assertEqual([(w.start_sample / 16000, w.end_sample / 16000) for w in windows], spans)
                self.assertTrue(all(0 < w.byte_length <= 960000 for w in windows))
        self.assertEqual(list(pcm_windows(0)), [])

    def test_adjacent_alignment_preserves_repeated_requests_elsewhere(self):
        from yandex_speech.overlap import assemble_text
        result = assemble_text([
            "Повтори этот припев. Затем общий контрольный стык",
            "общий контрольный стык. Повтори этот припев.",
        ])
        self.assertEqual(result["text"].count("Повтори этот припев."), 2)
        self.assertEqual(result["text"].casefold().count("общий контрольный стык"), 1)
        self.assertEqual(len(result["raw_hypotheses"]), 2)
        self.assertTrue(result["uncertainties"])  # v1 supplies no word-time proof.

    def test_repeated_ambiguous_overlap_is_not_silently_deleted(self):
        from yandex_speech.overlap import assemble_text
        result = assemble_text(["Да да", "да да да"])
        self.assertEqual(result["text"], "Да да да да да")
        self.assertEqual(result["uncertainties"][0]["kind"], "overlap-ambiguous")
        self.assertEqual(result["raw_hypotheses"], ["Да да", "да да да"])

    def test_cut_word_uses_full_raw_word_and_retains_the_partial_alternative(self):
        from yandex_speech.overlap import assemble_text
        result = assemble_text(["Начало через пять минут транскрипц",
                                "через пять минут транскрипция завершена"])
        self.assertEqual(result["text"], "Начало через пять минут транскрипция завершена")
        self.assertEqual(result["uncertainties"][0]["kind"], "overlap-cut-word")
        self.assertIn("транскрипц", result["raw_hypotheses"][0])

    def test_numbers_signs_and_profanity_are_not_normalized_away(self):
        from yandex_speech.overlap import assemble_text
        raw = ["температура минус -5 блядь", "температура плюс +5 блядь"]
        result = assemble_text(raw)
        self.assertIn("-5", result["text"])
        self.assertIn("+5", result["text"])
        self.assertEqual(result["text"].count("блядь"), 2)
        self.assertTrue(result["uncertainties"])

    def test_empty_hypotheses_do_not_invent_speech(self):
        from yandex_speech.overlap import assemble_text
        self.assertEqual(assemble_text(["", ""])["text"], "")
        self.assertEqual(assemble_text(["", "хвост"])["text"], "хвост")


class RecordedWindowTests(unittest.TestCase):
    def test_public_split_path_sends_exact_pcm_windows_and_preserves_uncertainties(self):
        from yandex_speech import SpeechTranscriber
        from yandex_speech.config import RuntimeContext

        class Response:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def json(self):
                return {"result": self.text}

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, *, params, data, timeout):
                self.calls.append((params, bytes(data)))
                start = struct.unpack("<h", data[:2])[0]
                return Response({0: "Да да", 25: "да да да", 50: "да да хвост"}[start])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for seconds, starts in [(29, [0]), (30, [0]), (31, [0, 25]),
                                    (55, [0, 25]), (56, [0, 25, 50])]:
                with self.subTest(seconds=seconds):
                    source = root / (str(seconds) + ".wav")
                    samples = array.array("h", (second for second in range(seconds) for _ in range(16000)))
                    with wave.open(str(source), "wb") as output:
                        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                        output.writeframes(samples.tobytes())
                    original = source.read_bytes()
                    client = SpeechTranscriber(api_key="synthetic-test-key",
                                               _context=RuntimeContext(user_home=root))
                    session = client.session = Session()
                    result = client.transcribe_chunked_direct(source, literature_text=False)
                    self.assertEqual([struct.unpack("<h", data[:2])[0] for _, data in session.calls], starts)
                    self.assertEqual([len(data) for _, data in session.calls],
                                     [min(30, seconds - start) * 32000 for start in starts])
                    self.assertTrue(all(params["format"] == "lpcm" and params["sampleRateHertz"] == "16000"
                                        and params["profanityFilter"] == "false" for params, _ in session.calls))
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(result["metadata_availability"]["timing"], "none")
                    self.assertTrue(all(s["start"] is None and s["end"] is None for s in result["segments"]))
                    self.assertEqual([w["start_seconds"] for w in result["raw"]["windows"]], starts)
                    if seconds > 30:
                        self.assertTrue(result["uncertainties"])
                    self.assertTrue(result["input_complete"])
