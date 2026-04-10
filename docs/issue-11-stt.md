# GitHub issue #11 — New sub-skill: speech (Speech-to-Text with diarization)

Source: https://github.com/bizyumov/yandex-office/issues/11

## Summary

Create a new sub-skill `speech/` for high-quality speech-to-text transcription using Yandex SpeechKit with production-grade features.

## Motivation

Current basic transcription (chunking + simple API calls) lacks critical features for meeting/webinar transcription:
- Word loss at chunk boundaries
- No speaker identification
- No timestamps for navigation
- No proper handling of long audio files

---

## Yandex SpeechKit API Overview

SpeechKit provides **two distinct recognition methods** with different use cases and limitations:

### 1. Synchronous API (Streaming Recognition)

**Endpoint:** `POST /speech/stt/v3/recognizeStreaming` (WebSocket/gRPC streaming)

**Use case:** Real-time recognition, short audio fragments

**Limitations:**
- Maximum audio duration: **~1 minute**
- Maximum request size: **1 MB**
- Audio is sent in chunks via streaming connection
- No file upload support — audio bytes sent in stream

**When to use:**
- Real-time transcription during live calls
- Short voice commands
- Microphone input streaming

**Not suitable for:** Meeting recordings, podcasts, webinars

---

### 2. Asynchronous API (Long Running Recognition)

**Endpoint:** `POST /speech/stt/v2/longRunningRecognize`

**Use case:** Batch processing of long audio files

**Capabilities:**
- Maximum audio duration: **4 hours**
- Maximum file size: **1 GB** (when using Object Storage)
- File must be stored in **Yandex Cloud Object Storage** (S3)
- Returns operation ID for polling
- Results stored for 72 hours (3 days)

**Workflow:**
1. Upload audio file to Yandex Cloud Object Storage bucket
2. Call `longRunningRecognize` with Object Storage URI
3. Receive `operationId` in response
4. Poll operation status via `GET /operations/{operationId}`
5. Retrieve result when `done: true`

**URI format for Object Storage:**
```
https://storage.yandexcloud.net/bucket-name/path/to/file.mp3
```

**Request example:**
```json
POST https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize
Authorization: Api-Key {API_KEY}
Content-Type: application/json

{
  "config": {
    "specification": {
      "audioEncoding": "MP3",
      "sampleRateHertz": 48000,
      "languageCode": "ru-RU",
      "speakerLabeling": true,
      "profanityFilter": false
    }
  },
  "audio": {
    "uri": "https://storage.yandexcloud.net/my-bucket/meeting.mp3"
  }
}
```

**Response:**
```json
{
  "id": "e03sup6d5h1q********",
  "description": "speech:LongRunningRecognize",
  "createdAt": "2024-01-15T10:30:00Z",
  "createdBy": "ajeshech",
  "modifiedAt": "2024-01-15T10:30:00Z",
  "done": false
}
```

---

## SpeechKit Limits Summary

| Parameter | Sync API (Streaming) | Async API (longRunningRecognize) |
|-----------|---------------------|----------------------------------|
| **Max duration** | ~1 minute | 4 hours |
| **Max file size** | 1 MB | 1 GB (via Object Storage) |
| **Audio source** | Streamed bytes | Object Storage URI |
| **Response** | Real-time chunks | Poll operation, then download |
| **Result storage** | Immediate | 72 hours |
| **Chunking needed** | No (streaming) | No (whole file) |

**Key point:** For files up to 4 hours, async API takes the WHOLE FILE from Object Storage. No chunking required at the client side.

---

## Requirements for Sub-skill

### 1. Speaker Diarization

**Problem:** Cannot distinguish who is speaking in multi-speaker recordings.

**Solution:**
- Use Yandex SpeechKit `speakerLabeling` parameter (`speaker_labeling=true`)
- Parse `speakerTag` from API response (1, 2, 3...)
- Map to human-readable labels (Speaker A, Speaker B)

**Response format with diarization:**
```json
{
  "chunks": [
    {
      "alternatives": [{
        "words": [
          {"word": "привет", "startTime": "0.5s", "endTime": "0.8s"},
          {"word": "всем", "startTime": "0.9s", "endTime": "1.1s"}
        ],
        "text": "привет всем"
      }],
      "channelTag": 1,
      "speakerTag": 1
    }
  ]
}
```

### 2. Timestamps

**Problem:** Cannot navigate to specific moments in audio from transcript.

**Solution:**
- Extract `startTime` and `endTime` for each word from API response
- Build phrase-level timestamps by grouping words

**Output formats:**

**Plain text with timestamps:**
```
[00:05:32] Speaker A: Привет всем, начинаем вебинар
[00:05:38] Speaker B: Добрый день, готов к обсуждению
```

**SRT subtitles:**
```
1
00:05:32,000 --> 00:05:38,000
Speaker A: Привет всем, начинаем вебинар

2
00:05:38,000 --> 00:05:45,000
Speaker B: Добрый день, готов к обсуждению
```

**VTT (WebVTT):**
```
WEBVTT

00:05:32.000 --> 00:05:38.000
Speaker A: Привет всем, начинаем вебинар

00:05:38.000 --> 00:05:45.000
Speaker B: Добрый день, готов к обсуждению
```

**JSON:**
```json
{
  "segments": [
    {
      "start": 332.0,
      "end": 338.0,
      "speaker": "A",
      "text": "Привет всем, начинаем вебинар"
    }
  ]
}
```

### 3. Long Audio Handling (CORRECTED)

**Previous misunderstanding:** Issue stated chunking needed for files > 1 hour.

**Correct approach:**
- **Files ≤ 1 minute:** Can use sync streaming API (if real-time needed)
- **Files ≤ 4 hours:** Use async API with Object Storage — NO CHUNKING NEEDED
- **Files > 4 hours:** Must split into multiple files (4h chunks)

**Implementation:**
```python
if duration_minutes <= 1 and real_time_needed:
    use_sync_streaming()
elif duration_hours <= 4:
    upload_to_object_storage()
    use_async_long_running()
else:
    split_into_4h_chunks()
    process_chunks_sequentially()
```

### 4. Object Storage Integration

Since async API requires Object Storage:

**Prerequisites:**
- Yandex Cloud bucket created
- Service account with `storage.uploader` and `storage.viewer` roles
- Access key (or IAM token with appropriate permissions)

**Upload workflow:**
```python
import boto3

s3 = boto3.client(
    's3',
    endpoint_url='https://storage.yandexcloud.net',
    aws_access_key_id='YOUR_KEY_ID',
    aws_secret_access_key='YOUR_SECRET'
)

# Upload file
s3.upload_file('local.mp3', 'my-bucket', 'audio/meeting.mp3')

# Get URI for SpeechKit
uri = 'https://storage.yandexcloud.net/my-bucket/audio/meeting.mp3'
```

---

## Proposed Architecture

```
speech/
├── speech.md              # Documentation (this content)
├── scripts/
│   ├── transcribe.py      # Main transcription script
│   ├── upload_to_s3.py    # Upload to Object Storage
│   └── poll_operation.py  # Poll async operation status
└── requirements.txt
```

---

## CLI Design

```bash
# Upload to Object Storage and transcribe async
python speech/scripts/transcribe.py input.mp3 \
  --bucket my-bucket \
  --output transcript.txt \
  --diarize \
  --timestamps

# Output as SRT subtitles
python speech/scripts/transcribe.py input.mp3 \
  --bucket my-bucket \
  --format srt \
  --output subtitles.srt

# Poll existing operation
python speech/scripts/poll_operation.py e03sup6d5h1q******** \
  --output result.json
```

---

## Python API Design

```python
from speech.scripts.transcribe import SpeechTranscriber
from speech.scripts.upload import ObjectStorageUploader

# Upload to S3 first
uploader = ObjectStorageUploader(
    bucket='my-bucket',
    access_key='...',
    secret_key='...'
)
uri = uploader.upload('meeting.mp3')

# Transcribe
transcriber = SpeechTranscriber(
    api_key="...",
    enable_diarization=True,
    output_format="srt"
)

operation_id = transcriber.start_transcription(uri)
result = transcriber.poll_until_complete(operation_id)

# result contains:
# - segments with timestamps
# - speaker tags
# - full text
```

---

## Configuration

```json
{
  "speech": {
    "object_storage": {
      "bucket": "my-speech-bucket",
      "endpoint": "https://storage.yandexcloud.net"
    },
    "default_format": "txt",
    "enable_diarization": true,
    "enable_timestamps": true,
    "language_code": "ru-RU",
    "profanity_filter": false
  }
}
```

---

## Implementation Notes

### Required API Parameters

**For async recognition:**
- `audioEncoding`: MP3, OGG_OPUS, LINEAR16_PCM
- `sampleRateHertz`: 48000, 16000, 8000 (must match actual file)
- `languageCode`: ru-RU, en-US, etc.
- `speakerLabeling`: true (for diarization)
- `profanityFilter`: true/false

### Response Processing

The async API returns `response` field when operation completes:

```json
{
  "id": "e03sup6d5h1q********",
  "done": true,
  "response": {
    "chunks": [
      {
        "alternatives": [...],
        "speakerTag": 1
      }
    ]
  }
}
```

Each `chunk` represents a continuous speech segment from one speaker.

---

## SpeechSense vs SpeechKit

**Yandex SpeechSense** — separate high-level analytics service built on SpeechKit:

| | SpeechKit | SpeechSense |
|---|---|---|
| **Level** | Low-level API | SaaS for business |
| **Function** | STT/TTS only | Dialog analytics + GPT |
| **Diarization** | Manual via API | Automatic |
| **AI analysis** | No | YandexGPT: summary, tags, sentiment |
| **Integrations** | API only | Phone, CRM, chats (built-in) |

For sub-skill "speech" we focus on **SpeechKit** (STT). SpeechSense is a separate higher abstraction level.

---

## Acceptance Criteria

- [ ] Async transcription works with Object Storage files up to 4 hours
- [ ] Diarization identifies different speakers correctly
- [ ] Timestamps accurate (word-level from API)
- [ ] Output formats: TXT, SRT, VTT, JSON
- [ ] Progress reporting during operation polling
- [ ] CLI and Python API documented
- [ ] Integration with shared config system

## Related

- Yandex SpeechKit docs: https://yandex.cloud/ru/docs/speechkit/stt/
- Yandex Object Storage docs: https://yandex.cloud/ru/docs/storage/
- Test file available: `2026-03-06_104528_Meeting_in_Yandex_Telemost_1992324783_audio_only.mp3` (54 MB, 1h+ duration)

## Priority

High - needed for webinar/meeting transcription workflow.


