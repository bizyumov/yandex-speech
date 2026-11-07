"""Multipart acceptance authority and aggregate-cache crash boundaries."""
import copy
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.test_partition_audio import wav
from yandex_speech.errors import SpeechError
from yandex_speech.jobs import JobHandle
from yandex_speech.partition_jobs import MODE, PartitionJob


class PartitionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "recording.wav"
        wav(self.source, frames=16000 * 8)
        self.parameters = {"language": "ru-RU", "diarize": False,
                           "timestamps": False, "literature_text": True}
        self.parent = PartitionJob.create(
            self.root / "jobs", self.source, parameters=self.parameters,
            transport="rest", transports=("rest",), requested_transport="rest",
            deadline_at=time.monotonic() + 30)
        audio = self.parent.manifest_path.parent / "audio"
        audio.mkdir(mode=0o700)
        parts = []
        for index, (start, end) in enumerate(((0, 96000), (16000, 112000), (32000, 128000))):
            path = audio / f"part-{index:05d}.wav"
            wav(path, frames=end - start)
            parts.append({"part_id": str(index), "start_frame": start, "end_frame": end,
                          "offset": start / 16000, "duration_ms": (end - start) / 16,
                          "audio_path": str(path), "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "size_bytes": path.stat().st_size, "status": "prepared",
                          "child_manifest_path": None, "operation_id": None})
        info = {"original_path": str(self.source), "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "duration_ms": 8000, "decoded_frames": 128000, "partition_capacity_frames": 96000,
                "timeline_offset_seconds": 3.5, "inline_wire_limits": {"rest": 60000000},
                "prepared": {"container": "WAV", "sample_rate_hertz": 16000, "channels": 1}}
        with self.parent.lock():
            self.parent.install_preparation(info, parts)

    def child(self, index, *, status="running", accepted=True, cached=False):
        part = self.parent.data["parts"][index]
        info = {"original_path": part["audio_path"], "source_sha256": part["audio_sha256"],
                "duration_ms": part["duration_ms"], "timeline_offset_seconds": 0,
                "prepared": {"container": "WAV", "sample_rate_hertz": 16000, "channels": 1}}
        child = JobHandle.create(self.root / "children", info, parameters=self.parameters, transport="rest")
        if accepted:
            child.data["operation_ids"] = [f"cloud-operation-{index}"]
            child.parts[0]["operation_id"] = child.operation_id
        child.set_status(status)
        part.update(child_manifest_path=str(child.manifest_path), child_job_id=child.job_id)
        if cached:
            child.store_result({"schema_version": 1, "status": status, "input_complete": True,
                                "mode": "async", "api_version": "3", "transport": "rest",
                                "text": "" if status == "no_speech" else f"part {index}",
                                "segments": [], "source_info": info, "raw": [], "uncertainties": []})
        self.parent.save()
        return child

    def aggregate(self):
        return {"schema_version": 1, "status": "completed", "mode": MODE,
                "api_version": "3", "transport": "rest", "input_complete": True,
                "completion_kind": "recorded-file", "text": "complete aggregate",
                "segments": [], "raw": [], "uncertainties": [],
                "job_id": self.parent.job_id,
                "source_info": copy.deepcopy(self.parent.data["source_info"]),
                "parts": copy.deepcopy(self.parent.parts)}

    def complete_children(self):
        children = [self.child(index, status="no_speech" if index == 1 else "completed", cached=True)
                    for index in range(3)]
        self.parent.reconcile_children()
        return children

    def test_plan_rejects_missing_duplicate_shifted_or_escaped_parts(self):
        variants = []
        missing = copy.deepcopy(self.parent.data); missing["parts"].pop(); variants.append(missing)
        duplicate = copy.deepcopy(self.parent.data); duplicate["parts"][1] = duplicate["parts"][0]; variants.append(duplicate)
        shifted = copy.deepcopy(self.parent.data); shifted["parts"][1]["start_frame"] += 1; variants.append(shifted)
        escaped = copy.deepcopy(self.parent.data); escaped["parts"][0]["audio_path"] = "/foreign/part.wav"; variants.append(escaped)
        boolean = copy.deepcopy(self.parent.data); boolean["parts"][1]["offset"] = True; variants.append(boolean)
        for data in variants:
            with self.subTest(data=data["parts"]), self.assertRaises(SpeechError):
                PartitionJob.from_data(self.parent.manifest_path, data)

    def test_child_acceptance_is_recovered_from_a_stale_parent_snapshot(self):
        child = self.child(0)
        self.assertEqual(self.parent.data["operation_ids"], [])
        loaded = PartitionJob.load(self.parent.manifest_path)
        with loaded.lock():
            loaded.reconcile_children()
        self.assertEqual(loaded.data["operation_ids"], [child.operation_id])
        self.assertEqual(loaded.parts[0]["submission_outcome"], "accepted")
        self.assertEqual(loaded.parts[0]["status"], "running")

    def test_only_prepared_child_proves_no_submission(self):
        child = self.child(0, status="prepared", accepted=False)
        for status, expected in (("prepared", "never_sent"), ("submitting", "unknown"),
                                 ("failed", "unknown"), ("cancelled", "unknown"),
                                 ("submission_uncertain", "unknown")):
            with self.subTest(status=status):
                child.set_status(status)
                self.parent.reconcile_children()
                self.assertEqual(self.parent.parts[0]["submission_outcome"], expected)
                self.assertEqual(self.parent.data["operation_ids"], [])

    def test_child_source_and_request_policy_cannot_be_substituted(self):
        child = self.child(0)
        child.data["parameters"]["language"] = "en-US"
        child.save()
        with self.assertRaises(SpeechError) as caught:
            self.parent.reconcile_children()
        self.assertEqual(caught.exception.category, "STATE")

    def test_parent_commit_preserves_child_statuses_and_result_provenance(self):
        self.complete_children()
        before = copy.deepcopy(self.parent.parts)
        result = self.parent.store_result(self.aggregate())
        self.assertEqual(self.parent.parts, before)
        self.assertEqual(self.parent.parts[1]["status"], "no_speech")
        self.assertTrue(all(part["saved_result_reference"] != "result.json" for part in self.parent.parts))
        self.assertEqual(self.parent.cached_result(), result)

    def test_parent_cache_recovers_without_media_or_child_receipts(self):
        children = self.complete_children()
        result = self.parent.store_result(self.aggregate())
        self.source.unlink()
        for part, child in zip(self.parent.parts, children):
            Path(part["audio_path"]).unlink()
            child.manifest_path.unlink()
        loaded = PartitionJob.load(self.parent.manifest_path)
        with patch.object(JobHandle, "load", side_effect=AssertionError("cache must not load children")):
            self.assertEqual(loaded.cached_result(), result)

    def test_pending_parent_result_is_adopted_without_overwriting_child_references(self):
        self.complete_children()
        before = copy.deepcopy(self.parent.parts)
        with patch.object(self.parent, "_commit_result", side_effect=SpeechError("PREREQUISITE", "simulated crash")):
            with self.assertRaises(SpeechError):
                self.parent.store_result(self.aggregate())
        loaded = PartitionJob.load(self.parent.manifest_path)
        result = loaded.cached_result()
        self.assertTrue(result["input_complete"])
        self.assertEqual(loaded.parts, before)
        self.assertNotIn("pending_result_sha256", loaded.data)

    def test_missing_required_child_never_publishes_complete_aggregate(self):
        self.complete_children()
        result = self.aggregate()
        result["parts"].pop()
        with self.assertRaises(SpeechError) as caught:
            self.parent.store_result(result)
        self.assertEqual(caught.exception.category, "OUTPUT_PARTIAL")
        self.assertFalse((self.parent.manifest_path.parent / "result.json").exists())
