"""Explicit synthetic API acceptance; never runs in ordinary offline CI."""
import hashlib
import json
import os
import tempfile
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from tests.provider.fixtures import fixture_dir
from yandex_speech.config import RuntimeContext
from yandex_speech.output import write_output
from yandex_speech.transcription import SpeechTranscriber


def late_control_tail(result, after_seconds):
    import math
    import re
    for segment in result.get("segments", []):
        if segment.get("timing_source") != "provider":
            continue
        words = segment.get("words", [])
        for left, right in zip(words, words[1:]):
            labels = [re.sub(r"[^а-яё]+", "", word.get("word", "").casefold()) for word in (left, right)]
            if labels != ["контрольный", "хвост"]:
                continue
            times = [left.get("start"), left.get("end"), right.get("start"), right.get("end")]
            if (all(type(value) in (int, float) and math.isfinite(value) for value in times)
                    and after_seconds <= times[0] < times[1]
                    and times[0] <= times[2] < times[3]):
                return True
    return False

@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1", "explicit provider opt-in required")
class LiveRecordingTests(unittest.TestCase):
    def setUp(self):
        self.key = os.environ.get("YANDEX_SPEECH_API_KEY")
        if not self.key:
            self.fail("existing YANDEX_SPEECH_API_KEY is required; no key is created by tests")
        self.fixtures = fixture_dir()
        destination = os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR")
        if destination:
            self.root = Path(destination) / ("recording-" + uuid.uuid4().hex)
            self.root.mkdir(parents=True, mode=0o700)
        else:
            self.tmp = tempfile.TemporaryDirectory(prefix="yandex-speech-acceptance-")
            self.addCleanup(self.tmp.cleanup)
            self.root = Path(self.tmp.name)
        self.context = RuntimeContext(user_home=self.root)
        self.client = SpeechTranscriber(api_key=self.key, job_dir=self.root / "jobs",
                                        _context=self.context)

    def test_real_recording_receipt_resume_literary_and_exports(self):
        path = self.fixtures / "single.wav"
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        job = self.client.start_transcription(path, timestamps=True, deadline_seconds=120)
        self.assertIsNotNone(job.operation_id)
        recovered = SpeechTranscriber(api_key=self.key, _context=self.context,
                                      job_dir=self.root / "jobs")
        result = recovered.poll_until_complete(job.manifest_path, deadline_seconds=120)
        text = result["text"].casefold()
        self.assertIn("контрольный хвост", text)
        self.assertIn("23", text)
        self.assertIn("сука", text)
        self.assertTrue(result["text"][0].isupper())
        self.assertTrue(any(c in result["text"] for c in ".!?"))
        self.assertTrue(result["input_complete"])
        self.assertEqual(result["metadata_availability"]["text_representation"], "literary")
        for fmt in ["json", "txt", "srt", "vtt"]:
            output = self.root / ("transcript." + fmt)
            write_output(result, output, fmt)
            self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
        recovered_output=self.root/"external-recovered.txt"
        command=Path(__file__).resolve().parents[2]/"scripts/transcribe.py"
        external=subprocess.run([sys.executable,str(command),"--resume",str(job.manifest_path),
            "--format","txt","--output",str(recovered_output)],cwd=self.root,
            env={"HOME":str(self.root),"PATH":os.environ["PATH"]},capture_output=True,text=True)
        self.assertEqual(external.returncode,0,external.stderr)
        self.assertIn("контрольный хвост",recovered_output.read_text().casefold())
        (self.root / "acceptance.json").write_text(json.dumps({
            "case": self.id(), "result": "passed", "operation_id": job.operation_id,
            "job_manifest": str(job.manifest_path), "api_version": result["api_version"],
            "segments": len(result["segments"]), "tail_verified": True,
            "literary_verified": True, "raw_preserved": bool(result["raw"])}, indent=2))

    def test_native_stereo_mp3_uses_encoded_bytes_with_nonzero_start(self):
        import wave
        from unittest.mock import patch
        from yandex_speech.grpc_stt import MAX_GRPC_BYTES
        from tests.provider.test_recording_maximum import write_sparse_recording
        with wave.open(str(self.fixtures / "single.wav"), "rb") as source:
            self.assertEqual((source.getnchannels(), source.getsampwidth(), source.getframerate()), (1, 2, 16000))
            marker = source.readframes(source.getnframes())
        sparse = self.root / "decoded-under-test.wav"
        write_sparse_recording(sparse, marker, 1900 * 16000, [0, 1900 * 16000 - len(marker) // 2])
        path = self.root / "native-stereo.mp3"
        encoded = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-n", "-i", str(sparse),
            "-ac", "2", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(path)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(encoded.returncode, 0)
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertGreater(1900 * 16000 * 2, MAX_GRPC_BYTES)
        self.assertLess(path.stat().st_size + 4096, MAX_GRPC_BYTES)
        from yandex_speech.media import _tool
        def no_expansion(name):
            self.assertNotEqual(name, "ffmpeg", "eligible native MP3 must not be expanded")
            return _tool(name)
        with patch("yandex_speech.media._tool", side_effect=no_expansion):
            job = self.client.start_transcription(path, diarize=False, timestamps=True,
                api_version="3", transport="grpc", deadline_seconds=180)
        result = self.client.poll_until_complete(job, deadline_seconds=180)
        self.assertTrue(result["input_complete"])
        self.assertTrue(late_control_tail(result, 1800), "the final marker tail needs its own genuine late word times")
        info = job.data["source_info"]
        self.assertFalse(info["converted"])
        self.assertEqual(info["source_sha256"], original)
        self.assertEqual(info["prepared"]["channels"], 2)
        self.assertEqual(info["prepared"]["container"], "MP3")
        self.assertGreater(info["original"]["start_time_seconds"], 0)
        self.assertEqual(info["timeline_offset_seconds"], info["original"]["start_time_seconds"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
        receipt = {"case": self.id(), "input_complete": True, "source_unchanged": True,
            "native_mp3_preserved": True, "channels": 2, "nonzero_encoder_start": True,
            "source_seconds": 1900, "encoded_bytes": path.stat().st_size,
            "decoded_mono_bytes": 1900 * 16000 * 2, "production_byte_cap": MAX_GRPC_BYTES,
            "tail_near_end_verified": True, "transport": result["transport"], "api_version": result["api_version"]}
        with (self.root / "acceptance.json").open("x") as stream:
            json.dump(receipt, stream, indent=2)
        (self.root / "acceptance.json").chmod(0o600)

    def test_real_two_speaker_timing_and_final_tail(self):
        job = self.client.start_transcription(self.fixtures / "two-speaker.wav",
            diarize=True, timestamps=True, deadline_seconds=120)
        result = self.client.poll_until_complete(job, deadline_seconds=120)
        speakers = {s["speaker"] for s in result["segments"] if s["speaker"] is not None}
        self.assertEqual(len(speakers), 2)
        self.assertIn("последняя контрольная фраза", result["text"].casefold())
        self.assertTrue(all(s["start"] is not None and s["end"] > s["start"]
                            for s in result["segments"]))
        self.assertTrue(result["input_complete"])
        write_output(result, self.root / "two-speaker.srt", "srt")
        (self.root / "acceptance.json").write_text(json.dumps({
            "case": self.id(), "result": "passed", "operation_id": job.operation_id,
            "job_manifest": str(job.manifest_path), "api_version": result["api_version"],
            "speakers": len(speakers), "segments": len(result["segments"]),
            "tail_verified": True, "provider_timing_verified": True}, indent=2))
