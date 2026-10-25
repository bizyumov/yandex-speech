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

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yandex_speech.config import RuntimeContext, resolve_settings
from yandex_speech.errors import SpeechError
from yandex_speech.artifacts import atomic_write

API_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
DEFAULT_LANG = "ru-RU"
DEFAULT_VOICE = "alena"
DEFAULT_FORMAT = "mp3"
MAX_CHARS = 5000
MAX_RETRIES = 3


class TtsError(RuntimeError):
    pass


class TextToSpeech:
    def __init__(self, api_key: str | None = None, folder_id: str | None = None,
                 *, _context=None, _adapter_defaults=None) -> None:
        self.api_key = api_key or os.getenv("YANDEX_SPEECH_API_KEY")
        self.folder_id = folder_id or os.getenv("YANDEX_CLOUD_FOLDER_ID")
        self.context = _context
        self.adapter_defaults = _adapter_defaults or {}
        self.settings = resolve_settings(_context=self.context, _adapter=self.adapter_defaults)
        if not self.api_key:
            raise TtsError("YANDEX_SPEECH_API_KEY is required")

    def synthesize_bytes(self, text: str, *, lang=None, voice=None, fmt=None,
                         speed=None, emotion=None, format=None) -> bytes:
        if format is not None and fmt is not None and format != fmt:
            raise TtsError("conflicting fmt/format options")
        values = {key:value for key,value in [
            ("default_lang",lang),("default_voice",voice),
            ("default_format",format if format is not None else fmt),
            ("default_speed",speed),("default_emotion",emotion)] if value is not None}
        settings = resolve_settings(_context=self.context, _adapter=self.adapter_defaults,
                                    _explicit={"tts":values})
        options = settings.value("tts")
        if options["api_version"] not in {"auto","1"} or options["transport"] == "grpc":
            raise TtsError("this preserved synthesis entry supports REST v1; v3 synthesis is Phase 3")
        if options["text_format"] != "plain":
            raise TtsError("SSML support is Phase 3")
        if not isinstance(text,str) or not text.strip():
            raise TtsError("Text is empty")
        if len(text) > MAX_CHARS:
            raise TtsError("single v1 request exceeds the 5000-character legacy budget")
        data = {"text":text,"lang":options["default_lang"],"voice":options["default_voice"],
                "format":options["default_format"],"speed":str(options["default_speed"]),
                "emotion":options["default_emotion"]}
        for attempt in range(1,MAX_RETRIES+1):
            try:
                response = requests.post(API_URL,
                    headers={"Authorization":f"Api-Key {self.api_key}"},
                    data=data,timeout=120)
            except requests.RequestException:
                if attempt == MAX_RETRIES:
                    raise TtsError("TTS transport failed after bounded retries") from None
                time.sleep(1.5*attempt)
                continue
            if response.status_code < 400 and "application/json" not in response.headers.get("Content-Type",""):
                return response.content
            if response.status_code in {429,500,502,503,504} and attempt < MAX_RETRIES:
                time.sleep(1.5*attempt)
                continue
            remedy = ("; verify ai.speechkit-tts.user and API key scope yc.ai.speechkitTts.execute"
                      if response.status_code == 403 else "")
            raise TtsError(f"TTS failed: HTTP {response.status_code}{remedy}")
        raise TtsError("TTS retry budget exhausted")

    def synthesize(self, text: str, output_path: str | Path, *, overwrite=False, **kwargs) -> list[Path]:
        import tempfile
        output = Path(output_path)
        chunks = split_text(text,MAX_CHARS)
        if not chunks:
            raise TtsError("Text is empty")
        if output.exists() and not overwrite:
            raise TtsError("output exists; explicit overwrite is required")
        if len(chunks) == 1:
            atomic_write(output,self.synthesize_bytes(chunks[0],**kwargs),overwrite=overwrite)
            return [output]
        fmt = kwargs.get("format") or kwargs.get("fmt") or self.settings.value("tts.default_format")
        with tempfile.TemporaryDirectory(prefix="yandex-speech-tts-") as directory:
            parts=[]
            for index,chunk in enumerate(chunks):
                part=Path(directory)/f"part{index:03d}.{fmt}"
                part.write_bytes(self.synthesize_bytes(chunk,**kwargs))
                parts.append(part)
            if fmt == "mp3" and has_ffmpeg():
                complete=Path(directory)/"complete.mp3"
                concat_mp3(parts,complete)
                atomic_write(output,complete.read_bytes(),overwrite=overwrite)
                return [output]
            # Preserve the legacy multipart return until Phase 3 complete-output assembly.
            targets=[output.with_name(f"{output.stem}.part{i:03d}{output.suffix}")
                     for i in range(1,len(parts)+1)]
            if not overwrite and any(p.exists() or p.is_symlink() for p in targets):
                raise TtsError("a multipart output exists; explicit overwrite is required")
            for part,target in zip(parts,targets):
                atomic_write(target,part.read_bytes(),overwrite=overwrite)
            return targets


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
    from yandex_speech import __version__
    parser = argparse.ArgumentParser(description="Yandex SpeechKit REST v1 TTS")
    parser.add_argument("--version",action="version",version=__version__)
    parser.add_argument("text",nargs="?",help="Text to synthesize")
    parser.add_argument("--input-file",help="UTF-8 file, or - for stdin")
    parser.add_argument("--output",required=True)
    parser.add_argument("--config",type=Path)
    parser.add_argument("--overwrite",action="store_true")
    parser.add_argument("--lang")
    parser.add_argument("--voice")
    parser.add_argument("--format",dest="fmt",choices=["mp3","oggopus","lpcm"])
    parser.add_argument("--speed",type=float)
    parser.add_argument("--emotion")
    return parser


def main() -> int:
    parser=build_parser()
    args=parser.parse_args()
    if (args.text is None) == (args.input_file is None):
        parser.error("provide exactly one of text or --input-file")
    try:
        text=args.text
        if args.input_file is not None:
            text=sys.stdin.read() if args.input_file == "-" else Path(args.input_file).read_text(encoding="utf-8")
        if args.input_file not in {None,"-"}:
            source=Path(args.input_file)
            target=Path(args.output)
            if source.resolve() == target.resolve() or (target.exists() and source.samefile(target)):
                raise TtsError("output must not replace the addressed text source")
        context=RuntimeContext(user_home=Path.home(),
            workspace=Path(os.environ["HERMES_HOME"]) if os.getenv("HERMES_HOME") else None,
            selected_file=args.config)
        outputs=TextToSpeech(_context=context).synthesize(text,args.output,
            lang=args.lang,voice=args.voice,fmt=args.fmt,speed=args.speed,
            emotion=args.emotion,overwrite=args.overwrite)
        for item in outputs:
            print(item)
        return 0
    except (TtsError,SpeechError,OSError,UnicodeError) as error:
        message=str(error) if isinstance(error,(TtsError,SpeechError)) else "cannot read input or write output"
        print(f"Error: {message}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
