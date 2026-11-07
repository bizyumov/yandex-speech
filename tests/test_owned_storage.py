"""Owned Object Storage uploads: conditional creation, recovery and exact-version cleanup."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from yandex_speech import SpeechError
from yandex_speech.config import RuntimeContext
from yandex_speech.jobs import JobHandle


class StorageError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code, "Message": "sentinel-secret-avoid"}}


class FakeS3:
    def __init__(self, *, versioned=True):
        self.versioned = versioned
        self.versions, self.latest = {}, {}
        self.puts, self.deletes = [], []
        self.lose_reply = self.collide = self.deny_head = False

    def put_object(self, **request):
        self.puts.append({k: v for k, v in request.items() if k != "Body"})
        if request.get("IfNoneMatch") != "*":
            raise AssertionError("upload must be atomic no-clobber")
        key = (request["Bucket"], request["Key"])
        if self.collide:
            self.latest[key] = "foreign"
            self.versions[(*key, "foreign")] = {"body": b"unowned", "metadata": {}}
        if key in self.latest:
            raise StorageError("PreconditionFailed")
        body = request["Body"].read()
        version = f"version-{len(self.puts)}" if self.versioned else "null"
        etag = '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"'
        self.versions[(*key, version)] = {"body": body, "metadata": request["Metadata"], "etag": etag}
        self.latest[key] = version
        if self.lose_reply:
            raise TimeoutError("sentinel-secret-avoid")
        return {"ETag": etag, "VersionId": version, "ResponseMetadata": {"HTTPStatusCode": 200}}

    def head_object(self, *, Bucket, Key, VersionId=None):
        if self.deny_head:
            raise StorageError("AccessDenied")
        key = (Bucket, Key)
        version = VersionId or self.latest.get(key)
        value = self.versions.get((*key, version))
        if value is None:
            raise StorageError("NoSuchKey")
        return {"VersionId": version, "ETag": value.get("etag"), "Metadata": copy.deepcopy(value["metadata"]),
                "ContentLength": len(value["body"]), "ResponseMetadata": {"HTTPStatusCode": 200}}

    def delete_object(self, **request):
        self.deletes.append(request)
        if not request.get("VersionId") or request["VersionId"] == "null":
            raise AssertionError("cleanup must target an immutable owned version")
        key = (request["Bucket"], request["Key"], request["VersionId"])
        self.versions.pop(key, None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}


class OwnedStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.wav"
        with wave.open(str(self.source), "wb") as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b"\0" * 32000)
        self.backend = FakeS3()

    def uploader(self, **options):
        from yandex_speech import ObjectStorageUploader
        return ObjectStorageUploader(bucket="private-speech", aws_access_key_id="test-storage-id",
            aws_secret_access_key="test-storage-secret", job_dir=self.root / "uploads",
            _context=RuntimeContext(user_home=self.root), _client=self.backend, **options)

    def completed_job(self, receipt):
        job = JobHandle.create(self.root / "jobs", receipt.source_info,
            parameters={"language": "ru-RU", "diarize": False, "timestamps": False, "literature_text": True})
        job.data["owned_uploads"] = [receipt.reference()]
        job.save()
        job.store_result({"schema_version": 1, "status": "completed", "input_complete": True,
                          "text": "synthetic result", "segments": [], "raw": []})
        return job

    def test_conditional_unique_uploads_have_private_durable_ownership_without_credentials(self):
        uploader = self.uploader()
        before = self.source.read_bytes()
        first = uploader.upload(self.source)
        second = uploader.upload(self.source)
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(first.source_info["metadata_origin"], "uploader_receipt")
        self.assertEqual(first.source_sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(first.size_bytes, len(before))
        self.assertIn("/private-speech/", first.uri)
        raw = first.manifest_path.read_text()
        self.assertNotIn("test-storage-secret", raw)
        self.assertNotIn("test-storage-id", raw)
        self.assertEqual(first.manifest_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(first.manifest_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(first.status, "uploaded")
        self.assertEqual(self.backend.deletes, [])

    def test_collision_preserves_unowned_object_and_never_retries_put(self):
        self.backend.collide = True
        uploader = self.uploader()
        with self.assertRaises(SpeechError) as raised:
            uploader.upload(self.source)
        self.assertEqual(raised.exception.code, "UPLOAD_COLLISION")
        self.assertEqual(len(self.backend.puts), 1)
        self.assertEqual(next(iter(self.backend.versions.values()))["body"], b"unowned")
        self.assertEqual(self.backend.deletes, [])

    def test_lost_put_reply_recovers_only_matching_owned_metadata_without_reupload(self):
        from yandex_speech import UploadReceipt
        self.backend.lose_reply = True
        uploader = self.uploader()
        with self.assertRaises(SpeechError) as raised:
            uploader.upload(self.source)
        self.assertEqual(raised.exception.code, "UPLOAD_UNCERTAIN")
        receipt_path = next((self.root / "uploads").glob("*/upload.json"))
        pending = UploadReceipt.load(receipt_path)
        self.assertEqual(pending.status, "upload_uncertain")
        recovered = uploader.recover_upload(receipt_path)
        self.assertEqual(recovered.status, "uploaded")
        self.assertEqual(len(self.backend.puts), 1)
        self.assertNotIn("sentinel-secret-avoid", receipt_path.read_text())

    def test_forbidden_recovery_inspection_is_not_reported_as_missing_or_reuploaded(self):
        from yandex_speech import UploadReceipt
        self.backend.lose_reply = True
        uploader = self.uploader()
        with self.assertRaises(SpeechError):
            uploader.upload(self.source)
        receipt = next((self.root / "uploads").glob("*/upload.json"))
        self.backend.deny_head = True
        with self.assertRaises(SpeechError) as raised:
            uploader.recover_upload(receipt)
        self.assertEqual(raised.exception.category, "PERMISSION")
        self.assertEqual(UploadReceipt.load(receipt).status, "upload_uncertain")
        self.assertEqual(len(self.backend.puts), 1)
        self.assertEqual(self.backend.deletes, [])

    def test_cleanup_requires_durable_completed_job_and_exact_ownership(self):
        uploader = self.uploader()
        receipt = uploader.upload(self.source)
        job = JobHandle.create(self.root / "jobs", receipt.source_info,
            parameters={"language": "ru-RU", "diarize": False, "timestamps": False, "literature_text": True})
        job.data["owned_uploads"] = [receipt.reference()]
        job.save()
        with self.assertRaises(SpeechError):
            uploader.cleanup(receipt, job)
        self.assertEqual(self.backend.deletes, [])
        job.store_result({"schema_version": 1, "status": "completed", "input_complete": True,
                          "text": "synthetic result", "segments": [], "raw": []})
        key = (receipt.bucket, receipt.key)
        self.backend.versions[(*key, "unowned-newer")] = {"body": b"foreign-newer", "metadata": {}}
        self.backend.latest[key] = "unowned-newer"
        uploader.cleanup(receipt, job)
        self.assertEqual(self.backend.deletes, [{"Bucket": receipt.bucket, "Key": receipt.key,
                                               "VersionId": "version-1"}])
        self.assertIn((*key, "unowned-newer"), self.backend.versions)
        uploader.cleanup(receipt, job)
        self.assertEqual(len(self.backend.deletes), 1)

    def test_unversioned_cleanup_and_tampered_results_preserve_objects(self):
        self.backend.versioned = False
        uploader = self.uploader()
        receipt = uploader.upload(self.source)
        job = self.completed_job(receipt)
        with self.assertRaises(SpeechError) as raised:
            uploader.cleanup(receipt, job)
        self.assertEqual(raised.exception.category, "PREREQUISITE")
        self.assertEqual(self.backend.deletes, [])
        self.backend.versioned = True
        other = uploader.upload(self.source)
        job = self.completed_job(other)
        (job.manifest_path.parent / "result.json").write_text("{}")
        with self.assertRaises(SpeechError):
            uploader.cleanup(other, job)
        self.assertEqual(self.backend.deletes, [])

    def test_file_and_explicit_storage_precedence_and_missing_separate_credentials(self):
        from yandex_speech import ObjectStorageUploader
        config = self.root / ".config/yandex-speech/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"speech": {"object_storage": {"bucket": "file-bucket", "prefix": "file-prefix"}}}))
        uploader = self.uploader(_adapter_defaults={"object_storage": {"prefix": "adapter-prefix"}},
            _environment={"YANDEX_SPEECH_S3_BUCKET": "environment-bucket"})
        receipt = uploader.upload(self.source)
        self.assertEqual(receipt.bucket, "private-speech")
        self.assertTrue(receipt.key.startswith("file-prefix/"))
        missing = ObjectStorageUploader(bucket="private-speech", job_dir=self.root / "missing",
            _context=RuntimeContext(user_home=self.root), _environment={})
        with self.assertRaises(SpeechError) as raised:
            missing.upload(self.source)
        self.assertEqual(raised.exception.category, "AUTH")

    def test_receipt_passes_to_public_transcription_without_upload_and_retains_ownership(self):
        from yandex_speech import SpeechTranscriber
        receipt = self.uploader().upload(self.source)

        class Rpc:
            def recognize_file(self, request, timeout):
                return {"id": "synthetic-accepted-operation"}

            def close(self):
                pass

        client = SpeechTranscriber(api_key="test-only-speech-key", job_dir=self.root / "jobs",
            _context=RuntimeContext(user_home=self.root), _transport=Rpc())
        with patch("yandex_speech.transcription.build_file_request", return_value=object()):
            job = client.start_transcription(receipt)
        self.assertEqual(len(self.backend.puts), 1)
        self.assertEqual(job.owned_uploads, [receipt.reference()])
        self.assertEqual(job.source_info["metadata_origin"], "uploader_receipt")

    def test_more_than_two_source_channels_are_adapted_before_upload(self):
        source = self.root / "surround.wav"
        with wave.open(str(source), "wb") as stream:
            stream.setnchannels(6); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b"\0" * (16000 * 2 * 6))
        original = source.read_bytes()
        receipt = self.uploader().upload(source)
        self.assertEqual(receipt.source_info["original"]["channels"], 6)
        self.assertEqual(receipt.source_info["prepared"]["channels"], 1)
        self.assertTrue(receipt.source_info["converted"])
        self.assertEqual(source.read_bytes(), original)

    def test_foreign_remote_metadata_or_unowned_job_never_authorizes_cleanup(self):
        uploader = self.uploader()
        receipt = uploader.upload(self.source)
        job = self.completed_job(receipt)
        job.data["owned_uploads"] = []
        job.save()
        with self.assertRaises(SpeechError):
            uploader.cleanup(receipt, job)
        self.assertEqual(self.backend.deletes, [])
        job.data["owned_uploads"] = [receipt.reference()]
        job.save()
        remote = self.backend.versions[(receipt.bucket, receipt.key, receipt.data["version_id"])]
        remote["metadata"]["speech-owner"] = "foreign-owner"
        with self.assertRaises(SpeechError) as raised:
            uploader.cleanup(receipt, job)
        self.assertEqual(raised.exception.category, "STATE")
        self.assertEqual(self.backend.deletes, [])

    def test_credential_bearing_endpoint_and_redirected_receipt_are_refused(self):
        from yandex_speech import ObjectStorageUploader, UploadReceipt
        with self.assertRaises(SpeechError) as raised:
            ObjectStorageUploader(bucket="private-speech",
                endpoint="https://operator:sentinel-secret@storage.yandexcloud.net",
                _context=RuntimeContext(user_home=self.root))
        self.assertNotIn("sentinel-secret", str(raised.exception))
        receipt = self.uploader().upload(self.source)
        alias = receipt.manifest_path.parent / "alias.json"
        alias.symlink_to(receipt.manifest_path)
        with self.assertRaises(SpeechError):
            UploadReceipt.load(alias)

    def test_lost_delete_reply_adopts_absent_owned_version_without_a_second_delete(self):
        from yandex_speech import UploadReceipt
        uploader = self.uploader()
        receipt = uploader.upload(self.source)
        job = self.completed_job(receipt)
        original = self.backend.delete_object

        def lost_reply(**request):
            original(**request)
            raise TimeoutError("sentinel-secret-avoid")

        self.backend.delete_object = lost_reply
        with self.assertRaises(SpeechError) as raised:
            uploader.cleanup(receipt, job)
        self.assertEqual(raised.exception.category, "OUTPUT_PARTIAL")
        self.assertEqual(UploadReceipt.load(receipt.manifest_path).status, "cleanup_uncertain")
        self.backend.delete_object = original
        uploader.cleanup(receipt, job)
        self.assertEqual(len(self.backend.deletes), 1)
        self.assertEqual(UploadReceipt.load(receipt.manifest_path).status, "deleted")

    def test_process_interruption_during_delete_resumes_exact_owned_version(self):
        from yandex_speech import UploadReceipt
        original = self.backend.delete_object
        for request_accepted in (False, True):
            with self.subTest(request_accepted=request_accepted):
                uploader = self.uploader()
                receipt = uploader.upload(self.source)
                job = self.completed_job(receipt)
                previous_deletes = len(self.backend.deletes)

                def interrupted(**request):
                    if request_accepted:
                        original(**request)
                    raise KeyboardInterrupt

                self.backend.delete_object = interrupted
                with self.assertRaises(KeyboardInterrupt):
                    uploader.cleanup(receipt, job)
                self.assertEqual(UploadReceipt.load(receipt.manifest_path).status, "deleting")
                self.backend.delete_object = original
                uploader.cleanup(receipt, job)
                self.assertEqual(len(self.backend.deletes), previous_deletes + 1)
                self.assertEqual(UploadReceipt.load(receipt.manifest_path).status, "deleted")

    def test_existing_caller_state_parent_mode_is_preserved(self):
        from yandex_speech import ObjectStorageUploader
        parent = self.root / "caller-state"
        parent.mkdir(mode=0o755)
        before = parent.stat().st_mode & 0o7777
        uploader = ObjectStorageUploader(bucket="private-speech", job_dir=parent,
            _context=RuntimeContext(user_home=self.root), _client=self.backend)
        receipt = uploader.upload(self.source)
        self.assertEqual(parent.stat().st_mode & 0o7777, before)
        self.assertEqual(receipt.manifest_path.parent.stat().st_mode & 0o777, 0o700)

    def test_real_optional_sdk_does_not_retry_an_uncertain_put(self):
        import importlib.util
        if importlib.util.find_spec("boto3") is None:
            self.skipTest("optional storage SDK is absent")
        import boto3
        from botocore.exceptions import ReadTimeoutError
        from yandex_speech import ObjectStorageUploader

        class Wire:
            calls = 0

            def send(self, request):
                self.calls += 1
                raise ReadTimeoutError(endpoint_url="https://storage.yandexcloud.net", error="synthetic loss")

            def close(self):
                pass

        wire = Wire()
        original = boto3.client

        def actual_client(*args, **kwargs):
            client = original(*args, **kwargs)
            client._endpoint.http_session = wire
            return client

        uploader = ObjectStorageUploader(bucket="private-speech",
            aws_access_key_id="synthetic-sdk-id", aws_secret_access_key="synthetic-sdk-secret",
            job_dir=self.root / "sdk-state", _context=RuntimeContext(user_home=self.root), _environment={})
        with patch("boto3.client", side_effect=actual_client):
            with self.assertRaises(SpeechError) as raised:
                uploader.upload(self.source)
        self.assertEqual(raised.exception.code, "UPLOAD_UNCERTAIN")
        self.assertEqual(wire.calls, 1)
