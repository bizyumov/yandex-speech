"""One admitted streaming RPC collected as a complete encoded file or LPCM.

Transport records are fragments of one verified provider container. Independent
RPC outputs are assembled by the caller; they are never blindly byte-concatenated.
"""
from dataclasses import dataclass
import math
import tempfile
import threading
import time
from pathlib import Path

from .artifacts import atomic_write
from .errors import SpeechError
from .tts_audio import finalize_audio
from .tts_capabilities import Capability
from .tts_v3 import grpc_sdk, request_body

MAX_AUDIO_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class CollectedAudio:
    data: bytes
    response_records: int
    audio_records: int
    first_audio_seconds: float
    final_seconds: float


def stream_contract(options):
    """Model/output framing covered by the explicit provider contract acceptance."""
    model = options.get("_model", options.get("model"))
    if model not in {"general", "livetts"}:
        return Capability("unverified", "streamed-file framing for the selected model is unverified")
    if options["default_format"] not in {"oggopus", "mp3", "wav", "lpcm"}:
        return Capability("schema-incompatible", "unsupported streamed-file output format")
    if options.get("unsafe_mode") or options.get("duration_ms", 0):
        return Capability("schema-incompatible", "unsafe/duration controls require utterance synthesis")
    if model == "livetts" and (options["default_speed"] != 1 or options.get("pitch_shift", 0) != 0):
        return Capability("schema-incompatible", "LiveTTS stream does not support speed or pitch")
    return Capability("ready", "general/LiveTTS single-RPC output framing verified; native commissioning remains separate")


def synthesis_options(options, sources=None):
    from google.protobuf.json_format import ParseDict
    _, p, _ = grpc_sdk()
    effective = dict(options, model=options["_model"], role=options["_role"])
    body = request_body("stream configuration", effective, sources=sources)
    configuration = {"model": body["model"], "outputAudioSpec": body["outputAudioSpec"]}
    for hint in body["hints"]:
        configuration.update(hint)
    default = options["_voice_info"]["default_role"]
    if "role" not in configuration and default is not None:
        configuration["role"] = default
    return ParseDict(configuration, p.SynthesisOptions())


def collect_stream_file(text, options, *, api_key, deadline_seconds=120,
                        sources=None, partial_directory=None, cancel_event=None,
                        _transport=None):
    """Drain one bounded RPC to successful EOF before returning validated audio."""
    started = time.monotonic()
    if not isinstance(text, str) or not text.strip() or len(text) > 5000:
        raise SpeechError("LIMIT", "streamed-file RPC requires 1–5000 admitted characters")
    if type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
        raise SpeechError("CONFIG", "stream deadline must be finite and positive")
    deadline = started + deadline_seconds
    if (options.get("api_version") == "1" or options.get("transport") == "rest"
            or options["text_format"] != "plain" or options.get("unsafe_mode")
            or options.get("duration_ms", 0)):
        raise SpeechError("CONFIG", "streamed-file synthesis requires compatible plain-text v3/gRPC controls")
    contract = stream_contract(options)
    if not contract.ready:
        raise SpeechError("PREREQUISITE", contract.reason)
    configuration = synthesis_options(options, sources)
    grpc, p, _ = grpc_sdk()
    from .tts_stream import GrpcStreamingTransport
    directory = None
    if partial_directory is not None:
        directory = Path(partial_directory)
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except OSError:
            raise SpeechError("CONFIG", "partial evidence requires a new writable owned directory") from None
    transport = _transport
    done, drained, expired = threading.Event(), threading.Event(), threading.Event()
    first_audio, record_count, audio_count = None, 0, 0
    watcher, failure, closed = None, None, False

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise SpeechError("CANCELLED", "streamed-file synthesis was cancelled")
        if expired.is_set() or time.monotonic() >= deadline:
            raise SpeechError("TIMEOUT", "whole streamed-file deadline expired")

    def requests():
        check()
        yield p.StreamSynthesisRequest(options=configuration)
        check()
        yield p.StreamSynthesisRequest(synthesis_input=p.SynthesisInput(text=text))
        check()
        yield p.StreamSynthesisRequest(force_synthesis=p.ForceSynthesisEvent())
        drained.set()

    def watch():
        while not done.wait(min(.02, max(.001, deadline-time.monotonic()))):
            cancelled = cancel_event is not None and cancel_event.is_set()
            if cancelled or time.monotonic() >= deadline:
                if not cancelled:
                    expired.set()
                try:
                    transport.close()
                except Exception:
                    pass  # Main-path cleanup retains its own failure without hiding the cause.
                return

    # Spill beyond 1 MiB and reject runaway responses; no unbounded audio queue.
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as stage:
        try:
            check()
            transport = transport or GrpcStreamingTransport(api_key)
            watcher = threading.Thread(target=watch, name="yandex-file-deadline", daemon=True)
            watcher.start()
            call = transport.open_stream(requests(), timeout=max(.001, deadline-time.monotonic()))
            for response in call:
                check()
                record_count += 1
                kind = response.DESCRIPTOR.fields_by_name["chunk_type"].enum_type.values_by_number.get(response.chunk_type)
                if kind is not None and kind.name == "NO_SPEAKABLE_TEXT":
                    raise SpeechError("OUTPUT_PARTIAL", "provider returned no speakable text")
                data = response.audio_chunk.data
                if not data:
                    continue
                if first_audio is None:
                    first_audio = time.monotonic() - started
                if stage.tell() + len(data) > MAX_AUDIO_BYTES:
                    raise SpeechError("LIMIT", "one streaming RPC exceeded its 256 MiB staging budget")
                stage.write(data)
                audio_count += 1
            check()
            if not drained.wait(min(.1, max(0, deadline-time.monotonic()))):
                raise SpeechError("OUTPUT_PARTIAL", "provider ended before complete request EOF")
            stage.seek(0)
            data = finalize_audio(stage.read(), options["default_format"])
            check()
            transport.close()
            closed = True
            return CollectedAudio(data, record_count, audio_count, first_audio,
                                  time.monotonic() - started)
        except BaseException as error:
            if cancel_event is not None and cancel_event.is_set():
                failure = SpeechError("CANCELLED", "streamed-file synthesis was cancelled")
            elif expired.is_set() or time.monotonic() >= deadline:
                failure = SpeechError("TIMEOUT", "whole streamed-file deadline expired")
            elif isinstance(error, (SpeechError, KeyboardInterrupt, SystemExit)):
                failure = error
            elif isinstance(error, grpc.RpcError):
                code = error.code().name
                category = {"UNAUTHENTICATED": "AUTH", "PERMISSION_DENIED": "PERMISSION",
                    "RESOURCE_EXHAUSTED": "RATE_LIMIT", "DEADLINE_EXCEEDED": "TIMEOUT",
                    "CANCELLED": "CANCELLED", "INVALID_ARGUMENT": "CONFIG"}.get(code, "OUTPUT_PARTIAL")
                failure = SpeechError(category, "streaming RPC failed before complete EOF; no automatic replay", code=code)
            else:
                failure = SpeechError("OUTPUT_PARTIAL", "streaming response failed before complete EOF")
            if stage.tell() and directory is not None:
                failure.partial_dir = directory
                try:
                    stage.seek(0)
                    atomic_write(directory / "incomplete-provider-audio.bin", stage.read())
                except Exception as retention_error:
                    failure.partial_retention_error = type(retention_error).__name__
            raise failure from None
        finally:
            done.set()
            if transport is not None and not closed:
                try:
                    transport.close()
                except Exception as cleanup_error:
                    if failure is not None:
                        failure.cleanup_error = type(cleanup_error).__name__
            if watcher is not None:
                watcher.join(timeout=1)
            if directory is not None and (failure is None or not hasattr(failure, "partial_dir")):
                try:
                    directory.rmdir()  # Only the invocation-created empty directory.
                except OSError:
                    pass
