"""Private durable receipts; accepted jobs are recovered, never implicitly resubmitted."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import threading
from pathlib import Path

from .artifacts import atomic_write, private_job_dir
from .errors import SpeechError

STATUSES = {"prepared", "submitting", "running", "results_ready", "completed",
            "no_speech", "failed", "cancelled", "submission_uncertain"}


class JobHandle:
    def __init__(self, manifest_path, data, *, deadline_at=None):
        self.manifest_path = Path(manifest_path).expanduser().absolute()
        self.data = data
        self.deadline_at = deadline_at
        self.cancel_event = threading.Event()

    @property
    def job_id(self):
        return self.data["job_id"]

    @property
    def operation_id(self):
        ids = self.data.get("operation_ids", [])
        return ids[0] if len(ids) == 1 else None

    @property
    def api_version(self):
        return self.data["api_version"]

    @property
    def status(self):
        return self.data["status"]

    @property
    def parts(self):
        return copy.deepcopy(self.data["parts"])

    def save(self):
        raw = json.dumps(self.data, ensure_ascii=False, indent=2, allow_nan=False).encode()
        atomic_write(self.manifest_path, raw, overwrite=self.manifest_path.exists())

    def set_status(self, status):
        if status not in STATUSES:
            raise SpeechError("STATE", "invalid job status")
        self.data["status"] = status
        self.save()

    @classmethod
    def create(cls, base, source_info, *, parameters, deadline_at=None, api_version="3", transport="grpc"):
        version = str(api_version)
        if version not in {"2", "3"} or transport not in {"grpc", "rest"} or version == "2" and transport != "rest":
            raise SpeechError("CONFIG", "unsupported recorded job version or transport")
        directory = private_job_dir(Path(base))
        data = {"schema_version": 1, "job_id": directory.name, "api_version": version,
                "transport": transport, "mode": "async", "status": "prepared",
                "source_info": copy.deepcopy(source_info), "parameters": copy.deepcopy(parameters),
                "source_ref": source_info.get("uri") or source_info.get("original_path"),
                "parts": [{"part_id": "0", "offset": 0.0,
                           "duration_ms": source_info.get("duration_ms"),
                           "api_version": version, "operation_id": None, "status": "prepared"}],
                "operation_ids": [], "owned_uploads": []}
        job = cls(directory / "job.json", data, deadline_at=deadline_at)
        job.save()
        return job

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser().absolute()
        try:
            if path.is_symlink():
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("mode") == "async-partitioned":
                from .partition_jobs import PartitionJob
                return PartitionJob.load(path)
            if (data.get("schema_version") != 1 or data.get("api_version") not in {"2", "3"}
                    or data.get("transport") not in {"grpc", "rest"}
                    or data.get("api_version") == "2" and data.get("transport") != "rest"
                    or data.get("status") not in STATUSES or path.name != "job.json"
                    or not re.fullmatch(r"[a-f0-9]{32}", data.get("job_id", ""))
                    or path.parent.name != data["job_id"]
                    or not isinstance(data.get("parts"), list) or len(data["parts"]) != 1
                    or not isinstance(data.get("operation_ids"), list)):
                raise ValueError
        except (OSError, ValueError, TypeError, AttributeError):
            raise SpeechError("CONFIG", "invalid or unsupported durable job receipt") from None
        return cls(path, data)

    @contextlib.contextmanager
    def lock(self):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.manifest_path.parent / ".lock", flags, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if "fd" in locals():
                os.close(fd)
            raise SpeechError("STATE", "another foreground attempt owns this receipt, or the receipt is not writable") from None
        try:
            latest = type(self).load(self.manifest_path)
            self.data = latest.data
            yield
        finally:
            os.close(fd)

    def cached_result(self):
        expected = self.data.get("result_sha256") or self.data.get("pending_result_sha256")
        if not expected:
            return None
        path = self.manifest_path.parent / "result.json"
        try:
            if path.is_symlink():
                raise ValueError
            if not path.exists() and self.data.get("pending_result_sha256") and not self.data.get("result_sha256"):
                return None
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError
            result = json.loads(raw)
            if (not isinstance(result, dict) or result.get("schema_version") != 1
                    or result.get("status") not in {"completed", "no_speech"}
                    or result.get("input_complete") is not True):
                raise ValueError
        except (OSError, ValueError):
            raise SpeechError("STATE", "saved result differs from its durable receipt") from None
        if self.data.get("pending_result_sha256"):
            return self._commit_result(result, expected)
        return result

    def _commit_result(self, result, digest):
        previous = self.data
        current = copy.deepcopy(previous)
        current["result_sha256"] = digest
        current.pop("pending_result_sha256", None)
        current["status"] = result["status"]
        for part in current["parts"]:
            part.update({"status": result["status"], "saved_result_reference": "result.json"})
        self.data = current
        try:
            self.save()
        except BaseException:
            self.data = previous
            raise
        return result

    def store_result(self, result):
        path = self.manifest_path.parent / "result.json"
        if path.is_symlink():
            raise SpeechError("STATE", "result path is a symlink")
        if path.exists():
            if not (self.data.get("result_sha256") or self.data.get("pending_result_sha256")):
                raise SpeechError("CONFIG", "an unrelated existing result is preserved")
            return self.cached_result()
        if self.cancel_event.is_set():
            raise SpeechError("CANCELLED", "local result publication cancelled")
        raw = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.data["pending_result_sha256"] = digest
        self.data["status"] = "results_ready"
        self.save()
        atomic_write(path, raw)
        return self._commit_result(result, digest)

    def __getattr__(self, name):
        if name in {"mode", "source_info", "source_ref", "operation_ids", "owned_uploads"}:
            return copy.deepcopy(self.__dict__.get("data", {}).get(name))
        raise AttributeError(name)
