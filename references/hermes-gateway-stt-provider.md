# Hermes gateway STT via yandex-speech

## When this applies

Use this when Telegram/gateway voice messages arrive as cached OGG files under the Hermes profile, but Hermes reports that no STT provider is configured. Do not install `faster-whisper` just to solve this when the user has requested Yandex SpeechKit: `yandex-speech` is the intended recognizer.

Profile note: paths are profile-specific. Use `$HERMES_HOME/cache/audio/*.ogg` or `/opt/hermes/agents/<profile>/cache/audio/*.ogg` for the active profile (for example, `margeana`, `velizar`, etc.); do not hardcode another profile's cache path.

## Manual fallback for a voice message

1. Load this skill first.
2. Look for the newest relevant cached voice file:
   - `$HERMES_HOME/cache/audio/*.ogg`
   - Velizar profile example: `/opt/hermes/agents/velizar/cache/audio/audio_*.ogg`
3. Transcribe the original cached OGG directly (replace `<profile>` and `<id>`):

```bash
python3 /opt/hermes/shared/skills/yandex-speech/scripts/transcribe.py \
  /opt/hermes/agents/<profile>/cache/audio/audio_<id>.ogg \
  --mode direct \
  --lang ru-RU \
  --format txt \
  --output /tmp/hermes_voice_transcript.txt
```

Return the transcript plainly. Do not tell the user STT is unavailable if the skill/env path can do the job.

Operational fallback notes:
- Use the active profile cache path, not only examples from another profile: `/opt/hermes/agents/<profile>/cache/audio/audio_<id>.ogg`.
- If writing to a fixed `/tmp/hermes_voice_transcript.txt` fails with `PermissionError`, retry with a unique per-run output path such as `/tmp/hermes_voice_transcript_$(date +%s).txt`; the durable lesson is to avoid shared temp filenames for gateway/profile voice transcription artifacts.
- If the skill says required env vars are missing, check the active profile `.env` only for presence of relevant Yandex Speech variables; do not print secret values.

If writing to a fixed temp output like `/tmp/hermes_voice_transcript.txt` fails with `PermissionError`, retry with a unique output file (`/tmp/hermes_voice_transcript_$(date +%s).txt`) or a profile-owned temp path. This is an output-file collision/permissions issue, not an STT failure.

## Automatic gateway STT with Hermes `local_command`

Hermes does not currently load a skill as `stt.provider: yandex-speech`. The working built-in bridge is `local_command`: set three environment variables in the active Hermes profile `.env`, then make a small `config.yaml` change.

Profile `.env` variables:

```bash
YANDEX_SPEECH_API_KEY=...
YANDEX_CLOUD_FOLDER_ID=...
HERMES_LOCAL_STT_COMMAND=/opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh {input_path} {output_dir} {language}
```

If the wrapper file is not executable, either `chmod +x /opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh` or prefix the command with `bash`:

```bash
HERMES_LOCAL_STT_COMMAND=bash /opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh {input_path} {output_dir} {language}
```

Minimal `config.yaml` change:

```yaml
stt:
  enabled: true
  provider: local_command
  local:
    language: ru-RU
```

`tools/transcription_tools.py` runs `HERMES_LOCAL_STT_COMMAND` and expects a `.txt` file in `{output_dir}`. For Telegram OGG files, current Hermes local-command preprocessing converts non-native audio to a temp WAV named like:

```text
/tmp/hermes-local-stt-<random>/audio_0e088b2d09af.wav
```

The wrapper reconstructs the original cached file from the stem:

```text
/opt/hermes/agents/velizar/cache/audio/audio_0e088b2d09af.ogg
```

and sends that original OGG to Yandex SpeechKit. The wrapper calls `transcribe.py --mode auto`: it tries direct STT first, and if Yandex rejects the file with direct-mode limits (`audio should be less than 1 mb` or `audio duration should be less than 30s`), it splits/re-encodes the audio into ~25s low-bitrate OGG/Opus chunks and transcribes them sequentially. This supports longer Telegram voice messages without requiring Object Storage credentials.

Do not put inline shell parameter expansions like `${stem%.*}` directly in `HERMES_LOCAL_STT_COMMAND` unless they are escaped as `${{stem%.*}}`: Hermes formats the command with Python `.format()` before shell execution, so unescaped shell braces become bogus Python-format placeholders. Prefer the wrapper command above.

Operational notes:
- Ensure the gateway process, not only the current agent/tool process, sees `YANDEX_SPEECH_API_KEY`, `YANDEX_CLOUD_FOLDER_ID`, and `HERMES_LOCAL_STT_COMMAND`. A manual tool-process fallback can also source the active profile `.env` before running the script, but never print secrets.
- `HERMES_LOCAL_STT_LANGUAGE` is optional when `stt.local.language` is set in YAML; the YAML value wins.
- If direct transcription fails with `YANDEX_SPEECH_API_KEY is required` or missing folder ID, report the exact missing env vars and the profile `.env` path to update; do not claim STT is impossible or that the voice content was understood.
- Before editing systemd units, gateway config, or env files, stop and get explicit user approval; these are persistent service changes.
- After persistent env/config changes in gateway mode, restart the gateway (for Telegram, `/restart` in the chat/channel is enough when available).
- If the user is asking a design question, answer from source behavior first. Do not mutate configs while answering.

## Source facts verified in this session

- `transcribe_audio()` calls `_prepare_local_audio()` before formatting `HERMES_LOCAL_STT_COMMAND`.
- `_prepare_local_audio()` uses `converted_path = os.path.join(work_dir, f"{audio_path.stem}.wav")`.
- Therefore `audio_<id>.ogg` becomes `/tmp/hermes-local-stt-*/audio_<id>.wav` for the command template.
- The tested original-cache reconstruction succeeded and transcribed the latest Telegram voice file.
- Long Telegram voice messages can exceed Yandex direct STT limits even when the original OGG is valid. Keep that complexity inside `transcribe.py`/the skill, not in the agent: `--mode auto` should try direct STT, then chunk/re-encode to short OGG/Opus segments and merge the transcript.
- For shared-skill installation documentation, follow the Hermes fresh-install model: install `yandex-speech` as a real checkout under `/opt/hermes/shared/skills/yandex-speech` with group/shared permissions and executable wrapper. Do not document temp clone + `rsync` as the default install path for shared skills.