"""Saved credentials are applied separately without cloud access or secret output."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from yandex_speech.errors import SpeechError
from yandex_speech.key_env import ENV_SECTION, apply_key


class ApplyKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="speech-key-env-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.source = self.home / "key.json"
        self.target = self.home / ".env"
        self.api = {"apiKey": {"id": "api-id", "serviceAccountId": "account"},
                    "secret": "synthetic-api-secret"}
        self.source.write_text(json.dumps(self.api))

    def test_api_key_is_moved_into_section_preserving_unrelated_data_and_metadata(self):
        original = (
            "# Search configuration\nYANDEX_CLOUD_FOLDER_ID=search-folder\n"
            "export YANDEX_SPEECH_API_KEY=old-secret\n"
            "UNRELATED='keep # this'\n# Keep this comment\n"
            "YANDEX_SPEECH_API_KEY=stale-duplicate\n"
        )
        self.target.write_text(original)
        self.target.chmod(0o664)
        before = self.target.stat()
        saved = self.source.read_bytes()
        report = apply_key(self.source, self.target)
        text = self.target.read_text()
        self.assertIn(ENV_SECTION + '\nYANDEX_SPEECH_API_KEY="synthetic-api-secret"\n', text)
        self.assertEqual(text.count("YANDEX_SPEECH_API_KEY="), 1)
        self.assertIn("# Search configuration\nYANDEX_CLOUD_FOLDER_ID=search-folder\n", text)
        self.assertIn("UNRELATED='keep # this'\n# Keep this comment\n", text)
        self.assertEqual(self.source.read_bytes(), saved)
        after = self.target.stat()
        self.assertEqual((after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                         (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)))
        self.assertEqual(report["updated_variables"], ["YANDEX_SPEECH_API_KEY"])
        self.assertFalse(report["gateway_environment_updated"])
        self.assertNotIn("synthetic-api-secret", json.dumps(report))

    def test_static_key_coexists_with_api_key_and_leaves_folder_id_untouched(self):
        apply_key(self.source, self.target)
        self.source.write_text(json.dumps({
            "accessKey": {"keyId": "synthetic-access-id", "folderId": "different-folder"},
            "secret": "synthetic-storage-secret",
        }))
        with self.target.open("a") as stream:
            stream.write("# Shared with search\nYANDEX_CLOUD_FOLDER_ID=search-folder\n")
        report = apply_key(self.source, self.target)
        text = self.target.read_text()
        self.assertIn('YANDEX_SPEECH_API_KEY="synthetic-api-secret"', text)
        self.assertIn('YANDEX_SPEECH_AWS_ACCESS_KEY_ID="synthetic-access-id"', text)
        self.assertIn('YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY="synthetic-storage-secret"', text)
        self.assertIn("# Shared with search\nYANDEX_CLOUD_FOLDER_ID=search-folder\n", text)
        self.assertNotIn("different-folder", text)
        self.assertNotIn("YANDEX_CLOUD_FOLDER_ID", report["updated_variables"])
        self.assertNotIn("synthetic-storage-secret", json.dumps(report))

    def test_repeat_application_is_idempotent_and_preserves_crlf(self):
        self.target.write_bytes(b"# Other\r\nOTHER=value\r\n")
        apply_key(self.source, self.target)
        once = self.target.read_bytes()
        apply_key(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), once)
        self.assertEqual(once.count(ENV_SECTION.encode()), 1)
        self.assertNotIn(b"\n", once.replace(b"\r\n", b""))

    def test_existing_section_and_final_line_without_newline(self):
        self.target.write_text(ENV_SECTION + "\nOTHER=value")
        apply_key(self.source, self.target)
        text = self.target.read_text()
        self.assertEqual(text.count(ENV_SECTION), 1)
        self.assertIn('\nYANDEX_SPEECH_API_KEY="synthetic-api-secret"\nOTHER=value', text)

    def test_invalid_response_does_not_change_env_or_expose_input(self):
        self.target.write_text("KEEP=unchanged\n")
        for response in ({"apiKey": {}, "secret": "secret\nINJECT=value"},
                         {"accessKey": {}, "secret": "sensitive-value"},
                         {"apiKey": {}, "accessKey": {}, "secret": "sensitive-value"},
                         [], {"apiKey": {}, "secret": ""}):
            with self.subTest(response=response):
                self.source.write_text(json.dumps(response))
                with self.assertRaises(SpeechError) as caught:
                    apply_key(self.source, self.target)
                self.assertNotIn("sensitive-value", str(caught.exception))
                self.assertEqual(self.target.read_text(), "KEEP=unchanged\n")
        self.source.write_text("sensitive-invalid-json")
        with self.assertRaises(SpeechError) as caught:
            apply_key(self.source, self.target)
        self.assertNotIn("sensitive-invalid-json", str(caught.exception))
        self.assertEqual(self.target.read_text(), "KEEP=unchanged\n")

    def test_source_cannot_be_overwritten_through_an_alias(self):
        saved = self.source.read_bytes()
        self.target.symlink_to(self.source)
        with self.assertRaises(SpeechError):
            apply_key(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), saved)

    def test_apply_cli_needs_no_management_credentials_and_does_not_use_network(self):
        from yandex_speech.readiness import main
        output = io.StringIO()
        with patch("yandex_speech.cloud_setup.ManagementClient.from_args") as auth:
            with patch("requests.Session") as session, contextlib.redirect_stdout(output):
                status = main(["apply-key", "--secret-file", str(self.source),
                               "--env-file", str(self.target)])
        self.assertEqual(status, 0)
        auth.assert_not_called()
        session.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["env_file"], str(self.target))
        self.assertNotIn("synthetic-api-secret", output.getvalue())
        self.assertIn("startup", report["next_action"])

    def test_apply_cli_reports_invalid_json_without_writing_env(self):
        from yandex_speech.readiness import main
        self.source.write_text("sensitive-invalid-json")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["apply-key", "--secret-file", str(self.source),
                           "--env-file", str(self.target)])
        self.assertEqual(status, 2)
        self.assertFalse(self.target.exists())
        self.assertNotIn("sensitive-invalid-json", output.getvalue())

    def test_env_values_with_quotes_and_backslashes_are_serialized(self):
        value = 'synthetic "quoted" \\ value # literal'
        self.api["secret"] = value
        self.source.write_text(json.dumps(self.api))
        apply_key(self.source, self.target)
        assignment = self.target.read_text().splitlines()[1]
        self.assertEqual(json.loads(assignment.split("=", 1)[1]), value)

    def test_storage_settings_from_setup_are_added_without_overriding_env_choices(self):
        self.target.write_text("YANDEX_SPEECH_S3_BUCKET=chosen-bucket\n"
                               "export YANDEX_SPEECH_S3_PREFIX='chosen/prefix'\n"
                               "YANDEX_CLOUD_FOLDER_ID=shared-folder\n")
        apply_key(self.source, self.target, s3_bucket="default-bucket", s3_prefix="speechkit")
        text = self.target.read_text()
        self.assertIn("YANDEX_SPEECH_S3_BUCKET=chosen-bucket\n", text)
        self.assertIn("export YANDEX_SPEECH_S3_PREFIX='chosen/prefix'\n", text)
        self.assertNotIn("default-bucket", text)
        self.assertIn("YANDEX_CLOUD_FOLDER_ID=shared-folder", text)
        self.target.unlink()
        report = apply_key(self.source, self.target, s3_bucket="default-bucket", s3_prefix="speechkit")
        text = self.target.read_text()
        self.assertIn('YANDEX_SPEECH_S3_BUCKET="default-bucket"', text)
        self.assertIn('YANDEX_SPEECH_S3_PREFIX="speechkit"', text)
        self.assertNotIn("YANDEX_SPEECH_S3_ENDPOINT", text)
        self.assertIn("YANDEX_SPEECH_S3_BUCKET", report["updated_variables"])
