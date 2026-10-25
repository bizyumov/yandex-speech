# Hermes 0.22.5 integration

The optional native plugin lives at integrations/hermes and registers a TranscriptionProvider named yandex. The skill remains the installed discovery entry; the plugin adds a host binding. No productive profile is modified by import/tests.

For an authorized profile, link its plugin directory entry named yandex-speech to the installed skill's integrations/hermes directory, then enable that plugin through Hermes's normal plugin configuration. Ensure the standard Hermes environment can import the skill and its optional gRPC dependencies. Verify the plugin is loaded in that exact profile before changing its selected STT provider. Example profile settings:

```yaml
stt:
  provider: yandex
  streaming: true
  yandex:
    language: ru-RU
```

The adapter binds .config/yandex-speech/config.json to the registered active workspace. User-file values override profile stt.yandex values and host language hints. The secret resolver must match that same profile; a foreign/missing scope fails closed. Do not edit global HOME to switch profiles. Required real methods are is_available(), transcribe(), streaming_capable and open_stream_session(); session push_audio/partial_transcript/finalize adapt the core completion contract to success/transcript/provider/error envelopes. A partial is never a completed ordinary input. The native finalize error envelope does not raise even when cleanup fails.

A same-name stt.providers.yandex entry with type: command takes precedence in Hermes; remove/reconcile that entry before native streaming selection. Registration diagnoses this conflict. File availability remains distinct from streaming eligibility. A profile declining/missing gRPC can use the existing REST file option when its text/metadata requirements fit; it is not eligible for arriving recognition. Automatic true-literary requests are not silently degraded to raw v1 text.

The host's tools.transcription_streaming.open_streaming_session consumes stt.streaming: true. That setting selects eligible provider streaming only on host surfaces which actually feed arriving audio. Complete attachment input remains recorded recognition. Full platform capture, turn controls, wake/stop words, interruption propagation and progressive reply/playback are Phase 4; native STT alone does not complete voice chatting.

## Command wrappers

Keep positional contracts:

- scripts/hermes-stt-wrapper.sh {input_path} {output_dir} {language} writes output_dir/transcript.txt. The exact addressed input is processed; no unrelated cache file is substituted. The host language remains an adapter default.
- scripts/hermes-tts-wrapper.sh {input_path} {output_path} reads UTF-8 text and writes service oggopus (host file type ogg). HERMES_TTS_VOICE and HERMES_TTS_LANG are adapter defaults below the workspace user file.

Wrappers resolve their own location and PATH-selected python3. The gateway PATH must select the standard Hermes venv. Keep scripts executable. Pass YANDEX_SPEECH_API_KEY through the host's existing secret-name mechanism; never embed it in commands. Hermes command-TTS removes its allocated output before invoking the wrapper, so ordinary no-clobber output is compatible. A synthesized voice-compatible file does not override opt-outs, recipient restrictions or delivery permissions.

[Hermes STT SPI](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/agent/transcription_provider.py) · [Host stream consumer](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/tools/transcription_streaming.py) · [Host command TTS](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/tools/tts_command_provider.py).
