---
name: yandex-speech
description: Transcribe recorded or arriving audio with Yandex SpeechKit, export transcripts with genuine timing and up to two anonymous speakers, synthesize speech files, and bind transcription to Hermes. Use for Yandex-specific voice transcription, recorded meetings/media, synthesis-provider work or recognition while audio arrives. Ordinary voice replies use the host's standard speech tool first; this skill is not a requirement to run a separate synthesis script. Diagnose provider prerequisites before provider-specific submissions; never guess the addressed recording.
---

# Yandex Speech

This skill supplies speech-transcription and the existing speech-synthesis entry points. Hermes is an optional host adapter. Recorded recognition supports v3 over gRPC or REST, explicit REST v2, private owned uploads, durable recovery and automatic media preparation. Arriving recognition uses bounded gRPC v3 sessions and native Hermes STT. REST v1/v3 complete-file synthesis, explicit LiveTTS, v1 SSML and gRPC incremental PCM synthesis are implemented; generation, file delivery and speaking acceptance are distinct. Continuous capture, full spoken-turn controls/playback and public rollout remain separate.

## Speech quality and voice choice

Use the [synthesis guide](references/synthesis-provider-testing.md) to choose REST v1 SSML or gRPC v3 general/LiveTTS voices, pronounce terms, place stress marks and preserve voice-specific roles. Ermil's good role is distinct from friendly. Keep API success, acoustic quality, a voice attachment and speaking in a channel as separate outcomes.

## Route ordinary voice replies first

1. First distinguish sending text, sending a voice message/attachment, and speaking in the connected voice channel. For a voice-message/attachment request, use the standard `text_to_speech` tool and native message delivery. For an explicit request to speak, use the running host's voice-channel playback path and verify speaking; a file is not successful speaking. Hermes 0.22.5 has queued/event-context routing limitations described in [the delivery contract](references/hermes.md#sending-text-sending-voice-and-speaking). Distinguish an ordinary audio attachment, a native voice message and live voice-channel playback: producing one does not satisfy an explicit request for another. Do not infer live-channel intent merely because the user rejects a file attachment. Do not turn an ordinary reply into provider readiness checks, CLI invocations or manually written text files.
2. Treat a request to use the standard tool as a route-specific acceptance criterion. Do not silently replace a failed native call with this skill's CLI: that may produce audio but does not test or repair the requested route. Use provider-specific scripts for an explicitly requested Yandex operation, an authorized diagnostic probe or an accepted fallback.
3. Preserve the requested male voice through the supported host configuration. A provider-specific voice preference is not permission to bypass the host mechanism; if that route cannot honor the preference, state the limitation rather than inventing a voice selection or changing persistent settings.
4. Do not equate an exposed tool schema with a configured, available synthesis backend. On failure, report the attempted route, returned error and whether audio was produced. Keep the cause unconfirmed until configuration/dependency evidence establishes it; a successful standalone Yandex call does not establish native-tool readiness.

## Provider-specific workflow

Apply the following steps to Yandex provider work, not as prerequisites for an ordinary host voice reply.

1. Identify the addressed attachment, recording or arriving source and requested output. Use the current message's attachment identity. Never select an older/newest cache item merely because the intended attachment has not arrived. Resolve ambiguity before submitting a different source. Recognition is evidence, not an authoritative verbatim quote; do not invent missing words for editing workflows.
2. Run [readiness](references/yandex-cloud-setup.md). Inspect only credential names/presence, selected non-secret configuration, dependencies, required API capabilities and permission evidence. The agent owns these checks and explains the exact missing role/scope/prerequisite and remedy. A present key or a 403 alone does not establish IAM grants. Arrange authorized setup with the service-account/resource owner; automated key creation is Phase 2.
3. Explain the available [API options](references/transcription.md#api-options) when a prerequisite/feature changes the choice. Recorded auto selection follows gRPC v3 → REST v3 with private storage/eligible inline → raw-text v1 windows only when version, transport and required features permit. A short unformatted input may use one direct v1 request; an accepted/uncertain job never triggers another submission. Helper uploads require separate storage access; unavailable optional upload access does not exclude eligible inline recognition. Fixed v2 supplies genuine timing but no native speaker labeling. Over-limit asynchronous partitioning remains unfinished. Arriving recognition requires gRPC. Do not silently discard required formatting, times or speakers or change a fixed API version.
4. Accept any audio/video format the available decoder can read. Probe actual content, select the first audio track unless another is explicit, and automatically convert to the selected provider representation. Do not ask the user to manually convert FLAC/AAC/AMR/video to Ogg. Headerless audio needs its actual sample representation/rate/channel count. See [media preparation](references/media-preparation.md).
5. Save output to an addressed durable path. The CLI saves a private `<output-stem>.job.json` recovery pointer. Retain accepted/uncertain receipts; resume rather than creating another billable job. An uncertain submit is never automatically retried. Existing outputs require explicit overwrite; original media/text remains protected. Only invocation-owned temporary artifacts are cleaned up.
6. Return the resulting path and relevant limitations. For native host calls, a complete recognition result becomes eligible input under the host's addressed context; partial text is a preview. Synthesis/delivery/player/recipient permission are separate checks. Respect voice opt-outs and the host's conversation policy.

## Voice diagnostics and samples

1. Separate the routes before testing: inbound STT, outbound TTS, native voice-message or live-channel delivery, host attachments and player rendering. Inspect the active profile's selected providers and exact command/native adapters using allowlisted non-secret fields. Also inspect supported session-level voice overrides and the running gateway's effective state before contradicting an operator who says automatic voice is enabled; a configuration-file value establishes only that file's value. Never infer the gateway's route from a standalone skill default, an absent tool in the agent's schema or from receiving a text message.
2. When asked to test the API, perform an authorized synthetic provider call, not just readiness. For a requested version/model, use that route directly; an unsupported legacy CLI is not a test of the newer API. Record actual method, transport, model, requested voice and provider completion evidence. A successful STT v3 test says nothing about TTS v3 support.
3. Exercise the complete configured wrapper command, including chained output handling, then inspect the resulting transcript or audio. Distinguish this synthetic test from tracing an actual addressed platform message. Do not substitute repeated reports of missing transcript paths for the assigned route diagnosis; report the missing utterance briefly and continue independent authorized checks.
4. For voice comparisons, synthesize the same short text with explicit voice identifiers, label attachments in the requested order and measure each final file's duration. Request a host-compatible final format directly where supported; if conversion is necessary, perform it internally rather than requiring another agent round-trip. Verify the final container, and distinguish successful decoding from user-confirmed playback or duration display. Do not attribute a player issue to WAV headers without evidence.
5. If the host TTS tool is absent or fails, distinguish that route's failure from provider-wide unavailability. Inspect the supported CLI/wrapper only when diagnosing the provider or when fallback delivery is within scope; do not substitute it for an explicitly requested native-tool test. A successful experimental provider probe does not implement or verify a native adapter, persistent voice setting or configured gateway route. See [Hermes route diagnosis](references/hermes.md#voice-reply-route-diagnosis).

For pronunciation and SSML samples, show the complete submitted markup inline with the audio on the first reply; preserve the same text across compared voices and label variants in playback order. Use the requested male voice for this user's spoken replies; do not substitute a female default when a requested voice is incompatible. When explicitly asked for an audio or native voice message, deliver the actual spoken answer through the requested supported host route rather than merely explaining that automatic voice mode is unverified. When the operator has selected live voice-channel speech, do not attach an audio file as a substitute: exercise and verify the host's live playback route. Preserve that delivery choice for subsequent replies and timers; distinguish one-off playback from changing persistent gateway settings. Use `hermes-messaging-gateway-operations` for live-channel and scheduled-delivery diagnosis, not standalone provider synthesis. Distinguish provider acceptance from measured pauses and listener-confirmed pronunciation. When speaker playback is picked up by incoming STT, do not treat the repeated sample text alone as a new instruction or successful acoustic evaluation.

For names and stress, retrieve the requested roster first, then verify each pronunciation separately before synthesis. If the user asks for all names with correct pronunciations, finish the roster/evidence ledger before presenting a full authoritative sample; regenerating a guessed list with disclaimers does not satisfy the request. Keep canonical identifiers, operator-selected spoken names and source-language pronunciation distinct; do not replace an agent's stated name with an agent-invented transliteration. Open the cited source and locate the exact headword and pronunciation; label a conclusion from a general stress rule as an inference, not a direct name-specific lookup. When challenged, inspect the evidence and correct or regenerate the affected sample in the same task instead of repeatedly replying that verification was omitted.

See [synthesis provider testing](references/synthesis-provider-testing.md) for LiveTTS request/response handling, SSML pronunciation recipes and acceptance boundaries.

## Diagnostic handoffs and deferred work

For a requested developer handoff, read the existing synthesis issue/specification and map the observed tests against its acceptance criteria. Mark untested work explicitly; do not close an implementation criterion from a few provider samples. Store session working documents in the existing project folder of the agent workspace, not the installed/shared skill repository, unless product-repository publication was explicitly requested. Publishing an issue comment does not authorize staging the local report in the product repository. Sanitize public comments separately, excluding local paths, private session/account/request identifiers and operational payloads. Read back the exact published issue/comment before reporting success. A registration-only or deferred issue request does not authorize implementation or live configuration changes.

## Long meetings and monologues

Native server streaming is a required pre-release STT capability tracked in
[#37](https://github.com/bizyumov/yandex-speech/issues/37). The current
240-second stream handles one bounded utterance; do not advertise it as a
complete continuous meeting recorder. Source audio must reach the native
provider while captured, with correct finalization, connection renewal,
recovery and transcript continuity. Use the
[recording/streaming guide](references/transcription.md#required-continuous-server-streaming)
to distinguish this from recorded-file recognition. Investigated settings,
existing short-stream tests and actual long-session support are different
evidence; do not change unrelated busy-message or notification options.

## Commands

Use the installed skill root in place of `/absolute/skill`. Inside Hermes, use plain `python`; its configured `VIRTUAL_ENV` and `PATH` already select the standard runtime. Never hardcode or prepend an interpreter path. Development and tests use this same configured environment, never a private/project venv.

```bash
python /absolute/skill/scripts/speech_setup.py --check
python /absolute/skill/scripts/transcribe.py ./audio.flac --format txt --output ./out/transcript.txt
python /absolute/skill/scripts/transcribe.py ./meeting.mp4 --mode async --audio-track 0 --diarize --timestamps --format srt --output ./out/meeting.srt
python /absolute/skill/scripts/transcribe.py ./meeting.mp3 --mode async --transport rest --api-version 3 --timestamps --format json --output ./out/rest.json
python /absolute/skill/scripts/transcribe.py ./meeting.mp3 --mode async --transport rest --bucket EXISTING_PRIVATE_BUCKET --timestamps --format json --output ./out/stored.json
python /absolute/skill/scripts/transcribe.py --resume ./out/meeting.job.json --format json --output ./out/recovered.json
python /absolute/skill/scripts/transcribe.py - --stream --audio-format lpcm --sample-format s16le --sample-rate 16000 --channels 1 --format json --output ./out/utterance.json < ./utterance.pcm
python /absolute/skill/scripts/tts.py --input-file ./reply.txt --format oggopus --output ./out/reply.ogg
```

`--version` needs no credentials or output. Recording output is JSON/TXT/SRT/VTT; arriving recognition supports JSON/TXT. SRT/VTT requires genuine provider timing. Native mixed-recording diarization has a **platform limit of two anonymous speakers**. More-than-two speaker support is future work, [issue #27](https://github.com/bizyumov/yandex-speech/issues/27); never invent people or cross-job identities.

## Defaults, secrets and configuration

- Service-account `YANDEX_SPEECH_API_KEY` is required for cloud calls. Never print/copy it into reports, receipts, issue bodies, config JSON or command arguments. A scoped Hermes secret resolver precedes environment fallback. Existing `folder_id` / `YANDEX_CLOUD_FOLDER_ID` remain optional compatibility fields, not request fields.
- Load `$HOME/.config/yandex-speech/config.json`, or the same path under the active Hermes workspace. Only explicit `--config PATH` chooses another file. No CWD/repo/config-ENV selector is supported. Invalid existing/explicit config fails before RPC.
- Precedence: explicit call/CLI → user file → adapter defaults → built-ins. `HERMES_TTS_VOICE`, `HERMES_TTS_LANG` and `YANDEX_SPEECH_S3_*` stay adapter defaults. Wrappers do not promote these aliases or host language into explicit overrides. Do not change global HOME to switch profiles.
- Recorded recognition defaults to API v3, preferring gRPC when available and otherwise REST. Arriving recognition requires gRPC v3. Russian `ru-RU`, literature_text=true with effective normalization, profanity masking false and phone formatting disabled remain defaults. Explicit false selects raw finals. V1 cannot supply literary style; an intentionally selected v1/direct route reports that unavailable capability.
- Existing synthesis defaults: REST v1, alena, ru-RU, MP3, speed 1.0, neutral. Honor configured voice strings exactly (`ermil`, for example); do not guess a replacement. Explicit v3 requests preserve model/voice/transport. File synthesis assembles one complete requested container, retains incomplete parts on failure, and finalizes WAV length placeholders internally. Native PCM streaming is v3/gRPC at 24 kHz mono signed-16-bit little-endian; file/attachment generation does not establish live playback acceptance.

[Transcription/configuration](references/transcription.md) · [Media preparation](references/media-preparation.md) · [Readiness and permissions](references/yandex-cloud-setup.md) · [Hermes adapters](references/hermes.md).

## Implemented synthesis commands

Use the ordinary Hermes speech tool for a normal reply. For an explicit provider/format request or standalone use, the supported commands below have executable/provider checks.

```bash
python /absolute/skill/scripts/tts.py "Проверка голоса." --api-version 3 --transport rest --model livetts --voice sergey --format mp3 --output ./reply.mp3
python /absolute/skill/scripts/tts.py --input-file ./marked-up.xml --text-format ssml --api-version 1 --voice ermil --format wav --output ./reply.wav
python /absolute/skill/scripts/tts.py --input-file - --stream --api-version 3 --transport grpc --model livetts --voice denis --format lpcm --output -
```

The last command consumes UTF-8 text fragments from stdin and emits headerless PCM progressively; a playback sink must know 24 kHz, mono, 16-bit little-endian. Safe v3 utterances are at most 250 Unicode code points and 24 seconds; unsafe mode is opt-in. V1 requests obey both 5,000-code-point and 15,000-encoded-byte limits. Multipart file output is assembled internally; do not add a separate agent WAV-to-MP3 step. LiveTTS MP3 is requested directly. WAV streaming placeholders are finalized without re-encoding. LiveTTS speed, pitch and duration controls are unavailable; do not substitute a different model or voice.
