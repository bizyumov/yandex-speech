#!/usr/bin/env bash
# Hermes TTS wrapper for Yandex SpeechKit (synthesis).
# Invoked by tts.providers.<name>.command (type: command).
# Paths resolved at runtime; bare python3 (PATH-resolved = the venv python via
# the gateway's PATH=/opt/hermes/venv/bin:...). Never hardcode an interpreter.
# Usage (Hermes substitutes placeholders): hermes-tts-wrapper.sh {input_path} {output_path}
#   {input_path}  = temp UTF-8 text file to synthesize
#   {output_path} = audio file the command must write (.ogg opus)
# Needs YANDEX_SPEECH_API_KEY + YANDEX_CLOUD_FOLDER_ID in the env (agent .env).
set -euo pipefail

input_path="${1:?input_path (text file) required}"
output_path="${2:?output_path (audio out) required}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

args=(--input-file "$input_path" --output "$output_path" --format oggopus)
[ -n "${HERMES_TTS_LANG:-}" ]  && args+=(--lang "$HERMES_TTS_LANG")
[ -n "${HERMES_TTS_VOICE:-}" ] && args+=(--voice "$HERMES_TTS_VOICE")

python3 "$script_dir/tts.py" "${args[@]}"
