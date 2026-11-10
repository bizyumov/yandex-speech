"""Account setup is explicit, scoped and never prints one-time credentials."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import requests

from yandex_speech.cloud_setup import ManagementClient, inspect_account, prepare_key, create_key
from yandex_speech.errors import SpeechError


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
    def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.calls = []
        self.deny_inventory = False
        self.deny_grants = False
        self.post_error = False
        self.keys = [{"id": "old", "serviceAccountId": "sa-test",
                      "scopes": ["yc.ai.speechkitStt.execute"],
                      "expiresAt": "2000-01-01T00:00:00Z",
                      "maskedSecret": "never-print-this", "description": "private-note"}]

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST":
            if self.post_error:
                raise requests.Timeout("never-print-this")
            body = kwargs["json"]
            meta = {"id": "new-id", "serviceAccountId": body["serviceAccountId"]}
            if url.endswith("accessKeys"):
                meta["keyId"] = "synthetic-access-id"
                return Response({"accessKey": meta, "secret": "synthetic-one-time-secret"})
            meta.update(scopes=body["scopes"])
            if "expiresAt" in body:
                meta["expiresAt"] = body["expiresAt"]
            return Response({"apiKey": meta, "secret": "synthetic-one-time-secret"})
        if "/serviceAccounts/" in url:
            return Response({"id": "sa-test", "folderId": "folder-test", "status": "ACTIVE"})
        if ":listAccessBindings" in url:
            return Response({"accessBindings": []}, 403 if self.deny_grants else 200)
        return Response({"accessKeys": []} if url.endswith("accessKeys") else {"apiKeys": self.keys},
                        403 if self.deny_inventory else 200)


class CloudSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="speech-key-setup-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.destination = self.home / "new-key.json"
        self.session = FakeSession()
        self.client = ManagementClient("synthetic-management-token", _session=self.session)

    def test_inspection_has_expiry_and_scopes_without_secret_or_permission_claims(self):
        self.session.deny_grants = True
        report = inspect_account(self.client, "sa-test")
        text = json.dumps(report)
        self.assertNotIn("never-print-this", text)
        self.assertNotIn("private-note", text)
        self.assertNotIn("synthetic-management-token", text)
        self.assertTrue(report["keys"]["items"][0]["expired"])
        self.assertEqual(report["permissions"]["status"], "not_verifiable")
        self.assertEqual(report["execution_probe"], "not_run")
        self.assertTrue(all(call[0] == "GET" for call in self.session.calls))

    def test_prepare_is_read_only_and_has_explicit_purpose_scopes(self):
        report = prepare_key(self.client, "sa-test", "both", self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(set(report["request"]["scopes"]),
            {"yc.ai.speechkitStt.execute", "yc.ai.speechkitTts.execute"})
        self.assertNotIn("expiresAt", report["request"])
        self.assertTrue(all(call[0] == "GET" for call in self.session.calls))

    def test_create_saves_secret_exclusively_and_returns_only_metadata(self):
        report = create_key(self.client, "sa-test", "stt", self.destination,
                            expires_at="2099-01-01T00:00:00Z")
        saved = json.loads(self.destination.read_text())
        self.assertEqual(saved["secret"], "synthetic-one-time-secret")
        reference = self.home / "ordinary-file"
        reference.touch()
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), stat.S_IMODE(reference.stat().st_mode))
        self.assertNotIn("synthetic-one-time-secret", json.dumps(report))
        self.assertEqual(report["key"]["expiresAt"], "2099-01-01T00:00:00Z")
        posts = [call for call in self.session.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["json"]["scopes"], ["yc.ai.speechkitStt.execute"])
        self.assertFalse(posts[0][2]["allow_redirects"])

    def test_existing_file_or_symlink_is_refused_before_any_management_call(self):
        for symlink in (False, True):
            target = self.home / ("link.json" if symlink else "existing.json")
            if symlink:
                target.symlink_to(self.home / "absent")
            else:
                target.write_text("operator-owned")
            with self.subTest(symlink=symlink), self.assertRaises(SpeechError):
                create_key(self.client, "sa-test", "both", target)
            self.assertEqual(self.session.calls, [])
            if not symlink:
                self.assertEqual(target.read_text(), "operator-owned")

    def test_directory_modes_do_not_restrict_creation(self):
        folder = self.home / "ordinary"
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
        report = create_key(self.client, "sa-test", "both", folder / "key.json")
        self.assertEqual(report["status"], "created")

    def test_symlinked_parent_uses_the_selected_location(self):
        link = self.home / "redirect"
        link.symlink_to(self.home, target_is_directory=True)
        report = create_key(self.client, "sa-test", "both", link / "key.json")
        self.assertEqual(report["status"], "created")
        self.assertTrue((self.home / "key.json").is_file())

    def test_unreadable_inventory_never_creates_a_key(self):
        self.session.deny_inventory = True
        report = inspect_account(self.client, "sa-test")
        self.assertEqual(report["keys"]["status"], "not_verifiable")
        with self.assertRaises(SpeechError):
            create_key(self.client, "sa-test", "both", self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(any(call[0] == "POST" for call in self.session.calls))

    def test_uncertain_creation_is_not_retried_and_reservation_blocks_a_repeat(self):
        self.session.post_error = True
        with self.assertRaises(SpeechError) as caught:
            create_key(self.client, "sa-test", "both", self.destination)
        self.assertEqual(caught.exception.category, "SUBMISSION_UNCERTAIN")
        self.assertNotIn("never-print-this", str(caught.exception))
        with self.assertRaises(SpeechError):
            create_key(self.client, "sa-test", "both", self.destination)
        self.assertEqual(sum(call[0] == "POST" for call in self.session.calls), 1)
        self.assertTrue(self.destination.exists())

    def test_secret_save_failure_does_not_recreate_or_revoke(self):
        with patch("yandex_speech.cloud_setup.os.write", side_effect=OSError("secret-value")):
            with self.assertRaises(SpeechError) as caught:
                create_key(self.client, "sa-test", "both", self.destination)
        self.assertEqual(caught.exception.category, "STATE")
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertEqual(sum(call[0] == "POST" for call in self.session.calls), 1)
        self.assertFalse(any(call[0] == "DELETE" for call in self.session.calls))

    def test_static_storage_key_is_distinct_and_does_not_invent_expiry(self):
        report = create_key(self.client, "sa-test", "s3-static", self.destination)
        saved = json.loads(self.destination.read_text())
        self.assertEqual(saved["accessKey"]["keyId"], "synthetic-access-id")
        self.assertNotIn("synthetic-access-id", json.dumps(report))
        post = next(call for call in self.session.calls if call[0] == "POST")
        self.assertTrue(post[1].endswith("/iam/aws-compatibility/v1/accessKeys"))
        self.assertNotIn("scopes", post[2]["json"])
        with self.assertRaises(SpeechError):
            prepare_key(self.client, "sa-test", "s3-static", self.home / "other.json",
                        expires_at="2099-01-01T00:00:00Z")

    def test_invalid_target_or_expired_request_fails_before_api(self):
        for account, expiry in (("../foreign", None), ("sa-test", "2000-01-01T00:00:00Z"),
                                 ("sa-test", "not-a-timestamp")):
            with self.subTest(account=account), self.assertRaises(SpeechError):
                prepare_key(self.client, account, "both", self.destination, expires_at=expiry)
        self.assertEqual(self.session.calls, [])

    def test_cli_uses_named_environment_without_printing_secrets(self):
        from yandex_speech.readiness import main
        output = io.StringIO()
        with patch.dict(os.environ, {"TEST_MANAGEMENT_TOKEN": "synthetic-management-token"}):
            with patch("requests.Session", return_value=self.session), contextlib.redirect_stdout(output):
                status = main(["create-key", "--service-account-id", "sa-test", "--purpose", "both",
                               "--secret-out", str(self.destination),
                               "--management-token-env", "TEST_MANAGEMENT_TOKEN"])
        self.assertEqual(status, 0)
        report = json.loads(output.getvalue())
        self.assertNotIn("synthetic-management-token", output.getvalue())
        self.assertNotIn("synthetic-one-time-secret", output.getvalue())
        self.assertEqual(report["status"], "created")

    def test_unexpected_scopes_are_captured_but_never_reported_as_success(self):
        original = self.session.request
        def request(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST":
                response.data["apiKey"]["scopes"] = ["unexpected-broad-scope"]
            return response
        with patch.object(self.session, "request", side_effect=request), self.assertRaises(SpeechError) as caught:
            create_key(self.client, "sa-test", "stt", self.destination)
        self.assertEqual(caught.exception.category, "STATE")
        self.assertEqual(json.loads(self.destination.read_text())["secret"], "synthetic-one-time-secret")
        self.assertEqual(sum(call[0] == "POST" for call in self.session.calls), 1)

    def test_requested_expiry_must_be_confirmed_by_the_creation_response(self):
        original = self.session.request
        def request(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST":
                response.data["apiKey"].pop("expiresAt", None)
            return response
        with patch.object(self.session, "request", side_effect=request), self.assertRaises(SpeechError):
            create_key(self.client, "sa-test", "tts", self.destination,
                       expires_at="2099-01-01T00:00:00Z")
        self.assertTrue(self.destination.exists())

    def test_pagination_collects_all_keys_and_refuses_a_nonadvancing_token(self):
        first = {"apiKeys": [self.session.keys[0]], "nextPageToken": "next"}
        second = {"apiKeys": [{"id": "second", "serviceAccountId": "sa-test", "scopes": []}]}
        with patch.object(self.client, "request", side_effect=[first, second]):
            keys = self.client.listing("https://unused", "apiKeys", serviceAccountId="sa-test")
        self.assertEqual([x["id"] for x in keys], ["old", "second"])
        with patch.object(self.client, "request", return_value=first), self.assertRaises(SpeechError):
            self.client.listing("https://unused", "apiKeys", serviceAccountId="sa-test")

    def test_named_management_profile_accepts_the_cli_token_line_without_argv_secret(self):
        from types import SimpleNamespace
        from yandex_speech.cloud_setup import ManagementClient
        result = SimpleNamespace(stdout="synthetic-management-token\n")
        args = SimpleNamespace(management_token_env=None, management_profile="approved")
        with patch("shutil.which", return_value="/fake/yc"), patch("subprocess.run", return_value=result) as run:
            client = ManagementClient.from_args(args)
        self.assertIsInstance(client, ManagementClient)
        self.assertEqual(run.call_args.args[0], ["/fake/yc", "--profile", "approved", "iam", "create-token"])
        self.assertNotIn("synthetic-management-token", str(run.call_args))

    def test_cli_existing_destination_does_not_even_obtain_a_management_token(self):
        from yandex_speech.readiness import main
        self.destination.write_text("operator-owned")
        with patch("yandex_speech.cloud_setup.ManagementClient.from_args") as auth:
            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["create-key", "--service-account-id", "sa-test",
                               "--secret-out", str(self.destination), "--management-profile", "approved"])
        self.assertEqual(status, 2)
        auth.assert_not_called()

    def test_definite_creation_denial_preserves_existing_keys_and_releases_empty_claim(self):
        original = self.session.request
        def request(method, url, **kwargs):
            if method == "POST":
                self.session.calls.append((method, url, kwargs))
                return Response({"message": "never-print-this"}, 403)
            return original(method, url, **kwargs)
        with patch.object(self.session, "request", side_effect=request), self.assertRaises(SpeechError) as caught:
            create_key(self.client, "sa-test", "both", self.destination)
        self.assertEqual(caught.exception.category, "PERMISSION")
        self.assertFalse(self.destination.exists())
        self.assertEqual(sum(call[0] == "POST" for call in self.session.calls), 1)
        self.assertEqual(self.session.keys[0]["id"], "old")

    def test_default_destination_is_generated_under_user_secrets(self):
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            planned = prepare_key(self.client, "sa-test", "both")
            self.assertFalse((self.home / "secrets").exists())
            first = create_key(self.client, "sa-test", "both")
            second = create_key(self.client, "sa-test", "both")
        for report in (planned, first, second):
            self.assertEqual(Path(report["secret_file"]).parent, self.home / "secrets")
        self.assertNotEqual(first["secret_file"], second["secret_file"])
        self.assertTrue(Path(first["secret_file"]).is_file())

    def test_creation_race_never_overwrites_the_newly_existing_file(self):
        from yandex_speech.cloud_setup import _plan
        def plan(*args):
            result = _plan(*args)
            self.destination.write_text("intervening file")
            return result
        with patch("yandex_speech.cloud_setup._plan", side_effect=plan):
            with self.assertRaises(SpeechError):
                create_key(self.client, "sa-test", "both", self.destination)
        self.assertEqual(self.destination.read_text(), "intervening file")
        self.assertFalse(any(call[0] == "POST" for call in self.session.calls))

    def test_cli_creation_without_secret_out_returns_a_generated_filename(self):
        from yandex_speech.readiness import main
        output = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(self.home), "TEST_MANAGEMENT_TOKEN": "synthetic-management-token"}):
            with patch("requests.Session", return_value=self.session), contextlib.redirect_stdout(output):
                status = main(["create-key", "--service-account-id", "sa-test",
                               "--management-token-env", "TEST_MANAGEMENT_TOKEN"])
        self.assertEqual(status, 0)
        saved = Path(json.loads(output.getvalue())["secret_file"])
        self.assertEqual(saved.parent, self.home / "secrets")
        self.assertTrue(saved.is_file())

    def test_execute_only_ancestor_as_an_unprivileged_process(self):
        import pwd
        import subprocess
        import sys
        account = pwd.getpwnam("frodo") if os.geteuid() == 0 else None
        traverse = self.home / "traverse"
        target = traverse / "destination"
        self.home.chmod(0o755)
        traverse.mkdir()
        target.mkdir()
        target.chmod(0o777)
        traverse.chmod(0o111)
        def demote():
            os.setgroups(os.getgrouplist(account.pw_name, account.pw_gid))
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
        program = (
            "from pathlib import Path; import sys; "
            "from tests.test_cloud_setup import FakeSession; "
            "from yandex_speech.cloud_setup import ManagementClient,create_key; "
            "p=Path(sys.argv[1]); "
            "r=create_key(ManagementClient('synthetic-token',_session=FakeSession()),'sa-test','both',p); "
            "assert r['status']=='created'; assert p.exists()"
        )
        try:
            result = subprocess.run([sys.executable, "-c", program, str(target / "key.json")],
                                    capture_output=True, text=True,
                                    preexec_fn=demote if account is not None else None)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            traverse.chmod(0o755)
