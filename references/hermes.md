# Hermes 0.22.5 integration

The optional native plugin lives at integrations/hermes and registers both a TranscriptionProvider and a TTSProvider named yandex. The skill remains the installed discovery entry; the plugin adds a host binding. No productive profile is modified by import/tests.

For an authorized profile, link its plugin directory entry named yandex-speech to the installed skill's integrations/hermes directory, then enable that plugin through Hermes's normal plugin configuration. Ensure the standard Hermes environment can import the skill and its optional gRPC dependencies. Verify the plugin is loaded in that exact profile before changing its selected STT provider. Example profile settings:

```yaml
stt:
  provider: yandex
  streaming: true
  yandex:
    language: ru-RU
```

The adapter binds .config/yandex-speech/config.json to the registered active workspace. User-file values override profile stt.yandex values and host language hints. The secret resolver must match that same profile; a foreign/missing scope fails closed. Do not edit global HOME to switch profiles. Required real methods are is_available(), transcribe(), streaming_capable and open_stream_session(); session push_audio/partial_transcript/finalize adapt the core completion contract to success/transcript/provider/error envelopes. A partial is never a completed ordinary input. The native finalize error envelope does not raise even when cleanup fails.

A same-name stt.providers.yandex entry with type: command takes precedence in Hermes; remove/reconcile that entry before native streaming selection. Registration diagnoses this conflict. File availability remains distinct from streaming eligibility. A profile declining/missing gRPC can use the existing REST file option when its text/metadata requirements fit; it is not eligible for arriving recognition. Automatic true-literary requests are not silently degraded to raw v1 text.

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

A same-name `tts.providers.yandex` command entry still takes precedence over native registration. Reconcile it through the scoped setup workflow before selecting native Yandex TTS. Compatibility wrappers remain available. Raw XML SSML remains an explicit skill CLI/Python v1 operation because the ordinary host text cleaner is not a raw-XML route.


## Scoped native enrollment

Run the helper in the intended active Hermes profile. It uses that profile's config and workspace, never another profile's credentials. Preview first, then explicitly apply the same enrollment:

```bash
python /absolute/skill/scripts/speech_setup.py --hermes-plan
python /absolute/skill/scripts/speech_setup.py --hermes-apply
python /absolute/skill/scripts/speech_setup.py --check
```

The preview shows selected non-secret changes and the plugin link, without writing config, backups, credentials or files. Apply uses Hermes's native configuration writer, verifies the saved result and preserves unrelated values/comments. A second equivalent apply is a no-op. The native plugin manifest is included beside its Python modules in the wheel as well as the installed skill projection, so enrollment works outside the source checkout.

Recognized canonical Yandex commands migrate their literal voice/language/role/model choices into native defaults without executing those commands. The workspace user file retains priority. Unknown commands, opaque environment prefixes, unknown input placeholders, incompatible defaults, redirected plugin directories and different existing plugin installations fail before mutation. Configuration aliases crossing changed settings are refused because the native round-trip writer can otherwise update an unrelated alias; separate those aliases before enrollment. Existing wrappers remain installed.

Streaming is enrolled only when explicitly selected and locally eligible:

```bash
python /absolute/skill/scripts/speech_setup.py --hermes-plan --enable-stt-streaming --enable-tts-pcm
python /absolute/skill/scripts/speech_setup.py --hermes-apply --enable-stt-streaming --enable-tts-pcm
```

The PCM selection is pinned to `tts.streaming.provider: yandex`; an existing different selection is otherwise retained. Native enrollment does not create keys, rewrite .env, install SDKs, copy another profile or restart a gateway. Local availability checks do not prove API execution, a loaded live gateway, actual source ingress, messenger delivery or channel playback. Real source/host activation remains a separate verified step. `--config` and `--probe-audio` belong to standalone checks and cannot be combined with native enrollment.
