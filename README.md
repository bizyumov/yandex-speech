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

An agent skill for transcribing uploaded audio/video, generating speech and connecting Yandex SpeechKit to stock Hermes 0.23.0. Root [SKILL.md](SKILL.md) is the skill entry. Existing Discord conversation is included; live meeting recording and its start/stop controls are deferred to the next release.

## Start here

Use the configured standard Hermes environment and plain `python`. The runtime owner installs the declared base requirements and, when needed, the optional gRPC/storage profiles. The skill never installs dependencies automatically. FFmpeg/ffprobe handle media preparation; no project-specific virtual environment is needed.

Set `YANDEX_SPEECH_API_KEY` through the owning runtime's secret mechanism. Keep credentials out of chat, command arguments and configuration JSON.

```sh
YS='<path-to-skill>'
python "$YS/scripts/speech_setup.py" --check
python "$YS/scripts/transcribe.py" ./recording.mp3 --mode async --diarize --timestamps --format srt --output ./out/recording.srt
python "$YS/scripts/transcribe.py" --resume ./out/recording.job.json --format json --output ./out/recording.json
python "$YS/scripts/tts.py" "Привет, мир" --voice ermil --emotion good --format oggopus --output ./out/reply.ogg
```

Set `YS` once to the installed skill directory by replacing `<path-to-skill>` above. Original inputs are preserved; replacing output requires `--overwrite`. The recovery pointer is `<output-stem>.job.json`. Commands work outside checkout. `--check` is local/read-only; only explicit `--probe-audio` submits an audio diagnostic.

## Preferences and limits

Saved preferences live in `$HOME/.config/yandex-speech/config.json`, relative to the active workspace in Hermes. Explicit call options take priority, then the selected user file, adapter defaults and built-ins. Only `--config PATH` changes the selected file.

Recorded recognition prefers API v3 through gRPC or REST and enables literary formatting by default. A fixed API choice, timing or speaker requirement is never silently dropped. Long/oversized recordings have explicit partitioning and durable recovery; see the transcription command's `--help` and [recording options](references/transcription.md). A single storage job supports up to four hours; partitioned speaker labels remain local to each job. Native mixed-recording diarization supports two anonymous speakers.

Short arriving-audio recognition is limited to 240 seconds per session. It does not provide the deferred live-meeting recording mode. Existing Discord streaming through the plugin is retained.

## Voices and setup

Choose a supported voice/style using the [voice catalog and pronunciation examples](references/synthesis-provider-testing.md). Ermil's `good` style is distinct from another voice's `friendly`. File synthesis supports REST v1/v3 and gRPC v3; SSML uses v1, while LiveTTS uses v3. Keep explicit saved choices instead of silently substituting a voice or model.

For XML pronunciation in Hermes, the plugin supplies `yandex_speech_ssml` and the normal voice-attachment delivery protocol. The built-in speech tool retains its plain-text behavior; see [the SSML route and delivery rule](references/hermes.md#preferences-and-voice-delivery).

[Hermes connection](references/hermes.md) uses the native plugin/configuration CLI. [Account/key setup](references/yandex-cloud-setup.md) covers read-only inspection, a setup plan and explicitly authorized key creation into a new protected file. No Hermes configuration backend or platform fork is required.

## Versioning

Releases use YYYY.M.D[-N], with unpadded month/day and an optional revision suffix, for example 2026.11.9. VERSION is authoritative for package, CLI, plugin and release identity. Release tags use the same identifier.

## Checks

```sh
python -m tests --phase 3 --profile grpc
python -m build --no-isolation
```

Run from checkout in the standard runtime. CI makes no provider calls; live provider/host checks require explicit synthetic fixtures and authorization. See [changelog](CHANGELOG.md), [MIT license](LICENSE) and [acceptance coverage](tests/acceptance.json).
