---
name: yandex-speech
description: Transcribe uploaded audio/video with Yandex SpeechKit, generate speech, choose voices and pronunciation, and connect Yandex STT/TTS to stock Hermes. Supports genuine timing and two anonymous speakers, durable recorded-file recovery and short arriving-audio recognition. Use the host's normal speech tool for ordinary voice replies.
---

# Yandex Speech

Use this skill for uploaded recordings, Yandex-specific synthesis, voice selection and setup. It includes the accepted Discord conversation integration on unmodified Hermes 0.23.0. Live meeting recording and its start/stop controls are next-release work; short streaming sessions do not provide that feature.

## Work from the addressed input

- Identify the attachment, source and requested result before recognition. Never substitute an older cache file. Accept any audio/video the decoder can read and prepare it internally; do not ask the user to convert it manually.
- Preserve original inputs. Existing outputs need explicit overwrite. Keep accepted/uncertain job receipts and resume them instead of creating another billable job. Only owned temporary artifacts/uploads may be cleaned up.
- Keep genuine timing, speaker scope and source identity. Native mixed-recording diarization supports two anonymous speakers; partitioned labels do not identify the same person across jobs. Never invent timestamps, people or missing words.

## Set up and choose a route

Use plain `python` from the configured standard Hermes environment; no hardcoded/private interpreter. Run the [local readiness check](references/yandex-cloud-setup.md) for provider-specific setup or diagnosis. A present key does not prove permission; report a denied/unverifiable operation without guessing which grant is absent.

The account helper can inspect metadata, prepare a plan and create authorized API/static keys. It returns a generated response filename under `~/secrets` by default; `--secret-out PATH` selects another new file. Existing response files are never overwritten. After successful creation/save, call the separate `apply-key --secret-file RETURNED_PATH --env-file "$HERMES_HOME/.env"` for your own profile. Follow the [exact variable mappings and env section](references/yandex-cloud-setup.md#apply-the-saved-key-to-the-agents-env); preserve existing `YANDEX_CLOUD_FOLDER_ID` shared with Yandex Search.

For S3, call `setup-storage` with the saved static response and your own env, then carry its returned bucket/prefix into `apply-key`. The skill defaults to bucket template `yandex-speech-<service-account-id>` and prefix `speechkit`. Env bucket/prefix override saved/default settings; check/setup exactly those coordinates. Explicit storage setup creates the selected resources only on confirmed absence, without changing IAM grants. Endpoint stays in skill configuration, not env. Key creation does not change buckets or retire old keys.

Gateway variables load at startup: until its next start, explicitly supply the new variables in immediate speech-generation subprocesses. Saving env does not refresh the running gateway.

Recorded automatic recognition prefers API v3 over gRPC, then compatible REST. A saved bucket is available capacity, not a storage-first preference: use inline content when admitted. S3 is required for v2 async or for a v3 whole-file job beyond inline admission that still fits the storage bounds; explicit URI/`use_storage=True`/`--bucket` selects it deliberately. Missing gRPC libraries alone do not require S3. Explicit version, timing, speaker and literary requirements are preserved. Text-only v1 fallback requires compatible explicit choices; a short-input fast path never silently changes the requested output. Use [recording options](references/transcription.md) for formats, limits, explicit partitioning and recovery.

## Preserve preferences

Read `$HOME/.config/yandex-speech/config.json`, relative to the active workspace in Hermes. Only explicit `--config PATH` selects another file. Precedence is explicit call → user file → adapter defaults → built-ins. For storage bucket/prefix specifically, env overrides the saved user file and adapter defaults; explicit call options remain highest. Existing voice/language aliases keep their documented default level. Do not switch process-global HOME between profiles.

`YANDEX_SPEECH_API_KEY` or the owning host secret resolver supplies SpeechKit authentication. Upload credentials are separate. Service-account SpeechKit calls do not send a folder ID. Recorded Russian recognition defaults to API v3, literary text enabled, profanity masking off and phone formatting off.

## Voice replies and pronunciation

Use the host's standard speech tool and addressed delivery for an ordinary voice reply. Generating a file, sending a voice attachment and speaking in a connected channel are different results: verify the requested one. Do not replace an explicitly requested native-tool operation with a standalone script and call that route repaired. Respect recipient opt-outs and the host conversation policy.

Keep the user's literal saved voice/model/style. Ermil's `good` differs from `friendly`; incompatible choices must be diagnosed rather than silently substituted. Complete-file synthesis defaults to `file_method=auto`: filter complete ready operations, then prefer gRPC v3 streamed file, gRPC v3 utterance, REST v3 utterance and compatible REST v1. Honor fixed API/transport/method controls, enable unsafe mode only explicitly, and never replay submitted or uncertain speech through another route. Use the [complete voice catalog and refresh procedure](references/synthesis-provider-testing.md#complete-official-voice-catalog) to check API version, general/LiveTTS family, language and roles. Check the current official source before listing available voices or selecting an unfamiliar one; update the full list and routing catalog together when it changes. State the last verification date if the source cannot be checked. Ordinary host cleanup is not a raw-SSML interface.

In Hermes, explicit XML pronunciation requests use this plugin's `yandex_speech_ssml` tool with one complete `ssml` speak document. Optional `voice`, `role`, `language`, `model` and `speed` choices override saved defaults; this operation uses REST v1 and rejects incompatible v3/LiveTTS choices. The built-in `text_to_speech` tool remains a plain/native-text interface. If the user explicitly requires that exact built-in tool with raw XML, report its limitation rather than claiming a different operation repaired it.

On successful SSML generation, emit the returned `media_tag` FIELD VALUE verbatim in the final reply, outside JSON, quotes or code. Do not send the same audio through another tool. A custom tool result is not automatically recovered when the assistant omits that directive. A generated file does not establish delivery, hearing or speech in a connected Discord voice channel.

Generate previews only when requested. For a requested quality comparison use the same complete text, label voice/style/route, retain the submitted markup with the audio and obtain a listening verdict. A fixed sample pack and mandatory listening are not catalog or release requirements. API success, file size or an STT round trip does not establish pronunciation quality. Preserve canonical written names separately from verified spoken aliases.

## Essential commands

Set `YS` once to the installed skill directory (replace `<path-to-skill>` below). These commands work outside checkout.

```sh
YS='<path-to-skill>'
python "$YS/scripts/speech_setup.py" --check
python "$YS/scripts/transcribe.py" ./recording.mp3 --mode async --diarize --timestamps --format srt --output ./out/recording.srt
python "$YS/scripts/transcribe.py" --resume ./out/recording.job.json --format json --output ./out/recording.json
python "$YS/scripts/transcribe.py" ./large.mp3 --mode async --api-version 3 --transport rest --split-inline --timestamps --format json --output ./out/partitioned.json
python "$YS/scripts/tts.py" "Привет, мир" --voice ermil --emotion good --format oggopus --output ./out/reply.ogg
```

`--version` needs no credentials. Use `--help` for operation-specific controls. Recorded exports are TXT/JSON/SRT/VTT; SRT/VTT require genuine timing. Short arriving recognition exports TXT/JSON and is bounded to 240 seconds. Synthesis includes v1/v3 files, explicit LiveTTS and incremental v3 PCM; a PCM generator alone is not a playback sink.

[Quick start](README.md) · [Hermes connection](references/hermes.md) · [Account setup](references/yandex-cloud-setup.md) · [Media preparation](references/media-preparation.md).
