"""Secret-safe speech readiness; Hermes configuration uses its native CLI."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
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
            "remedy": "Ask the service-account owner to verify roles on the resource and the API key execution scopes. Use inspect-account and prepare-key with the authorized service-account ID and named management credential. Only explicit create-key creates a scoped key in a new protected file.",
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
            {"route": "over-limit async partitioning", "status": "implemented", "tradeoff": "Explicit compatible inline partitions retain complete samples and durable recovery; speaker labels remain partition-local."},
        ],
    }
    report["synthesis"] = synthesis_readiness(settings, key_present=key_present)
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



def synthesis_readiness(settings, *, key_present=False, text=None, host_sink_ready=None):
    """Local selected-operation readiness, without a paid call or host-sink assumption."""
    from dataclasses import asdict
    from .tts_capabilities import probe_runtime
    from .tts_selection import plan_synthesis
    from .tts_voices import resolve_voice
    report = {"file_ready": False, "pcm_stream_ready": False,
              "host_sink_ready": host_sink_ready, "host_sink_status": "unverified" if host_sink_ready is None else "caller-verified",
              "permissions": "unverified", "availability_kind": "local-only",
              "input_scope": "actual SDK input" if text is not None else "representative input; final input unknown",
              "automatic_routing_active": False, "exclusions": [],
              "native_boundary": "Per-callback SDK preflight cannot establish undisclosed whole-message length or host combination/delivery."}
    try:
        options = resolve_voice(settings.value("tts"), settings.sources)
        runtime = probe_runtime()
        report["dependencies"] = {name: asdict(getattr(runtime, name)) for name in
                                  ("http", "grpc", "utterance", "stream")}
        if not runtime.http.ready:
            raise SpeechError("PREREQUISITE", "base TTS dependency: " + runtime.http.reason)
        from .tts_stream_file import stream_contract
        runtime = probe_runtime(dict(options, model=options["_model"], role=options["_role"]))
        report["effective"] = {"voice": options["default_voice"], "model": options["_model"],
            "role": options["_role"] or options["_voice_info"]["default_role"],
            "format": options["default_format"], "file_method": options["file_method"]}
        report["dependencies"] = {name: asdict(getattr(runtime, name)) for name in
                                  ("http", "grpc", "utterance", "stream")}
        pcm_options = dict(options, default_format="lpcm", sample_rate_hertz=24000)
        pcm_runtime = probe_runtime(dict(pcm_options, model=options["_model"], role=options["_role"]))
        report["pcm_stream_ready"] = bool(key_present and pcm_runtime.http.ready and pcm_runtime.stream.ready
            and options["api_version"] != "1" and options["transport"] != "rest"
            and options["file_method"] != "utterance" and "3" in options["_voice_info"]["versions"]
            and not options.get("unsafe_mode") and not options.get("duration_ms"))
        plan = plan_synthesis(text if text is not None else "Readiness preview.", options,
            runtime=runtime, stream_contract=stream_contract(options), sources=settings.sources,
            config_path=settings.path)
        report["operation"] = {"api_version": plan.api_version, "transport": plan.transport,
            "method": plan.method, "planned_parts": len(plan.parts), "assembly": plan.assembly,
            "reason": plan.reason, "output_budget_bytes": 256 * 1024 * 1024}
        report["exclusions"] = [asdict(item) for item in plan.exclusions]
        if options["file_method"] == "auto":
            report["selection_status"] = "preview; automatic call-site activation awaits native acceptance"
            report["file_ready"] = None if key_present else False
        else:
            report["selection_status"] = "explicit complete-file operation"
            report["file_ready"] = bool(key_present)
        if not key_present:
            report["error"] = {"category": "AUTH", "message": "YANDEX_SPEECH_API_KEY is missing"}
    except SpeechError as error:
        report["error"] = {"category": error.category, "message": str(error)}
        report["exclusions"] = [asdict(item) for item in getattr(error, "exclusions", ())]
    return report

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"inspect-account", "prepare-key", "create-key", "apply-key", "setup-storage"}:
        from .cloud_setup import main as setup_main
        return setup_main(argv)
    if argv[:1] == ["check"]:
        argv = argv[1:]
    parser = argparse.ArgumentParser(description=__doc__, epilog="Account setup: inspect-account, prepare-key, create-key, setup-storage or apply-key --help. Other Hermes configuration uses the native CLI.")
    parser.add_argument("--check", action="store_true", help="Check prerequisites without changing configuration")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--probe-audio", type=Path, help="Explicitly submit this synthetic recording; incurs API usage")
    args = parser.parse_args(argv)
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
