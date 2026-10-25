"""Create synthetic acceptance data using explicitly supplied existing credentials."""
from __future__ import annotations

import json
import os
import wave
from pathlib import Path

import requests


SAMPLES = [
    ("filipp", "Проверка распознавания. Число двадцать три. Слово сука. Контрольный хвост."),
    ("jane", "Добрый день. Говорит второй участник. Обсуждаем план встречи и проверяем разные голоса."),
    ("filipp", "Первый участник снова говорит. Проверка двух говорящих завершена. Последняя контрольная фраза."),
]


def create_fixtures(directory: Path, api_key: str):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    targets = [directory / "single.wav", directory / "two-speaker.wav", directory / "fixture.json"]
    if any(p.exists() for p in targets):
        raise RuntimeError("choose an empty fixture destination; existing data is preserved")
    pcm = []
    for voice, text in SAMPLES:
        response = requests.post("https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize",
            headers={"Authorization": "Api-Key " + api_key},
            data={"text": text, "voice": voice, "lang": "ru-RU", "format": "lpcm",
                  "sampleRateHertz": "16000"}, timeout=(10, 120))
        if response.status_code != 200:
            raise RuntimeError(f"synthetic TTS fixture request failed with HTTP {response.status_code}; no credential or response body is logged")
        if not response.content or len(response.content) % 2:
            raise RuntimeError("synthetic source returned invalid PCM")
        pcm.append(response.content)
    for path, data in [(targets[0], pcm[0]), (targets[1], (b"\0" * 19200).join(pcm))]:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(data)
    manifest = {"source": "synthetic SpeechKit v1 voice fixture; no operational audio",
                "sample_rate_hertz": 16000, "channels": 1, "voices": [x[0] for x in SAMPLES],
                "text": [x[1] for x in SAMPLES], "synthesized_characters": sum(len(x[1]) for x in SAMPLES),
                "single_seconds": len(pcm[0]) / 32000,
                "two_speaker_seconds": (sum(map(len, pcm)) + 38400) / 32000,
                "single_tail": "контрольный хвост", "expected_speakers": 2}
    targets[2].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def fixture_dir():
    path = os.environ.get("YANDEX_SPEECH_TEST_FIXTURE_DIR")
    if not path:
        raise RuntimeError("set YANDEX_SPEECH_TEST_FIXTURE_DIR to the reviewed synthetic fixture directory")
    root = Path(path)
    if not all((root / name).is_file() for name in ["single.wav", "two-speaker.wav", "fixture.json"]):
        raise RuntimeError("reviewed synthetic fixtures are incomplete")
    return root
