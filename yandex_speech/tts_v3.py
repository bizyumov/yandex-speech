"""SpeechKit v3 complete-utterance transports; no runtime request interception."""
import base64
import binascii
import json
import math
import time

import requests

from .errors import SpeechError
from .tts_audio import finalize_wav

URL = "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis"
LIVE_VOICES = frozenset({"sofia", "vera", "sergey", "vasily", "denis", "irina"})
LIVE_ROLES = {
    "sofia": {"casual", "support"}, "vera": {"casual", "support"},
    "sergey": {"support", "sales"}, "vasily": {"support", "sales"},
    "denis": {"casual", "formal", "neutral", "sales"},
    "irina": {"formal", "support", "narrator", "sales", "neutral"},
}


def request_body(text, options, *, sources=None):
    if not isinstance(text, str) or not text.strip():
        raise SpeechError("CONFIG", "synthesis text must contain speakable content")
    limit = 5000 if options.get("unsafe_mode", False) else 250
    if len(text) > limit:
        raise SpeechError("LIMIT", "v3 utterance exceeds its selected Unicode text budget",
                          code="UTTERANCE_LIMIT")
    voice = options["default_voice"]
    model = options.get("model", "auto")
    if model == "auto":
        model = "livetts" if voice in LIVE_VOICES else "general"
    if (voice in LIVE_VOICES) != (model == "livetts"):
        raise SpeechError("CONFIG", "voice and synthesis model families are incompatible")
    if model not in {"general", "general:rc", "general:deprecated", "livetts"}:
        raise SpeechError("CONFIG", "unsupported synthesis model")
    role = options.get("role")
    if role in (None, "auto"):
        origin = (sources or {}).get("tts.default_emotion", "builtin")
        role = options.get("default_emotion") if origin != "builtin" else None
    if model == "livetts":
        if options["default_speed"] != 1 or options.get("pitch_shift",0)!=0 or options.get("duration_ms",0)>0:
            raise SpeechError("CONFIG", "LiveTTS does not support speed, pitch or duration control")
        if role is not None and role not in LIVE_ROLES[voice]:
            raise SpeechError("CONFIG", "role is incompatible with the selected LiveTTS voice")
    hints = [{"voice": voice}]
    if role is not None:
        hints.append({"role": role})
    if model != "livetts" and options["default_speed"] != 1:
        hints.append({"speed": options["default_speed"]})
    if options.get("volume",-19)!=-19:
        hints.append({"volume":options["volume"]})
    if model!="livetts" and options.get("pitch_shift",0)!=0:
        hints.append({"pitchShift":options["pitch_shift"]})
    if model!="livetts" and options.get("duration_ms",0)>0:
        hints.append({"duration":{"policy":{"min":"MIN_DURATION","max":"MAX_DURATION","exact":"EXACT_DURATION"}[options.get("duration_policy","max")],"durationMs":str(options["duration_ms"])}})
    fmt = options["default_format"]
    if fmt == "lpcm":
        rate = options.get("sample_rate_hertz", 48000)
        if type(rate) is not int or not 8000 <= rate <= 48000:
            raise SpeechError("CONFIG", "v3 PCM sample rate must be in [8000, 48000]")
        audio = {"rawAudio": {"audioEncoding": "LINEAR16_PCM", "sampleRateHertz": str(rate)}}
    else:
        kinds = {"wav": "WAV", "mp3": "MP3", "oggopus": "OGG_OPUS"}
        if fmt not in kinds:
            raise SpeechError("CONFIG", "unsupported synthesis output format")
        audio = {"containerAudio": {"containerAudioType": kinds[fmt]}}
    return {"model": model, "text": text, "hints": hints, "outputAudioSpec": audio,
            "unsafeMode": bool(options.get("unsafe_mode", False))}


def _http_error(response):
    status = response.status_code
    category = {401: "AUTH", 403: "PERMISSION", 429: "RATE_LIMIT"}.get(status, "PROVIDER")
    if status == 400:
        try:
            document = response.json()
            message = str(document.get("message") or (document.get("error") or {}).get("message", ""))
        except (ValueError, AttributeError, TypeError):
            message = ""
        if "too long" in message.casefold() or "duration" in message.casefold():
            return SpeechError("LIMIT", "provider rejected the utterance text/duration budget",
                               code="UTTERANCE_LIMIT")
        category = "CONFIG"
    return SpeechError(category, f"SpeechKit TTS request failed: HTTP {status}", code=str(status))


def rest_synthesize(api_key, body, *, deadline_seconds=120):
    if type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
        raise SpeechError("CONFIG", "synthesis deadline must be positive and finite")
    deadline = time.monotonic() + deadline_seconds
    try:
        response = requests.post(URL, headers={"Authorization": "Api-Key " + api_key},
                                 json=body, stream=True,
                                 timeout=(min(10, deadline_seconds), deadline_seconds))
    except requests.RequestException:
        raise SpeechError("SUBMISSION_UNCERTAIN", "synthesis acceptance is unknown; request was not resubmitted") from None
    chunks = []
    try:
        if response.status_code >= 400:
            raise _http_error(response)
        for line in response.iter_lines():
            if time.monotonic() >= deadline:
                raise SpeechError("TIMEOUT", "synthesis operation deadline expired")
            if not line:
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError
                if record.get("error"):
                    raise SpeechError("OUTPUT_PARTIAL", "provider reported an error before complete synthesis")
                result = record.get("result", record)
                if not isinstance(result, dict):
                    raise ValueError
                audio = result.get("audioChunk")
                if audio is not None:
                    if not isinstance(audio, dict):
                        raise ValueError
                    encoded = audio.get("data")
                    if encoded:
                        chunks.append(base64.b64decode(encoded, validate=True))
            except (ValueError, TypeError, binascii.Error):
                raise SpeechError("OUTPUT_PARTIAL", "malformed synthesis response record") from None
    except requests.RequestException:
        raise SpeechError("OUTPUT_PARTIAL", "synthesis response ended before a complete transport drain") from None
    finally:
        response.close()
    data = b"".join(chunks)
    if not data:
        raise SpeechError("PROVIDER", "provider returned no synthesized audio")
    if body["outputAudioSpec"].get("containerAudio", {}).get("containerAudioType") == "WAV":
        data = finalize_wav(data)
    if "rawAudio" in body["outputAudioSpec"] and len(data)%2:
        raise SpeechError("OUTPUT_PARTIAL","provider PCM ends inside a sample")
    return data

def grpc_sdk():
    try:
        import grpc
        from yandex.cloud.ai.tts.v3 import tts_pb2, tts_service_pb2_grpc
    except ImportError:
        raise SpeechError("PREREQUISITE","optional gRPC synthesis dependencies are unavailable; REST v3 file synthesis remains available") from None
    return grpc,tts_pb2,tts_service_pb2_grpc


def grpc_available():
    try:
        grpc_sdk()
        return True
    except SpeechError:
        return False


def grpc_synthesize(api_key,body,*,deadline_seconds=120,_channel=None):
    from google.protobuf.json_format import ParseDict
    grpc,p,service=grpc_sdk()
    request=ParseDict(body,p.UtteranceSynthesisRequest())
    deadline=time.monotonic()+deadline_seconds
    own_channel=_channel is None
    channel=_channel or grpc.secure_channel("tts.api.cloud.yandex.net:443",
        grpc.ssl_channel_credentials(),options=[
            ("grpc.max_receive_message_length",64*1024*1024),
            ("grpc.max_send_message_length",64*1024*1024)])
    call=None
    chunks=[]
    try:
        try:
            grpc.channel_ready_future(channel).result(timeout=min(10,deadline_seconds))
        except grpc.FutureTimeoutError:
            raise SpeechError("TRANSPORT","gRPC connection unavailable before synthesis submission",code="GRPC_UNREACHABLE") from None
        call=service.SynthesizerStub(channel).UtteranceSynthesis(request,
            metadata=(("authorization","Api-Key "+api_key),),
            timeout=max(.001,deadline-time.monotonic()))
        for response in call:
            if time.monotonic()>=deadline:
                raise SpeechError("TIMEOUT","synthesis operation deadline expired")
            if response.audio_chunk.data:
                chunks.append(response.audio_chunk.data)
    except grpc.RpcError as error:
        code=error.code().name
        if chunks:
            raise SpeechError("OUTPUT_PARTIAL","gRPC synthesis ended before successful response EOF",code=code) from None
        category={"UNAUTHENTICATED":"AUTH","PERMISSION_DENIED":"PERMISSION",
                  "RESOURCE_EXHAUSTED":"RATE_LIMIT","DEADLINE_EXCEEDED":"TIMEOUT",
                  "CANCELLED":"CANCELLED","INVALID_ARGUMENT":"CONFIG"}.get(code,"PROVIDER")
        details=error.details() or ""
        if code=="INVALID_ARGUMENT" and ("too long" in details.casefold() or "duration" in details.casefold()):
            raise SpeechError("LIMIT","provider rejected the utterance text/duration budget",code="UTTERANCE_LIMIT") from None
        raise SpeechError(category,"SpeechKit gRPC synthesis failed",code=code) from None
    finally:
        if call is not None:
            call.cancel()
        if own_channel:
            channel.close()
    data=b"".join(chunks)
    if not data:
        raise SpeechError("PROVIDER","provider returned no synthesized audio")
    if body["outputAudioSpec"].get("containerAudio",{}).get("containerAudioType")=="WAV":
        data=finalize_wav(data)
    if "rawAudio" in body["outputAudioSpec"] and len(data)%2:
        raise SpeechError("OUTPUT_PARTIAL","provider PCM ends inside a sample")
    return data
