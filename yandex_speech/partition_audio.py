"""Sample-exact, cancellable preparation under the actual async wire budgets."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import wave

from .errors import SpeechError
from .media import AudioInputSpec, PreparedAudio, _probe, _tool
from . import grpc_stt, recording_request, rest_stt

_RATE = 16000
_MAX_PARTS = 10000
_PREPARATION = threading.BoundedSemaphore(1)


@dataclass(frozen=True)
class FrameWindow:
    index: int
    start: int
    end: int


def frame_windows(total_frames, capacity_frames, sample_rate):
    if (any(type(value) is not int for value in (total_frames, capacity_frames, sample_rate))
            or not 0 <= total_frames <= 2**63 - 1
            or not 1 <= capacity_frames <= 2**63 - 1 or not 1 <= sample_rate <= 384000):
        raise SpeechError("CONFIG", "partition geometry needs nonnegative frames and positive capacity/rate")
    if total_frames == 0:
        return []
    overlap = sample_rate * 5 if capacity_frames > sample_rate * 5 else capacity_frames // 2
    stride = capacity_frames - overlap
    count = 1 + max(0, (total_frames - capacity_frames + stride - 1) // stride)
    if count > _MAX_PARTS:
        raise SpeechError("LIMIT", "the selected inline budget would require too many recording parts")
    return [FrameWindow(index, index * stride, min(index * stride + capacity_frames, total_frames))
            for index in range(count)]


def _check(deadline, cancelled):
    if cancelled.is_set():
        raise SpeechError("CANCELLED", "recording partition preparation cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SpeechError("TIMEOUT", "recording partition preparation exceeded its deadline")
    return remaining


def _hash_file(path, deadline, cancelled):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            while True:
                _check(deadline, cancelled)
                block = stream.read(1048576)
                if not block:
                    break
                digest.update(block)
    except OSError:
        raise SpeechError("UNSUPPORTED_AUDIO", "the addressed partition source is unavailable") from None
    return digest.hexdigest()


def _varint_size(value):
    return max(1, (value.bit_length() + 6) // 7)


def inline_frame_capacity(transports, directory, *, language="ru-RU", diarize=False,
                          literature_text=True, channels=1):
    if (not isinstance(transports, (list, tuple)) or not transports
            or any(transport not in {"grpc", "rest"} for transport in transports)
            or type(channels) is not int or channels not in {1, 2}
            or type(diarize) is not bool or type(literature_text) is not bool
            or not isinstance(language, str) or not language or diarize and channels != 1):
        raise SpeechError("CONFIG", "invalid partition transport, channels or request policy")
    capacities = []
    with tempfile.TemporaryDirectory(prefix="wire-budget-", dir=directory) as temporary:
        path = Path(temporary) / "prototype.wav"
        with path.open("xb") as stream:
            with wave.open(stream, "wb") as audio:
                audio.setparams((channels, 2, _RATE, 0, "NONE", "not compressed"))
                audio.writeframes(bytes(channels * 2))
        header = path.stat().st_size - channels * 2
        prepared = PreparedAudio(path, "WAV", _RATE, channels, 1000 / _RATE, {"converted": True})
        options = {"language": language, "diarize": diarize, "literature_text": literature_text}
        for transport in dict.fromkeys(transports):
            if transport == "rest":
                body = json.loads(rest_stt.build_rest_request(prepared, **options))
                body["content"] = ""
                capacity = 3 * ((rest_stt.MAX_REST_BYTES - len(rest_stt._encode(body))) // 4)
            else:
                request = grpc_stt.build_file_request(prepared, **options)
                field = request.DESCRIPTOR.fields_by_name["content"].number
                request.ClearField("content")
                available = grpc_stt.MAX_GRPC_BYTES - request.ByteSize() - _varint_size(field * 8 + 2)
                lower, upper = 0, max(0, available)
                while lower < upper:
                    candidate = (lower + upper + 1) // 2
                    if candidate + _varint_size(candidate) <= available:
                        lower = candidate
                    else:
                        upper = candidate - 1
                capacity = lower
            capacities.append((capacity - header) // (channels * 2))
    maximum = min(*capacities, int(recording_request.MAX_FILE_DURATION_MS * _RATE / 1000))
    if maximum < 1:
        raise SpeechError("LIMIT", "request metadata and WAV header leave no valid inline audio frame")
    return maximum


def _source_description(path, spec, track, deadline, cancelled):
    if track is not None and (type(track) is not int or track < 0):
        raise SpeechError("CONFIG", "audio track must be a nonnegative audio-stream index")
    if not isinstance(spec, AudioInputSpec):
        raise SpeechError("CONFIG", "partition audio_input must be an AudioInputSpec")
    if not path.is_file():
        raise SpeechError("UNSUPPORTED_AUDIO", "the addressed recording is not a readable file")
    if spec.source_format == "lpcm":
        if track not in (None, 0):
            raise SpeechError("CONFIG", "a raw source contains only audio track zero")
        fmt, rate, channels, width = spec.raw_values()
        if path.stat().st_size % (width * channels):
            raise SpeechError("UNSUPPORTED_AUDIO", "raw recording ends inside a declared sample frame")
        original = {"format": "lpcm", "codec": fmt, "sample_format": fmt,
                    "sample_rate_hertz": rate, "channels": channels, "track": 0,
                    "audio_stream_count": 1, "start_time_seconds": 0.0,
                    "container_start_time_seconds": 0.0,
                    "duration_ms": path.stat().st_size / (width * channels * rate) * 1000}
        arguments = ["-f", fmt, "-ar", str(rate), "-ac", str(channels)]
    elif spec.source_format == "auto":
        original, _ = _probe(path, track, timeout=_check(deadline, cancelled))
        arguments = []
    else:
        raise SpeechError("CONFIG", "partition source format must be auto or lpcm")
    return original, arguments


def _stop_decoder(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        raise SpeechError("STATE", "owned partition decoder did not stop") from None


def _decode(path, output, *, raw_arguments, track, channels, deadline, cancelled):
    process = None
    try:
        with output.open("xb") as target:
            os.chmod(output, 0o600)
            process = subprocess.Popen(
                [_tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-xerror",
                 *raw_arguments, "-i", str(path), "-map", f"0:a:{track}", "-vn",
                 "-ac", str(channels), "-ar", str(_RATE), "-c:a", "pcm_s16le",
                 "-f", "s16le", "pipe:1"],
                stdin=subprocess.DEVNULL, stdout=target, stderr=subprocess.DEVNULL,
                start_new_session=True)
            while True:
                remaining = _check(deadline, cancelled)
                try:
                    code = process.wait(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if code != 0:
                raise SpeechError("UNSUPPORTED_AUDIO", "recording is corrupt or unavailable to the decoder")
            target.flush()
            os.fsync(target.fileno())
    except OSError:
        raise SpeechError("PREREQUISITE", "cannot start or store the required partition decoder") from None
    finally:
        if process is not None:
            _stop_decoder(process)


def _write_part(decoded, path, window, channels, deadline, cancelled):
    width = channels * 2
    decoded.seek(window.start * width)
    remaining = window.end - window.start
    with path.open("xb") as output:
        os.chmod(path, 0o600)
        with wave.open(output, "wb") as audio:
            audio.setparams((channels, 2, _RATE, 0, "NONE", "not compressed"))
            while remaining:
                _check(deadline, cancelled)
                count = min(remaining, 32768)
                data = decoded.read(count * width)
                if len(data) != count * width:
                    raise SpeechError("STATE", "decoded partition source changed or truncated")
                audio.writeframesraw(data)
                remaining -= count
        output.flush()
        os.fsync(output.fileno())


def prepare_parts(source, directory, *, transports, language="ru-RU", diarize=False,
                  literature_text=True, audio_input=None, audio_track=None,
                  deadline=None, cancel_event=None):
    cancelled = cancel_event if cancel_event is not None else threading.Event()
    deadline = time.monotonic() + 1800 if deadline is None else deadline
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise SpeechError("CONFIG", "partition deadline must be finite")
    _check(deadline, cancelled)
    if type(diarize) is not bool or type(literature_text) is not bool:
        raise SpeechError("CONFIG", "partition behavior options must be booleans")
    if not _PREPARATION.acquire(blocking=False):
        raise SpeechError("RATE_LIMIT", "one local recording partition preparation is already active")
    try:
        path, directory = Path(source).expanduser().absolute(), Path(directory).absolute()
        if not directory.is_dir():
            raise SpeechError("CONFIG", "partition preparation needs its existing private job directory")
        spec = audio_input if audio_input is not None else AudioInputSpec()
        original, arguments = _source_description(path, spec, audio_track, deadline, cancelled)
        source_hash = _hash_file(path, deadline, cancelled)
        channels = 1 if diarize or original["channels"] > 2 else original["channels"]
        capacity = inline_frame_capacity(
            transports, directory, language=language, diarize=diarize,
            literature_text=literature_text, channels=channels)
        audio_directory = directory / "audio"
        try:
            audio_directory.mkdir(mode=0o700)
        except FileExistsError:
            raise SpeechError("CONFIG", "existing partition preparation is preserved; use its receipt") from None
        raw = audio_directory / "decoded.pcm"
        _decode(path, raw, raw_arguments=arguments, track=original["track"], channels=channels,
                deadline=deadline, cancelled=cancelled)
        if raw.stat().st_size % (channels * 2):
            raise SpeechError("UNSUPPORTED_AUDIO", "decoder returned an incomplete PCM sample frame")
        total = raw.stat().st_size // (channels * 2)
        windows = frame_windows(total, capacity, _RATE)
        needed = sum((item.end - item.start) * channels * 2 + 128 for item in windows)
        if shutil.disk_usage(audio_directory).free < needed:
            raise SpeechError("LIMIT", "insufficient private storage for all required recording parts")
        parts = []
        options = {"language": language, "diarize": diarize, "literature_text": literature_text}
        with raw.open("rb") as decoded:
            for window in windows:
                target = audio_directory / f"part-{window.index:05d}.wav"
                _write_part(decoded, target, window, channels, deadline, cancelled)
                observed, duration = _probe(target, 0, timeout=_check(deadline, cancelled))
                if (observed["sample_rate_hertz"] != _RATE or observed["channels"] != channels
                        or abs(duration - (window.end - window.start) / _RATE * 1000) > 1):
                    raise SpeechError("STATE", "prepared partition metadata differs from complete PCM coverage")
                prepared = PreparedAudio(target, "WAV", _RATE, channels, duration, {"converted": True})
                for transport in dict.fromkeys(transports):
                    _check(deadline, cancelled)
                    builder = grpc_stt.build_file_request if transport == "grpc" else rest_stt.build_rest_request
                    builder(prepared, **options)
                parts.append({"part_id": str(window.index), "start_frame": window.start,
                              "end_frame": window.end, "offset": window.start / _RATE,
                              "duration_ms": (window.end - window.start) / _RATE * 1000,
                              "audio_path": str(target), "size_bytes": target.stat().st_size,
                              "audio_sha256": _hash_file(target, deadline, cancelled),
                              "status": "prepared", "child_manifest_path": None, "operation_id": None})
        if _hash_file(path, deadline, cancelled) != source_hash:
            raise SpeechError("STATE", "the addressed recording changed during partition preparation")
        info = {"original_path": str(path), "source_sha256": source_hash,
                "original_size_bytes": path.stat().st_size, "original": original,
                "duration_ms": total / _RATE * 1000, "decoded_frames": total,
                "partition_capacity_frames": capacity,
                "inline_wire_limits": {transport: grpc_stt.MAX_GRPC_BYTES if transport == "grpc"
                                      else rest_stt.MAX_REST_BYTES for transport in dict.fromkeys(transports)},
                "decoded_pcm_sha256": _hash_file(raw, deadline, cancelled),
                "timeline_offset_seconds": max(0.0, original["start_time_seconds"]),
                "converted": True, "prepared": {"container": "WAV",
                    "sample_rate_hertz": _RATE, "channels": channels}}
        descriptor = os.open(audio_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        raw.unlink()
        return info, parts
    finally:
        _PREPARATION.release()
