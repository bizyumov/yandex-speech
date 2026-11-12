"""Native Hermes file/PCM synthesis with per-call profile-bound clients."""
from pathlib import Path
import json
import logging
import time
import shutil
import stat
import tempfile
import uuid

from agent.tts_provider import TTSProvider
from yandex_speech import TextToSpeech
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.tts_voices import CATALOG
from .provider import profile_secret


class YandexTTSProvider(TTSProvider):
    name = "yandex"
    stream_sample_rate = 24000
    voice_compatible = True

    def __init__(self, workspace, profile=None, *, _secret_resolver=None,
                 _client_factory=TextToSpeech):
        self.workspace = Path(workspace)
        self.profile = dict(profile or {})
        self._secret_resolver = _secret_resolver or (
            lambda name: profile_secret(self.workspace, name))
        self._client_factory = _client_factory

    def _client(self, *, voice=None, model=None, speed=None):
        key = self._secret_resolver("YANDEX_SPEECH_API_KEY")
        if not key:
            raise SpeechError("AUTH", "YANDEX_SPEECH_API_KEY is missing in the active host profile")
        native = self.profile.get("yandex") or {}
        adapter = {}
        for source, target in (("language", "default_lang"), ("role", "default_emotion"),
                               ("api_version", "api_version"), ("transport", "transport"),
                               ("file_method", "file_method"), ("unsafe_mode", "unsafe_mode")):
            if native.get(source) is not None:
                adapter[target] = native[source]
        for source, target, hint in (("voice", "default_voice", voice),
                                     ("model", "model", model),
                                     ("speed", "default_speed", speed),
                                     ("output_format", "default_format", None)):
            value = hint if hint is not None else self.profile.get(source)
            if value is not None:
                adapter[target] = {"ogg": "oggopus", "opus": "oggopus"}.get(value, value)
        environment = {name: value for name in ("HERMES_TTS_VOICE", "HERMES_TTS_LANG")
                       if (value := self._secret_resolver(name))}
        return self._client_factory(api_key=key,
            _context=RuntimeContext(workspace=self.workspace),
            _adapter_defaults={"tts": adapter}, _environment=environment)

    def _preflight(self, *, pcm=False):
        client = self._client()
        settings, options, version = client._options()
        if options["text_format"] != "plain":
            raise SpeechError("CONFIG", "native replies require plain text; use the explicit skill SSML operation")
        if pcm:
            if (options["api_version"] == "1" or options["transport"] == "rest"
                    or options.get("file_method") == "utterance"
                    or options.get("unsafe_mode") or options.get("duration_ms")
                    or "3" not in options["_voice_info"]["versions"]):
                raise SpeechError("CONFIG", "effective settings do not support native PCM streaming")
            options = dict(options, default_format="lpcm", sample_rate_hertz=24000)
            version = "3"
        if pcm:
            from yandex_speech.tts_capabilities import probe_runtime
            capability = probe_runtime(dict(options, model=options["_model"], role=options["_role"])).stream
            if not capability.ready:
                raise SpeechError("PREREQUISITE", capability.reason)
        elif options["file_method"] != "auto":
            from yandex_speech.tts_selection import plan_synthesis
            from yandex_speech.tts_stream_file import stream_contract
            plan = plan_synthesis("Native availability check.", options, stream_contract=stream_contract(options),
                sources=settings.sources, config_path=settings.path, profile_scope=self.workspace)
            version = plan.api_version
        if version == "3":
            from yandex_speech.tts_v3 import request_body
            request_body("Native availability check.", options, sources=settings.sources)
        else:
            if options.get("unsafe_mode"):
                raise SpeechError("CONFIG", "unsafe mode requires API v3")
            from yandex_speech.tts_text import v1_form
            v1_form("Native availability check.", options)
        return options, version

    def is_available(self):
        try:
            options, _ = self._preflight()
            if options["transport"] == "grpc":
                from yandex_speech.tts_v3 import grpc_sdk
                grpc_sdk()
            return True
        except Exception:
            return False

    def list_models(self):
        return [{"id": "general", "display": "Yandex general"},
                {"id": "general:rc", "display": "Yandex general:rc"},
                {"id": "general:deprecated", "display": "Yandex general:deprecated"},
                {"id": "livetts", "display": "Yandex LiveTTS"}]

    def list_voices(self):
        return [{"id": name, "display": name,
                 "language": info["language"], "model": info["model"]}
                for name, info in CATALOG.items()]

    def default_voice(self):
        return self._client()._options()[1]["default_voice"]

    def default_model(self):
        return self._client()._options()[1]["_model"]

    @property
    def streams_pcm(self):
        try:
            self._preflight(pcm=True)
            from yandex_speech.tts_v3 import grpc_sdk
            grpc_sdk()
            return True
        except Exception:
            return False

    @staticmethod
    def _empty_allocation(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size:
            raise SpeechError("CONFIG", "native TTS output must be absent or an owned empty regular allocation")
        return info.st_dev, info.st_ino

    def synthesize(self, text, output_path, *, voice=None, model=None, speed=None,
                   format="mp3", **extra):
        from yandex_speech.artifacts import atomic_write
        started = time.monotonic()
        requested = Path(output_path)
        self._empty_allocation(requested)
        fmt = {"ogg": "oggopus", "opus": "oggopus"}.get(format, format)
        if fmt not in {"mp3", "wav", "oggopus"}:
            fmt = "wav"
        extension = {"mp3": ".mp3", "wav": ".wav", "oggopus": ".ogg"}[fmt]
        target = requested.with_suffix(extension)
        before = self._empty_allocation(target)
        client = self._client(voice=voice, model=model, speed=speed)
        stage = Path(tempfile.mkdtemp(prefix=".yandex-native-tts-", dir=target.parent))
        retain = False
        staged_output = stage / ("audio" + extension)
        try:
            client.synthesize(text, staged_output, format=fmt)
            if self._empty_allocation(target) != before:
                raise SpeechError("STATE", "native TTS output allocation changed during synthesis")
            # A preallocated inode can acquire another writer after any check.
            # The native SPI consumes the returned path, so preserve that inode
            # and publish a unique sibling instead of ever enabling overwrite.
            output = target if before is None else target.with_name(
                target.stem + "-" + uuid.uuid4().hex + extension)
            atomic_write(output, staged_output.read_bytes())
            metadata = getattr(client, "last_synthesis", {})
            receipt = {name: metadata[name] for name in (
                "api_version", "transport", "method", "voice", "model", "role", "format", "parts",
                "planned_parts", "duration_splits", "assembly", "stream_receipts", "output_budget_bytes")
                if name in metadata}
            receipt.update(output=str(output), input_characters=len(text),
                           elapsed_seconds=time.monotonic()-started)
            receipt.setdefault("method", "utterance")
            logging.getLogger(__name__).info("Yandex native TTS receipt %s", json.dumps(receipt))
            return str(output)
        except Exception as error:
            retain = getattr(error, "partial_dir", None) is not None or staged_output.is_file()
            if retain and getattr(error, "partial_dir", None) is None:
                error.partial_dir = stage
            raise
        finally:
            if not retain:
                shutil.rmtree(stage)

    def stream(self, text, *, voice=None, model=None, format="opus", **extra):
        if format != "pcm":
            raise NotImplementedError("native Yandex streaming provides 24 kHz mono s16le PCM")
        client = self._client(voice=voice, model=model, speed=extra.get("speed"))
        return client.stream_audio(text, format="lpcm", sample_rate=24000)
