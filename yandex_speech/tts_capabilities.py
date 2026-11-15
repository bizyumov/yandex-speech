"""Non-billable TTS interface and encoded-assembly readiness probes."""
from __future__ import annotations

import importlib
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    status: str
    reason: str

    @property
    def ready(self):
        return self.status == "ready"


@dataclass(frozen=True)
class RuntimeCapabilities:
    http: Capability
    grpc: Capability
    utterance: Capability
    stream: Capability


READY = Capability("ready", "required interface is usable")
MODULES = {
    "grpc": "grpc",
    "protobuf": "google.protobuf.json_format",
    "messages": "yandex.cloud.ai.tts.v3.tts_pb2",
    "service": "yandex.cloud.ai.tts.v3.tts_service_pb2_grpc",
}
MESSAGE_FIELDS = {
    "UtteranceSynthesisRequest": {"model", "text", "hints", "output_audio_spec", "unsafe_mode"},
    "UtteranceSynthesisResponse": {"audio_chunk", "chunk_type"},
    "SynthesisOptions": {"model", "voice", "role", "speed", "volume", "pitch_shift", "output_audio_spec"},
    "StreamSynthesisRequest": {"options", "synthesis_input", "force_synthesis"},
    "StreamSynthesisResponse": {"audio_chunk", "text_chunk", "chunk_type"},
    "SynthesisInput": {"text"},
    "ForceSynthesisEvent": set(),
    "AudioChunk": {"data"},
    "AudioFormatOptions": {"raw_audio", "container_audio"},
}


def _load(name):
    try:
        return importlib.import_module(name), READY
    except ModuleNotFoundError as error:
        absent = error.name and (name == error.name or name.startswith(error.name + "."))
        status = "absent" if absent else "import-broken"
        return None, Capability(status, name + ": " + status)
    except Exception as error:
        return None, Capability("import-broken", name + ": " + type(error).__name__)


class _ProbeChannel:
    """Construct a generated stub without opening a channel or invoking an RPC."""
    def unary_stream(self, *args, **kwargs):
        return lambda *a, **k: None

    def stream_stream(self, *args, **kwargs):
        return lambda *a, **k: None


def _interface(messages, service, method, names):
    try:
        for name in names:
            message = getattr(messages, name)
            fields = set(message.DESCRIPTOR.fields_by_name)
            missing = MESSAGE_FIELDS[name] - fields
            if missing:
                return Capability("schema-incompatible", name + " missing fields: " + ", ".join(sorted(missing)))
        stub = service.SynthesizerStub(_ProbeChannel())
        if not callable(getattr(stub, method, None)):
            return Capability("schema-incompatible", "SynthesizerStub." + method + " is unavailable")
    except Exception as error:
        return Capability("schema-incompatible", method + ": " + type(error).__name__)
    return READY


def probe_runtime(options=None):
    """Inspect real imports/fields/stub interfaces; never compile, install or synthesize."""
    requests, http = _load("requests")
    if http.ready and not callable(getattr(requests, "post", None)):
        http = Capability("schema-incompatible", "requests.post is unavailable")
    loaded = {key: _load(name) for key, name in MODULES.items()}
    common = next((state for module, state in loaded.values() if not state.ready), READY)
    if common.ready:
        grpc = loaded["grpc"][0]
        parser = loaded["protobuf"][0]
        if not all(callable(getattr(grpc, name, None)) for name in
                   ("secure_channel", "ssl_channel_credentials", "channel_ready_future")):
            common = Capability("schema-incompatible", "grpc TLS/channel interfaces are unavailable")
        elif not callable(getattr(parser, "ParseDict", None)):
            common = Capability("schema-incompatible", "protobuf ParseDict is unavailable")
    if not common.ready:
        return RuntimeCapabilities(http, common, common, common)
    messages, service = loaded["messages"][0], loaded["service"][0]
    utterance = _interface(messages, service, "UtteranceSynthesis",
        ("UtteranceSynthesisRequest", "UtteranceSynthesisResponse", "AudioChunk", "AudioFormatOptions"))
    stream = _interface(messages, service, "StreamSynthesis",
        ("SynthesisOptions", "StreamSynthesisRequest", "StreamSynthesisResponse",
         "SynthesisInput", "ForceSynthesisEvent", "AudioChunk", "AudioFormatOptions"))
    if options is not None and http.ready:
        if utterance.ready:
            utterance = _selected_schema(options, messages, loaded["protobuf"][0].ParseDict, stream=False)
        if stream.ready:
            stream = _selected_schema(options, messages, loaded["protobuf"][0].ParseDict, stream=True)
    return RuntimeCapabilities(http, common, utterance, stream)



def _selected_schema(options, messages, parse, *, stream):
    """Parse selected nested output/controls using the same SDK shape as execution."""
    try:
        from .tts_v3 import request_body
        effective = dict(options, model=options.get("_model", options.get("model", "auto")),
                         role=options.get("_role", options.get("role")))
        body = request_body("schema preflight", effective)
        if stream:
            if options.get("duration_ms", 0):
                return Capability("schema-incompatible", "StreamSynthesis has no duration control")
            configuration = {"model": body["model"], "outputAudioSpec": body["outputAudioSpec"]}
            for hint in body["hints"]:
                configuration.update(hint)
            default_role = options.get("_voice_info", {}).get("default_role")
            if "role" not in configuration and default_role is not None:
                configuration["role"] = default_role
            parse(configuration, messages.SynthesisOptions())
        else:
            parse(body, messages.UtteranceSynthesisRequest())
    except Exception as error:
        return Capability("schema-incompatible", "selected " +
            ("stream" if stream else "utterance") + " request: " + type(error).__name__)
    return READY

def probe_joiner(fmt):
    """Check concat/demuxer/muxer availability; stream copy needs no encoder."""
    if fmt in {"wav", "lpcm"}:
        return Capability("ready", "standard-library PCM/WAV assembly")
    names = {"oggopus": "ogg", "mp3": "mp3"}
    if fmt not in names:
        return Capability("schema-incompatible", "unsupported encoded assembly format")
    executable = shutil.which("ffmpeg")
    if executable is None:
        return Capability("absent", "ffmpeg is required for independent encoded parts")
    try:
        tables = {}
        for kind in ("demuxers", "muxers"):
            result = subprocess.run([executable, "-hide_banner", "-" + kind],
                capture_output=True, text=True, timeout=5, check=False)
            if result.returncode:
                return Capability("executable-unusable", "ffmpeg cannot list " + kind)
            tables[kind] = {name for line in result.stdout.splitlines()
                if len(columns := line.split()) >= 2 and columns[0] in {"D", "E", "DE"}
                for name in columns[1].split(",")}
    except (OSError, subprocess.SubprocessError):
        return Capability("executable-unusable", "ffmpeg capability probe failed")
    required = {"demuxers": {"concat", names[fmt]}, "muxers": {names[fmt]}}
    missing = [kind + ":" + name for kind in required for name in sorted(required[kind] - tables[kind])]
    if missing:
        return Capability("executable-unusable", "ffmpeg missing " + ", ".join(missing))
    return Capability("ready", "ffmpeg concat and " + names[fmt] + " demuxer/muxer; stream copy")
