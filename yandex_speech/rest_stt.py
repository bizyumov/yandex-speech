"""Versioned SpeechKit REST wire transport; no generated SDK or upload dependency."""
from __future__ import annotations

import base64
import json
import math
import re
import threading
import time

import requests

from .errors import SpeechError
from .recording_request import file_source

MAX_REST_BYTES = 60_000_000
MAX_EVENT_BYTES = 8_000_000
MAX_RESULT_BYTES = 256_000_000
_OPERATIONS_URL = "https://operation.api.cloud.yandex.net/operations/"
_HTTP_CODES = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
               404: "NOT_FOUND", 409: "ABORTED", 412: "FAILED_PRECONDITION",
               429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 502: "UNAVAILABLE",
               503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED"}
_GRPC_CODES = {1: "CANCELLED", 3: "INVALID_ARGUMENT", 4: "DEADLINE_EXCEEDED",
               5: "NOT_FOUND", 7: "PERMISSION_DENIED", 8: "RESOURCE_EXHAUSTED",
               9: "FAILED_PRECONDITION", 13: "INTERNAL", 14: "UNAVAILABLE", 16: "UNAUTHENTICATED"}


def _encode(body):
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def build_rest_request(audio=None, *, uri=None, source_info=None, language="ru-RU",
                       diarize=False, literature_text=True, api_version="3"):
    version = str(api_version)
    if version not in {"2", "3"} or type(diarize) is not bool or type(literature_text) is not bool:
        raise SpeechError("CONFIG", "select supported REST v2/v3 settings")
    if version == "2":
        if uri is None or diarize:
            raise SpeechError("CONFIG", "REST v2 requires a prepared storage URI and has no native speaker labeling")
        source, metadata = file_source(uri=uri, source_info=source_info)
        encoding = {"LPCM": "LINEAR16_PCM", "OGG_OPUS": "OGG_OPUS", "MP3": "MP3"}.get(metadata["container"])
        if encoding is None:
            raise SpeechError("CONFIG", "REST v2 needs headerless LPCM, Ogg Opus or MP3; adapt WAV before upload")
        specification = {"languageCode": language, "model": "general", "profanityFilter": False,
                         "literature_text": literature_text, "rawResults": not literature_text,
                         "audioEncoding": encoding}
        if encoding == "LINEAR16_PCM":
            specification.update(sampleRateHertz=metadata["sample_rate_hertz"],
                                 audioChannelCount=metadata["channels"])
        return _encode({"config": {"specification": specification}, "audio": source})
    # Construct the metadata envelope before reading local audio, so base64 and
    # UTF-8 language/model settings share one exact complete-wire admission cap.
    if uri is None:
        if audio is None:
            raise SpeechError("CONFIG", "prepared audio is required")
        metadata = {"container": audio.container, "sample_rate_hertz": audio.sample_rate_hertz,
                    "channels": audio.channels}
    else:
        _, metadata = file_source(uri=uri, source_info=source_info, mono_required=diarize)
    audio_format = (
        {"rawAudio": {"audioEncoding": "LINEAR16_PCM",
                      "sampleRateHertz": str(metadata["sample_rate_hertz"]),
                      "audioChannelCount": str(metadata["channels"])}}
        if metadata["container"] == "LPCM" else
        {"containerAudio": {"containerAudioType": metadata["container"]}})
    body = {"recognitionModel": {"model": "general:rc" if diarize else "general", "audioFormat": audio_format,
            "textNormalization": {"textNormalization": "TEXT_NORMALIZATION_ENABLED" if literature_text else
                                  "TEXT_NORMALIZATION_DISABLED", "literatureText": literature_text,
                                  "profanityFilter": False, "phoneFormattingMode": "PHONE_FORMATTING_MODE_DISABLED"},
            "languageRestriction": {"restrictionType": "WHITELIST", "languageCode": [language]}},
            "speakerLabeling": {"speakerLabeling": "SPEAKER_LABELING_ENABLED" if diarize else
                               "SPEAKER_LABELING_DISABLED"}}
    if uri is None:
        body["content"] = ""
        envelope = len(_encode(body))
        available = MAX_REST_BYTES - envelope
        if available < 0:
            raise SpeechError("LIMIT", "REST request metadata exceeds the complete JSON wire budget")
        source, _ = file_source(audio, inline_limit=3 * (available // 4), mono_required=diarize)
        body["content"] = base64.b64encode(source["content"]).decode("ascii")
    else:
        body["uri"] = str(uri)
    wire = _encode(body)
    if len(wire) > MAX_REST_BYTES:
        raise SpeechError("LIMIT", f"complete REST JSON is {len(wire)} bytes; limit is {MAX_REST_BYTES} bytes")
    return wire


class RestRequestError(Exception):
    def __init__(self, code, request_id=None):
        self._code, self._request_id = code, request_id
        super().__init__("SpeechKit REST request failed")

    def code(self):
        return self._code

    def trailing_metadata(self):
        return [("x-request-id", self._request_id)] if self._request_id else []

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestRequestError("DATA_LOSS")
        result[key] = value
    return result


class _ExactAuth(requests.auth.AuthBase):
    def __init__(self, api_key):
        self._api_key = api_key

    def __call__(self, request):
        request.headers["Authorization"] = "Api-Key " + self._api_key
        return request


def _operation_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise SpeechError("CONFIG", "invalid provider operation ID")
    return value


def _snake(value):
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            # REST gateway fields use camelCase. Internal snake_case aliases
            # must not bypass validation or shadow a documented wire field.
            if not isinstance(key, str) or "_" in key:
                raise RestRequestError("DATA_LOSS")
            name = re.sub(r"[A-Z]", lambda match: "_" + match.group().lower(), key)
            if name in normalized:
                raise RestRequestError("DATA_LOSS")
            normalized[name] = _snake(item)
        return normalized
    if isinstance(value, list):
        return [_snake(item) for item in value]
    return value

def _int64(value):
    if type(value) is int:
        valid = 0 <= value <= 9_223_372_036_854_775_807
    elif isinstance(value, str):
        valid = bool(re.fullmatch(r"[0-9]{1,19}", value)) and int(value) <= 9_223_372_036_854_775_807
    else:
        valid = False
    if not valid:
        raise RestRequestError("DATA_LOSS")


def _integer_fields(record, names):
    for name in names:
        if name in record:
            _int64(record[name])


def _string_fields(record, names):
    for name in names:
        if name in record and not isinstance(record[name], str):
            raise RestRequestError("DATA_LOSS")


def _validate_update(update):
    if not isinstance(update, dict) or not isinstance(update.get("alternatives", []), list):
        raise RestRequestError("DATA_LOSS")
    _string_fields(update, ("channelTag",))
    for alternative in update.get("alternatives", []):
        if not isinstance(alternative, dict) or not isinstance(alternative.get("words", []), list):
            raise RestRequestError("DATA_LOSS")
        _string_fields(alternative, ("text",))
        _integer_fields(alternative, ("startTimeMs", "endTimeMs"))
        for word in alternative.get("words", []):
            if not isinstance(word, dict):
                raise RestRequestError("DATA_LOSS")
            _string_fields(word, ("text",))
            _integer_fields(word, ("startTimeMs", "endTimeMs"))


def _validate_event(event):
    _string_fields(event, ("channelTag",))
    _integer_fields(event, ("responseWallTimeMs",))
    if "audioCursors" in event:
        cursors = event["audioCursors"]
        if not isinstance(cursors, dict):
            raise RestRequestError("DATA_LOSS")
        _integer_fields(cursors, ("receivedDataMs", "resetTimeMs", "partialTimeMs",
                                 "finalTimeMs", "finalIndex", "eouTimeMs"))
    kinds = {"partial", "final", "eouUpdate", "finalRefinement", "statusCode",
             "classifierUpdate", "speakerAnalysis", "conversationAnalysis", "summarization"} & event.keys()
    if len(kinds) > 1 or any(not isinstance(event[kind], dict) for kind in kinds):
        raise RestRequestError("DATA_LOSS")
    for kind in ("partial", "final"):
        if kind in event:
            _validate_update(event[kind])
    if "finalRefinement" in event:
        refinement = event["finalRefinement"]
        _integer_fields(refinement, ("finalIndex",))
        if "normalizedText" in refinement:
            _validate_update(refinement["normalizedText"])
    if "eouUpdate" in event:
        _integer_fields(event["eouUpdate"], ("timeMs",))


def _event(line):
    try:
        frame = json.loads(line, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        raise RestRequestError("DATA_LOSS") from None
    if not isinstance(frame, dict):
        raise RestRequestError("DATA_LOSS")
    if "error" in frame:
        code = frame["error"].get("code") if isinstance(frame["error"], dict) else None
        raise RestRequestError(_GRPC_CODES.get(code, "INTERNAL") if type(code) is int else "INTERNAL")
    event = frame.get("result")
    if not isinstance(event, dict) or not event:
        raise RestRequestError("DATA_LOSS")
    _validate_event(event)
    normalized = _snake(event)
    normalized["_raw_provider_event"] = frame
    return normalized


class RestTransport:
    def __init__(self, api_key, *, api_version="3", _session=None):
        if str(api_version) not in {"2", "3"}:
            raise SpeechError("CONFIG", "REST recorded recognition supports API v2 or v3")
        self.api_version = str(api_version)
        self.session = _session or requests.Session()
        if _session is None:
            self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self.auth = _ExactAuth(api_key)
        self._stop, self._cancel_event = threading.Event(), None
        self._responses, self._lock = set(), threading.Lock()
        self.submission_started = False

    def bind_cancel_event(self, event):
        self._cancel_event = event

    def _check(self, deadline):
        if self._stop.is_set() or self._cancel_event is not None and self._cancel_event.is_set():
            raise RestRequestError("CANCELLED")
        if time.monotonic() >= deadline:
            raise RestRequestError("DEADLINE_EXCEEDED")

    def _close_response(self, response):
        with self._lock:
            self._responses.discard(response)
        response.close()

    def _request(self, method, url, timeout, **options):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise SpeechError("CONFIG", "REST call timeout must be positive and finite")
        deadline = time.monotonic() + timeout
        self._check(deadline)
        if method == "POST":
            self.submission_started = True
        try:
            response = self.session.request(method, url, auth=self.auth, stream=True, allow_redirects=False,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=(min(10, timeout), min(30, timeout)), **options)
        except requests.exceptions.Timeout:
            raise RestRequestError("DEADLINE_EXCEEDED") from None
        except requests.exceptions.RequestException:
            raise RestRequestError("UNAVAILABLE") from None
        with self._lock:
            self._responses.add(response)
        try:
            self._check(deadline)
            if not 200 <= response.status_code < 300:
                raise RestRequestError(_HTTP_CODES.get(response.status_code, "UNKNOWN"),
                                       response.headers.get("x-request-id"))
        except BaseException:
            self._close_response(response)
            raise
        return response, deadline

    def _chunks(self, response, deadline, *, limit):
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=8192):
                self._check(deadline)
                total += len(chunk)
                if total > limit:
                    raise RestRequestError("RESOURCE_EXHAUSTED")
                if chunk:
                    yield chunk
            self._check(deadline)
        except requests.exceptions.Timeout:
            raise RestRequestError("DEADLINE_EXCEEDED") from None
        except requests.exceptions.RequestException:
            raise RestRequestError("UNAVAILABLE") from None

    def _json_request(self, method, url, timeout, **options):
        response, deadline = self._request(method, url, timeout, **options)
        try:
            raw = b"".join(self._chunks(response, deadline, limit=64_000_000))
            try:
                document = json.loads(raw, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError):
                raise RestRequestError("DATA_LOSS") from None
            if not isinstance(document, dict):
                raise RestRequestError("DATA_LOSS")
            self._check(deadline)
            return document
        finally:
            self._close_response(response)

    def recognize_file(self, request, timeout):
        url = ("https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync" if self.api_version == "3" else
               "https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize")
        return self._json_request("POST", url, timeout, data=request)

    def get_operation(self, operation_id, timeout):
        operation = self._json_request("GET", _OPERATIONS_URL + _operation_id(operation_id), timeout)
        if (type(operation.get("done", False)) is not bool
                or operation.get("error") is not None and not isinstance(operation["error"], dict)):
            raise RestRequestError("DATA_LOSS")
        return operation

    def get_recognition(self, operation_id, timeout):
        if self.api_version != "3":
            raise SpeechError("CONFIG", "v2 results are contained in the accepted operation response")
        response, deadline = self._request("GET", "https://stt.api.cloud.yandex.net/stt/v3/getRecognition",
                                          timeout, params={"operationId": _operation_id(operation_id)})
        buffer, count = bytearray(), 0
        try:
            for chunk in self._chunks(response, deadline, limit=MAX_RESULT_BYTES):
                buffer.extend(chunk)
                while (end := buffer.find(b"\n")) != -1:
                    if end > MAX_EVENT_BYTES:
                        raise RestRequestError("RESOURCE_EXHAUSTED")
                    line = bytes(buffer[:end])
                    del buffer[:end+1]
                    if line.strip():
                        event = _event(line)
                        count += int("final" in event)
                        yield event
                if len(buffer) > MAX_EVENT_BYTES:
                    raise RestRequestError("RESOURCE_EXHAUSTED")
            if buffer.strip():
                event = _event(bytes(buffer))
                count += int("final" in event)
                yield event
            if not count:
                raise RestRequestError("DATA_LOSS")
        finally:
            self._close_response(response)

    def cancel_all(self):
        self._stop.set()
        with self._lock:
            responses = tuple(self._responses)
        for response in responses:
            self._close_response(response)

    def close(self):
        self.cancel_all()
        self.session.close()
