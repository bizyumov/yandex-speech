# RMS/HLS conference recordings → transcripts

Use this when a conference site embeds recordings through `player.rms-cloud.ru` or similar HLS players and the user asks for session transcripts.

## Discovery pattern

1. Open the embedded player in a browser and inspect network/resource URLs for:
   - `/api/events`
   - `/api/events/<slug>/playlist`
   - `.m3u8` resources
2. For RMS player, `/api/events` lists event slugs, e.g. `cipr-2026-day1`, `cipr-2026-day2`, `cipr-2026-day3`.
3. Fetch each playlist endpoint; halls usually contain `sessions[]` with:
   - `title`
   - `start_time`
   - `hls_url`
   - `archive_url`
   - `backup_hls_url`
4. Prefer `archive_url` when present; otherwise use `hls_url` or `backup_hls_url`.

Example playlist fetch:

```python
import requests
slug = "cipr-2026-day1"
data = requests.get(
    f"https://player.rms-cloud.ru/api/events/{slug}/playlist",
    headers={"Referer": "https://cipr.ru/", "User-Agent": "Mozilla/5.0"},
    timeout=30,
).json()
for hall in data.get("halls", []):
    for s in hall.get("sessions", []):
        url = s.get("archive_url") or s.get("hls_url") or s.get("backup_hls_url")
        if url:
            print(slug, hall["name"], s["start_time"], s["title"], url)
```

## Audio extraction

Use `ffmpeg` directly against the HLS URL. For quick verification, extract a 20–25 second sample from a point where people are speaking:

```bash
ffmpeg -y -hide_banner -loglevel error \
  -ss 00:10:00 -i "$HLS_URL" -t 25 \
  -vn -ac 1 -ar 16000 -c:a libopus -b:a 24k /tmp/sample.ogg
python3 /opt/hermes/shared/skills/yandex-speech/scripts/transcribe.py \
  /tmp/sample.ogg --mode direct --lang ru-RU --format txt --output /tmp/sample.txt
```

SpeechKit direct mode requires audio shorter than 30 seconds; use 25 seconds for smoke tests.

## Full-session transcription

For long sessions, chunk the HLS audio locally with `ffmpeg` into <30s OGG/Opus files and run `transcribe.py` per chunk, or use `async-s3` if Object Storage credentials are configured. Preserve absolute session offsets in output filenames/metadata so final `.txt`, `.srt`, or `.vtt` timestamps remain meaningful.

Recommended outputs:
- raw transcript `.txt` with timestamps;
- `.srt` or `.vtt` when the user needs navigation;
- Markdown summary with key claims, quotes, speakers, and “useful for us” takeaways.

## Pitfalls

- Many sites expose recordings as HLS playlists, not as downloadable videos or ready transcripts.
- Player UI may show only the current hall/session; the playlist API is the reliable index.
- The first minutes of a recording may be silence, intro music, or unrelated audio. Smoke-test from 5–10 minutes in.
- Do not claim speaker diarization unless the STT output actually includes speaker labels.
- Machine transcripts are not official stenograms; label them as automatic recognition.
