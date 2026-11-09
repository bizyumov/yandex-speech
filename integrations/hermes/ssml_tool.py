"""Explicit, profile-scoped SSML synthesis through the stock plugin tool API."""
import json
import math
import stat
import time
from pathlib import Path

from yandex_speech.artifacts import private_job_dir
from yandex_speech.errors import SpeechError
from yandex_speech.tts_text import validate_ssml


TOOL_NAME = "yandex_speech_ssml"
DELIVERY_INSTRUCTIONS = (
    "For explicit SSML pronunciation requests, use yandex_speech_ssml with one complete "
    "<speak> document. Ordinary plain-text replies use text_to_speech. The built-in "
    "speech tool does not preserve raw SSML. On success, emit the returned media_tag "
    "FIELD VALUE verbatim in the final assistant response, outside JSON, quotes, code "
    "or examples. Do not copy the whole JSON result. Do not send the same audio again "
    "through another tool. This tool only generates audio; do not claim that a file "
    "was delivered or heard before the gateway/user confirms it. It does not speak "
    "in a connected Discord voice channel."
)
SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Generate a voice attachment from explicit Yandex SSML using REST API v1. "
        "Requires one complete <speak> document; preserves aliases and phonemes. "
        "Uses saved profile preferences unless voice/role/language/model/speed are "
        "provided. V3-only voices and LiveTTS cannot be used. "
        + DELIVERY_INSTRUCTIONS
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ssml": {"type": "string", "description": "Complete supported <speak> XML document."},
            "voice": {"type": ["string", "null"], "description": "Explicit v1-compatible voice ID."},
            "role": {"type": ["string", "null"], "description": "Literal role supported by the selected voice."},
            "language": {"type": ["string", "null"], "description": "Explicit synthesis language code."},
            "model": {"type": ["string", "null"], "description": "Explicit compatible general model."},
            "speed": {"type": ["number", "null"], "description": "Explicit speed, validated by the synthesis core."},
        },
        "required": ["ssml"],
        "additionalProperties": False,
    },
}


class HermesSSMLTool:
    def __init__(self, provider, *, _profile_loader=None):
        self.provider = provider
        self._profile_loader = _profile_loader

    def _client(self):
        if self._profile_loader is None:
            return self.provider._client()
        from .tts_provider import YandexTTSProvider
        profile = self._profile_loader()
        if not isinstance(profile, dict):
            raise SpeechError("CONFIG", "native TTS preferences must be a mapping")
        current = YandexTTSProvider(
            self.provider.workspace, profile=profile,
            _secret_resolver=self.provider._secret_resolver,
            _client_factory=self.provider._client_factory)
        return current._client()

    def is_available(self):
        # Do not hide all compatible per-call choices because one saved voice is v3-only.
        try:
            self._client()
            return True
        except Exception:
            return False

    def _arguments(self, args):
        allowed = set(SCHEMA["parameters"]["properties"])
        if not isinstance(args, dict) or set(args) - allowed:
            raise SpeechError("CONFIG", "unknown SSML tool arguments")
        args = {name: value for name, value in args.items() if value is not None or name == "ssml"}
        if not isinstance(args.get("ssml"), str) or not args["ssml"].strip():
            raise SpeechError("CONFIG", "ssml must be one nonempty speak document")
        for name in ("voice", "role", "language", "model"):
            if name in args and (not isinstance(args[name], str) or not args[name].strip()):
                raise SpeechError("CONFIG", name + " must be a nonempty string")
        if "speed" in args and (type(args["speed"]) not in (int, float)
                or not math.isfinite(args["speed"])):
            raise SpeechError("CONFIG", "speed must be finite and numeric")
        validate_ssml(args["ssml"])
        options = {"text_format": "ssml", "api_version": "1",
                   "transport": "rest", "format": "oggopus"}
        for source, target in (("voice", "voice"), ("role", "role"),
                               ("language", "lang"), ("model", "model"), ("speed", "speed")):
            if source in args:
                options[target] = args[source]
        return args["ssml"], options

    def _new_job(self):
        workspace = self.provider.workspace.resolve()
        base = workspace / "cache" / "yandex-speech-ssml"
        if any(ord(c) < 32 or c in {'"', "'"} for c in str(base)):
            raise SpeechError("CONFIG", "profile audio path cannot be represented safely")
        if not base.resolve().is_relative_to(workspace):
            raise SpeechError("CONFIG", "SSML audio cache must remain inside the active profile")
        try:
            return private_job_dir(base)
        except OSError:
            raise SpeechError("STATE", "cannot allocate profile-owned SSML audio output") from None

    def handle(self, args):
        started = time.monotonic()
        job = None
        try:
            text, options = self._arguments(args)
            client = self._client()
            # Validate the complete effective voice/role/API selection before allocating output.
            client._options(**options)
            job = self._new_job()
            output = job / "speech.ogg"
            client.synthesize(text, output, **options)
            info = output.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise SpeechError("STATE", "SSML synthesis did not publish a complete regular file")
            path = str(output)
            return json.dumps({
                "success": True, "provider": "yandex", "api_version": "1",
                "transport": "rest", "text_format": "ssml", "format": "oggopus",
                "file_path": path, "file_paths": [path], "voice_compatible": True,
                "media_tag": "[[audio_as_voice]]\nMEDIA:" + path,
                "delivery_status": "awaiting_host_delivery",
                "synthesis_seconds": time.monotonic() - started,
            }, ensure_ascii=False)
        except SpeechError as error:
            result = {"success": False, "error": error.as_dict()}
        except Exception:
            result = {"success": False, "error": {
                "category": "INTERNAL", "message": "SSML synthesis failed in the active profile"}}
        if job is not None:
            try:
                if any(job.iterdir()):
                    result["artifact_dir"] = str(job)
                else:
                    job.rmdir()
            except OSError:
                pass
        return json.dumps(result, ensure_ascii=False)


def register_ssml_tool(ctx, provider):
    def current_profile():
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config_readonly
        if get_hermes_home().resolve() != provider.workspace.resolve():
            raise SpeechError("AUTH", "active Hermes profile does not match the SSML tool")
        return (load_config_readonly() or {}).get("tts") or {}

    tool = HermesSSMLTool(provider, _profile_loader=current_profile)
    handle = ctx.register_tool(
        TOOL_NAME, "tts", SCHEMA, tool.handle, check_fn=tool.is_available,
        description=SCHEMA["description"])
    if handle is None:
        raise SpeechError("CONFIG", "yandex_speech_ssml conflicts with an existing tool")
    ctx.register_system_prompt_section(
        "yandex-speech-ssml", DELIVERY_INSTRUCTIONS)
    return tool
