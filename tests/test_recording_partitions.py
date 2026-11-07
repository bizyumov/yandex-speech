"""Fault injection across durable parent/child submission and recovery."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.test_partition_audio import wav
from yandex_speech import SpeechError, SpeechTranscriber
from yandex_speech.config import RuntimeContext
from yandex_speech.jobs import JobHandle
from yandex_speech.partition_jobs import PartitionJob
from yandex_speech.recording_partitions import start_partitioned, resume_partitioned, poll_partitioned


class Transport:
    def __init__(self):
        self.submitted = []
        self.before_submit = None
        self.cancelled = 0
        self.events = {}

    def bind_cancel_event(self, event):
        self.event = event

    def recognize_file(self, request, timeout):
        if self.before_submit is not None:
            self.before_submit()
        index = len(self.submitted)
        self.submitted.append(request)
        return {"id": f"accepted-{index}"}

    def get_operation(self, operation_id, timeout):
        return {"id": operation_id, "done": True}

    def get_recognition(self, operation_id, timeout):
        if operation_id in self.events:
            return iter(self.events[operation_id])
        return iter([{"audio_cursors": {"final_index": "0"}, "final": {"alternatives": [{
            "text": f"tail {operation_id}", "words": [
                {"text": "tail", "start_time_ms": "100", "end_time_ms": "300"},
                {"text": operation_id, "start_time_ms": "400", "end_time_ms": "800"}]}]}},
            {"final_refinement": {"final_index": "0", "normalized_text": {
                "alternatives": [{"text": f"Tail {operation_id}."}]}}}])

    def close(self):
        pass

    def cancel_all(self):
        self.cancelled += 1


class PartitionLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.wav"
        wav(self.source, frames=8 * 16000)
        self.transport = Transport()
        self.client = SpeechTranscriber(api_key="synthetic-key", job_dir=self.root / "jobs",
            _context=RuntimeContext(user_home=self.root),
            _transport_factory=lambda version, transport: self.transport)
        capacity = patch("yandex_speech.partition_audio.inline_frame_capacity", return_value=6 * 16000)
        capacity.start()
        self.addCleanup(capacity.stop)

    def start(self, **options):
        return start_partitioned(self.client, self.source, transport="rest", **options)

    def test_all_parts_and_child_link_are_durable_before_first_paid_call(self):
        observed = []
        def before():
            parents = [PartitionJob.load(path) for path in (self.root / "jobs").glob("*/job.json")
                       if json.loads(path.read_text()).get("mode") == "async-partitioned"]
            self.assertEqual(len(parents), 1)
            parent = parents[0]
            self.assertEqual(len(parent.parts), 3)
            self.assertTrue(all(Path(part["audio_path"]).is_file() for part in parent.parts))
            linked = [part for part in parent.parts if part.get("child_manifest_path")]
            child = JobHandle.load(linked[-1]["child_manifest_path"])
            self.assertEqual(child.status, "submitting")
            observed.append(child.job_id)
        self.transport.before_submit = before
        parent = self.start()
        self.assertEqual(len(set(observed)), 3)
        self.assertEqual(parent.operation_ids, ["accepted-0", "accepted-1", "accepted-2"])
        result = poll_partitioned(self.client, parent)
        self.assertTrue(result["input_complete"])
        self.assertEqual(len(result["parts"]), 3)
        self.assertTrue(all(part["saved_result_sha256"] for part in parent.parts))
        self.assertNotIn("synthetic-key", parent.manifest_path.read_text())

    def test_parent_save_failure_after_acceptance_recovers_without_rebilling(self):
        original = PartitionJob.save
        failed = False
        def save(parent):
            nonlocal failed
            if parent.data.get("operation_ids") and not failed:
                failed = True
                raise SpeechError("PREREQUISITE", "simulated parent checkpoint failure")
            return original(parent)
        with patch.object(PartitionJob, "save", save):
            with self.assertRaises(SpeechError) as raised:
                self.start()
        self.assertEqual(len(self.transport.submitted), 1)
        path = Path(raised.exception.receipt_path)
        self.assertEqual(PartitionJob.load(path).operation_ids, [])
        parent = resume_partitioned(self.client, path)
        self.assertEqual(parent.operation_ids, ["accepted-0", "accepted-1", "accepted-2"])
        self.assertEqual(len(self.transport.submitted), 3)

    def test_initial_submission_and_resume_share_one_parent_lock(self):
        attempts = []
        def accepted(parent):
            count = len(self.transport.submitted)
            with self.assertRaises(SpeechError) as raised:
                resume_partitioned(self.client, parent.manifest_path)
            self.assertEqual(raised.exception.category, "STATE")
            self.assertEqual(len(self.transport.submitted), count)
            attempts.append(count)
        self.start(_on_accepted=accepted)
        self.assertEqual(attempts, [1, 2, 3])

    def test_unknown_child_blocks_every_new_paid_call_on_resume(self):
        def uncertain():
            self.transport.submitted.append(b"possibly accepted")
            raise SpeechError("SUBMISSION_UNCERTAIN", "connection lost after send")
        self.transport.before_submit = uncertain
        with self.assertRaises(SpeechError) as raised:
            self.start()
        path = Path(raised.exception.receipt_path)
        self.transport.before_submit = None
        with self.assertRaises(SpeechError) as resumed:
            resume_partitioned(self.client, path)
        self.assertEqual(resumed.exception.category, "SUBMISSION_UNCERTAIN")
        self.assertEqual(len(self.transport.submitted), 1)
        self.assertFalse((path.parent / "result.json").exists())

    def test_cancellation_keeps_accepted_id_and_fresh_resume_only_sends_remaining_parts(self):
        captured = []
        def before():
            self.client.cancel(captured[0])
        self.transport.before_submit = before
        with self.assertRaises(SpeechError) as raised:
            self.start(_on_prepared=captured.append)
        self.assertEqual(raised.exception.category, "CANCELLED")
        parent = PartitionJob.load(raised.exception.receipt_path)
        self.assertEqual(parent.operation_ids, ["accepted-0"])
        self.assertEqual(len(self.transport.submitted), 1)
        self.assertGreaterEqual(self.transport.cancelled, 1)
        self.transport.before_submit = None
        parent = resume_partitioned(self.client, captured[0])
        self.assertFalse(parent.cancel_event.is_set())
        self.assertEqual(len(self.transport.submitted), 3)
        self.assertTrue(poll_partitioned(self.client, parent)["input_complete"])

    def test_cancel_after_preparation_prevents_any_submission(self):
        with self.assertRaises(SpeechError) as raised:
            self.start(_on_prepared=self.client.cancel)
        self.assertEqual(raised.exception.category, "CANCELLED")
        self.assertEqual(self.transport.submitted, [])

    def test_stale_missing_part_cannot_be_published_as_complete(self):
        parent = self.start()
        parent.data["parts"].pop()
        parent.save()
        with self.assertRaises(SpeechError):
            poll_partitioned(self.client, parent)
        self.assertFalse((parent.manifest_path.parent / "result.json").exists())

    def test_cached_aggregate_needs_no_media_children_or_credentials(self):
        parent = self.start()
        expected = poll_partitioned(self.client, parent)
        self.source.unlink()
        for part in parent.parts:
            Path(part["audio_path"]).unlink()
            Path(part["child_manifest_path"]).unlink()
        client = SpeechTranscriber(_context=RuntimeContext(user_home=self.root), _environment={})
        loaded = PartitionJob.load(parent.manifest_path)
        with patch.object(client, "_rpc", side_effect=AssertionError("cache must not call the provider")):
            self.assertEqual(poll_partitioned(client, loaded), expected)

    def test_diarization_requires_explicit_partition_local_consent_before_billing(self):
        with self.assertRaises(SpeechError) as raised:
            self.start(diarize=True)
        self.assertEqual(raised.exception.category, "PREREQUISITE")
        self.assertEqual(self.transport.submitted, [])

    def test_configured_storage_is_bypassed_only_by_explicit_partition_choice(self):
        self.client.adapter_defaults = {"object_storage": {"bucket": "existing-authorized-bucket"}}
        with patch.object(self.client, "_storage_uploader", side_effect=AssertionError("must remain inline")):
            parent = self.start()
        self.assertEqual(len(parent.operation_ids), 3)
        self.assertEqual(parent.data["owned_uploads"], [])

    def test_explicit_incompatible_version_and_storage_fail_without_billing(self):
        for options in ({"api_version": "1"}, {"api_version": "2"}, {"bucket": "explicit-bucket"},
                        {"use_storage": True}, {"allow_partitioned_speakers": 1}):
            with self.subTest(options=options), self.assertRaises(SpeechError) as raised:
                self.start(**options)
            self.assertEqual(raised.exception.category, "CONFIG")
        self.assertEqual(self.transport.submitted, [])

    def test_public_auto_keeps_poll_interval_out_of_submission_options(self):
        result = self.client.transcribe_auto(self.source, split_inline=True, transport="rest", poll_interval=0.01)
        self.assertTrue(result["input_complete"])
        self.assertEqual(len(self.transport.submitted), 3)

    def test_invalid_poll_interval_fails_before_partition_billing(self):
        for interval in (0, -1, 121, True, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaises(SpeechError) as raised:
                self.client.transcribe_auto(self.source, split_inline=True, transport="rest", poll_interval=interval)
            self.assertEqual(raised.exception.category, "CONFIG")
        self.assertEqual(self.transport.submitted, [])
