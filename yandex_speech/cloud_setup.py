"""Explicit account/key setup and separate env application; never grants roles."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid
import subprocess

import requests

from .errors import SpeechError
from .key_env import apply_key

IAM = "https://iam.api.cloud.yandex.net"
FOLDERS = "https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/"
SCOPES = {"stt": ["yc.ai.speechkitStt.execute"],
          "tts": ["yc.ai.speechkitTts.execute"],
          "both": ["yc.ai.speechkitStt.execute", "yc.ai.speechkitTts.execute"],
          "s3-static": []}
ROLES = {"stt": ["ai.speechkit-stt.user"], "tts": ["ai.speechkit-tts.user"],
         "both": ["ai.speechkit-stt.user", "ai.speechkit-tts.user"],
         "s3-static": ["storage.uploader"]}
COMMANDS = ("inspect-account", "prepare-key", "create-key", "apply-key", "setup-storage")


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        raise SpeechError("CONFIG", "expiry must be an RFC3339 timestamp with a timezone") from None


def _validate(account_id, purpose, expires_at=None):
    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", account_id):
        raise SpeechError("CONFIG", "supply the exact service-account ID")
    if purpose not in SCOPES:
        raise SpeechError("CONFIG", "purpose must be stt, tts, both or s3-static")
    if expires_at is not None:
        if purpose == "s3-static":
            raise SpeechError("CONFIG", "static storage keys do not support this expiry field")
        if _timestamp(expires_at) <= datetime.now(timezone.utc):
            raise SpeechError("CONFIG", "requested API-key expiry must be in the future")


class _Bearer(requests.auth.AuthBase):
    def __init__(self, token):
        self.token = token
    def __call__(self, request):
        request.headers["Authorization"] = "Bearer " + self.token
        return request


class ManagementClient:
    def __init__(self, token, *, _session=None):
        token = token.strip() if isinstance(token, str) else token
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise SpeechError("AUTH", "a management IAM token is required")
        self._auth = _Bearer(token)
        self._session = _session or requests.Session()

    @classmethod
    def from_args(cls, args):
        if args.management_token_env:
            name = args.management_token_env
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise SpeechError("CONFIG", "management-token-env must name an environment variable")
            return cls(os.environ.get(name))
        profile = args.management_profile
        if not profile or profile.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
            raise SpeechError("CONFIG", "supply an existing named yc management profile")
        executable = shutil.which("yc")
        if not executable:
            raise SpeechError("PREREQUISITE", "yc is required for management-profile; alternatively use management-token-env")
        try:
            result = subprocess.run([executable, "--profile", profile, "iam", "create-token"],
                capture_output=True, text=True, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError):
            raise SpeechError("AUTH", "the selected yc profile could not obtain a management IAM token") from None
        return cls(result.stdout)

    def request(self, method, url, **kwargs):
        try:
            response = self._session.request(method, url, auth=self._auth,
                allow_redirects=False, timeout=(5, 30), **kwargs)
        except requests.RequestException:
            category = "SUBMISSION_UNCERTAIN" if method == "POST" else "NETWORK"
            raise SpeechError(category, "management request outcome is uncertain; inspect inventory before retrying" if method == "POST"
                              else "management metadata could not be retrieved") from None
        status = response.status_code
        if status != 200:
            category = {400: "CONFIG", 401: "AUTH", 403: "PERMISSION", 429: "LIMIT"}.get(status)
            if category is None:
                category = "SUBMISSION_UNCERTAIN" if method == "POST" else "STATE"
            raise SpeechError(category, "management API rejected the request; roles/scopes or resource absence are not inferred")
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise SpeechError("SUBMISSION_UNCERTAIN" if method == "POST" else "STATE",
                              "management response could not be read; inspect inventory before retrying") from None
        if not isinstance(data, dict):
            raise SpeechError("SUBMISSION_UNCERTAIN" if method == "POST" else "STATE",
                              "management response is not a resource mapping; inspect inventory before retrying")
        return data

    def listing(self, url, field, **params):
        items, tokens = [], set()
        for _ in range(100):
            data = self.request("GET", url, params={**params, "pageSize": 1000})
            page = data.get(field, [])
            if not isinstance(page, list) or any(not isinstance(x, dict) for x in page):
                raise SpeechError("STATE", "management inventory has an invalid shape")
            items.extend(page)
            if len(items) > 10000:
                raise SpeechError("LIMIT", "management inventory exceeds the inspection budget")
            token = data.get("nextPageToken")
            if not token:
                return items
            if not isinstance(token, str) or token in tokens or len(token) > 2000:
                raise SpeechError("STATE", "management pagination did not advance")
            tokens.add(token)
            params["pageToken"] = token
        raise SpeechError("LIMIT", "management inventory exceeds the page budget")


def _endpoint(purpose):
    return IAM + ("/iam/aws-compatibility/v1/accessKeys" if purpose == "s3-static" else "/iam/v1/apiKeys")


def _metadata(key):
    result = {name: key[name] for name in ("id", "serviceAccountId", "createdAt", "expiresAt")
              if isinstance(key.get(name), str)}
    scopes = key.get("scopes", [key["scope"]] if isinstance(key.get("scope"), str) else [])
    result["scopes"] = [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else []
    if result.get("expiresAt"):
        try:
            result["expired"] = _timestamp(result["expiresAt"]) <= datetime.now(timezone.utc)
        except SpeechError:
            result["expired"] = None
    else:
        result["expired"] = False
    return result


def inspect_account(client, account_id, purpose="both"):
    _validate(account_id, purpose)
    account = client.request("GET", IAM + "/iam/v1/serviceAccounts/" + account_id)
    if account.get("id") != account_id:
        raise SpeechError("STATE", "returned account does not match the requested target")
    result = {"account": {name: account[name] for name in ("id", "folderId", "status", "expiresAt")
                          if isinstance(account.get(name), str)},
              "purpose": purpose, "execution_probe": "not_run",
              "permissions": {"status": "not_verifiable", "required_roles": ROLES[purpose],
                              "required_scopes": SCOPES[purpose], "direct_folder_roles": [],
                              "note": "Inherited/custom/resource grants and actual execution are not certified."}}
    field = "accessKeys" if purpose == "s3-static" else "apiKeys"
    try:
        keys = client.listing(_endpoint(purpose), field, serviceAccountId=account_id)
        if any(key.get("serviceAccountId") != account_id for key in keys):
            raise SpeechError("STATE", "inventory contains a different service account")
        result["keys"] = {"status": "inspected", "items": [_metadata(key) for key in keys]}
    except SpeechError as error:
        result["keys"] = {"status": "not_verifiable", "reason": error.category, "items": []}
    folder = account.get("folderId")
    if isinstance(folder, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", folder):
        try:
            bindings = client.listing(FOLDERS + folder + ":listAccessBindings", "accessBindings")
            roles = sorted({binding["roleId"] for binding in bindings
                if binding.get("subject") == {"id": account_id, "type": "serviceAccount"}
                and isinstance(binding.get("roleId"), str)})
            result["permissions"]["direct_folder_roles"] = roles
            if roles:
                result["permissions"]["status"] = "partial_metadata"
        except SpeechError:
            pass
    return result


def _secret_path(destination, account_id, purpose):
    if destination is None:
        destination = Path.home() / "secrets" / f"yandex-speech-{account_id}-{purpose}-{uuid.uuid4().hex}.json"
    return Path(destination).expanduser().absolute()


@contextmanager
def _secret_destination(destination):
    path = Path(destination).expanduser().absolute()
    if os.path.lexists(path):
        raise SpeechError("CONFIG", "secret destination already exists; it will not be replaced")
    try:
        yield path
    except OSError:
        raise SpeechError("STATE", "secret destination access failed; inspect the destination and key inventory before retrying") from None


def _plan(client, account_id, purpose, expires_at):
    report = inspect_account(client, account_id, purpose)
    account = report["account"]
    if account.get("status") not in {None, "ACTIVE"}:
        raise SpeechError("CONFIG", "the service account is not active")
    if account.get("expiresAt") and _timestamp(account["expiresAt"]) <= datetime.now(timezone.utc):
        raise SpeechError("CONFIG", "the service account has expired")
    if report["keys"]["status"] != "inspected":
        raise SpeechError("PERMISSION", "readable key inventory is required before creation; access is not verifiable")
    request = {"serviceAccountId": account_id, "description": "Yandex Speech " + purpose}
    if purpose != "s3-static":
        request["scopes"] = list(SCOPES[purpose])
        if expires_at is not None:
            request["expiresAt"] = expires_at
    return {"status": "planned", "purpose": purpose, "request": request,
            "creation_role": "iam.serviceAccounts.accessKeyAdmin" if purpose == "s3-static" else "iam.serviceAccounts.apiKeyAdmin",
            "inspection": report, "next_action": "Only explicit create-key creates a credential; no roles or runtime settings are changed."}


def prepare_key(client, account_id, purpose, secret_out=None, *, expires_at=None):
    _validate(account_id, purpose, expires_at)
    with _secret_destination(_secret_path(secret_out, account_id, purpose)) as path:
        return {**_plan(client, account_id, purpose, expires_at), "secret_file": str(path)}


def _claim_matches(path, fd):
    current = os.stat(path, follow_symlinks=False)
    owned = os.fstat(fd)
    return (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino)


def create_key(client, account_id, purpose, secret_out=None, *, expires_at=None):
    _validate(account_id, purpose, expires_at)
    with _secret_destination(_secret_path(secret_out, account_id, purpose)) as path:
        plan = _plan(client, account_id, purpose, expires_at)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except OSError:
            raise SpeechError("STATE", "could not create a new secret destination; no key was requested") from None
        try:
            os.fsync(fd)
            if not _claim_matches(path, fd):
                raise SpeechError("STATE", "secret destination changed; no key was requested")
            try:
                response = client.request("POST", _endpoint(purpose), json=plan["request"])
            except SpeechError as error:
                if error.category in {"AUTH", "PERMISSION", "CONFIG", "LIMIT"} and _claim_matches(path, fd):
                    os.unlink(path)
                raise
            # Capture the one-time response before interpreting optional metadata.
            try:
                payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
                view = memoryview(payload)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError()
                    view = view[count:]
                os.fsync(fd)
                if not _claim_matches(path, fd):
                    raise OSError()
            except OSError:
                raise SpeechError("STATE", "key creation succeeded but secret save failed; inspect inventory and destination, do not retry or revoke automatically") from None
            field = "accessKey" if purpose == "s3-static" else "apiKey"
            metadata = response.get(field)
            if (not isinstance(metadata, dict) or metadata.get("serviceAccountId") != account_id
                    or not isinstance(metadata.get("id"), str) or not metadata["id"]
                    or not isinstance(response.get("secret"), str) or not response["secret"].strip()):
                raise SpeechError("STATE", "unexpected creation response retained in the saved file; inspect it before retrying")
            actual = _metadata(metadata)
            if purpose != "s3-static":
                if set(actual["scopes"]) != set(SCOPES[purpose]):
                    raise SpeechError("STATE", "created key scopes do not match the request; response retained, do not retry automatically")
                if expires_at is not None:
                    try:
                        confirmed = _timestamp(actual.get("expiresAt"))
                    except SpeechError:
                        confirmed = None
                    if confirmed != _timestamp(expires_at):
                        raise SpeechError("STATE", "created key expiry was not confirmed; response retained, do not retry automatically")
            elif not isinstance(metadata.get("keyId"), str) or not metadata["keyId"]:
                raise SpeechError("STATE", "created storage key ID is missing; response retained")
            return {"status": "created", "purpose": purpose, "key": actual,
                    "secret_file": str(path), "execution_probe": "not_run",
                    "next_action": "Call apply-key with this secret_file and your own .env file. Gateway variables load at startup; explicitly supply the new variables to immediate speech-generation child processes."}
        finally:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=COMMANDS)
    parser.add_argument("--service-account-id")
    parser.add_argument("--purpose", choices=tuple(SCOPES), default="both")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--management-profile", help="Existing authorized yc profile")
    auth.add_argument("--management-token-env", help="Name of the environment variable containing a management IAM token")
    parser.add_argument("--secret-out", type=Path, help="New response file; default: generated filename in ~/secrets")
    parser.add_argument("--secret-file", type=Path, help="Saved response JSON for apply-key or setup-storage")
    parser.add_argument("--env-file", type=Path, help="Agent's own env file")
    parser.add_argument("--s3-bucket", help="Bucket returned by setup-storage; existing env value takes priority")
    parser.add_argument("--s3-prefix", help="Prefix returned by setup-storage; existing env value takes priority")
    parser.add_argument("--expires-at", help="Explicit API-key expiry in RFC3339")
    args = parser.parse_args(argv)
    if args.operation in {"apply-key", "setup-storage"}:
        if args.env_file is None or (args.operation == "apply-key" and args.secret_file is None):
            parser.error("apply-key requires --secret-file and --env-file; setup-storage requires --env-file")
        if any((args.service_account_id, args.management_profile, args.management_token_env,
                args.secret_out, args.expires_at)):
            parser.error("apply-key/setup-storage use saved credentials; do not supply management or creation options")
        if args.operation != "apply-key" and (args.s3_bucket is not None or args.s3_prefix is not None):
            parser.error("--s3-bucket and --s3-prefix belong to apply-key; setup-storage selects from env and skill defaults")
    else:
        if any((args.secret_file, args.env_file, args.s3_bucket, args.s3_prefix)):
            parser.error("env application and storage setup options do not belong to account operations")
        if not args.service_account_id or not (args.management_profile or args.management_token_env):
            parser.error("account operations require --service-account-id and management-profile or management-token-env")
        if args.operation == "inspect-account" and (args.secret_out is not None or args.expires_at is not None):
            parser.error("inspection does not take secret-output or expiry options")
    try:
        if args.operation == "apply-key":
            report = apply_key(args.secret_file, args.env_file,
                               s3_bucket=args.s3_bucket, s3_prefix=args.s3_prefix)
        elif args.operation == "setup-storage":
            from .storage_setup import setup_storage
            report = setup_storage(args.env_file, args.secret_file)
        else:
            _validate(args.service_account_id, args.purpose, args.expires_at)
            if args.operation != "inspect-account":
                args.secret_out = _secret_path(args.secret_out, args.service_account_id, args.purpose)
                with _secret_destination(args.secret_out):
                    pass
            client = ManagementClient.from_args(args)
            if args.operation == "inspect-account":
                report = inspect_account(client, args.service_account_id, args.purpose)
            else:
                operation = create_key if args.operation == "create-key" else prepare_key
                report = operation(client, args.service_account_id, args.purpose, args.secret_out, expires_at=args.expires_at)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except SpeechError as error:
        print(json.dumps({"error": {"category": error.category, "message": str(error)}}))
        return 3 if error.category == "PREREQUISITE" else 2
