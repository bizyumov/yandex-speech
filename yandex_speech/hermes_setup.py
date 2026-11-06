"""Plan and apply native speech enrollment only in the active Hermes profile."""
from __future__ import annotations

import copy
import inspect
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shlex
import sys
import sysconfig

from .errors import SpeechError


def _plugin_root():
    root = Path(__file__).resolve().parents[1]
    candidates = [root / "integrations/hermes",
                  Path(sysconfig.get_path("data")) / "share/yandex-speech/integrations/hermes"]
    required = ("__init__.py", "provider.py", "tts_provider.py", "plugin.yaml")
    for candidate in candidates:
        if all((candidate / name).is_file() for name in required):
            return candidate.resolve()
    raise SpeechError("PREREQUISITE", "install the complete native Yandex speech plugin payload")


class _NativeBackend:
    def __init__(self):
        try:
            from hermes_constants import get_hermes_home
            from utils import fast_safe_load
            from hermes_cli.plugins_discovery import suppress_plugin_discovery
            with suppress_plugin_discovery():
                from hermes_cli.config import atomic_config_replace
        except ImportError:
            raise SpeechError("PREREQUISITE", "native enrollment requires the supported Hermes runtime") from None
        self._get_home = get_hermes_home
        self.home = Path(get_hermes_home()).resolve()
        self.path = self.home / "config.yaml"
        self._read = fast_safe_load
        self._write = atomic_config_replace
        self.supports_expected_state = "expected_state" in inspect.signature(atomic_config_replace).parameters

    def _assert_scope(self):
        if Path(self._get_home()).resolve() != self.home:
            raise SpeechError("AUTH", "active Hermes profile changed during native enrollment")

    def read(self):
        self._assert_scope()
        if self.path.is_symlink() or self.path.exists() and not self.path.is_file():
            raise SpeechError("CONFIG", "active profile configuration must be a regular file or absent")
        if not self.path.exists():
            return {}
        try:
            with self.path.open(encoding="utf-8-sig") as stream:
                value = self._read(stream)
        except Exception:
            raise SpeechError("CONFIG", "cannot retrieve readable active profile configuration; no enrollment changes made") from None
        if value is None:
            return {}
        if not isinstance(value, dict) or any(not isinstance(name, str) for name in value):
            raise SpeechError("CONFIG", "active profile configuration must be a mapping with setting names")
        return copy.deepcopy(value)

    def write(self, value, *, expected_state=None):
        self._assert_scope()
        if expected_state is not None and not self.supports_expected_state:
            raise SpeechError("PREREQUISITE", "install the tested native configuration safety update before applying enrollment")
        try:
            from hermes_cli.plugins_discovery import suppress_plugin_discovery
            with suppress_plugin_discovery():
                if expected_state is None:
                    self._write(self.path, value)
                else:
                    self._write(self.path, value, expected_state=expected_state)
        except (OSError, ValueError, RuntimeError):
            raise SpeechError("STATE", "native profile publication failed; inspect the active profile before retrying") from None


def _put_preference(section, name, value):
    if value is None:
        return
    if name in section and section[name] != value:
        raise SpeechError("CONFIG", "legacy command and native speech preferences conflict; no settings changed")
    section[name] = value


def _migration_value(values, name, value):
    if name == "output_format":
        value = {"oggopus": "ogg", "opus": "ogg"}.get(value, value)
    if name == "speed":
        try:
            value = float(value)
        except ValueError:
            raise SpeechError("CONFIG", "legacy speed is invalid") from None
    _put_preference(values, name, value)


def _direct_defaults(kind, words, values):
    options = {"--lang": "language", "--language": "language",
               "--api-version": "api_version", "--transport": "transport"}
    if kind == "tts":
        options.update({"--voice": "voice", "--adapter-voice": "voice",
                        "--emotion": "role", "--adapter-emotion": "role", "--role": "role",
                        "--model": "model", "--speed": "speed", "--format": "output_format"})
    inputs = outputs = 0
    while words:
        token = words.pop(0)
        if token == "{input_path}" and kind == "stt":
            inputs += 1
            continue
        if token not in options and token not in {"--input-file", "--output", "-o", "--output-dir",
                                                  "--mode", "--format"}:
            raise SpeechError("CONFIG", "legacy command contains unsupported arguments")
        if not words:
            raise SpeechError("CONFIG", "legacy command option is missing its value")
        value = words.pop(0)
        if token == "--input-file":
            if kind != "tts" or value != "{input_path}":
                raise SpeechError("CONFIG", "legacy input must be the exact addressed input")
            inputs += 1
        elif token in {"--output", "-o", "--output-dir"}:
            allowed = {"{output_path}"} if kind == "tts" else {
                "{output_dir}", "{output_dir}/transcript.txt"}
            if value not in allowed or kind == "tts" and token == "--output-dir":
                raise SpeechError("CONFIG", "legacy output does not match the native addressed contract")
            outputs += 1
        elif token == "--mode":
            if kind != "stt" or value not in {"auto", "async"}:
                raise SpeechError("CONFIG", "legacy recognition mode cannot be preserved")
        elif token == "--format" and kind == "stt":
            if value != "txt":
                raise SpeechError("CONFIG", "native message transcription cannot preserve this export selection")
        elif value == "{language}" and kind == "stt" and token in {"--lang", "--language"}:
            continue
        elif "{" in value or "}" in value:
            raise SpeechError("CONFIG", "legacy behavioral placeholders require reconciliation")
        else:
            _migration_value(values, options[token], value)
    if inputs != 1 or outputs != 1:
        raise SpeechError("CONFIG", "canonical file commands require one exact input and output")
    return values


def _legacy_defaults(kind, entry, scripts):
    """Recognize complete canonical command shapes without executing a shell."""
    if not isinstance(entry, dict) or entry.get("type") != "command":
        raise SpeechError("CONFIG", "same-name Yandex provider is not a recognized command entry")
    command = entry.get("command")
    if not isinstance(command, str):
        raise SpeechError("CONFIG", "legacy Yandex command must be a recognized string")
    try:
        words = shlex.split(command)
    except ValueError:
        raise SpeechError("CONFIG", "legacy Yandex command cannot be parsed safely") from None
    values = {}
    if words and words[0] == "env":
        words.pop(0)
    aliases = {"HERMES_TTS_VOICE": "voice", "HERMES_TTS_LANG": "language"}
    while words and "=" in words[0]:
        name, value = words.pop(0).split("=", 1)
        if kind != "tts" or name not in aliases or not value or any(c in value for c in "$\x60"):
            raise SpeechError("CONFIG", "legacy Yandex command has unsupported environment prefixes")
        values[aliases[name]] = value
    runner = None
    if words and (words[0] in {"python", "python3", "sh", "bash"}
                  or Path(words[0]).resolve() == Path(sys.executable).resolve()):
        runner = words.pop(0)
    if not words:
        raise SpeechError("CONFIG", "legacy Yandex command is empty")
    script = Path(words.pop(0)).expanduser().resolve()
    wrappers = {root / f"hermes-{kind}-wrapper.sh" for root in scripts}
    direct = {root / ("tts.py" if kind == "tts" else "transcribe.py") for root in scripts}
    if script not in wrappers | direct:
        raise SpeechError("CONFIG", "legacy command is not a canonical Yandex script or wrapper")
    if runner is not None and ((script in wrappers) != (runner in {"sh", "bash"})):
        raise SpeechError("CONFIG", "legacy interpreter does not match its canonical script")
    if kind == "tts" and entry.get("voice_compatible") is False:
        raise SpeechError("CONFIG", "legacy voice delivery opt-out is incompatible with native enrollment")
    if kind == "stt" and entry.get("language"):
        values["language"] = entry["language"]
    if kind == "tts" and entry.get("output_format"):
        _migration_value(values, "output_format", entry["output_format"])
    if script in wrappers:
        expected = ["{input_path}", "{output_path}"] if kind == "tts" else [
            "{input_path}", "{output_dir}", "{language}"]
        if kind == "stt" and words == expected + ["&&", "cat", "{output_dir}/transcript.txt"]:
            words = words[:3]
        if words != expected:
            raise SpeechError("CONFIG", "legacy wrapper arguments do not match its exact addressed contract")
        return values
    return _direct_defaults(kind, words, values)


def _public_changes(before, after, prefix=""):
    changes = []
    for name in sorted(before.keys() | after.keys()):
        path = prefix + name
        old, new = before.get(name), after.get(name)
        if old == new and (name in before) == (name in after):
            continue
        if path in {"stt.providers.yandex", "tts.providers.yandex"}:
            changes.append({"path": path, "action": "remove-recognized-command"})
        elif isinstance(old, dict) and isinstance(new, dict):
            changes.extend(_public_changes(old, new, path + "."))
        else:
            changes.append({"path": path, "before": old, "after": new})
    return changes


def _reject_changed_aliases(before, after):
    """The native round-trip writer retains shared YAML container identities."""
    references = {}
    active = set()

    def visit(value, path):
        if not isinstance(value, (dict, list)):
            return
        identity = id(value)
        if identity in active:
            raise SpeechError("CONFIG", "cyclic profile aliases cannot be safely enrolled")
        if identity in references:
            references[identity].append(path)
            return
        references[identity] = [path]
        active.add(identity)
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for name, child in items:
            visit(child, path + ("." if path else "") + str(name))
        active.remove(identity)

    visit(before, "")
    changes = [item["path"] for item in _public_changes(before, after)]
    for paths in references.values():
        if len(paths) < 2:
            continue
        if any(change == path or change.startswith(path + ".")
               or path.startswith(change + ".") for path in paths for change in changes):
            raise SpeechError("CONFIG", "changed native settings share YAML aliases; separate those aliases before enrollment")


@contextmanager
def _plugin_directory(home):
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW") or os.symlink not in os.supports_dir_fd:
        raise SpeechError("PREREQUISITE", "safe native enrollment requires directory-relative filesystem operations")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    home_fd = os.open(home, flags)
    plugin_fd = None
    try:
        try:
            os.mkdir("plugins", mode=0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        plugin_fd = os.open("plugins", flags, dir_fd=home_fd)

        def verify():
            current = os.stat("plugins", dir_fd=home_fd, follow_symlinks=False)
            opened = os.fstat(plugin_fd)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise SpeechError("STATE", "profile plugins directory changed during enrollment")
            live_home = Path(home).lstat()
            opened_home = os.fstat(home_fd)
            if (live_home.st_dev, live_home.st_ino) != (opened_home.st_dev, opened_home.st_ino):
                raise SpeechError("STATE", "active profile directory changed during enrollment")

        verify()
        yield plugin_fd, verify
    except OSError:
        raise SpeechError("CONFIG", "cannot safely open the active profile plugins directory") from None
    finally:
        if plugin_fd is not None:
            os.close(plugin_fd)
        os.close(home_fd)


@dataclass
class NativePlan:
    backend: object
    before: dict
    after: dict
    plugin_target: Path
    checks: dict

    @property
    def plugin_link(self):
        return self.backend.home / "plugins/yandex-speech"

    def _link_state(self):
        link = self.plugin_link
        parent = link.parent
        if parent.is_symlink() or parent.exists() and not parent.is_dir():
            raise SpeechError("CONFIG", "profile plugins directory must be a local directory")
        if link.is_symlink():
            if link.resolve() != self.plugin_target:
                raise SpeechError("CONFIG", "existing Yandex plugin link points to a different installation")
            return "present"
        if link.exists():
            raise SpeechError("CONFIG", "existing Yandex plugin entry requires reconciliation before enrollment")
        return "absent"

    def summary(self, *, applied=False):
        state = self._link_state()
        return {"status": "applied" if applied else "planned",
                "profile_home": str(self.backend.home),
                "config_file": str(self.backend.path),
                "changes": _public_changes(self.before, self.after),
                "plugin": {"path": str(self.plugin_link), "source": str(self.plugin_target),
                           "action": "create-link" if state == "absent" else "keep-link"},
                "availability": self.checks,
                "safe_apply_available": bool(getattr(self.backend, "supports_expected_state", False)),
                "changed": self.before != self.after or state == "absent"}

    def apply(self):
        report = self.summary()
        if not getattr(self.backend, "supports_expected_state", False):
            raise SpeechError("PREREQUISITE", "install the tested native configuration safety update before applying enrollment")
        if self.backend.read() != self.before:
            raise SpeechError("STATE", "profile configuration changed after native enrollment planning")
        self._link_state()
        created = None
        with _plugin_directory(self.backend.home) as (directory, verify):
            try:
                try:
                    os.symlink(str(self.plugin_target), "yandex-speech", dir_fd=directory)
                    info = os.stat("yandex-speech", dir_fd=directory, follow_symlinks=False)
                    created = info.st_dev, info.st_ino
                except FileExistsError:
                    info = os.stat("yandex-speech", dir_fd=directory, follow_symlinks=False)
                    target = os.stat("yandex-speech", dir_fd=directory)
                    expected = self.plugin_target.stat()
                    if not stat.S_ISLNK(info.st_mode) or (target.st_dev, target.st_ino) != (expected.st_dev, expected.st_ino):
                        raise SpeechError("CONFIG", "existing plugin entry changed before enrollment")
                verify()
                if self.backend.read() != self.before:
                    raise SpeechError("STATE", "profile configuration changed before native enrollment publication")
                if self.after != self.before:
                    self.backend.write(copy.deepcopy(self.after), expected_state=self.before)
                    if self.backend.read() != self.after:
                        raise SpeechError("STATE", "native profile read-back does not match the enrollment plan")
                verify()
            except Exception:
                try:
                    unchanged = self.backend.read() == self.before
                except Exception:
                    unchanged = False
                if created is not None and unchanged:
                    try:
                        info = os.stat("yandex-speech", dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        info = None
                    if info is not None and (info.st_dev, info.st_ino) == created:
                        os.unlink("yandex-speech", dir_fd=directory)
                raise
        report["status"] = "applied"
        return report


def plan_native(*, enable_stt_streaming=False, enable_tts_pcm=False,
                _backend=None, _providers=None, _plugin=None):
    backend = _backend or _NativeBackend()
    before = backend.read()
    after = copy.deepcopy(before)
    plugin = Path(_plugin or _plugin_root()).resolve()
    scripts = {Path(__file__).resolve().parents[1] / "scripts", plugin.parents[1] / "scripts"}
    for kind in ("stt", "tts"):
        section = copy.deepcopy(after.get(kind, {}))
        after[kind] = section
        if not isinstance(section, dict):
            raise SpeechError("CONFIG", "native speech configuration must be a mapping")
        providers = copy.deepcopy(section.get("providers") or {})
        if "providers" in section:
            section["providers"] = providers
        if not isinstance(providers, dict):
            raise SpeechError("CONFIG", "native speech providers must be a mapping")
        entry = providers.get("yandex")
        if entry is not None:
            values = _legacy_defaults(kind, entry, scripts)
            specific = copy.deepcopy(section.get("yandex", {}))
            section["yandex"] = specific
            if not isinstance(specific, dict):
                raise SpeechError("CONFIG", "native Yandex preferences must be a mapping")
            for name, value in values.items():
                destination = section if kind == "tts" and name in {
                    "voice", "model", "speed", "output_format"} else specific
                _put_preference(destination, name, value)
            del providers["yandex"]
        section["provider"] = "yandex"
    plugins = copy.deepcopy(after.get("plugins", {}))
    after["plugins"] = plugins
    if not isinstance(plugins, dict):
        raise SpeechError("CONFIG", "native plugin settings must be a mapping")
    for name in ("enabled", "disabled"):
        if name in plugins and not isinstance(plugins[name], list):
            raise SpeechError("CONFIG", "native plugin selections must be lists")
    enabled = list(plugins.get("enabled", []))
    plugins["enabled"] = enabled
    if "yandex-speech" not in enabled:
        enabled.append("yandex-speech")
    if "disabled" in plugins:
        plugins["disabled"] = [name for name in plugins["disabled"] if name != "yandex-speech"]
    if _providers is None:
        from integrations.hermes.provider import YandexTranscriptionProvider
        from integrations.hermes.tts_provider import YandexTTSProvider
        _providers = (YandexTranscriptionProvider, YandexTTSProvider)
    stt = _providers[0](backend.home, profile=after["stt"].get("yandex") or {})
    tts = _providers[1](backend.home, profile=after["tts"])
    if not stt.is_available() or not tts.is_available():
        raise SpeechError("PREREQUISITE", "native Yandex providers are unavailable; check this profile's key, preferences and dependencies")
    checks = {"local_only": True, "execution_verified": False,
              "stt_file": True, "tts_file": True,
              "stt_streaming": bool(stt.streaming_capable), "tts_pcm": bool(tts.streams_pcm)}
    if enable_stt_streaming or after["stt"].get("streaming"):
        if not checks["stt_streaming"]:
            raise SpeechError("PREREQUISITE", "native STT streaming is unavailable for the effective settings")
    if enable_stt_streaming:
        after["stt"]["streaming"] = True
    streaming = after["tts"].get("streaming") or {}
    if not isinstance(streaming, dict):
        raise SpeechError("CONFIG", "native TTS streaming settings must be a mapping")
    if enable_tts_pcm or streaming.get("provider") == "yandex":
        if not checks["tts_pcm"]:
            raise SpeechError("PREREQUISITE", "native TTS PCM is unavailable for the effective settings")
    if enable_tts_pcm:
        after["tts"].setdefault("streaming", {})["provider"] = "yandex"
    _reject_changed_aliases(before, after)
    plan = NativePlan(backend, before, after, plugin, checks)
    plan._link_state()
    return plan
