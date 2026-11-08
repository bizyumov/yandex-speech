"""Create six labeled pronunciation/style comparisons; listening is a separate verdict."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import wave
from pathlib import Path
import subprocess
import time
import unittest
import uuid

from scripts.tts import TextToSpeech
from yandex_speech.config import RuntimeContext


def comparison_cases():
    ordinary = "Изучаем REST. Слово молоко. Слово молоко."
    ssml = ('<speak><p>Изучаем <sub alias="рэст">REST</sub>.</p>'
            '<p>Слово <phoneme alphabet="ipa" ph="məlɐko">молоко</phoneme>.</p>'
            '<p>Слово <phoneme alphabet="x-sampa" ph="m@l@ko">молоко</phoneme>.</p></speak>')
    stress = "Перед нами старинный замок. Мы закрываем замок. Произносим молоко."
    native = "Перед нами старинный з+амок. sil<[300]> Мы закрываем зам+ок. <[accented]> Произносим [[m ə l ɐ k o]]."
    return [
        {"id": "01-ermil-good-plain", "text": ordinary, "api_version": "1", "transport": "rest",
         "voice": "ermil", "role": "good", "model": "general", "text_format": "plain"},
        {"id": "02-ermil-good-ssml", "text": ssml, "api_version": "1", "transport": "rest",
         "voice": "ermil", "role": "good", "model": "general", "text_format": "ssml"},
        {"id": "03-alexander-good-plain", "text": stress, "api_version": "3", "transport": "grpc",
         "voice": "alexander", "role": "good", "model": "general", "text_format": "plain"},
        {"id": "04-alexander-good-markup", "text": native, "api_version": "3", "transport": "grpc",
         "voice": "alexander", "role": "good", "model": "general", "text_format": "plain"},
        {"id": "05-marina-friendly", "text": ordinary, "api_version": "1", "transport": "rest",
         "voice": "marina", "role": "friendly", "model": "general", "text_format": "plain"},
        {"id": "06-sergey-support", "text": ordinary, "api_version": "3", "transport": "grpc",
         "voice": "sergey", "role": "support", "model": "livetts", "text_format": "plain"},
    ]


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1", "explicit synthesis usage opt-in required")
class LiveVoiceComparisonTests(unittest.TestCase):
    def test_create_reviewable_pronunciation_and_style_comparisons(self):
        key = os.environ.get("YANDEX_SPEECH_API_KEY")
        destination = os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR")
        self.assertTrue(key and destination, "existing authorized key and durable comparison output required")
        root = Path(destination) / ("voice-comparisons-" + uuid.uuid4().hex)
        root.mkdir(parents=True, mode=0o700)
        client = TextToSpeech(api_key=key, _context=RuntimeContext(user_home=root))
        records = []
        for case in comparison_cases():
            raw = root / (case["id"] + "-provider.mp3")
            started = time.monotonic()
            options = {name: case[name] for name in ("api_version", "transport", "voice", "role", "model", "text_format")}
            paths = client.synthesize(case["text"], raw, lang="ru-RU", fmt="mp3", **options)
            self.assertEqual(paths, [raw])
            actual = dict(client.last_synthesis)
            for name in ("api_version", "transport", "voice", "role", "model"):
                self.assertEqual(actual[name], case[name])
            self.assertEqual(actual["format"], "mp3")
            self.assertIsNone(actual["conversion"])
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration,format_name:stream=codec_name", "-of", "json", str(raw)],
                capture_output=True, text=True, check=True)
            observed = json.loads(probe.stdout)
            self.assertEqual(observed["format"]["format_name"], "mp3")
            self.assertGreater(float(observed["format"]["duration"]), 0)
            normalized = root / (case["id"] + ".wav")
            normalize = subprocess.run(["ffmpeg", "-v", "error", "-n", "-i", str(raw),
                "-af", "loudnorm=I=-19:TP=-2:LRA=7", "-ar", "24000", "-ac", "1",
                "-c:a", "pcm_s16le", str(normalized)], capture_output=True, text=True)
            self.assertEqual(normalize.returncode, 0, "comparison loudness normalization failed")
            self.assertTrue(normalized.stat().st_size > 44)
            with wave.open(str(normalized), "rb") as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 24000))
            measured = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(normalized), "-af",
                "loudnorm=I=-19:TP=-2:LRA=7:print_format=json", "-f", "null", "-"],
                capture_output=True, text=True, check=True)
            match = re.search(r'\{\s*"input_i"[\s\S]*?\}', measured.stderr)
            self.assertIsNotNone(match, "loudness measurement missing")
            loudness = float(json.loads(match.group(0))["input_i"])
            self.assertTrue(math.isfinite(loudness))
            self.assertLessEqual(abs(loudness + 19), 1.0)
            raw.chmod(0o600)
            normalized.chmod(0o600)
            records.append({**case, "actual_client_route": actual,
                "route_observation": "successful client request selection; provider audio has no voice/model echo",
                "provider_audio": raw.name, "provider_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                "listen_audio": normalized.name, "listen_sha256": hashlib.sha256(normalized.read_bytes()).hexdigest(),
                "provider_duration_seconds": float(observed["format"]["duration"]),
                "listen_format": "24kHz mono PCM16 WAV", "loudness_target_lufs": -19, "measured_loudness_lufs": loudness,
                "elapsed_seconds": time.monotonic() - started, "listener_verdict": "pending"})
        receipt = root / "acceptance.json"
        with receipt.open("x") as output:
            json.dump({"case": self.id(), "samples": records, "source": "synthetic comparison text only",
                "same_logical_text_groups": [[records[i]["id"] for i in (0, 1, 4, 5)],
                                             [records[i]["id"] for i in (2, 3)]],
                "acoustic_acceptance": "pending-listener",
                "note": "IPA and X-SAMPA milk transcriptions are intentionally distinct; API success is not a pronunciation verdict."},
                output, ensure_ascii=False, indent=2)
        receipt.chmod(0o600)
        print("Voice comparisons for listening:", root)
