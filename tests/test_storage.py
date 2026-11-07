"""Legacy storage compatibility without external bucket mutations."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from yandex_speech import SpeechTranscriber,SpeechError
from yandex_speech.config import RuntimeContext


class StorageContractTests(unittest.TestCase):
    def test_unique_owned_keys_and_nonsecret_storage_defaults(self):
        from scripts import transcribe
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"same.wav"
            source.write_bytes(b"original")
            backend=Mock()
            with patch.object(transcribe,"boto3",backend),patch.dict(os.environ,{
                "YANDEX_SPEECH_AWS_ACCESS_KEY_ID":"synthetic-access",
                "YANDEX_SPEECH_AWS_SECRET_ACCESS_KEY":"synthetic-secret"}):
                settings={"bucket":"configured","prefix":"owned","endpoint":"https://storage.yandexcloud.net"}
                first=transcribe.upload_to_object_storage(source,_storage=settings)
                second=transcribe.upload_to_object_storage(source,_storage=settings)
            self.assertNotEqual(first,second)
            self.assertIn("/configured/owned/",first)
            self.assertNotIn("synthetic-access",first)
            self.assertNotIn("synthetic-secret",second)
            calls=backend.client.return_value.upload_file.call_args_list
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[0].args[1],"configured")
            self.assertEqual(source.read_bytes(),b"original")

    def test_explicit_bucket_precedes_file_and_environment_defaults(self):
        import wave
        from yandex_speech.storage import ObjectStorageUploader
        from yandex_speech.rest_stt import RestTransport
        from tests.test_owned_storage import FakeS3
        from tests.test_rest_stt import Response, Session
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".config/yandex-speech/config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"speech": {"object_storage": {
                "bucket": "file-bucket", "prefix": "file-prefix"}}}))
            source = root / "audio.wav"
            with wave.open(str(source), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(bytes(32000))
            captured, backend = {}, FakeS3()
            session = Session(Response({"id": "accepted-v2"}), Response({"done": True, "response": {"chunks": []}}))

            def uploader_factory(**options):
                uploader = ObjectStorageUploader(_client=backend, **options)
                captured.update(bucket=uploader.bucket, prefix=uploader.prefix)
                return uploader

            with patch.dict(os.environ, {"YANDEX_SPEECH_S3_BUCKET": "environment-bucket"}):
                client = SpeechTranscriber(api_key="synthetic", _context=RuntimeContext(user_home=root),
                    _transport_factory=lambda version, transport: RestTransport(
                        "synthetic", api_version=version, _session=session))
                with patch("yandex_speech.storage.ObjectStorageUploader", side_effect=uploader_factory):
                    result = client.transcribe_async_s3(source, bucket="explicit-bucket")
            self.assertEqual(captured["bucket"], "explicit-bucket")
            self.assertEqual(captured["prefix"], "file-prefix")
            self.assertEqual(result["api_version"], "2")
            self.assertEqual(len(backend.puts), 1)

    def test_fixed_v2_high_level_uses_preserved_async_storage_route(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SpeechTranscriber(api_key="synthetic", _context=RuntimeContext(user_home=Path(directory)))
            with patch.object(client, "transcribe_async_s3", return_value={"api_version": "2"}) as call:
                result = client.transcribe_auto("audio.ogg", api_version="2")
            self.assertEqual(result["api_version"], "2")
            call.assert_called_once_with("audio.ogg", api_version="2")

    def test_storage_failure_does_not_escape_or_log_provider_payload(self):
        import wave
        from yandex_speech.storage import ObjectStorageUploader
        class Denied(Exception):
            response = {"Error": {"Code": "AccessDenied", "Message": "sentinel-secret-315"}}
        class Backend:
            def put_object(self, **options):
                raise Denied()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "audio.wav"
            with wave.open(str(source), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(bytes(32000))
            context = RuntimeContext(user_home=root)
            uploader = ObjectStorageUploader(bucket="private-source", job_dir=root / "uploads",
                _context=context, _client=Backend())
            rpc = Mock(side_effect=AssertionError("denied upload submitted recognition"))
            client = SpeechTranscriber(api_key="synthetic", _context=context,
                _uploader=uploader, _transport_factory=rpc)
            with self.assertRaises(SpeechError) as cm:
                client.transcribe_async_s3(source, bucket="private-source")
            self.assertEqual(cm.exception.category, "PERMISSION")
            self.assertNotIn("sentinel-secret-315", str(cm.exception))
            rpc.assert_not_called()
