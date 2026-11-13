"""Owner-scoped stock-gateway clients and reversible native TTS test selection."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
import uuid

import requests
from ruamel.yaml import YAML

from yandex_speech.artifacts import atomic_write
from yandex_speech.errors import SpeechError


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()



def native_log_cursor(workspace):
    paths = [Path(workspace) / "logs" / name for name in
             ("gateway.log", "agent.log", "managed-worker.log")]
    return {path: (path.stat().st_ino, path.stat().st_size) if path.exists() else (None, 0)
            for path in paths}


def native_log_lines(cursor):
    lines, seen = [], set()
    for path, (inode, offset) in cursor.items():
        if not path.exists():
            continue
        current = path.stat()
        if inode is not None and current.st_ino != inode:
            raise ValueError("native log rotated; reconcile trace evidence")
        if current.st_ino in seen:
            continue
        seen.add(current.st_ino)
        with path.open() as stream:
            stream.seek(offset)
            lines.extend(stream.read(16 * 1024 * 1024).splitlines())
    return lines


def native_traces(lines, directory):
    marker = "Yandex native TTS receipt "
    unique = {}
    for line in lines:
        if marker not in line:
            continue
        trace = json.loads(line.split(marker, 1)[1])
        if Path(trace["output"]).parent != Path(directory):
            continue
        previous = unique.setdefault(trace["output"], trace)
        if previous != trace:
            raise ValueError("conflicting native trace records for one output")
    return sorted(unique.values(), key=lambda trace: trace["file_ready_timestamp"])


def validate_native_trace(case, fragment, trace):
    expected = case.get("expected_route", case["route"])
    if (trace["voice"], trace["model"], trace["role"]) != (case["voice"], case["model"], case["role"]):
        raise ValueError("native generation substituted identity")
    if (trace["api_version"], trace["transport"], trace["method"]) != (
            expected.api_version, expected.transport, expected.file_method):
        raise ValueError("native generation used a different operation")
    if trace["unsafe_mode"] is not case["route"].unsafe_mode:
        raise ValueError("native unsafe mode differs from the selected operation")
    lengths = trace["output_part_lengths"]
    if (trace["input_characters"] != len(fragment) or sum(lengths) != len(fragment)
            or len(lengths) != trace["parts"]):
        raise ValueError("native provider parts do not cover the callback input")
    limit = (5000 if expected.file_method == "stream" or expected.unsafe_mode else 250) if expected.api_version == "3" else 5000
    if any(type(length) is not int or not 0 < length <= limit for length in lengths):
        raise ValueError("native provider parts exceed the selected budget")
    if expected.api_version == "3" and (expected.file_method == "stream" or expected.unsafe_mode) and len(fragment) <= 5000 and trace["parts"] != 1:
        raise ValueError("native admitted stream/unsafe input was unnecessarily split")
    if len(trace["submitted_part_lengths"]) != trace["rpc_calls"]:
        raise ValueError("native submitted-attempt accounting disagrees with RPC count")
    if trace["format"] != "oggopus":
        raise ValueError("native voice comparison changed its requested Ogg format")

class NativeGateway:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        if self.workspace.stat().st_uid != os.geteuid():
            raise ValueError("native tests must run as the owning profile principal")
        self.identity = self.control("identify")
        if self.identity["state"] != "ready":
            raise ValueError("running gateway is not ready")
        self.websocket = None
        self.rpc_id = 0
        self.events = []
        self.pending_submission = None

    def control(self, verb, params=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(10)
            channel.connect(str(self.workspace / "gateway.sock"))
            channel.sendall((json.dumps({"protocol": 1, "verb": verb,
                "params": params or {}}) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = channel.recv(65536)
                if not chunk or len(data) > 1024 * 1024:
                    raise ValueError("incomplete/oversized native control response")
                data += chunk
        result = json.loads(data)
        if not result.get("ok"):
            raise ValueError("native control request was refused")
        return result["result"]

    def ticket(self, purpose):
        return self.control("session-ticket", {"profile_id": str(self.workspace),
            "instance_id": self.identity["instance_id"], "purpose": purpose})["ticket"]

    def idle(self):
        state = self.control("active-work")
        if state["pid"] != self.identity["pid"] or state["active_agents"] != 0:
            raise ValueError("native profile has active work or a changed process; test selection was not changed")
        return state

    def connect(self):
        from websockets.sync.client import connect
        from gateway.session_contract import CANONICAL_GATEWAY_PROTOCOL
        self.websocket = connect(self.identity["api_origin"].replace("http://", "ws://") + "/api/ws",
            subprotocols=[CANONICAL_GATEWAY_PROTOCOL, "hermes-gateway-ticket." + self.ticket("interactive")],
            open_timeout=10, close_timeout=5, max_size=16 * 1024 * 1024)

    def rpc(self, method, params):
        self.rpc_id += 1
        identity = self.rpc_id
        self.websocket.send(json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            message = json.loads(self.websocket.recv(timeout=max(.01, deadline-time.monotonic())))
            if message.get("id") == identity:
                if "error" in message:
                    raise ValueError("native RPC refused " + method + ": " + str(message["error"].get("code")))
                return message["result"]
            if len(self.events) < 1000:
                self.events.append(message.get("method"))
        raise TimeoutError("native RPC receipt not observed; no automatic retry")

    def create_session(self, title):
        self.connect()
        result = self.rpc("session.create", {"request_id": uuid.uuid4().hex,
            "source": "tool", "cwd": str(self.workspace), "title": title, "max_turns": 6})
        session_id = result.get("session_id") or result.get("ref", {}).get("session_id")
        if not session_id:
            raise ValueError("native session identity is absent")
        return session_id

    def messages(self, session_id, after=0):
        database = sqlite3.connect("file:" + str(self.workspace / "state.db") + "?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in database.execute(
                "SELECT id,role,content,tool_calls,tool_call_id,tool_name,finish_reason,timestamp FROM messages "
                "WHERE session_id=? AND id>? ORDER BY id", (session_id, after))]
        finally:
            database.close()

    def prompt_speech(self, session_id, case, output, *, timeout=180, expect_rejection=False):
        from tools.tts_text_normalize import prepare_spoken_text
        before = self.messages(session_id)
        start = max((row["id"] for row in before), default=0)
        submission_id = "issue83-" + case["id"] + "-" + uuid.uuid4().hex
        prompt = (
            "Operator-approved synthetic TTS acceptance. Call the standard text_to_speech tool exactly once if available. "
            "Set text to the exact JSON string below and output_path to the specified path. "
            "Use the configured provider, voice and settings; supply no voice/provider overrides. "
            "Do not use terminal, direct APIs, fallback tools, configuration changes or external messages. "
            "Return the tool's media_tag unchanged on success; report unavailability or failure without retry.\n"
            "output_path: " + str(output) + "\ntext: " + json.dumps(case["text"], ensure_ascii=False))
        self.pending_submission = {"session_id": session_id, "submission_id": submission_id, "admission_id": None}
        admitted = self.rpc("prompt.submit", {"session_id": session_id,
            "submission_id": submission_id, "text": prompt})
        self.pending_submission["admission_id"] = admitted.get("admission_id")
        if not self.pending_submission["admission_id"]:
            raise ValueError("admission receipt lacks an identity; retain recovery state")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.messages(session_id, start)
            unique_calls = {}
            for row in rows:
                if row["tool_calls"]:
                    values = json.loads(row["tool_calls"]) if isinstance(row["tool_calls"], str) else row["tool_calls"]
                    for call in values:
                        previous = unique_calls.setdefault(call["id"], (call, row["timestamp"]))
                        if previous[0] != call:
                            raise ValueError("conflicting mirrored native tool calls")
            calls = list(unique_calls.values())
            if any(call.get("function", {}).get("name") != "text_to_speech" for call, timestamp in calls):
                raise ValueError("native agent invoked another tool instead of the approved TTS operation")
            if len(calls) > 1:
                raise ValueError("native agent resubmitted speech; case cannot pass")
            finals = [row for row in rows if row["role"] == "assistant" and not row["tool_calls"]
                      and row["finish_reason"] == "stop"]
            if finals and not calls and not expect_rejection:
                raise ValueError("native agent completed without invoking the standard TTS tool")
            if finals and (calls or expect_rejection):
                result, tool_row, call, call_start = {"success": False}, None, None, None
                if calls:
                    call, call_start = calls[0]
                    arguments = call["function"]["arguments"]
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                    if any(value is not None for name, value in arguments.items() if name not in {"text", "output_path"}):
                        raise ValueError("native agent supplied unapproved speech overrides")
                    if arguments.get("output_path") != str(output):
                        raise ValueError("native agent changed the controlled output allocation")
                    if prepare_spoken_text(arguments["text"], max_chars=None) != case["text"]:
                        raise ValueError("native tool changed the controlled complete speech input")
                    results = [row for row in rows if row["role"] == "tool" and row["tool_call_id"] == call["id"]]
                    if not results or len({row["content"] for row in results}) != 1:
                        raise ValueError("native TTS result identity is absent or ambiguous")
                    tool_row = results[0]
                    result = json.loads(tool_row["content"])
                if expect_rejection:
                    if result.get("success") or result.get("file_path") or result.get("file_paths") or result.get("media_tag"):
                        raise ValueError("unsupported request unexpectedly returned synthesized audio")
                else:
                    if not result.get("success") or result.get("provider") != "yandex":
                        raise ValueError("native agent standard TTS failed")
                    if result.get("delivery_file_count") != 1:
                        raise ValueError("native delivery split into several files; one-file case needs review")
                    if result["media_tag"] not in (finals[-1]["content"] or ""):
                        raise ValueError("native agent did not present the actual voice artifact to its client")
                terminal = self.rpc("prompt.receipt", {"session_id": session_id,
                    "admission_id": self.pending_submission["admission_id"]})
                if terminal.get("status") != "terminal":
                    continue
                self.pending_submission = None
                self.settle_for_restore()  # Terminal receipt can precede managed-worker cleanup.
                return {"submission_id": submission_id, "admission": {key: admitted.get(key)
                        for key in ("admission_id", "status", "submission_id")},
                    "tool_start_timestamp": call_start,
                    "tool_result_timestamp": tool_row["timestamp"] if tool_row else None,
                    "tool_call_id": call["id"] if call else None,
                    "tool_message_id": tool_row["id"] if tool_row else None,
                    "assistant_message_id": finals[-1]["id"],
                    "normalized_characters": len(case["text"]), "result": result}
            try:
                message = json.loads(self.websocket.recv(timeout=min(1, max(.01, deadline-time.monotonic()))))
                if len(self.events) < 1000:
                    self.events.append(message.get("method"))
            except TimeoutError:
                pass
        raise TimeoutError("native agent did not complete; do not retry an uncertain admission")


    def settle_for_restore(self, timeout=30):
        """Cancel only the owned admission/generation; never interrupt other work."""
        deadline = time.monotonic() + timeout
        pending = self.pending_submission
        if pending is not None:
            if not pending.get("admission_id"):
                raise ValueError("unknown admission receipt; recovery must reconcile its submission ID")
            parameters = {key: pending[key] for key in ("session_id", "admission_id")}
            state = self.rpc("prompt.receipt", parameters)
            if state.get("status") != "terminal":
                self.rpc("prompt.cancel", parameters)
            while True:
                state = self.rpc("prompt.receipt", parameters)
                if state.get("status") == "terminal":
                    self.pending_submission = None
                    break
                if time.monotonic() >= deadline:
                    raise ValueError("owned native admission did not settle")
                time.sleep(.1)
        while True:
            state = self.control("active-work")
            if state["pid"] != self.identity["pid"]:
                raise ValueError("gateway changed; recovery must verify its new instance")
            if state["active_agents"] == 0:
                return state
            if time.monotonic() >= deadline:
                raise ValueError("active work remains; configuration rollback was not attempted")
            time.sleep(.1)


    def download(self, path, session_id):
        started = time.monotonic()
        response = requests.get(self.identity["api_origin"] + "/api/fs/read-data-url",
            headers={"x-hermes-gateway-ticket": self.ticket("native-http")},
            params={"path": path, "session_id": session_id}, timeout=60)
        if response.status_code != 200:
            raise ValueError("addressed native client download failed: HTTP " + str(response.status_code))
        value = response.json()["dataUrl"]
        return base64.b64decode(value.split(",", 1)[1], validate=True), time.monotonic()-started

    def close(self):
        if self.websocket is not None:
            self.websocket.close()



def assert_unshared_native_paths(document):
    """Refuse YAML aliases crossing parents that the native CLI would mutate."""
    def references(target):
        count, seen, queue = 0, set(), [document]
        while queue:
            node = queue.pop()
            if node is target:
                count += 1
            if not isinstance(node, (dict, list)) or id(node) in seen:
                continue
            seen.add(id(node))
            queue.extend(node.values() if isinstance(node, dict) else node)
        return count
    for path in (("tts", "providers", "yandex"), ("tts", "yandex", "max_text_length")):
        node = document
        for key in path:
            if not isinstance(node, dict):
                break
            if references(node) > 1:
                raise ValueError("shared YAML mapping crosses the test mutation path; use a reviewed migration")
            if key not in node:
                break
            node = node[key]

class SpeechSelection:
    """Only test-owned user choices and one explicitly recognized command shadow.

    Uses the stock config CLI for Hermes, including --force for this plugin's
    provider namespace. Its before image is retained before the first mutation.
    """
    def __init__(self, gateway, evidence):
        self.gateway = gateway
        self.workspace = gateway.workspace
        self.evidence = Path(evidence)
        self.path = self.workspace / ".config/yandex-speech/config.json"
        self.native_path = self.workspace / "config.yaml"
        self.written = None
        self.shadow_removed = False
        self.original = None
        self.original_mode = None
        self.before = None
        self.outer_changed = False

    def _config(self):
        return YAML(typ="safe").load(self.native_path.read_text())

    def _native(self, *arguments):
        result = subprocess.run(["hermes", "config", *arguments], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError("stock native config command failed; no inferred success")

    def __enter__(self):
        self.gateway.idle()
        if self.path.is_symlink():
            raise ValueError("test selection refuses a symlinked preference file")
        self.original = self.path.read_bytes() if self.path.exists() else None
        self.original_mode = self.path.stat().st_mode & 0o777 if self.original is not None else None
        self.before = json.loads(self.original) if self.original is not None else {"speech": {}}
        # Validate role/schema before storing the non-secret skill preferences.
        from yandex_speech.config import resolve_settings, RuntimeContext
        resolve_settings(_context=RuntimeContext(workspace=self.workspace), _environment={})
        self.native_before = self._config()
        assert_unshared_native_paths(self.native_before)
        native_yandex = self.native_before.get("tts", {}).get("yandex") or {}
        self.original_outer_present = "max_text_length" in native_yandex
        self.original_outer = native_yandex.get("max_text_length")
        self.outer_written = self.original_outer
        if self.original_outer_present and (type(self.original_outer) is not int or self.original_outer <= 0):
            raise ValueError("native outer limit needs a reviewed override")
        if self.native_before.get("tts", {}).get("provider") != "yandex":
            raise ValueError("native tests require the already selected Yandex provider")
        self.shadow = copy.deepcopy(self.native_before.get("tts", {}).get("providers", {}).get("yandex"))
        if self.shadow is not None:
            if (not isinstance(self.shadow, dict) or self.shadow.get("type") != "command"
                    or set(self.shadow) - {"type", "command", "format"}
                    or not self.shadow.get("command", "").startswith("python " + str(Path(__file__).resolve().parents[2] / "scripts/tts.py") + " ")
                    or any(flag in self.shadow["command"].lower() for flag in
                           ("--api-key", "--token", "--password", "authorization"))):
                raise ValueError("command shadow needs a reviewed migration; it was not executed or changed")
        rollback = {"schema_version": 1, "skill_preferences": self.before if self.original is not None else None,
                    "preference_mode": self.original_mode, "command_shadow": self.shadow,
                    "outer_limit_present": self.original_outer_present, "outer_limit": self.original_outer}
        atomic_write(self.evidence / "rollback.json", json.dumps(rollback).encode())
        try:
            if self.shadow is not None:
                expected = copy.deepcopy(self.native_before)
                del expected["tts"]["providers"]["yandex"]
                if not expected["tts"]["providers"]:
                    del expected["tts"]["providers"]
                self.shadow_removed = True  # Arm rollback before a command that may write then fail.
                self._native("unset", "tts.providers.yandex")
                if self._config() != expected:
                    raise ValueError("native command removal changed unrelated preferences")
            return self
        except BaseException as failure:
            self.__exit__(type(failure), failure, failure.__traceback__)
            raise


    def _outer_limit(self, value):
        current = self._config()
        assert_unshared_native_paths(current)
        actual = current.get("tts", {}).get("yandex", {}).get("max_text_length")
        if actual != self.outer_written and actual != self.original_outer:
            raise ValueError("native outer limit changed concurrently")
        if actual == value:
            self.outer_written = value
            return
        expected = copy.deepcopy(current)
        self.outer_changed = True
        self.outer_written = value
        if value is None:
            del expected["tts"]["yandex"]["max_text_length"]
            if not expected["tts"]["yandex"]:
                del expected["tts"]["yandex"]
            self._native("unset", "tts.yandex.max_text_length")
        else:
            expected.setdefault("tts", {}).setdefault("yandex", {})["max_text_length"] = value
            self._native("set", "--force", "tts.yandex.max_text_length", str(value))
        if self._config() != expected:
            raise ValueError("native outer-limit update changed unrelated preferences")

    def select(self, case):
        self.gateway.idle()
        self._outer_limit(case.get("outer_max", self.original_outer))
        current = self.path.read_bytes() if self.path.exists() else None
        expected = self.written if self.written is not None else self.original
        if current != expected:
            raise ValueError("preferences changed concurrently; test selection was not overwritten")
        document = copy.deepcopy(self.before)
        values = document.setdefault("speech", {}).setdefault("tts", {})
        route = case["route"]
        values.update(default_voice=case["voice"], model=case["model"], role=case["role"],
            api_version=route.api_version, transport=route.transport,
            file_method=route.file_method, unsafe_mode=route.unsafe_mode)
        content = json.dumps(document, ensure_ascii=False).encode()
        self.written = content  # Arm rollback before publication, including post-write failures.
        atomic_write(self.path, content, overwrite=current is not None)
        if self.path.read_bytes() != content:
            raise ValueError("test selection readback mismatch")

    def __exit__(self, kind, error, traceback):
        failures = []
        try:
            self.gateway.settle_for_restore()
        except Exception as settlement_error:
            atomic_write(self.evidence / "rollback-result.json", json.dumps({
                "restored": False, "recovery_required": True,
                "reason": type(settlement_error).__name__ + ": native work not settled",
                "pending_submission": getattr(self.gateway, "pending_submission", None)}).encode())
            raise ValueError("native work not settled; settings retained with rollback.json for recovery") from error
        if self.written is not None:
            try:
                self.gateway.idle()
                current = self.path.read_bytes() if self.path.exists() else None
                if current != self.original:
                    if current != self.written:
                        raise ValueError("preference rollback refused concurrent changes")
                    if self.original is None:
                        self.path.unlink()
                    else:
                        atomic_write(self.path, self.original, overwrite=True)
                        os.chmod(self.path, self.original_mode)
            except Exception as failure:
                failures.append(type(failure).__name__ + ": preference rollback")
        if self.outer_changed:
            try:
                self.gateway.idle()
                self._outer_limit(self.original_outer)
            except Exception as failure:
                failures.append(type(failure).__name__ + ": outer-limit rollback")
        if self.shadow_removed:
            try:
                self.gateway.idle()
                current = self._config()
                assert_unshared_native_paths(current)
                actual = current.get("tts", {}).get("providers", {}).get("yandex")
                if actual != self.shadow:
                    if actual is not None:
                        raise ValueError("command rollback refused concurrent changes")
                    expected = copy.deepcopy(current)
                    expected.setdefault("tts", {}).setdefault("providers", {})["yandex"] = self.shadow
                    self._native("set", "--force", "tts.providers.yandex", json.dumps(self.shadow))
                    if self._config() != expected:
                        raise ValueError("command rollback readback mismatch")
            except Exception as failure:
                failures.append(type(failure).__name__ + ": command rollback")
        atomic_write(self.evidence / "rollback-result.json", json.dumps({
            "restored": not failures, "failures": failures}).encode())
        if failures:
            raise ValueError("native test settings need recovery: " + "; ".join(failures)) from error
        return False
