"""Storage defaults, exact env targets and scoped S3 setup without folder enumeration."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yandex_speech.errors import SpeechError
from yandex_speech.storage_setup import setup_storage


class S3Error(Exception):
    def __init__(self, code):
        super().__init__("sensitive-provider-error")
        self.response = {"Error": {"Code": code}}


class FakeSetupS3:
    def __init__(self):
        self.buckets = set()
        self.objects = {}
        self.calls = []
        self.denied = False
        self.corrupt = False

    def head_bucket(self, **args):
        self.calls.append(("head", args))
        if self.denied:
            raise S3Error("AccessDenied")
        if args["Bucket"] not in self.buckets:
            raise S3Error("NoSuchBucket")
        return {}

    def create_bucket(self, **args):
        self.calls.append(("create", args))
        self.buckets.add(args["Bucket"])
        return {}

    def list_objects_v2(self, **args):
        self.calls.append(("list", args))
        keys = [key for bucket, key in self.objects
                if bucket == args["Bucket"] and key.startswith(args["Prefix"])]
        return {"Contents": [{"Key": key} for key in keys[:args["MaxKeys"]]]}

    def put_object(self, **args):
        self.calls.append(("put", {key: value for key, value in args.items() if key != "Body"}))
        key = (args["Bucket"], args["Key"])
        if key in self.objects:
            raise S3Error("PreconditionFailed")
        assert args["IfNoneMatch"] == "*"
        self.objects[key] = args["Body"]
        return {"VersionId": "fixture-version"}

    def get_object(self, **args):
        self.calls.append(("get", args))
        value = b"wrong" if self.corrupt else self.objects[(args["Bucket"], args["Key"])]
        return {"Body": io.BytesIO(value)}

    def delete_object(self, **args):
        self.calls.append(("delete", args))
        del self.objects[(args["Bucket"], args["Key"])]
        return {}


class StorageSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="speech-storage-setup-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = self.home / ".env"
        self.secret = self.home / "static.json"
        self.secret.write_text(json.dumps({
            "accessKey": {"serviceAccountId": "sa-test", "keyId": "synthetic-id"},
            "secret": "synthetic-secret"}))
        self.client = FakeSetupS3()

    def test_default_bucket_is_created_and_default_prefix_materialized(self):
        report = setup_storage(self.env, self.secret, _client=self.client)
        self.assertEqual(report["bucket"], "yandex-speech-sa-test")
        self.assertEqual(report["prefix"], "speechkit")
        self.assertEqual(report["endpoint"], "https://storage.yandexcloud.net")
        self.assertTrue(report["bucket_created"])
        self.assertEqual(self.client.objects, {("yandex-speech-sa-test", "speechkit/"): b""})
        self.assertFalse(self.env.exists())
        self.assertNotIn("synthetic-secret", json.dumps(report))
        deleted = next(args for name, args in self.client.calls if name == "delete")
        self.assertEqual(deleted["VersionId"], "fixture-version")

    def test_env_selects_exact_existing_bucket_and_prefix(self):
        text = ("YANDEX_CLOUD_FOLDER_ID=search-folder\n"
                "export YANDEX_SPEECH_S3_BUCKET='chosen-bucket'\n"
                "YANDEX_SPEECH_S3_PREFIX=chosen/prefix # configured\n"
                "YANDEX_SPEECH_S3_ENDPOINT=https://ignored.invalid\n")
        self.env.write_text(text)
        self.client.buckets.add("chosen-bucket")
        self.client.objects[("chosen-bucket", "chosen/prefix/existing")] = b"preserved"
        report = setup_storage(self.env, self.secret, _client=self.client)
        self.assertEqual((report["bucket"], report["prefix"]), ("chosen-bucket", "chosen/prefix"))
        self.assertEqual(report["endpoint"], "https://storage.yandexcloud.net")
        self.assertFalse(report["bucket_created"])
        self.assertTrue(all(args["Bucket"] == "chosen-bucket" for _, args in self.client.calls))
        selected = next(args for name, args in self.client.calls if name == "list")
        self.assertEqual(selected["Prefix"], "chosen/prefix/")
        self.assertFalse(any(name == "create" for name, _ in self.client.calls))
        self.assertEqual(self.env.read_text(), text)
        self.assertEqual(self.client.objects[("chosen-bucket", "chosen/prefix/existing")], b"preserved")

    def test_missing_configured_bucket_is_created_at_that_exact_name(self):
        self.env.write_text("YANDEX_SPEECH_S3_BUCKET=chosen-bucket\n")
        report = setup_storage(self.env, self.secret, _client=self.client)
        self.assertEqual(report["bucket"], "chosen-bucket")
        self.assertTrue(report["bucket_created"])
        self.assertEqual(self.client.buckets, {"chosen-bucket"})
        self.assertTrue(all(args["Bucket"] == "chosen-bucket" for _, args in self.client.calls))

    def test_denial_is_not_treated_as_absence_and_never_exposes_provider_error(self):
        self.client.denied = True
        with self.assertRaises(SpeechError) as caught:
            setup_storage(self.env, self.secret, _client=self.client)
        self.assertEqual(caught.exception.category, "PERMISSION")
        self.assertNotIn("sensitive-provider-error", str(caught.exception))
        self.assertFalse(any(name == "create" for name, _ in self.client.calls))

    def test_missing_configured_prefix_is_materialized_at_that_exact_prefix(self):
        self.env.write_text("YANDEX_SPEECH_S3_PREFIX=chosen/prefix\n")
        self.client.buckets.add("yandex-speech-sa-test")
        report = setup_storage(self.env, self.secret, _client=self.client)
        self.assertEqual(report["prefix"], "chosen/prefix")
        self.assertFalse(report["prefix_previously_present"])
        self.assertEqual(self.client.objects, {("yandex-speech-sa-test", "chosen/prefix/"): b""})

    def test_existing_env_credentials_are_usable_without_issuing_another_key(self):
        self.env.write_text("YANDEX_SPEECH_S3_BUCKET=chosen-bucket\n"
                            "YANDEX_SPEECH_AWS_ACCESS_KEY_ID=existing-id\n"
                            "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY=existing-secret\n")
        self.client.buckets.add("chosen-bucket")
        report = setup_storage(self.env, _client=self.client)
        self.assertEqual(report["bucket"], "chosen-bucket")
        self.assertIn(("chosen-bucket", "speechkit/"), self.client.objects)

    def test_probe_failure_cleans_only_its_own_version(self):
        self.client.buckets.add("yandex-speech-sa-test")
        self.client.objects[("yandex-speech-sa-test", "speechkit/existing")] = b"keep"
        self.client.corrupt = True
        with self.assertRaises(SpeechError):
            setup_storage(self.env, self.secret, _client=self.client)
        self.assertEqual(self.client.objects, {("yandex-speech-sa-test", "speechkit/existing"): b"keep"})

    def test_cli_storage_setup_uses_saved_pair_without_management_auth(self):
        from yandex_speech.readiness import main
        import contextlib as contexts
        output = io.StringIO()
        with patch("yandex_speech.cloud_setup.ManagementClient.from_args") as auth:
            with patch("yandex_speech.storage_setup.ObjectStorageUploader._connection",
                       return_value=contexts.nullcontext(self.client)), contextlib.redirect_stdout(output):
                status = main(["setup-storage", "--secret-file", str(self.secret),
                               "--env-file", str(self.env)])
        self.assertEqual(status, 0)
        auth.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")
        self.assertNotIn("synthetic-secret", output.getvalue())
