"""Opt-in running-agent standard-tool, addressed-client and length-silo acceptance."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid

from tests.__main__ import source_digest
from tests.issue83_cases import alena_cases, automatic_cases, native_cases, rejection_cases, rollover_cases
from tests.provider.issue83_fixtures import (NativeGateway, SpeechSelection, digest,
    native_case_selection, case_rollbacks,
    native_log_cursor, native_log_lines, native_traces, validate_native_trace)


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1"
                     and os.environ.get("YANDEX_SPEECH_HOST_TESTS") == "1",
                     "explicit provider and running-host opt-in required")
class LiveNativeTests(unittest.TestCase):

    def prepare_run(self, scope):
        import psutil
        workspace = Path(os.environ["HERMES_HOME"]).resolve()
        self.assertEqual(Path(sys.prefix).resolve(), Path(os.environ["VIRTUAL_ENV"]).resolve())
        gateway = NativeGateway(workspace)
        started_process = psutil.Process(gateway.identity["pid"]).create_time()
        project = Path(__file__).resolve().parents[2]
        self.assertEqual((workspace / "plugins/yandex-speech").resolve(), project / "integrations/hermes")
        runtime_files = list((project / "yandex_speech").glob("*.py")) + list((project / "integrations/hermes").glob("*.py"))
        runtime_files.append(project / "scripts/tts.py")
        self.assertGreater(started_process, max(path.stat().st_mtime for path in runtime_files),
                           "gateway predates current runtime code; use a verified quiet activation")
        root = Path(os.environ["YANDEX_SPEECH_TEST_OUTPUT_DIR"]) / (scope + "-" + uuid.uuid4().hex)
        root.mkdir(parents=True)
        receipt = {"schema_version": 1, "scope": scope, "native_commissioned": False,
            "source_digest": source_digest(), "synthesis_source_digest": source_digest(synthesis=True),
            "credential_before_sha256": digest(workspace / ".env"),
            "candidate_revision": os.environ.get("YANDEX_SPEECH_TEST_REVISION"),
            "gateway": {key: gateway.identity[key] for key in
                        ("pid", "instance_id", "code_sha", "code_version")},
            "principal_uid": os.geteuid(), "interpreter": sys.executable, "cases": []}
        return workspace, root, gateway, receipt

    def run_cases(self, cases, scope):
        from tools.tts_tool_delivery import _resolve_max_text_length, _split_text_for_tts
        from ruamel.yaml import YAML
        workspace, root, gateway, receipt = self.prepare_run(scope)
        source = receipt["source_digest"]
        env_before = receipt["credential_before_sha256"]
        rows = receipt["cases"]
        receipt["expected_case_ids"] = [case["id"] for case in cases]
        path = root / "receipt.json"
        def save():
            temporary = root / ".receipt-update"
            temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
            os.replace(temporary, path)
        group, session_id = None, None
        baseline = {}
        try:
            for case in cases:
                directory = root / case["id"]
                directory.mkdir()
                row = {"id": case["id"], "status": "started", "voice": case["voice"],
                    "model": case["model"], "role": case["role"], "route": asdict(case["route"]),
                    "whole_normalized_characters": case["length"], "silo": case["silo"],
                    "input_sha256": hashlib.sha256(case["text"].encode()).hexdigest()}
                rows.append(row)
                save()
                try:
                    with native_case_selection(gateway, directory, baseline) as selection:
                        selection.select(case)
                        current_group = (case["voice"], case["length"], case.get("outer_max", 4000))
                        if group != current_group:
                            gateway.close()
                            session_id = gateway.create_session("TTS " + "-".join(map(str, current_group)) + " " + uuid.uuid4().hex[:8])
                            group = current_group
                        row["native_session_id"] = session_id
                        output = directory / "native.ogg"
                        cursor = native_log_cursor(workspace)
                        started = time.monotonic()
                        proof = gateway.prompt_speech(session_id, case, output)
                        result = proof["result"]
                        data, delivery_seconds = gateway.download(result["file_path"], session_id)
                        client_audio_seconds = time.monotonic() - started
                        delivered = directory / ("delivered" + Path(result["file_path"]).suffix)
                        delivered.write_bytes(data)
                        self.assertEqual(hashlib.sha256(data).hexdigest(), digest(result["file_path"]))
                        decoder = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror",
                            "-i", str(delivered), "-f", "null", "-"],
                            capture_output=True, timeout=60)
                        self.assertEqual(decoder.returncode, 0, "complete native delivery does not decode")
                        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration,format_name:stream=codec_name,sample_rate", "-of", "json",
                            str(delivered)], capture_output=True, check=True, timeout=30)
                        traces = native_traces(native_log_lines(cursor), directory)
                        profile = YAML(typ="safe").load((workspace / "config.yaml").read_text())["tts"]
                        outer = _split_text_for_tts(case["text"], _resolve_max_text_length("yandex", profile))
                        self.assertEqual(len(traces), len(outer))
                        self.assertEqual(result["chunk_count"], len(outer))
                        expected_route = case.get("expected_route", case["route"])
                        for fragment, trace in zip(outer, traces):
                            validate_native_trace(case, fragment, trace)
                        lengths = [length for trace in traces for length in trace["submitted_part_lengths"]]
                        units = sum((length + 249) // 250 for length in lengths) if expected_route.api_version == "3" else sum(lengths)
                        row.update(status="passed-native-generation-delivery", proof=proof,
                            actual_whole_input_characters=proof["normalized_characters"],
                            outer_chunks=len(outer), rpc_calls=sum(trace["rpc_calls"] for trace in traces),
                            submitted_part_lengths=lengths,
                            output_part_lengths=[length for trace in traces for length in trace["output_part_lengths"]],
                            traces=traces,
                            delivered_file=str(delivered.relative_to(root)), bytes=len(data),
                            sha256=digest(delivered), probe=json.loads(probe.stdout),
                            native_tool_seconds=proof["tool_result_timestamp"]-proof["tool_start_timestamp"],
                            native_callback_seconds=sum(trace["elapsed_seconds"] for trace in traces),
                            file_ready_seconds=proof["tool_result_timestamp"]-proof["tool_start_timestamp"],
                            first_client_audio_seconds=client_audio_seconds,
                            first_provider_fragment_seconds=(traces[0].get("stream_receipts") or [{}])[0].get("first_audio_seconds"),
                            addressed_delivery_seconds=delivery_seconds,
                            agent_and_delivery_wall_seconds=client_audio_seconds,
                            tariff_units=units, tariff_unit_kind="250-character block" if expected_route.api_version == "3" else "submitted character",
                            first_client_audio_basis="prompt admission attempt to downloaded complete file, including model work; no progressive-playback claim",
                            file_ready_basis="completed native tool result after host output combination",
                            listener_verdict="pending", tail_verdict="pending-listener")
                        self.assertEqual(gateway.control("identify")["instance_id"], gateway.identity["instance_id"])
                except BaseException as error:
                    row.update(status="failed", error_type=type(error).__name__, error=str(error)[:500])
                    save()
                    raise
                save()
                print(json.dumps({"case": case["id"], "characters": case["length"],
                    "status": row["status"], "outer_chunks": row["outer_chunks"],
                    "rpc_calls": row["rpc_calls"]}), flush=True)
            receipt["all_generation_delivery_cases_passed"] = all(row["status"] == "passed-native-generation-delivery" for row in rows)
            self.assertEqual({row["id"] for row in rows}, {case["id"] for case in cases})
        finally:
            gateway.close()
            receipt["sources_unchanged"] = source_digest() == source
            receipt["credentials_unchanged"] = digest(workspace / ".env") == env_before
            receipt["rollback"] = case_rollbacks(root, rows)
            save()
            print(json.dumps({"scope": scope, "receipt": str(path),
                              "passed_cases": sum(row["status"] == "passed-native-generation-delivery" for row in rows),
                              "commissioned": False}), flush=True)
        self.assertTrue(receipt["sources_unchanged"])
        self.assertTrue(receipt["credentials_unchanged"])
        self.assertTrue(receipt["rollback"]["restored"])

    def test_native_canary(self):
        cases = [case for case in native_cases() if case["voice"] == "ermil"
                 and case["route"].name == "grpc-v3-stream" and case["length"] in (200, 1000, 5500)]
        self.run_cases(cases, "issue83-native-canary")

    def test_native_matrix(self):
        self.run_cases(native_cases() + alena_cases() + rollover_cases(), "issue83-native-matrix")

    def test_native_automatic_matrix(self):
        self.run_cases(automatic_cases(), "issue83-native-automatic")

    def test_native_rejections(self):
        workspace, root, gateway, receipt = self.prepare_run("issue83-native-rejections")
        observations = receipt["cases"]
        try:
            with SpeechSelection(gateway, root) as selection:
                control = next(case for case in native_cases() if case["voice"] == "ermil"
                    and case["length"] == 200 and case["route"].name == "grpc-v3-stream")
                selection.select(control)
                session_id = gateway.create_session("TTS rejection " + uuid.uuid4().hex[:8])
                control_directory = root / "positive-control"
                control_directory.mkdir()
                positive = gateway.prompt_speech(session_id, control, control_directory / "native.ogg")
                self.assertTrue(positive["result"]["success"])
                receipt["positive_control"] = positive
                for case in rejection_cases():
                    selection.select(case)
                    directory = root / case["id"]
                    directory.mkdir()
                    cursor = native_log_cursor(workspace)
                    proof = gateway.prompt_speech(session_id, case, directory / "native.ogg",
                                                  expect_rejection=True)
                    lines = native_log_lines(cursor)
                    self.assertFalse(any("Yandex native TTS receipt " in line for line in lines))
                    marker = "Yandex native TTS preflight "
                    rejections = [json.loads(line.split(marker, 1)[1]) for line in lines if marker in line]
                    self.assertTrue(rejections, "native local-only preflight evidence is required")
                    self.assertTrue(all(row["local_only"] and row["category"] == "CONFIG" for row in rejections))
                    self.assertFalse(proof["result"].get("success"))
                    self.assertFalse(list(directory.glob("*.ogg")))
                    observations.append({"id": case["id"], "outcome": "expected-rejection",
                        "proof": proof, "audio_returned": False, "synthesis_submitted": False,
                        "local_preflight": rejections})
                    self.assertEqual(gateway.control("identify")["instance_id"], gateway.identity["instance_id"])
        finally:
            gateway.close()
            receipt["sources_unchanged"] = source_digest() == receipt["source_digest"]
            receipt["credentials_unchanged"] = digest(workspace / ".env") == receipt["credential_before_sha256"]
            receipt["rollback"] = json.loads((root / "rollback-result.json").read_text()) if (root / "rollback-result.json").exists() else {"restored": False}
            (root / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        self.assertEqual({row["id"] for row in observations}, {case["id"] for case in rejection_cases()})
        self.assertTrue(receipt["sources_unchanged"])
        self.assertTrue(receipt["credentials_unchanged"])
        self.assertTrue(receipt["rollback"]["restored"])
