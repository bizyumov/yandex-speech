"""Optional generated SpeechKit v3 client; no gRPC imports on the base path."""
from __future__ import annotations

import hashlib
import math
import threading
from urllib.parse import urlparse

from .errors import SpeechError

MAX_GRPC_BYTES = 60_000_000
MAX_FILE_DURATION_MS = 14_400_000
MAX_STORAGE_BYTES = 1_000_000_000


def sdk():
    try:
        import grpc
        from yandex.cloud.ai.stt.v3 import stt_pb2, stt_service_pb2, stt_service_pb2_grpc
        from yandex.cloud.operation import operation_service_pb2, operation_service_pb2_grpc
    except ImportError:
        raise SpeechError("PREREQUISITE", "optional gRPC support is unavailable; follow the host dependency procedure or choose an available REST mode") from None
    return grpc, stt_pb2, stt_service_pb2, stt_service_pb2_grpc, operation_service_pb2, operation_service_pb2_grpc


def _model(p, container, rate, *, language, diarize, literature_text, streaming=False, channels=1):
    if container == "LPCM":
        audio = p.AudioFormatOptions(raw_audio=p.RawAudio(
            audio_encoding=p.RawAudio.LINEAR16_PCM, sample_rate_hertz=rate, audio_channel_count=channels))
    else:
        try:
            kind = p.ContainerAudio.ContainerAudioType.Value(container)
        except ValueError:
            raise SpeechError("CONFIG", "prepared container is not supported by v3") from None
        audio = p.AudioFormatOptions(container_audio=p.ContainerAudio(container_audio_type=kind))
    options = {
        "model": "general:rc" if diarize else "general",
        "audio_format": audio,
        "text_normalization": p.TextNormalizationOptions(
            text_normalization=p.TextNormalizationOptions.TEXT_NORMALIZATION_ENABLED if literature_text else p.TextNormalizationOptions.TEXT_NORMALIZATION_DISABLED,
            literature_text=literature_text, profanity_filter=False,
            phone_formatting_mode=p.TextNormalizationOptions.PHONE_FORMATTING_MODE_DISABLED),
        "language_restriction": p.LanguageRestrictionOptions(
            restriction_type=p.LanguageRestrictionOptions.WHITELIST, language_code=[language]),
    }
    if streaming:
        options["audio_processing_type"] = p.RecognitionModelOptions.REAL_TIME
    return p.RecognitionModelOptions(**options)


def build_file_request(audio=None, *, language="ru-RU", diarize=False,
                       literature_text=True, uri=None, source_info=None):
    _, p, *_ = sdk()
    if uri is None:
        if audio is None:
            raise SpeechError("CONFIG", "prepared audio is required")
        duration, size, container, rate = (audio.duration_ms, audio.size_bytes,
                                          audio.container, audio.sample_rate_hertz)
        if size > MAX_GRPC_BYTES:
            raise SpeechError("LIMIT", "prepared audio exceeds the v3 inline request budget; use a storage route")
        with audio.path.open("rb") as stream:
            content = stream.read(MAX_GRPC_BYTES + 1)
        if len(content) > MAX_GRPC_BYTES:
            raise SpeechError("LIMIT", "prepared audio grew beyond the inline request budget")
        if not audio.source_info.get("converted") and audio.source_info.get("source_sha256"):
            if hashlib.sha256(content).hexdigest() != audio.source_info["source_sha256"]:
                raise SpeechError("STATE", "the addressed source changed during admission")
        channels = audio.channels
        source = {"content": content}
    else:
        try:
            parsed = urlparse(str(uri))
            port = parsed.port
        except ValueError:
            raise SpeechError("CONFIG", "invalid source object URI") from None
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query
                or parsed.fragment or port not in (None, 443)
                or not (host == "storage.yandexcloud.net" or host.endswith(".storage.yandexcloud.net"))):
            raise SpeechError("CONFIG", "use a supported non-secret Yandex Object Storage URI")
        info = source_info or {}
        if not isinstance(info,dict) or not isinstance(info.get("prepared"),dict):
            raise SpeechError("CONFIG", "source object requires prepared-audio metadata")
        prepared = info["prepared"]
        duration, size = info.get("duration_ms"), info.get("size_bytes")
        container, rate = prepared.get("container"), prepared.get("sample_rate_hertz")
        channels = prepared.get("channels")
        if type(size) is not int or not 0 <= size <= MAX_STORAGE_BYTES:
            raise SpeechError("LIMIT", "source object size is missing or exceeds the storage budget")
        source = {"uri": str(uri)}
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= MAX_FILE_DURATION_MS:
        raise SpeechError("LIMIT", "prepared recording must fit the four-hour single-operation budget")
    if (container not in {"LPCM","WAV","OGG_OPUS","MP3"} or type(rate) is not int or rate <= 0
            or container in {"LPCM","WAV"} and rate not in {8000,16000,48000}):
        raise SpeechError("CONFIG", "source metadata does not describe a supported prepared representation")
    if type(channels) is not int or channels not in {1, 2}:
        raise SpeechError("CONFIG", "source metadata must describe one or two audio channels")
    model = _model(p, container, rate, language=language, diarize=diarize,
                   literature_text=literature_text, channels=channels)
    request = p.RecognizeFileRequest(**source, recognition_model=model,
        speaker_labeling=p.SpeakerLabelingOptions(speaker_labeling=p.SpeakerLabelingOptions.SPEAKER_LABELING_ENABLED if diarize else p.SpeakerLabelingOptions.SPEAKER_LABELING_DISABLED))
    if uri is None and request.ByteSize() > MAX_GRPC_BYTES:
        raise SpeechError("LIMIT", "serialized v3 protobuf exceeds 60,000,000 bytes including its envelope")
    return request


def build_stream_options(*, language="ru-RU", literature_text=True):
    _, p, *_ = sdk()
    return p.StreamingRequest(session_options=p.StreamingOptions(
        recognition_model=_model(p, "LPCM", 16000, language=language, diarize=False,
                                 literature_text=literature_text, streaming=True),
        eou_classifier=p.EouClassifierOptions(external_classifier=p.ExternalEouClassifier())))


def operation_dict(operation):
    if isinstance(operation, dict):
        return operation
    result = {"id": operation.id, "done": operation.done}
    if operation.HasField("error"):
        result["error"] = {"code": operation.error.code}
    return result


def status_name(error):
    try:
        code = error.code()
        return code.name if hasattr(code, "name") else str(code)
    except (AttributeError, TypeError):
        return None


def rpc_error(error, *, submitting=False):
    name = status_name(error)
    request_id = None
    try:
        for key, value in error.trailing_metadata() or []:
            if key.lower() in {"x-request-id", "x-server-trace-id"}:
                request_id = value
                break
    except (AttributeError, TypeError):
        pass
    if submitting and name not in {"UNAUTHENTICATED", "PERMISSION_DENIED", "INVALID_ARGUMENT", "FAILED_PRECONDITION"}:
        return SpeechError("SUBMISSION_UNCERTAIN", "submission acceptance is unknown; retain the receipt and do not resubmit automatically", code=name, request_id=request_id)
    category = {"UNAUTHENTICATED": "AUTH", "PERMISSION_DENIED": "PERMISSION",
                "RESOURCE_EXHAUSTED": "RATE_LIMIT", "DEADLINE_EXCEEDED": "TIMEOUT",
                "CANCELLED": "CANCELLED", "UNAVAILABLE": "TRANSPORT",
                "INVALID_ARGUMENT": "CONFIG"}.get(name, "PROVIDER")
    message = "SpeechKit request failed"
    if category == "PERMISSION":
        message = "SpeechKit execution denied; check ai.speechkit-stt.user and key scope yc.ai.speechkitStt.execute; denial alone does not identify which is missing"
    if category == "AUTH":
        message = "SpeechKit authentication rejected; inspect the selected runtime credential and key validity"
    return SpeechError(category, message, code=name, request_id=request_id)


class GrpcTransport:
    def __init__(self, api_key, *, _channels=None):
        grpc, p, service, stubs, ops, ops_stubs = sdk()
        self.grpc, self.p, self.service, self.ops = grpc, p, service, ops
        self.metadata = (("authorization", "Api-Key " + api_key),)
        self._stop = threading.Event()
        self._cancel_event = None
        self._watcher = None
        self._calls = set()
        self._calls_lock = threading.Lock()
        self.submission_started = False
        options = [("grpc.max_send_message_length", 64 * 1024 * 1024),
                   ("grpc.max_receive_message_length", 64 * 1024 * 1024)]
        if _channels is None:
            credentials = grpc.ssl_channel_credentials()
            _channels = (
                grpc.secure_channel("stt.api.cloud.yandex.net:443", credentials, options=options),
                grpc.secure_channel("operation.api.cloud.yandex.net:443", credentials, options=options))
        self.stt_channel, self.operation_channel = _channels
        self.async_stub = stubs.AsyncRecognizerStub(self.stt_channel)
        self.stream_stub = stubs.RecognizerStub(self.stt_channel)
        self.operation_stub = ops_stubs.OperationServiceStub(self.operation_channel)

    def bind_cancel_event(self, event):
        if self._watcher is not None:
            if event is not self._cancel_event:
                raise SpeechError("STATE", "transport is already bound to another operation")
            return
        self._cancel_event = event
        def watch():
            while not self._stop.wait(0.02):
                if event.is_set():
                    self.cancel_all()
                    return
        self._watcher = threading.Thread(target=watch, name="yandex-speech-cancel", daemon=True)
        self._watcher.start()

    def _track(self, call):
        with self._calls_lock:
            self._calls.add(call)
        if self._stop.is_set() or (self._cancel_event is not None and self._cancel_event.is_set()):
            call.cancel()
        return call

    def _forget(self, call):
        with self._calls_lock:
            self._calls.discard(call)

    def _unary(self, method, request, timeout):
        call = self._track(method.future(request, metadata=self.metadata, timeout=timeout))
        try:
            return call.result()
        finally:
            self._forget(call)

    def recognize_file(self, request, timeout):
        ready = self._track(self.grpc.channel_ready_future(self.stt_channel))
        try:
            ready.result(timeout=min(10, timeout))
        except self.grpc.FutureTimeoutError:
            raise SpeechError("TRANSPORT", "SpeechKit gRPC connection is unavailable before submission") from None
        except self.grpc.FutureCancelledError:
            raise SpeechError("CANCELLED", "local connection attempt cancelled before submission") from None
        finally:
            self._forget(ready)
        self.submission_started = True
        return self._unary(self.async_stub.RecognizeFile, request, timeout)

    def get_operation(self, operation_id, timeout):
        return self._unary(self.operation_stub.Get,
            self.ops.GetOperationRequest(operation_id=operation_id), timeout)

    def get_recognition(self, operation_id, timeout):
        return self._track(self.async_stub.GetRecognition(
            self.service.GetRecognitionRequest(operation_id=operation_id),
            metadata=self.metadata, timeout=timeout))

    def open_stream(self, requests, timeout):
        return self._track(self.stream_stub.RecognizeStreaming(
            requests, metadata=self.metadata, timeout=timeout))

    def cancel_all(self):
        with self._calls_lock:
            calls = list(self._calls)
        for call in calls:
            call.cancel()

    def close(self):
        self._stop.set()
        self.cancel_all()
        self.stt_channel.close()
        self.operation_channel.close()
        if self._watcher is not None and self._watcher is not threading.current_thread():
            self._watcher.join(timeout=2)
            if self._watcher.is_alive():
                raise SpeechError("STATE", "owned transport worker failed the two-second shutdown budget")
