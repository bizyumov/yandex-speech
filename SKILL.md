---
name: yandex-speech
description: Use Yandex SpeechKit for speech-to-text and text-to-speech from this workspace. Use when you need to transcribe audio/video with timestamps or speaker labels, synthesize speech audio from text, or process long recordings through Object Storage-backed async recognition without copying secrets into files.
---

# yandex-speech

Personal skill for Yandex SpeechKit work from this workspace.

Use it when you need to:
- transcribe audio or audio-only video with Yandex SpeechKit;
- generate transcript outputs as JSON, TXT, SRT, or VTT;
- request speaker labeling when the API returns it;
- synthesize speech audio from text with Yandex TTS;
- keep credentials in environment variables only.

## Rules

- Do not copy or print `YANDEX_SPEECH_API_KEY`.
- Read secrets only from environment variables at runtime.
- Prefer this skill over ad-hoc curl snippets when Yandex SpeechKit is requested.
- Do not claim diarization/timestamps unless the API response actually contains enough data.

## Required environment variables

Always required:
- `YANDEX_SPEECH_API_KEY`
- `YANDEX_CLOUD_FOLDER_ID`

Optional for async long-running STT through Yandex Object Storage:
- `YANDEX_SPEECH_S3_BUCKET`
- `YANDEX_SPEECH_AWS_ACCESS_KEY_ID`
- `YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY`
- `YANDEX_SPEECH_S3_PREFIX`
- `YANDEX_SPEECH_S3_ENDPOINT`

## Files

- `scripts/tts.py` — Yandex TTS CLI + Python API
- `scripts/transcribe.py` — Yandex STT CLI + Python API
- `scripts/hermes-stt-wrapper.sh` — Hermes `local_command` wrapper that reconstructs the original cached Telegram `.ogg` from Hermes' temp WAV path before invoking Yandex STT
- `requirements.txt` — Python dependencies for this skill
- `docs/issue-11-stt.md` — full GitHub issue #11 text
- `docs/issue-12-tts.md` — full GitHub issue #12 text
- `references/hermes-gateway-stt-provider.md` — how to wire this skill into Hermes gateway voice transcription via `stt.provider: local_command`
- `references/hermes-gateway-long-voice-stt.md` — session-tested Hermes/Yandex gateway setup: 3 env vars + YAML, wrapper pitfalls, and long Telegram voice chunking fallback
- `references/session-2026-06-04-gateway-stt-cache-root-regression.md` — regression note: gateway may cache voices under `$HERMES_HOME/audio_cache/` instead of `$HERMES_HOME/cache/audio/`; wrapper must check both roots and remain executable
- `references/gateway-audio-cache-roots.md` — Telegram voice failure mode where gateway uses `$HERMES_HOME/audio_cache/` instead of `$HERMES_HOME/cache/audio/`; wrapper verification and executable-bit checks

## Quick start

### HLS / conference recordings

For conference sites that embed recordings through HLS players, first discover the playlist/API rather than trying to download from the visible UI. RMS-style players often expose `/api/events` and `/api/events/<slug>/playlist` with per-session `hls_url` / `archive_url` values; extract audio with `ffmpeg`, smoke-test a 20–25s speaking sample, then chunk full sessions for STT. See `references/rms-hls-conference-transcripts.md`.

### Telegram/gateway voice messages

When a user asks to transcribe a Telegram voice message and the gateway says no STT provider is configured, do not stop there if this skill is installed. Check the Hermes profile audio cache for the saved voice file (commonly `$HERMES_HOME/audio_cache/*.ogg`, `$HERMES_HOME/cache/audio/*.ogg`, `/opt/hermes/agents/<profile>/audio_cache/*.ogg`, or `/opt/hermes/agents/<profile>/cache/audio/*.ogg`), pick the relevant/recent file, and run `scripts/transcribe.py` directly. If multiple plausible files exist, use timestamps and conversation context; ask only if ambiguity remains. If required Yandex env vars are missing, report the exact variables and active profile `.env` path to configure rather than presenting it as a generic STT failure.

Save-location pitfall: `mktemp /tmp/...` is fine only as scratch output while verifying STT. If the user asks to save the transcript, copy or write the final artifact into the current working area (for example `{CWD}/transcripts/<audio-stem>_transcript.txt`) and verify the final path/size before reporting. Do not present a `/tmp` transcript as a durable saved file.

Telegram Premium transcript caveat: the user may see a better built-in Telegram transcript that Hermes cannot access. Do not assume Yandex/Hermes STT is literal ground truth. For exact editorial wording, treat Yandex STT as a hypothesis; if it is garbled, use the original audio with alternate/multi-STT or ask for typed text rather than inventing a “dословно” quote.

Attachment-handling pitfall: if the user's message says something like “вот тебе аудио:” but the attachment has not yet appeared in the current turn, do **not** infer an older cached audio file as the intended input. First inspect for a new cache file whose mtime matches the actual attachment arrival; only then announce the exact path and process it. If the user challenges the path, stop explaining and re-check cache mtimes/current attachments immediately. If no new matching cache file exists after the user says “вот файл” / “вот аудио”, report that exact blocker and do not transcribe the previous newest cache item unless Boris explicitly confirms it is the intended file.

Exact-dictation pitfall for editing workflows: a successful Yandex/Hermes transcription is not automatically an authoritative quote. If the user requires `дословно` and the transcript is grammatically broken or drops a plausible word, do not patch that text as-is and do not guess the missing word. Re-run the original cached `.ogg` through another STT path or ask for a typed exact phrase if no better recognition path is available. Telegram Premium transcripts may be visible only to the user, not to Hermes.

Use absolute paths from shared skills when needed, for example:

```bash
out="$(mktemp /tmp/hermes_voice_transcript.XXXXXX.txt)"
python3 /opt/hermes/shared/skills/yandex-speech/scripts/transcribe.py \
  /opt/hermes/agents/velizar/cache/audio/<file>.ogg \
  --mode direct \
  --lang ru-RU \
  --format txt \
  --output "$out"
sed -n '1,120p' "$out"
```

Use a unique output path (`mktemp` or a timestamped file) rather than a fixed `/tmp/hermes_voice_transcript.txt`: on shared/profiled runs, an older transcript file may be owned by another process/user and cause `PermissionError`.

For automatic gateway STT, do **not** try `stt.provider: yandex-speech`: Hermes does not load skills as native STT providers. Configure Hermes' built-in `local_command` provider to invoke `scripts/hermes-stt-wrapper.sh`; see `references/hermes-gateway-stt-provider.md` for the exact config, shared-skill install shape, original-cache `.ogg` reconstruction rule, long-message chunking, and verification path. The wrapper expects args `{input_path} {output_dir} {language}` and recovers `/opt/hermes/agents/velizar/cache/audio/audio_<id>.ogg` from temp WAV paths like `/tmp/hermes-local-stt-*/audio_<id>.wav`. Keep the skill as a real shared checkout under `/opt/hermes/shared/skills/yandex-speech`; do not install it as a detached temp clone copied by `rsync` unless the user explicitly requests a preservation copy.

Pitfalls for Hermes gateway `local_command`:
- Prefer pointing `HERMES_LOCAL_STT_COMMAND` at `scripts/hermes-stt-wrapper.sh` instead of writing a complex inline shell one-liner in `.env`.
- If an inline command is unavoidable, remember Hermes runs Python `str.format()` over the command before shell execution. Shell parameter expansions with braces must be escaped as doubled braces, e.g. use `${{stem%.*}}` and `${{stem}}`, not `${stem%.*}` / `${stem}`; otherwise Hermes treats them as format placeholders and the wrapper logic fails before shell runs.
- `{input_path}` for Telegram `.ogg` voice is usually a Hermes-created temp `.wav`, not the original `.ogg`. Directly feeding `{input_path}` to Yandex direct STT can exceed the 1 MB limit or fail format negotiation; recover the original cached `.ogg` by stem first.
- Check both common cache roots when reconstructing originals: `$HERMES_HOME/audio_cache/audio_<id>.ogg` and `$HERMES_HOME/cache/audio/audio_<id>.ogg`.
- After editing the shell wrapper via skill/file tooling, verify it is executable (`chmod 775` or equivalent when appropriate); otherwise `local_command` can fail with `Permission denied` before transcription runs.
- `HERMES_LOCAL_STT_COMMAND` and Yandex credentials must be visible in the gateway process environment because this path uses `os.getenv()`; agent/tool-runner env alone is not enough.

TTS:

```bash
python3 skills/yandex-speech/scripts/tts.py "Hello from Yandex" --output /tmp/yandex-hello.mp3
```

TTS from file:

```bash
python3 skills/yandex-speech/scripts/tts.py \
  --input-file ./text.txt \
  --voice alena \
  --format mp3 \
  --output /tmp/out.mp3
```

Direct STT:

```bash
python3 skills/yandex-speech/scripts/transcribe.py ./sample.mp3 \
  --mode direct \
  --format json \
  --output /tmp/transcript.json
```

Async STT via Object Storage:

```bash
python3 skills/yandex-speech/scripts/transcribe.py ./meeting.mp3 \
  --mode async-s3 \
  --diarize \
  --format srt \
  --output /tmp/meeting.srt
```

## Behavior notes

### TTS

`tts.py`:
- reads API key and folder id from env;
- supports `text` or `--input-file`;
- supports `lang`, `voice`, `format`, `speed`, `emotion`;
- splits long text into chunks;
- retries transient request failures and common rate-limit/server errors;
- can concatenate MP3 chunks with `ffmpeg` when available.

Voice name pitfall: the Russian male voice is `ermil`, not `ermis`. The API returns `BAD_REQUEST / Unsupported voice` for typos. Common Russian voices: `ermil` (male), `alena` (female, default), `filipp` (male). When a user's memory profile specifies a chosen TTS voice, use that exact string — do not guess or adapt it.

When the user sends voice messages and expects voice replies, prefer Yandex TTS (via this skill) over the built-in `text_to_speech` tool. The built-in Edge TTS can intermittently fail with "No audio was received" while Yandex SpeechKit remains stable. Use `--format oggopus` for Telegram-compatible output.

### STT

`transcribe.py` supports three modes:
- `auto` — default mode; tries `direct` first, then falls back to ~25s chunked direct recognition when Yandex rejects direct input because it is over 1 MB or over 30 seconds. Use this for Telegram voice/manual recovery and Hermes gateway wrapper integration.
- `direct` — simple direct request for shorter files and quick checks; auto-transcodes unsupported compressed input to OGG/Opus when `ffmpeg` is available.
- `async-s3` — upload to Object Storage + poll long-running recognition when Object Storage credentials are configured and true long-running recognition is required.

Output formats:
- `json`
- `txt`
- `srt`
- `vtt`

### Long recordings

For long recordings, prefer `async-s3`.

If the required S3 environment variables are missing, fail fast with a clear error instead of guessing configuration.

## When to read docs/

Read `docs/issue-11-stt.md` and `docs/issue-12-tts.md` only when you need the original task context or acceptance criteria that motivated this skill.
