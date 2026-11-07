"""Actual optional SDK mutation retry policy; selected by the storage profile."""
import importlib.util
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from yandex_speech import ObjectStorageUploader, SpeechError
from yandex_speech.config import RuntimeContext


class StorageSdkTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("boto3"), "optional storage SDK is absent")
    def test_real_sdk_does_not_retry_an_uncertain_put(self):
        import boto3
        from botocore.exceptions import ReadTimeoutError

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

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            with wave.open(str(source), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(bytes(32000))
            uploader = ObjectStorageUploader(bucket="private-speech",
                aws_access_key_id="synthetic-sdk-id", aws_secret_access_key="synthetic-sdk-secret",
                job_dir=root / "sdk-state", _context=RuntimeContext(user_home=root), _environment={})
            with patch("boto3.client", side_effect=actual_client):
                with self.assertRaises(SpeechError) as raised:
                    uploader.upload(source)
            self.assertEqual(raised.exception.code, "UPLOAD_UNCERTAIN")
            self.assertEqual(wire.calls, 1)
