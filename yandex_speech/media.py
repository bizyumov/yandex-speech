"""Automatic source-media preparation; provider formats are not source restrictions."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import SpeechError

RAW_WIDTHS = {"u8": 1, "s8": 1, "s16le": 2, "s16be": 2, "s24le": 3,
              "s24be": 3, "s32le": 4, "s32be": 4, "f32le": 4,
              "f32be": 4, "f64le": 8, "f64be": 8}


@dataclass(frozen=True)
class AudioInputSpec:
    source_format: str = "auto"
    sample_format: str | None = None
    sample_rate_hertz: int | None = None
    channels: int | None = None

    def raw_values(self) -> tuple[str, int, int, int]:
        fmt = self.sample_format or "s16le"
        if self.source_format != "lpcm" or fmt not in RAW_WIDTHS:
            raise SpeechError("CONFIG", "unsupported raw audio descriptor")
        if type(self.sample_rate_hertz) is not int or not 1 <= self.sample_rate_hertz <= 384000:
            raise SpeechError("CONFIG", "raw audio requires its actual sample rate")
        if type(self.channels) is not int or not 1 <= self.channels <= 64:
            raise SpeechError("CONFIG", "raw audio requires its actual channel count")
        return fmt, self.sample_rate_hertz, self.channels, RAW_WIDTHS[fmt]


@dataclass
class PreparedAudio:
    path: Path
    container: str
    sample_rate_hertz: int
    channels: int
    duration_ms: float
    source_info: dict

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SpeechError("PREREQUISITE", f"{name} is required for this media; use the host package procedure")
    return path


def _run(command: list[str], *, timeout: float = 120) -> bytes:
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise SpeechError("TIMEOUT", "media preparation exceeded its deadline") from None
    except OSError:
        raise SpeechError("PREREQUISITE", "cannot start the required media tool") from None
    if result.returncode != 0:
        raise SpeechError("UNSUPPORTED_AUDIO", "media is corrupt, protected or unavailable to the installed decoder")
    return result.stdout


def _digest(path: Path, *, deadline=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if deadline is not None and time.monotonic() >= deadline:
                raise SpeechError("TIMEOUT", "audio preparation deadline expired")
            digest.update(block)
    return digest.hexdigest()


def _probe(path: Path, track: int | None, *, timeout=120) -> tuple[dict, float]:
    if track is not None and (type(track) is not int or track < 0):
        raise SpeechError("CONFIG", "audio track must be a nonnegative audio-stream index")
    raw = _run([_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(path)], timeout=timeout)
    try:
        data = json.loads(raw)
        streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        index = 0 if track is None else track
        stream = streams[index]
        container = data.get("format", {})
        duration = stream.get("duration") or container.get("duration")
        seconds = float(duration) if duration not in (None, "N/A") else None
        start = stream.get("start_time")
        origin = container.get("start_time")
        start = float(start) if start not in (None, "N/A", "") else 0.0
        origin = float(origin) if origin not in (None, "N/A", "") else 0.0
        if (seconds is None or not math.isfinite(seconds) or seconds < 0
                or not math.isfinite(start) or not math.isfinite(origin)):
            raise ValueError
        return {
            "format": container.get("format_name", ""), "codec": stream.get("codec_name", ""),
            "sample_rate_hertz": int(stream["sample_rate"]),
            "channels": int(stream["channels"]), "sample_format": stream.get("sample_fmt"),
            "track": index, "start_time_seconds": start, "container_start_time_seconds": origin,
            "duration_ms": seconds * 1000,
        }, seconds * 1000
    except (ValueError, KeyError, IndexError, TypeError):
        raise SpeechError("UNSUPPORTED_AUDIO", "no usable audio track, duration or timestamp origin in supplied media") from None


@contextlib.contextmanager
def prepare_audio(source: Path, *, audio_input: AudioInputSpec | None = None,
                  audio_track: int | None = None, timeout: float = 120):
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout <= 0:
        raise SpeechError("CONFIG", "audio preparation timeout must be positive and finite")
    deadline=time.monotonic()+timeout
    def remaining():
        left=deadline-time.monotonic()
        if left <= 0:
            raise SpeechError("TIMEOUT", "audio preparation deadline expired")
        return left
    path = Path(source).expanduser().absolute()
    if not path.is_file():
        raise SpeechError("UNSUPPORTED_AUDIO", "the addressed audio source is not a readable file")
    spec = audio_input or AudioInputSpec()
    if spec.source_format not in {"auto", "lpcm"}:
        raise SpeechError("CONFIG", "source format must be auto or lpcm")
    raw_args = []
    if spec.source_format == "lpcm":
        fmt, rate, channels, width = spec.raw_values()
        size = path.stat().st_size
        if size % (width * channels):
            raise SpeechError("UNSUPPORTED_AUDIO", "raw source ends inside a sample frame")
        original = {"format": "lpcm", "codec": fmt, "sample_format": fmt,
                    "sample_rate_hertz": rate, "channels": channels, "track": 0,
                    "start_time_seconds": 0.0, "container_start_time_seconds": 0.0}
        duration_ms = size / (width * channels * rate) * 1000
        raw_args = ["-f", fmt, "-ar", str(rate), "-ac", str(channels)]
    else:
        original, duration_ms = _probe(path, audio_track, timeout=remaining())
        rate, channels = original["sample_rate_hertz"], original["channels"]
    original["duration_ms"] = duration_ms
    info = {"original_path": str(path), "source_sha256": _digest(path, deadline=deadline),
            "duration_ms": duration_ms, "original_size_bytes": path.stat().st_size,
            "original": original, "metadata_origin": "local_probe",
            "timeline_offset_seconds": max(0.0, original["start_time_seconds"])}
    container = None
    if channels == 1 and original["track"] == 0 and original["start_time_seconds"] == 0:
        if spec.source_format == "lpcm" and original["codec"] == "s16le" and rate in {8000,16000,48000}:
            container = "LPCM"
        elif original["codec"] == "pcm_s16le" and "wav" in original["format"].split(",") and rate in {8000,16000,48000}:
            container = "WAV"
        elif original["codec"] == "opus" and "ogg" in original["format"].split(","):
            container = "OGG_OPUS"
        elif original["codec"] == "mp3" and "mp3" in original["format"].split(","):
            container = "MP3"
    remaining()
    if container:
        info.update({"converted": False, "size_bytes": path.stat().st_size,
                     "prepared": {"container": container, "sample_rate_hertz": rate, "channels": 1}})
        yield PreparedAudio(path, container, rate, 1, duration_ms, info)
        return
    with tempfile.TemporaryDirectory(prefix="yandex-speech-audio-") as directory:
        prepared = Path(directory) / "prepared.wav"
        _run([_tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-xerror", "-y",
              *raw_args, "-i", str(path), "-map", f"0:a:{original['track']}", "-vn",
              "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(prepared)], timeout=remaining())
        converted, actual_ms = _probe(prepared, 0, timeout=remaining())
        remaining()
        info.update({"converted": True, "duration_ms": actual_ms,
                     "size_bytes": prepared.stat().st_size,
                     "prepared": {"container": "WAV", "sample_rate_hertz": 16000, "channels": 1}})
        yield PreparedAudio(prepared, "WAV", 16000, 1, actual_ms, info)
