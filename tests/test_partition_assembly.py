"""Timed overlap, original-media offsets, and request-local speaker identities."""
import copy
from types import SimpleNamespace
import unittest

from yandex_speech.errors import SpeechError
from yandex_speech.output import render_srt, render_txt
from yandex_speech.partition_assembly import assemble_parts
from yandex_speech.partition_jobs import MODE


def segment(text, times, *, words=None, speaker=None):
    labels = words if words is not None else text.split()
    return {"text": text, "start": times[0][0] if times else None,
            "end": times[-1][1] if times else None, "speaker": speaker, "speaker_tag": speaker,
            "channel_tag": "1", "words": [
                {"word": word, "start": start, "end": end}
                for word, (start, end) in zip(labels, times)], "words_representation": "raw",
            "timing_source": "provider", "speaker_source": "provider" if speaker else "none"}


def fixture(groups, *, origin=3.5, diarize=False):
    parts, results = [], []
    for index, segments in enumerate(groups):
        part = {"part_id": str(index), "start_frame": index * 16000, "end_frame": (index + 6) * 16000,
                "offset": float(index), "child_job_id": f"child-{index}", "operation_id": f"operation-{index}",
                "status": "completed" if segments else "no_speech"}
        parts.append(part)
        results.append({"schema_version": 1, "status": part["status"], "input_complete": True,
                        "mode": "async", "api_version": "3", "transport": "rest",
                        "job_id": part["child_job_id"], "operation_id": part["operation_id"],
                        "text": " ".join(item["text"] for item in segments), "segments": segments,
                        "source_info": {"timeline_offset_seconds": 0}, "uncertainties": [], "raw": [{"unaltered": index}],
                        "metadata_availability": {"text_representation": "literary"}})
    job = SimpleNamespace(parts=parts, job_id="parent", manifest_path="/private/parent/job.json",
        operation_ids=[part["operation_id"] for part in parts],
        data={"preparation_complete": True, "source_info": {"timeline_offset_seconds": origin},
              "parameters": {"diarize": diarize, "timestamps": True}, "transport": "rest", "routing": {}})
    return job, results


class PartitionAssemblyTests(unittest.TestCase):
    def test_timed_overlap_is_removed_and_nonzero_original_offset_is_applied_once(self):
        left = segment("Начало общая фраза конец.", [(.2, .4), (1.5, 1.8), (2.5, 2.8), (5.5, 5.8)])
        right = segment("Общая фраза конец новый хвост.", [(.5, .8), (1.5, 1.8), (4.5, 4.8), (5.1, 5.3), (5.5, 5.8)])
        job, children = fixture([[left], [right]])
        original = copy.deepcopy(children)
        result = assemble_parts(job, children)
        self.assertEqual(result["text"], "Начало общая фраза конец. новый хвост.")
        self.assertEqual(result["segments"][0]["start"], 3.7)
        self.assertEqual(result["segments"][1]["start"], 9.6)
        self.assertEqual(result["segments"][1]["words"][0]["start"], 9.6)
        self.assertEqual(result["segments"][1]["end"], 10.3)
        self.assertEqual(result["assembly_boundaries"][0]["matched_words"], 3)
        self.assertEqual(result["uncertainties"], [])
        self.assertEqual(children, original)
        self.assertEqual(result["raw"][1]["result"], original[1])
        self.assertEqual(result["mode"], MODE)
        self.assertIn("00:00:09,600", render_srt(result))

    def test_cut_word_replaced_only_by_unique_timed_continuation(self):
        left = segment("Начало общая фраза контрол", [(.2, .4), (1.5, 1.8), (2.5, 2.8), (5.5, 6.0)])
        right = segment("Общая фраза контрольный хвост.", [(.5, .8), (1.5, 1.8), (4.5, 5.4), (5.5, 5.8)])
        job, children = fixture([[left], [right]])
        result = assemble_parts(job, children)
        self.assertEqual(result["text"], "Начало общая фраза контрольный хвост.")
        self.assertEqual(result["assembly_boundaries"][0]["kind"], "overlap-cut-word-time")
        self.assertEqual(result["segments"][0]["end"], 6.3)

    def test_same_phrase_at_another_time_is_preserved(self):
        left = segment("Общая фраза.", [(2, 2.2), (3, 3.2)])
        right = segment("Общая фраза.", [(5, 5.2), (5.5, 5.7)])
        job, children = fixture([[left], [right]])
        result = assemble_parts(job, children)
        self.assertEqual(result["text"], "Общая фраза. Общая фраза.")
        self.assertEqual(result["uncertainties"][0]["selection"], "retain-both")

    def test_nearby_disjoint_and_weakly_overlapping_repeated_phrases_are_preserved(self):
        for shift in (0.24, 0.20, 0.075):
            with self.subTest(shift=shift):
                left = segment("Да да.", [(5, 5.1), (5.11, 5.21)])
                right = segment("Да да.", [(4 + shift, 4.1 + shift), (4.11 + shift, 4.21 + shift)])
                job, children = fixture([[left], [right]])
                result = assemble_parts(job, children)
                self.assertEqual(result["text"], "Да да. Да да.")
                self.assertEqual(result["uncertainties"][0]["selection"], "retain-both")

    def test_partial_word_away_from_window_eof_is_not_assumed_to_be_clipped(self):
        left = segment("Общая фраза контрол", [(1.5, 1.8), (2.5, 2.8), (4.5, 4.8)])
        right = segment("Общая фраза контрольный хвост.", [(.5, .8), (1.5, 1.8), (3.5, 4.2), (5.5, 5.8)])
        job, children = fixture([[left], [right]])
        result = assemble_parts(job, children)
        self.assertEqual(result["uncertainties"][0]["selection"], "retain-both")
        self.assertIn("контрол Общая", result["text"])

    def test_fully_duplicate_middle_part_does_not_break_next_boundary(self):
        a = segment("Общая фраза.", [(2.2, 2.4), (2.7, 2.9)])
        b = segment("Общая фраза.", [(1.2, 1.4), (1.7, 1.9)])
        c = segment("Общая фраза. Хвост.", [(.2, .4), (.7, .9), (4.1, 4.4)])
        job, children = fixture([[a], [b], [c]])
        result = assemble_parts(job, children)
        self.assertEqual(result["text"], "Общая фраза. Хвост.")
        self.assertEqual([note["matched_words"] for note in result["assembly_boundaries"]], [2, 2])

    def test_literary_number_mapping_is_not_invented_from_raw_words(self):
        left = segment("В 2026 году.", [(1, 1.2), (2, 2.2), (3, 3.2), (4, 4.2), (5, 5.2), (5.5, 5.8)],
                       words=["В", "две", "тысячи", "двадцать", "шестом", "году"])
        right = segment("2026 году. Хвост.", [(0, .2), (1, 1.2), (2, 2.2), (3, 3.2), (4, 4.2), (5.5, 5.8)],
                        words=["две", "тысячи", "двадцать", "шестом", "году", "Хвост"])
        job, children = fixture([[left], [right]])
        result = assemble_parts(job, children)
        self.assertEqual(result["text"], "В 2026 году. 2026 году. Хвост.")
        self.assertEqual(result["uncertainties"][0]["kind"], "overlap-unresolved")
        self.assertEqual(result["raw"][0]["result"]["segments"][0]["text"], left["text"])

    def test_anonymous_speakers_are_namespaced_in_json_txt_and_subtitles(self):
        a = segment("Первый.", [(0, .5)], speaker="1")
        b = segment("Второй.", [(5, 5.5)], speaker="1")
        job, children = fixture([[a], [b]], diarize=True)
        result = assemble_parts(job, children)
        self.assertEqual([item["speaker"] for item in result["segments"]], ["part-0/1", "part-1/1"])
        self.assertEqual(result["metadata_availability"]["speaker_scope"], "partition-job-local")
        self.assertIn("Speaker part-0/1", render_txt(result))
        self.assertIn("Speaker part-1/1", render_srt(result))
        self.assertEqual(result["raw"][1]["result"]["segments"][0]["speaker"], "1")

    def test_missing_or_incomplete_child_cannot_be_assembled(self):
        job, children = fixture([[segment("Хвост.", [(0, .5)])], []])
        for variant in (children[:-1], [children[0], dict(children[1], input_complete=False)],
                        [children[0], dict(children[1], operation_id="foreign")]):
            with self.subTest(variant=variant), self.assertRaises(SpeechError):
                assemble_parts(job, variant)

    def test_completed_and_no_speech_parts_are_both_required(self):
        job, children = fixture([[], [segment("Хвост.", [(5, 5.5)])]])
        result = assemble_parts(job, children)
        self.assertTrue(result["input_complete"])
        self.assertEqual(result["text"], "Хвост.")
        self.assertEqual(result["parts"][0]["status"], "no_speech")
