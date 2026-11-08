"""Explicit actual-limit uploaded-file check; never runs in ordinary CI."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import unittest
import uuid
import wave

from yandex_speech import SpeechTranscriber, grpc_stt
from yandex_speech.config import RuntimeContext
from yandex_speech.media import PreparedAudio
from yandex_speech.partition_audio import frame_windows, inline_frame_capacity
from yandex_speech.recording_request import MAX_FILE_DURATION_MS


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sparse_recording(path, marker, total_frames, placements, rate=16000):
    assert len(marker) % 2 == 0 and total_frames > 0
    marker_frames = len(marker) // 2
    assert all(0 <= start and start + marker_frames <= total_frames for start in placements)
    size = total_frames * 2
    header = struct.pack("<4sI4s4sIHHIIHH4sI",
                         b"RIFF", 36 + size, b"WAVE", b"fmt ", 16,
                         1, 1, rate, rate * 2, 2, 16, b"data", size)
    with Path(path).open("xb") as output:
        output.write(header)
        output.seek(44 + size - 1)
        output.write(b"\0")
        for start in placements:
            output.seek(44 + start * 2)
            output.write(marker)
        output.flush()
        os.fsync(output.fileno())
    Path(path).chmod(0o600)


def validate_children(result, windows, job):
    parts = result.get("parts", [])
    assert len(parts) == len(windows)
    for index, (part, window) in enumerate(zip(parts, windows)):
        assert (part["start_frame"], part["end_frame"]) == (window.start, window.end)
        assert part.get("status") in {"completed", "no_speech"}
        for field in ("child_manifest_path", "child_job_id", "operation_id",
                      "saved_result_reference", "saved_result_sha256"):
            assert part.get(field)
        child = job.child(index)
        assert child is not None
        assert Path(part["child_manifest_path"]) == child.manifest_path
        assert Path(part["saved_result_reference"]) == child.manifest_path.parent / "result.json"
        assert part["saved_result_sha256"] == child.data.get("result_sha256")
        cached = child.cached_result()
        assert cached and cached.get("input_complete") is True
        assert cached.get("status") == part["status"]
        assert cached.get("mode") == "async" and cached.get("api_version") == "3"
        assert cached.get("job_id") == part["child_job_id"]
        assert cached.get("operation_id") == part["operation_id"]

def validate_result(result, total_frames, placements, windows, job):
    assert result.get("input_complete") is True and result.get("status") == "completed"
    assert result.get("mode") == "async-partitioned" and result.get("completion_kind") == "recorded-file"
    validate_children(result, windows, job)
    parts = result["parts"]
    assert parts and parts[0]["start_frame"] == 0 and parts[-1]["end_frame"] == total_frames
    text = re.sub(r"[^а-яё0-9]+", " ", result["text"].casefold()).strip()
    assert text.count("повтори припев") == len(placements) * 2
    assert text.count("контрольный хвост") == len(placements)
    words, seen = [], set()
    for segment in result.get("segments", []):
        assert segment.get("timing_source") == "provider"
        previous_start, local_seen = None, set()
        for word in segment.get("words", []):
            start, end, token = word.get("start"), word.get("end"), word.get("word")
            assert type(start) in (int, float) and type(end) in (int, float)
            assert math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= total_frames / 16000 + 0.1
            assert isinstance(token, str) and token
            identity = (token, start, end)
            assert identity not in local_seen
            assert previous_start is None or start >= previous_start - 0.05
            previous_start = start
            local_seen.add(identity)
            if identity not in seen:
                seen.add(identity)
                words.append(word)
    words.sort(key=lambda word: (word["start"], word["end"]))
    assert words
    def occurrences(phrase, begin):
        labels, found = phrase.split(), set()
        for segment in result["segments"]:
            tokens = []
            for word in segment.get("words", []):
                if word["start"] >= begin - 0.5 and word["end"] <= begin + 56 + 0.5:
                    for label in re.sub(r"[^а-яё0-9]+", " ", word["word"].casefold()).split():
                        tokens.append((label, word["start"], word["end"]))
            for index in range(len(tokens) - len(labels) + 1):
                selected = tokens[index:index + len(labels)]
                if [item[0] for item in selected] == labels:
                    found.add(tuple(selected))
        return found

    for placement in placements:
        start = placement / 16000
        assert len(occurrences("повтори припев", start)) == 2
        assert len(occurrences("контрольный хвост", start)) == 1
    final_tails = occurrences("контрольный хвост", placements[-1] / 16000)
    assert any(min(word[1] for word in tail) >= MAX_FILE_DURATION_MS / 1000
               and max(word[2] for word in tail) > total_frames / 16000 - 15 for tail in final_tails)
    return True


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1" and
                     os.environ.get("YANDEX_SPEECH_MAXIMUM_RECORDING_TESTS") == "1",
                     "explicit provider and maximum-uploaded-recording opt-ins required")
class LiveMaximumRecordingTests(unittest.TestCase):
    def test_recording_beyond_four_hours_uses_actual_limits_and_recovers_cached(self):
        destination = os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR")
        fixture = os.environ.get("YANDEX_SPEECH_TEST_PARTITION_FIXTURE")
        self.assertTrue(destination and fixture, "explicit durable output and reviewed synthetic fixture required")
        self.assertEqual(MAX_FILE_DURATION_MS, 14_400_000)
        self.assertEqual(grpc_stt.MAX_GRPC_BYTES, 60_000_000)
        total_frames = 14456 * 16000
        resume = os.environ.get("YANDEX_SPEECH_TEST_MAXIMUM_JOB")
        started = time.monotonic()
        if resume:
            from yandex_speech.partition_jobs import PartitionJob
            job = PartitionJob.load(resume)
            self.assertIn(job.data["status"], {"completed", "no_speech"})
            original = Path(job.data["source_ref"])
            root = original.parent
            annotation = json.loads((root / "fixture.json").read_text())
            self.assertEqual(annotation["source_frames"], total_frames)
            original_hash = annotation["source_sha256"]
            self.assertEqual(hash_file(original), original_hash)
            result = job.cached_result()
            self.assertIsNotNone(result, "completed cached aggregate is required; replay must not submit")
            capacity = inline_frame_capacity(("grpc",), root, language="ru-RU", diarize=False,
                                             literature_text=True, channels=1)
            windows = frame_windows(total_frames, capacity, 16000)
            placements = annotation["placements_frames"]
        else:
            key = os.environ.get("YANDEX_SPEECH_API_KEY")
            self.assertTrue(key, "existing authorized SpeechKit credential required")
            root = Path(destination) / ("maximum-recording-" + uuid.uuid4().hex)
            root.mkdir(parents=True, mode=0o700)
            self.assertGreaterEqual(shutil.disk_usage(root).free, 2_000_000_000, "insufficient local test space")
            with wave.open(str(fixture), "rb") as source:
                self.assertEqual((source.getnchannels(), source.getsampwidth(), source.getframerate()), (1, 2, 16000))
                self.assertEqual(source.getnframes(), 56 * 16000)
                marker = source.readframes(source.getnframes())
            capacity = inline_frame_capacity(("grpc",), root, language="ru-RU", diarize=False,
                                             literature_text=True, channels=1)
            windows = frame_windows(total_frames, capacity, 16000)
            placements = [0] + [part.start - 28 * 16000 for part in windows[1:]] + [total_frames - len(marker) // 2]
            self.assertEqual(len(placements), len(set(placements)))
            original = root / "uploaded-beyond-four-hours.wav"
            write_sparse_recording(original, marker, total_frames, placements)
            original_hash = hash_file(original)
            annotation = {"fixture": "reviewed synthetic speech, repeated across real request joins",
                "source_sha256": original_hash, "marker_sha256": hashlib.sha256(marker).hexdigest(),
                "source_frames": total_frames, "sample_rate": 16000,
                "placements_frames": placements, "expected_parts": len(windows)}
            (root / "fixture.json").write_text(json.dumps(annotation, indent=2))
            print("Maximum uploaded-recording run:", root, flush=True)
            client = SpeechTranscriber(api_key=key, job_dir=root / "jobs", _context=RuntimeContext(user_home=root))
            job = client.start_transcription(original, split_inline=True, api_version="3", transport="grpc",
                language_code="ru-RU", timestamps=True, diarize=False, literature_text=True, deadline_seconds=1800)
            result = client.poll_until_complete(job)
        validate_result(result, total_frames, placements, windows, job)
        requests = []
        for part in job.data["parts"]:
            prepared = PreparedAudio(Path(part["audio_path"]), "WAV", 16000, 1, part["duration_ms"], {"converted": True})
            request = grpc_stt.build_file_request(prepared, language="ru-RU", diarize=False, literature_text=True)
            requests.append({"part_id": part["part_id"], "duration_ms": part["duration_ms"],
                             "request_bytes": request.ByteSize(), "operation_present": bool(part["operation_id"])})
            self.assertLessEqual(request.ByteSize(), 60_000_000)
            self.assertLessEqual(part["duration_ms"], 14_400_000)
            self.assertTrue(part["operation_id"])
            del request
        output = root / ("cached-" + uuid.uuid4().hex + ".json")
        script = Path(__file__).resolve().parents[2] / "scripts/transcribe.py"
        cached = subprocess.run([sys.executable, str(script), "--resume", str(job.manifest_path),
            "--format", "json", "--output", str(output)], cwd=root,
            env={"HOME": str(root), "PATH": os.environ["PATH"], "VIRTUAL_ENV": sys.prefix},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(cached.returncode, 0, "credential-free cached recovery failed; private evidence retained")
        self.assertEqual(hash_file(original), original_hash)
        recovered = json.loads(output.read_text())
        self.assertEqual(recovered["text"], result["text"])
        report = {"case": self.id(), "source": "synthetic uploaded recording; no operational audio",
            "source_seconds": total_frames / 16000, "source_sha256": original_hash,
            "production_limits_unchanged": True, "parts": requests,
            "every_part_complete": True, "original_frame_coverage": True,
            "speech_after_four_hours": True, "legitimate_repetition_verified": True,
            "marker_count": len(placements), "word_timing_tolerance_seconds": 0.5,
            "boundary_uncertainties": len(result.get("uncertainties", [])),
            "word_order_verified": "within each provider hypothesis; markers bound to original-time intervals",
            "final_tail_verified": True, "cached_without_credentials": True,
            "maximum_duration_acceptance": True, "submission_reused": bool(resume),
            "elapsed_seconds": time.monotonic() - started,
            "transcript_sha256": hashlib.sha256(result["text"].encode()).hexdigest()}
        receipt = root / ("acceptance-" + uuid.uuid4().hex + ".json")
        with receipt.open("x") as output:
            json.dump(report, output, indent=2)
        receipt.chmod(0o600)
        print("Maximum uploaded-recording receipt:", receipt)
