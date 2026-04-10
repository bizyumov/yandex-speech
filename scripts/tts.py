#!/usr/bin/env python3
"""Yandex SpeechKit TTS helper."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

import requests

API_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
DEFAULT_LANG = "ru-RU"
DEFAULT_VOICE = "alena"
DEFAULT_FORMAT = "mp3"
MAX_CHARS = 5000
MAX_RETRIES = 3


class TtsError(RuntimeError):
    pass


class TextToSpeech:
    def __init__(self, api_key: str | None = None, folder_id: str | None = None) -> None:
        self.api_key = api_key or os.getenv("YANDEX_SPEECH_API_KEY")
        self.folder_id = folder_id or os.getenv("YANDEX_CLOUD_FOLDER_ID")
        if not self.api_key:
            raise TtsError("YANDEX_SPEECH_API_KEY is required")
        if not self.folder_id:
            raise TtsError("YANDEX_CLOUD_FOLDER_ID is required")

    def synthesize_bytes(
        self,
        text: str,
        *,
        lang: str = DEFAULT_LANG,
        voice: str = DEFAULT_VOICE,
        fmt: str = DEFAULT_FORMAT,
        speed: float = 1.0,
        emotion: str = "neutral",
    ) -> bytes:
        if not 0.5 <= speed <= 3.0:
            raise TtsError("speed must be between 0.5 and 3.0")

        last_error: str | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    API_URL,
                    headers={"Authorization": f"Api-Key {self.api_key}"},
                    data={
                        "folderId": self.folder_id,
                        "text": text,
                        "lang": lang,
                        "voice": voice,
                        "format": fmt,
                        "speed": str(speed),
                        "emotion": emotion,
                    },
                    timeout=120,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt == MAX_RETRIES:
                    raise TtsError(f"TTS request failed after retries: {last_error}") from exc
                time.sleep(1.5 * attempt)
                continue

            content_type = response.headers.get("Content-Type", "")
            if response.status_code < 400 and "application/json" not in content_type:
                return response.content

            last_error = f"{response.status_code} {response.text}"
            if response.status_code in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)
                continue
            raise TtsError(f"TTS failed: {last_error}")

        raise TtsError(f"TTS failed after retries: {last_error}")

    def synthesize(self, text: str, output_path: str | Path, **kwargs) -> list[Path]:
        output = Path(output_path)
        chunks = split_text(text, MAX_CHARS)
        if not chunks:
            raise TtsError("Text is empty")

        if len(chunks) == 1:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(self.synthesize_bytes(chunks[0], **kwargs))
            return [output]

        part_paths: list[Path] = []
        for index, chunk in enumerate(chunks, start=1):
            part = output.with_name(f"{output.stem}.part{index:03d}{output.suffix}")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(self.synthesize_bytes(chunk, **kwargs))
            part_paths.append(part)

        fmt = kwargs.get("fmt", DEFAULT_FORMAT)
        if fmt == "mp3" and has_ffmpeg():
            concat_mp3(part_paths, output)
            return [output]
        return part_paths


def split_text(text: str, limit: int) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    if len(normalized) <= limit:
        return [normalized]

    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", normalized):
        if not sentence:
            continue
        if len(sentence) > limit:
            words = sentence.split(" ")
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if len(candidate) > limit:
                    if current:
                        chunks.append(current)
                    current = word
                else:
                    current = candidate
            continue
        candidate = sentence if not current else f"{current} {sentence}"
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def has_ffmpeg() -> bool:
    return subprocess.run(["bash", "-lc", "command -v ffmpeg >/dev/null 2>&1"], check=False).returncode == 0


def concat_mp3(parts: Iterable[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    list_file = output.with_suffix(output.suffix + ".concat.txt")
    list_file.write_text("".join(f"file '{part.resolve()}'\n" for part in parts), encoding="utf-8")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                str(output),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    finally:
        list_file.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Yandex SpeechKit TTS")
    parser.add_argument("text", nargs="?", help="Text to synthesize")
    parser.add_argument("--input-file", help="Read text from file")
    parser.add_argument("--output", required=True, help="Output audio path")
    parser.add_argument("--lang", default=DEFAULT_LANG)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--format", dest="fmt", choices=["mp3", "oggopus", "lpcm"], default=DEFAULT_FORMAT)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--emotion", default="neutral")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    text = args.text
    if args.input_file:
        text = Path(args.input_file).read_text(encoding="utf-8")
    if not text:
        print("Error: provide text or --input-file", file=sys.stderr)
        return 2

    try:
        tts = TextToSpeech()
        outputs = tts.synthesize(
            text,
            args.output,
            lang=args.lang,
            voice=args.voice,
            fmt=args.fmt,
            speed=args.speed,
            emotion=args.emotion,
        )
        for item in outputs:
            print(item)
        return 0
    except TtsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
