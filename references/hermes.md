# Hermes integration

The optional native plugin lives at integrations/hermes and registers both a TranscriptionProvider and a TTSProvider named yandex. The skill remains the installed discovery entry; the plugin adds a host binding. No productive profile is modified by import/tests.

For an authorized profile, install and enable the binding through the stock Hermes CLI described below; an existing reviewed installation remains usable. Ensure the standard Hermes environment can import the skill and its base dependencies; gRPC is needed for arriving recognition or an explicit gRPC file selection. Verify the plugin is loaded in that exact profile before changing its selected STT provider. Example profile settings:

```yaml
stt:
  provider: yandex
  streaming: true
  yandex:
    language: ru-RU
```

The adapter binds .config/yandex-speech/config.json to the registered active workspace. User-file values override profile stt.yandex values and host language hints. The secret resolver must match that same profile; a foreign/missing scope fails closed. Do not edit global HOME to switch profiles. Required real methods are is_available(), transcribe(), streaming_capable and open_stream_session(); session push_audio/partial_transcript/finalize adapt the core completion contract to success/transcript/provider/error envelopes. A partial is never a completed ordinary input. The native finalize error envelope does not raise even when cleanup fails. Feeding or polling a failed native session raises its saved SpeechError immediately. Failed finalization remains cached and nonraising; its stream_failure metadata names the first phase/category/code and any secondary cleanup failure. Concurrent cleanup is reported as cleanup_pending; the retained failure envelope refreshes its cleanup diagnostics when that cleanup finishes and when finalization is read again. Cancellation preserves that original cause. Healthy source EOF finalizes the same stream; a retained-file fallback belongs to the host after a failed stream and does not count as streaming acceptance. A microphone toggle is not an API-selection option: diagnose the first stream failure before attributing a later file fallback to mute.

A same-name stt.providers.yandex entry with type: command takes precedence in Hermes; remove/reconcile that entry before native streaming selection. Registration diagnoses this conflict. File availability remains distinct from streaming eligibility. Native file calls use the common versioned recording API: REST v3 preserves timing/labeling when gRPC is unavailable or REST is explicit; fixed v2 uses its timing-only storage method. These file routes are not arriving recognition. Automatic true-literary requests are not silently degraded to raw v1 text. Storage defaults and separate upload keys resolve only from the owning profile's secret scope, below its user-file preferences; another profile's ambient S3 environment is excluded.

The host's tools.transcription_streaming.open_streaming_session consumes stt.streaming: true. That setting selects eligible provider streaming only on host surfaces which actually feed arriving audio. Complete attachment input remains recorded recognition. Full platform capture, turn controls, wake/stop words, interruption propagation and progressive reply/playback are Phase 4; native STT alone does not complete voice chatting.

## Voice-reply route diagnosis

First distinguish sending text, sending a voice message/attachment, and speaking in a connected voice channel. The standard `text_to_speech` tool creates audio; speaking requires the running host's voice-channel playback operation. For an ordinary voice-message/attachment request, start with the standard `text_to_speech` call. Do not require a standalone SpeechKit readiness probe before attempting that route. If the call fails, diagnose in this order:

1. Retain the exact tool error and any reported backend; establish whether synthesis produced a complete audio artifact. Without audio, attachment upload and player rendering have not yet been tested.
2. Inspect the active profile's allowlisted TTS selection and adapter fields, then the selected backend's dependencies in its actual execution context. A backend name in an error identifies the reported attempt, not the verified origin of configuration. A module import failure in a diagnostic interpreter does not establish that the gateway lacks the same module.
3. Compare native-tool, configured-wrapper and standalone-provider results as separate evidence. Standalone synthesis success with a native-tool failure localizes a difference between routes; it does not by itself identify a missing registration, credential problem or configuration precedence bug. Likewise, receiving an inbound transcript is not proof of outbound TTS readiness or word-perfect recognition.
4. Report observed failure, verified cause and unresolved questions separately. A generic setup suggestion in an error is not a diagnosis or authorization to install a different engine. For an explanation-only request, stop at the supported explanation; change configuration or packages only within authorized repair scope.
5. After an authorized repair, repeat the exact native operation and verify synthesis, addressed attachment delivery and user playback separately. Do not close native acceptance using only a direct CLI sample.

## Command wrappers

Keep positional contracts:

- scripts/hermes-stt-wrapper.sh {input_path} {output_dir} {language} writes output_dir/transcript.txt. The exact addressed input is processed; no unrelated cache file is substituted. The host language remains an adapter default.
- scripts/hermes-tts-wrapper.sh {input_path} {output_path} reads UTF-8 text and writes service oggopus (host file type ogg). HERMES_TTS_VOICE and HERMES_TTS_LANG are adapter defaults below the workspace user file.

The STT wrapper keeps CLI output-path/status text on stderr. Hermes falls back to stdout when its transcript file is empty, so a pathname on stdout would become false user input during silence. For the command-provider contract, empty output is unsuccessful and the Discord voice receiver drops it; it must never be turned into a pathname message.

Wrappers resolve their own location and plain python from the gateway-configured VIRTUAL_ENV/PATH. Never prepend an interpreter path. Keep scripts executable. Pass YANDEX_SPEECH_API_KEY through the host's existing secret-name mechanism; never embed it in commands. Hermes command-TTS removes its allocated output before invoking the wrapper, so ordinary no-clobber output is compatible. A synthesized voice-compatible file does not override opt-outs, recipient restrictions or delivery permissions.

[Hermes STT SPI](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/agent/transcription_provider.py) · [Host stream consumer](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/tools/transcription_streaming.py) · [Host command TTS](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/tools/tts_command_provider.py).


## Sending text, sending voice and speaking

These are separate requested outcomes:

| Requested action | Success means | Hermes route |
|---|---|---|
| Send text | Text reaches the addressed chat. | Normal text delivery. |
| Send voice | A voice message or audio attachment reaches the chat. | Synthesis followed by the host's voice-message delivery. |
| Speak in the voice channel | Audio is played in the addressed, already-connected voice channel. | The running gateway's voice-channel playback path. |

The standard `text_to_speech` tool produces audio. Its `voice_compatible` result does not select a connected voice channel. A `MEDIA:` attachment, native voice message, or incoming transcript echo does not prove speaking.

In Hermes v0.22.5, ordinary automatic speech can use Discord's `play_tts` and its active text-to-voice-channel binding. The queued-first-final path sends text/explicit attachments without that automatic speech step. The runner's separate voice-reply path falls back to an attachment if its raw event lacks server data, even when the session source contains the server identifier. These differences are tracked in [issue #4](https://github.com/bizyumov/yandex-speech/issues/4); do not claim that `/voice all`, a successful file synthesis, or identical final-answer wording repairs them.

For an explicit request to speak, use an available host operation that targets the connected voice channel and verify its playback result. An attachment is a failed speaking attempt. Do not turn one successful plain final into a rule that all final answers speak. A newly constructed runner/adapter in a terminal or execute-code subprocess does not hold the running gateway's connected voice client and cannot be used as that playback operation.


### Executable delivery matrix

Historical trace of the earlier inspected host. The fork links below preserve research provenance; they are not installation requirements for the stock0.23.0 binding.

The following source trace uses the preserved 0.22.5 host plus its [tested native-config correction](https://github.com/bizyumov/hermes-agent/tree/d70347878ae939e09d140839e7def0f127df5344). These are in-process host methods, not new terminal commands or a gateway RPC. Supply the owning adapter/profile, addressed source and current conversation binding. A source trace establishes which call is possible; delivery and listening require their own receipts.

| Requested outcome / execution branch | Actual operation and required context | Result and remaining verification |
|---|---|---|
| Text in a chat | `adapter.send(chat_id, content, metadata=...)` on the owning connected adapter; retain thread/reply routing. | Check `SendResult.success` and the addressed platform message. No speech is implied. |
| Voice message or attachment | Standard `text_to_speech` creates the artifact; `adapter.send_voice(chat_id, audio_path, ...)` performs delivery. | Verify the artifact, delivery result and recipient playback separately. Discord attachment delivery does not play into a voice channel. |
| Ordinary automatic spoken reply | The normal completion path uses `adapter.play_tts`; Discord resolves its active text-channel binding and calls `play_in_voice_channel(guild_id, audio_path)`. Voice preferences and recipient restrictions still apply. | Verify the same connected channel and actual client audio. The current playback method returns true even after its timeout branch, so that Boolean alone does not certify complete playback. |
| Explicit runner voice reply | `GatewayVoiceMixin._deliver_voice_reply(event, paths)` selects the delivery adapter and extracts the guild from the raw event. | A connected guild uses `play_in_voice_channel`; missing raw guild data falls back to `send_voice`. A guild stored only in `event.source` does not repair that missing context. |
| First response before a queued follow-up | `_run_agent_deliver_first_response` calls `_deliver_queued_first_response`, which edits/sends final text and delivers explicit media. | This branch has no whole-file automatic `play_tts` call. Its successful text/media receipt cannot pass a speaking request. A prior streaming consumer is separate and requires a supported adapter sink. |
| Steered turn / leftover steer | In-band steering modifies the current run. A leftover steer is merged into the pending follow-up; an event-less follow-up retains the source/prompt but has no new voice message type. | No distinct speaking operation is added by steering. Trace the resulting normal or queued completion; preserve original speaker, delivery intent and generation explicitly when repairing this gap. |
| Delayed process completion | `_send_watcher_message` sends a non-conversational text notice. `_inject_watch_notification` instead admits an internal `MessageType.TEXT` event using the stored originating route. | The former is text delivery; the latter starts a separately routed turn. Neither is a reliable explicit speaking action. Verify the actual event branch rather than assuming the latest foreground voice context applies. |
| Progressive reply | `StreamingTTSConsumer` resolves the selected provider and calls the adapter's `supports_streaming_tts`, `begin_streaming_tts`, `write_streaming_tts`, `finish_streaming_tts` and abort operations. | This Discord adapter inherits the unsupported default sink. Native Yandex PCM availability alone cannot make Discord playback progressive. A plugin must provide a supported progressive sink; this historical trace does not require a Hermes core patch. |

The relevant host sources are [voice routing](https://github.com/bizyumov/hermes-agent/blob/d70347878ae939e09d140839e7def0f127df5344/gateway/run_voice.py), [queued and delayed delivery](https://github.com/bizyumov/hermes-agent/blob/d70347878ae939e09d140839e7def0f127df5344/gateway/run_notifications.py), [turn/follow-up handling](https://github.com/bizyumov/hermes-agent/blob/d70347878ae939e09d140839e7def0f127df5344/gateway/run_turn.py), [Discord capture/playback](https://github.com/bizyumov/hermes-agent/blob/d70347878ae939e09d140839e7def0f127df5344/plugins/platforms/discord/adapter.py) and [stream consumer](https://github.com/bizyumov/hermes-agent/blob/d70347878ae939e09d140839e7def0f127df5344/gateway/streaming_tts_consumer.py).

### Capture and interruption ownership

Current Discord receive decodes 48 kHz stereo PCM into per-SSRC buffers. After silence, the listen loop resolves the participant, checks the current guild allowlist, writes a complete WAV and calls file transcription. It does not open a native live session while packets arrive. A binding move discards pending buffers, and the completed-file path checks that its text-channel binding still matches before callback. The gateway then canonicalizes the source and rechecks participant authorization. This is useful file-path protection; it does not establish live-stream continuity or a source/generation identifier.

The legacy playback path pauses receive until playback ends. The optional mixer path keeps receive running, but still delivers completed utterances. The current gateway interrupt monitor waits for a pending event, transcribes pending voice first, then calls `agent.interrupt` and aborts its streaming-TTS consumer. That is not early incoming-audio cancellation, and it can interrupt agent work. No dedicated notification that a reply was partly heard is established by these paths. Do not claim that changing busy-input or notification settings supplies this missing contract.

The plugin input binding must feed authorized source packets before WAV completion, preserve sequence/time and binding ownership, bound queues and choose exactly one completion path. Continuous recording is record-only and does not submit segment finals as commands. Reliable explicit speaking, a Discord PCM sink and output-only interruption need their separate host design and consuming-client evidence under [issue #4](https://github.com/bizyumov/yandex-speech/issues/4). The current text-similarity duplicate filter can suppress a legitimate repeated request; the replacement must deduplicate source identity instead. No live event, playback or hour-long acceptance is claimed by this matrix.


## Native Yandex TTS

The same plugin registers file synthesis and an optional 24 kHz mono signed-16-bit little-endian PCM stream. File availability and PCM eligibility are separate: a supported REST file selection remains available without the optional gRPC SDK. PCM requires an eligible v3 voice, compatible settings and the SDK. Each call creates its own core client, so overlapping sentence synthesis does not share mutable synthesis state.

Native profile preferences use the host's normal fields:

```yaml
tts:
  provider: yandex
  voice: ermil
  model: general
  speed: 1.0
  output_format: mp3
  yandex:
    language: ru-RU
    role: good
    api_version: auto
    transport: auto
```

The active workspace's user-file TTS preferences remain above native voice/model/speed hints. The native role supplies the default-emotion layer, so a user-file emotion or role retains priority. Literal Ermil/Good is preserved; Friendly belongs to voices that support it. Legacy voice/language aliases are resolved in the owning profile, rather than taken from another profile's ambient process environment. Credentials resolve synchronously in that same owning scope before an audio worker starts.

Native file requests map host ogg/opus to SpeechKit oggopus and return a path with the actual container extension. An unsupported native file format uses WAV and a .wav path. A nonempty file or symlink is refused before synthesis. An existing empty host allocation is left untouched: the adapter publishes a unique completed sibling and returns its actual path, as the native caller contract permits. Absent destinations also publish without clobbering. A changed or populated allocation detected before publication fails and retains the owned staged audio. A writer arriving after the last check keeps its original inode because the adapter never enables overwrite. Incomplete multipart evidence remains retained by its owning synthesis operation. These adapter checks do not certify delivery to a messenger or playback in a connected channel.

A same-name `tts.providers.yandex` command entry still takes precedence over native registration. Inspect its saved preferences and reconcile only the owned entry through the native configuration CLI before selecting native Yandex TTS. Compatibility wrappers remain available. Raw XML SSML remains an explicit skill CLI/Python v1 operation because the ordinary host text cleaner is not a raw-XML route.


## Stock Hermes configuration

Use the standard Hermes plugin manager and configuration CLI in the intended profile. The complete Yandex Speech Python package and required gRPC dependencies must already be available in the configured Hermes environment. Replace COMMIT with the reviewed skill revision:

```sh
hermes plugins install bizyumov/yandex-speech/integrations/hermes --ref COMMIT --no-enable
hermes plugins enable yandex-speech
hermes config set stt.provider yandex
hermes config set stt.streaming true
hermes config set stt.yandex.language ru-RU
hermes config set tts.provider yandex
```

Review an existing same-name command provider and retain its preferences before removing it through the native CLI. Native plugin installation/admission and these ordinary key changes do not require a platform fork or an extended configuration writer. The skill-owned enrollment/configuration writer and its `--hermes-plan`, `--hermes-apply`, `--enable-stt-streaming` and `--enable-tts-pcm` switches have been removed. Standalone `python scripts/speech_setup.py --check` remains a read-only readiness diagnostic; only explicit `--probe-audio` submits audio.

Preserve existing voice/language/role preferences before removing a same-name command entry. For example, retain literal Ermil/Good with `hermes config set tts.voice ermil` and `hermes config set tts.yandex.role good`, then remove only the reconciled entry with `hermes config unset tts.providers.yandex` (and the equivalent STT entry if present). Do not remove an unrecognized command or overwrite unrelated preferences automatically. Verify selected values using `hermes config get stt.provider`, `hermes config get stt.streaming` and `hermes config get tts.provider`. Registration, synthesis and actual Discord/Telegram delivery are separate checks.

Offline package acceptance places the installed plugin payload in a fresh profile's `plugins/yandex-speech` directory, enables it with the native CLI, selects providers through native configuration commands and verifies real registration from outside the checkout. It also verifies repeatability and preservation of another profile, credentials, comments and unrelated settings. These checks make no provider call and do not test Hermes's network Git downloader. Existing productive gateways are not changed by them.

## Discord arriving-audio binding

The current binding targets official unmodified Hermes0.23.0 at2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19. It registers a platform handler against the existing Discord client, observes the original receiver's decoded PCM and feeds the stock streaming factory. Its private receiver/callback dependencies are tied to that inspected host contract; another release requires verification.

Managed live mode requires an explicitly selected Yandex provider and stt.streaming enabled when the plugin loads. With that mode disabled, the existing file voice route remains active. Late attachment to a nonempty buffer rejects that utterance until a fresh boundary. Authorization, receiver identity, voice-channel identity and text binding are checked before upload and before delivery. Moves, teardown and untrusted generations are explicit failures.

One ffmpeg converter per active source produces16kHz mono signed16-bit PCM. Plugin input and submitted-versus-inner-accepted budgets bound backlog. A healthy silence/EOF finishes the same recognition stream. The resulting genuine text is handed once to the unchanged, source-bound Native voice callback, retaining its canonicalization and authorization checks.

The stock receiver still allocates a WAV after silence. In managed live mode the plugin defers ASR for that allocation, preventing a second recognition/callback; the allocation itself is not evidence of a switch to file recognition. Failed live captures do not silently replay through file ASR or select another provider. Normal attachments retain recorded recognition.

[Issue43](https://github.com/bizyumov/yandex-speech/issues/43) defines the acceptance contract. Focused offline checks and a real-Yandex/local-UDP component test cover the receiver, progressive recognition, complete tail and lifecycle. A real Discord canary on the existing official adapter subsequently produced progressive Yandex text during speech, a complete agreed tail, and exactly one matching ordinary Native input. Loaded Native method fingerprints matched the official release. This proves the observed capability, not lossless or hour-long capture.

```sh
python -m unittest tests.test_discord_ingress
```
