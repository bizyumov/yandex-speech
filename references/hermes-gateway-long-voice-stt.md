# Hermes gateway Yandex STT: Telegram voice + long audio

## Class of problem

Use when Hermes gateway receives Telegram voice messages and should transcribe them through the `yandex-speech` skill rather than built-in Whisper/faster-whisper providers.

## Minimal working configuration

Active Hermes profile `.env` needs exactly these STT/Yandex variables visible to the gateway process:

```bash
YANDEX_SPEECH_API_KEY=...
YANDEX_CLOUD_FOLDER_ID=...
HERMES_LOCAL_STT_COMMAND=/opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh {input_path} {output_dir} {language}
```

If the wrapper is not executable, either set executable bit or use:

```bash
HERMES_LOCAL_STT_COMMAND=bash /opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh {input_path} {output_dir} {language}
```

Minimal `config.yaml`:

```yaml
stt:
  enabled: true
  provider: local_command
  local:
    language: ru-RU
```

After `.env`/`config.yaml` changes in Telegram gateway mode, `/restart` in the Telegram chat/channel is sufficient when the slash command is available.

## Source behavior to remember

- `local_command` is a real Hermes STT provider key.
- `HERMES_LOCAL_STT_COMMAND` is formatted with Python `.format()` before shell execution. Inline shell expansions such as `${stem%.*}` must be escaped as `${{stem%.*}}`, but prefer the wrapper to avoid this entirely.
- Hermes preprocesses non-native local STT audio and passes a temp WAV path like `/tmp/hermes-local-stt-*/audio_<id>.wav` as `{input_path}`.
- The wrapper reconstructs the original cached Telegram OGG from the stem: `/opt/hermes/agents/<profile>/cache/audio/audio_<id>.ogg`.
- Direct Yandex STT has small-file limits (`audio should be less than 1 mb`, `audio duration should be less than 30s`). For long voice messages, use `transcribe.py --mode auto`, which falls back to chunked direct recognition without requiring Object Storage credentials.

## Debugging sequence

1. If gateway says `YANDEX_SPEECH_API_KEY is required`, the command is running but the gateway process lacks Yandex env vars.
2. If gateway says `audio should be less than 1 mb` while using direct mode, check whether the command is bypassing the wrapper or whether the source OGG itself exceeds direct limits.
3. For manual recovery, transcribe the newest relevant original cached OGG from `$HERMES_HOME/cache/audio/*.ogg`.
4. If the original OGG is too large/long for direct mode, run `transcribe.py --mode auto` rather than giving up or installing faster-whisper.

## User-workflow lesson

When Boris is actively editing config/env himself, do not write `.env`, secrets, systemd units, or gateway config without explicit permission. Answer from source facts, then let him apply the small change unless he asks the agent to mutate files.