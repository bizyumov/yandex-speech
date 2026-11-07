"""Durable multipart recording ownership; child receipts are acceptance authority."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import threading

from .artifacts import private_job_dir
from .errors import SpeechError
from .jobs import JobHandle, STATUSES
from .partition_audio import frame_windows

MODE = "async-partitioned"
_END = {"completed", "no_speech"}
_HASH = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[a-f0-9]{32}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate receipt key")
        result[key] = value
    return result


class PartitionJob(JobHandle):
    def __init__(self, manifest_path, data, *, deadline_at=None):
        super().__init__(manifest_path, data, deadline_at=deadline_at)
        self._foreground_lock = threading.Lock()
        self._foreground_child = None

    @classmethod
    def create(cls, base, source, *, parameters, transport, transports, requested_transport, deadline_at,
               allow_partitioned_speakers=False):
        directory = private_job_dir(Path(base).expanduser().absolute())
        data = {
            "schema_version": 1, "job_id": directory.name, "mode": MODE, "api_version": "3",
            "transport": transport, "submission_transports": list(transports),
            "requested_transport": requested_transport, "status": "prepared",
            "preparation_complete": False, "allow_partitioned_speakers": allow_partitioned_speakers, "source_ref": str(Path(source).expanduser().absolute()),
            "source_info": {"original_path": str(Path(source).expanduser().absolute())},
            "parameters": copy.deepcopy(parameters), "parts": [], "operation_ids": [],
            "owned_uploads": [], "routing": {"requested_transport": requested_transport, "reasons": []},
        }
        cls.validate_data(directory / "job.json", data)
        job = cls(directory / "job.json", data, deadline_at=deadline_at)
        job.save()
        return job

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser().absolute()
        try:
            if path.is_symlink() or path.stat().st_size > 16777216:
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            return cls.from_data(path, data)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            raise SpeechError("CONFIG", "invalid durable recording partition receipt") from None

    @classmethod
    def from_data(cls, path, data):
        path = Path(path).expanduser().absolute()
        cls.validate_data(path, data)
        return cls(path, data)

    @staticmethod
    def validate_data(path, data):
        try:
            if (not isinstance(data, dict) or data.get("schema_version") != 1
                    or type(data.get("schema_version")) is not int
                    or data.get("mode") != MODE or data.get("api_version") != "3"
                    or path.name != "job.json" or not _ID.fullmatch(data.get("job_id", ""))
                    or path.parent.name != data["job_id"] or data.get("status") not in STATUSES
                    or data.get("transport") not in {"grpc", "rest"}
                    or data.get("requested_transport") not in {"auto", "grpc", "rest"}
                    or type(data.get("preparation_complete")) is not bool
                    or type(data.get("allow_partitioned_speakers")) is not bool):
                raise ValueError
            transports = data["submission_transports"]
            if (not isinstance(transports, list) or not transports
                    or len(transports) != len(set(transports))
                    or any(value not in {"grpc", "rest"} for value in transports)
                    or data["transport"] not in transports
                    or data["requested_transport"] != "auto" and transports != [data["requested_transport"]]):
                raise ValueError
            parameters = data["parameters"]
            if (set(parameters) != {"language", "diarize", "timestamps", "literature_text"}
                    or not isinstance(parameters["language"], str) or not parameters["language"]
                    or any(type(parameters[name]) is not bool
                           for name in ("diarize", "timestamps", "literature_text"))):
                raise ValueError
            info, parts, identifiers = data["source_info"], data["parts"], data["operation_ids"]
            if (not isinstance(info, dict) or not isinstance(info.get("original_path"), str)
                    or not Path(info["original_path"]).is_absolute()
                    or not isinstance(parts, list) or len(parts) > 10000
                    or not isinstance(identifiers, list)
                    or any(not isinstance(value, str) or not value for value in identifiers)
                    or len(identifiers) != len(set(identifiers))):
                raise ValueError
            if not data["preparation_complete"]:
                if parts or identifiers or data["status"] in _END:
                    raise ValueError
                return
            if (not _HASH.fullmatch(info.get("source_sha256", ""))
                    or type(info.get("decoded_frames")) is not int or info["decoded_frames"] < 0
                    or not isinstance(info.get("prepared"), dict)
                    or info["prepared"].get("sample_rate_hertz") != 16000
                    or type(info["prepared"].get("channels")) is not int or info["prepared"]["channels"] not in {1, 2}
                    or type(info.get("timeline_offset_seconds")) not in (int, float)
                    or not math.isfinite(info["timeline_offset_seconds"]) or info["timeline_offset_seconds"] < 0
                    or not isinstance(info.get("inline_wire_limits"), dict)
                    or set(info["inline_wire_limits"]) != set(transports)
                    or any(type(value) is not int or value <= 0 for value in info["inline_wire_limits"].values())
                    or type(info.get("partition_capacity_frames")) is not int):
                raise ValueError
            expected = frame_windows(info["decoded_frames"], info["partition_capacity_frames"], 16000)
            if (len(parts) != len(expected)
                    or parameters["diarize"] and len(parts) > 1 and not data["allow_partitioned_speakers"]):
                raise ValueError
            observed_ids, child_refs = [], set()
            for index, (part, window) in enumerate(zip(parts, expected)):
                audio_path = path.parent / "audio" / f"part-{index:05d}.wav"
                if (not isinstance(part, dict) or part.get("part_id") != str(index)
                        or type(part.get("start_frame")) is not int or type(part.get("end_frame")) is not int
                        or part["start_frame"] != window.start or part["end_frame"] != window.end
                        or type(part.get("offset")) not in (int, float) or part["offset"] != window.start / 16000
                        or type(part.get("duration_ms")) not in (int, float)
                        or part["duration_ms"] != (window.end - window.start) / 16
                        or part.get("audio_path") != str(audio_path)
                        or not _HASH.fullmatch(part.get("audio_sha256", ""))
                        or type(part.get("size_bytes")) is not int or part["size_bytes"] < 44
                        or part.get("status") not in STATUSES
                        or part.get("submission_outcome", "never_sent") not in {"never_sent", "accepted", "unknown", "rejected"}):
                    raise ValueError
                reference, operation = part.get("child_manifest_path"), part.get("operation_id")
                if reference is not None:
                    child = Path(reference)
                    if (not isinstance(reference, str) or not child.is_absolute() or child.name != "job.json"
                            or not _ID.fullmatch(child.parent.name) or child == path or reference in child_refs
                            or part.get("child_job_id") != child.parent.name):
                        raise ValueError
                    child_refs.add(reference)
                elif operation is not None or part.get("child_job_id") is not None:
                    raise ValueError
                if part["status"] in _END and (reference is None or operation is None):
                    raise ValueError
                if operation is not None:
                    if not isinstance(operation, str) or not operation:
                        raise ValueError
                    observed_ids.append(operation)
            if identifiers != observed_ids or len(observed_ids) != len(set(observed_ids)):
                raise ValueError
        except (ValueError, TypeError, AttributeError, KeyError):
            raise SpeechError("CONFIG", "recording partition geometry, policy or child identity is invalid") from None

    def install_preparation(self, source_info, parts):
        previous = self.data
        self.data = copy.deepcopy(previous)
        self.data.update(source_info=copy.deepcopy(source_info), parts=copy.deepcopy(parts),
                         preparation_complete=True)
        for part in self.data["parts"]:
            part["submission_outcome"] = "never_sent"
        try:
            self.validate_data(self.manifest_path, self.data)
            self.save()
        except BaseException:
            self.data = previous
            raise

    def _validate_result(self, result):
        if (not self.data["preparation_complete"]
                or any(part["status"] not in _END for part in self.parts)
                or type(result.get("schema_version")) is not int or result.get("schema_version") != 1
                or result.get("mode") != MODE or result.get("api_version") != "3"
                or result.get("transport") not in {"grpc", "rest", "mixed"}
                or result.get("input_complete") is not True
                or result.get("status") not in _END or result.get("job_id") != self.job_id
                or result.get("source_info", {}).get("source_sha256") != self.data["source_info"]["source_sha256"]
                or not isinstance(result.get("parts"), list) or len(result["parts"]) != len(self.parts)):
            raise SpeechError("OUTPUT_PARTIAL", "every required recording part and its assembly must complete")
        for expected, actual in zip(self.parts, result["parts"]):
            if any(actual.get(key) != expected.get(key) for key in (
                    "part_id", "child_manifest_path", "child_job_id", "operation_id", "start_frame", "end_frame",
                    "status", "saved_result_reference", "saved_result_sha256")):
                raise SpeechError("STATE", "aggregate result differs from its recording part receipts")

    def _commit_result(self, result, digest):
        self._validate_result(result)
        previous = self.data
        self.data = copy.deepcopy(previous)
        self.data["result_sha256"] = digest
        self.data.pop("pending_result_sha256", None)
        self.data["status"] = result["status"]
        try:
            self.save()
        except BaseException:
            self.data = previous
            raise
        return result

    def store_result(self, result):
        self._validate_result(result)
        return super().store_result(result)

    def cached_result(self):
        result = super().cached_result()
        if result is not None:
            self._validate_result(result)
        return result

    def child(self, index):
        part = self.parts[index]
        reference = part.get("child_manifest_path")
        if reference is None:
            return None
        child = JobHandle.load(Path(reference))
        if (child.data.get("mode") != "async" or child.job_id != part["child_job_id"]
                or child.api_version != "3" or child.data["transport"] not in self.data["submission_transports"]
                or child.data["parameters"] != self.data["parameters"]
                or child.data["source_info"].get("source_sha256") != part["audio_sha256"]
                or child.data["source_info"].get("original_path") != part["audio_path"]
                or child.data["source_info"].get("timeline_offset_seconds", 0) != 0
                or part.get("operation_id") is not None and child.operation_id != part["operation_id"]):
            raise SpeechError("STATE", "child receipt does not belong to its recording part and policy")
        child.cancel_event = self.cancel_event
        return child

    def reconcile_children(self):
        children = []
        for index, part in enumerate(self.data["parts"]):
            child = self.child(index)
            children.append(child)
            if child is None:
                part["submission_outcome"] = "never_sent"
                continue
            part["status"] = child.status
            if child.operation_id:
                part["operation_id"] = child.operation_id
                part["submission_outcome"] = "accepted"
                if child.data.get("result_sha256"):
                    part["saved_result_reference"] = str(child.manifest_path.parent / "result.json")
                    part["saved_result_sha256"] = child.data["result_sha256"]
            elif child.status == "prepared":
                part["submission_outcome"] = "never_sent"
            else:
                # A failed/cancelled receipt alone does not prove non-acceptance.
                part["submission_outcome"] = "unknown"
        self.data["operation_ids"] = [part["operation_id"] for part in self.parts if part.get("operation_id")]
        self.validate_data(self.manifest_path, self.data)
        self.save()
        return children
