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
