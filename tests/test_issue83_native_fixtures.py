import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from tests.issue83_cases import native_cases
from tests.provider.issue83_fixtures import SpeechSelection


class NativeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "profile"
        self.workspace.mkdir()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        command = "python " + str(Path(__file__).resolve().parents[1] / "scripts/tts.py") + " --input-file {input_path} --output {output_path} --api-version 1"
        self.original = {"tts": {"provider": "yandex", "voice": "ermil", "yandex": {"role": "good"},
            "providers": {"yandex": {"type": "command", "command": command, "format": "oggopus"}}},
            "display": {"tool_progress": "all"}}
        self.config = self.workspace / "config.yaml"
        self.config.write_text(yaml.safe_dump(self.original))
        self.preferences = self.workspace / ".config/yandex-speech/config.json"
        self.preferences.parent.mkdir(parents=True)
        self.before = b'{"speech":{"language_code":"en-US","tts":{"default_voice":"ermil"}}}'
        self.preferences.write_bytes(self.before)
        self.preferences.chmod(0o640)
        self.env = patch.dict(os.environ, {"HERMES_HOME": str(self.workspace),
                                         "HOME": str(self.root / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.gateway = SimpleNamespace(workspace=self.workspace, idle=lambda: {"active_agents": 0},
                                       settle_for_restore=lambda: {"active_agents": 0})

    def test_real_native_cli_scoped_change_and_exact_rollback(self):
        with SpeechSelection(self.gateway, self.evidence) as selection:
            self.assertNotIn("providers", yaml.safe_load(self.config.read_text())["tts"])
            selection.select(native_cases()[0])
            active = json.loads(self.preferences.read_text())
            self.assertEqual(active["speech"]["language_code"], "en-US")
            self.assertEqual(active["speech"]["tts"]["file_method"], "utterance")
        self.assertEqual(yaml.safe_load(self.config.read_text()), self.original)
        self.assertEqual(self.preferences.read_bytes(), self.before)
        self.assertEqual(self.preferences.stat().st_mode & 0o777, 0o640)
        self.assertTrue(json.loads((self.evidence / "rollback-result.json").read_text())["restored"])

    def test_post_write_native_failure_is_compensated(self):
        selection = SpeechSelection(self.gateway, self.evidence)
        native = selection._native
        def failure_after_write(*args):
            native(*args)
            if args[0] == "unset":
                raise ValueError("synthetic post-write failure")
        with patch.object(selection, "_native", side_effect=failure_after_write):
            with self.assertRaisesRegex(ValueError, "synthetic"):
                with selection:
                    self.fail("failed setup must not enter")
        self.assertEqual(yaml.safe_load(self.config.read_text()), self.original)
        self.assertEqual(self.preferences.read_bytes(), self.before)

    def test_concurrent_preferences_are_preserved_and_reported(self):
        with self.assertRaisesRegex(ValueError, "need recovery"):
            with SpeechSelection(self.gateway, self.evidence) as selection:
                selection.select(native_cases()[0])
                self.preferences.write_bytes(b'{"speech":{"language_code":"kk-KZ"}}')
        self.assertEqual(self.preferences.read_bytes(), b'{"speech":{"language_code":"kk-KZ"}}')
        self.assertEqual(yaml.safe_load(self.config.read_text()), self.original)
        self.assertFalse(json.loads((self.evidence / "rollback-result.json").read_text())["restored"])

    def test_unknown_command_shadow_is_refused_before_any_native_write(self):
        original = yaml.safe_load(self.config.read_text())
        original["tts"]["providers"]["yandex"]["command"] = "opaque-program secret"
        self.config.write_text(yaml.safe_dump(original))
        with patch.object(SpeechSelection, "_native") as native:
            with self.assertRaisesRegex(ValueError, "reviewed migration"):
                with SpeechSelection(self.gateway, self.evidence):
                    self.fail("opaque command cannot enter")
        native.assert_not_called()
        self.assertEqual(yaml.safe_load(self.config.read_text()), original)

    def test_active_work_blocks_every_rollback_write_and_retains_recovery(self):
        selection = SpeechSelection(self.gateway, self.evidence)
        selection.__enter__()
        selection.select(native_cases()[0])
        selected_preferences = self.preferences.read_bytes()
        selected_native = self.config.read_bytes()
        self.gateway.settle_for_restore = lambda: (_ for _ in ()).throw(ValueError("active unrelated work"))
        with self.assertRaisesRegex(ValueError, "settings retained"):
            selection.__exit__(None, None, None)
        self.assertEqual(self.preferences.read_bytes(), selected_preferences)
        self.assertEqual(self.config.read_bytes(), selected_native)
        receipt = json.loads((self.evidence / "rollback-result.json").read_text())
        self.assertTrue(receipt["recovery_required"])
        self.assertFalse(receipt["restored"])

    def test_owned_cancel_is_exact_and_unrelated_work_is_never_interrupted(self):
        from tests.provider.issue83_fixtures import NativeGateway
        gateway = object.__new__(NativeGateway)
        gateway.pending_submission = {"session_id": "owned-session", "submission_id": "owned-input",
                                      "admission_id": "owned-admission"}
        gateway.identity = {"pid": 123}
        states = iter([{"status": "started"}, {"status": "terminal"}])
        calls = []
        def rpc(method, params):
            calls.append((method, params))
            return next(states) if method == "prompt.receipt" else {"status": "started"}
        gateway.rpc = rpc
        gateway.control = lambda verb: {"pid": 123, "active_agents": 0}
        gateway.settle_for_restore(timeout=.2)
        cancel = next(params for method, params in calls if method == "prompt.cancel")
        self.assertEqual(cancel, {"session_id": "owned-session", "admission_id": "owned-admission"})
        self.assertFalse(any(method == "session.interrupt" for method, params in calls))
        self.assertIsNone(gateway.pending_submission)
        gateway.control = lambda verb: {"pid": 123, "active_agents": 1}
        with self.assertRaisesRegex(ValueError, "active work"):
            gateway.settle_for_restore(timeout=.01)

    def test_trace_rejects_normal_execution_labeled_as_unsafe(self):
        from tests.provider.issue83_fixtures import validate_native_trace
        case = next(case for case in native_cases() if case["voice"] == "ermil"
                    and case["length"] == 1000 and case["route"].name == "grpc-v3-unsafe")
        trace = {"voice": "ermil", "model": "general", "role": "good",
            "api_version": "3", "transport": "grpc", "method": "utterance",
            "unsafe_mode": False, "input_characters": 1000, "format": "oggopus",
            "parts": 4, "submitted_part_lengths": [250] * 4, "output_part_lengths": [250] * 4, "rpc_calls": 4}
        with self.assertRaisesRegex(ValueError, "unsafe mode"):
            validate_native_trace(case, case["text"], trace)
        trace["unsafe_mode"] = True
        with self.assertRaisesRegex(ValueError, "unnecessarily split"):
            validate_native_trace(case, case["text"], trace)
        trace.update(parts=1, submitted_part_lengths=[1000], output_part_lengths=[1000], rpc_calls=1)
        validate_native_trace(case, case["text"], trace)

    def test_stock_outer_override_and_rollback_exercise_sdk_rollover(self):
        from tests.issue83_cases import rollover_cases
        with SpeechSelection(self.gateway, self.evidence) as selection:
            selection.select(rollover_cases()[0])
            self.assertEqual(yaml.safe_load(self.config.read_text())["tts"]["yandex"]["max_text_length"], 10000)
            selection.select(native_cases()[0])
            self.assertNotIn("max_text_length", yaml.safe_load(self.config.read_text())["tts"]["yandex"])
            selection.select(rollover_cases()[1])
        self.assertEqual(yaml.safe_load(self.config.read_text()), self.original)
        self.assertEqual(self.preferences.read_bytes(), self.before)

    def test_crossing_yaml_aliases_are_refused_before_native_mutation(self):
        shared = self.original["tts"]["providers"]
        self.original["stt"] = {"providers": shared}
        self.config.write_text(yaml.safe_dump(self.original))
        with patch.object(SpeechSelection, "_native") as native:
            with self.assertRaisesRegex(ValueError, "shared YAML mapping"):
                with SpeechSelection(self.gateway, self.evidence):
                    self.fail("crossing alias must not enter")
        native.assert_not_called()
        self.assertEqual(yaml.safe_load(self.config.read_text()), self.original)
