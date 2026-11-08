"""Explicit account/key setup. Never edits runtime configuration or grants roles."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

import requests

from .errors import SpeechError

IAM = "https://iam.api.cloud.yandex.net"
FOLDERS = "https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/"
SCOPES = {"stt": ["yc.ai.speechkitStt.execute"],
          "tts": ["yc.ai.speechkitTts.execute"],
          "both": ["yc.ai.speechkitStt.execute", "yc.ai.speechkitTts.execute"],
          "s3-static": []}
ROLES = {"stt": ["ai.speechkit-stt.user"], "tts": ["ai.speechkit-tts.user"],
         "both": ["ai.speechkit-stt.user", "ai.speechkit-tts.user"],
         "s3-static": ["storage.uploader"]}
COMMANDS = ("inspect-account", "prepare-key", "create-key")


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


@contextmanager
def _private_destination(destination):
    path = Path(destination).expanduser().absolute()
    if ".." in path.parts or path.name in {"", ".", ".."}:
        raise SpeechError("CONFIG", "secret output must be an unambiguous new file")
    directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = following
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SpeechError("CONFIG", "secret output needs an existing owned directory with mode 0700")
        try:
            os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SpeechError("CONFIG", "secret destination already exists; it will not be replaced")
        yield path, directory_fd
    except OSError:
        raise SpeechError("CONFIG", "secret output needs an existing private directory without symlink redirection") from None
    finally:
        os.close(directory_fd)


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


def prepare_key(client, account_id, purpose, secret_out, *, expires_at=None):
    _validate(account_id, purpose, expires_at)
    with _private_destination(secret_out) as (path, _):
        return {**_plan(client, account_id, purpose, expires_at), "secret_file": str(path)}


def _claim_matches(parent, name, fd):
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    owned = os.fstat(fd)
    directory = os.fstat(parent)
    return (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino) and (
        stat.S_ISREG(current.st_mode) and stat.S_IMODE(current.st_mode) == 0o600 and
        directory.st_uid == os.getuid() and stat.S_IMODE(directory.st_mode) == 0o700)


def create_key(client, account_id, purpose, secret_out, *, expires_at=None):
    _validate(account_id, purpose, expires_at)
    with _private_destination(secret_out) as (path, parent):
        plan = _plan(client, account_id, purpose, expires_at)
        try:
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        except OSError:
            raise SpeechError("STATE", "secret destination changed before creation; no key was requested") from None
        try:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.fsync(parent)
            if not _claim_matches(parent, path.name, fd):
                raise SpeechError("STATE", "secret destination changed; no key was requested")
            try:
                response = client.request("POST", _endpoint(purpose), json=plan["request"])
            except SpeechError as error:
                if error.category in {"AUTH", "PERMISSION", "CONFIG", "LIMIT"} and _claim_matches(parent, path.name, fd):
                    os.unlink(path.name, dir_fd=parent)
                    os.fsync(parent)
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
                os.fsync(parent)
                if not _claim_matches(parent, path.name, fd):
                    raise OSError()
            except OSError:
                raise SpeechError("STATE", "key creation succeeded but protected secret publication failed; inspect inventory and destination, do not retry or revoke automatically") from None
            field = "accessKey" if purpose == "s3-static" else "apiKey"
            metadata = response.get(field)
            if (not isinstance(metadata, dict) or metadata.get("serviceAccountId") != account_id
                    or not isinstance(metadata.get("id"), str) or not metadata["id"]
                    or not isinstance(response.get("secret"), str) or not response["secret"].strip()):
                raise SpeechError("STATE", "unexpected creation response retained in the protected file; inspect it before retrying")
            actual = _metadata(metadata)
            if purpose != "s3-static":
                if set(actual["scopes"]) != set(SCOPES[purpose]):
                    raise SpeechError("STATE", "created key scopes do not match the request; protected response retained, do not retry automatically")
                if expires_at is not None:
                    try:
                        confirmed = _timestamp(actual.get("expiresAt"))
                    except SpeechError:
                        confirmed = None
                    if confirmed != _timestamp(expires_at):
                        raise SpeechError("STATE", "created key expiry was not confirmed; protected response retained, do not retry automatically")
            elif not isinstance(metadata.get("keyId"), str) or not metadata["keyId"]:
                raise SpeechError("STATE", "created storage key ID is missing; protected response retained")
            return {"status": "created", "purpose": purpose, "key": actual,
                    "secret_file": str(path), "execution_probe": "not_run",
                    "next_action": "Load the protected credential into the intended runtime through its supported secret writer; verify actual execution before retiring any old key."}
        finally:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=COMMANDS)
    parser.add_argument("--service-account-id", required=True)
    parser.add_argument("--purpose", choices=tuple(SCOPES), default="both")
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--management-profile", help="Existing authorized yc profile")
    auth.add_argument("--management-token-env", help="Name of the environment variable containing a management IAM token")
    parser.add_argument("--secret-out", type=Path)
    parser.add_argument("--expires-at", help="Explicit API-key expiry in RFC3339")
    args = parser.parse_args(argv)
    if args.operation != "inspect-account" and args.secret_out is None:
        parser.error("prepare-key/create-key require --secret-out in an existing private directory")
    if args.operation == "inspect-account" and (args.secret_out is not None or args.expires_at is not None):
        parser.error("inspection does not take secret-output or expiry options")
    try:
        _validate(args.service_account_id, args.purpose, args.expires_at)
        if args.operation != "inspect-account":
            with _private_destination(args.secret_out):
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
