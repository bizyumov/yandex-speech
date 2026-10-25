---
name: yandex-speech
description: Transcribe recorded or arriving audio with Yandex SpeechKit, export transcripts with genuine timing and up to two anonymous speakers, synthesize speech files, and bind transcription to Hermes. Use for voice-message transcription, recorded meetings/media, voice replies or recognition while audio arrives. Diagnose available API/dependency/permission options before submitting; never guess the addressed recording.
---

# Yandex Speech

This skill supplies speech-transcription and the existing speech-synthesis entry points. Hermes is an optional host adapter. Phase 1 implements gRPC v3 recorded/arriving recognition, durable recovery, automatic media preparation and native Hermes STT. New REST/storage fallback, new synthesis APIs, full spoken-turn controls/playback and public rollout are later phases; do not claim those are delivered.

## Agent workflow

1. Identify the addressed attachment, recording or arriving source and requested output. Use the current message's attachment identity. Never select an older/newest cache item merely because the intended attachment has not arrived. Resolve ambiguity before submitting a different source. Recognition is evidence, not an authoritative verbatim quote; do not invent missing words for editing workflows.
2. Run [readiness](references/yandex-cloud-setup.md). Inspect only credential names/presence, selected non-secret configuration, dependencies, required API capabilities and permission evidence. The agent owns these checks and explains the exact missing role/scope/prerequisite and remedy. A present key or a 403 alone does not establish IAM grants. Arrange authorized setup with the service-account/resource owner; automated key creation is Phase 2.
3. Explain the available [API options](references/transcription.md#api-options) when a prerequisite/feature changes the choice. Prefer gRPC v3 for recordings and arriving recognition. The planned fallback chain is gRPC v3 → REST v3 + S3 → FFmpeg split + REST v1; only the first new tier is implemented in Phase 1. Explicit legacy REST methods remain usable with their declared limitations. Do not silently discard required formatting, times or speakers or change a fixed API version.
4. Accept any audio/video format the available decoder can read. Probe actual content, select the first audio track unless another is explicit, and automatically convert to the selected provider representation. Do not ask the user to manually convert FLAC/AAC/AMR/video to Ogg. Headerless audio needs its actual sample representation/rate/channel count. See [media preparation](references/media-preparation.md).
5. Save output to an addressed durable path. The CLI saves a private `<output-stem>.job.json` recovery pointer. Retain accepted/uncertain receipts; resume rather than creating another billable job. An uncertain submit is never automatically retried. Existing outputs require explicit overwrite; original media/text remains protected. Only invocation-owned temporary artifacts are cleaned up.
6. Return the resulting path and relevant limitations. For native host calls, a complete recognition result becomes eligible input under the host's addressed context; partial text is a preview. Synthesis/delivery/player/recipient permission are separate checks. Respect voice opt-outs and the host's conversation policy.

## Commands

Use the installed skill root in place of `/absolute/skill`. All development, execution and testing use the standard Hermes environment, `/opt/hermes/venv/bin/python`.

```bash
/opt/hermes/venv/bin/python /absolute/skill/scripts/speech_setup.py --check
/opt/hermes/venv/bin/python /absolute/skill/scripts/transcribe.py ./audio.flac --format txt --output ./out/transcript.txt
/opt/hermes/venv/bin/python /absolute/skill/scripts/transcribe.py ./meeting.mp4 --mode async --audio-track 0 --diarize --timestamps --format srt --output ./out/meeting.srt
/opt/hermes/venv/bin/python /absolute/skill/scripts/transcribe.py --resume ./out/meeting.job.json --format json --output ./out/recovered.json
/opt/hermes/venv/bin/python /absolute/skill/scripts/transcribe.py - --stream --audio-format lpcm --sample-format s16le --sample-rate 16000 --channels 1 --format json --output ./out/utterance.json < ./utterance.pcm
/opt/hermes/venv/bin/python /absolute/skill/scripts/tts.py --input-file ./reply.txt --format oggopus --output ./out/reply.ogg
```

`--version` needs no credentials or output. Recording output is JSON/TXT/SRT/VTT; arriving recognition supports JSON/TXT. SRT/VTT requires genuine provider timing. Native mixed-recording diarization has a **platform limit of two anonymous speakers**. More-than-two speaker support is future work, [issue #27](https://github.com/bizyumov/yandex-speech/issues/27); never invent people or cross-job identities.

## Defaults, secrets and configuration

- Service-account `YANDEX_SPEECH_API_KEY` is required for cloud calls. Never print/copy it into reports, receipts, issue bodies, config JSON or command arguments. A scoped Hermes secret resolver precedes environment fallback. Existing `folder_id` / `YANDEX_CLOUD_FOLDER_ID` remain optional compatibility fields, not request fields.
- Load `$HOME/.config/yandex-speech/config.json`, or the same path under the active Hermes workspace. Only explicit `--config PATH` chooses another file. No CWD/repo/config-ENV selector is supported. Invalid existing/explicit config fails before RPC.
- Precedence: explicit call/CLI → user file → adapter defaults → built-ins. `HERMES_TTS_VOICE`, `HERMES_TTS_LANG` and `YANDEX_SPEECH_S3_*` stay adapter defaults. Wrappers do not promote these aliases or host language into explicit overrides. Do not change global HOME to switch profiles.
- Recording/arrival defaults: gRPC v3, Russian `ru-RU`, literature_text=true with effective normalization, profanity masking false, phone formatting disabled. Explicit false selects raw finals. V1 cannot supply literary style; an intentionally selected v1/direct route reports that unavailable capability.
- Existing synthesis defaults: REST v1, alena, ru-RU, MP3, speed 1.0, neutral. Honor configured voice strings exactly (`ermil`, for example); do not guess a replacement. New v3 synthesis/SSML/progressive delivery is Phase 3. Legacy multipart synthesis outside MP3 may return part files; do not claim one complete voice delivery until the host has verified it.

[Transcription/configuration](references/transcription.md) · [Media preparation](references/media-preparation.md) · [Readiness and permissions](references/yandex-cloud-setup.md) · [Hermes adapters](references/hermes.md).
