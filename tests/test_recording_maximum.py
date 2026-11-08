"""Offline rejection fixtures for the maximum uploaded-recording proof."""
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import wave

from yandex_speech.partition_audio import frame_windows
from tests.provider.test_recording_maximum import write_sparse_recording, validate_result


def accepted_result():
    total = 14456 * 16000
    windows = frame_windows(total, 1800 * 16000, 16000)
    placements = [0] + [part.start - 28 * 16000 for part in windows[1:]] + [14400 * 16000]
    parts, children = [], []
    for i, window in enumerate(windows):
        path = Path("/synthetic") / str(i) / "job.json"
        digest = "a" * 64
        part = {"start_frame": window.start, "end_frame": window.end, "status": "completed",
                "child_manifest_path": str(path), "child_job_id": f"child-{i}", "operation_id": f"operation-{i}",
                "saved_result_reference": str(path.parent / "result.json"), "saved_result_sha256": digest}
        cached = {"input_complete": True, "status": "completed", "mode": "async", "api_version": "3",
                  "job_id": part["child_job_id"], "operation_id": part["operation_id"]}
        children.append(SimpleNamespace(manifest_path=path, data={"result_sha256": digest},
            cached_result=lambda value=cached: value))
        parts.append(part)
    segments = []
    labels = ("повтори", "припев", "повтори", "припев", "контрольный", "хвост")
    for placement in placements:
        start = placement / 16000
        words = [{"word": word, "start": start + offset, "end": start + offset + 0.4}
                 for word, offset in zip(labels, (1, 2, 25, 26, 52, 53))]
        segments.append({"start": words[0]["start"], "end": words[-1]["end"],
                         "text": " ".join(labels), "words": words, "timing_source": "provider"})
    result = {"input_complete": True, "status": "completed", "mode": "async-partitioned",
              "completion_kind": "recorded-file", "text": " ".join(s["text"] for s in segments),
              "parts": parts, "segments": segments}
    job = SimpleNamespace(child=lambda index: children[index])
    return result, total, placements, windows, job, children


class MaximumRecordingContractTests(unittest.TestCase):
    def test_sparse_fixture_has_the_exact_declared_frames_and_marker_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            write_sparse_recording(path, b"\x01\x00" * 4, 40, [0, 18, 36], rate=10)
            with wave.open(str(path), "rb") as source:
                self.assertEqual((source.getnframes(), source.getframerate(), source.getnchannels()), (40, 10, 1))
                data = source.readframes(40)
            self.assertEqual(data[:8], b"\x01\x00" * 4)
            self.assertEqual(data[36:44], b"\x01\x00" * 4)
            self.assertEqual(data[-8:], b"\x01\x00" * 4)
            with self.assertRaises(FileExistsError):
                write_sparse_recording(path, b"\0\0", 40, [0], rate=10)

    def test_complete_receipts_and_individually_timed_markers_pass(self):
        result, total, placements, windows, job, _ = accepted_result()
        self.assertTrue(validate_result(result, total, placements, windows, job))

    def test_failed_running_missing_or_mismatched_children_cannot_pass(self):
        for mode in ("failed", "running", "missing-child", "missing-reference", "missing-cache", "wrong-job", "geometry"):
            result, total, placements, windows, job, children = accepted_result()
            if mode in ("failed", "running"): result["parts"][0]["status"] = mode
            elif mode == "missing-child": children[0] = None
            elif mode == "missing-reference": result["parts"][0].pop("saved_result_sha256")
            elif mode == "missing-cache": children[0].cached_result = lambda: None
            elif mode == "wrong-job": children[0].cached_result()["job_id"] = "foreign"
            else: result["parts"][1]["end_frame"] -= 1
            with self.subTest(mode=mode), self.assertRaises(AssertionError):
                validate_result(result, total, placements, windows, job)

    def test_collapsed_shifted_or_untimed_markers_cannot_fake_original_positions(self):
        for mode in ("collapse", "shift-first", "double-offset", "missing-time", "duplicate-word"):
            result, total, placements, windows, job, _ = accepted_result()
            if mode == "collapse":
                for segment in result["segments"]:
                    for word in segment["words"]:
                        word["start"], word["end"] = 14450.0, 14450.4
            elif mode == "shift-first":
                for word in result["segments"][0]["words"]:
                    word["start"] += placements[1] / 16000
                    word["end"] += placements[1] / 16000
            elif mode == "double-offset":
                result["segments"][-1]["words"][0]["end"] += 14400
            elif mode == "missing-time":
                result["segments"][1]["words"][0]["end"] = None
            else:
                result["segments"][0]["words"].append(copy.deepcopy(result["segments"][0]["words"][0]))
            with self.subTest(mode=mode), self.assertRaises(AssertionError):
                validate_result(result, total, placements, windows, job)

    def test_overlapping_provider_hypotheses_do_not_require_a_flat_word_order(self):
        result, total, placements, windows, job, _ = accepted_result()
        original = result["segments"][1]
        start = original["words"][0]["start"] + 0.1
        alternative = {"start": start, "end": start + 0.4, "text": "слово",
                       "timing_source": "provider",
                       "words": [{"word": "слово", "start": start, "end": start + 0.4}]}
        result["segments"].insert(2, alternative)
        result["uncertainties"] = [{"selection": "retain-both", "overlap_start": start, "overlap_end": start + 1}]
        self.assertTrue(validate_result(result, total, placements, windows, job))

    def test_native_mp3_control_tail_must_have_its_own_late_word_times(self):
        from tests.provider.test_recording import late_control_tail
        first = {"timing_source": "provider", "text": "контрольный хвост",
                 "words": [{"word": "контрольный", "start": 1, "end": 2},
                           {"word": "хвост", "start": 2, "end": 3}]}
        late = {"timing_source": "provider", "text": "незавершённый фрагмент",
                "words": [{"word": "фрагмент", "start": 1890, "end": 1891}]}
        result = {"segments": [first, late]}
        self.assertFalse(late_control_tail(result, 1800))
        result["segments"].append({"timing_source": "provider", "text": "контрольный хвост",
            "words": [{"word": "контрольный", "start": 1897, "end": 1898},
                      {"word": "хвост", "start": 1898, "end": 1899}]})
        self.assertTrue(late_control_tail(result, 1800))
