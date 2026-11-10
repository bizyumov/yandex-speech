"""Expose live failures and preserve Hermes's nonraising completion envelopes."""
import contextvars
import logging
import threading
from pathlib import Path

from agent.transcription_provider import TranscriptionProvider, TranscriptionStreamSession
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.grpc_stt import sdk
from yandex_speech.media import AudioInputSpec
from yandex_speech.transcription import SpeechTranscriber

logger = logging.getLogger(__name__)


def profile_secret(workspace, name):
    """Resolve only within the provider's owning native profile."""
    from agent.secret_scope import get_secret_str, current_secret_scope_home
    from hermes_constants import get_hermes_home
    scope=current_secret_scope_home()
    actual=Path(scope) if scope is not None else Path(get_hermes_home())
    if actual.resolve() != Path(workspace).resolve():
        raise SpeechError("AUTH","active host secret/profile scope does not match this provider")
    return get_secret_str(name)


def _envelope(result):
    if not result.get("input_complete"):
        return _failure(SpeechError("OUTPUT_PARTIAL","complete addressed input is required"))
    return {"success":True,"transcript":result.get("text",""),"provider":"yandex",
            "error":None,"speech_result":result}


def _failure(error):
    return {"success":False,"transcript":"","provider":"yandex",
            "error":str(error) if isinstance(error,SpeechError) else "Yandex speech operation failed"}


class YandexStreamSession(TranscriptionStreamSession):
    def __init__(self, core, *, capture=None):
        self.core = core
        self._error = None
        self._result = None
        self._failure_phase = None
        self._cleanup_failed = False
        self._cleanup_inflight = 0
        self._finalizing = False
        self._state_lock = threading.RLock()
        self._finalize_lock = threading.Lock()
        self._capture = capture
        if capture is not None:
            capture.bind_inner(self)

    @property
    def failure(self):
        with self._state_lock:
            self._sync_core_failure()
            return self._error

    def _refresh_cached_failure(self):
        if self._result is None or self._result.get("success"):
            return
        self._sync_core_failure()
        self._result["error"] = str(self._error)
        metadata = self._result.setdefault("stream_failure", {})
        metadata.update({
            "phase": self._failure_phase, "category": self._error.category,
            "code": self._error.code,
            "cleanup_failed": self._cleanup_failed or getattr(self.core, "cleanup_failed", False) is True,
            "cleanup_pending": self._cleanup_inflight > 0 or getattr(self.core, "cleanup_pending", False) is True,
        })

    def _abort(self):
        with self._state_lock:
            self._cleanup_inflight += 1
            self._refresh_cached_failure()
        try:
            self.core.cancel()
        except Exception:
            with self._state_lock:
                self._cleanup_failed = True
                if self._error is None:
                    self._error = SpeechError("STATE", "owned stream cleanup failed")
                    self._failure_phase = "cleanup"
        finally:
            with self._state_lock:
                self._cleanup_inflight -= 1
                self._refresh_cached_failure()

    def _remember_failure(self, error, phase):
        core_failure = getattr(self.core, "failure", None)
        failure = core_failure if isinstance(core_failure, SpeechError) else error
        failure = failure if isinstance(failure, SpeechError) else SpeechError(
            "PROVIDER", "Yandex speech operation failed")
        with self._state_lock:
            first = self._error is None
            if first:
                self._error = failure
                self._failure_phase = phase
            original = self._error
        if first:
            logger.warning("Native Yandex stream failed: phase=%s category=%s code=%s",
                           phase, original.category, original.code)
        self._abort()
        return original

    def push_audio(self, chunk):
        with self._state_lock:
            if self._error is not None:
                raise self._error
            if self._finalizing or self._result is not None:
                raise SpeechError("STATE", "cannot feed a finalized native session")
        try:
            self.core.push_audio(chunk)
        except Exception as error:
            raise self._remember_failure(error, "push") from None
        with self._state_lock:
            if self._error is not None:
                raise self._error
        if self._capture is not None:
            try:
                self._capture.acknowledge(len(chunk))
            except Exception as error:
                raise self._remember_failure(error, "acknowledge") from None

    def partial_transcript(self):
        with self._state_lock:
            if self._error is not None:
                raise self._error
            if self._result is not None:
                return ""
        try:
            preview = self.core.partial_transcript()
        except Exception as error:
            raise self._remember_failure(error, "preview") from None
        with self._state_lock:
            if self._error is not None:
                raise self._error
        return preview

    def finalize(self):
        with self._finalize_lock:
            with self._state_lock:
                if self._result is not None:
                    self._refresh_cached_failure()
                    return self._result
                self._finalizing = True
                failed = self._error is not None
            candidate = None
            if not failed:
                try:
                    candidate = _envelope(self.core.finalize())
                    if not candidate["success"]:
                        self._remember_failure(SpeechError(
                            "OUTPUT_PARTIAL", "complete addressed input is required"), "finalize")
                except Exception as error:
                    self._remember_failure(error, "finalize")
            with self._state_lock:
                self._sync_core_failure()
                if self._error is not None:
                    candidate = _failure(self._error)
                self._result = candidate
                self._refresh_cached_failure()
                self._finalizing = False
                return self._result

    def _sync_core_failure(self):
        failure = getattr(self.core, "failure", None)
        if isinstance(failure, SpeechError) and (
                self._error is None or
                self._error.category == "CANCELLED" and failure.category != "CANCELLED"):
            self._error = failure
            self._failure_phase = "core"
            return True
        return False

    def cancel(self):
        with self._state_lock:
            if self._result is not None:
                return
            adopted = self._sync_core_failure()
            if self._error is None:
                self._error = SpeechError("CANCELLED", "local host utterance cancelled")
                self._failure_phase = "cancel"
            failure = self._error
        if adopted:
            logger.warning("Native Yandex stream failed: phase=core category=%s code=%s",
                           failure.category, failure.code)
        self._abort()
        with self._state_lock:
            self._sync_core_failure()


class YandexTranscriptionProvider(TranscriptionProvider):
    name="yandex"

    def __init__(self,workspace,profile=None,*,_secret_resolver=None,
                 _client_factory=SpeechTranscriber):
        self.workspace=Path(workspace)
        self.profile=dict(profile or {})
        self._profile_secrets = _secret_resolver is None
        self._secret_resolver=_secret_resolver or self._scoped_secret
        self._client_factory=_client_factory
        self._discord_ingress = None
        self._opening_capture = contextvars.ContextVar("yandex_live_capture", default=None)

    def _scoped_secret(self):
        return profile_secret(self.workspace, "YANDEX_SPEECH_API_KEY")

    def _client(self,language=None):
        key=self._secret_resolver()
        if not key:
            raise SpeechError("AUTH","YANDEX_SPEECH_API_KEY is missing in the active host profile")
        adapter={}
        aliases={"language":"language_code","diarize":"enable_diarization",
                 "timestamps":"enable_timestamps"}
        allowed={"language_code","enable_diarization","enable_timestamps",
                 "literature_text","api_version","transport","profanity_filter"}
        for key_name,value in self.profile.items():
            target=aliases.get(key_name,key_name)
            if target in allowed:
                adapter[target]=value
        if language:
            adapter["language_code"]=language
        names = ("YANDEX_CLOUD_FOLDER_ID", "YANDEX_SPEECH_S3_BUCKET",
                 "YANDEX_SPEECH_S3_PREFIX", "YANDEX_SPEECH_AWS_ACCESS_KEY_ID",
                 "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY")
        environment = ({name: value for name in names if (value := profile_secret(self.workspace, name))}
                       if self._profile_secrets else {})
        return self._client_factory(api_key=key, _context=RuntimeContext(workspace=self.workspace),
                                    _adapter_defaults=adapter, _environment=environment)

    def is_available(self):
        try:
            self._client()._file_preflight()
            import requests
            return True
        except Exception:
            return False

    def list_models(self):
        return [{"id":"general","display":"Yandex SpeechKit general"}]

    @property
    def streaming_unavailable_reason(self):
        try:
            client=self._client()
            if client.settings.value("transport") == "rest" or client.settings.value("api_version") not in {"auto","3"}:
                return "recognition during arrival requires gRPC v3"
            if client.settings.value("enable_diarization"):
                return "speaker labeling requires the recorded-file method"
            sdk()
            return ""
        except SpeechError as error:
            return str(error)
        except Exception:
            return "host profile credentials or optional gRPC prerequisites are unavailable"

    @property
    def streaming_capable(self):
        return not self.streaming_unavailable_reason

    def transcribe(self, file_path, *, model=None, language=None, **extra):
        try:
            if model not in {None, "general"}:
                raise SpeechError("CONFIG", "unsupported Yandex recognition model selection")
            ingress = self._discord_ingress
            if ingress is not None:
                deferred = ingress.file_policy.decide(file_path)
                if deferred is not None:
                    ingress.note_file_deferred()
                    return deferred
                ingress.note_recorded_asr()
            result = self._client(language).transcribe_auto(file_path)
            return _envelope(result)
        except Exception as error:
            return _failure(error)

    def open_stream_session(self,*,language=None,prompt=None):
        if not self.streaming_capable:
            raise SpeechError("PREREQUISITE",self.streaming_unavailable_reason)
        core=self._client(language).open_stream(
            audio_input=AudioInputSpec("lpcm","s16le",16000,1))
        return YandexStreamSession(core, capture=self._opening_capture.get())
