# Transcription, configuration and recovery

## API options

| Route | Delivery status | Requirements and trade-offs |
|---|---|---|
| gRPC v3 RecognizeFile → operation polling → GetRecognition | Implemented; default recording route | Optional yandexcloud/grpcio/grpcio-tools, requests base, SpeechKit execution permission. Timing and native two-speaker mixed recording labels. Binary inline content or caller-provided supported storage URI. |
| gRPC v3 RecognizeStreaming | Implemented for one bounded utterance | Recognition while audio arrives; ordered prepared PCM, previews, external EOU, complete response drain. No mixed-recording diarization/subtitle output. |
| REST v1 direct / overlapped split | Implemented explicit text-only entries | No gRPC/S3. Sample-exact windows up to 30 seconds, five-second overlap and 25-second stride. Raw output has no genuine word timing/speaker labels or literary-style guarantee. |
| REST v2 async-s3 | Preserved explicit compatibility | requests+boto3 and authorized object access. Existing v2 response handling, not v3 streaming/results. New durable recovery/storage helpers are Phase 2. |
| REST v3 + S3 or eligible inline; complete fallback chain | Phase 2 | Whole-file v3 metadata without optional gRPC dependencies; upload/polling/access setup. |

The full intended chain is gRPC v3 → REST v3 + S3 → FFmpeg split + REST v1. Phase 1 never claims that automatic chain is complete. Fixed API versions and grpc/rest selections are strict. Default literary true/required speaker or timing output cannot silently become unformatted v1 text. An explicitly selected direct/v1 route reports the unavailable literary representation.

Exact project admission limits are applied to prepared audio, not its original container or suffix:

| Method | Bound |
|---|---|
| REST v1 direct | Mono LPCM/Ogg, duration ≤30,000 ms; audio body **<1,000,000 bytes**. A 30-second 16 kHz mono s16le body is 960,000 bytes. WAV headers are removed for raw LPCM. |
| gRPC v3 inline | Duration ≤14,400,000 ms; complete serialized protobuf including envelope ≤60,000,000 bytes; finite 64 MiB client message limits. |
| gRPC v3 caller-supplied storage URI | Prepared object ≤1,000,000,000 bytes and ≤14,400,000 ms; compatible source_info required. Only provider-supported HTTPS Object Storage URIs, with no embedded credentials/query. |
| Arriving recognition | Native 16 kHz mono s16le PCM; ≤240 s / 7,680,000 bytes per utterance. Source queue ≤1,048,576 bytes; prepared queue ≤64,000 bytes; two local speech operations and one inline submission at a time. Finalization ≤5 s; accepted cancellation cleanup ≤2 s. |

[Provider limits](https://aistudio.yandex.ru/en/docs/speechkit/concepts/limits) · [Formats](https://aistudio.yandex.ru/en/docs/speechkit/formats) · [v3 recorded API](https://aistudio.yandex.ru/en/docs/speechkit/stt-v3/api-ref/grpc/AsyncRecognizer/recognizeFile).

## Recorded split recognition

For an explicitly selected unformatted alternative, use the existing split entry:

```bash
python scripts/transcribe.py recording.wav --mode direct-chunked --api-version 1 --transport rest --no-literature-text --format json --output transcript.json
```

The default literary request remains true. This command explicitly selects v1 raw text; it does not fulfill timing, speaker labels, subtitle cues or literary formatting. Prefer the ordinary v3 recording route when those features are required. The synchronous service accepts one channel, up to 30 seconds and 1 MB; the project keeps each complete 16 kHz mono s16le window at 960,000 bytes. [Synchronous API](https://aistudio.yandex.ru/en/docs/speechkit/stt/api/request-api).

The decoder prepares one owned PCM recording, then extracts windows at 0, 25, 50, … seconds, each ending at the earlier of start+30 seconds or recording EOF. It stops when the previous window already covers EOF: 55 seconds needs two requests, while 56 seconds needs three. Original media remains unchanged. JSON retains window/sample/source offsets, every raw response and every text hypothesis. These window spans are not provider word times; normalized speech timing stays absent.

Assembly compares only adjacent suffix/prefix hypotheses. A unique multiword match appears once in the selected text, while genuine repeated phrases elsewhere remain. An anchored cut word can use the complete raw word from the next window and retain its partial alternative. Multiple matching lengths, single-word matches and disagreement retain both hypotheses with a structured uncertainty. Even a selected text-only alignment records the lack of word-time proof. The CLI reports boundary caveats on stderr, preserving its output-path-only stdout. Select JSON upfront when inspecting these alternatives; TXT contains the conservative selected text.

The opt-in `tests.provider.test_overlap` acceptance uses a synthetic 56-second recording with a word clip crossing 30 seconds, two genuine repeated requests and the final control tail. Both the public Python split operation and the CLI are exercised; its summaries contain counts/hashes rather than transcript or credential values. Run with the existing synthetic-provider opt-in and an optional unique evidence destination:

```bash
YANDEX_SPEECH_LIVE_TESTS=1 python -m unittest tests.provider.test_overlap -v
```

## Configuration

Default file: $HOME/.config/yandex-speech/config.json; active Hermes workspace replaces HOME for this scope. Only CLI --config PATH selects a different file. No YANDEX_SPEECH_CONFIG, XDG config redirect, public Python config_path or CWD discovery. Missing standard file is valid; malformed/unreadable/unknown fields in an existing or explicitly selected file fail CONFIG before submission.

```json
{
  "speech": {
    "api_version": "auto",
    "transport": "auto",
    "language_code": "ru-RU",
    "literature_text": true,
    "profanity_filter": false,
    "enable_diarization": false,
    "enable_timestamps": false,
    "default_format": "json",
    "object_storage": {"endpoint": "https://storage.yandexcloud.net", "prefix": "speechkit"},
    "tts": {"api_version": "auto", "transport": "auto", "text_format": "plain",
            "default_voice": "alena", "default_lang": "ru-RU", "default_format": "mp3",
            "default_speed": 1.0, "default_emotion": "neutral"}
  }
}
```

Explicit call/CLI → selected user file → native adapter defaults → legacy environment aliases → built-ins. Explicit false remains explicit. Credentials never belong in this JSON. Enable-profanity=true is rejected. HERMES_TTS_VOICE/HERMES_TTS_LANG and existing S3 aliases stay below the user file. `auto` resolves per operation: new recording/arrival v3 over gRPC in Phase 1; existing ordinary synthesis REST v1. Diagnostics report actual effective values/sources and unavailable later routes.

## Results and state

Use `from yandex_speech import SpeechTranscriber, AudioInputSpec, SpeechError`. `start_transcription(path, diarize=True, timestamps=True)` returns a private JobHandle; `poll_until_complete(job)` or a saved receipt Path retrieves the accepted job. `get_result` uses the same recovery operation. No uncertain RecognizeFile submission is automatically retried. Transient operation/result calls have at most three attempts and a whole deadline (default 1,800 seconds); polling does not invent a new job.

Python state uses explicit job_dir or XDG_STATE_HOME/yandex-speech/jobs, falling back to ~/.local/state/yandex-speech/jobs. Hermes scopes state under its workspace. The CLI writes `<output-stem>.job.json` (or --job-file PATH) as a private pointer to the complete UUID-scoped receipt. Resume accepts either that pointer or the canonical receipt. Retain both pointer and state until no longer needed. Bare --operation-id requires explicit version 3 in Phase 1; v2 recovery is Phase 2.

Result schema 1 contains text, ordered segments/words, raw provider events, source identity/conversion provenance, actual api_version/transport, metadata_availability, uncertainties, input_complete and completion_kind. Only successful whole-input drain is complete. No-speech is a successful complete take with empty text, not a transport error. Missing timing/speakers stay null. Speaker tags are anonymous and request-local; native labeling has a platform limit of two speakers.

Literary true explicitly enables provider normalization/capitalization/punctuation, with profanity masking and phone formatting off. Correlate final_refinement by final index/channel, preserve raw finals and do not duplicate words. Missing refinement is reported raw-unfulfilled, never silently called literary. False selects raw finals and disables normalization. [Normalization](https://aistudio.yandex.ru/en/docs/speechkit/stt/normalization).

TXT/JSON/SRT/VTT use actual supplied timing. SRT/VTT never fabricate zero-length cues or timing from chunks. Atomic output is no-clobber unless --overwrite is explicit; original media is protected even with overwrite. Receipt/result publication records hashes before/after atomic publication so restart recovery can adopt only the matching owned artifact. Unknown pre-existing files are preserved.

## Arriving audio

`open_stream(audio_input=AudioInputSpec(...))` returns a context-managed session with push_audio(bytes), partial_transcript(), finalize() and cancel(). Native host PCM is 16 kHz mono s16le. A zero-audio take may return local no_speech without RPC. Partial and provider-final events alone never complete a host utterance.

Finalization stops accepting source bytes, closes/drains source input, successfully flushes decoder/resampler, sends every prepared sample in FIFO order, sends exactly one external-classifier EOU, half-closes requests and drains responses to normal EOF. EouUpdate.time_ms is an estimated provider time: it must be consistent with that acknowledgement's eou_time_ms/final_time_ms cursor and within sent audio, rather than equal PCM byte duration. Missing/inconsistent acknowledgement, premature EOF, conversion error, timeout or cancellation fails. Repeated finalize reuses the completed result. [Protocol fields](https://aistudio.yandex.ru/en/docs/speechkit/stt-v3/api-ref/grpc/Recognizer/recognizeStreaming).

## Exit categories

0 success/no-speech; 2 CLI/config; 3 prerequisites/audio/limit; 4 authentication/permission; 5 provider/transport/retry exhaustion; 6 deadline; 7 incomplete/uncertain output; 130 cancellation. Progress/receipt paths go to stderr; successful output paths go to stdout. Keep receipts after cancellation: local cancellation does not establish server-side job cancellation or authorize rebilling.


## Required continuous server streaming

Long meetings and monologues require a continuous transcription lifecycle.
The existing 240-second utterance interface is not that lifecycle.

SpeechKit provides a managed bidirectional gRPC RecognizeStreaming service.
Its published per-connection limits are five minutes of audio, 10 MB and one
channel; it can terminate after five seconds without recognition messages.
Continuous capture must renew connections while preserving transcript/source
continuity, handle silence and recovery, and retain the final tail. The
60 MB unary file-request budget is a separate constraint.

The installed SDK includes REAL_TIME/FULL_DATA and server/default or external
EOU options. Evaluate their actual service behavior and define one EOU owner;
a segment-final event must not silently end a meeting. Preserve genuine
timing/speaker information without inventing cross-connection identities.

[Issue #37](https://github.com/bizyumov/yandex-speech/issues/37) specifies the
required investigation, native Hermes source wiring, long-session behavior
and release evidence. [Release #26](https://github.com/bizyumov/yandex-speech/issues/26)
depends on it. A short stream, a provider registration or stt.streaming=true
does not verify that audio is being streamed from the actual source.
Recorded files retain the appropriate asynchronous/native-container route.

[Streaming contract](https://aistudio.yandex.ru/en/docs/speechkit/stt/streaming) ·
[v3 request/events](https://aistudio.yandex.ru/en/docs/speechkit/stt-v3/api-ref/grpc/Recognizer/recognizeStreaming) ·
[Service limits](https://aistudio.yandex.ru/en/docs/speechkit/concepts/limits).


### Measured server prerequisite — 2026-11-06

The opt-in `tests.provider.test_server_streaming_contract` suite passed three synthetic cases against the managed TLS endpoint with yandexcloud 0.412.0 and grpcio 1.84.1. REAL_TIME with both server/default EOU and external EOU produced recognition observations while audio was still arriving. Spoken raw finals contained genuine word timings; literary refinements correlated by final index and retained both repeated control tails. Empty finals on silence also correlated and are not speech-bearing transcript entries. The complete input iterator drained and the response stream ended normally in all three cases.

FULL_DATA was measured separately: the server returned final/refined results after input ended, with no progressive recognition observation in that run. It is not substituted for REAL_TIME capture. These short contract results satisfy a prerequisite for the continuous implementation; they do not establish rolling-window recovery, hour-long continuity or an actual Hermes/Discord consuming path.

Reproduce only with explicit synthetic-provider opt-in, existing authorized credentials and reviewed fixtures:

```bash
YANDEX_SPEECH_LIVE_TESTS=1 python -m unittest tests.provider.test_server_streaming_contract -v
```

Supply the existing provider suite's `YANDEX_SPEECH_TEST_FIXTURE_DIR` and optional `YANDEX_SPEECH_TEST_OUTPUT_DIR`. The latter receives unique run directories with sanitized event/timing counts and installed SDK versions. No credentials or transcript text are written to those summaries.
