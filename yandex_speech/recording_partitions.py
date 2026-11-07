"""Explicit async partition submission and recovery without rebilling accepted children."""
from __future__ import annotations

import contextlib
import copy
from pathlib import Path
import threading
import time

from .errors import SpeechError
from .grpc_stt import build_file_request, operation_dict, rpc_error
from .jobs import JobHandle
from .media import PreparedAudio
from .partition_audio import _hash_file, prepare_parts
from .partition_assembly import assemble_parts
from .partition_jobs import PartitionJob
from .rest_stt import build_rest_request


class _Attempt:
    def __init__(self, client, job, deadline):
        self.client, self.job, self.deadline = client, job, deadline
        self.child = None
        self._lock = threading.Lock()

    def select(self, child):
        with self._lock:
            self.child = child
        if self.job.cancel_event.is_set() and child is not None:
            self.client.cancel(child)

    def cancel_all(self):
        self.job.cancel_event.set()
        with self._lock:
            child = self.child
        if child is not None:
            self.client.cancel(child)

    def check(self):
        if self.job.cancel_event.is_set():
            raise SpeechError("CANCELLED", "local partition operation cancelled; accepted work remains recoverable")
        return self.client._remaining(self.deadline, cap=None)


@contextlib.contextmanager
def _foreground(client, job, deadline, *, fresh=False):
    with job.lock():
        if fresh:
            job.cancel_event = threading.Event()
        attempt = _Attempt(client, job, deadline)
        with client._active_lock:
            client._active[job.job_id] = attempt
        try:
            yield attempt
        except SpeechError as error:
            error.receipt_path = str(job.manifest_path)
            raise
        except OSError:
            raise SpeechError("PREREQUISITE", "cannot persist or read private partition state",
                              receipt_path=str(job.manifest_path)) from None
        finally:
            with client._active_lock:
                if client._active.get(job.job_id) is attempt:
                    client._active.pop(job.job_id)


def _child_info(job, part):
    return {"original_path": part["audio_path"], "source_sha256": part["audio_sha256"],
            "original_size_bytes": part["size_bytes"], "duration_ms": part["duration_ms"],
            "timeline_offset_seconds": 0.0, "converted": False,
            "prepared": copy.deepcopy(job.data["source_info"]["prepared"])}


def _submit(attempt, index, child, on_accepted):
    # These gates bound actual child RPCs and inline payload memory, not parent waits.
    from .transcription import _INLINE, _OPERATIONS
    client, job = attempt.client, attempt.job
    part = job.data["parts"][index]
    if not _OPERATIONS.acquire(blocking=False):
        raise SpeechError("RATE_LIMIT", "two local speech operations are already active")
    inline, rpc = False, None
    try:
        attempt.check()
        inline = _INLINE.acquire(blocking=False)
        if not inline:
            raise SpeechError("RATE_LIMIT", "one local inline upload is already active")
        path = Path(part["audio_path"])
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size != part["size_bytes"]:
            raise SpeechError("STATE", "the prepared part no longer matches its saved identity")
        if _hash_file(path, time.monotonic() + attempt.check(), job.cancel_event) != part["audio_sha256"]:
            raise SpeechError("STATE", "the prepared part changed; accepted audio must not be replaced")
        if child is None:
            child = JobHandle.create(client.job_dir, _child_info(job, part),
                parameters=job.data["parameters"], api_version="3", transport=job.data["transport"],
                deadline_at=attempt.deadline)
            child.cancel_event = job.cancel_event
            part.update(child_manifest_path=str(child.manifest_path), child_job_id=child.job_id)
            # This reference is durable before this child is allowed to submit.
            job.save()
        attempt.select(child)
        with child.lock():
            validated = job.child(index)
            child.data = validated.data
            if child.operation_id or child.status != "prepared":
                raise SpeechError("SUBMISSION_UNCERTAIN", "only a never-submitted prepared child can start recognition")
            channels = job.data["source_info"]["prepared"]["channels"]
            prepared = PreparedAudio(path, "WAV", 16000, channels, part["duration_ms"], _child_info(job, part))
            parameters = job.data["parameters"]
            builder = build_file_request if child.data["transport"] == "grpc" else build_rest_request
            request = builder(prepared, language=parameters["language"], diarize=parameters["diarize"],
                              literature_text=parameters["literature_text"])
            try:
                rpc = client._rpc(child)
            except SpeechError:
                raise
            except Exception:
                raise SpeechError("PREREQUISITE", "selected partition transport could not initialize locally",
                                  code="TRANSPORT_INIT") from None
            attempt.check()
            child.set_status("submitting")
            try:
                receipt = operation_dict(rpc.recognize_file(request, min(120, attempt.check())))
            except SpeechError as error:
                child.set_status("submission_uncertain" if error.category == "SUBMISSION_UNCERTAIN" else "failed")
                raise
            except Exception as error:
                failure = rpc_error(error, submitting=True)
                child.set_status("submission_uncertain" if failure.category == "SUBMISSION_UNCERTAIN" else "failed")
                raise failure from None
            operation_id = receipt.get("id")
            if not isinstance(operation_id, str) or not operation_id:
                child.set_status("submission_uncertain")
                raise SpeechError("SUBMISSION_UNCERTAIN", "provider returned no recoverable child operation ID")
            child.data["operation_ids"] = [operation_id]
            child.data["parts"][0].update(operation_id=operation_id, status="running")
            child.set_status("running")
        # The child receipt above remains acceptance authority if either callback fails.
        job.reconcile_children()
        if on_accepted is not None:
            on_accepted(job)
        attempt.check()
    finally:
        if child is not None:
            client._release_rpc(child, rpc)
        attempt.select(None)
        if inline:
            _INLINE.release()
        _OPERATIONS.release()


def _submit_remaining(attempt, *, on_accepted=None):
    job = attempt.job
    # Inspect all existing child receipts before permitting any new paid operation.
    children = job.reconcile_children()
    if any(part["submission_outcome"] == "unknown" for part in job.parts):
        raise SpeechError("SUBMISSION_UNCERTAIN",
                          "a child may have been accepted without a saved ID; no audio is automatically resubmitted")
    for index, (part, child) in enumerate(zip(job.parts, children)):
        attempt.check()
        if part["submission_outcome"] == "accepted":
            continue
        _submit(attempt, index, child, on_accepted)
    job.set_status("running")


def start_partitioned(client, source, *, audio_input=None, audio_track=None,
                      language_code=None, diarize=None, timestamps=None, literature_text=None,
                      api_version=None, transport=None, bucket=None, endpoint=None, prefix=None,
                      use_storage=None, deadline_seconds=1800, allow_partitioned_speakers=False,
                      _on_prepared=None, _on_accepted=None):
    settings = client._options(language_code=language_code, diarize=diarize, timestamps=timestamps,
                               literature_text=literature_text, api_version=api_version, transport=transport)
    if settings.value("api_version") not in {"auto", "3"}:
        raise SpeechError("CONFIG", "explicit inline partitioning requires API v3")
    if (bucket is not None or endpoint is not None or prefix is not None or use_storage is True
            or use_storage is not None and type(use_storage) is not bool):
        raise SpeechError("CONFIG", "explicit inline partitioning conflicts with explicit storage selection")
    if type(allow_partitioned_speakers) is not bool:
        raise SpeechError("CONFIG", "allow_partitioned_speakers must be a boolean")
    if any(callback is not None and not callable(callback) for callback in (_on_prepared, _on_accepted)):
        raise SpeechError("CONFIG", "internal job callbacks must be callable")
    if not isinstance(source, (str, Path)) or str(source).startswith(("https://", "http://")):
        raise SpeechError("CONFIG", "inline partitioning requires the addressed local recording")
    if not client.api_key:
        raise SpeechError("AUTH", "YANDEX_SPEECH_API_KEY is required")
    actual, reasons = client._recorded_transport(settings.value("transport"), "3")
    deadline = client._deadline(deadline_seconds)
    parameters = {"language": settings.value("language_code"), "diarize": settings.value("enable_diarization"),
                  "timestamps": settings.value("enable_timestamps"), "literature_text": settings.value("literature_text")}
    job = PartitionJob.create(client.job_dir, source, parameters=parameters, transport=actual,
        transports=(actual,), requested_transport=settings.value("transport"), deadline_at=deadline,
        allow_partitioned_speakers=allow_partitioned_speakers)
    with _foreground(client, job, deadline) as attempt:
        info, parts = prepare_parts(source, job.manifest_path.parent, transports=(actual,),
            language=parameters["language"], diarize=parameters["diarize"], literature_text=parameters["literature_text"],
            audio_input=audio_input, audio_track=audio_track,
            deadline=time.monotonic() + attempt.check(), cancel_event=job.cancel_event)
        if parameters["diarize"] and len(parts) > 1 and not allow_partitioned_speakers:
            raise SpeechError("PREREQUISITE", "multiple recognition jobs cannot share speaker identity; use one existing "
                              "authorized Yandex Object Storage job, or explicitly allow partition-local speakers")
        job.install_preparation(info, parts)
        job.data["routing"]["reasons"] = reasons + ["explicit inline partitioning; configured storage preference bypassed"]
        job.save()
        if _on_prepared is not None:
            _on_prepared(job)
        _submit_remaining(attempt, on_accepted=_on_accepted)
    return job


def resume_partitioned(client, job_or_path, *, api_version=None, deadline_seconds=1800):
    job = job_or_path if isinstance(job_or_path, PartitionJob) else PartitionJob.load(job_or_path)
    if api_version not in (None, "auto") and str(api_version) != "3":
        raise SpeechError("CONFIG", "explicit recovery version conflicts with the saved partition job")
    deadline = client._deadline(deadline_seconds)
    with _foreground(client, job, deadline, fresh=True) as attempt:
        if job.cached_result() is not None:
            return job
        if not job.data["preparation_complete"]:
            raise SpeechError("STATE", "partition preparation did not finish; no new paid operation was started")
        _submit_remaining(attempt)
    job.deadline_at = deadline
    return job


def poll_partitioned(client, job, *, deadline_seconds=None, transport=None, progress=None, poll_interval=1.0):
    deadline = client._deadline(deadline_seconds) if deadline_seconds is not None else job.deadline_at or client._deadline(1800)
    with _foreground(client, job, deadline) as attempt:
        cached = job.cached_result()
        if cached is not None:
            return cached
        children = job.reconcile_children()
        if (not job.data["preparation_complete"]
                or any(part["submission_outcome"] != "accepted" for part in job.parts)):
            raise SpeechError("SUBMISSION_UNCERTAIN",
                              "not all parts have accepted operations; use explicit partition resume for never-sent parts")
        results = []
        for child in children:
            attempt.check()
            attempt.select(child)
            results.append(client.poll_until_complete(child, deadline_seconds=attempt.check(),
                transport=transport, progress=progress, poll_interval=poll_interval))
            job.reconcile_children()
        attempt.select(None)
        attempt.check()
        return job.store_result(assemble_parts(job, results))
