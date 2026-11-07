#!/usr/bin/env python3
"""Yandex SpeechKit STT helper."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yandex_speech.transcription import SpeechTranscriber
from yandex_speech.media import prepare_audio
from yandex_speech.errors import SpeechError

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None

SYNC_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
ASYNC_URL = "https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize"
OPERATIONS_URL = "https://operation.api.cloud.yandex.net/operations"
DEFAULT_LANG = "ru-RU"


class SttError(RuntimeError):
    pass


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SttError(f"{name} is required")
    return value


class _LegacySpeechTranscriber:
    def __init__(self, api_key: str | None = None, folder_id: str | None = None) -> None:
        self.api_key = api_key or require_env("YANDEX_SPEECH_API_KEY")
        self.folder_id = folder_id or os.getenv("YANDEX_CLOUD_FOLDER_ID")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Api-Key {self.api_key}"})

    def _remaining(self):
        deadline=getattr(self,"deadline_at",time.monotonic()+120)
        remaining=deadline-time.monotonic()
        if remaining <= 0:
            raise SpeechError("TIMEOUT","legacy recognition deadline expired")
        return min(120,remaining)

    def transcribe_direct(self, path: Path, *, language_code: str, diarize: bool) -> dict[str, Any]:
        import wave
        if diarize:
            raise SttError("v1 direct recognition does not provide speaker labels")
        with prepare_audio(Path(path),audio_input=getattr(self,"audio_input",None),
                           audio_track=getattr(self,"audio_track",None),timeout=self._remaining()) as audio:
            if audio.duration_ms > 30_000:
                raise SttError("audio duration should be less than 30s")
            if audio.container == "OGG_OPUS" and audio.channels == 1:
                content=audio.path.read_bytes()
                params={"format":"oggopus"}
            elif audio.container == "LPCM" and audio.channels == 1 and audio.sample_rate_hertz == 16000:
                content = audio.path.read_bytes()
                params = {"format": "lpcm", "sampleRateHertz": "16000"}
            elif audio.container == "WAV" and audio.channels == 1:
                with wave.open(str(audio.path),"rb") as stream:
                    content=stream.readframes(stream.getnframes())
                params={"format":"lpcm","sampleRateHertz":str(audio.sample_rate_hertz)}
            else:
                raw_args=(["-f","s16le","-ar",str(audio.sample_rate_hertz),
                           "-ac",str(audio.channels)] if audio.container == "LPCM" else [])
                result=subprocess.run(["ffmpeg","-nostdin","-v","error",*raw_args,"-i",str(audio.path),
                    "-vn","-ac","1","-ar","16000","-f","s16le","pipe:1"],
                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=self._remaining())
                if result.returncode:
                    raise SttError("ffmpeg failed to prepare v1 audio")
                content=result.stdout
                params={"format":"lpcm","sampleRateHertz":"16000"}
            transmitted = "OGG_OPUS" if params["format"] == "oggopus" else "LPCM"
            rate = int(params.get("sampleRateHertz", audio.sample_rate_hertz))
            info = audio.source_info
            info.update({"converted": info["converted"] or audio.container != transmitted
                         or audio.channels != 1 or audio.sample_rate_hertz != rate,
                         "size_bytes": len(content),
                         "prepared": {"container": transmitted,
                                      "sample_rate_hertz": rate, "channels": 1}})
            if transmitted == "LPCM":
                info["duration_ms"] = len(content) / (2 * rate) * 1000
            if len(content)>=1_000_000:
                raise SttError("audio should be less than 1 mb")
            response=self.session.post(SYNC_URL,
                params={"lang":language_code,"topic":"general","profanityFilter":"false","rawResults":"true",**params},
                data=content,timeout=self._remaining())
            if response.status_code>=400:
                category={401:"AUTH",403:"PERMISSION",429:"RATE_LIMIT"}.get(response.status_code,"PROVIDER")
                remedy="; verify service-account roles and API-key execution scope; denial alone does not identify the missing grant" if response.status_code == 403 else ""
                raise SpeechError(category,f"Direct STT failed: HTTP {response.status_code}{remedy}",code=str(response.status_code))
            payload=response.json()
            text=payload.get("result","")
            return {"text":text,
                    "segments":[{"start":None,"end":None,"speaker":None,"text":text}] if text else [],
                    "raw":payload,"mode":"direct","diarization_requested":False,
                    "source_info":audio.source_info}

    def transcribe_auto(self, path: Path, *, language_code: str, diarize: bool) -> dict[str, Any]:
        try:
            return self.transcribe_direct(path, language_code=language_code, diarize=diarize)
        except SttError as exc:
            message = str(exc)
            if "audio should be less than 1 mb" not in message and "audio duration should be less than 30s" not in message:
                raise
            return self.transcribe_chunked_direct(path, language_code=language_code, diarize=diarize)

    def transcribe_chunked_direct(self, path: Path, *, language_code: str, diarize: bool) -> dict[str, Any]:
        import os
        from yandex_speech.media import AudioInputSpec
        from yandex_speech.overlap import assemble_text, pcm_windows

        if diarize:
            raise SttError("v1 split recognition does not provide speaker labels")
        if not has_ffmpeg():
            raise SpeechError("PREREQUISITE", "ffmpeg is required for split recognition")
        with prepare_audio(Path(path), audio_input=getattr(self, "audio_input", None),
                           audio_track=getattr(self, "audio_track", None),
                           timeout=self._remaining()) as prepared:
            with tempfile.TemporaryDirectory(prefix="yandex-stt-chunks-") as directory:
                decoded = Path(directory) / "source.pcm"
                raw_args = (["-f", "s16le", "-ar", str(prepared.sample_rate_hertz),
                             "-ac", str(prepared.channels)] if prepared.container == "LPCM" else [])
                descriptor = os.open(decoded, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    try:
                        process = subprocess.run(
                            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", *raw_args,
                             "-i", str(prepared.path), "-map", "0:a:0", "-vn",
                             "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                            stdout=output, stderr=subprocess.PIPE, timeout=self._remaining())
                    except subprocess.TimeoutExpired:
                        raise SpeechError("TIMEOUT", "split audio preparation exceeded its deadline") from None
                if process.returncode or decoded.stat().st_size % 2:
                    raise SpeechError("UNSUPPORTED_AUDIO", "split source could not be decoded completely")
                hypotheses, raw, windows = [], [], []
                with decoded.open("rb") as source:
                    for window in pcm_windows(decoded.stat().st_size // 2):
                        self._remaining()
                        source.seek(window.start_sample * 2)
                        content = source.read(window.byte_length)
                        if len(content) != window.byte_length:
                            raise SpeechError("STATE", "prepared split source changed during extraction")
                        chunk = Path(directory) / f"window_{window.index:06d}.pcm"
                        descriptor = os.open(chunk, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(descriptor, "wb") as output:
                            output.write(content)
                        child = _LegacySpeechTranscriber(self.api_key, self.folder_id)
                        child.session = self.session
                        child.audio_input = AudioInputSpec("lpcm", "s16le", 16000, 1)
                        if hasattr(self, "deadline_at"):
                            child.deadline_at = self.deadline_at
                        data = child.transcribe_direct(chunk, language_code=language_code, diarize=False)
                        hypotheses.append(data.get("text", ""))
                        raw.append(data.get("raw"))
                        windows.append(window.metadata(prepared.source_info.get("timeline_offset_seconds", 0)))
                assembled = assemble_text(hypotheses)
                text = assembled["text"]
                info = dict(prepared.source_info)
                info.update({"converted": True, "duration_ms": decoded.stat().st_size / 32,
                             "size_bytes": decoded.stat().st_size,
                             "prepared": {"container": "LPCM", "sample_rate_hertz": 16000, "channels": 1}})
                return {"text": text,
                        "segments": [{"start": None, "end": None, "speaker": None, "text": text}] if text else [],
                        "raw": {"chunks": raw, "windows": windows,
                                "hypotheses": assembled["raw_hypotheses"]},
                        "uncertainties": assembled["uncertainties"], "mode": "direct-chunked",
                        "diarization_requested": False, "source_info": info}

    def transcribe_async_s3(self, path: Path, *, language_code: str, diarize: bool, poll_interval: float = 5.0) -> dict[str, Any]:
        uri = upload_to_object_storage(path, _storage=getattr(self, "storage", None))
        request = {
            "config": {
                "specification": {
                    "languageCode": language_code,
                    "audioEncoding": detect_audio_encoding(path),
                    "sampleRateHertz": probe_sample_rate(path),
                    "rawResults": True,
                    "speakerLabeling": diarize,
                }
            },
            "audio": {"uri": uri},
        }
        response = self.session.post(ASYNC_URL, json=request, timeout=120)
        response.raise_for_status()
        operation = response.json()
        operation_id = operation["id"]
        deadline = time.monotonic() + 1800
        if type(poll_interval) not in (int,float) or not 0 < poll_interval <= 120:
            raise SttError("poll interval must be in (0, 120] seconds")
        while time.monotonic() < deadline:
            current = self.session.get(f"{OPERATIONS_URL}/{operation_id}", timeout=120)
            current.raise_for_status()
            payload = current.json()
            if payload.get("done"):
                if "error" in payload:
                    raise SttError("legacy accepted operation failed; retain its operation ID for recovery")
                return normalize_async_result(payload, uri=uri, operation_id=operation_id)
            print(f"waiting for operation {operation_id}", file=sys.stderr)
            time.sleep(poll_interval)
        raise SttError("legacy wait expired; retain the accepted operation ID")


def prepare_sync_audio(path: Path) -> tuple[Path, Path | None, dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix in {".ogg", ".opus"}:
        return path, None, {"format": "oggopus"}
    if suffix in {".wav", ".pcm"}:
        return path, None, {}
    if not has_ffmpeg():
        raise SttError(
            "Direct STT only works reliably with OGG/Opus or WAV input; ffmpeg is required to transcode this file automatically"
        )
    converted = path.with_suffix(".direct-stt.ogg")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-c:a", "libopus", str(converted)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0 or not converted.exists():
        raise SttError(f"ffmpeg transcode for direct STT failed: {result.stderr[-500:]}")
    return converted, converted, {"format": "oggopus"}


def upload_to_object_storage(path: Path, *, _storage=None) -> str:
    import uuid
    from yandex_speech.config import resolve_settings
    if boto3 is None:
        raise SttError("boto3 is required for explicit legacy async-s3 mode")
    storage=_storage if _storage is not None else resolve_settings().value("object_storage")
    bucket=storage.get("bucket")
    if not bucket:
        raise SttError("configure an existing authorized object-storage bucket")
    access_key=require_env("YANDEX_SPEECH_AWS_ACCESS_KEY_ID")
    secret_key=require_env("YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY")
    prefix=storage.get("prefix","speechkit")
    endpoint=storage.get("endpoint","https://storage.yandexcloud.net")
    key=f"{prefix.rstrip('/')}/{uuid.uuid4().hex}/{path.name}"
    client=boto3.client("s3",endpoint_url=endpoint,
        aws_access_key_id=access_key,aws_secret_access_key=secret_key)
    client.upload_file(str(path),bucket,key)
    return f"{endpoint.rstrip('/')}/{bucket}/{key}"


def detect_audio_encoding(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "MP3"
    if suffix in {".ogg", ".opus"}:
        return "OGG_OPUS"
    if suffix in {".wav", ".pcm"}:
        return "LINEAR16_PCM"
    return "MP3"


def has_ffmpeg() -> bool:
    return subprocess.run(["bash", "-lc", "command -v ffmpeg >/dev/null 2>&1"], check=False).returncode == 0


def probe_sample_rate(path: Path) -> int:
    if subprocess.run(["bash", "-lc", "command -v ffprobe >/dev/null 2>&1"], check=False).returncode != 0:
        return 48000
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 48000


def probe_duration(path: Path) -> float:
    if subprocess.run(["bash", "-lc", "command -v ffprobe >/dev/null 2>&1"], check=False).returncode != 0:
        return 0.0
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def normalize_async_result(payload: dict[str, Any], *, uri: str, operation_id: str) -> dict[str, Any]:
    response = payload.get("response", {})
    chunks = response.get("chunks", [])
    segments: list[dict[str, Any]] = []
    full_text: list[str] = []
    for chunk in chunks:
        alternatives = chunk.get("alternatives") or []
        if not alternatives:
            continue
        best = alternatives[0]
        text = best.get("text", "").strip()
        if not text:
            continue
        words = best.get("words") or []
        start = parse_seconds(words[0].get("startTime")) if words else 0.0
        end = parse_seconds(words[-1].get("endTime")) if words else start
        speaker = chunk.get("speakerTag")
        segments.append(
            {
                "start": start,
                "end": end,
                "speaker": f"Speaker {speaker}" if speaker is not None else None,
                "speaker_tag": speaker,
                "text": text,
                "words": words,
            }
        )
        full_text.append(text)
    return {
        "text": "\n".join(full_text),
        "segments": segments,
        "raw": payload,
        "mode": "async-s3",
        "operation_id": operation_id,
        "audio_uri": uri,
    }


def parse_seconds(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return 0.0


def format_timestamp(seconds: float, *, srt: bool = False) -> str:
    millis = int(round(seconds * 1000))
    hours = millis // 3_600_000
    millis %= 3_600_000
    minutes = millis // 60_000
    millis %= 60_000
    secs = millis // 1000
    millis %= 1000
    if srt:
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def render_txt(data: dict[str, Any]) -> str:
    lines = []
    for segment in data.get("segments", []):
        speaker = f"{segment['speaker']}: " if segment.get("speaker") else ""
        lines.append(f"[{format_timestamp(segment.get('start', 0.0))}] {speaker}{segment.get('text', '')}")
    return "\n".join(lines) + ("\n" if lines else "")


def render_srt(data: dict[str, Any]) -> str:
    blocks = []
    for idx, segment in enumerate(data.get("segments", []), start=1):
        speaker = f"{segment['speaker']}: " if segment.get("speaker") else ""
        blocks.append(
            f"{idx}\n{format_timestamp(segment.get('start', 0.0), srt=True)} --> {format_timestamp(segment.get('end', 0.0), srt=True)}\n{speaker}{segment.get('text', '')}\n"
        )
    return "\n".join(blocks)


def render_vtt(data: dict[str, Any]) -> str:
    lines = ["WEBVTT", ""]
    for segment in data.get("segments", []):
        speaker = f"{segment['speaker']}: " if segment.get("speaker") else ""
        lines.append(
            f"{format_timestamp(segment.get('start', 0.0))} --> {format_timestamp(segment.get('end', 0.0))}\n{speaker}{segment.get('text', '')}\n"
        )
    return "\n".join(lines)


def write_output(data: dict[str, Any], output: Path, fmt: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif fmt == "txt":
        output.write_text(render_txt(data), encoding="utf-8")
    elif fmt == "srt":
        output.write_text(render_srt(data), encoding="utf-8")
    elif fmt == "vtt":
        output.write_text(render_vtt(data), encoding="utf-8")
    else:
        raise SttError(f"Unsupported format: {fmt}")


def build_parser() -> argparse.ArgumentParser:
    from yandex_speech.cli import build_parser as parser
    return parser()


def main() -> int:
    from yandex_speech.cli import transcribe_main
    return transcribe_main()


if __name__ == "__main__":
    raise SystemExit(main())
