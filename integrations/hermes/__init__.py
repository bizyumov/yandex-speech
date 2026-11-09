"""Native Yandex speech providers and an opt-in stock Discord streaming binding."""
import sys
from pathlib import Path

# The plugin may be linked from a skill checkout rather than pip-installed.
_root=Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0,str(_root))


def register(ctx):
    from hermes_cli.config import load_config_readonly
    from tools.transcription_streaming import streaming_enabled
    from .provider import YandexTranscriptionProvider
    from .tts_provider import YandexTTSProvider
    from yandex_speech.errors import SpeechError
    config=load_config_readonly() or {}
    stt=config.get("stt") or {}
    named=(stt.get("providers") or {}).get("yandex") or {}
    if named.get("type") == "command" and streaming_enabled(stt):
        raise SpeechError("CONFIG","same-name yandex command provider takes precedence; remove that command entry before selecting native streaming")
    workspace=Path(ctx._manager.home_path)
    provider=YandexTranscriptionProvider(workspace,profile=stt.get("yandex") or {})
    ctx.register_transcription_provider(provider)
    tts_provider = YandexTTSProvider(workspace,profile=config.get("tts") or {})
    ctx.register_tts_provider(tts_provider)
    from .ssml_tool import register_ssml_tool
    register_ssml_tool(ctx, tts_provider)
    if streaming_enabled(stt) and str(stt.get("provider", "")).lower() == "yandex":
        from yandex_speech.discord_ingress import DiscordIngress
        ingress = DiscordIngress(workspace, provider, stt)
        ctx.register_platform_handler("discord", ingress.factory)
        ctx.register_hook("pre_transcription", ingress.observe_pre_transcription)
        ctx.register_hook("post_gateway_admission", ingress.observe_admission)
        ctx.on_unload(ingress.close)
    return provider
