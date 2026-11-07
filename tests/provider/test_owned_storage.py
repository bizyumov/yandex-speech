"""Explicit synthetic storage acceptance through an existing management identity.

The production uploader's static-key auth is separate. This internal supplied-client
fixture uses a short-lived IAM bearer from the selected existing yc profile, never
exports it to argv/files, and changes no bucket, role, key or public-access setting.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.provider.fixtures import fixture_dir
from yandex_speech import ObjectStorageUploader, SpeechError, SpeechTranscriber, UploadReceipt
from yandex_speech.config import RuntimeContext


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1",
                     "explicit provider opt-in required")
class LiveOwnedStorageTests(unittest.TestCase):
    def test_private_object_conditional_collision_recovery_and_recognizer_access(self):
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        from botocore.exceptions import ClientError

        bucket = os.environ.get("YANDEX_SPEECH_TEST_STORAGE_BUCKET")
        profile = os.environ.get("YANDEX_SPEECH_TEST_MANAGEMENT_PROFILE")
        key = os.environ.get("YANDEX_SPEECH_API_KEY")
        version = os.environ.get("YANDEX_SPEECH_TEST_STORAGE_API_VERSION", "3")
        transport = os.environ.get("YANDEX_SPEECH_TEST_STORAGE_TRANSPORT", "auto")
        self.assertIn(version, {"2", "3"})
        self.assertIn(transport, {"auto", "grpc", "rest"})
        self.assertTrue(bucket and profile and key,
                        "select existing authorized storage bucket/management profile and speech key")
        environment = {name: os.environ[name] for name in ("HOME", "PATH", "LANG", "XDG_CONFIG_HOME")
                       if name in os.environ}
        environment["YC_CLI_INITIALIZATION_SILENCE"] = "true"
        auth = subprocess.run(["yc", "--profile", profile, "--retry", "0", "iam", "create-token"],
                              capture_output=True, text=True, env=environment, timeout=60)
        self.assertEqual(auth.returncode, 0, "existing management authentication is unavailable")
        token = auth.stdout.strip()
        self.assertTrue(token, "management CLI returned no ephemeral token")
        client = boto3.client("s3", endpoint_url="https://storage.yandexcloud.net", region_name="ru-central1",
            aws_access_key_id="unused-under-unsigned-auth", aws_secret_access_key="unused-under-unsigned-auth",
            config=Config(signature_version=UNSIGNED, retries={"total_max_attempts": 1},
                          connect_timeout=10, read_timeout=60, s3={"addressing_style": "path"},
                          request_checksum_calculation="when_required"))
        self.addCleanup(client.close)

        def authorize(request, **kwargs):
            request.headers["Authorization"] = "Bearer " + token

        client.meta.events.register("before-send.s3", authorize)
        destination = Path(os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR",
                           str(Path.home() / ".local/state/yandex-speech/storage-acceptance")))
        root = destination / ("owned-storage-" + uuid.uuid4().hex)
        root.mkdir(parents=True, mode=0o700)

        class LoseFirstReply:
            def put_object(self, **kwargs):
                client.put_object(**kwargs)
                raise TimeoutError("injected reply loss; no authentication data")

            def __getattr__(self, name):
                return getattr(client, name)

        uploader = ObjectStorageUploader(bucket=bucket, prefix="speechkit/acceptance",
            job_dir=root / "uploads", _context=RuntimeContext(user_home=root), _client=LoseFirstReply())
        source = fixture_dir() / "single.wav"
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        with self.assertRaises(SpeechError) as uncertain:
            uploader.upload(source, api_version=version, deadline_seconds=120)
        self.assertEqual(uncertain.exception.code, "UPLOAD_UNCERTAIN")
        receipt_path = Path(uncertain.exception.as_dict()["receipt_path"])
        self.assertEqual(UploadReceipt.load(receipt_path).status, "upload_uncertain")
        receipt = uploader.recover_upload(receipt_path)
        self.assertEqual(receipt.status, "uploaded")
        before = client.head_object(Bucket=receipt.bucket, Key=receipt.key)
        try:
            client.put_object(Bucket=receipt.bucket, Key=receipt.key, Body=b"must-not-replace",
                              IfNoneMatch="*", Metadata={"speech-owner": "foreign-attempt"})
        except ClientError as collision:
            self.assertIn(str(collision.response.get("Error", {}).get("Code")),
                          {"PreconditionFailed", "412", "ConditionalRequestConflict"})
        else:
            self.fail("conditional collision unexpectedly replaced an existing object")
        after = client.head_object(Bucket=receipt.bucket, Key=receipt.key)
        self.assertEqual(after["ETag"], before["ETag"])
        self.assertEqual(after["ContentLength"], before["ContentLength"])
        # Fixture creation may share only this newly owned synthetic object with
        # the explicitly selected recognition account. No bucket/IAM role changes.
        reader = os.environ.get("YANDEX_SPEECH_TEST_RECOGNIZER_ACCOUNT")
        if reader:
            self.assertEqual(after.get("Metadata", {}).get("speech-owner"), receipt.upload_id)
            acl = client.get_object_acl(Bucket=receipt.bucket, Key=receipt.key)
            grants = list(acl.get("Grants", []))
            grant = {"Grantee": {"Type": "CanonicalUser", "ID": reader}, "Permission": "READ"}
            if not any(g.get("Grantee", {}).get("ID") == reader and g.get("Permission") == "READ"
                       for g in grants):
                grants.append(grant)
                client.put_object_acl(Bucket=receipt.bucket, Key=receipt.key,
                    AccessControlPolicy={"Owner": acl["Owner"], "Grants": grants})
            shared = client.get_object_acl(Bucket=receipt.bucket, Key=receipt.key)
            self.assertTrue(any(g.get("Grantee", {}).get("ID") == reader and g.get("Permission") == "READ"
                                for g in shared.get("Grants", [])))
        recognizer = SpeechTranscriber(api_key=key, job_dir=root / "jobs",
                                       _context=RuntimeContext(user_home=root))
        job = recognizer.start_transcription(receipt, api_version=version, transport=transport,
                                             timestamps=True, deadline_seconds=120)
        result = recognizer.poll_until_complete(job, deadline_seconds=120)
        self.assertTrue(result["input_complete"])
        self.assertIn("контрольный хвост", result["text"].casefold())
        self.assertEqual(job.owned_uploads, [receipt.reference()])
        if receipt.data.get("version_id") not in (None, "", "null"):
            uploader.cleanup(receipt, job)
            cleanup = "exact-owned-version-deleted"
        else:
            with self.assertRaises(SpeechError) as retained:
                uploader.cleanup(receipt, job)
            self.assertEqual(retained.exception.category, "PREREQUISITE")
            preserved = client.head_object(Bucket=receipt.bucket, Key=receipt.key)
            self.assertEqual(preserved["ContentLength"], receipt.size_bytes)
            cleanup = "retained-unversioned"
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)
        report = {"case": self.id(), "source": "reviewed synthetic voice fixture",
                  "conditional_collision_preserved": True, "lost_reply_recovered_without_put_retry": True,
                  "recognizer_private_read": "execution-verified", "upload_bytes": receipt.size_bytes,
                  "api_version": result["api_version"], "transport": result["transport"],
                  "job_manifest": str(job.manifest_path), "upload_manifest": str(receipt.manifest_path),
                  "cleanup": cleanup, "source_unchanged": True,
                  "new_test_object_read_acl": bool(reader), "bucket_or_iam_changes": False,
                  "transcript_sha256": hashlib.sha256(result["text"].encode()).hexdigest()}
        output = root / "acceptance.json"
        with output.open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        output.chmod(0o600)
