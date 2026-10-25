"""Phase 1 gRPC v3 orchestration with explicit legacy-route boundaries."""
from __future__ import annotations

import copy
import math
import os
import random
import threading
import time
from pathlib import Path

from .config import RuntimeContext, resolve_settings
from .errors import SpeechError
from .grpc_stt import (GrpcTransport, build_file_request, operation_dict, rpc_error, status_name)
from .jobs import JobHandle
from .media import AudioInputSpec, prepare_audio
from .results import ResultAccumulator

_OPERATIONS = threading.BoundedSemaphore(2)
_INLINE = threading.BoundedSemaphore(1)


class SpeechTranscriber:
    def __init__(self, api_key=None, folder_id=None, *, job_dir=None, _context=None,
                 _adapter_defaults=None, _transport=None, _clock=time.monotonic,
                 _sleep=time.sleep):
        self.api_key = api_key or os.getenv("YANDEX_SPEECH_API_KEY")
        self.folder_id = folder_id or os.getenv("YANDEX_CLOUD_FOLDER_ID")
        self.context = _context or RuntimeContext(
            user_home=Path(os.environ.get("HOME") or Path.home()),
            workspace=Path(os.environ["HERMES_HOME"]) if os.environ.get("HERMES_HOME") else None)
        self.adapter_defaults = _adapter_defaults or {}
        self.settings = resolve_settings(_context=self.context, _adapter=self.adapter_defaults)
        home = self.context.workspace or self.context.user_home or Path.home()
        self.job_dir = Path(job_dir) if job_dir is not None else Path(home) / ".local/share/yandex-speech/jobs"
        self._transport = _transport
        self._clock, self._sleep = _clock, _sleep
        self._active = {}
        self._active_lock = threading.Lock()

    def _options(self, *, language_code=None, diarize=None, timestamps=None,
                 literature_text=None, api_version=None, transport=None):
        explicit = {}
        for key, value in [("language_code", language_code), ("enable_diarization", diarize),
                           ("enable_timestamps", timestamps), ("literature_text", literature_text),
                           ("api_version", str(api_version) if type(api_version) is int else api_version),
                           ("transport", transport)]:
            if value is not None:
                explicit[key] = value
        return resolve_settings(_context=self.context, _adapter=self.adapter_defaults, _explicit=explicit)

    def _deadline(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 86400:
            raise SpeechError("CONFIG", "deadline must be finite and in (0, 86400] seconds")
        return self._clock() + seconds

    def _remaining(self, deadline):
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise SpeechError("TIMEOUT", "speech operation deadline expired; retain the receipt for recovery")
        return min(120.0, remaining)

    def _rpc(self, job):
        if not self.api_key:
            raise SpeechError("AUTH", "YANDEX_SPEECH_API_KEY is required")
        rpc = self._transport or GrpcTransport(self.api_key)
        if hasattr(rpc, "bind_cancel_event"):
            rpc.bind_cancel_event(job.cancel_event)
        with self._active_lock:
            self._active[job.job_id] = rpc
        return rpc

    def _release_rpc(self, job, rpc):
        with self._active_lock:
            self._active.pop(job.job_id, None)
        if rpc is not None:
            rpc.close()

    def cancel(self, job):
        if not isinstance(job, JobHandle):
            raise SpeechError("CONFIG", "cancel requires the foreground job handle")
        job.cancel_event.set()
        with self._active_lock:
            rpc = self._active.get(job.job_id)
        if rpc is not None and hasattr(rpc, "cancel_all"):
            rpc.cancel_all()

    def _retry(self, call, job, deadline):
        for attempt in range(3):
            if job.cancel_event.is_set():
                raise SpeechError("CANCELLED", "local operation cancelled; cloud completion is separate")
            try:
                return call(self._remaining(deadline))
            except SpeechError:
                raise
            except Exception as exc:
                if job.cancel_event.is_set():
                    raise SpeechError("CANCELLED", "local operation cancelled; cloud completion is separate") from None
                code = status_name(exc)
                if code not in {"UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED"} or attempt == 2:
                    raise rpc_error(exc) from None
                delay = min((1 << attempt) + random.uniform(0, 0.25), self._remaining(deadline))
                if self._sleep is time.sleep:
                    if job.cancel_event.wait(delay):
                        raise SpeechError("CANCELLED", "local operation cancelled") from None
                else:
                    self._sleep(delay)
        raise SpeechError("STATE", "retry budget exhausted")

    def start_transcription(self, source, *, source_info=None, audio_input=None,
                            audio_track=None, language_code=None, diarize=None,
                            timestamps=None, literature_text=None, api_version=None,
                            transport=None, deadline_seconds=1800):
        settings = self._options(language_code=language_code, diarize=diarize,
            timestamps=timestamps, literature_text=literature_text, api_version=api_version,
            transport=transport)
        if settings.value("api_version") not in {"auto", "3"} or settings.value("transport") == "rest":
            raise SpeechError("PREREQUISITE", "durable v3 jobs in Phase 1 require gRPC; existing direct/async-s3 entry points remain available and REST v3 is Phase 2")
        if not self.api_key:
            raise SpeechError("AUTH", "YANDEX_SPEECH_API_KEY is required")
        deadline = self._deadline(deadline_seconds)
        if not _OPERATIONS.acquire(blocking=False):
            raise SpeechError("RATE_LIMIT", "two local speech operations are already active")
        rpc, job, inline = None, None, False
        try:
            params = {"language": settings.value("language_code"),
                      "diarize": settings.value("enable_diarization"),
                      "timestamps": settings.value("enable_timestamps"),
                      "literature_text": settings.value("literature_text")}
            uri = getattr(source, "uri", None)
            if uri is not None:
                source_info = getattr(source, "source_info", source_info)
            elif isinstance(source, str) and source.startswith("https://"):
                uri = source
            if uri is not None:
                info = copy.deepcopy(source_info or {})
                info["metadata_origin"] = "caller_supplied"
                info["uri"] = str(uri)
                request = build_file_request(uri=uri, source_info=info,
                    language=params["language"], diarize=params["diarize"],
                    literature_text=params["literature_text"])
            else:
                inline = _INLINE.acquire(blocking=False)
                if not inline:
                    raise SpeechError("RATE_LIMIT", "one local inline upload is already active")
                with prepare_audio(Path(source), audio_input=audio_input, audio_track=audio_track,
                                   timeout=self._remaining(deadline)) as prepared:
                    info = copy.deepcopy(prepared.source_info)
                    request = build_file_request(prepared, language=params["language"],
                        diarize=params["diarize"], literature_text=params["literature_text"])
            job = JobHandle.create(self.job_dir, info, parameters=params, deadline_at=deadline)
            rpc = self._rpc(job)
            job.set_status("submitting")
            try:
                receipt = operation_dict(rpc.recognize_file(request, self._remaining(deadline)))
            except SpeechError as exc:
                job.set_status("submission_uncertain" if exc.category == "SUBMISSION_UNCERTAIN" else "failed")
                raise
            except Exception as exc:
                error = rpc_error(exc, submitting=True)
                job.set_status("submission_uncertain" if error.category == "SUBMISSION_UNCERTAIN" else "failed")
                raise error from None
            operation_id = receipt.get("id")
            if not isinstance(operation_id, str) or not operation_id:
                job.set_status("submission_uncertain")
                raise SpeechError("SUBMISSION_UNCERTAIN", "provider returned no recoverable operation ID; retain the receipt")
            job.data["operation_ids"] = [operation_id]
            job.data["parts"][0].update({"operation_id": operation_id, "status": "running"})
            job.set_status("running")
            return job
        finally:
            if job is not None:
                self._release_rpc(job, rpc)
            if inline:
                _INLINE.release()
            _OPERATIONS.release()

    def poll_until_complete(self, job_or_id, *, api_version=None, deadline_seconds=None,
                            progress=None):
        if isinstance(job_or_id, JobHandle):
            job = job_or_id
        elif isinstance(job_or_id, Path):
            job = JobHandle.load(job_or_id)
        elif isinstance(job_or_id, str):
            if str(api_version) != "3":
                raise SpeechError("CONFIG", "a bare operation ID requires its explicit supported API version")
            job = JobHandle.create(self.job_dir, {"metadata_origin": "existing_operation"},
                parameters={"language": self.settings.value("language_code"),
                            "diarize": self.settings.value("enable_diarization"),
                            "timestamps": self.settings.value("enable_timestamps"),
                            "literature_text": self.settings.value("literature_text")})
            job.data["operation_ids"] = [job_or_id]
            job.data["parts"][0]["operation_id"] = job_or_id
            job.set_status("running")
        else:
            raise SpeechError("CONFIG", "provide a job handle, receipt Path or operation ID")
        if api_version is not None and str(api_version) != job.api_version:
            raise SpeechError("CONFIG", "explicit recovery version conflicts with the saved job")
        if job.status == "submission_uncertain" or not job.operation_id:
            raise SpeechError("SUBMISSION_UNCERTAIN", "receipt has no accepted operation ID; do not resubmit it automatically")
        deadline = self._deadline(deadline_seconds) if deadline_seconds is not None else job.deadline_at or self._deadline(1800)
        if not _OPERATIONS.acquire(blocking=False):
            raise SpeechError("RATE_LIMIT", "two local speech operations are already active")
        rpc = None
        try:
            with job.lock():
                cached = job.cached_result()
                if cached is not None:
                    return cached
                rpc = self._rpc(job)
                while True:
                    operation = operation_dict(self._retry(
                        lambda timeout: rpc.get_operation(job.operation_id, timeout), job, deadline))
                    if operation.get("error"):
                        job.set_status("failed")
                        raise SpeechError("PROVIDER", "the accepted cloud operation failed", code=str(operation["error"].get("code", "")))
                    if progress:
                        progress({"operation_id": job.operation_id, "status": "results_ready" if operation.get("done") else "running"})
                    if operation.get("done"):
                        break
                    if job.cancel_event.wait(min(1.0, self._remaining(deadline))):
                        raise SpeechError("CANCELLED", "local wait cancelled; receipt remains recoverable")
                job.set_status("results_ready")
                def retrieve(timeout):
                    accumulator = ResultAccumulator(
                        literature_text=job.data["parameters"]["literature_text"],
                        diarize=job.data["parameters"]["diarize"],
                        timestamps=job.data["parameters"]["timestamps"])
                    for event in rpc.get_recognition(job.operation_id, timeout):
                        if job.cancel_event.is_set():
                            raise SpeechError("CANCELLED", "local result retrieval cancelled")
                        self._remaining(deadline)
                        accumulator.feed(event)
                    return accumulator.result(source_info=job.data["source_info"])
                result = self._retry(retrieve, job, deadline)
                result.update({"operation_id": job.operation_id, "job_id": job.job_id})
                return job.store_result(result)
        except SpeechError as exc:
            if exc.category in {"CANCELLED", "TIMEOUT"}:
                job.set_status("cancelled" if exc.category == "CANCELLED" else "failed")
            raise
        finally:
            self._release_rpc(job, rpc)
            _OPERATIONS.release()

    def get_result(self, job, **options):
        return self.poll_until_complete(job, **options)

    def transcribe_auto(self, path, **options):
        job = self.start_transcription(path, **options)
        return self.poll_until_complete(job)

    def open_stream(self, *, audio_input=None, language_code=None, diarize=None,
                    timestamps=None, literature_text=None, api_version=None,
                    transport=None, deadline_seconds=1800, **unknown):
        if unknown:
            raise SpeechError("CONFIG", "unknown standalone streaming options")
        settings = self._options(language_code=language_code, diarize=diarize,
            timestamps=timestamps, literature_text=literature_text,
            api_version=api_version, transport=transport)
        if settings.value("api_version") not in {"auto", "3"} or settings.value("transport") == "rest":
            raise SpeechError("CONFIG", "recognition during arrival requires gRPC API v3")
        if settings.value("enable_diarization"):
            raise SpeechError("CONFIG", "mixed-recording speaker labeling requires the recorded-file method")
        if not self.api_key:
            raise SpeechError("AUTH", "YANDEX_SPEECH_API_KEY is required")
        self._deadline(deadline_seconds)
        if not _OPERATIONS.acquire(blocking=False):
            raise SpeechError("RATE_LIMIT", "two local speech operations are already active")
        rpc = None
        try:
            from .streaming import TranscriptionSession
            rpc = self._transport or GrpcTransport(self.api_key)
            return TranscriptionSession(rpc, audio_input=audio_input,
                language=settings.value("language_code"),
                literature_text=settings.value("literature_text"),
                timestamps=settings.value("enable_timestamps"),
                _deadline_seconds=deadline_seconds, _on_close=_OPERATIONS.release)
        except BaseException:
            try:
                if rpc is not None:
                    rpc.close()
            finally:
                _OPERATIONS.release()
            raise

