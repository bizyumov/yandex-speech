"""REST wire contracts without generated protobuf/gRPC dependencies."""
import base64
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from yandex_speech.errors import SpeechError
from yandex_speech.media import PreparedAudio


class Response:
    def __init__(self, data, status=200, *, fragment=7):
        self.data = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.status_code = status
        self.headers = {"x-request-id": "synthetic-request"}
        self.fragment = fragment
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.data), self.fragment):
            yield self.data[offset:offset+self.fragment]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.closed = False

    def request(self, method, url, **options):
        self.calls.append((method, url, options))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed = True


class RestSttTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "prepared.wav"
        self.path.write_bytes(bytes(range(256)) * 3)
        self.info = {"duration_ms": 1000, "size_bytes": self.path.stat().st_size,
            "source_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(), "converted": False,
            "metadata_origin": "local_probe",
            "prepared": {"container": "WAV", "sample_rate_hertz": 16000, "channels": 1}}
        self.audio = PreparedAudio(self.path, "WAV", 16000, 1, 1000, self.info)

    def test_v3_inline_wire_retains_format_and_explicit_text_policy_without_sdk(self):
        from yandex_speech.rest_stt import build_rest_request
        with patch("yandex_speech.grpc_stt.sdk", side_effect=AssertionError("generated SDK was used")):
            wire = build_rest_request(self.audio, language="ru-RU", diarize=True, literature_text=False)
        body = json.loads(wire)
        self.assertEqual(base64.b64decode(body["content"]), self.path.read_bytes())
        self.assertNotIn("uri", body)
        model = body["recognitionModel"]
        self.assertEqual(model["audioFormat"], {"containerAudio": {"containerAudioType": "WAV"}})
        self.assertEqual(model["model"], "general:rc")
        self.assertEqual(model["textNormalization"], {"textNormalization": "TEXT_NORMALIZATION_DISABLED",
            "literatureText": False, "profanityFilter": False, "phoneFormattingMode": "PHONE_FORMATTING_MODE_DISABLED"})
        self.assertEqual(body["speakerLabeling"]["speakerLabeling"], "SPEAKER_LABELING_ENABLED")

    def test_rest_limit_counts_complete_utf8_json_and_base64_at_boundary(self):
        from yandex_speech.rest_stt import build_rest_request
        wire = build_rest_request(self.audio, language="ru-RU")
        for budget, admitted in ((len(wire)-1, False), (len(wire), True), (len(wire)+1, True)):
            with self.subTest(budget=budget), patch("yandex_speech.rest_stt.MAX_REST_BYTES", budget):
                if admitted:
                    self.assertEqual(build_rest_request(self.audio), wire)
                else:
                    with self.assertRaises(SpeechError) as raised:
                        build_rest_request(self.audio)
                    self.assertEqual(raised.exception.category, "LIMIT")

    def test_uri_needs_prepared_metadata_but_no_storage_or_generated_sdk(self):
        from yandex_speech.rest_stt import build_rest_request
        uri = "https://storage.yandexcloud.net/private-speech/owned/audio.wav"
        with patch.dict("sys.modules", {"boto3": None, "grpc": None}):
            body = json.loads(build_rest_request(uri=uri, source_info=self.info))
        self.assertEqual(body["uri"], uri)
        self.assertNotIn("content", body)
        for supplied in (None, {"duration_ms": 1000, "size_bytes": True}):
            with self.assertRaises(SpeechError):
                build_rest_request(uri=uri, source_info=supplied)
        with self.assertRaises(SpeechError):
            build_rest_request(uri=uri+"?credential=sentinel", source_info=self.info)

    def test_v2_is_explicit_uri_only_with_its_own_schema(self):
        from yandex_speech.rest_stt import build_rest_request
        info = json.loads(json.dumps(self.info))
        info["prepared"]["container"] = "MP3"
        body = json.loads(build_rest_request(uri="https://storage.yandexcloud.net/private-speech/audio.mp3",
            source_info=info, api_version="2", literature_text=True))
        specification = body["config"]["specification"]
        self.assertEqual(specification["audioEncoding"], "MP3")
        self.assertIs(specification["literature_text"], True)
        self.assertIs(specification["rawResults"], False)
        self.assertIs(specification["profanityFilter"], False)
        self.assertNotIn("recognitionModel", body)
        with self.assertRaises(SpeechError):
            build_rest_request(self.audio, api_version="2")
        with self.assertRaises(SpeechError):
            build_rest_request(uri=body["audio"]["uri"], source_info=info, api_version="2", diarize=True)

    def test_fragmented_gateway_events_keep_wire_and_genuine_timing(self):
        from yandex_speech.rest_stt import RestTransport
        raw = {"audioCursors": {"finalIndex": "4"}, "channelTag": "1",
            "final": {"alternatives": [{"text": "Контрольный хвост", "words": [
                {"text": "Контрольный", "startTimeMs": "100", "endTimeMs": "700"}]}]}}
        refined = {"finalRefinement": {"finalIndex": "4",
            "normalizedText": {"alternatives": [{"text": "Контрольный хвост."}]}}, "channelTag": "1"}
        response = Response(b"\n".join(json.dumps({"result": event}, ensure_ascii=False).encode()
                                      for event in (raw, refined)), fragment=1)
        session = Session(response)
        transport = RestTransport("synthetic-key", _session=session)
        events = list(transport.get_recognition("accepted-id", 5))
        self.assertEqual(events[0]["final"]["alternatives"][0]["words"][0]["start_time_ms"], "100")
        self.assertEqual(events[0]["audio_cursors"]["final_index"], "4")
        self.assertEqual(events[1]["final_refinement"]["normalized_text"]["alternatives"][0]["text"],
                         "Контрольный хвост.")
        self.assertEqual(events[0]["_raw_provider_event"], {"result": raw})
        self.assertTrue(response.closed)
        self.assertEqual(session.calls[0][2]["params"], {"operationId": "accepted-id"})

    def test_empty_truncated_malformed_and_error_streams_never_complete(self):
        from yandex_speech.rest_stt import RestRequestError, RestTransport
        for wire in (b"", b'{"result":', b'{"result":true}\n', b'{"error":{"code":7,"message":"sentinel-secret"}}\n'):
            with self.subTest(wire=wire):
                response = Response(wire)
                transport = RestTransport("synthetic-key", _session=Session(response))
                with self.assertRaises(RestRequestError) as raised:
                    list(transport.get_recognition("accepted-id", 5))
                self.assertNotIn("sentinel-secret", str(raised.exception))
                self.assertTrue(response.closed)

    def test_submit_has_one_attempt_no_redirect_and_safe_errors(self):
        import requests
        from yandex_speech.rest_stt import RestRequestError, RestTransport
        session = Session(requests.exceptions.ReadTimeout("sentinel-secret"))
        transport = RestTransport("synthetic-key", _session=session)
        with self.assertRaises(RestRequestError) as raised:
            transport.recognize_file(b"{}", 1)
        self.assertNotIn("sentinel-secret", str(raised.exception))
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertTrue(session.calls[0][2]["stream"])
        self.assertEqual(session.calls[0][1], "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync")

    def test_v2_submission_and_operation_endpoint_never_use_v3_results(self):
        from yandex_speech.rest_stt import RestTransport
        session = Session(Response({"id": "accepted-v2"}), Response({"done": True, "response": {"chunks": []}}))
        transport = RestTransport("synthetic-key", api_version="2", _session=session)
        self.assertEqual(transport.recognize_file(b"{}", 5)["id"], "accepted-v2")
        self.assertTrue(transport.get_operation("accepted-v2", 5)["done"])
        self.assertEqual(session.calls[0][1], "https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize")
        self.assertEqual(session.calls[1][1], "https://operation.api.cloud.yandex.net/operations/accepted-v2")

    def test_cancelled_call_and_untrusted_operation_id_never_reach_network(self):
        from yandex_speech.rest_stt import RestRequestError, RestTransport
        session = Session()
        transport = RestTransport("synthetic-key", _session=session)
        with self.assertRaises(SpeechError):
            transport.get_operation("../../foreign", 1)
        event = threading.Event()
        event.set()
        transport.bind_cancel_event(event)
        with self.assertRaises(RestRequestError) as raised:
            transport.get_operation("accepted-id", 1)
        self.assertEqual(raised.exception.code(), "CANCELLED")
        self.assertEqual(session.calls, [])

    def test_bounded_stream_does_not_accept_an_unterminated_oversized_record(self):
        from yandex_speech.rest_stt import RestRequestError, RestTransport
        transport = RestTransport("synthetic-key", _session=Session(Response(b" "*100, fragment=10)))
        with patch("yandex_speech.rest_stt.MAX_EVENT_BYTES", 32):
            with self.assertRaises(RestRequestError):
                list(transport.get_recognition("accepted-id", 1))

    def test_metadata_only_streams_and_malformed_operation_status_cannot_complete(self):
        from yandex_speech.rest_stt import RestRequestError, RestTransport
        for wire in ({"result": {"sessionUuid": {"uuid": "synthetic"}}},
                     {"result": {"unexpected": "payload"}}, {"result": {"eouUpdate": {"timeMs": "1000"}}},
                     {"error": {"code": []}}):
            with self.subTest(wire=wire):
                transport = RestTransport("synthetic", _session=Session(Response(wire)))
                with self.assertRaises(RestRequestError):
                    list(transport.get_recognition("accepted-id", 1))
        for operation in ({"done": "false"}, {"done": True, "error": []}):
            with self.subTest(operation=operation):
                transport = RestTransport("synthetic", _session=Session(Response(operation)))
                with self.assertRaises(RestRequestError):
                    transport.get_operation("accepted-id", 1)

    def test_duplicate_gateway_fields_are_not_silently_overwritten(self):
        from yandex_speech.rest_stt import RestRequestError, RestTransport
        wire = b'{"result":{"final":{"alternatives":[{"text":"first","text":"second"}]}}}\n'
        transport = RestTransport("synthetic", _session=Session(Response(wire)))
        with self.assertRaises(RestRequestError):
            list(transport.get_recognition("accepted-id", 1))
        transport = RestTransport("synthetic", _session=Session(Response(b'{"id":"first","id":"second"}')))
        with self.assertRaises(RestRequestError):
            transport.recognize_file(b"{}", 1)
