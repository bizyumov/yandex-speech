import copy
import json
import tempfile
import unittest
from pathlib import Path



def native_review_fixture(root):
    """Synthetic schema fixtures, never live-generation or listening evidence."""
    import hashlib
    from tests.__main__ import source_digest
    from tests.issue83_cases import native_cases, alena_cases, rollover_cases, automatic_cases
    groups = [("issue83-native-matrix", native_cases() + alena_cases() + rollover_cases()),
              ("issue83-native-automatic", automatic_cases())]
    artifacts, references = [], []
    for scope, cases in groups:
        rows = []
        for case in cases:
            audio = root / (case["id"] + ".mp3")
            audio.write_bytes(("synthetic parser fixture " + case["id"]).encode())
            sha = hashlib.sha256(audio.read_bytes()).hexdigest()
            artifacts.append({"path": audio.name, "case_id": case["id"], "verdict": "passed", "sha256": sha})
            rows.append({"id": case["id"], "delivered_file": audio.name, "sha256": sha,
                         "status": "passed-native-generation-delivery"})
        document = {"schema_version": 1, "scope": scope, "source_digest": source_digest(),
            "synthesis_source_digest": source_digest(synthesis=True), "sources_unchanged": True,
            "credentials_unchanged": True, "rollback": {"restored": True},
            "all_generation_delivery_cases_passed": True,
            "expected_case_ids": [case["id"] for case in cases], "cases": rows}
        native = root / (scope + ".json")
        native.write_text(json.dumps(document))
        references.append({"path": native.name, "sha256": hashlib.sha256(native.read_bytes()).hexdigest()})
    return {"schema_version": 1, "source_digest": source_digest(synthesis=True),
        "reviews": {"issue83-native-quality": {"verdict": "passed", "reviewer": "synthetic-parser-fixture",
            "note": "Schema test only; no real generation or listening.",
            "artifacts": artifacts, "native_receipts": references}}}

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
        self.assertTrue({"AC21", "AC26", "AC27", "AC61", "AC65", "AC66"} <= FEEDBACK_REQUIRED)
        self.assertTrue({"AC55", "AC59", "AC69", "AC70", "AC71", "AC72", "AC73"}.isdisjoint(FEEDBACK_REQUIRED))
        self.assertEqual(document["acceptance"]["AC65"]["offline"], ["tests.test_voice_catalog"])
        reviews = {name for record in document["acceptance"].values() for name in record.get("review", [])}
        self.assertEqual(reviews, {"telegram-delivery"})

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

    def test_issue83_maps_all_criteria_and_requires_native_and_listener_evidence(self):
        from tests.__main__ import read_manifest, ISSUE83_REQUIRED, parse_args
        document = read_manifest(Path(__file__).with_name("acceptance.json"), increment="issue83")
        self.assertEqual(set(document["acceptance"]), ISSUE83_REQUIRED)
        self.assertEqual(len(ISSUE83_REQUIRED), 11)
        self.assertTrue(all(record["offline"] for record in document["acceptance"].values()))
        self.assertTrue(document["acceptance"]["AC83-11"]["host"])
        self.assertEqual(document["acceptance"]["AC83-11"]["review"], ["issue83-native-quality"])
        self.assertEqual(parse_args(["--increment", "issue83"]).increment, "issue83")
        original = json.loads(Path(__file__).with_name("acceptance.json").read_text())
        del original["increments"]["issue83"]["acceptance"]["AC83-11"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(original))
            with self.assertRaises(ValueError):
                read_manifest(path, increment="issue83")

    def test_live_only_collection_keeps_offline_evidence_pending(self):
        from tests.__main__ import parse_args
        args = parse_args(["--increment", "issue83", "--provider", "--host", "--live-only"])
        self.assertTrue(args.live_only and args.provider and args.host)
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--increment", "issue83", "--live-only"])

    def test_native_collection_stops_after_failure_while_offline_collects_all_results(self):
        import contextlib
        import io
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        from tests import __main__ as runner
        for host in (True, False):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                events = []
                class SyntheticCases(unittest.TestCase):
                    def test_first(self):
                        events.append("first")
                        self.fail("synthetic native failure; later profile work must not start")
                    def test_later(self):
                        events.append("later")
                first, later = SyntheticCases("test_first"), SyntheticCases("test_later")
                cases = {first.id(): first, later.id(): later}
                evidence = Path(directory) / "synthetic-runner.json"
                manifest = {"scope": "synthetic-control-fixture", "acceptance": {
                    "synthetic": {"offline": [], "provider": [], "host": ["synthetic-cases"]}}}
                arguments = ["--increment", "issue83", "--evidence", str(evidence)]
                if host:
                    arguments += ["--provider", "--host", "--live-only"]
                with patch.dict(os.environ), patch.object(runner, "validate_runtime"), \
                        patch.object(runner, "read_manifest", return_value=manifest), \
                        patch.object(runner, "selected_tests", return_value=cases), \
                        patch.object(runner, "read_reviews", return_value={}), \
                        patch.object(runner, "source_digest", return_value="synthetic"), \
                        patch.object(runner, "_version", return_value="synthetic"), \
                        patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(runner.main(arguments), 1)
                self.assertEqual(events, ["first"] if host else ["first", "later"])
                record = json.loads(evidence.read_text())
                self.assertEqual(record["test_count"], 1 if host else 2)
                self.assertEqual(record["outcomes"][first.id()], "failed")
                if host:
                    self.assertNotIn(later.id(), record["outcomes"])

    def test_issue83_listening_cannot_omit_or_repeat_a_required_comparison(self):
        from tests.__main__ import read_reviews
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = native_review_fixture(root)
            receipt = root / "review.json"
            receipt.write_text(json.dumps(original))
            self.assertEqual(read_reviews(receipt, {"issue83-native-quality"})["issue83-native-quality"], "passed")
            for mode in ("missing", "failed", "repeated"):
                record = copy.deepcopy(original)
                items = record["reviews"]["issue83-native-quality"]["artifacts"]
                if mode == "missing":
                    items.pop()
                elif mode == "failed":
                    items[0]["verdict"] = "failed"
                else:
                    items[-1] = copy.deepcopy(items[0])
                receipt.write_text(json.dumps(record))
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    read_reviews(receipt, {"issue83-native-quality"})

    def test_native_listening_rejects_wrong_audio_and_stale_generation(self):
        from tests.__main__ import read_reviews
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = native_review_fixture(root)
            receipt = root / "review.json"
            wrong = root / "unrelated.mp3"
            wrong.write_bytes(b"unrelated synthetic parser fixture")
            record = copy.deepcopy(original)
            item = record["reviews"]["issue83-native-quality"]["artifacts"][0]
            item.update(path=wrong.name, sha256=hashlib.sha256(wrong.read_bytes()).hexdigest())
            receipt.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "not the native-generated"):
                read_reviews(receipt, {"issue83-native-quality"})
            reference = original["reviews"]["issue83-native-quality"]["native_receipts"][0]
            native = root / reference["path"]
            document = json.loads(native.read_text())
            document["source_digest"] = "stale"
            native.write_text(json.dumps(document))
            reference["sha256"] = hashlib.sha256(native.read_bytes()).hexdigest()
            receipt.write_text(json.dumps(original))
            with self.assertRaisesRegex(ValueError, "stale"):
                read_reviews(receipt, {"issue83-native-quality"})

    def test_explicit_listening_gate_precedes_auto_but_cannot_commission_final(self):
        from tests.__main__ import read_reviews, parse_args
        from tests.issue83_cases import native_cases, alena_cases, rollover_cases
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = native_review_fixture(root)
            review = record["reviews"].pop("issue83-native-quality")
            explicit = {case["id"] for case in native_cases() + alena_cases() + rollover_cases()}
            review["artifacts"] = [item for item in review["artifacts"] if item["case_id"] in explicit]
            review["native_receipts"] = review["native_receipts"][:1]
            record["reviews"]["issue83-explicit-quality"] = review
            receipt = root / "review.json"
            receipt.write_text(json.dumps(record))
            self.assertEqual(read_reviews(receipt, {"issue83-explicit-quality"})["issue83-explicit-quality"], "passed")
            args = parse_args(["--increment", "issue83", "--review-evidence", str(receipt),
                               "--validate-explicit-review"])
            self.assertTrue(args.validate_explicit_review)
            record["reviews"] = {"issue83-native-quality": review}
            receipt.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                read_reviews(receipt, {"issue83-native-quality"})

    def test_listening_montage_must_contain_the_native_audio_excerpts(self):
        from tests.__main__ import verify_native_montage
        from yandex_speech.tts_audio import wrap_pcm
        from unittest.mock import patch
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "listen.wav"
            pcm = b"\1\0" * 48
            audio.write_bytes(wrap_pcm(pcm, 48000))
            montage = {"path": audio.name, "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                       "tail_seconds": 8, "gap_seconds": 1}
            with patch("tests.__main__.native_tail_pcm", return_value=pcm):
                verify_native_montage(root, montage, [{"case_id": "fixture"}],
                                      {"fixture": {"path": root / "original.ogg"}})
                audio.write_bytes(wrap_pcm(b"\2\0" * 48, 48000))
                montage["sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
                with self.assertRaisesRegex(ValueError, "does not contain"):
                    verify_native_montage(root, montage, [{"case_id": "fixture"}],
                                          {"fixture": {"path": root / "original.ogg"}})

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
