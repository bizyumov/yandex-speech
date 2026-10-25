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


class TtsError(SpeechError):
    def __init__(self, message, category="CONFIG", *, code=None, request_id=None):
        super().__init__(category, message, code=code, request_id=request_id)


class TextToSpeech:
    def __init__(self, api_key: str | None = None, folder_id: str | None = None,
                 *, _context=None, _adapter_defaults=None) -> None:
        self.api_key = api_key or os.getenv("YANDEX_SPEECH_API_KEY")
        self.folder_id = folder_id or os.getenv("YANDEX_CLOUD_FOLDER_ID")
        self.context = _context
        self.adapter_defaults = _adapter_defaults or {}
        self.settings = resolve_settings(_context=self.context, _adapter=self.adapter_defaults)
        if not self.api_key:
            raise TtsError("YANDEX_SPEECH_API_KEY is required",category="AUTH")

    def _options(self, *, lang=None, voice=None, fmt=None, format=None, speed=None,
                 emotion=None, api_version=None, transport=None, model=None, role=None,
                 text_format=None, unsafe_mode=None, sample_rate=None, volume=None,
                 pitch_shift=None, duration_ms=None, duration_policy=None, **unknown):
        if unknown:
            raise SpeechError("CONFIG", "unknown standalone synthesis options")
        if format is not None and fmt is not None and format != fmt:
            raise TtsError("conflicting fmt/format options")
        values = {key:value for key,value in [
            ("default_lang",lang),("default_voice",voice),
            ("default_format",format if format is not None else fmt),
            ("default_speed",speed),("default_emotion",emotion),
            ("api_version",str(api_version) if api_version is not None else None),
            ("transport",transport),("model",model),("role",role),("text_format",text_format),
            ("unsafe_mode",unsafe_mode),("sample_rate_hertz",sample_rate),("volume",volume),
            ("pitch_shift",pitch_shift),("duration_ms",duration_ms),("duration_policy",duration_policy)] if value is not None}
        settings = resolve_settings(_context=self.context, _adapter=self.adapter_defaults,
                                    _explicit={"tts":values})
        from yandex_speech.tts_voices import resolve_voice, select_version
        options = resolve_voice(settings.value("tts"), settings.sources)
        version=select_version(options)
        if sample_rate is not None and options["default_format"]!="lpcm" and not(version=="1" and options["default_format"]=="wav"):
            raise SpeechError("CONFIG","sample-rate applies to LPCM output, not this container method")
        return settings, options, version

    def synthesize_bytes(self, text: str, *, deadline_seconds=120, **kwargs) -> bytes:
        settings,options,version=self._options(**kwargs)
        return self._synthesize_request(text,settings,options,version,deadline_seconds=deadline_seconds)

    def _synthesize_request(self, text, settings, options, version, *, deadline_seconds=120):
        import math
        if type(deadline_seconds) not in (int,float) or not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise SpeechError("CONFIG", "synthesis deadline must be finite and positive")
        if not isinstance(text,str) or not text.strip():
            raise TtsError("Text is empty")
        deadline_seconds=min(120,deadline_seconds)
        deadline = time.monotonic() + deadline_seconds
        transport = "rest"
        if version == "3":
            from yandex_speech.tts_v3 import request_body, rest_synthesize, grpc_synthesize, grpc_available
            body = request_body(text, options, sources=settings.sources)
            if options["transport"] == "grpc" or options["transport"] == "auto" and grpc_available():
                try:
                    data = grpc_synthesize(self.api_key,body,deadline_seconds=deadline_seconds)
                    transport = "grpc"
                except SpeechError as error:
                    if options["transport"] != "auto" or error.code != "GRPC_UNREACHABLE":
                        raise
                    data = rest_synthesize(self.api_key,body,deadline_seconds=max(.001,deadline-time.monotonic()))
            else:
                data = rest_synthesize(self.api_key,body,deadline_seconds=deadline_seconds)
        else:
            if options.get("unsafe_mode"):
                raise SpeechError("CONFIG", "unsafe mode is an API v3 utterance option")
            from yandex_speech.tts_text import v1_form, v1_fits, validate_ssml
            if options["text_format"]=="ssml":
                validate_ssml(text)
            if not v1_fits(text,options):
                raise SpeechError("LIMIT", "API v1 request exceeds 5000 code points or 15000 encoded body bytes")
            payload = v1_form(text,options)
            for attempt in range(3):
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise SpeechError("TIMEOUT", "synthesis deadline expired")
                try:
                    response=requests.post(API_URL,
                        headers={"Authorization":f"Api-Key {self.api_key}"},
                        data=payload,timeout=min(120,remaining))
                except requests.RequestException:
                    raise TtsError("TTS request acceptance is unknown; no automatic resubmission",
                                   category="SUBMISSION_UNCERTAIN") from None
                if response.status_code < 400 and "application/json" not in response.headers.get("Content-Type",""):
                    data=response.content
                    if not data:
                        raise TtsError("provider returned no synthesized audio",category="PROVIDER")
                    break
                if response.status_code in {429,500,502,503,504} and attempt < 2:
                    delay=min(1.5*(attempt+1),max(0,deadline-time.monotonic()))
                    time.sleep(delay)
                    continue
                category={401:"AUTH",403:"PERMISSION",429:"RATE_LIMIT"}.get(response.status_code,"PROVIDER")
                remedy="; verify ai.speechkit-tts.user and API key scope yc.ai.speechkitTts.execute" if response.status_code==403 else ""
                raise TtsError(f"TTS failed: HTTP {response.status_code}{remedy}",category=category,
                               code=str(response.status_code))
            else:
                raise TtsError("TTS retry budget exhausted",category="PROVIDER")
            if options["default_format"]=="lpcm" and len(data)%2:
                raise SpeechError("OUTPUT_PARTIAL","provider PCM ends inside a sample")
            if options["default_format"] == "wav":
                from yandex_speech.tts_audio import wrap_pcm
                data=wrap_pcm(data,options["sample_rate_hertz"])
        self.last_synthesis={"api_version":version,"transport":transport,
            "model":options["_model"],"voice":options["default_voice"],
            "language":options["default_lang"],"role":options["_role"] or options["_voice_info"]["default_role"],
            "format":options["default_format"],"conversion":None}
        return data

    def synthesize(self,text,output_path,*,overwrite=False,deadline_seconds=1800,**kwargs) -> list[Path]:
        import json
        import math
        import uuid
        from collections import deque
        from yandex_speech.tts_audio import join_audio
        from yandex_speech.tts_text import partition_plain,v1_fits,bisect_text,partition_ssml,bisect_ssml
        if type(overwrite) is not bool:
            raise SpeechError("CONFIG","overwrite must be explicitly boolean")
        if type(deadline_seconds) not in (int,float) or not math.isfinite(deadline_seconds) or deadline_seconds<=0:
            raise SpeechError("CONFIG","synthesis deadline must be finite and positive")
        output=Path(output_path)
        if output.is_symlink() or output.exists() and (not overwrite or not output.is_file()):
            raise SpeechError("CONFIG","select an absent output or explicitly overwrite an owned regular file")
        settings,options,version=self._options(**kwargs)
        fmt=options["default_format"]
        limit=5000 if options.get("unsafe_mode") else 250
        fits=lambda s:v1_fits(s,options) if version=="1" else len(s)<=limit
        pieces=partition_ssml(text,fits) if options["text_format"]=="ssml" else partition_plain(text,fits)
        if len(pieces)>1 and fmt in {"mp3","oggopus"} and not has_ffmpeg():
            raise SpeechError("PREREQUISITE","ffmpeg is required to assemble multipart encoded output")
        deadline=time.monotonic()+deadline_seconds
        pending=deque(pieces)
        completed=[]
        attempts=0
        self.last_synthesis={"status":"prepared","api_version":version,"format":fmt}
        try:
            while pending:
                if time.monotonic()>=deadline:
                    raise SpeechError("TIMEOUT","whole synthesis operation deadline expired")
                piece=pending.popleft()
                try:
                    data=self._synthesize_request(piece,settings,options,version,
                        deadline_seconds=deadline-time.monotonic())
                except SpeechError as error:
                    if error.category=="LIMIT" and error.code=="UTTERANCE_LIMIT" and attempts<32:
                        smaller=bisect_ssml(piece,fits) if options["text_format"]=="ssml" else bisect_text(piece)
                        pending.extendleft(reversed(smaller))
                        attempts+=1
                        continue
                    raise
                completed.append(data)
            result=join_audio(completed,fmt,options["sample_rate_hertz"],
                              timeout=deadline-time.monotonic())
            atomic_write(output,result,overwrite=overwrite)
            self.last_synthesis.update(status="complete",parts=len(completed),
                output=str(output),assembly="single" if len(completed)==1 else "complete-container")
            return [output]
        except Exception as error:
            self.last_synthesis.update(status="incomplete",parts=len(completed),output=None)
            if completed:
                directory=output.parent/(".yandex-speech-partial-"+uuid.uuid4().hex)
                directory.mkdir(parents=True,mode=0o700)
                for i,part in enumerate(completed):
                    atomic_write(directory/f"part-{i:06d}.{fmt}",part)
                atomic_write(directory/"manifest.json",json.dumps({
                    "status":"incomplete","parts":len(completed),"api_version":version,
                    "format":fmt,"reason":getattr(error,"category","OUTPUT_PARTIAL")}).encode())
                error.partial_dir=directory
            raise

    def stream_audio(self,text_or_fragments,*,deadline_seconds=1800,**kwargs):
        from google.protobuf.json_format import ParseDict
        from yandex_speech.tts_v3 import grpc_sdk,request_body
        from yandex_speech.tts_stream import AudioStream,GrpcStreamingTransport
        settings,options,version=self._options(**kwargs)
        if options["api_version"]=="1" or options["transport"]=="rest":
            raise SpeechError("CONFIG","native synthesis streaming requires API v3 over gRPC")
        if options["text_format"]!="plain":
            raise SpeechError("CONFIG","SSML is supported only by file synthesis API v1")
        if "3" not in options["_voice_info"]["versions"]:
            raise SpeechError("CONFIG","selected voice is not eligible for native v3 streaming")
        if kwargs.get("fmt") not in (None,"lpcm") or kwargs.get("format") not in (None,"lpcm"):
            raise SpeechError("CONFIG","native streaming emits headerless PCM only")
        if kwargs.get("sample_rate") not in (None,24000):
            raise SpeechError("CONFIG","native synthesis stream uses 24000 Hz")
        if options.get("unsafe_mode") or options.get("duration_ms",0):
            raise SpeechError("CONFIG","unsafe/duration utterance controls do not apply to StreamSynthesis")
        _,p,_=grpc_sdk()
        native=dict(options,default_format="lpcm",sample_rate_hertz=24000)
        body=request_body("stream options",native,sources=settings.sources)
        configuration={"model":body["model"],"outputAudioSpec":body["outputAudioSpec"]}
        for hint in body["hints"]:configuration.update(hint)
        if "role" not in configuration and options["_voice_info"]["default_role"] is not None:
            configuration["role"]=options["_voice_info"]["default_role"]
        fixed=ParseDict(configuration,p.SynthesisOptions())
        transport=getattr(self,"_stream_transport",None) or GrpcStreamingTransport(self.api_key)
        stream=AudioStream(text_or_fragments,transport,fixed,deadline_seconds=deadline_seconds)
        stream.metadata.update(api_version="3",transport="grpc",model=body["model"],
            voice=options["default_voice"],format="lpcm",audio_spec=stream.audio_spec)
        self.last_synthesis=stream.metadata
        return stream


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
    parser = argparse.ArgumentParser(description="Yandex SpeechKit synthesis API v1/v3")
    parser.add_argument("--version",action="version",version=__version__)
    parser.add_argument("text",nargs="?",help="Text to synthesize")
    parser.add_argument("--input-file",help="UTF-8 file, or - for stdin")
    parser.add_argument("--output",required=True)
    parser.add_argument("--config",type=Path)
    parser.add_argument("--overwrite",action="store_true")
    parser.add_argument("--lang")
    parser.add_argument("--voice")
    parser.add_argument("--format",dest="fmt",choices=["mp3","oggopus","lpcm","wav"])
    parser.add_argument("--speed",type=float)
    parser.add_argument("--emotion")
    parser.add_argument("--api-version", choices=["auto", "1", "3"])
    parser.add_argument("--transport", choices=["auto", "grpc", "rest"])
    parser.add_argument("--model", choices=["auto", "general", "general:rc", "general:deprecated", "livetts"])
    parser.add_argument("--role")
    parser.add_argument("--text-format", choices=["plain","ssml"])
    parser.add_argument("--stream",action="store_true")
    parser.add_argument("--unsafe-mode", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--sample-rate", type=int)
    parser.add_argument("--deadline-seconds", type=float, default=1800)
    parser.add_argument("--volume",type=float)
    parser.add_argument("--pitch-shift",type=float)
    parser.add_argument("--duration-ms",type=int)
    parser.add_argument("--duration-policy",choices=["min","max","exact"])
    for name in ["voice","lang","emotion","model","role"]:
        parser.add_argument("--adapter-"+name,help=argparse.SUPPRESS)
    return parser


def main() -> int:
    import json
    import tempfile
    from yandex_speech.cli import exit_code
    from yandex_speech.artifacts import publish_prepared_file
    parser=build_parser()
    args=parser.parse_args()
    if (args.text is None)==(args.input_file is None):
        parser.error("provide exactly one of text or --input-file")
    temporary=None
    try:
        if args.input_file not in {None,"-"}:
            source=Path(args.input_file);target=Path(args.output)
            if source.resolve()==target.resolve() or target.exists() and source.samefile(target):
                raise TtsError("output must not replace the addressed text source")
        context=RuntimeContext(user_home=Path.home(),
            workspace=Path(os.environ["HERMES_HOME"]) if os.getenv("HERMES_HOME") else None,
            selected_file=args.config)
        adapter={}
        for flag,key in [("voice","default_voice"),("lang","default_lang"),("emotion","default_emotion"),("model","model"),("role","role")]:
            value=getattr(args,"adapter_"+flag)
            if value:adapter[key]=value
        client=TextToSpeech(_context=context,_adapter_defaults={"tts":adapter})
        options=dict(lang=args.lang,voice=args.voice,fmt=args.fmt,speed=args.speed,
            emotion=args.emotion,api_version=args.api_version,transport=args.transport,
            model=args.model,role=args.role,text_format=args.text_format,
            unsafe_mode=args.unsafe_mode,sample_rate=args.sample_rate,
            volume=args.volume,pitch_shift=args.pitch_shift,
            duration_ms=args.duration_ms,duration_policy=args.duration_policy)
        if args.stream:
            from yandex_speech.tts_stream import StdinFragments
            if args.input_file is not None:
                if args.input_file=="-":
                    fragments=StdinFragments(sys.stdin.fileno())
                else:
                    fragments=StdinFragments(os.open(args.input_file,os.O_RDONLY))
            else:fragments=args.text
            target=Path(args.output)
            if args.output!="-":
                if target.is_symlink() or target.exists() and (not args.overwrite or not target.is_file()):
                    raise TtsError("stream output exists; choose another output or explicit overwrite")
                target.parent.mkdir(parents=True,exist_ok=True)
                fd,name=tempfile.mkstemp(prefix="."+target.name+".",suffix=".partial",dir=target.parent)
                temporary=Path(name)
                sink=os.fdopen(fd,"wb")
            else:sink=sys.stdout.buffer
            try:
                with client.stream_audio(fragments,deadline_seconds=args.deadline_seconds,**options) as audio:
                    for chunk in audio:
                        sink.write(chunk);sink.flush()
                    if not audio.input_complete:
                        raise SpeechError("OUTPUT_PARTIAL","text/audio stream did not complete")
                if args.output!="-":
                    os.fsync(sink.fileno());sink.close()
                    publish_prepared_file(temporary,target,overwrite=args.overwrite)
                    temporary=None
                    print(target)
            finally:
                if args.output!="-":sink.close()
                if args.input_file not in {None,"-"}:
                    os.close(fragments.descriptor)
        else:
            text=args.text
            if args.input_file is not None:
                text=sys.stdin.read() if args.input_file=="-" else Path(args.input_file).read_text(encoding="utf-8")
            outputs=client.synthesize(text,args.output,overwrite=args.overwrite,
                deadline_seconds=args.deadline_seconds,**options)
            for path in outputs:print(path)
        if getattr(client,"last_synthesis",None):
            print(json.dumps(client.last_synthesis,ensure_ascii=False),file=sys.stderr)
        return 0
    except (TtsError,SpeechError,OSError,UnicodeError) as error:
        message=str(error) if isinstance(error,SpeechError) else "cannot read input or write output"
        print(f"Error: {message}",file=sys.stderr)
        if temporary is not None:
            print(f"Incomplete PCM retained: {temporary}",file=sys.stderr)
        if getattr(error,"partial_dir",None):
            print(f"Incomplete synthesis parts retained: {error.partial_dir}",file=sys.stderr)
        return exit_code(error.category) if isinstance(error,SpeechError) else 1
    except KeyboardInterrupt:
        print("CANCELLED: synthesis interrupted",file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
