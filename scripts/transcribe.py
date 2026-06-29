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


class SpeechTranscriber:
    def __init__(self, api_key: str | None = None, folder_id: str | None = None) -> None:
        self.api_key = api_key or require_env("YANDEX_SPEECH_API_KEY")
        self.folder_id = folder_id or require_env("YANDEX_CLOUD_FOLDER_ID")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Api-Key {self.api_key}"})

    def transcribe_direct(self, path: Path, *, language_code: str, diarize: bool) -> dict[str, Any]:
        prepared_path, cleanup, extra_params = prepare_sync_audio(path)
        try:
            with open(prepared_path, "rb") as handle:
                response = self.session.post(
                    SYNC_URL,
                    params={
                        "folderId": self.folder_id,
                        "lang": language_code,
                        "topic": "general",
                        **extra_params,
                    },
                    data=handle,
                    timeout=600,
                )
            if response.status_code >= 400:
                raise SttError(f"Direct STT failed: {response.status_code} {response.text}")
            payload = response.json()
            text = payload.get("result", "")
            return {
                "text": text,
                "segments": [{"start": 0.0, "end": 0.0, "speaker": None, "text": text}] if text else [],
                "raw": payload,
                "mode": "direct",
                "diarization_requested": diarize,
                "prepared_input": str(prepared_path),
            }
        finally:
            if cleanup is not None:
                cleanup.unlink(missing_ok=True)

    def transcribe_auto(self, path: Path, *, language_code: str, diarize: bool) -> dict[str, Any]:
        try:
            return self.transcribe_direct(path, language_code=language_code, diarize=diarize)
        except SttError as exc:
            message = str(exc)
            if "audio should be less than 1 mb" not in message and "audio duration should be less than 30s" not in message:
                raise
            return self.transcribe_chunked_direct(path, language_code=language_code, diarize=diarize)

    def transcribe_chunked_direct(self, path: Path, *, language_code: str, diarize: bool) -> dict[str, Any]:
        if not has_ffmpeg():
            raise SttError("Chunked direct STT fallback requires ffmpeg")
        with tempfile.TemporaryDirectory(prefix="yandex-stt-chunks-") as tmp:
            tmp_path = Path(tmp)
            pattern = tmp_path / "chunk_%03d.ogg"
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "16k",
                    "-f",
                    "segment",
                    "-segment_time",
                    "25",
                    "-reset_timestamps",
                    "1",
                    str(pattern),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            chunks = sorted(tmp_path.glob("chunk_*.ogg"))
            if result.returncode != 0 or not chunks:
                raise SttError(f"ffmpeg split for direct STT failed: {result.stderr[-500:]}")

            segments: list[dict[str, Any]] = []
            full_text: list[str] = []
            offset = 0.0
            for chunk in chunks:
                data = self.transcribe_direct(chunk, language_code=language_code, diarize=diarize)
                duration = probe_duration(chunk) or 25.0
                for segment in data.get("segments", []):
                    shifted = dict(segment)
                    shifted["start"] = float(segment.get("start", 0.0) or 0.0) + offset
                    shifted["end"] = float(segment.get("end", 0.0) or 0.0) + offset
                    segments.append(shifted)
                text = data.get("text", "").strip()
                if text:
                    full_text.append(text)
                offset += duration

            return {
                "text": "\n".join(full_text),
                "segments": segments,
                "raw": {"chunks": [str(chunk) for chunk in chunks]},
                "mode": "direct-chunked",
                "diarization_requested": diarize,
                "source_input": str(path),
            }

    def transcribe_async_s3(self, path: Path, *, language_code: str, diarize: bool, poll_interval: float = 5.0) -> dict[str, Any]:
        uri = upload_to_object_storage(path)
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
        while True:
            current = self.session.get(f"{OPERATIONS_URL}/{operation_id}", timeout=120)
            current.raise_for_status()
            payload = current.json()
            if payload.get("done"):
                if "error" in payload:
                    raise SttError(json.dumps(payload["error"], ensure_ascii=False))
                return normalize_async_result(payload, uri=uri, operation_id=operation_id)
            print(f"waiting for operation {operation_id}", file=sys.stderr)
            time.sleep(poll_interval)


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


def upload_to_object_storage(path: Path) -> str:
    if boto3 is None:
        raise SttError("boto3 is required for async-s3 mode")
    bucket = require_env("YANDEX_SPEECH_S3_BUCKET")
    access_key = require_env("YANDEX_SPEECH_AWS_ACCESS_KEY_ID")
    secret_key = require_env("YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY")
    prefix = os.getenv("YANDEX_SPEECH_S3_PREFIX", "speechkit")
    endpoint = os.getenv("YANDEX_SPEECH_S3_ENDPOINT", "https://storage.yandexcloud.net")
    key = f"{prefix.rstrip('/')}/{path.name}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    client.upload_file(str(path), bucket, key)
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
    parser = argparse.ArgumentParser(description="Yandex SpeechKit STT")
    parser.add_argument("input", help="Audio file path")
    parser.add_argument("--output", required=True, help="Output path")
    parser.add_argument("--format", choices=["json", "txt", "srt", "vtt"], default="json")
    parser.add_argument("--lang", default=DEFAULT_LANG)
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument("--mode", choices=["auto", "direct", "async-s3"], default="auto")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source = Path(args.input)
    if not source.exists():
        print(f"Error: file not found: {source}", file=sys.stderr)
        return 2
    try:
        transcriber = SpeechTranscriber()
        if args.mode == "auto":
            data = transcriber.transcribe_auto(source, language_code=args.lang, diarize=args.diarize)
        elif args.mode == "direct":
            data = transcriber.transcribe_direct(source, language_code=args.lang, diarize=args.diarize)
        else:
            data = transcriber.transcribe_async_s3(source, language_code=args.lang, diarize=args.diarize)
        write_output(data, Path(args.output), args.format)
        print(args.output)
        return 0
    except (SttError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
