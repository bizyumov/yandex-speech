# Speech quality, voices and pronunciation

Choose the requested voice and pronunciation first, then select a compatible API and model.

| Need | Route | Voice/model examples |
|---|---|---|
| XML aliases and phonemes | REST v1, explicit SSML | general / ermil with good; marina with friendly |
| New general-family voices | gRPC v3, general | alexander, kirill, anton, dasha, julia, lera |
| LiveTTS voices and natural delivery | gRPC v3, livetts | sergey/support, vasily/sales, denis/neutral |

API v3 does not mean LiveTTS. General and LiveTTS voices belong to separate model families. Preserve the user's literal voice/model/role choices.

## REST v1 with SSML

Set `YS` once to the installed skill directory (replace `<path-to-skill>` in the command below) and reuse it in the examples.

Save this as `pronunciation.ssml`:

```xml
<speak>
  <p>Изучаем <sub alias="рэст">REST</sub>.</p>
  <p>Слово <phoneme alphabet="ipa" ph="məlɐko">молоко</phoneme>.</p>
  <p>Слово <phoneme alphabet="x-sampa" ph="m@l@ko">молоко</phoneme>.</p>
</speak>
```

```bash
YS='<path-to-skill>'
python "$YS/scripts/tts.py" --input-file ./pronunciation.ssml --text-format ssml --api-version 1 --transport rest --model general --lang ru-RU --voice ermil --emotion good --format mp3 --output ./pronunciation.mp3
```

SSML is a v1 operation. The supported phoneme feature is Russian-only. IPA `ə` maps to X-SAMPA `@`; IPA `ɐ` maps to `6`. The two milk examples above are therefore different transcriptions. Preserve the requested examples as comparisons; do not certify them as acoustically equivalent.

The complete published phoneme inventory, validation and comparison tooling is [follow-up #32](https://github.com/bizyumov/yandex-speech/issues/32). Current XML forwarding does not establish full phoneme support.

## gRPC v3 and native text markup

Stress marks go immediately before the stressed vowel, inside complete Russian sentences:

```bash
python "$YS/scripts/tts.py" "Перед нами старинный з+амок. sil<[300]> Мы закрываем зам+ок." --api-version 3 --transport grpc --model general --lang ru-RU --voice alexander --role good --format mp3 --output ./stress.mp3
python "$YS/scripts/tts.py" "Здравствуйте! Чем я могу вам помочь?" --api-version 3 --transport grpc --model livetts --lang ru-RU --voice sergey --role support --format mp3 --output ./livetts.mp3
```

Native text markup is distinct from XML SSML and is supported for Russian text in API v1 and v3:

- `+` before a stressed vowel.
- `sil<[300]>` for an internal explicit pause; maximum 7,000 ms.
- `<[tiny]>`, `<[small]>`, `<[medium]>`, `<[large]>`, `<[huge]>` for contextual pauses.
- `<[accented]>` or `**word**` for emphasis.
- `[[m ə l ɐ k o]]` for native phonetic notation.

Beginning/end pauses can be ignored. Markup is a synthesis cue; compare complete sentences rather than treating it as a sound generator.

In Hermes, use the plugin's `yandex_speech_ssml` tool for explicit XML pronunciation. It accepts one complete `ssml` speak document plus optional voice/role/language/model/speed and calls REST v1 after normal tool authorization. Copy its returned `media_tag` value verbatim into the final response; JSON copied wholesale does not send the attachment. The tool generates audio and does not claim delivery or hearing.

The built-in `text_to_speech` tool remains a plain/native-text operation. Its formatter removes Markdown bold and damages XML closing tags before the provider receives them. It preserves the tested stress, pause, accented-tag and native phoneme examples; use `<[accented]>` instead of Markdown emphasis there. The explicit CLI/Python SSML commands above remain available outside the host tool workflow. Neither operation is a repair of the built-in tool's raw-XML behavior.

## Voice and role selection

`good` and `friendly` are different role identifiers. For Ermil, `good` is the cheerful role; supported roles are `neutral` and `good`. Do not silently replace it with `friendly`. Marina supports `neutral`, `whisper`, `friendly`.

### Complete official voice catalog

Verified against the official catalog on 2026-11-09 (34 current voices; implementation/table conformance checked). Primary language is the catalog's voice-training language, not an invented prohibition on every cross-language request. Realtime below is vendor availability; this skill does not implement a Realtime client.

| Voice ID | Documented name | Primary language | Gender | Family | API availability | Roles | Default role |
|---|---|---|---|---|---|---|---|
| lea | lea | de-DE | F | general | v1, v3; Realtime | — | — |
| john | john | en-US | M | general | v1, v3; Realtime | — | — |
| naomi | naomi | he-IL | F | general | v3; Realtime | classic, modern | modern |
| amira | amira | kk-KK / kk-KZ | F | general | v1, v3; Realtime | — | — |
| madi | madi | kk-KK / kk-KZ | M | general | v1, v3; Realtime | — | — |
| saule | saule | kk-KK / kk-KZ | F | general | v3; Realtime | neutral, strict | neutral |
| zhanar | zhanar | kk-KK / kk-KZ | F | general | v3; Realtime | friendly, neutral | neutral |
| filipp | filipp | ru-RU | M | general | v1, v3; Realtime | — | — |
| ermil | ermil | ru-RU | M | general | v1, v3; Realtime | good, neutral | neutral |
| jane | jane | ru-RU | F | general | v1, v3; Realtime | evil, good, neutral | neutral |
| omazh | omazh | ru-RU | F | general | v1, v3; Realtime | evil, neutral | neutral |
| zahar | zahar | ru-RU | M | general | v1, v3; Realtime | good, neutral | neutral |
| dasha | dasha | ru-RU | F | general | v3; Realtime | friendly, good, neutral | neutral |
| julia | julia | ru-RU | F | general | v3; Realtime | neutral, strict | neutral |
| lera | lera | ru-RU | F | general | v3; Realtime | friendly, neutral | neutral |
| masha | masha | ru-RU | F | general | v3; Realtime | friendly, good, strict | good |
| marina | marina (catalog default) | ru-RU | F | general | v1, v3; Realtime | friendly, neutral, whisper | neutral |
| alexander | alexander | ru-RU | M | general | v3; Realtime | good, neutral | neutral |
| kirill | kirill | ru-RU | M | general | v3; Realtime | good, neutral, strict | neutral |
| anton | anton | ru-RU | M | general | v3; Realtime | good, neutral | neutral |
| madi_ru | madi_ru | ru-RU | M | general | v1, v3; Realtime | — | — |
| saule_ru | saule_ru | ru-RU | F | general | v3; Realtime | neutral, strict, whisper | neutral |
| zamira_ru | zamira_ru | ru-RU | F | general | v3; Realtime | friendly, neutral, strict | neutral |
| zhanar_ru | zhanar_ru | ru-RU | F | general | v3; Realtime | friendly, neutral, strict | neutral |
| yulduz_ru | yulduz_ru | ru-RU | F | general | v3; Realtime | friendly, neutral, strict, whisper | neutral |
| nigora | nigora | uz-UZ | F | general | v1, v3; Realtime | — | — |
| zamira | zamira | uz-UZ | F | general | v3; Realtime | friendly, neutral, strict | neutral |
| yulduz | yulduz | uz-UZ | F | general | v3; Realtime | friendly, neutral, strict, whisper | neutral |
| sofia | Софья | ru-RU | F | livetts | v3; Realtime | casual, support | support |
| vera | Вера | ru-RU | F | livetts | v3; Realtime | casual, support | support |
| vasily | Василий | ru-RU | M | livetts | v3; Realtime | sales, support | support |
| sergey | Сергей | ru-RU | M | livetts | v3; Realtime | sales, support | support |
| denis | Денис | ru-RU | M | livetts | v3; Realtime | casual, formal, neutral, sales | neutral |
| irina | Ирина | ru-RU | F | livetts | v3; Realtime | formal, narrator, neutral, sales, support | neutral |

The official catalog marks Marina as its default. **Alena is available through both API v1 and API v3:** live provider requests on 2026-11-12 using `voice=alena` and neutral style returned HTTP 200 and decodable MP3 audio from both APIs (30,573 bytes; 3.816 seconds each). Invalid-voice controls were rejected, and a v3 Ermil control succeeded. Alena is absent from the current published voice catalog, so it is identified separately from that table.

The installed library currently restricts Alena to API v1. This is a client-side classification error, not evidence of a provider limitation; do not describe Alena as v1-only or merely retained for legacy compatibility. The voice/API routing specification must account for the live v1/v3 result before that library restriction is changed. Preserve explicit saved preferences rather than silently substituting Marina or another voice.

### Keep the catalog current

This table is the voice list; sample clips are optional previews. Its API availability, model family, language and roles determine whether a requested selection can run.

Before listing available voices or choosing an unfamiliar voice, check the current [official voice catalog](https://aistudio.yandex.ru/en/docs/speechkit/tts/voices). Recheck before every release and when the provider rejects a previously supported combination. Verify the whole catalog, including v1 versus v3 availability, general versus LiveTTS, roles/default roles and language; do not infer availability from a few successful audio requests.

When the official catalog changes, update this complete table and `yandex_speech/tts_voices.py` together, record the verification date, and run `python -m unittest tests.test_voice_catalog tests.test_tts_routing`. Preserve separately labeled legacy choices. These tests check consistency between the installed list and routing; they do not fetch the upstream catalog or prove that it has not changed.

If the official source cannot be checked, give the last verification date and say that current availability is unverified. Never silently substitute a different voice, API, family or role. Generate a short preview through the existing speech interface only when requested; there is no mandatory sample pack or catalog listening step.

### Agent procedure

1. Reuse the saved voice preference. Ask for a preference only when none exists or the requested combination cannot work.
2. When a comparison is requested, use the same short sentence, format and loudness; label each voice/model/role. Apply per-call choices instead of rewriting the active profile.
3. Use explicit roles in demonstrations so an unrelated saved emotion cannot accidentally select an incompatible combination.
4. Verify the actual method, voice/model/role, complete audio and delivery mode. Request a listening verdict when pronunciation or perceived quality is disputed.
5. Keep canonical written terms separate from spoken aliases. Neither a successful HTTP response nor an STT round trip proves pronunciation quality.

User settings remain explicit call → HOME/workspace user file → native profile defaults → legacy ENV defaults → built-ins. Native Hermes onboarding and migration are tracked in [#33](https://github.com/bizyumov/yandex-speech/issues/33). Ordinary Telegram voice messages retain their standard tool/delivery route. A voice attachment is not speaking in a Discord voice channel; see [Hermes delivery](hermes.md#preferences-and-voice-delivery).

[SSML phonemes](https://aistudio.yandex.ru/en/docs/speechkit/tts/markup/ssml-supported-phonemes) · [Native text markup](https://aistudio.yandex.ru/en/docs/speechkit/tts/markup/tts-markup) · [Voices](https://aistudio.yandex.ru/ru/docs/speechkit/tts/voices#livetts) · [Models](https://aistudio.yandex.ru/ru/docs/speechkit/tts/models).

## Optional previews

Generate previews on request using the selected voice, supported API/model and existing CLI/Python/native speech interface. Preserve the submitted text and label the settings. Obtain a listening verdict only for a requested comparison or disputed pronunciation; a fixed six-clip exercise is not a catalog or release requirement.
