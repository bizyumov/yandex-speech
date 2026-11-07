"""Transport-neutral admission for prepared recordings and private source URIs."""
from __future__ import annotations

import hashlib
import math
from urllib.parse import urlparse

from .errors import SpeechError

MAX_INLINE_BYTES = 60_000_000
MAX_FILE_DURATION_MS = 14_400_000
MAX_STORAGE_BYTES = 1_000_000_000


def inline_size_error(size, limit, *, representation="prepared audio", audio_limit=None, prepared_size=None):
    detail = f"{representation} is {size} bytes; inline request limit is {limit} bytes ({limit / 1_000_000:g} MB). "
    if audio_limit is not None:
        detail += f"The effective audio-content cap is {audio_limit} bytes after transport encoding/envelope. "
    if prepared_size is not None:
        detail += f"Prepared audio is {prepared_size} bytes. "
    return SpeechError("LIMIT", detail +
        "Choose existing authorized Yandex Object Storage (S3-compatible storage): "
        "python scripts/transcribe.py INPUT --mode async --api-version 3 --transport rest "
        "--bucket EXISTING_BUCKET --output OUTPUT; or explicitly recognize every inline part: "
        "python scripts/transcribe.py INPUT --mode async --api-version 3 --transport rest "
        "--split-inline --output OUTPUT. Configured storage defaults remain usable; "
        "this diagnostic starts no paid request or infrastructure change.")

def file_source(audio=None, *, uri=None, source_info=None, inline_limit=MAX_INLINE_BYTES,
                duration_limit=MAX_FILE_DURATION_MS, storage_limit=MAX_STORAGE_BYTES, mono_required=False,
                inline_wire_limit=None):
    if uri is None:
        if audio is None:
            raise SpeechError("CONFIG", "prepared audio is required")
        duration, size, container, rate, channels = (
            audio.duration_ms, audio.size_bytes, audio.container, audio.sample_rate_hertz, audio.channels)
        if type(size) is not int or size < 0 or size > inline_limit:
            raise inline_size_error(size, inline_wire_limit if inline_wire_limit is not None else inline_limit,
                                    audio_limit=inline_limit)
    else:
        try:
            parsed = urlparse(str(uri))
            port = parsed.port
        except ValueError:
            raise SpeechError("CONFIG", "invalid source object URI") from None
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query
                or parsed.fragment or port not in (None, 443)
                or not (host == "storage.yandexcloud.net" or host.endswith(".storage.yandexcloud.net"))):
            raise SpeechError("CONFIG", "use a supported non-secret Yandex Object Storage URI")
        info = source_info or {}
        if not isinstance(info, dict) or not isinstance(info.get("prepared"), dict):
            raise SpeechError("CONFIG", "source object requires prepared-audio metadata")
        prepared = info["prepared"]
        duration, size = info.get("duration_ms"), info.get("size_bytes")
        container, rate, channels = prepared.get("container"), prepared.get("sample_rate_hertz"), prepared.get("channels")
        if type(size) is not int or not 0 <= size <= storage_limit:
            raise SpeechError("LIMIT", "source object size is missing or exceeds the storage budget")
    if (type(duration) not in (int, float) or not math.isfinite(duration)
            or not 0 <= duration <= duration_limit):
        raise SpeechError("LIMIT", "prepared recording must fit the four-hour single-operation budget")
    if (container not in {"LPCM", "WAV", "OGG_OPUS", "MP3"} or type(rate) is not int or rate <= 0
            or container in {"LPCM", "WAV"} and rate not in {8000, 16000, 48000}):
        raise SpeechError("CONFIG", "source metadata does not describe a supported prepared representation")
    if type(channels) is not int or channels not in {1, 2}:
        raise SpeechError("CONFIG", "source metadata must describe one or two audio channels")
    if mono_required and channels != 1:
        raise SpeechError("CONFIG", "native speaker labeling requires mono prepared audio; "
                          "prepare local audio or upload with diarize=True")
    metadata = {"duration_ms": duration, "size_bytes": size, "container": container,
                "sample_rate_hertz": rate, "channels": channels}
    if uri is not None:
        return {"uri": str(uri)}, metadata
    with audio.path.open("rb") as stream:
        content = stream.read(inline_limit + 1)
    if len(content) > inline_limit:
        raise SpeechError("LIMIT", "prepared audio grew beyond the inline request budget")
    if (not audio.source_info.get("converted") and audio.source_info.get("source_sha256")
            and hashlib.sha256(content).hexdigest() != audio.source_info["source_sha256"]):
        raise SpeechError("STATE", "the addressed source changed during admission")
    return {"content": content}, metadata
