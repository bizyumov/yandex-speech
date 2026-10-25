import unittest

from yandex_speech.errors import SpeechError
from yandex_speech.results import ResultAccumulator
from yandex_speech.output import render_txt, render_srt, render_vtt


def final(index, text, channel="0", start=0, end=1000):
    return {"audio_cursors": {"final_index": index}, "channel_tag": channel,
            "final": {"alternatives": [{"text": text, "start_time_ms": start,
                                        "end_time_ms": end,
                                        "words": [{"text": text, "start_time_ms": start,
                                                   "end_time_ms": end}]}]}}


def refinement(index, text, channel="0"):
    return {"channel_tag": channel, "final_refinement": {
        "final_index": index, "normalized_text": {"alternatives": [{"text": text}]}}}


class ResultTests(unittest.TestCase):
    def test_refinements_replace_matching_segments_without_duplicates(self):
        acc = ResultAccumulator(literature_text=True, diarize=True)
        acc.feed(refinement(1, "Второй.", "1"))
        acc.feed(final(0, "двадцать три", start=0, end=500))
        acc.feed(final(1, "второй", "1", 500, 1000))
        acc.feed(refinement(0, "23."))
        data = acc.result(completion_kind="recorded-file")
        self.assertEqual(data["text"], "23. Второй.")
        self.assertEqual(len(data["segments"]), 2)
        self.assertEqual([x["speaker"] for x in data["segments"]], ["0", "1"])
        self.assertEqual(data["segments"][0]["words"][0]["word"], "двадцать три")
        self.assertEqual(data["segments"][0]["words_representation"], "raw")
        self.assertEqual(data["raw"][1]["final"]["alternatives"][0]["text"], "двадцать три")
        self.assertEqual(data["metadata_availability"]["text_representation"], "literary")

    def test_duplicates_deduplicate_and_conflicting_final_is_failure(self):
        acc = ResultAccumulator(literature_text=False)
        event = final(0, "repeat")
        acc.feed(event)
        acc.feed(event)
        self.assertEqual(acc.result()["text"], "repeat")
        with self.assertRaises(SpeechError):
            acc.feed(final(0, "conflict"))

    def test_channel_is_not_a_speaker_without_diarization(self):
        acc = ResultAccumulator(literature_text=False, diarize=False)
        acc.feed(final(0, "text", "1"))
        data = acc.result()
        self.assertIsNone(data["segments"][0]["speaker"])
        self.assertEqual(data["metadata_availability"]["speaker"], "none")

    def test_literary_request_without_refinement_is_honest(self):
        acc = ResultAccumulator(literature_text=True)
        acc.feed(final(0, "raw words"))
        data = acc.result()
        self.assertEqual(data["text"], "raw words")
        self.assertEqual(data["metadata_availability"]["text_representation"], "raw-unfulfilled")
        self.assertTrue(data["uncertainties"])

    def test_empty_success_and_genuine_subtitle_export(self):
        empty = ResultAccumulator().result()
        self.assertEqual(empty["status"], "no_speech")
        self.assertEqual(render_srt(empty), "")
        acc = ResultAccumulator(literature_text=False, diarize=True, timestamps=True)
        acc.feed(final(0, "Text", "0", 250, 1250))
        data = acc.result()
        self.assertIn("00:00:00,250 --> 00:00:01,250", render_srt(data))
        self.assertIn("00:00:00.250 --> 00:00:01.250", render_vtt(data))
        self.assertIn("Speaker 0", render_txt(data))
        data["segments"][0]["start"] = None
        with self.assertRaises(SpeechError):
            render_srt(data)
