# Hermes gateway STT: audio cache roots and wrapper verification

Session lesson: Telegram voice transcription failed with Yandex SpeechKit error:

```text
Direct STT failed: 400 {"error_code":"BAD_REQUEST","error_message":"ogg header has not been found"}
```

Root cause was not Yandex credentials or Telegram audio corruption. The Hermes gateway had cached fresh Telegram voice files under:

```text
$HERMES_HOME/audio_cache/audio_<id>.ogg
```

while the Yandex SpeechKit `local_command` wrapper only reconstructed originals from the older path:

```text
$HERMES_HOME/cache/audio/audio_<id>.ogg
```

Hermes `tools.transcription_tools` may pass a temp WAV to `HERMES_LOCAL_STT_COMMAND`. If the wrapper fails to recover the original `.ogg`, Yandex direct STT can be called with a WAV while `format=oggopus` is expected, producing `ogg header has not been found`.

## Durable wrapper rule

When reconstructing the original Telegram voice from the temp input stem, check both roots in this order:

```bash
$HERMES_HOME/audio_cache/${stem}.ogg
$HERMES_HOME/cache/audio/${stem}.ogg
```

If `$HERMES_HOME` is not available in the wrapper environment, use the active profile root explicitly, but do not assume only one of the two cache layouts exists.

## Verification pattern

1. Find recent cache files and confirm they are valid OGG/Opus:

```bash
file /opt/hermes/agents/<profile>/audio_cache/audio_*.ogg | tail
file /opt/hermes/agents/<profile>/cache/audio/audio_*.ogg | tail
```

2. Reproduce the gateway path by converting the original `.ogg` to a temp WAV whose stem matches the original, then call the wrapper:

```bash
src=/opt/hermes/agents/<profile>/audio_cache/audio_<id>.ogg
td=$(mktemp -d /tmp/hermes-local-stt-test.XXXXXX)
out=$(mktemp -d /tmp/hermes-stt-out.XXXXXX)
ffmpeg -y -v error -i "$src" "$td/audio_<id>.wav"
/opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh \
  "$td/audio_<id>.wav" "$out" ru-RU
sed -n '1,20p' "$out/transcript.txt"
```

3. Also verify the actual Hermes STT function when possible:

```python
from tools.transcription_tools import transcribe_audio
print(transcribe_audio('/opt/hermes/agents/<profile>/audio_cache/audio_<id>.ogg'))
```

## Pitfall: executable bit

After editing skill scripts, verify the wrapper remains executable. Some skill-management writes/patches may leave scripts as non-executable. Fix and verify:

```bash
chmod 775 /opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh
stat -c '%a %U:%G %n' /opt/hermes/shared/skills/yandex-speech/scripts/hermes-stt-wrapper.sh
```
