import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import io
from ruamel.yaml import YAML


def dump_yaml(document):
    stream = io.StringIO()
    YAML(typ="safe").dump(document, stream)
    return stream.getvalue()

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
        self.config.write_text(dump_yaml(self.original))
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
            self.assertNotIn("providers", YAML(typ="safe").load(self.config.read_text())["tts"])
            selection.select(native_cases()[0])
            active = json.loads(self.preferences.read_text())
            self.assertEqual(active["speech"]["language_code"], "en-US")
            self.assertEqual(active["speech"]["tts"]["file_method"], "utterance")
        self.assertEqual(YAML(typ="safe").load(self.config.read_text()), self.original)
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
        self.assertEqual(YAML(typ="safe").load(self.config.read_text()), self.original)
        self.assertEqual(self.preferences.read_bytes(), self.before)

    def test_concurrent_preferences_are_preserved_and_reported(self):
        with self.assertRaisesRegex(ValueError, "need recovery"):
            with SpeechSelection(self.gateway, self.evidence) as selection:
                selection.select(native_cases()[0])
                self.preferences.write_bytes(b'{"speech":{"language_code":"kk-KZ"}}')
        self.assertEqual(self.preferences.read_bytes(), b'{"speech":{"language_code":"kk-KZ"}}')
        self.assertEqual(YAML(typ="safe").load(self.config.read_text()), self.original)
        self.assertFalse(json.loads((self.evidence / "rollback-result.json").read_text())["restored"])

    def test_unrelated_activity_does_not_block_exact_owned_hot_restoration(self):
        with SpeechSelection(self.gateway, self.evidence) as selection:
            selection.select(native_cases()[0])
            self.gateway.settle_for_restore = lambda: {"active_agents": 1}
            self.gateway.idle = lambda: (_ for _ in ()).throw(AssertionError("unrelated chat must not block rollback"))
        self.assertEqual(yaml_config := YAML(typ="safe").load(self.config.read_text()), self.original)
        self.assertEqual(self.preferences.read_bytes(), self.before)
        self.assertTrue(json.loads((self.evidence / "rollback-result.json").read_text())["restored"])

    def test_unknown_command_shadow_is_refused_before_any_native_write(self):
        original = YAML(typ="safe").load(self.config.read_text())
        original["tts"]["providers"]["yandex"]["command"] = "opaque-program secret"
        self.config.write_text(dump_yaml(original))
        with patch.object(SpeechSelection, "_native") as native:
            with self.assertRaisesRegex(ValueError, "reviewed migration"):
                with SpeechSelection(self.gateway, self.evidence):
                    self.fail("opaque command cannot enter")
        native.assert_not_called()
        self.assertEqual(YAML(typ="safe").load(self.config.read_text()), original)

    def test_unsettled_owned_work_blocks_every_rollback_write_and_retains_recovery(self):
        selection = SpeechSelection(self.gateway, self.evidence)
        selection.__enter__()
        selection.select(native_cases()[0])
        selected_preferences = self.preferences.read_bytes()
        selected_native = self.config.read_bytes()
        self.gateway.settle_for_restore = lambda: (_ for _ in ()).throw(ValueError("owned admission uncertain"))
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
        state = gateway.settle_for_restore(timeout=.01)
        self.assertEqual(state["active_agents"], 1)
        self.assertFalse(any(method == "session.interrupt" for method, params in calls))

    def test_prompt_waits_for_terminal_worker_cleanup_before_return(self):
        from tests.provider.issue83_fixtures import NativeGateway
        case = native_cases()[0]
        output = Path("/synthetic/native.ogg")
        tag = "[[audio_as_voice]]MEDIA:/synthetic/native.ogg"
        call = {"id": "owned-call", "function": {"name": "text_to_speech",
            "arguments": json.dumps({"text": case["text"], "output_path": str(output)})}}
        rows = [
            {"id": 1, "role": "assistant", "content": "", "tool_calls": json.dumps([call]),
             "timestamp": 1.0, "finish_reason": "tool_calls", "tool_call_id": None},
            {"id": 2, "role": "tool", "content": json.dumps({"success": True, "provider": "yandex",
                "delivery_file_count": 1, "file_path": str(output), "media_tag": tag}),
             "tool_calls": None, "timestamp": 2.0, "finish_reason": None, "tool_call_id": "owned-call"},
            {"id": 3, "role": "assistant", "content": tag, "tool_calls": None,
             "timestamp": 3.0, "finish_reason": "stop", "tool_call_id": None},
        ]
        gateway = object.__new__(NativeGateway)
        observations = iter([[], rows])
        gateway.messages = lambda session_id, after=0: next(observations)
        gateway.rpc = unittest.mock.Mock(side_effect=lambda method, params: ({"admission_id": "owned-admission"}
            if method == "prompt.submit" else {"status": "terminal"}))
        gateway.settle_for_restore = unittest.mock.Mock()
        result = gateway.prompt_speech("owned-session", case, output)
        gateway.settle_for_restore.assert_called_once()
        self.assertEqual(gateway.rpc.call_args_list[0], unittest.mock.call("session.resume",
            {"session_id": "owned-session", "omit_messages": True}))
        self.assertEqual(gateway.rpc.call_args_list[1].args[0], "prompt.submit")
        self.assertIsNone(gateway.pending_submission)
        self.assertEqual(result["tool_call_id"], "owned-call")

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
            self.assertEqual(YAML(typ="safe").load(self.config.read_text())["tts"]["yandex"]["max_text_length"], 10000)
            selection.select(native_cases()[0])
            self.assertNotIn("max_text_length", YAML(typ="safe").load(self.config.read_text())["tts"]["yandex"])
            selection.select(rollover_cases()[1])
        self.assertEqual(YAML(typ="safe").load(self.config.read_text()), self.original)
        self.assertEqual(self.preferences.read_bytes(), self.before)

    def test_crossing_yaml_aliases_are_refused_before_native_mutation(self):
        shared = self.original["tts"]["providers"]
        self.original["stt"] = {"providers": shared}
        self.config.write_text(dump_yaml(self.original))
        with patch.object(SpeechSelection, "_native") as native:
            with self.assertRaisesRegex(ValueError, "shared YAML mapping"):
                with SpeechSelection(self.gateway, self.evidence):
                    self.fail("crossing alias must not enter")
        native.assert_not_called()
        self.assertEqual(YAML(typ="safe").load(self.config.read_text()), self.original)

class PerCaseSelectionTests(unittest.TestCase):
    def setUp(self):
        NativeSelectionTests.setUp(self)

    def test_busy_gateway_wait_is_read_only_and_checks_process_identity(self):
        from tests.provider.issue83_fixtures import NativeGateway
        gateway = NativeGateway.__new__(NativeGateway)
        gateway.identity = {"pid": 71}
        gateway.pending_submission = None
        gateway.websocket = None
        with patch.object(gateway, "control", side_effect=[
                {"pid": 71, "active_agents": 2}, {"pid": 71, "active_agents": 0}]) as control, \
                patch("tests.provider.issue83_fixtures.time.sleep") as sleep:
            self.assertEqual(gateway.wait_idle(timeout=5)["active_agents"], 0)
        self.assertEqual(control.call_args_list, [unittest.mock.call("active-work")] * 2)
        sleep.assert_called_once()
        with patch.object(gateway, "control", return_value={"pid": 72, "active_agents": 0}):
            with self.assertRaisesRegex(ValueError, "process"):
                gateway.wait_idle(timeout=5)
        with patch.object(gateway, "control", return_value={"pid": 71, "active_agents": 1}), \
                patch("tests.provider.issue83_fixtures.time.monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(TimeoutError, "idle"):
                gateway.wait_idle(timeout=5)

    def test_each_case_restores_before_waiting_for_the_next_idle_window(self):
        from tests.provider.issue83_fixtures import native_case_selection, case_rollbacks
        baseline, rows, waited = {}, [], []
        def wait_idle():
            self.assertEqual(YAML(typ="safe").load(self.config.read_text()), self.original)
            self.assertEqual(self.preferences.read_bytes(), self.before)
            waited.append(True)
        self.gateway.wait_idle = wait_idle
        for case in native_cases()[:2]:
            directory = self.evidence / case["id"]
            directory.mkdir()
            rows.append({"id": case["id"]})
            with native_case_selection(self.gateway, directory, baseline) as selection:
                selection.select(case)
        self.assertEqual(len(waited), 2)
        summary = case_rollbacks(self.evidence, rows)
        self.assertTrue(summary["restored"])
        self.assertEqual(len(summary["cases"]), 2)

    def test_interruption_restores_the_owned_case_without_claiming_completion(self):
        from tests.provider.issue83_fixtures import native_case_selection, case_rollbacks
        self.gateway.wait_idle = lambda: None
        case = native_cases()[0]
        directory = self.evidence / case["id"]
        directory.mkdir()
        with self.assertRaises(KeyboardInterrupt):
            with native_case_selection(self.gateway, directory, {}) as selection:
                selection.select(case)
                raise KeyboardInterrupt()
        summary = case_rollbacks(self.evidence, [{"id": case["id"], "status": "failed"}])
        self.assertTrue(summary["restored"])
        self.assertEqual(self.preferences.read_bytes(), self.before)
        self.assertNotIn("all_generation_delivery_cases_passed", summary)

    def test_setup_failure_is_included_in_aggregate_rollback(self):
        from tests.provider.issue83_fixtures import native_case_selection, case_rollbacks
        self.gateway.wait_idle = lambda: None
        directory = self.evidence / "setup"
        directory.mkdir()
        native = SpeechSelection._native
        def fail_after_write(selection, *args):
            native(selection, *args)
            if args[0] == "unset":
                raise ValueError("post-write setup failure")
        with patch.object(SpeechSelection, "_native", new=fail_after_write):
            with self.assertRaisesRegex(ValueError, "post-write"):
                with native_case_selection(self.gateway, directory, {}):
                    self.fail("failed setup must not enter")
        self.assertTrue(case_rollbacks(self.evidence, [{"id": "setup"}])["restored"])
        self.assertEqual(YAML(typ="safe").load(self.config.read_text()), self.original)

    def test_changed_inherited_comparison_settings_are_preserved_and_stop_the_run(self):
        from tests.provider.issue83_fixtures import native_case_selection
        self.gateway.wait_idle = lambda: None
        baseline = {}
        first = self.evidence / "first"
        first.mkdir()
        with native_case_selection(self.gateway, first, baseline):
            pass
        changed = dict(self.original, tts=dict(self.original["tts"], speed=1.2))
        self.config.write_text(dump_yaml(changed))
        second = self.evidence / "second"
        second.mkdir()
        with self.assertRaisesRegex(ValueError, "comparison settings changed"):
            with native_case_selection(self.gateway, second, baseline):
                self.fail("different inherited settings cannot share a matched comparison")
        self.assertEqual(YAML(typ="safe").load(self.config.read_text()), changed)
        self.assertEqual(self.preferences.read_bytes(), self.before)

    def test_missing_failed_or_malformed_rollback_never_counts_as_restored(self):
        from tests.provider.issue83_fixtures import case_rollbacks
        self.assertFalse(case_rollbacks(self.evidence, [])["restored"])
        for name, payload in (("good", {"restored": True}), ("bad", {"restored": False}),
                              ("malformed", "invalid")):
            directory = self.evidence / name
            directory.mkdir()
            (directory / "rollback-result.json").write_text(json.dumps(payload))
        for name in ("missing", "bad", "malformed"):
            summary = case_rollbacks(self.evidence, [{"id": "good"}, {"id": name}])
            self.assertFalse(summary["restored"])
            self.assertEqual(len(summary["cases"]), 2)

class NativeConnectionTests(unittest.TestCase):
    def gateway(self):
        from tests.provider.issue83_fixtures import NativeGateway
        gateway = NativeGateway.__new__(NativeGateway)
        gateway.identity = {"pid": 71}
        gateway.websocket = None
        gateway.pending_submission = None
        gateway.rpc_id = 0
        gateway.events = []
        return gateway

    def test_idle_wait_closes_previous_connection_before_polling_unrelated_work(self):
        gateway = self.gateway()
        events = []
        old = SimpleNamespace(close=lambda: events.append("close"))
        gateway.websocket = old
        states = iter([{"pid": 71, "active_agents": 1}, {"pid": 71, "active_agents": 0}])
        def control(method):
            events.append(method)
            return next(states)
        with patch.object(gateway, "control", side_effect=control), \
                patch("tests.provider.issue83_fixtures.time.sleep"):
            gateway.wait_idle()
        self.assertEqual(events, ["close", "active-work", "active-work"])
        self.assertIsNone(gateway.websocket)

    def test_default_idle_wait_allows_normal_work_to_finish_without_a_reserved_window(self):
        gateway = self.gateway()
        with patch.object(gateway, "control", side_effect=[
                {"pid": 71, "active_agents": 1}, {"pid": 71, "active_agents": 0}]), \
                patch("tests.provider.issue83_fixtures.time.monotonic", side_effect=[0, 901]), \
                patch("tests.provider.issue83_fixtures.time.sleep"):
            self.assertEqual(gateway.wait_idle()["active_agents"], 0)

    def test_wait_refuses_unsettled_owned_submission_before_closing_or_polling(self):
        gateway = self.gateway()
        gateway.pending_submission = {"admission_id": "owned"}
        with patch.object(gateway, "close") as close, patch.object(gateway, "control") as control:
            with self.assertRaisesRegex(ValueError, "owned"):
                gateway.wait_idle(timeout=0)
        close.assert_not_called()
        control.assert_not_called()

    def test_resume_refusal_never_creates_an_uncertain_prompt(self):
        gateway = self.gateway()
        gateway.messages = unittest.mock.Mock(return_value=[])
        gateway.rpc = unittest.mock.Mock(side_effect=ValueError("session attachment refused"))
        with self.assertRaisesRegex(ValueError, "attachment"):
            gateway.prompt_speech("owned-session", native_cases()[0], Path("/synthetic/native.ogg"))
        gateway.rpc.assert_called_once_with("session.resume",
            {"session_id": "owned-session", "omit_messages": True})
        gateway.messages.assert_not_called()
        self.assertIsNone(gateway.pending_submission)

    def test_rpc_opens_fresh_connection_once_without_replaying_a_failed_send(self):
        gateway = self.gateway()
        sent = []
        websocket = SimpleNamespace(send=lambda message: sent.append(json.loads(message)),
            recv=lambda timeout: json.dumps({"id": 1, "result": {"admission_id": "once"}}))
        def connect():
            gateway.websocket = websocket
        with patch.object(gateway, "connect", side_effect=connect) as opened:
            self.assertEqual(gateway.rpc("prompt.submit", {"submission_id": "stable"})["admission_id"], "once")
            opened.assert_called_once()
            self.assertEqual(len(sent), 1)
            with patch.object(websocket, "send", side_effect=ConnectionError("closed after send attempt")):
                with self.assertRaises(ConnectionError):
                    gateway.rpc("prompt.submit", {"submission_id": "another"})
            opened.assert_called_once()
        self.assertEqual([entry["method"] for entry in sent], ["prompt.submit"])

    def test_close_is_idempotent_and_forgets_even_a_failed_close(self):
        gateway = self.gateway()
        websocket = SimpleNamespace(close=lambda: None)
        gateway.websocket = websocket
        with patch.object(websocket, "close") as close:
            gateway.close()
            gateway.close()
            close.assert_called_once()
        self.assertIsNone(gateway.websocket)
        gateway.websocket = websocket
        with patch.object(websocket, "close", side_effect=ConnectionError("close failed")):
            with self.assertRaises(ConnectionError):
                gateway.close()
        self.assertIsNone(gateway.websocket)
