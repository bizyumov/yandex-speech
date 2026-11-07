"""Secret-safe speech readiness and explicit native Hermes enrollment."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
from pathlib import Path

from .config import RuntimeContext, resolve_settings
from .errors import SpeechError


def _available(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def check(*, _context=None, _environment=None, _probe=None):
    env = os.environ if _environment is None else _environment
    context = _context or RuntimeContext(user_home=Path(env.get("HOME") or Path.home()),
        workspace=Path(env["HERMES_HOME"]) if env.get("HERMES_HOME") else None)
    settings = resolve_settings(_context=context, _environment=env)
    grpc = all(_available(module) for module in (
        "grpc", "yandex.cloud.ai.stt.v3.stt_pb2", "yandex.cloud.ai.stt.v3.stt_service_pb2_grpc",
        "yandex.cloud.operation.operation_service_pb2_grpc"))
    requests_available, storage_sdk = _available("requests"), _available("boto3")
    key_present = bool(env.get("YANDEX_SPEECH_API_KEY"))
    version = settings.value("api_version")
    version = "3" if version == "auto" else version
    selected_transport = settings.value("transport")
    route_transport = ("grpc" if version == "3" and grpc else "rest") if selected_transport == "auto" else selected_transport
    route_available = requests_available and (
        grpc if (version, route_transport) == ("3", "grpc") else
        route_transport == "rest" and version in {"1", "2", "3"})
    storage_ready = storage_sdk and bool(settings.value("object_storage.bucket")) and bool(
        env.get("YANDEX_SPEECH_AWS_ACCESS_KEY_ID")) and bool(env.get("YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY"))
    report = {
        "authentication": {"status": "present-unverified" if key_present else "missing",
                           "credential": "YANDEX_SPEECH_API_KEY"},
        "permissions": {
            "status": "unverified",
            "verified_operation": None,
            "tts_execution_status": "unverified",
            "stt_role": "ai.speechkit-stt.user",
            "stt_scope": "yc.ai.speechkitStt.execute",
            "tts_role": "ai.speechkit-tts.user",
            "tts_scope": "yc.ai.speechkitTts.execute",
            "remedy": "Ask the service-account owner to verify roles on the resource and the API key execution scopes. Supply the authorized service account to the agent for setup planning; key creation helpers are Phase 2.",
        },
        "config_file": str(settings.path),
        "effective": settings.speech,
        "sources": settings.sources,
        "dependencies": {"requests": requests_available, "grpc": grpc,
                         "storage": storage_sdk,
                         "ffmpeg": bool(shutil.which("ffmpeg")),
                         "ffprobe": bool(shutil.which("ffprobe"))},
        "effective_route": {"api_version": version, "transport": route_transport,
                            "available": bool(route_available), "availability_kind": "local-only",
                            "inputs": {"inline": bool(route_available and version in {"1", "3"}),
                                       "caller_uri": bool(route_available and version in {"2", "3"}),
                                       "helper_upload": bool(route_available and version in {"2", "3"} and storage_ready)},
                            "streaming_available": bool(route_available and version == "3" and route_transport == "grpc"
                                                        and not settings.value("enable_diarization"))},
        "options": [
            {"route": "gRPC v3", "status": "implemented", "tradeoff": "Recorded timing/speakers and bounded arriving audio; optional generated SDK."},
            {"route": "REST v3 inline/URI", "status": "implemented", "tradeoff": "Recorded timing/speakers without generated SDK; inline JSON/base64 has a complete wire cap."},
            {"route": "REST v2 + S3", "status": "implemented", "tradeoff": "Explicit versioned timing/recovery without native speaker labels; existing readable URI needs no upload SDK."},
            {"route": "REST v1 direct/split", "status": "implemented", "tradeoff": "Explicit raw-text alternative without genuine word timing or speaker labels."},
            {"route": "automatic recording fallback", "status": "implemented", "tradeoff": "Local-only gRPC/REST changes and eligible raw-text v1 windows; accepted/uncertain work is never resubmitted."},
            {"route": "over-limit async partitioning", "status": "pending", "tradeoff": "Oversized async sources still require explicit compatible recovery; no cross-job speaker identity is promised."},
        ],
    }
    if _probe is not None and key_present:
        try:
            result = _probe()
            if not result.get("input_complete"):
                raise SpeechError("OUTPUT_PARTIAL", "probe did not complete")
            report["authentication"]["status"] = "execution-verified"
            report["permissions"]["status"] = "execution-verified"
            report["permissions"]["verified_operation"] = "speech-transcription"
        except SpeechError as error:
            report["authentication"]["status"] = "rejected" if error.category == "AUTH" else "present-unverified"
            report["permissions"]["status"] = "denied-cause-unverified" if error.category == "PERMISSION" else "unverified"
            report["probe_error"] = {"category": error.category}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true", help="Check prerequisites without changing configuration")
    operation.add_argument("--hermes-plan", action="store_true", help="Preview native STT/TTS enrollment in the active Hermes profile")
    operation.add_argument("--hermes-apply", action="store_true", help="Apply the checked native STT/TTS enrollment plan")
    parser.add_argument("--enable-stt-streaming", action="store_true", help="Explicitly enroll eligible native arriving-audio STT")
    parser.add_argument("--enable-tts-pcm", action="store_true", help="Explicitly select Yandex native PCM synthesis streaming")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--probe-audio", type=Path, help="Explicitly submit this synthetic recording; incurs API usage")
    args = parser.parse_args(argv)
    native = args.hermes_plan or args.hermes_apply
    if not native and (args.enable_stt_streaming or args.enable_tts_pcm):
        parser.error("streaming enrollment requires --hermes-plan or --hermes-apply")
    if native:
        if args.config is not None or args.probe_audio is not None:
            parser.error("native enrollment uses the active workspace defaults; standalone config/probe options cannot be combined")
        try:
            from .hermes_setup import plan_native
            plan = plan_native(enable_stt_streaming=args.enable_stt_streaming,
                               enable_tts_pcm=args.enable_tts_pcm)
            report = plan.apply() if args.hermes_apply else plan.summary()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        except SpeechError as error:
            print(json.dumps({"error": {"category": error.category, "message": str(error)}}))
            return 3 if error.category == "PREREQUISITE" else 2
    context = RuntimeContext(user_home=Path.home(),
        workspace=Path(os.environ["HERMES_HOME"]) if os.getenv("HERMES_HOME") else None,
        selected_file=args.config)
    probe = None
    if args.probe_audio is not None:
        from .transcription import SpeechTranscriber
        probe = lambda: SpeechTranscriber(_context=context).transcribe_auto(args.probe_audio)
    try:
        report = check(_context=context, _probe=probe)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["authentication"]["status"] not in {"present-unverified", "execution-verified"} or "probe_error" in report:
            return 2
        return 0 if report["effective_route"]["available"] else 3
    except SpeechError as error:
        print(json.dumps({"error": {"category": error.category, "message": str(error)}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
