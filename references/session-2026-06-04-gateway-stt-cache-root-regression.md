# 2026-06-04 — Hermes gateway STT cache root regression

## Symptom

Telegram voice messages reached the Yandex SpeechKit `local_command` path, but transcription failed with:

```text
Direct STT failed: 400 {"error_code":"BAD_REQUEST","error_message":"ogg header has not been found"}
```

## Root cause

Hermes gateway cached new Telegram voice files under:

```text
/opt/hermes/agents/velizar/audio_cache/audio_<id>.ogg
```

The `hermes-stt-wrapper.sh` wrapper only looked for originals under the older cache root:

```text
/opt/hermes/agents/velizar/cache/audio/audio_<id>.ogg
```

When the original was not found, Hermes' `local_command` flow passed a temp WAV converted from the voice into the wrapper. The wrapper forwarded that temp WAV to Yandex SpeechKit through a path that expected OGG/Opus params, yielding the misleading "ogg header has not been found" error.

## Fix pattern

The wrapper should recover the original cached `.ogg` by stem from both roots before falling back to `{input_path}`:

```bash
source_path="$input_path"
for candidate in \
  "/opt/hermes/agents/velizar/audio_cache/${stem}.ogg" \
  "/opt/hermes/agents/velizar/cache/audio/${stem}.ogg"; do
  if [[ -f "$candidate" ]]; then
    source_path="$candidate"
    break
  fi
done
```

After editing scripts through skill tooling, verify execute permissions. A patch/write operation can leave a shell wrapper non-executable. Expected mode here:

```text
775 velizar:hermes-shared /opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh
```

## Verification

Use the current Hermes runtime transcription entrypoint, not only the raw skill script:

```python
from tools.transcription_tools import transcribe_audio
res = transcribe_audio('/opt/hermes/agents/velizar/audio_cache/audio_<id>.ogg')
assert res['success'] is True
assert res['provider'] == 'local_command'
```

A successful run on the failing cached file confirmed the full path:

```text
success: True
provider: local_command
```
