"""Source compatibility must not turn changed implementation or host checks green."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.evidence_compat import unchanged_core_evidence


class EvidenceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = "tests.provider.test_rest_recording.LiveTests.test_recording"
        self.host = "tests.provider.test_synthesis.LiveTests.test_host"
        self.old = {
            "yandex_speech/core.py": b"core implementation",
            "scripts/transcribe.py": b"public interface",
            "tests/provider/test_rest_recording.py": b"real provider fixture",
            "tests/__init__.py": b"provider ancestor initialization",
            "integrations/hermes/__init__.py": b"previous adapter",
            "requirements.txt": b"requests==2.33.0\n",
            "VERSION": b"0.1.0\n",
            "pyproject.toml": b'[project]\nname="yandex-speech"\n[tool.setuptools.data-files]\n"share/yandex-speech/integrations/hermes"=["integrations/hermes/__init__.py"]\n',
        }
        for name, raw in self.old.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        digest = hashlib.sha256()
        for name, raw in sorted(self.old.items()):
            digest.update(name.encode() + b"\0")
            digest.update(raw)
        self.document = {"revision": "a" * 40, "source_digest": digest.hexdigest(),
                         "outcomes": {self.case: "passed", self.host: "passed"}}

    def check(self):
        with patch("tests.evidence_compat._revision_sources", return_value=self.old):
            return unchanged_core_evidence(self.document, self.root, {self.case, self.host})

    def test_adapter_change_reuses_only_eligible_standalone_observations(self):
        (self.root / "integrations/hermes/__init__.py").write_bytes(b"reviewed new adapter")
        outcomes, proof = self.check()
        self.assertEqual(outcomes, {self.case: "passed"})
        self.assertFalse(proof["host_evidence_reused"])
        self.assertEqual(self.document["outcomes"][self.host], "passed")

    def test_changed_core_fixture_dependency_or_missing_input_is_rejected(self):
        for name in ("yandex_speech/core.py", "tests/provider/test_rest_recording.py",
                     "requirements.txt", "VERSION"):
            with self.subTest(name=name):
                path = self.root / name
                raw = path.read_bytes()
                path.write_bytes(raw + b" changed")
                with self.assertRaises(ValueError):
                    self.check()
                path.write_bytes(raw)
        (self.root / "scripts/transcribe.py").unlink()
        with self.assertRaises(ValueError):
            self.check()

    def test_new_core_module_requires_fresh_evidence(self):
        (self.root / "yandex_speech/new_core.py").write_bytes(b"new implementation")
        with self.assertRaises(ValueError):
            self.check()

    def test_forged_origin_digest_is_rejected(self):
        self.document["source_digest"] = "0" * 64
        with self.assertRaises(ValueError):
            self.check()

    def test_additive_integration_payload_is_allowed_but_dependency_or_replacement_is_not(self):
        path = self.root / "pyproject.toml"
        path.write_bytes(self.old["pyproject.toml"].replace(
            b'["integrations/hermes/__init__.py"]',
            b'["integrations/hermes/__init__.py","integrations/hermes/ssml_tool.py"]'))
        self.assertEqual(self.check()[0], {self.case: "passed"})
        path.write_bytes(path.read_bytes().replace(b'name="yandex-speech"', b'name="different"'))
        with self.assertRaises(ValueError):
            self.check()

    def test_multiple_evidence_inputs_are_explicit_and_conflicts_do_not_become_success(self):
        from tests.__main__ import parse_args, merge_provider_observations
        args = parse_args(["--increment", "feedback", "--provider-evidence", "core.json",
                           "--provider-evidence", "host.json", "--reuse-unchanged-core"])
        self.assertEqual(args.provider_evidence, [Path("core.json"), Path("host.json")])
        self.assertTrue(args.reuse_unchanged_core)
        core = {"outcomes": {self.case: "passed"}, "host_opt_in": False}
        host = {"outcomes": {self.host: "passed"}, "host_opt_in": True}
        outcomes, eligible = merge_provider_observations([core, host])
        self.assertEqual(outcomes, {self.case: "passed", self.host: "passed"})
        self.assertEqual(eligible, {self.host})
        with self.assertRaises(ValueError):
            merge_provider_observations([core, {"outcomes": {self.case: "failed"}}])

    def test_added_dependency_input_and_changed_provider_package_initializer_are_rejected(self):
        (self.root / "requirements-extra.txt").write_bytes(b"new dependency")
        with self.assertRaises(ValueError):
            self.check()
        (self.root / "requirements-extra.txt").unlink()
        (self.root / "tests/__init__.py").write_bytes(b"changed package initialization")
        with self.assertRaises(ValueError):
            self.check()

    def test_origin_fingerprint_uses_the_existing_path_ordering_protocol(self):
        from tests.__main__ import source_digest
        additions = {"yandex_speech/part.py": b"module",
                     "yandex_speech/part/__init__.py": b"package"}
        self.old.update(additions)
        for name, raw in additions.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        with patch("tests.__main__.ROOT", self.root):
            self.document["source_digest"] = source_digest()
        self.assertEqual(self.check()[0], {self.case: "passed"})

    def test_provider_package_addition_cannot_shadow_an_original_fixture_module(self):
        package = self.root / "tests/provider/test_rest_recording"
        package.mkdir()
        (package / "__init__.py").write_bytes(b"replacement producer")
        with self.assertRaises(ValueError):
            self.check()

    def test_symlinked_parent_cannot_redirect_core_inputs(self):
        original = self.root / "scripts"
        moved = self.root / "moved_scripts"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.check()

    def test_host_and_unknown_provider_cases_cannot_be_reclassified_as_core(self):
        self.document["outcomes"] = {self.host: "passed",
                                    "tests.provider.unknown.LiveTests.test_case": "passed"}
        with self.assertRaises(ValueError):
            self.check()
