"""Apply a saved Yandex credential to a selected env file, without cloud access."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re

from .errors import SpeechError

ENV_SECTION = "# Yandex Search API keys ([https://github.com/bizyumov/yandex-speech/](https://github.com/bizyumov/yandex-speech/))"


def _credential_variables(response):
    if not isinstance(response, dict):
        raise SpeechError("CONFIG", "saved key response must be a JSON object")
    secret = response.get("secret")
    api = isinstance(response.get("apiKey"), dict)
    storage = isinstance(response.get("accessKey"), dict)
    if api == storage or not isinstance(secret, str) or not secret.strip():
        raise SpeechError("CONFIG", "saved response must contain one API or storage key and its secret")
    values = ({"YANDEX_SPEECH_API_KEY": secret} if api else {
        "YANDEX_SPEECH_AWS_ACCESS_KEY_ID": response["accessKey"].get("keyId"),
        "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY": secret,
    })
    if any(not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\0")
           for value in values.values()):
        raise SpeechError("CONFIG", "saved credential fields must be nonempty single-line strings")
    return values


def _env_text(original, values):
    lines = original.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in original else "\n"
    assignment = re.compile(r"^[ \t]*(?:export[ \t]+)?(" + "|".join(values) + r")[ \t]*=")
    lines = [line for line in lines if not assignment.match(line)]
    block = [name + "=" + json.dumps(value, ensure_ascii=False) + newline
             for name, value in values.items()]
    section = next((i for i, line in enumerate(lines) if line.strip() == ENV_SECTION), None)
    if section is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.extend([ENV_SECTION + newline, *block])
    else:
        if not lines[section].endswith(("\n", "\r")):
            lines[section] += newline
        lines[section + 1:section + 1] = block
    return "".join(lines)


def apply_key(secret_file, env_file):
    source = Path(secret_file).expanduser().absolute()
    target = Path(env_file).expanduser().absolute()
    try:
        if target.exists() and source.samefile(target):
            raise SpeechError("CONFIG", "secret response and env destination must be different files")
        with source.open(encoding="utf-8") as stream:
            response = json.load(stream)
    except (OSError, ValueError):
        raise SpeechError("CONFIG", "could not read a saved key JSON response") from None
    values = _credential_variables(response)
    try:
        fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o666)
        with os.fdopen(fd, "r+", encoding="utf-8", newline="") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            original = stream.read()
            updated = _env_text(original, values)
            if updated != original:
                stream.seek(0)
                stream.write(updated)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, UnicodeError):
        raise SpeechError("STATE", "could not update the selected env file") from None
    return {
        "status": "applied",
        "secret_file": str(source),
        "env_file": str(target),
        "updated_variables": list(values),
        "gateway_environment_updated": False,
        "next_action": "Gateway variables are loaded at startup. Until its next start, explicitly pass the new variables to speech-generation child processes; writing this file does not update the running gateway.",
    }
