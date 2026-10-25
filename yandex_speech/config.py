"""Home-scoped non-secret configuration and one provenance-preserving resolver."""
from __future__ import annotations

import copy
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import SpeechError

DEFAULTS = {
    "default_format": "json", "enable_diarization": False, "enable_timestamps": False,
    "language_code": "ru-RU", "transport": "auto", "api_version": "auto",
    "profanity_filter": False, "literature_text": True,
    "object_storage": {"endpoint": "https://storage.yandexcloud.net", "prefix": "speechkit"},
    "tts": {"api_version": "auto", "transport": "auto", "text_format": "plain",
            "default_voice": "alena", "default_lang": "ru-RU", "default_format": "mp3",
            "default_speed": 1.0, "default_emotion": "neutral", "model": "auto",
            "role": "auto", "unsafe_mode": False, "sample_rate_hertz": 48000,
            "volume": -19.0, "pitch_shift": 0.0, "duration_ms": 0, "duration_policy": "max"},
}
SCHEMA = {
    "default_format": str, "enable_diarization": bool, "enable_timestamps": bool,
    "language_code": str, "transport": str, "api_version": str,
    "profanity_filter": bool, "literature_text": bool,
    "object_storage": {"bucket": str, "endpoint": str, "prefix": str},
    "tts": {"api_version": str, "transport": str, "text_format": str,
            "default_voice": str, "default_lang": str, "default_format": str,
            "default_speed": float, "default_emotion": str, "model": str,
            "role": str, "unsafe_mode": bool, "sample_rate_hertz": int,
            "volume": float, "pitch_shift": float, "duration_ms": int, "duration_policy": str},
}
ENV_DEFAULTS = {
    "HERMES_TTS_VOICE": ("tts", "default_voice"),
    "HERMES_TTS_LANG": ("tts", "default_lang"),
    "YANDEX_SPEECH_S3_BUCKET": ("object_storage", "bucket"),
    "YANDEX_SPEECH_S3_ENDPOINT": ("object_storage", "endpoint"),
    "YANDEX_SPEECH_S3_PREFIX": ("object_storage", "prefix"),
}


@dataclass(frozen=True)
class RuntimeContext:
    """Internal caller scope; selected_file is supplied only by CLI --config."""
    user_home: Path | None = None
    workspace: Path | None = None
    selected_file: Path | None = None

    @property
    def config_file(self) -> Path:
        if self.selected_file is not None:
            return Path(self.selected_file).expanduser().absolute()
        base = self.workspace or self.user_home or Path.home()
        return Path(base) / ".config" / "yandex-speech" / "config.json"


@dataclass(frozen=True)
class Settings:
    speech: dict
    sources: dict[str, str]
    path: Path

    def value(self, dotted: str, default=None):
        node = self.speech
        for name in dotted.split("."):
            if not isinstance(node, dict) or name not in node:
                return default
            node = node[name]
        return node


def _validate(values, schema=SCHEMA, prefix="speech") -> None:
    if not isinstance(values, dict):
        raise SpeechError("CONFIG", f"{prefix} must be an object")
    for key, value in values.items():
        if key not in schema:
            raise SpeechError("CONFIG", f"unknown configuration field in {prefix}")
        rule = schema[key]
        name = f"{prefix}.{key}"
        if isinstance(rule, dict):
            _validate(value, rule, name)
            continue
        if rule is float:
            valid = type(value) in (int, float) and math.isfinite(value)
            if valid:
                valid = (-145 <= value < 0 if key == "volume" else -1000 <= value <= 1000 if key == "pitch_shift" else 0.5 <= value <= 3.0)
        else:
            valid = type(value) is rule and (rule is not str or bool(value.strip()))
        if not valid:
            raise SpeechError("CONFIG", f"invalid type or value for {name}")
        if key == "transport" and value not in {"auto", "grpc", "rest"}:
            raise SpeechError("CONFIG", f"unsupported {name}")
        if key == "api_version" and value not in ({"auto", "1", "3"} if prefix.endswith(".tts") else {"auto", "1", "2", "3"}):
            raise SpeechError("CONFIG", f"unsupported {name}")
        if key == "sample_rate_hertz" and not 8000 <= value <= 48000:
            raise SpeechError("CONFIG", "sample rate must be in [8000, 48000]")
        if key == "duration_ms" and value < 0:
            raise SpeechError("CONFIG", "duration hint must be nonnegative")
        if key == "duration_policy" and value not in {"min","max","exact"}:
            raise SpeechError("CONFIG", "unsupported duration policy")
        if key == "text_format" and value not in {"plain", "ssml"}:
            raise SpeechError("CONFIG", f"unsupported {name}")
        if name == "speech.default_format" and value not in {"json", "txt", "srt", "vtt"}:
            raise SpeechError("CONFIG", f"unsupported {name}")
        if name == "speech.tts.default_format" and value not in {"mp3", "oggopus", "lpcm", "wav"}:
            raise SpeechError("CONFIG", f"unsupported {name}")
        if key == "profanity_filter" and value:
            raise SpeechError("CONFIG", "profanity masking must remain disabled")


def _merge(target, incoming, sources, label, prefix=""):
    for key, value in incoming.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            _merge(target.setdefault(key, {}), value, sources, label, name)
        else:
            target[key] = value
            sources[name] = label


def resolve_settings(*, _context: RuntimeContext | None = None,
                     _adapter: dict | None = None, _explicit: dict | None = None,
                     _environment: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if _environment is None else _environment
    if _context is None:
        _context = RuntimeContext(user_home=Path(env.get("HOME") or Path.home()),
                                  workspace=Path(env["HERMES_HOME"]) if env.get("HERMES_HOME") else None)
    path = _context.config_file
    file_values = {}
    try:
        if not path.exists():
            if _context.selected_file is not None:
                raise SpeechError("CONFIG", "explicit config file does not exist")
        else:
            if not path.is_file() or not path.stat().st_mode & 0o444:
                raise SpeechError("CONFIG", "config file is unreadable")
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or set(document) - {"speech"}:
                raise SpeechError("CONFIG", "config root must contain only speech")
            file_values = document.get("speech", {})
    except (OSError, UnicodeError, ValueError) as exc:
        raise SpeechError("CONFIG", "cannot read valid user configuration") from None
    aliases = {}
    for name, (group, key) in ENV_DEFAULTS.items():
        if env.get(name):
            aliases.setdefault(group, {})[key] = env[name]
    result, sources = {}, {}
    for values, label in [(DEFAULTS, "builtin"), (aliases, "environment"),
                          (_adapter or {}, "adapter"), (file_values, "config"),
                          (_explicit or {}, "call")]:
        _validate(values)
        _merge(result, copy.deepcopy(values), sources, label)
    return Settings(result, sources, path)
