"""Portable Phase 1 transcription CLI, preserving explicit legacy modes."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .artifacts import atomic_write
from .config import RuntimeContext, resolve_settings
from .errors import SpeechError
from .media import AudioInputSpec
from .output import write_output
from .transcription import SpeechTranscriber


def exit_code(category):
    return {"CONFIG":2,"PREREQUISITE":3,"UNSUPPORTED_AUDIO":3,"LIMIT":3,
            "AUTH":4,"PERMISSION":4,"PROVIDER":5,"TRANSPORT":5,"RATE_LIMIT":5,
            "TIMEOUT":6,"OUTPUT_PARTIAL":7,"SUBMISSION_UNCERTAIN":7,
            "CANCELLED":130}.get(category,5)

def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version",action="version",version=__version__)
    parser.add_argument("input",nargs="?",help="Addressed media path, or - for arriving stdin")
    parser.add_argument("--resume",type=Path)
    parser.add_argument("--operation-id")
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--format",choices=["json","txt","srt","vtt"])
    parser.add_argument("--lang",dest="language_code")
    parser.add_argument("--adapter-language",help=argparse.SUPPRESS)
    parser.add_argument("--diarize",action=argparse.BooleanOptionalAction,default=None)
    parser.add_argument("--timestamps",action=argparse.BooleanOptionalAction,default=None)
    parser.add_argument("--literature-text",action=argparse.BooleanOptionalAction,default=None)
    parser.add_argument("--mode",choices=["auto","async","direct","direct-chunked","async-s3"],default=None)
    parser.add_argument("--transport",choices=["auto","grpc","rest"])
    parser.add_argument("--api-version",choices=["auto","1","2","3"])
    parser.add_argument("--config",type=Path)
    parser.add_argument("--job-file",type=Path,help="Publish a private recovery pointer to the durable job receipt")
    parser.add_argument("--deadline-seconds",type=float,default=1800)
    parser.add_argument("--overwrite",action="store_true")
    parser.add_argument("--audio-track",type=int)
    parser.add_argument("--audio-format",choices=["auto","lpcm"],default="auto")
    parser.add_argument("--sample-format")
    parser.add_argument("--sample-rate",type=int)
    parser.add_argument("--channels",type=int)
    parser.add_argument("--stream",action="store_true")
    parser.add_argument("--bucket",help="Explicit legacy async-s3 bucket; never creates a bucket")
    return parser


def _preflight_output(path,overwrite):
    if path.is_symlink() or (path.exists() and not overwrite):
        raise SpeechError("CONFIG","output exists or is a symlink; select another output or explicitly overwrite an owned regular file")
    if path.exists() and not path.is_file():
        raise SpeechError("CONFIG","output is not a regular file")


def _resume_path(path):
    try:
        document=json.loads(path.read_text(encoding="utf-8"))
        if document.get("kind") == "yandex-speech-job-pointer":
            return Path(document["manifest_path"])
        return path
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise SpeechError("CONFIG","cannot read a valid job receipt or recovery pointer") from None


def transcribe_main(argv=None):
    parser=build_parser()
    args=parser.parse_args(argv)
    if sum(value is not None for value in [args.input,args.resume,args.operation_id]) != 1:
        parser.error("provide exactly one audio input, --resume or --operation-id")
    if args.stream and (args.input != "-" or args.mode is not None):
        parser.error("--stream requires arriving stdin (-) and no explicit file mode")
    if args.input == "-" and not args.stream:
        parser.error("stdin requires --stream")
    if args.operation_id and args.api_version not in {"2","3"}:
        parser.error("--operation-id requires its explicit --api-version 2 or 3")
    if args.stream and (args.diarize or args.format in {"srt","vtt"}):
        parser.error("arriving recognition does not support diarization or subtitle export")
    try:
        _preflight_output(args.output,args.overwrite)
        recovering=args.resume is not None or args.operation_id is not None
        if recovering:
            if args.transport == "rest":
                raise SpeechError("CONFIG","REST recovery is Phase 2; Phase 1 recovery uses the saved gRPC v3 job")
            if (args.mode is not None or args.language_code is not None or args.diarize is not None
                    or args.timestamps is not None or args.literature_text is not None
                    or args.audio_track is not None or args.audio_format != "auto"
                    or args.sample_format is not None or args.sample_rate is not None
                    or args.channels is not None or args.bucket is not None):
                raise SpeechError("CONFIG","recovery uses saved recognition settings; new submission options are incompatible")
        context=RuntimeContext(user_home=Path.home(),
            workspace=Path(os.environ["HERMES_HOME"]) if os.getenv("HERMES_HOME") else None,
            selected_file=args.config)
        adapter={"language_code":args.adapter_language} if args.adapter_language else {}
        if args.bucket:
            adapter["object_storage"]={"bucket":args.bucket}
        settings=resolve_settings(_context=context,_adapter=adapter)
        effective_version=args.api_version or settings.value("api_version")
        new_recording=args.input is not None and not args.stream and args.mode in {None,"auto","async"} and effective_version not in {"1","2"}
        if new_recording and args.job_file is None:
            args.job_file=args.output.with_suffix(".job.json")
        if args.job_file is not None:
            if args.resume or args.operation_id or args.stream:
                raise SpeechError("CONFIG","--job-file only publishes a new recorded job pointer")
            _preflight_output(args.job_file,False)
            if args.job_file.resolve() == args.output.resolve():
                raise SpeechError("CONFIG","job and transcript paths must differ")
        if args.bucket is not None and args.mode != "async-s3" and not(effective_version == "2" and args.mode in {None,"auto"}):
            raise SpeechError("CONFIG","--bucket requires legacy async-s3 in Phase 1; REST v3 uploads are Phase 2")
        if args.input and args.input != "-" and args.output.resolve() == Path(args.input).resolve():
            raise SpeechError("CONFIG","the output must not replace the addressed source")
        
        fmt=args.format or settings.value("default_format")
        if args.stream and (settings.value("enable_diarization") and args.diarize is not False
                            or fmt in {"srt","vtt"}):
            raise SpeechError("CONFIG","arriving recognition cannot fulfill selected speaker/subtitle output")
        client=SpeechTranscriber(_context=context,_adapter_defaults=adapter)
        options={"language_code":args.language_code,"diarize":args.diarize,
                 "timestamps":args.timestamps,"literature_text":args.literature_text,
                 "api_version":args.api_version,"transport":args.transport}
        if fmt in {"srt","vtt"} and args.timestamps is None:
            options["timestamps"]=True
        if args.resume or args.operation_id:
            if args.api_version == "2":
                raise SpeechError("PREREQUISITE","v2 job recovery is Phase 2; Phase 1 recovers saved v3 jobs")
            result=client.poll_until_complete(_resume_path(args.resume) if args.resume else args.operation_id,
                api_version=args.api_version,transport=args.transport,deadline_seconds=args.deadline_seconds)
        elif args.stream:
            descriptor=AudioInputSpec(args.audio_format,args.sample_format,args.sample_rate,args.channels)
            with client.open_stream(audio_input=descriptor,deadline_seconds=args.deadline_seconds,**options) as stream:
                while data := sys.stdin.buffer.read(640 if args.audio_format == "lpcm" else 4096):
                    stream.push_audio(data)
                    if args.audio_format == "lpcm":
                        _,rate,channels,width=descriptor.raw_values()
                        time.sleep(len(data)/(rate*channels*width))
                result=stream.finalize()
        elif args.mode in {"direct","direct-chunked","async-s3"} or effective_version in {"1","2"} and args.mode in {None,"auto"}:
            if args.job_file:
                raise SpeechError("CONFIG","durable job receipts require the v3 recorded method in Phase 1")
            method={"direct":"transcribe_direct","direct-chunked":"transcribe_chunked_direct",
                    "async-s3":"transcribe_async_s3"}.get(args.mode,"transcribe_auto")
            result=getattr(client,method)(Path(args.input),
                audio_input=AudioInputSpec(args.audio_format,args.sample_format,args.sample_rate,args.channels),
                audio_track=args.audio_track,deadline_seconds=args.deadline_seconds,
                **({"bucket":args.bucket} if args.bucket is not None else {}),**options)
        else:
            descriptor=AudioInputSpec(args.audio_format,args.sample_format,args.sample_rate,args.channels)
            job=client.start_transcription(Path(args.input),audio_input=descriptor,
                audio_track=args.audio_track,deadline_seconds=args.deadline_seconds,**options)
            if args.job_file:
                atomic_write(args.job_file,json.dumps({"kind":"yandex-speech-job-pointer",
                    "manifest_path":str(job.manifest_path)}).encode())
            print(f"Recovery receipt: {job.manifest_path}",file=sys.stderr)
            result=client.poll_until_complete(job)
        if not result.get("input_complete"):
            raise SpeechError("OUTPUT_PARTIAL","incomplete input cannot be published as a transcript")
        write_output(result,args.output,fmt,overwrite=args.overwrite)
        print(args.output)
        return 0
    except SpeechError as error:
        print(str(error),file=sys.stderr)
        return exit_code(error.category)
    except (OSError,ValueError):
        print("INPUT_OUTPUT: cannot read selected input or publish output",file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("CANCELLED: local command interrupted; retain accepted job receipts for recovery",file=sys.stderr)
        return 130
