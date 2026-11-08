import copy
import json
import tempfile
import unittest
from pathlib import Path


class AcceptanceRunnerTests(unittest.TestCase):
    def test_missing_acceptance_and_empty_selector_are_rejected(self):
        from tests.__main__ import read_manifest
        source=Path(__file__).with_name("acceptance.json")
        original=json.loads(source.read_text())
        for mutate in ["missing","empty"]:
            document=copy.deepcopy(original)
            if mutate == "missing":
                del document["acceptance"]["AC01"]
            else:
                document["acceptance"]["AC01"]["offline"]=[]
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/"manifest.json"
                path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    read_manifest(path)

    def test_unknown_test_selector_is_not_a_successful_zero_test_run(self):
        from tests.__main__ import selected_tests
        with self.assertRaises(ValueError):
            selected_tests(["tests.this_module_does_not_exist"])

    def test_skipped_required_test_is_failed_evidence(self):
        from tests.__main__ import EvidenceResult
        import io
        class Skipped(unittest.TestCase):
            @unittest.skip("synthetic unavailable requirement")
            def test_required(self):
                pass
        result=unittest.TextTestRunner(stream=io.StringIO(),resultclass=EvidenceResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Skipped))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(next(iter(result.outcomes.values())),"skipped")

    def test_synthesis_phase_requires_all_eight_acceptance_criteria(self):
        from tests.__main__ import read_manifest
        source = Path(__file__).with_name("acceptance.json")
        manifest = read_manifest(source, phase=3)
        self.assertEqual(set(manifest["acceptance"]),
                         {f"AC{i:02d}" for i in range(29, 37)})
        document = json.loads(source.read_text())
        del document["phases"]["3"]["acceptance"]["AC36"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                read_manifest(path, phase=3)

    def test_feedback_manifest_maps_current_release_and_excludes_deferred_work(self):
        from tests.__main__ import read_manifest, FEEDBACK_REQUIRED
        path = Path(__file__).with_name("acceptance.json")
        document = read_manifest(path, increment="feedback")
        self.assertEqual(set(document["acceptance"]), FEEDBACK_REQUIRED)
        self.assertTrue({"AC21", "AC26", "AC27", "AC55", "AC61", "AC65", "AC66"} <= FEEDBACK_REQUIRED)
        self.assertTrue({"AC59", "AC69", "AC70", "AC71", "AC72", "AC73"}.isdisjoint(FEEDBACK_REQUIRED))

    def test_feedback_missing_or_extra_deferred_requirement_is_rejected(self):
        from tests.__main__ import read_manifest
        source = json.loads(Path(__file__).with_name("acceptance.json").read_text())
        for mode in ("missing", "deferred"):
            value = copy.deepcopy(source)
            if mode == "missing":
                del value["increments"]["feedback"]["acceptance"]["AC26"]
            else:
                value["increments"]["feedback"]["acceptance"]["AC69"] = {"offline": ["tests.test_streaming"]}
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    read_manifest(path, increment="feedback")

    def test_review_receipt_requires_the_heard_artifact_and_current_synthesis_sources(self):
        from tests.__main__ import read_reviews, source_digest
        from tests.provider.test_voice_quality import comparison_cases
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = []
            for case in comparison_cases():
                audio = root / (case["id"] + ".mp3")
                audio.write_bytes(("synthetic-" + case["id"]).encode())
                artifacts.append({"path": audio.name, "case_id": case["id"], "verdict": "passed",
                                  "sha256": hashlib.sha256(audio.read_bytes()).hexdigest()})
            receipt = root / "review.json"
            record = {"schema_version": 1, "source_digest": source_digest(synthesis=True),
                "reviews": {"voice-quality": {"verdict": "passed", "reviewer": "operator",
                    "note": "Explicit listening verdict", "artifacts": artifacts}}}
            receipt.write_text(json.dumps(record))
            self.assertEqual(read_reviews(receipt, {"voice-quality"})["voice-quality"], "passed")
            original = copy.deepcopy(record)
            record["reviews"]["voice-quality"]["artifacts"][0]["sha256"] = "0" * 64
            receipt.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                read_reviews(receipt, {"voice-quality"})
            record = copy.deepcopy(original)
            record["source_digest"] = "stale"
            receipt.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                read_reviews(receipt, {"voice-quality"})
            record = copy.deepcopy(original)
            record["reviews"]["voice-quality"]["artifacts"].pop()
            receipt.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                read_reviews(receipt, {"voice-quality"})
            record = copy.deepcopy(original)
            record["reviews"]["voice-quality"]["artifacts"][0]["verdict"] = "failed"
            receipt.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                read_reviews(receipt, {"voice-quality"})

    def test_reused_provider_evidence_must_match_sources_runtime_and_real_opt_in(self):
        from tests.__main__ import read_provider_evidence
        value = {"schema_version": 1, "profile": "grpc", "provider_opt_in": True, "host_opt_in": True,
                 "source_digest": "current", "hermes": {"revision": "stock", "worktree_clean": True}, "python": __import__("platform").python_version(),
                 "packages": {"requests": "tested"}, "test_count": 1,
                 "outcomes": {"tests.provider.synthetic.case": "passed"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps(value))
            self.assertEqual(read_provider_evidence(path, "current", {"revision": "stock", "worktree_clean": True}, {"requests": "tested"})["outcomes"],
                             value["outcomes"])
            dirty = copy.deepcopy(value)
            dirty["hermes"]["worktree_clean"] = False
            path.write_text(json.dumps(dirty))
            with self.assertRaises(ValueError):
                read_provider_evidence(path, "current", {"revision": "stock", "worktree_clean": True}, {"requests": "tested"})
            for field, bad in (("source_digest", "stale"), ("provider_opt_in", False), ("test_count", 0), ("python", "unmatched-runtime")):
                record = copy.deepcopy(value)
                record[field] = bad
                path.write_text(json.dumps(record))
                with self.assertRaises(ValueError):
                    read_provider_evidence(path, "current", {"revision": "stock", "worktree_clean": True}, {"requests": "tested"})

    def test_increment_cli_is_explicit_and_long_file_permission_needs_provider_opt_in(self):
        from tests.__main__ import parse_args
        import contextlib,io
        self.assertEqual(parse_args(["--increment", "feedback"]).increment, "feedback")
        self.assertEqual(parse_args(["--phase", "3"]).phase, 3)
        for args in (["--phase", "3", "--increment", "feedback"],
                     ["--increment", "feedback", "--maximum-recording"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)

    def test_local_audio_playback_is_not_a_telegram_delivery_receipt(self):
        from tests.__main__ import read_reviews, source_digest
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "heard.mp3"
            audio.write_bytes(b"synthetic")
            record = {"schema_version": 1, "source_digest": source_digest(synthesis=True),
                "reviews": {"telegram-delivery": {"verdict": "passed", "reviewer": "operator",
                    "note": "Heard locally", "route": "local", "delivered": True, "heard": True,
                    "timings": {"synthesis_seconds": 0.1, "delivery_seconds": 0.2},
                    "timing_basis": "separate observed synthesis and addressed delivery wall times",
                    "artifacts": [{"path": audio.name, "sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}]}}}
            receipt = root / "review.json"
            receipt.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                read_reviews(receipt, {"telegram-delivery"})
            record["reviews"]["telegram-delivery"]["route"] = "telegram"
            receipt.write_text(json.dumps(record))
            self.assertEqual(read_reviews(receipt, {"telegram-delivery"})["telegram-delivery"], "passed")

    def test_telegram_receipt_needs_separate_valid_synthesis_and_delivery_observations(self):
        from tests.__main__ import read_reviews, source_digest
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "heard.mp3"
            audio.write_bytes(b"synthetic")
            item = {"verdict": "passed", "reviewer": "operator", "note": "Heard in Telegram",
                    "route": "telegram", "delivered": True, "heard": True,
                    "timings": {"synthesis_seconds": 0.5, "delivery_seconds": 0.25},
                    "timing_basis": "separate observed tool completion and platform acknowledgement",
                    "artifacts": [{"path": audio.name, "sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}]}
            for wrong in (None, {}, {"synthesis_seconds": 1},
                          {"synthesis_seconds": -1, "delivery_seconds": 0},
                          {"synthesis_seconds": 1, "delivery_seconds": float("inf")},
                          {"synthesis_seconds": True, "delivery_seconds": 0}):
                record = copy.deepcopy(item)
                record["timings"] = wrong
                receipt = root / "review.json"
                receipt.write_text(json.dumps({"schema_version": 1, "source_digest": source_digest(synthesis=True),
                                               "reviews": {"telegram-delivery": record}}))
                with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                    read_reviews(receipt, {"telegram-delivery"})
