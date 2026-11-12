"""Whole-recording route selection; only local failures permit a new submission."""
from __future__ import annotations

import copy
from pathlib import Path

from .errors import SpeechError
from .jobs import JobHandle
from .media import AudioInputSpec, _probe
from .storage import UploadReceipt

_SETTINGS = {"language_code", "diarize", "timestamps", "literature_text", "api_version", "transport"}
_OPTIONS = _SETTINGS | {"source_info", "audio_input", "audio_track", "bucket", "endpoint", "prefix",
                        "use_storage", "deadline_seconds", "poll_interval", "_on_prepared", "_on_accepted", "_require_job"}


def _duration(path, audio_input, track, timeout):
    source = Path(path).expanduser().absolute()
    if not source.is_file():
        raise SpeechError("UNSUPPORTED_AUDIO", "the addressed recording is not a readable file")
    spec = audio_input or AudioInputSpec()
    if spec.source_format == "lpcm":
        _, rate, channels, width = spec.raw_values()
        size = source.stat().st_size
        if size % (width * channels):
            raise SpeechError("UNSUPPORTED_AUDIO", "raw source ends inside a sample frame")
        return size / (rate * channels * width) * 1000
    return _probe(source, track, timeout=timeout)[1]


def _storage_ready(client, options):
    uploader = client._storage_uploader(**{key: options.get(key) for key in ("bucket", "endpoint", "prefix")})
    if getattr(uploader, "_provided_client", None) is None:
        uploader._credentials()
        try:
            import boto3
        except ImportError:
            raise SpeechError("PREREQUISITE", "boto3 is unavailable for helper uploads") from None


def _prepared_job(error):
    if error.category not in {"PREREQUISITE", "LIMIT"}:
        return None
    if error.receipt_path is None:
        return False
    try:
        job = JobHandle.load(error.receipt_path)
    except SpeechError:
        return None
    if job.data.get("mode") == "async-partitioned":
        return None
    return job if job.status == "prepared" and not job.operation_ids else None


def _rejected_upload(error):
    if error.category not in {"AUTH", "PERMISSION"} or error.receipt_path is None:
        return False
    try:
        receipt = UploadReceipt.load(error.receipt_path)
    except SpeechError:
        return False
    return receipt.status == "failed" and receipt.data.get("error_code") in {
        "AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch", "401"}


def _owned_source(job):
    if not job or not job.owned_uploads:
        return None
    reference = job.owned_uploads[0]
    receipt = UploadReceipt.load(reference["manifest_path"])
    if receipt.reference() != reference or receipt.uri != job.source_ref or receipt.status != "uploaded":
        raise SpeechError("STATE", "saved preparation does not identify its owned source")
    return receipt


def transcribe_recording(client, source, options):
    options = dict(options)
    if options.keys() - _OPTIONS:
        raise SpeechError("CONFIG", "unknown standalone recording options")
    settings = client._options(**{key: value for key, value in options.items() if key in _SETTINGS})
    version, policy = settings.value("api_version"), settings.value("transport")
    if not client.api_key:
        raise SpeechError("AUTH", "YANDEX_SPEECH_API_KEY is required")
    deadline = client._deadline(options.pop("deadline_seconds", 1800))
    require_job = options.pop("_require_job", False)
    if type(require_job) is not bool:
        raise SpeechError("CONFIG", "internal job requirement must be boolean")
    if version == "2":
        return client.transcribe_async_s3(source, deadline_seconds=client._remaining(deadline, cap=None), **options)
    interval = options.pop("poll_interval", 1.0)
    if type(interval) not in (int, float) or not 0 < interval <= 120:
        raise SpeechError("CONFIG", "poll interval must be in (0, 120] seconds")
    raw = not settings.value("literature_text")
    metadata = settings.value("enable_timestamps") or settings.value("enable_diarization")
    legacy_allowed = version in {"auto", "1"} and policy in {"auto", "rest"} and not metadata and not require_job and (raw or version == "1")
    supplied_uri = getattr(source, "uri", None) or (isinstance(source, str) and source.startswith("https://"))
    cloud_required = options.get("use_storage") is True or options.get("bucket") is not None and options.get("use_storage") is not False
    cloud_available = cloud_required or options.get("use_storage") is not False and bool(
        settings.value("object_storage").get("bucket"))
    routing = {"requested_api_version": version, "requested_transport": policy, "attempts": [], "reasons": []}

    def legacy(*, allow_windows_on_limit=True):
        if supplied_uri or not legacy_allowed:
            raise SpeechError("PREREQUISITE", "available text-only v1 cannot fulfill the selected version or required features")
        duration = _duration(source, options.get("audio_input"), options.get("audio_track"),
                             client._remaining(deadline, cap=None))
        method = "transcribe_direct" if duration <= 30_000 else "transcribe_chunked_direct"
        allowed = _SETTINGS | {"audio_input", "audio_track"}
        selected = {key: value for key, value in options.items() if key in allowed}
        selected.update(api_version="1", transport="rest",
                        deadline_seconds=client._remaining(deadline, cap=None))
        routing["attempts"].append({"api_version": "1", "transport": "rest", "mode": method})
        try:
            value = client._legacy_call(method, source, **selected)
        except SpeechError as error:
            if not allow_windows_on_limit or method != "transcribe_direct" or error.category != "LIMIT":
                raise
            routing["reasons"].append({"kind": "direct-local-limit", "category": "LIMIT"})
            routing["attempts"].append({"api_version": "1", "transport": "rest", "mode": "transcribe_chunked_direct"})
            selected["deadline_seconds"] = client._remaining(deadline, cap=None)
            value = client._legacy_call("transcribe_chunked_direct", source, **selected)
        value["routing"] = copy.deepcopy(routing)
        return value

    if version == "1":
        if policy == "grpc" or cloud_required:
            raise SpeechError("CONFIG", "API v1 conflicts with the selected transport or storage operation")
        return legacy()
    if (legacy_allowed and raw and policy == "auto" and not supplied_uri and not cloud_required
            and _duration(source, options.get("audio_input"), options.get("audio_track"),
                          client._remaining(deadline, cap=None)) <= 30_000):
        routing["reasons"].append({"kind": "short-raw-direct", "message": "eligible unformatted short recording"})
        try:
            return legacy(allow_windows_on_limit=False)
        except SpeechError as error:
            if error.category != "LIMIT":
                raise
            routing["reasons"].append({"kind": "direct-local-limit", "category": error.category})

    modes = ["uri"] if supplied_uri else (["storage"] if cloud_required else ["inline"])
    if cloud_available and not supplied_uri and not cloud_required:
        modes.append("storage")
    transports = ["grpc", "rest"] if policy == "auto" else [policy]
    last_error, storage_disabled, working_source = None, False, source
    for transport in transports:
        inline_oversized = False
        for mode in modes:
            if mode == "storage":
                if storage_disabled or not cloud_required and not inline_oversized:
                    continue
                try:
                    _storage_ready(client, options)
                except SpeechError as error:
                    if cloud_required or error.category == "CONFIG":
                        raise
                    storage_disabled = True
                    routing["reasons"].append({"kind": "storage-unavailable", "category": error.category, "code": error.code})
                    last_error = error
                    continue
            selected = dict(options)
            selected.update(api_version="3", transport=transport, use_storage=mode == "storage",
                            deadline_seconds=client._remaining(deadline, cap=None))
            routing["attempts"].append({"api_version": "3", "transport": transport, "input": mode})
            try:
                job = client.start_transcription(working_source, **selected)
            except SpeechError as error:
                if mode == "storage" and not cloud_required and _rejected_upload(error):
                    storage_disabled = True
                    routing["reasons"].append({"kind": "storage-denied", "category": error.category, "code": error.code})
                    last_error = error
                    continue
                prepared = _prepared_job(error)
                if prepared is None:
                    raise
                routing["reasons"].append({"kind": "local-preflight", "transport": transport,
                                           "input": mode, "category": error.category, "code": error.code})
                last_error = error
                if prepared:
                    owned = _owned_source(prepared)
                    if owned is not None:
                        working_source = owned
                    break
                inline_oversized = mode == "inline" and error.code == "INLINE_SIZE"
                continue
            job.data["routing"] = copy.deepcopy(routing)
            job.save()
            # Accepted cloud work is recovered. Result errors never enter the
            # submission fallback loop, even when they leave a partial result.
            value = client.poll_until_complete(job, deadline_seconds=client._remaining(deadline, cap=None),
                                               poll_interval=interval)
            value["routing"] = copy.deepcopy(routing)
            return value
    if legacy_allowed and not cloud_required and not supplied_uri:
        routing["reasons"].append({"kind": "async-unavailable", "message": "eligible raw-text window fallback"})
        return legacy()
    if last_error is not None:
        raise last_error
    raise SpeechError("PREREQUISITE", "no compatible recording route is locally ready")
