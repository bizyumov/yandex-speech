# Hermes connection

The optional plugin in `integrations/hermes` registers Yandex transcription and synthesis. It targets unmodified official Hermes 0.23.0, tested at `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`. Other host releases need verification. The complete skill/Python package and its selected dependencies must be available in the standard runtime before a plugin-only install.

## Connect an authorized profile

Use the owning profile's normal Hermes environment. Replace COMMIT with the reviewed skill revision:

```sh
hermes plugins install bizyumov/yandex-speech/integrations/hermes --ref COMMIT --no-enable
hermes plugins enable yandex-speech
hermes config set stt.provider yandex
hermes config set stt.streaming true
hermes config set stt.yandex.language ru-RU
hermes config set tts.provider yandex
```

Keep an existing correct installation. A same-name `stt.providers.yandex` or `tts.providers.yandex` command entry takes precedence over the native provider. Inspect it and preserve its literal preferences before removing only the reconciled entry. For an Ermil/Good selection:

```sh
hermes config set tts.voice ermil --force
hermes config set tts.yandex.role good --force
hermes config unset tts.providers.yandex
hermes config get stt.provider
hermes config get stt.streaming
hermes config get tts.provider
```

The plugin-specific voice/style leaves use the native CLI's `--force` option because they are runtime-read plugin defaults outside its built-in key catalog. Inspect those exact leaves first. Do not remove an unrecognized command or change unrelated preferences automatically. No custom Hermes configuration writer is required. The removed skill enrollment switches are not setup commands. Use [account readiness/setup](yandex-cloud-setup.md) for credential problems; importing or testing the plugin does not change productive profiles.

## Preferences and voice delivery

The user file `.config/yandex-speech/config.json` is relative to the active workspace. It overrides profile defaults and host voice/model/language hints. Storage bucket/prefix are an exception: the owning env values override saved/default storage settings, while an explicit call remains highest. Storage endpoint belongs in skill configuration, not env. Credentials resolve in that same owning scope; another profile's ambient settings are excluded. Keep literal roles: Ermil supports `good`, while `friendly` belongs to compatible other voices.

Use the standard host speech tool for ordinary voice replies. Audio generation, an addressed attachment/voice message and playback in a connected channel are different results. Verify the requested result; a generated file alone does not prove delivery or playback. For explicit XML pronunciation, the same plugin adds the scoped `yandex_speech_ssml` operation; the built-in speech tool itself does not preserve XML.

Use `yandex_speech_ssml` with a complete `ssml` speak document and optional per-call voice/role/language/model/speed. It uses REST v1 and preserves compatible saved choices. Current profile defaults are read through the stock configuration API; no configuration is rewritten.

The tool returns generated audio and a `media_tag`. The assistant must emit the field VALUE directly in its final response, outside JSON/code/quotes. The gateway then handles its ordinary voice attachment. This custom tool is outside Hermes0.23's built-in omission-recovery allowlist: copying JSON or omitting the directive does not send audio. Generation, delivery and hearing remain separate. This operation does not speak into a connected Discord voice channel.

Native synthesis preserves existing nonempty files and symlinks, returns an artifact with its actual container extension and can publish a new sibling for an empty host allocation. File synthesis remains available through eligible REST routes without gRPC. Optional v3 PCM output requires compatible voice/settings and gRPC; PCM availability alone does not create a player.

## Complete files and host chunking

`speech.tts.file_method` defaults to `auto`, which filters complete ready operations and ranks gRPC streamed file, gRPC utterance, REST v3 utterance, then compatible REST v1. Set `utterance` or `stream` to constrain the producer explicitly. Saved API/transport/voice/model/role controls remain fixed; automatic routing never enables unsafe mode itself. Native `tts.yandex.file_method`, `api_version` and `transport` remain adapter defaults below the selected skill user file.

Streamed-file synthesis uses gRPC v3 internally and still returns a completed audio file to the normal Hermes tool. Progressive PCM uses the separate existing stream interface; a completed file is not speech in a connected voice channel.

For the native Yandex binding, stock Hermes 0.23.0's outer cap is 4,000 characters when no provider override applies. The skill then applies its own API/body budgets to each callback. A 5,500-character logical reply can therefore become two host callbacks even when each callback uses one streaming RPC. Report both counts; splitting does not move the complete reply into another length band.

Hermes supports a provider-specific `tts.yandex.max_text_length` override. A reviewed value of 10,000 permits the skill's own 5,000-character streaming admission/rollover to operate within a callback. This changes host partitioning, not SpeechKit's documented limits or the skill's per-RPC policy. Inspect the current setting and preserve the profile owner's choice; use the stock config CLI for an authorized change.

A same-name command entry can contain explicit `--api-version`, `--transport`, voice and role options. Those call options outrank the skill user file. An API-v1/REST command pin cannot be turned into gRPC streaming by editing a lower-priority JSON setting. When selecting the native binding, preserve the intended API/transport/voice/model/role as reviewed native defaults or user preferences, then remove only the reconciled command entry. Native defaults remain below the selected user file.

Successful native callbacks emit an allowlisted `Yandex native TTS receipt` in the owning profile logs: actual API/transport/method, voice/model/role, unsafe mode, submitted attempts, completed output parts, file readiness and exposed stream timings. It contains no speech text or credentials. Core readiness is local evidence; normal tool execution, addressed delivery and hearing remain distinct checks.

## Which voice settings apply?

Ctrl+B (the default voice.record_key) toggles recording in the focused interactive Hermes CLI/TUI when local voice mode is enabled. It uses the microphone on the machine running that CLI. A gateway service has no interactive terminal for this shortcut.

The voice.silence_threshold, voice.silence_duration and voice.max_recording_seconds settings control that local microphone recorder. Its beep, auto_tts, stop_phrases and barge_in settings also belong to local voice mode. They do not change Discord capture, its pause during replies or its phrase timing.

Discord uses the selected stt.provider and stt.streaming path. On stock Hermes 0.23.0 its receiver ends a phrase after 1.5 seconds without packets and requires at least 0.5 seconds of buffered audio; these are source constants, not voice.* configuration leaves. discord.voice_channel_inactivity_timeout_seconds controls how long the bot stays in the channel, separately from phrase timing and recognition latency.

## Accepted Discord conversation

The plugin feeds authorized decoded PCM from the existing Discord client into the stock streaming factory. It preserves profile/user/channel binding, bounds pending audio and submits a successful final once through the unchanged native voice callback. Healthy silence/mute finalizes that same stream; managed live mode prevents duplicate recognition of the receiver's WAV. Normal attachments keep recorded recognition. Failed live capture is explicit.

This release retains the accepted conversation behavior. The stock one-shot player can pause reception during speech for echo protection. Additional simultaneous speaking/listening modes, progressive playback and live-meeting recording controls are follow-ups. Short recognition sessions remain bounded to 240 seconds.

## Upgrade and rollback

Record the working package, its checksum and the plugin's immutable commit before upgrading. Keep the owning profile's config, secret store and saved preferences. The runtime owner updates the complete package and plugin together; for an existing installation, the native plugin CLI supports `--force --ref COMMIT`, where COMMIT is a reviewed 40-character commit.

To roll back, restore the recorded package and matching plugin revision, preserve those profile files, and verify the selected STT/TTS providers and ordinary speech path. Activate either direction only at a verified gateway quiet window. The skill/plugin rollback does not require changing Hermes itself.

## Compatibility wrappers and checks

- `scripts/hermes-stt-wrapper.sh {input_path} {output_dir} {language}` processes the exact addressed input and writes `output_dir/transcript.txt`; path/status output stays off stdout.
- `scripts/hermes-tts-wrapper.sh {input_path} {output_path}` reads UTF-8 text and produces voice-compatible Ogg/Opus. Existing voice/language aliases remain adapter defaults.

Wrappers use plain `python` from the configured standard runtime. Keep them executable and pass only declared secret names through the host's secret mechanism, never secret values in command arguments. Preserve recipient opt-outs and source provenance.

The offline installed-wheel test enables the packaged plugin with the native CLI, verifies real STT/TTS registration outside checkout and checks preference, other-profile, credential and comment preservation. It does not exercise the network Git downloader or messenger delivery. Focused streaming checks: `python -m unittest tests.test_discord_ingress`.

[Voice choices](synthesis-provider-testing.md) · [Recorded-file options](transcription.md) · [Official STT contract](https://github.com/NousResearch/hermes-agent/blob/v0.23.0/agent/transcription_provider.py) · [Official TTS contract](https://github.com/NousResearch/hermes-agent/blob/v0.23.0/agent/tts_provider.py).
