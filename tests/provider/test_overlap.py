"""Opt-in synthetic cut-boundary acceptance for 30/5/25 REST-v1 windows (#19)."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path

import requests

from yandex_speech.config import RuntimeContext
from yandex_speech.transcription import SpeechTranscriber


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1",
                     "explicit synthetic provider opt-in required")
class LiveOverlapTests(unittest.TestCase):
    def test_annotated_cut_word_repeated_request_and_final_tail(self):
        key = os.environ.get("YANDEX_SPEECH_API_KEY")
        self.assertTrue(key, "existing service-account key required; tests create no credentials")
        destination = os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR")
        if destination:
            root = Path(destination) / ("overlap-" + uuid.uuid4().hex)
            root.mkdir(parents=True, mode=0o700)
        else:
            directory = tempfile.TemporaryDirectory(prefix="yandex-overlap-")
            self.addCleanup(directory.cleanup)
            root = Path(directory.name)
        texts = [
            "Начало записи. Повтори припев. Число двадцать три.",
            "Теперь важно услышать слово",
            "Достопримечательности",
            "Контрольный хвост завершён.",
        ]
        clips = []
        for text in texts:
            response = requests.post(
                "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize",
                headers={"Authorization": "Api-Key " + key},
                data={"text": text, "voice": "filipp", "lang": "ru-RU",
                      "format": "lpcm", "sampleRateHertz": "16000"},
                timeout=(10, 120))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content and len(response.content) % 2 == 0)
            clips.append(response.content)
        pcm = bytearray(56 * 32000)
        word_start = 30 * 16000 - len(clips[2]) // 4
        starts = [16000, word_start - len(clips[1]) // 2 - 800,
                  word_start, 56 * 16000 - len(clips[3]) // 2 - 1600]
        placements = [(starts[0], clips[0]), (starts[1], clips[1]),
                      (starts[2], clips[2]), (38 * 16000, clips[0]),
                      (starts[3], clips[3])]
        for start, clip in placements:
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(start * 2 + len(clip), len(pcm))
            pcm[start * 2:start * 2 + len(clip)] = clip
        source = root / "annotated-56-seconds.wav"
        with wave.open(str(source), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
            output.writeframes(pcm)
        source.chmod(0o600)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        client = SpeechTranscriber(api_key=key, _context=RuntimeContext(user_home=root))
        result = client.transcribe_chunked_direct(
            source, literature_text=False, api_version="1", transport="rest", deadline_seconds=120)
        normalized = re.sub(r"[^а-яё0-9]+", " ", result["text"].casefold()).strip()
        windows = result["raw"]["windows"]
        report = {
            "case": self.id(), "source": "synthetic SpeechKit v1 clips; no operational audio",
            "source_sha256": source_hash, "source_seconds": 56,
            "cut_word_clip_start_seconds": starts[2] / 16000,
            "cut_word_clip_end_seconds": (starts[2] + len(clips[2]) // 2) / 16000,
            "cut_seconds": 30, "windows": windows,
            "cut_word_present": "достопримечательности" in normalized,
            "repeated_request_count": normalized.count("повтори припев"),
            "final_tail_present": "контрольный хвост" in normalized,
            "uncertainties": len(result["uncertainties"]),
            "raw_hypotheses": len(result["raw"]["hypotheses"]),
            "transcript_sha256": hashlib.sha256(result["text"].encode()).hexdigest(),
            "timing_source": result["metadata_availability"]["timing"],
            "api_version": result["api_version"], "transport": result["transport"],
        }
        cli_output = root / "cli-transcript.json"
        cli = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/transcribe.py"),
            str(source), "--mode", "direct-chunked", "--api-version", "1", "--transport", "rest",
            "--no-literature-text", "--format", "json", "--output", str(cli_output),
            "--deadline-seconds", "120"], cwd=root,
            env={"HOME": str(root), "PATH": os.environ["PATH"], "VIRTUAL_ENV": sys.prefix,
                 "YANDEX_SPEECH_API_KEY": key}, capture_output=True, text=True, timeout=150)
        self.assertEqual(cli.returncode, 0)
        cli_result = json.loads(cli_output.read_text())
        report["cli_success"] = True
        report["cli_boundary_warning"] = "Overlap reconciliation:" in cli.stderr
        report["cli_raw_windows"] = len(cli_result["raw"]["windows"])
        receipt = root / "acceptance.json"
        with receipt.open("x") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        receipt.chmod(0o600)
        self.assertEqual([w["start_seconds"] for w in windows], [0, 25, 50])
        self.assertEqual([w["size_bytes"] for w in windows], [960000, 960000, 192000])
        self.assertLess(report["cut_word_clip_start_seconds"], 30)
        self.assertGreater(report["cut_word_clip_end_seconds"], 30)
        self.assertTrue(report["cut_word_present"])
        self.assertEqual(report["repeated_request_count"], 2)
        self.assertTrue(report["final_tail_present"])
        self.assertEqual(report["timing_source"], "none")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
        self.assertTrue(result["input_complete"])
        self.assertTrue(report["cli_boundary_warning"])
        self.assertEqual(report["cli_raw_windows"], 3)
