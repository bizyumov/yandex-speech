#!/usr/bin/env bash
# Hermes STT wrapper for Yandex SpeechKit.
# Invoked by stt.providers.<name>.command (type: command) or HERMES_LOCAL_STT_COMMAND.
# Paths resolved at runtime:
#   - skill location: from this script's own path (<skill>/scripts/)
#   - agent voice cache: $HERMES_HOME (set per-agent by the Hermes gateway)
# Usage: hermes-stt-wrapper.sh {input_path} {output_dir} {language}
set -euo pipefail

input_path="${1:?input_path required}"
output_dir="${2:?output_dir required}"
language="${3:-ru-RU}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Recover the original cached OGG by stem; the provider may pass a temp WAV
# converted from the voice, and Yandex direct STT rejects WAV OGG headers.
stem="$(basename "$input_path")"; stem="${stem%.*}"
source_path="$input_path"
if [[ -n "${HERMES_HOME:-}" ]]; then
  for candidate in "$HERMES_HOME/audio_cache/${stem}.ogg" "$HERMES_HOME/cache/audio/${stem}.ogg"; do
    if [[ -f "$candidate" ]]; then
      source_path="$candidate"
      break
    fi
  done
fi

python3 "$script_dir/transcribe.py" "$source_path" \
  --mode direct --lang "$language" --format txt \
  --output "$output_dir/transcript.txt"
