"""Plan and apply native speech enrollment only in the active Hermes profile."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
import shlex
import string
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
            from hermes_cli.config import fast_safe_load, atomic_config_replace
        except ImportError:
            raise SpeechError("PREREQUISITE", "native enrollment requires the supported Hermes runtime") from None
        self._get_home = get_hermes_home
        self.home = Path(get_hermes_home()).resolve()
        self.path = self.home / "config.yaml"
        self._read = fast_safe_load
        self._write = atomic_config_replace

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

    def write(self, value):
        self._assert_scope()
        try:
            self._write(self.path, value)
        except (OSError, ValueError, RuntimeError):
            raise SpeechError("STATE", "native profile publication failed; inspect the active profile before retrying") from None


def _put_preference(section, name, value):
    if value is None:
        return
    if name in section and section[name] != value:
        raise SpeechError("CONFIG", "legacy command and native speech preferences conflict; no settings changed")
    section[name] = value


def _addressed_placeholder(value, kind):
    allowed = {"input_path", "output_dir", "language"} if kind == "stt" else {"input_path", "output_path"}
    try:
        fields = [(name, spec, conversion) for _, name, spec, conversion
                  in string.Formatter().parse(value) if name is not None]
    except ValueError:
        return False
    return bool(fields) and all(name in allowed and not spec and conversion is None
                                for name, spec, conversion in fields)


def _legacy_defaults(kind, entry, scripts):
    """Recognize canonical commands without executing a shell or expanding values."""
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
    if words and (words[0] in {"python", "python3", "sh", "bash"}
                  or Path(words[0]).resolve() == Path(sys.executable).resolve()):
        words.pop(0)
    if not words:
        raise SpeechError("CONFIG", "legacy Yandex command is empty")
    script = Path(words.pop(0)).expanduser().resolve()
    wrappers = {root / f"hermes-{kind}-wrapper.sh" for root in scripts}
    direct = {root / ("tts.py" if kind == "tts" else "transcribe.py") for root in scripts}
    if script not in wrappers | direct:
        raise SpeechError("CONFIG", "legacy command is not a canonical Yandex script or wrapper")
    if kind == "tts" and entry.get("voice_compatible") is False:
        raise SpeechError("CONFIG", "legacy voice delivery opt-out is incompatible with native enrollment")
    if kind == "stt" and entry.get("language"):
        values["language"] = entry["language"]
    if kind == "tts" and entry.get("output_format"):
        values["output_format"] = entry["output_format"]
    options = {"--lang": "language", "--language": "language",
               "--api-version": "api_version", "--transport": "transport"}
    if kind == "tts":
        options.update({"--voice": "voice", "--emotion": "role", "--role": "role",
                        "--model": "model", "--speed": "speed", "--format": "output_format"})
    ignore = {"--input-file", "--output", "-o", "--output-dir"}
    while words:
        token = words.pop(0)
        if token in options or token in ignore:
            if not words:
                raise SpeechError("CONFIG", "legacy command option is missing its value")
            value = words.pop(0)
            if token in ignore:
                if not _addressed_placeholder(value, kind):
                    raise SpeechError("CONFIG", "legacy input/output is not a recognized addressed placeholder")
                continue
            if "{" in value:
                if value == "{language}" and kind == "stt":
                    continue
                raise SpeechError("CONFIG", "legacy behavioral placeholders require explicit reconciliation")
            target = options[token]
            if target == "speed":
                try:
                    value = float(value)
                except ValueError:
                    raise SpeechError("CONFIG", "legacy speed is invalid") from None
            _put_preference(values, target, value)
        elif token == "--mode":
            if not words or words.pop(0) not in {"auto", "async"}:
                raise SpeechError("CONFIG", "legacy recognition mode cannot be preserved by native enrollment")
        elif token.startswith("-") or not _addressed_placeholder(token, kind):
            raise SpeechError("CONFIG", "legacy command contains unsupported arguments; no settings changed")
    return values


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
                "changed": self.before != self.after or state == "absent"}

    def apply(self):
        report = self.summary()
        if self.backend.read() != self.before:
            raise SpeechError("STATE", "profile configuration changed after native enrollment planning")
        created = None
        if self._link_state() == "absent":
            self.plugin_link.parent.mkdir(parents=True, exist_ok=True)
            self.plugin_link.symlink_to(self.plugin_target, target_is_directory=True)
            info = self.plugin_link.lstat()
            created = info.st_dev, info.st_ino
        try:
            if self.backend.read() != self.before:
                raise SpeechError("STATE", "profile configuration changed before native enrollment publication")
            if self.after != self.before:
                self.backend.write(copy.deepcopy(self.after))
                if self.backend.read() != self.after:
                    raise SpeechError("STATE", "native profile read-back does not match the enrollment plan")
        except Exception:
            try:
                unchanged = self.backend.read() == self.before
            except Exception:
                unchanged = False
            if created is not None and unchanged and self.plugin_link.is_symlink():
                info = self.plugin_link.lstat()
                if (info.st_dev, info.st_ino) == created and self.plugin_link.resolve() == self.plugin_target:
                    self.plugin_link.unlink()
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
