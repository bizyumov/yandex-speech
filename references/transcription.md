# Transcription, configuration and recovery

## API options

| Route | Status in Phase 1 | Requirements and trade-offs |
|---|---|---|
| gRPC v3 RecognizeFile → operation polling → GetRecognition | Implemented; default recording route | Optional yandexcloud/grpcio/grpcio-tools, requests base, SpeechKit execution permission. Timing and native two-speaker mixed recording labels. Binary inline content or caller-provided supported storage URI. |
| gRPC v3 RecognizeStreaming | Implemented | Recognition while audio arrives; ordered prepared PCM, previews, external EOU, complete response drain. No mixed-recording diarization/subtitle output. |
| REST v1 direct / legacy 25-second split | Preserved explicit entries | No gRPC/S3. Text-only raw output; no genuine timing or speaker labels, no literary-style guarantee. Corrected 30-second/five-second-overlap assembly is Phase 2. |
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
