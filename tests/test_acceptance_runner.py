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
