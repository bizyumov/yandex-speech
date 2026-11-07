import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import grpc
from tests.helpers import make_wave
from tests.test_results import final, refinement
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.grpc_stt import build_file_request, MAX_GRPC_BYTES
from yandex_speech.media import AudioInputSpec, prepare_audio
from yandex_speech.transcription import SpeechTranscriber


class FakeRpc:
    def __init__(self):
        self.submissions = []
        self.polls = []
        self.events = [final(0, "контрольный хвост"),
                       refinement(0, "Контрольный хвост.")]
        self.failure = None

    def recognize_file(self, request, timeout):
        self.submissions.append(request)
        if self.failure:
            raise self.failure
        return {"id": "operation-test", "done": False}

    def get_operation(self, operation_id, timeout):
        self.polls.append(operation_id)
        return {"id": operation_id, "done": True}

    def get_recognition(self, operation_id, timeout):
        return iter(self.events)

    def close(self):
        pass


class UncertainRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.DEADLINE_EXCEEDED

    def details(self):
        return "synthetic-credential-and-transcript-must-not-leak"


class GrpcFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = make_wave(self.root / "sample.wav")
        self.rpc = FakeRpc()
        self.client = SpeechTranscriber(api_key="synthetic-key",
            job_dir=self.root / "jobs", _context=RuntimeContext(user_home=self.root),
            _transport=self.rpc)

    def test_native_mp3_survives_nonzero_start_and_stereo_without_pcm_expansion(self):
        for channels in (1, 2):
            with self.subTest(channels=channels):
                path = self.root / f"native-{channels}.mp3"
                subprocess.run([
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                    "-i", f"anullsrc=r=16000:cl={'mono' if channels == 1 else 'stereo'}",
                    "-t", "1900", "-c:a", "libmp3lame", "-b:a", "8k", str(path),
                ], check=True, timeout=60)
                original = path.read_bytes()
                # Even the former mono/16 kHz conversion would exceed the real budget.
                self.assertGreater(1900 * 16000 * 2, MAX_GRPC_BYTES)
                self.assertLess(len(original) + 4096, MAX_GRPC_BYTES)
                with patch("yandex_speech.media._tool", side_effect=lambda name:
                           self.fail("native MP3 must not invoke ffmpeg")
                           if name == "ffmpeg" else name):
                    job = self.client.start_transcription(path, diarize=False)
                request = self.rpc.submissions[-1]
                self.assertEqual(request.content, original)
                self.assertEqual(
                    request.recognition_model.audio_format.container_audio.container_audio_type,
                    request.recognition_model.audio_format.container_audio.MP3,
                )
                self.assertLessEqual(request.ByteSize(), MAX_GRPC_BYTES)
                info = job.data["source_info"]
                self.assertFalse(info["converted"])
                self.assertEqual(info["original_path"], str(path))
                self.assertEqual(info["source_sha256"], hashlib.sha256(original).hexdigest())
                self.assertEqual(info["prepared"]["channels"], channels)
                self.assertGreater(info["original"]["start_time_seconds"], 0)
                self.assertEqual(info["timeline_offset_seconds"],
                                 info["original"]["start_time_seconds"])
                self.assertEqual(path.read_bytes(), original)

    def test_stereo_wav_and_raw_preserve_channel_metadata_in_recorded_requests(self):
        wav = make_wave(self.root / "stereo.wav", channels=2)
        raw = self.root / "stereo.pcm"
        raw.write_bytes(b"\\0" * 6400)
        for path, spec, container in (
            (wav, None, "WAV"),
            (raw, AudioInputSpec(source_format="lpcm", sample_rate_hertz=16000,
                                channels=2), "LPCM"),
        ):
            with self.subTest(container=container):
                original = path.read_bytes()
                with prepare_audio(path, audio_input=spec) as audio:
                    self.assertEqual(audio.path, path)
                    self.assertEqual(audio.channels, 2)
                    self.assertEqual(audio.source_info["prepared"]["channels"], 2)
                    self.assertFalse(audio.source_info["converted"])
                    request = build_file_request(audio, diarize=False)
                    with self.assertRaises(SpeechError) as mono_required:
                        build_file_request(audio, diarize=True)
                    self.assertEqual(mono_required.exception.category, "CONFIG")
                    self.assertEqual(request.content, original)
                    if container == "LPCM":
                        self.assertEqual(
                            request.recognition_model.audio_format.raw_audio.audio_channel_count, 2)
                info = {"duration_ms": 100, "size_bytes": len(original),
                        "prepared": {"container": container,
                                     "sample_rate_hertz": 16000, "channels": 2}}
                stored = build_file_request(
                    uri="https://storage.yandexcloud.net/synthetic/stereo", source_info=info)
                with self.assertRaises(SpeechError) as mono_required:
                    build_file_request(uri="https://storage.yandexcloud.net/synthetic/stereo",
                                       source_info=info, diarize=True)
                self.assertEqual(mono_required.exception.category, "CONFIG")
                if container == "LPCM":
                    self.assertEqual(
                        stored.recognition_model.audio_format.raw_audio.audio_channel_count, 2)

    def test_actual_protobuf_fields_and_effective_policy(self):
        with prepare_audio(self.path) as audio:
            request = build_file_request(audio, language="ru-RU", diarize=True,
                                         literature_text=True)
        model = request.recognition_model
        self.assertEqual(model.model, "general:rc")
        self.assertTrue(model.text_normalization.literature_text)
        self.assertFalse(model.text_normalization.profanity_filter)
        self.assertEqual(model.text_normalization.text_normalization, 1)
        self.assertEqual(model.text_normalization.phone_formatting_mode, 1)
        self.assertEqual(request.speaker_labeling.speaker_labeling, 1)
        self.assertEqual(request.WhichOneof("AudioSource"), "content")
        self.assertLessEqual(request.ByteSize(), MAX_GRPC_BYTES)

    def test_serialized_envelope_not_only_audio_size_is_admitted(self):
        with prepare_audio(self.path) as audio:
            with patch("yandex_speech.grpc_stt.MAX_GRPC_BYTES", audio.size_bytes):
                with self.assertRaises(SpeechError) as cm:
                    build_file_request(audio, language="ru-RU")
        self.assertEqual(cm.exception.category, "LIMIT")

    def test_exact_storage_duration_and_size_boundaries(self):
        uri="https://storage.yandexcloud.net/synthetic/audio.wav"
        info={"duration_ms":14_400_000,"size_bytes":1_000_000_000,
              "prepared":{"container":"WAV","sample_rate_hertz":16000,"channels":1}}
        request=build_file_request(uri=uri,source_info=info)
        self.assertEqual(request.uri,uri)
        for key,value in [("duration_ms",14_400_001),("size_bytes",1_000_000_001),
                          ("duration_ms",float("nan")),("size_bytes",-1)]:
            with self.subTest(key=key,value=value):
                broken=dict(info)
                broken[key]=value
                with self.assertRaises(SpeechError) as cm:
                    build_file_request(uri=uri,source_info=broken)
                self.assertEqual(cm.exception.category,"LIMIT")

    def test_external_metadata_and_uri_are_validated_before_submission(self):
        uri="https://storage.yandexcloud.net/synthetic/audio.wav"
        valid={"duration_ms":1000,"size_bytes":32000,
               "prepared":{"container":"LPCM","sample_rate_hertz":16000,"channels":1}}
        for prepared in [None,{"container":"LPCM","sample_rate_hertz":44100,"channels":1},
                         {"container":"MP3","sample_rate_hertz":0,"channels":1}]:
            info=dict(valid)
            info["prepared"]=prepared
            with self.assertRaises(SpeechError) as cm:
                build_file_request(uri=uri,source_info=info)
            self.assertEqual(cm.exception.category,"CONFIG")
        with self.assertRaises(SpeechError):
            build_file_request(uri="https://storage.yandexcloud.net:invalid/audio.wav",source_info=valid)

    def test_exact_serialized_inline_boundary_includes_the_envelope(self):
        from yandex_speech.media import PreparedAudio
        path=self.root/"boundary.pcm"
        path.write_bytes(b"\0"*1024)
        audio=PreparedAudio(path,"LPCM",16000,1,32,{"converted":True})
        size=build_file_request(audio).ByteSize()
        with patch("yandex_speech.grpc_stt.MAX_GRPC_BYTES",size):
            self.assertEqual(build_file_request(audio).ByteSize(),size)
        with patch("yandex_speech.grpc_stt.MAX_GRPC_BYTES",size-1):
            with self.assertRaises(SpeechError) as cm:
                build_file_request(audio)
        self.assertEqual(cm.exception.category,"LIMIT")

    def test_submit_save_resume_getrecognition_no_resubmission(self):
        job = self.client.start_transcription(self.path, timestamps=True)
        self.assertEqual(job.operation_id, "operation-test")
        receipt = json.loads(job.manifest_path.read_text())
        self.assertEqual(receipt["api_version"], "3")
        self.assertNotIn("synthetic-key", job.manifest_path.read_text())
        recovered = SpeechTranscriber(api_key="synthetic-key", _transport=self.rpc,
            _context=RuntimeContext(user_home=self.root), job_dir=self.root / "jobs")
        result = recovered.poll_until_complete(job.manifest_path)
        self.assertEqual(result["text"], "Контрольный хвост.")
        self.assertTrue(result["input_complete"])
        self.assertEqual(len(self.rpc.submissions), 1)
        self.assertEqual(result["completion_kind"], "recorded-file")

    def test_uncertain_submission_is_saved_and_not_retried(self):
        self.rpc.failure = UncertainRpcError()
        with self.assertRaises(SpeechError) as cm:
            self.client.start_transcription(self.path)
        self.assertEqual(cm.exception.category, "SUBMISSION_UNCERTAIN")
        self.assertEqual(len(self.rpc.submissions), 1)
        receipts = list((self.root / "jobs").glob("*/job.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())["status"], "submission_uncertain")
        self.assertNotIn("synthetic-credential", str(cm.exception))

    def test_fixed_version_does_not_fall_back_or_reinterpret_resume(self):
        for version in ["1", "2"]:
            with self.subTest(version=version):
                with self.assertRaises(SpeechError) as cm:
                    self.client.start_transcription(self.path, api_version=version, transport="grpc")
                self.assertEqual(cm.exception.category,"CONFIG")
        self.assertEqual(self.rpc.submissions, [])
        job = self.client.start_transcription(self.path)
        with self.assertRaises(SpeechError):
            self.client.poll_until_complete(job, api_version="2")

    def test_resume_auto_uses_saved_version(self):
        job=self.client.start_transcription(self.path)
        result=self.client.poll_until_complete(job,api_version="auto")
        self.assertEqual(result["api_version"],"3")
        self.assertEqual(len(self.rpc.submissions),1)

    def test_recovery_respects_rest_policy_and_explicit_grpc_override(self):
        job = self.client.start_transcription(self.path)
        path = self.root / ".config/yandex-speech/config.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"speech":{"transport":"rest"}}')
        recovered = SpeechTranscriber(api_key="synthetic", _transport=self.rpc,
            _context=RuntimeContext(user_home=self.root))
        result = recovered.poll_until_complete(job.manifest_path)
        self.assertEqual(result["transport"], "rest")
        self.assertEqual(len(self.rpc.submissions), 1)
        second = self.client.start_transcription(self.path, transport="grpc")
        result = recovered.poll_until_complete(second.manifest_path, transport="grpc")
        self.assertEqual(result["transport"], "grpc")
        self.assertTrue(result["input_complete"])
        self.assertEqual(len(self.rpc.submissions), 2)

    def test_missing_provider_result_is_explicit_and_never_resubmitted(self):
        class MissingResult(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.NOT_FOUND
        job=self.client.start_transcription(self.path)
        with patch.object(self.rpc,"get_recognition",side_effect=MissingResult()):
            with self.assertRaises(SpeechError) as cm:
                self.client.poll_until_complete(job)
        self.assertEqual(cm.exception.category,"PROVIDER")
        self.assertEqual(cm.exception.code,"RESULT_EXPIRED")
        self.assertEqual(len(self.rpc.submissions),1)

    def test_failed_cloud_operation_records_the_failed_part(self):
        job=self.client.start_transcription(self.path)
        with patch.object(self.rpc,"get_operation",return_value={"id":job.operation_id,
                "done":True,"error":{"code":13}}):
            with self.assertRaises(SpeechError):
                self.client.poll_until_complete(job)
        self.assertEqual(job.status,"failed")
        self.assertEqual(job.parts[0]["status"],"failed")
        self.assertEqual(len(self.rpc.submissions),1)
