"""Immutable complete-file synthesis planning before provider submission."""
from dataclasses import dataclass

from .errors import SpeechError
from .tts_capabilities import Capability, probe_joiner, probe_runtime
from .tts_text import partition_plain, partition_ssml, v1_fits


@dataclass(frozen=True)
class Exclusion:
    candidate: str
    category: str
    reason: str



@dataclass(frozen=True)
class RequestSnapshot:
    values: tuple[tuple[str, str | float | int | bool | None], ...]
    voice_versions: tuple[str, ...]
    voice_roles: tuple[str, ...]
    voice_default_role: str | None
    sources: tuple[tuple[str, str], ...]
    config_path: str | None
    profile_scope: str | None

    @classmethod
    def capture(cls, options, sources, config_path, profile_scope):
        values = tuple(sorted((key, value) for key, value in options.items() if key != "_voice_info"))
        if any(type(value) not in (str, float, int, bool, type(None)) for key, value in values):
            raise SpeechError("CONFIG", "resolved synthesis options must be scalar")
        info = options["_voice_info"]
        return cls(values, tuple(sorted(info["versions"])), tuple(sorted(info["roles"])),
                   info["default_role"], tuple(sorted((sources or {}).items())),
                   str(config_path) if config_path is not None else None,
                   str(profile_scope) if profile_scope is not None else None)

    def as_options(self):
        return dict(self.values, _voice_info={"versions": frozenset(self.voice_versions),
            "roles": frozenset(self.voice_roles), "default_role": self.voice_default_role})

@dataclass(frozen=True)
class SynthesisPlan:
    api_version: str
    transport: str
    method: str
    request: RequestSnapshot
    parts: tuple[str, ...]
    format: str
    assembly: str
    reason: str
    exclusions: tuple[Exclusion, ...]

    @property
    def candidate(self):
        return self.transport + "-v" + self.api_version + "-" + self.method


CANDIDATES = (("3", "grpc", "stream"), ("3", "grpc", "utterance"),
              ("3", "rest", "utterance"), ("1", "rest", "utterance"))


def plan_synthesis(text, options, *, runtime=None, stream_contract=None, joiner=None, excluded=(),
                   sources=None, config_path=None, profile_scope=None):
    """Filter exact constraints and complete output work, then rank ready candidates.

    stream_contract describes the implemented collector's verified parameter/framing
    contract; installed StreamSynthesis stubs alone cannot supply that evidence.
    """
    if not isinstance(text, str) or not text.strip():
        raise SpeechError("CONFIG", "synthesis text must contain speakable content")
    constraints = {candidate: _incompatibility(options, *candidate) for candidate in CANDIDATES}
    if all(constraints.values()):
        error = SpeechError("CONFIG", "no synthesis method supports the explicit selections: " +
            "; ".join("-".join(candidate) + ": " + reason for candidate, reason in constraints.items()))
        error.exclusions = tuple(Exclusion(transport + "-v" + version + "-" + method, "CONFIG", reason)
            for (version, transport, method), reason in constraints.items())
        raise error
    request = RequestSnapshot.capture(options, sources, config_path, profile_scope)
    options = request.as_options()
    runtime = runtime or probe_runtime(dict(options, model=options["_model"], role=options["_role"]))
    stream_contract = stream_contract or Capability("unverified", "streamed-file collector/selected contract is unverified")
    joiner = joiner or probe_joiner
    if not runtime.http.ready:
        raise SpeechError("PREREQUISITE", "base TTS dependency: " + runtime.http.reason)
    exclusions = []
    fmt = options["default_format"]
    for version, transport, method in CANDIDATES:
        candidate = transport + "-v" + version + "-" + method
        incompatibility = constraints[(version, transport, method)]
        if incompatibility:
            exclusions.append(Exclusion(candidate, "CONFIG", incompatibility))
            continue
        capability = (runtime.stream if method == "stream" else
                      runtime.utterance if transport == "grpc" else runtime.http)
        if candidate in excluded:
            capability = Capability("unreachable", "connection failed before any synthesis RPC began")
        if capability.ready and method == "stream":
            capability = stream_contract
        if not capability.ready:
            exclusions.append(Exclusion(candidate, "PREREQUISITE", capability.status + ": " + capability.reason))
            continue
        limit = 5000 if method == "stream" or options.get("unsafe_mode") else 250
        fits = (lambda value: v1_fits(value, options)) if version == "1" else (lambda value: len(value) <= limit)
        try:
            parts = tuple(partition_ssml(text, fits) if options["text_format"] == "ssml"
                          else partition_plain(text, fits))
        except SpeechError as error:
            exclusions.append(Exclusion(candidate, error.category, str(error)))
            continue
        if len(parts) > 1:
            assembly = joiner(fmt)
            if not assembly.ready:
                exclusions.append(Exclusion(candidate, "PREREQUISITE", assembly.status + ": " + assembly.reason))
                continue
        return SynthesisPlan(version, transport, method, request, parts, fmt,
            "single" if len(parts) == 1 else "complete-container",
            "first complete ready operation after explicit constraints and output preflight", tuple(exclusions))
    category = ("PREREQUISITE" if any(item.category == "PREREQUISITE" for item in exclusions)
                else "LIMIT" if any(item.category == "LIMIT" for item in exclusions) else "CONFIG")
    error = SpeechError(category, "no complete ready synthesis operation: " +
                        "; ".join(item.candidate + ": " + item.reason for item in exclusions))
    error.exclusions = tuple(exclusions)
    raise error


def _incompatibility(options, version, transport, method):
    if version not in options["_voice_info"]["versions"]:
        return "selected voice does not support API v" + version
    if options["api_version"] not in {"auto", version}:
        return "fixed API excludes this candidate"
    if options["transport"] not in {"auto", transport}:
        return "fixed transport excludes this candidate"
    requested = options.get("file_method", "auto")
    if requested not in {"auto", method}:
        return "fixed file method excludes this candidate"
    if options["text_format"] == "ssml" and version != "1":
        return "SSML requires REST v1"
    if options.get("unsafe_mode") and (version != "3" or method != "utterance"):
        return "explicit unsafe mode requires v3 utterance"
    if version == "1" and (
            options.get("model", "auto") not in {"auto", "general"}
            or options.get("volume", -19) != -19 or options.get("pitch_shift", 0) != 0
            or options.get("duration_ms", 0) > 0):
        return "selected model/control requires API v3"
    if method == "stream" and options.get("duration_ms", 0) > 0:
        return "duration control is unavailable in StreamSynthesis"
    if options["_model"] == "livetts" and (
            options["default_speed"] != 1 or options.get("pitch_shift", 0) != 0
            or options.get("duration_ms", 0) > 0):
        return "selected LiveTTS speed/pitch/duration control is unsupported"
    return None
