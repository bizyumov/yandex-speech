# Testing synthesis providers, SSML and pronunciation

## Select the operation deliberately

Distinguish API generation, transport, model, voice and container. Provider v3 support does not establish installed-skill support. Recheck the installed CLI's help and current official method/model documentation before choosing an entry point. Keep generic v3 synthesis and LiveTTS model selection explicit rather than silently substituting one for the other.

Use the existing profile-scoped secret resolver and API-Key authentication. Do not infer a missing credential from an empty environment variable in a different execution context; a supported host entry point may resolve it without exporting it. Never extract or print the resolved value for diagnostics.

## Current implemented interfaces and verification

The implemented interfaces expose synthesis through `TextToSpeech` and `scripts/tts.py`; it uses no request interception. It has exercised REST v1, REST v3 and gRPC v3 file synthesis in MP3, WAV, Ogg/Opus and raw LPCM. Explicit LiveTTS requests for Sergey/Vasily/Denis return direct MP3; no WAV conversion is needed. Provider WAV RIFF/data size placeholders of 0xffffffff are replaced with actual collected lengths before publication, preserving PCM bytes.

Use plain `python` in Hermes's configured VIRTUAL_ENV/PATH. For an ordinary reply, use the standard Hermes speech tool; the CLI recipes are for explicit provider work or standalone use.

```bash
python /absolute/skill/scripts/tts.py "Проверка голоса." --api-version 3 --transport rest --model livetts --voice sergey --format mp3 --output ./reply.mp3
python /absolute/skill/scripts/tts.py --input-file ./input.xml --text-format ssml --api-version 1 --voice ermil --format wav --output ./reply.wav
python /absolute/skill/scripts/tts.py --input-file - --stream --api-version 3 --transport grpc --model livetts --voice denis --format lpcm --output -
```

The stream command consumes UTF-8 newline-delimited fragments and emits headerless 24 kHz mono signed-16-bit little-endian PCM before text EOF. Its Python counterpart is `with tts.stream_audio(fragments, voice="sergey", model="livetts") as audio`. Closing/cancelling stops owned workers; caller iterables must yield cooperatively. Streams cap text at 5,000 Unicode code points per RPC, rolling into another RPC without dropping content. File utterances use the safe 250-code-point/24-second constraints; unsafe mode is explicit. V1 measures both 5,000 code points and the complete 15,000-byte encoded form.

File synthesis partitions/reassembles one complete requested container; explicit provider duration rejection subdivides only the rejected portion. SSML is parsed and kept well-formed with paragraph/sentence boundaries and indivisible phoneme/substitution elements. Failed output is never returned as complete; prior successful parts are retained in an explicitly incomplete artifact directory. Metadata reports actual version, transport, model, voice, format and assembly.

Configuration remains call → user file → adapter defaults → environment aliases → built-ins. Model/voice families remain literal; known unsupported controls fail before RPC. LiveTTS has no speed/pitch/duration controls. The catalog records primary languages, not an exhaustive language-rejection rule: explicit cross-language requests are preserved where the provider accepts them. The retained alena default is verified through v1; no replacement voice is silently chosen.

Focused offline cases are in `tests/test_tts_*.py`; opt-in real cases are `tests.provider.test_synthesis`. Run provider tests as the active profile owner with its authorized credential projection and explicit provider/host test flags. Generation and protocol checks do not establish messenger receipt/playback; those remain operator acceptance. The exploratory observations below are historical supporting evidence, not substitutes for these implemented-interface checks.

## Complete-utterance REST probe

The exercised LiveTTS route is HTTP POST to `https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis` with a short text, an explicit model/voice and output specification:

```json
{
  "model": "livetts",
  "text": "Short synthetic sample text",
  "hints": [{"voice": "sergey"}],
  "outputAudioSpec": {
    "containerAudio": {"containerAudioType": "WAV"}
  }
}
```

Explicit requests for `sergey`, `vasily` and `denis` have returned HTTP 200 with decodable complete-file samples through this route. This is provider evidence, not a guarantee that a current installed skill exposes the route or that every voice/role combination works. Do not use runtime interception of an older entry point as the production design; integrate the new method through the skill's supported resolver and public interfaces.

Consume all response records in order. Metadata records may have absent or null `audioChunk`; skip those safely. Decode present `audioChunk.data` from base64. Validate provider errors, termination/completeness and framing before publishing a final artifact; HTTP 200 and some audio bytes alone are insufficient. A streamed REST response to complete text is not bidirectional incremental-text synthesis.

Keep diagnostic inputs within the documented utterance text and duration budgets. A provider `Too long text` response is evidence of rejection, not a completed long-text implementation. Do not silently shorten requested content or enable unsafe mode; partition and assemble through a tested implementation, or report the unsupported operation. Use short examples when the task is only a voice comparison.

## Final-file and host verification

The early WAV-to-MP3 exploration was followed by direct provider MP3 checks. The supported CLI requests MP3 directly for that output; WAV output is finalized internally without re-encoding. Prefer requesting the final desired format to avoid unnecessary conversion; where conversion is required, keep it inside one supported operation and record the conversion provenance.

Probe the saved final file with `ffprobe`, not just the intermediate audio or filename extension. Preserve requested sample order and label each voice. User receipt and player-duration confirmation are separate evidence from provider success. Equal sizes or durations are not proof of acoustic voice identity; never certify distinct voices solely from requested identifiers.

## SSML request and comparison recipe

1. Check the current provider SSML contract and the selected voice's API compatibility before synthesizing. Reference: https://aistudio.yandex.ru/en/docs/speechkit/tts/markup/ssml. Do not assume a LiveTTS voice supports the v1 SSML interface merely because it supports v3 plain-text synthesis.
2. Prepare one well-formed `<speak>` document. The exercised tag set is `speak`, `p`, `s`, `break`, `phoneme` and `sub`; test supported attributes separately rather than calling one combined sample exhaustive coverage. Include an audible description before each variant so the listener can follow it.
3. For the v1 provider probe, POST URL-encoded form data to `https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize`, using the existing secret resolver and API-Key header. Send `ssml`, `voice`, `lang=ru-RU` and `format=mp3`; omit `text`. Do not feed XML through an ordinary-text CLI and call that an SSML test.
4. Request the final MP3 directly. This v1 route has returned `audio/mp3` samples for `filipp`, `ermil`, `zahar` and `madi_ru` without WAV conversion. Recheck current voice compatibility rather than presenting these as a permanent complete catalog.
5. Probe the final audio duration, attach voices in the requested order and show the exact submitted SSML inline. Request an acoustic verdict for each disputed feature: HTTP 200 proves acceptance, not that pauses, stress or phonemes sounded as intended.

Use explicit `time` when testing a measured pause; `strength` does not specify a duration. Compare a control without the tag when its audible effect is disputed. Do not diagnose ignored markup solely from the listener's transcribed playback.

## Technical terms, phonemes and stress

Start with one term alone and in a sentence before expanding the sample. Keep pronunciation entries separate from the canonical spelling by using `<sub>` or `<phoneme>`; do not rename identifiers in the surrounding technical text.

For abbreviations, compare a joined alias with spaced letter names: spacing can introduce unwanted pauses. A joined Russian rendering with a stress marker before the target vowel was preferred acoustically in a gRPC comparison:

```xml
<speak>
  <s>Передаём аудио через <sub alias="джиэрпис+и">gRPC</sub>.</s>
</speak>
```

Put `+` immediately before the intended vowel in a stress-alias trial, not after the word: a trailing plus was spoken literally. If the stressed vowel is the first character, the candidate form starts with `+`, as in `+анор`. Treat each candidate as a pronunciation test; do not certify stress merely from successful synthesis. Canonical name stress must be verified independently from a reliable pronunciation source, not inferred from how the TTS reads it or from the fleet inventory.

The following IPA-like strings were accepted by the Russian `ermil` v1 voice as experimental approximations. They are input fixtures, not authoritative English IPA or universally correct pronunciations:

```xml
<phoneme alphabet="ipa" ph="ɛjpʲiaj">API</phoneme>
<phoneme alphabet="ipa" ph="dʐɨɛrpʲisʲi">gRPC</phoneme>
<phoneme alphabet="ipa" ph="spʲitʂkʲit">SpeechKit</phoneme>
```

Inspect the provider's documented phoneme alphabet before transferring English transcriptions into a Russian voice. IPA support does not imply that every IPA symbol or stress marker is accepted. Preserve provider parse-error evidence, change one disputed control at a time, and use a supported alias fallback for the actual sample. Do not retain rejected stress strings as recommended recipes. The phoneme-only gRPC fixture above was accepted but drew a listener correction about the final syllables; use it as a comparison case, not a validated final pronunciation.

## Fleet and proper-name pronunciation recipe

1. Retrieve the roster through the governing inventory route. Deduplicate identifiers programmatically and distinguish declared inventory from independently live-verified membership; a credential manifest alone may omit later additions or include retired actors.
2. Build a small pronunciation ledger: canonical identifier, operator-selected spoken spelling if supplied, source-language form, stressed syllable, evidence and verification status. A fleet identifier is not itself evidence for a Russian transliteration or stress.
3. Open a pronunciation reference for the exact name. Confirm that the page actually contains that headword and the cited pronunciation, including redirects. Decode the reference's own stress notation using its pronunciation key before mapping it to a Russian alias; an apostrophe, accent or long vowel cannot be interpreted from appearance alone. Do not claim a name-specific check from a general pronunciation guide or a search snippet. When the user cannot find the quoted word, reopen the actual destination and provide a short exact headword/pronunciation excerpt with a retrievable section or location; never repeat an unverified citation as proof. Where sources disagree, expose the alternatives before treating one as canonical.
4. Preserve an operator's explicit fleet naming correction as an operator-selected alias, not a linguistic claim about Tolkien or another source language. Do not merge spelling and stress decisions: correcting a consonant does not prove the stressed syllable.
5. Generate the selected variants with `<sub>` aliases, show the complete exact markup with the attachment, and retain canonical identifiers in the element text. Mark unresolved variants as comparison trials rather than verified pronunciations.
6. Evaluate acoustic pronunciation from the listener or an authorized audio check. Incoming STT of speaker playback cannot establish stress and can accidentally look like a new user instruction. On a disputed result, isolate that name/control and regenerate instead of repeatedly resending a supposedly authoritative full list.

## Reporting and implementation acceptance

Record request shape, actual route, requested voice/model, response status, complete-output validation, duration and host confirmation, without secrets or private runtime identifiers. Separate:

- standalone provider probe;
- supported skill CLI/Python operation;
- native host provider integration;
- configured gateway execution;
- attachment delivery and playback.

A few REST voice samples do not pass gRPC synthesis, full container coverage, long-text partitioning, SSML, incremental text, cancellation, configuration precedence or release documentation gates. Use the owning issue's current acceptance criteria for that mapping rather than inventing completion.

## Synthesis selection and controls

| Setting | Implemented behavior |
|---|---|
| `--api-version auto\|1\|3` | Auto chooses v1 for compatible ordinary text/SSML and v3 for v3-only voices or controls; a fixed version is strict. |
| `--transport auto\|rest\|grpc` | V1 uses REST. V3 auto prefers an available gRPC SDK and can use REST before submission; an accepted request is not resubmitted through another transport. |
| `--text-format plain\|ssml` | Plain text is never detected as XML. SSML uses v1; fixed v3/gRPC conflicts fail before a provider request. |
| `--model` | `auto`, `general`, `general:rc`, `general:deprecated`, or `livetts`; model families and explicit voices are preserved. |
| `--voice`, `--lang`, `--role`, `--emotion` | Literal voice/language and catalog-compatible role; emotion retains the legacy alias. Unsupported combinations fail explicitly. |
| `--format mp3\|oggopus\|wav\|lpcm` | One complete requested file. WAV headers are finalized; encoded multipart output is assembled internally without re-encoding audio. |
| `--speed`, `--volume`, `--pitch-shift`, `--duration-ms`, `--duration-policy` | Forward method-supported controls; duration policy is `min\|max\|exact`. LiveTTS does not support speed/pitch/duration control. |
| `--sample-rate` | Describes file LPCM, or v1 WAV wrapping. Container methods reject an inapplicable explicit rate. Streaming PCM is fixed at 24 kHz mono signed-16-bit little-endian. |
| `--unsafe-mode` | Opt-in v3 5,000-code-point utterance budget; the default safe budget is 250 code points and 24 seconds. |
| `--deadline-seconds` | Whole-operation deadline, default 1,800 seconds. Partial file results are retained as incomplete on failure. |
| `--config PATH`, `--overwrite` | Explicit config-file selection and explicit replacement of an owned regular output; source inputs and symlinks remain protected. |

The compatibility snapshot covers the published general and LiveTTS voices,
including the v3-only general voices. Inspect the current
[official voice/role catalog](https://aistudio.yandex.ru/en/docs/speechkit/tts/voices)
and `yandex_speech.tts_voices.CATALOG` together. The language in that catalog is
the primary language, not an exhaustive prohibition on explicit cross-language
requests. Unknown voices are diagnosed instead of substituted.

Legacy file defaults are `alena`, `ru-RU`, MP3, speed 1 and neutral; alena's
retained v1 route has a real provider check. Streaming requires a v3-capable
voice such as `filipp`, or an explicit LiveTTS model/voice. It does not silently
replace a configured legacy-only voice.

With no gRPC SDK, ordinary v1 and explicit v3 REST files remain available.
True `--stream` reports the missing requirement; selecting REST does not turn
an incremental stream into a working voice-channel conversation.

For development acceptance, build a wheel in the configured standard Hermes
environment, make that wheel readable by the profile owner, and set the
non-secret `YANDEX_SPEECH_TEST_WHEEL` path for the provider test.
Run `python -m tests --phase 3 --profile grpc` for offline AC29–AC36 coverage.
Real provider tests are `tests.provider.test_synthesis`, explicitly enabled
with `YANDEX_SPEECH_LIVE_TESTS=1`; the configured-host check additionally
requires `YANDEX_SPEECH_HOST_TESTS=1`. They run as the active profile's Unix
owner with its own authorized credential projection. A provider/host omission
is pending evidence, never an acceptance pass.
