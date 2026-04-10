# GitHub issue #12 — [speech] Add TTS (Text-to-Speech) support

Source: https://github.com/bizyumov/yandex-office/issues/12

## Summary

Add Text-to-Speech (TTS) support to the speech sub-skill using Yandex SpeechKit TTS API.

## API Reference

**Endpoint:** `POST https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize`

### Authentication
- Header: `Authorization: Api-Key {YANDEX_SPEECH_API_KEY}`
- API Key location: `.env` file as `YANDEX_SPEECH_API_KEY`

### Request Parameters (form-urlencoded)

| Parameter | Required | Description |
|-----------|----------|-------------|
| `text` | Yes | Text to synthesize (max 5000 chars for Pro voices) |
| `lang` | Yes | Language code: `ru-RU`, `en-US`, `tr-TR`, `kk-KZ` |
| `voice` | No | Voice name. Default depends on language |
| `format` | No | Audio format: `mp3`, `oggopus`, `lpcm`. Default: `oggopus` |
| `speed` | No | Speech speed: `0.5` to `3.0`. Default: `1.0` |
| `emotion` | No | Voice emotion: `neutral`, `good`, `evil`. Default: `neutral` |
| `folderId` | Yes | Yandex Cloud Folder ID |

### Voices for Russian (ru-RU)

| Voice | Gender | Type | Description |
|-------|--------|------|-------------|
| `alena` | Female | Standard | Default female voice |
| `filipp` | Male | Standard | Default male voice |
| `jane` | Female | Standard | Alternative female |
| `omazh` | Female | Standard | Alternative female |
| `zahar` | Male | Standard | Alternative male |
| `ermil` | Male | Standard | Alternative male |

### Response

- Success: Raw audio data (MP3/OGG/LPCM)
- Error: JSON with `error_code` and `error_message`

### Example Request

```bash
curl -X POST "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize" \
  -H "Authorization: Api-Key ${YANDEX_SPEECH_API_KEY}" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "folderId=${YANDEX_CLOUD_FOLDER_ID}" \
  -d "text=Привет, мир" \
  -d "lang=ru-RU" \
  -d "voice=alena" \
  -d "format=mp3"
```

### Error Codes

| Code | Description |
|------|-------------|
| `UNAUTHORIZED` | Invalid or missing API key |
| `BAD_REQUEST` | Invalid parameters |
| `LIMIT_EXCEEDED` | Text too long |

## Proposed CLI Interface

```bash
# Basic TTS
python speech/scripts/tts.py "Привет, мир" --output hello.mp3

# With options
python speech/scripts/tts.py "Текст для синтеза" \
  --voice alena \
  --emotion good \
  --speed 1.2 \
  --format mp3 \
  --output output.mp3

# From file
python speech/scripts/tts.py --input-file text.txt --output speech.mp3
```

## Proposed Python API

```python
from speech.scripts.tts import TextToSpeech
import os

tts = TextToSpeech(
    folder_id=os.getenv("YANDEX_CLOUD_FOLDER_ID"),
    api_key=os.getenv("YANDEX_SPEECH_API_KEY")
)

# Synthesize to file
tts.synthesize(
    text="Привет, мир",
    output_path="hello.mp3",
    voice="alena",
    lang="ru-RU",
    format="mp3",
    speed=1.0,
    emotion="neutral"
)

# Synthesize to bytes
audio_data = tts.synthesize_bytes(
    text="Текст",
    voice="filipp"
)
```

## Configuration

Add to `config.json`:

```json
{
  "speech": {
    "tts": {
      "default_voice": "alena",
      "default_lang": "ru-RU",
      "default_format": "mp3",
      "default_speed": 1.0,
      "default_emotion": "neutral"
    }
  }
}
```

## Implementation Notes

1. **Environment variables**: Use `YANDEX_SPEECH_API_KEY` from `.env`
2. **Folder ID**: Use `YANDEX_CLOUD_FOLDER_ID` from `.env`
3. **Long text handling**: Split text > 5000 chars into chunks, synthesize separately, concatenate audio
4. **Voice selection**: Map language to default voices
5. **Error handling**: Retry on rate limits, validate text length before request

## Related

- Issue #11 - Speech-to-Text (STT) with diarization
- Yandex SpeechKit TTS docs: https://yandex.cloud/ru/docs/speechkit/tts/

## Acceptance Criteria

- [ ] TTS CLI script with all major options
- [ ] Python API for programmatic use
- [ ] Support for all Russian voices
- [ ] Support for MP3 and OGG formats
- [ ] Long text chunking (>5000 chars)
- [ ] Configuration integration
- [ ] Error handling and retries

