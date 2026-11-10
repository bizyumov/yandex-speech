"""Set up the selected S3 bucket/prefix using static credentials, without folder listing."""
from __future__ import annotations

import re
import time
import uuid

from .config import DEFAULTS, STORAGE_BUCKET_TEMPLATE
from .errors import SpeechError
from .key_env import _credential_variables, _load_response, read_env_values
from .storage import ObjectStorageUploader, _coordinates, _error


def _code(error):
    response = getattr(error, "response", {})
    return str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""


def setup_storage(env_file, secret_file=None, *, _client=None):
    selected = read_env_values(env_file)
    if secret_file is not None:
        response = _load_response(secret_file)
        credentials = _credential_variables(response)
        account = response.get("accessKey", {}).get("serviceAccountId")
    else:
        credentials, account = selected, None
    access = credentials.get("YANDEX_SPEECH_AWS_ACCESS_KEY_ID")
    secret = credentials.get("YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY")
    if not access or not secret:
        raise SpeechError("AUTH", "storage setup requires the saved static key or the selected env's static credential pair")
    bucket_env, prefix_env = "YANDEX_SPEECH_S3_BUCKET", "YANDEX_SPEECH_S3_PREFIX"
    if bucket_env in selected:
        bucket = selected[bucket_env]
    else:
        if not isinstance(account, str) or not re.fullmatch(r"[a-z0-9-]{1,49}", account):
            raise SpeechError("CONFIG", "default bucket naming needs the serviceAccountId from a saved static-key response")
        bucket = STORAGE_BUCKET_TEMPLATE.format(service_account_id=account)
    defaults = DEFAULTS["object_storage"]
    bucket, endpoint, prefix = _coordinates(
        bucket, defaults["endpoint"], selected.get(prefix_env, defaults["prefix"]))
    uploader = ObjectStorageUploader(bucket=bucket, endpoint=endpoint, prefix=prefix,
        aws_access_key_id=access, aws_secret_access_key=secret, _environment={}, _client=_client)
    created = False
    with uploader._connection(endpoint, time.monotonic() + 90) as client:
        try:
            client.head_bucket(Bucket=bucket)
        except Exception as error:
            if _code(error) not in {"404", "NoSuchBucket", "NotFound"}:
                raise _error(error) from None
            try:
                client.create_bucket(Bucket=bucket)
                created = True
                client.head_bucket(Bucket=bucket)
            except Exception as error:
                raise _error(error, mutation=True) from None
        try:
            listing = client.list_objects_v2(Bucket=bucket, Prefix=prefix + "/", MaxKeys=1)
            present = bool(listing.get("Contents"))
            if not present:
                try:
                    client.put_object(Bucket=bucket, Key=prefix + "/", Body=b"", IfNoneMatch="*")
                except Exception as error:
                    if _code(error) not in {"PreconditionFailed", "412"}:
                        raise
            probe = prefix + "/.yandex-speech-setup-" + uuid.uuid4().hex
            payload = b"yandex-speech storage setup probe"
            receipt = client.put_object(Bucket=bucket, Key=probe, Body=payload, IfNoneMatch="*")
            version = receipt.get("VersionId")
            coordinates = {"Bucket": bucket, "Key": probe}
            if version:
                coordinates["VersionId"] = version
            try:
                result = client.get_object(**coordinates)
                body = result["Body"]
                try:
                    if body.read() != payload:
                        raise SpeechError("PROVIDER", "storage setup probe content did not match")
                finally:
                    body.close()
            finally:
                client.delete_object(**coordinates)
        except SpeechError:
            raise
        except Exception as error:
            raise _error(error, mutation=True) from None
    return {"status": "ready", "bucket": bucket, "prefix": prefix, "endpoint": endpoint,
            "bucket_created": created, "prefix_previously_present": present,
            "access_probe": "list-write-read-delete-passed", "env_file_changed": False,
            "next_action": "Call apply-key separately with the saved key, your own --env-file, and these --s3-bucket and --s3-prefix values. Existing env settings have priority. Endpoint stays in skill configuration."}
