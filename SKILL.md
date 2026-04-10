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
- `requirements.txt` — Python dependencies for this skill
- `docs/issue-11-stt.md` — full GitHub issue #11 text
- `docs/issue-12-tts.md` — full GitHub issue #12 text

## Quick start

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

### STT

`transcribe.py` supports two modes:
- `direct` — simple direct request for shorter files and quick checks; auto-transcodes unsupported compressed input to OGG/Opus when `ffmpeg` is available;
- `async-s3` — upload to Object Storage + poll long-running recognition.

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
