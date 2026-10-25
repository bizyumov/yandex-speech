"""Hermes 0.22.5 native Yandex STT plugin."""
import sys
from pathlib import Path

# The plugin may be linked from a skill checkout rather than pip-installed.
_root=Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0,str(_root))


def register(ctx):
    from hermes_cli.config import load_config_readonly
    from .provider import YandexTranscriptionProvider
    from yandex_speech.errors import SpeechError
    config=load_config_readonly() or {}
    stt=config.get("stt") or {}
    named=(stt.get("providers") or {}).get("yandex") or {}
    if named.get("type") == "command" and stt.get("streaming"):
        raise SpeechError("CONFIG","same-name yandex command provider takes precedence; remove that command entry before selecting native streaming")
    workspace=Path(ctx._manager.home_path)
    provider=YandexTranscriptionProvider(workspace,profile=stt.get("yandex") or {})
    ctx.register_transcription_provider(provider)
    return provider
