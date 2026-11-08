# Speech quality, voices and pronunciation

Choose the requested voice and pronunciation first, then select a compatible API and model.

| Need | Route | Voice/model examples |
|---|---|---|
| XML aliases and phonemes | REST v1, explicit SSML | general / ermil with good; marina with friendly |
| New general-family voices | gRPC v3, general | alexander, kirill, anton, dasha, julia, lera |
| LiveTTS voices and natural delivery | gRPC v3, livetts | sergey/support, vasily/sales, denis/neutral |

API v3 does not mean LiveTTS. General and LiveTTS voices belong to separate model families. Preserve the user's literal voice/model/role choices.

## REST v1 with SSML

Save this as `pronunciation.ssml`:

```xml
<speak>
  <p>Изучаем <sub alias="рэст">REST</sub>.</p>
  <p>Слово <phoneme alphabet="ipa" ph="məlɐko">молоко</phoneme>.</p>
  <p>Слово <phoneme alphabet="x-sampa" ph="m@l@ko">молоко</phoneme>.</p>
</speak>
```

```bash
python /absolute/skill/scripts/tts.py --input-file ./pronunciation.ssml --text-format ssml --api-version 1 --transport rest --model general --lang ru-RU --voice ermil --emotion good --format mp3 --output ./pronunciation.mp3
```

SSML is a v1 operation. The supported phoneme feature is Russian-only. IPA `ə` maps to X-SAMPA `@`; IPA `ɐ` maps to `6`. The two milk examples above are therefore different transcriptions. Preserve the requested examples as comparisons; do not certify them as acoustically equivalent.

The complete published phoneme inventory, validation and comparison tooling is [follow-up #32](https://github.com/bizyumov/yandex-speech/issues/32). Current XML forwarding does not establish full phoneme support.

## gRPC v3 and native text markup

Stress marks go immediately before the stressed vowel, inside complete Russian sentences:

```bash
python /absolute/skill/scripts/tts.py "Перед нами старинный з+амок. sil<[300]> Мы закрываем зам+ок." --api-version 3 --transport grpc --model general --lang ru-RU --voice alexander --role good --format mp3 --output ./stress.mp3
python /absolute/skill/scripts/tts.py "Здравствуйте! Чем я могу вам помочь?" --api-version 3 --transport grpc --model livetts --lang ru-RU --voice sergey --role support --format mp3 --output ./livetts.mp3
```

Native text markup is distinct from XML SSML and is supported for Russian text in API v1 and v3:

- `+` before a stressed vowel.
- `sil<[300]>` for an internal explicit pause; maximum 7,000 ms.
- `<[tiny]>`, `<[small]>`, `<[medium]>`, `<[large]>`, `<[huge]>` for contextual pauses.
- `<[accented]>` or `**word**` for emphasis.
- `[[m ə l ɐ k o]]` for native phonetic notation.

Beginning/end pauses can be ignored. Markup is a synthesis cue; compare complete sentences rather than treating it as a sound generator.

Stock Hermes 0.23.0's standard TTS cleanup preserves the tested stress, pause, accented-tag and native phoneme examples, but removes Markdown bold and corrupts SSML closing tags. For standard Hermes replies use `<[accented]>` instead of Markdown emphasis. Run explicit SSML through this skill's CLI/Python SSML operation. Never claim that raw XML passed intact through a host cleaner without testing that route.

## Voice and role selection

`good` and `friendly` are different role identifiers. For Ermil, `good` is the cheerful role; supported roles are `neutral` and `good`. Do not silently replace it with `friendly`. Marina supports `neutral`, `whisper`, `friendly`.

### Complete official voice catalog

Verified against the official catalog on 2026-11-08 (34 current voices; implementation/table conformance checked). Primary language is the catalog's voice-training language, not an invented prohibition on every cross-language request. Realtime below is vendor availability; this skill does not implement a Realtime client.

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

The official catalog marks Marina as its default. This skill retains alena as a separately identified legacy v1 compatibility choice; it is not an extra row in the current official catalog. Preserve explicit saved preferences rather than silently changing defaults.

### Agent procedure

1. Reuse the saved voice preference. Ask for a preference only when none exists or the requested combination cannot work.
2. For comparisons use the same short sentence, format and loudness; label each voice/model/role. Apply per-call choices instead of rewriting the active profile.
3. Use explicit roles in demonstrations so an unrelated saved emotion cannot accidentally select an incompatible combination.
4. Verify the actual method, voice/model/role, complete audio and delivery mode. Request a listening verdict when pronunciation or perceived quality is disputed.
5. Keep canonical written terms separate from spoken aliases. Neither a successful HTTP response nor an STT round trip proves pronunciation quality.

User settings remain explicit call → HOME/workspace user file → native profile defaults → legacy ENV defaults → built-ins. Native Hermes onboarding and migration are tracked in [#33](https://github.com/bizyumov/yandex-speech/issues/33). Ordinary Telegram voice messages retain their standard tool/delivery route. A voice attachment is not speaking in a Discord voice channel; see [Hermes delivery](hermes.md#preferences-and-voice-delivery).

[SSML phonemes](https://aistudio.yandex.ru/en/docs/speechkit/tts/markup/ssml-supported-phonemes) · [Native text markup](https://aistudio.yandex.ru/en/docs/speechkit/tts/markup/tts-markup) · [Voices](https://aistudio.yandex.ru/ru/docs/speechkit/tts/voices#livetts) · [Models](https://aistudio.yandex.ru/ru/docs/speechkit/tts/models).

## Listening check

The release comparison produces labeled Ermil/Good plain versus SSML, Alexander/Good plain versus native markup, and the same ordinary phrase with Marina/Friendly and LiveTTS Sergey/Support. Provider MP3 and normalized WAV are retained separately. Compare the same logical text with the requested controls, keep client request metadata distinct from server acknowledgement, and record the listener verdict. IPA and X-SAMPA examples above remain intentionally different transcriptions.
