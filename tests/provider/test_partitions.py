"""Opt-in real-provider partition boundaries using the reviewed 56-second fixture."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import wave

from yandex_speech import SpeechTranscriber, rest_stt
from yandex_speech.config import RuntimeContext
from yandex_speech.jobs import JobHandle
from yandex_speech.media import PreparedAudio


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1", "explicit provider opt-in required")
class LivePartitionTests(unittest.TestCase):
    def test_small_wire_boundary_python_cli_and_cached_recovery(self):
        key = os.environ.get("YANDEX_SPEECH_API_KEY")
        self.assertTrue(key, "existing authorized service-account key required")
        supplied = os.environ.get("YANDEX_SPEECH_TEST_PARTITION_FIXTURE")
        self.assertTrue(supplied, "set the reviewed synthetic annotated 56-second WAV fixture explicitly")
        source = Path(supplied)
        with wave.open(str(source), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()),
                             (1, 2, 16000, 56 * 16000))
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR")
        if destination:
            root = Path(destination) / ("partitions-" + uuid.uuid4().hex)
            root.mkdir(parents=True, mode=0o700)
        else:
            temporary = tempfile.TemporaryDirectory(prefix="speech-partition-provider-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
        prototype = root / "prototype.wav"
        with prototype.open("xb") as stream:
            with wave.open(stream, "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(bytes(30 * 32000))
        prototype.chmod(0o600)
        cap = len(rest_stt.build_rest_request(PreparedAudio(
            prototype, "WAV", 16000, 1, 30000, {"converted": True})))
        client = SpeechTranscriber(api_key=key, job_dir=root / "jobs", _context=RuntimeContext(user_home=root))
        started = time.monotonic()
        with patch.object(rest_stt, "MAX_REST_BYTES", cap):
            job = client.start_transcription(source, split_inline=True, api_version="3", transport="rest",
                                             timestamps=True, deadline_seconds=180)
            result = client.poll_until_complete(job)
        self.assertTrue(result["input_complete"])
        self.assertEqual([part["start_frame"] for part in result["parts"]], [0, 25 * 16000, 50 * 16000])
        self.assertEqual(result["parts"][-1]["end_frame"], 56 * 16000)
        normalized = re.sub(r"[^а-яё0-9]+", " ", result["text"].casefold()).strip()
        self.assertEqual(normalized.count("повтори припев"), 2)
        self.assertIn("контрольный хвост", normalized)
        self.assertTrue(all(segment["start"] is not None and segment["end"] > segment["start"]
                            for segment in result["segments"]))
        output = root / "cli.json"
        script = Path(__file__).resolve().parents[2] / "scripts/transcribe.py"
        bootstrap = (
            "import runpy,sys; from unittest.mock import patch; "
            "sys.path.insert(0,sys.argv.pop(1)); from yandex_speech import rest_stt; "
            "cap=int(sys.argv.pop(1)); script=sys.argv.pop(1); "
            "with_patch=patch.object(rest_stt,'MAX_REST_BYTES',cap); with_patch.start(); "
            "runpy.run_path(script,run_name='__main__')"
        )
        environment = {"HOME": str(root), "PATH": os.environ["PATH"], "VIRTUAL_ENV": sys.prefix,
                       "YANDEX_SPEECH_API_KEY": key}
        cli = subprocess.run([sys.executable, "-c", bootstrap, str(script.parent.parent), str(cap), str(script),
            str(source), "--mode", "async", "--api-version", "3", "--transport", "rest", "--split-inline",
            "--timestamps", "--format", "json", "--output", str(output), "--deadline-seconds", "180"],
            env=environment, cwd=root, capture_output=True, text=True, timeout=210)
        self.assertEqual(cli.returncode, 0, "explicit partition CLI failed; private receipt/output retained")
        self.assertEqual(cli.stdout.strip(), str(output))
        cli_result = json.loads(output.read_text())
        self.assertTrue(cli_result["input_complete"])
        self.assertEqual(len(cli_result["parts"]), 3)
        pointer = json.loads(output.with_suffix(".job.json").read_text())
        parent = JobHandle.load(pointer["manifest_path"])
        recovered = root / "cached.srt"
        cache = subprocess.run([sys.executable, str(script), "--resume", str(parent.manifest_path),
            "--format", "srt", "--output", str(recovered)], cwd=root,
            env={"HOME": str(root), "PATH": os.environ["PATH"], "VIRTUAL_ENV": sys.prefix},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(cache.returncode, 0, "credential-free cached CLI recovery failed")
        self.assertTrue(recovered.stat().st_size)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
        report = {"case": self.id(), "source": "reviewed synthetic fixture; no operational audio",
                  "source_sha256": source_hash, "source_seconds": 56,
                  "test_request_wire_cap_bytes": cap, "production_wire_cap_changed": False,
                  "python_parts": len(result["parts"]), "cli_parts": len(cli_result["parts"]),
                  "every_part_complete": True, "final_tail_verified": True, "legitimate_repetition_verified": True,
                  "original_unchanged": True, "cached_cli_without_credentials": True,
                  "boundary_uncertainties": len(result["uncertainties"]),
                  "transcript_sha256": hashlib.sha256(result["text"].encode()).hexdigest(),
                  "elapsed_seconds": time.monotonic() - started,
                  "maximum_duration_acceptance": False}
        receipt = root / "acceptance.json"
        with receipt.open("x") as stream:
            json.dump(report, stream, indent=2)
        receipt.chmod(0o600)
