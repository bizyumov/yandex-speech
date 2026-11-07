# The business scenarios

These are the real-life scenarios where the yandex-speech SKILL is used, notably new discord sceanario.

1. push-to-talk messaging (in a messenger platform e.g. telegram). I send a voice message (taking notes, sending commands, when voice messagine is more convenient than texting)
2. receive voice reply (LLM converts text reply into speech e.g. when driving a car or otherwise listening is more convenient than reading)
3. transcribing audio records (meeting, monologue, podcast, webinar etc. when i have recorded audio and want a text equivalent, including speakers)
4. NEW! voice chatting (in a voice communication platform e.g. discord, when back-and-forth exchange speed and seamlessness is key)

Audio is either files or stream. The default audio file/stream vehicle for all these scenarios is hermes autonomous agent, which does not exclude other agentic platforms. So the SKILL provides native support for hermes, which in turn natively supports telegram, discord etc. and is thus very convenient.

For more info on hermes voice support: https://www.youtube.com/watch?v=fVAnKu_9ZuI. Of special note:

- streaming support (stt.streaming: true)
- stop word
- wake words ("hey Hermes")



# Yandex Speech

An agent skill for Yandex SpeechKit. This branch implements development Phase 1: the skill baseline, gRPC v3 speech transcription and a native Hermes 0.22.5 STT binding. VERSION is 0.1.0; no public release/tag is implied. Read [SKILL.md](SKILL.md) for the agent workflow.

| User scenario | Capability used | Implemented baseline |
|---|---|---|
| push-to-talk messaging | speech-transcription; spoken-turn-handling | Recorded attachment recognition and native STT binding. Complete addressed conversation integration is Phase 4. |
| receive voice reply | speech-synthesis; spoken-turn-handling | Current worktree adds v1/v3 complete files, LiveTTS, SSML and incremental gRPC PCM. Standard Hermes tool binding is exercised; complete playback integration remains separate. |
| transcribing audio records | speech-transcription | gRPC v3 recording jobs, recovery, any locally decodable media, genuine timing, two anonymous speakers, TXT/JSON/SRT/VTT. |
| voice chatting | speech-transcription; speech-synthesis; spoken-turn-handling | Recognition while audio arrives and native Hermes STT. Complete capture → agent → progressive reply → playback is Phase 4. |

## Use

Keep the checkout in the platform's configured skills directory so its root SKILL.md is discovered. Set `YANDEX_SPEECH_API_KEY` through the runtime's secret mechanism. The key belongs to a service account; a folder ID is optional legacy configuration and is not sent with these calls.

Inside Hermes, development, execution and tests use plain `python` from the configured `VIRTUAL_ENV` and `PATH`. Never hardcode or prepend a venv path. Have the runtime administrator admit the dependencies declared by `requirements.txt` and optional `requirements-grpc.txt` into that environment; do not create a project venv. The skill never installs dependencies implicitly. FFmpeg and ffprobe are required for media probing/conversion; native described PCM streams need neither.

```bash
python /absolute/skill/scripts/speech_setup.py --check
python /absolute/skill/scripts/transcribe.py ./meeting.mp3 --mode async --diarize --timestamps --format srt --output ./out/meeting.srt
python /absolute/skill/scripts/transcribe.py --resume ./out/meeting.job.json --format json --output ./out/meeting.json
python /absolute/skill/scripts/tts.py "Привет, мир" --format oggopus --output ./out/reply.ogg
```

Replace `/absolute/skill` with the installed skill root. Commands work outside the checkout. Output replacement requires `--overwrite`; source files remain protected. The default recovery pointer is `<output-stem>.job.json`; it points to private durable state and is sufficient for `--resume`.

## Defaults and limits

Read `$HOME/.config/yandex-speech/config.json`; Hermes binds this location to the active workspace. Only `--config PATH` selects another file. Explicit options override user-file values, which override adapter defaults, which override built-ins. Existing voice/language/S3 environment aliases stay adapter defaults. [Configuration and API options](references/transcription.md).

Recorded recognition defaults to **API v3**, preferring gRPC when available and otherwise REST, with `literature_text=true`, normalization enabled, profanity masking disabled and phone formatting disabled. A fixed API version never silently changes generation. For gRPC inline recognition, prepared duration is at most **14,400,000 ms** and the complete serialized protobuf is at most **60,000,000 bytes**. REST v3 applies that wire cap to the complete JSON including base64. Compatible storage objects may be at most **1,000,000,000 bytes** and **14,400,000 ms**. Streaming requires gRPC and uses 16 kHz mono signed-16-bit little-endian PCM internally, at most **240 seconds / 7,680,000 bytes**, with a five-second finalization budget. [Request, state and completion contracts](references/transcription.md), [media preparation](references/media-preparation.md).

Native mixed-recording diarization is limited to **two anonymous speakers by the platform**; more than two is a future [new-feature issue](https://github.com/bizyumov/yandex-speech/issues/27). Labels are request-local and do not identify people.

The intended recording fallback chain is **gRPC v3 → REST v3 + Object Storage → FFmpeg split + REST v1**. Dependency-based gRPC/REST selection, owned uploads and versioned v2/v3 recovery are implemented. Automatic v1 fallback and over-limit partitioning remain unfinished. Explicit v1 and v2/S3 entries preserve their documented capabilities; v2 supplies timing without native speaker labels. Missing gRPC never triggers installation without the runtime owner's authorization. [Available options and trade-offs](references/transcription.md#api-options).

## Integration and verification

[Hermes native STT and command wrappers](references/hermes.md); [credential/readiness procedure](references/yandex-cloud-setup.md). No productive profile is changed by import, tests or installation.

```bash
python -m tests --phase 3 --profile grpc
python -m tests --phase 1
python -m build --no-isolation
```

Run verification from the checkout. Provider/host acceptance requires explicit synthetic fixtures and authorized credentials; ordinary CI makes no provider calls. See [acceptance coverage](tests/acceptance.json). Synthesis supports REST v1/v3 files, gRPC v3 files and incremental PCM, v1 SSML, explicit LiveTTS and complete multipart output; see [synthesis commands and controls](references/synthesis-provider-testing.md). Later scope includes full REST/storage recognition fallback, account/key creation, complete spoken-turn controls/playback and public rollout. [Changelog](CHANGELOG.md), [MIT license](LICENSE).

Speech quality and voice selection: [v1 SSML, v3 general/LiveTTS voices and pronunciation](references/synthesis-provider-testing.md).
