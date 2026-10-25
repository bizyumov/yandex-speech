#!/usr/bin/env bash
# Positional Hermes command adapter: {input_path} {output_dir} {language}.
# Use the addressed source; automatic media preparation handles host WAV input.
set -euo pipefail

input_path="${1:?input_path required}"
output_dir="${2:?output_dir required}"
language="${3:-ru-RU}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Keep CLI status/path output off stdout: Hermes uses stdout as a transcript fallback.
# Host language is an adapter default below workspace config, not a call override.
python "$script_dir/transcribe.py" "$input_path" \
  --mode auto --adapter-language "$language" --format txt \
  --output "$output_dir/transcript.txt" 1>&2
