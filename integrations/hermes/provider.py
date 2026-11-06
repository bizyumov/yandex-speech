"""Adapt the core completion contract to Hermes's nonraising STT envelopes."""
from pathlib import Path

from agent.transcription_provider import TranscriptionProvider, TranscriptionStreamSession
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.grpc_stt import sdk
from yandex_speech.media import AudioInputSpec
from yandex_speech.transcription import SpeechTranscriber


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
    def __init__(self,core):
        self.core=core
        self._error=None
        self._result=None

    def _abort(self):
        try:
            self.core.cancel()
        except Exception:
            self._error=SpeechError("STATE","owned stream cleanup failed")

    def push_audio(self,chunk):
        if self._error is not None:
            return
        try:
            self.core.push_audio(chunk)
        except Exception as error:
            self._error=error
            self._abort()

    def partial_transcript(self):
        return "" if self._error is not None else self.core.partial_transcript()

    def finalize(self):
        if self._result is None:
            try:
                if self._error is not None:
                    self._result=_failure(self._error)
                else:
                    self._result=_envelope(self.core.finalize())
            except Exception as error:
                self._result=_failure(error)
                self._abort()
        return self._result

    def cancel(self):
        self._abort()
        self._error=SpeechError("CANCELLED","local host utterance cancelled")


class YandexTranscriptionProvider(TranscriptionProvider):
    name="yandex"

    def __init__(self,workspace,profile=None,*,_secret_resolver=None,
                 _client_factory=SpeechTranscriber):
        self.workspace=Path(workspace)
        self.profile=dict(profile or {})
        self._secret_resolver=_secret_resolver or self._scoped_secret
        self._client_factory=_client_factory

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
        return self._client_factory(api_key=key,_context=RuntimeContext(workspace=self.workspace),
                                    _adapter_defaults=adapter)

    def is_available(self):
        try:
            self._client()
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

    def transcribe(self,file_path,*,model=None,language=None,**extra):
        try:
            if model not in {None,"general"}:
                raise SpeechError("CONFIG","unsupported Yandex recognition model selection")
            client=self._client(language)
            if client.settings.value("transport") == "rest" or client.settings.value("api_version") in {"1","2"}:
                if client.settings.value("api_version") == "2":
                    result=client.transcribe_async_s3(file_path)
                else:
                    result=client._legacy_call("transcribe_auto",file_path)
            else:
                try:
                    sdk()
                except SpeechError:
                    if client.settings.value("api_version") == "3":
                        raise
                    result=client._legacy_call("transcribe_auto",file_path)
                else:
                    result=client.transcribe_auto(file_path)
            return _envelope(result)
        except Exception as error:
            return _failure(error)

    def open_stream_session(self,*,language=None,prompt=None):
        if not self.streaming_capable:
            raise SpeechError("PREREQUISITE",self.streaming_unavailable_reason)
        core=self._client(language).open_stream(
            audio_input=AudioInputSpec("lpcm","s16le",16000,1))
        return YandexStreamSession(core)
