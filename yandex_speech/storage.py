"""Private conditional Object Storage uploads and durable, version-scoped ownership."""

from __future__ import annotations

import base64
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from .artifacts import atomic_write, private_job_dir
from .config import RuntimeContext, resolve_settings
from .errors import SpeechError
from .media import prepare_audio

_MAX_OBJECT_BYTES = 1_000_000_000
_MAX_DURATION_MS = 14_400_000
_STATUSES = {"prepared", "uploading", "uploaded", "upload_uncertain", "failed",
             "deleting", "deleted", "cleanup_uncertain"}
_FIELDS = {"schema_version", "kind", "upload_id", "status", "bucket", "endpoint", "key",
           "uri", "source_info", "source_sha256", "uploaded_sha256", "size_bytes",
           "version_id", "etag", "retention", "error_code"}


def _coordinates(bucket, endpoint, prefix="speechkit"):
    if (not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            or ".." in bucket):
        raise SpeechError("CONFIG", "select an existing authorized Object Storage bucket")
    try:
        parsed = urlsplit(endpoint)
        valid = (parsed.scheme == "https" and parsed.hostname == "storage.yandexcloud.net"
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and parsed.path in ("", "/"))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SpeechError("CONFIG", "private recognition uploads require the supported HTTPS Object Storage endpoint")
    if (not isinstance(prefix, str) or not prefix.strip("/")
            or any(part in ("", ".", "..") for part in prefix.strip("/").split("/"))
            or any(ord(char) < 32 for char in prefix)):
        raise SpeechError("CONFIG", "storage prefix must be a nonempty relative key namespace")
    return bucket, endpoint.rstrip("/"), prefix.strip("/")


def _uri(endpoint, bucket, key):
    return f"{endpoint}/{bucket}/{quote(key, safe='/')}"


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise SpeechError("TIMEOUT", "storage operation exceeded its deadline")
    return value


def _deadline(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise SpeechError("CONFIG", "storage deadline must be positive and finite")
    return time.monotonic() + seconds


def _fingerprint(path, deadline):
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with Path(path).open("rb") as source:
        while data := source.read(1_048_576):
            _remaining(deadline)
            size += len(data)
            if size > _MAX_OBJECT_BYTES:
                raise SpeechError("LIMIT", "prepared object exceeds the single-object storage budget")
            sha.update(data)
            md5.update(data)
    return sha.hexdigest(), base64.b64encode(md5.digest()).decode(), size


def _error(error, *, mutation=False):
    response = getattr(error, "response", {})
    code = str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""
    if code in {"PreconditionFailed", "412"}:
        return SpeechError("STATE", "upload key collision; existing object is preserved", code="UPLOAD_COLLISION")
    category = {"AccessDenied": "PERMISSION", "403": "PERMISSION",
                "InvalidAccessKeyId": "AUTH", "SignatureDoesNotMatch": "AUTH",
                "401": "AUTH", "SlowDown": "RATE_LIMIT", "429": "RATE_LIMIT"}.get(code)
    if category:
        return SpeechError(category, "storage request denied or limited; inspect the selected principal and resource",
                           code=code)
    if mutation:
        return SpeechError("OUTPUT_PARTIAL", "storage mutation outcome is uncertain; retain its ownership receipt",
                           code="UPLOAD_UNCERTAIN")
    return SpeechError("PROVIDER", "storage metadata is unavailable; do not infer missing grants or retry upload",
                       code=code or None)

@contextlib.contextmanager
def _receipt_errors(receipt):
    try:
        yield
    except SpeechError as error:
        error.receipt_path = str(receipt.manifest_path)
        raise


class UploadReceipt:
    """URI/probed metadata plus a private uploading-operation ownership record."""

    def __init__(self, manifest_path, data):
        self.manifest_path = Path(manifest_path).expanduser().absolute()
        self.data = data

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser().absolute()
        try:
            if path.is_symlink() or path.name != "upload.json":
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) - _FIELDS
                    or data.get("schema_version") != 1 or data.get("kind") != "yandex-speech-upload"
                    or data.get("status") not in _STATUSES
                    or not re.fullmatch(r"[a-f0-9]{32}", data.get("upload_id", ""))
                    or path.parent.name != data["upload_id"]):
                raise ValueError
            _coordinates(data["bucket"], data["endpoint"])
            if (data["upload_id"] not in data["key"].split("/")
                    or data["uri"] != _uri(data["endpoint"], data["bucket"], data["key"])
                    or not isinstance(data["source_info"], dict)
                    or not re.fullmatch(r"[a-f0-9]{64}", data["uploaded_sha256"])):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise SpeechError("CONFIG", "invalid private upload ownership receipt") from None
        return cls(path, data)

    def save(self):
        if self.manifest_path.exists():
            existing = type(self).load(self.manifest_path)
            if existing.reference() != self.reference():
                raise SpeechError("STATE", "upload identity changed; existing receipt is preserved")
        atomic_write(self.manifest_path, json.dumps(self.data, ensure_ascii=False, indent=2,
                     allow_nan=False).encode(), overwrite=self.manifest_path.exists())

    @contextlib.contextmanager
    def lock(self):
        fd = None
        try:
            fd = os.open(self.manifest_path.parent / ".upload-lock",
                         os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if fd is not None:
                os.close(fd)
            raise SpeechError("STATE", "another attempt owns this upload receipt") from None
        try:
            self.data = type(self).load(self.manifest_path).data
            yield
        finally:
            os.close(fd)

    def reference(self):
        return {"manifest_path": str(self.manifest_path), **{
            key: self.data.get(key) for key in ("upload_id", "bucket", "endpoint", "key",
                                               "version_id", "etag", "uploaded_sha256")}}

    @property
    def source_info(self):
        return copy.deepcopy(self.data["source_info"])

    @property
    def ownership_record(self):
        return self.manifest_path

    def __getattr__(self, name):
        if name in {"upload_id", "uri", "bucket", "key", "source_sha256", "size_bytes", "status"}:
            return copy.deepcopy(self.__dict__.get("data", {}).get(name))
        raise AttributeError(name)


class ObjectStorageUploader:
    def __init__(self, bucket=None, endpoint=None, prefix=None, *, aws_access_key_id=None,
                 aws_secret_access_key=None, job_dir=None, _context=None, _adapter_defaults=None,
                 _environment=None, _client=None):
        supplied = os.environ if _environment is None else _environment
        names = {"HOME", "HERMES_HOME", "XDG_STATE_HOME", "YANDEX_SPEECH_S3_BUCKET",
                 "YANDEX_SPEECH_S3_ENDPOINT", "YANDEX_SPEECH_S3_PREFIX",
                 "YANDEX_SPEECH_AWS_ACCESS_KEY_ID", "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY"}
        self.environment = {name: supplied[name] for name in names if name in supplied}
        self.context = _context or RuntimeContext(
            user_home=Path(self.environment.get("HOME") or Path.home()),
            workspace=Path(self.environment["HERMES_HOME"]) if self.environment.get("HERMES_HOME") else None)
        explicit = {key: value for key, value in (("bucket", bucket), ("endpoint", endpoint), ("prefix", prefix))
                    if value is not None}
        settings = resolve_settings(_context=self.context, _adapter=_adapter_defaults,
                                    _environment=self.environment,
                                    _explicit={"object_storage": explicit} if explicit else {})
        storage = settings.value("object_storage")
        self.bucket, self.endpoint, self.prefix = _coordinates(
            storage.get("bucket"), storage.get("endpoint"), storage.get("prefix"))
        self._access = aws_access_key_id
        self._secret = aws_secret_access_key
        self._provided_client = _client
        home = self.context.workspace or self.context.user_home or Path.home()
        state = Path(self.environment.get("XDG_STATE_HOME", str(Path(home) / ".local/state")))
        if self.context.workspace is not None or not state.is_absolute():
            state = Path(home) / ".local/state"
        self.job_dir = Path(job_dir) if job_dir is not None else state / "yandex-speech/uploads"

    def _credentials(self):
        access = self._access if self._access is not None else self.environment.get("YANDEX_SPEECH_AWS_ACCESS_KEY_ID")
        secret = self._secret if self._secret is not None else self.environment.get("YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY")
        if not isinstance(access, str) or not access.strip() or not isinstance(secret, str) or not secret.strip():
            raise SpeechError("AUTH", "separate YANDEX_SPEECH_AWS_ACCESS_KEY_ID and YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY are required")
        return access, secret

    @contextlib.contextmanager
    def _connection(self, endpoint, deadline):
        if self._provided_client is not None:
            yield self._provided_client
            return
        access, secret = self._credentials()
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise SpeechError("PREREQUISITE", "boto3 is required only for helper-owned storage operations") from None
        timeout = _remaining(deadline)
        client = boto3.client("s3", endpoint_url=endpoint, region_name="ru-central1",
            aws_access_key_id=access, aws_secret_access_key=secret,
            config=Config(signature_version="s3v4", connect_timeout=min(10, timeout),
                          read_timeout=min(60, timeout), retries={"total_max_attempts": 1},
                          s3={"addressing_style": "path"}, request_checksum_calculation="when_required"))
        try:
            yield client
        finally:
            client.close()

    def upload(self, path, *, audio_input=None, audio_track=None, deadline_seconds=1800):
        deadline = _deadline(deadline_seconds)
        if self._provided_client is None:
            self._credentials()
        with prepare_audio(Path(path), audio_input=audio_input, audio_track=audio_track,
                           timeout=_remaining(deadline)) as prepared:
            if (prepared.duration_ms > _MAX_DURATION_MS or prepared.size_bytes > _MAX_OBJECT_BYTES):
                raise SpeechError("LIMIT", "prepared audio requires partitioning before a single storage upload")
            if prepared.channels not in (1, 2):
                raise SpeechError("UNSUPPORTED_AUDIO", "prepared storage audio must have one or two channels")
            sha, content_md5, size = _fingerprint(prepared.path, deadline)
            info = copy.deepcopy(prepared.source_info)
            if not info.get("converted") and info.get("source_sha256") != sha:
                raise SpeechError("STATE", "the addressed source changed during storage admission")
            directory = private_job_dir(self.job_dir)
            suffix = {"LPCM": "pcm", "WAV": "wav", "OGG_OPUS": "ogg", "MP3": "mp3"}[prepared.container]
            key = f"{self.prefix}/{directory.name}/audio.{suffix}"
            uri = _uri(self.endpoint, self.bucket, key)
            info.update({"metadata_origin": "uploader_receipt", "uri": uri,
                         "prepared_sha256": sha, "size_bytes": size})
            data = {"schema_version": 1, "kind": "yandex-speech-upload", "upload_id": directory.name,
                    "status": "prepared", "bucket": self.bucket, "endpoint": self.endpoint, "key": key,
                    "uri": uri, "source_info": info, "source_sha256": info.get("source_sha256"),
                    "uploaded_sha256": sha, "size_bytes": size, "version_id": None, "etag": None,
                    "retention": "retain"}
            receipt = UploadReceipt(directory / "upload.json", data)
            receipt.save()
            with _receipt_errors(receipt), receipt.lock(), self._connection(self.endpoint, deadline) as client:
                receipt.data["status"] = "uploading"
                receipt.save()
                _remaining(deadline)
                try:
                    with prepared.path.open("rb") as source:
                        response = client.put_object(Bucket=self.bucket, Key=key, Body=source,
                            ContentLength=size, ContentMD5=content_md5, IfNoneMatch="*",
                            Metadata={"speech-owner": receipt.upload_id, "speech-sha256": sha})
                except Exception as exc:
                    error = _error(exc, mutation=True)
                    receipt.data.update(status="upload_uncertain" if error.code == "UPLOAD_UNCERTAIN" else "failed",
                                        error_code=error.code)
                    receipt.save()
                    raise error from None
                if (not isinstance(response, dict)
                        or not 200 <= response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) < 300):
                    receipt.data.update(status="upload_uncertain", error_code="UPLOAD_UNCERTAIN")
                    receipt.save()
                    raise SpeechError("OUTPUT_PARTIAL", "upload returned no verified success receipt", code="UPLOAD_UNCERTAIN",
                                      receipt_path=str(receipt.manifest_path))
                receipt.data.update(status="uploaded", version_id=response.get("VersionId"), etag=response.get("ETag"))
                # Identity fields are completed by this accepted PUT, so persist their
                # first transition separately from later identity-preserving updates.
                atomic_write(receipt.manifest_path, json.dumps(receipt.data, ensure_ascii=False,
                             indent=2, allow_nan=False).encode(), overwrite=True)
                _remaining(deadline)
            return receipt

    @staticmethod
    def _verify_head(receipt, response):
        metadata = {str(key).lower(): value for key, value in response.get("Metadata", {}).items()}
        if (metadata.get("speech-owner") != receipt.upload_id
                or metadata.get("speech-sha256") != receipt.data["uploaded_sha256"]
                or response.get("ContentLength") != receipt.size_bytes):
            raise SpeechError("STATE", "remote object does not match the private upload ownership record")

    def recover_upload(self, manifest_path, *, deadline_seconds=120):
        receipt = UploadReceipt.load(manifest_path)
        deadline = _deadline(deadline_seconds)
        with receipt.lock():
            if receipt.status == "uploaded":
                return receipt
            if receipt.status not in {"uploading", "upload_uncertain"}:
                raise SpeechError("STATE", "this receipt is not an uncertain upload; no upload is repeated")
            with self._connection(receipt.data["endpoint"], deadline) as client:
                _remaining(deadline)
                try:
                    response = client.head_object(Bucket=receipt.bucket, Key=receipt.key)
                except Exception as exc:
                    raise _error(exc) from None
            self._verify_head(receipt, response)
            receipt.data.update(status="uploaded", version_id=response.get("VersionId"), etag=response.get("ETag"))
            atomic_write(receipt.manifest_path, json.dumps(receipt.data, ensure_ascii=False,
                         indent=2, allow_nan=False).encode(), overwrite=True)
            _remaining(deadline)
        return receipt

    def cleanup(self, upload, job, *, deadline_seconds=120):
        from .jobs import JobHandle

        receipt = UploadReceipt.load(upload.manifest_path if isinstance(upload, UploadReceipt) else upload)
        deadline = _deadline(deadline_seconds)
        with receipt.lock():
            verified = JobHandle.load(job.manifest_path if isinstance(job, JobHandle) else job)
            if (verified.status not in {"completed", "no_speech"} or verified.cached_result() is None
                    or receipt.reference() not in (verified.owned_uploads or [])
                    or verified.source_ref != receipt.uri):
                raise SpeechError("STATE", "cleanup requires the matching completed job and durable saved result")
            if receipt.status == "deleted":
                return {"status": "deleted", "upload_id": receipt.upload_id}
            if receipt.status not in {"uploaded", "deleting", "cleanup_uncertain"}:
                raise SpeechError("STATE", "upload is not eligible for owned cleanup")
            version = receipt.data.get("version_id")
            if not version or version == "null":
                raise SpeechError("PREREQUISITE", "safe cleanup requires an immutable uploaded object version; unversioned sources stay retained")
            with self._connection(receipt.data["endpoint"], deadline) as client:
                _remaining(deadline)
                try:
                    response = client.head_object(Bucket=receipt.bucket, Key=receipt.key, VersionId=version)
                    self._verify_head(receipt, response)
                    if response.get("VersionId") != version:
                        raise SpeechError("STATE", "storage did not identify the exact owned version")
                except SpeechError:
                    raise
                except Exception as exc:
                    error = _error(exc)
                    if (receipt.status in {"deleting", "cleanup_uncertain"}
                            and error.code in {"NoSuchKey", "NoSuchVersion", "404"}):
                        receipt.data["status"] = "deleted"
                        receipt.save()
                        return {"status": "deleted", "upload_id": receipt.upload_id}
                    raise error from None
                receipt.data["status"] = "deleting"
                receipt.save()
                _remaining(deadline)
                try:
                    deleted = client.delete_object(Bucket=receipt.bucket, Key=receipt.key, VersionId=version)
                    if not 200 <= deleted.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) < 300:
                        raise ValueError("no verified deletion receipt")
                except Exception:
                    receipt.data["status"] = "cleanup_uncertain"
                    receipt.save()
                    raise SpeechError("OUTPUT_PARTIAL", "owned version deletion outcome is uncertain; retain its receipt") from None
            receipt.data["status"] = "deleted"
            receipt.save()
            _remaining(deadline)
        return {"status": "deleted", "upload_id": receipt.upload_id}
