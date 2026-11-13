"""Complete-file SDK dispatch; provider simulation is never native commissioning."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tts import TextToSpeech
from tests.test_tts_audio import encoded_tone, decoded_pcm, tone_strength
from tests.test_tts_selection import RUNTIME
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.tts_stream_file import CollectedAudio


class FileDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first, cls.tail = encoded_tone(440), encoded_tone(880)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = TextToSpeech(api_key="synthetic",
            _context=RuntimeContext(user_home=self.root))

    def test_explicit_route_length_matrix_preserves_text_identity_and_tail(self):
        routes = [("1", "rest", "utterance", False), ("3", "rest", "utterance", False),
                  ("3", "grpc", "utterance", False), ("3", "rest", "utterance", True),
                  ("3", "grpc", "utterance", True), ("3", "grpc", "stream", False)]
        for length in (200, 250, 1000, 5000, 5500):
            for version, transport, method, unsafe in routes:
                with self.subTest(length=length, api=version, transport=transport, method=method, unsafe=unsafe):
                    calls = []
                    text = "a" * length
                    def utterance(piece, settings, options, selected_version, **kwargs):
                        calls.append((piece, dict(options), selected_version))
                        return self.tail if sum(len(call[0]) for call in calls) == length else self.first
                    def stream(piece, options, **kwargs):
                        data = utterance(piece, None, options, "3")
                        return CollectedAudio(data, 3, 2, .01, .02)
                    target = self.root / ("case-" + str(length) + "-" + version + transport + method + str(unsafe) + ".ogg")
                    with patch("yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME), patch.object(
                            self.client, "_synthesize_request", side_effect=utterance), patch(
                            "yandex_speech.tts_stream_file.collect_stream_file", side_effect=stream):
                        self.client.synthesize(text, target, file_method=method, api_version=version,
                            transport=transport, unsafe_mode=unsafe, voice="ermil", role="good", fmt="oggopus")
                    self.assertEqual("".join(call[0] for call in calls), text)
                    self.assertTrue(all(call[1]["default_voice"] == "ermil" and call[1]["_role"] == "good"
                                        and call[1]["_model"] == "general" for call in calls))
                    self.assertEqual(self.client.last_synthesis["method"], method)
                    self.assertEqual(self.client.last_synthesis["api_version"], version)
                    self.assertEqual(self.client.last_synthesis["transport"], transport)
                    self.assertEqual(self.client.last_synthesis["input_characters"], length)
                    self.assertEqual(sum(self.client.last_synthesis["submitted_part_lengths"]), length)
                    self.assertEqual(self.client.last_synthesis["rpc_calls"], len(calls))
                    self.assertIs(self.client.last_synthesis["unsafe_mode"], unsafe)
                    if method == "stream":
                        self.assertEqual(len(calls), (length + 4999) // 5000)
                        self.assertTrue(all(len(call[0]) <= 5000 for call in calls))
                    pcm = decoded_pcm(target.read_bytes())
                    tail = pcm[-7200:-2400]
                    self.assertGreater(tone_strength(tail, 880), 10 * tone_strength(tail, 440))

    def test_failed_middle_part_keeps_prior_owned_audio_without_publishing_prefix(self):
        target = self.root / "failed.ogg"
        with patch("yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME), patch.object(
                self.client, "_synthesize_request",
                side_effect=[self.first, SpeechError("SUBMISSION_UNCERTAIN", "synthetic middle failure")]) as provider:
            with self.assertRaises(SpeechError) as caught:
                self.client.synthesize("a" * 600, target, file_method="utterance",
                    api_version="3", transport="rest", fmt="oggopus")
        self.assertFalse(target.exists())
        self.assertEqual(provider.call_count, 2)
        self.assertEqual((caught.exception.partial_dir / "part-000000.ogg").read_bytes(), self.first)
        self.assertEqual(self.client.last_synthesis["status"], "incomplete")

    def test_default_automatic_wiring_waits_for_native_comparison(self):
        with patch.object(self.client, "_synthesize_legacy", return_value=["legacy"]) as legacy:
            self.assertEqual(self.client.synthesize("Тест.", self.root / "auto.mp3"), ["legacy"])
        legacy.assert_called_once()

    def test_stream_bytes_uses_real_file_method_contract_without_utterance_fallback(self):
        with patch("yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME), patch(
                "yandex_speech.tts_stream_file.collect_stream_file",
                return_value=CollectedAudio(self.first, 2, 2, .01, .02)) as stream, patch.object(
                self.client, "_synthesize_request") as utterance:
            self.assertEqual(self.client.synthesize_bytes("a" * 1000,
                file_method="stream", api_version="3", transport="grpc", fmt="oggopus"), self.first)
        self.assertEqual(stream.call_count, 1)
        utterance.assert_not_called()

    def test_v1_submitted_http_errors_are_never_automatically_replayed(self):
        from types import SimpleNamespace
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status), patch("scripts.tts.requests.post",
                    return_value=SimpleNamespace(status_code=status, headers={}, content=b"")) as provider:
                with self.assertRaises(SpeechError):
                    self.client.synthesize("Тест.", self.root / ("error-" + str(status) + ".mp3"),
                        file_method="utterance", api_version="1", transport="rest")
                self.assertEqual(provider.call_count, 1)

    def test_invalid_output_parent_fails_before_provider_submission(self):
        parent = self.root / "regular-file"
        parent.write_bytes(b"preserve")
        with patch.object(self.client, "_synthesize_request") as provider:
            with self.assertRaises(SpeechError) as caught:
                self.client.synthesize("Тест.", parent / "out.mp3",
                    file_method="utterance", api_version="1")
        self.assertEqual(caught.exception.category, "CONFIG")
        provider.assert_not_called()
        self.assertEqual(parent.read_bytes(), b"preserve")

    def test_aggregate_audio_budget_preserves_the_part_crossing_limit(self):
        output = self.root / "budget.ogg"
        with patch("scripts.tts.MAX_SYNTHESIS_BYTES", len(self.first) + len(self.tail) - 1), patch(
                "yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME), patch.object(
                self.client, "_synthesize_request", side_effect=[self.first, self.tail]) as provider:
            with self.assertRaises(SpeechError) as caught:
                self.client.synthesize("a" * 600, output, file_method="utterance",
                    api_version="3", transport="rest", fmt="oggopus")
        self.assertEqual(caught.exception.category, "LIMIT")
        self.assertEqual(provider.call_count, 2)
        self.assertFalse(output.exists())
        directory = caught.exception.partial_dir
        self.assertEqual((directory / "part-000000.ogg").read_bytes(), self.first)
        self.assertEqual((directory / "part-000001.ogg").read_bytes(), self.tail)

    def test_progressive_operation_rejects_explicit_utterance_method_sdk_and_cli(self):
        import contextlib
        import io
        from scripts.tts import main
        with patch("yandex_speech.tts_stream.GrpcStreamingTransport") as transport:
            with self.assertRaises(SpeechError) as caught:
                self.client.stream_audio("Тест.", file_method="utterance")
        self.assertEqual(caught.exception.category, "CONFIG")
        transport.assert_not_called()
        with patch("sys.argv", ["tts", "Тест.", "--output", str(self.root / "out.pcm"),
                              "--stream", "--file-method", "utterance"]), patch(
                "scripts.tts.TextToSpeech") as client, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cli_error:
                main()
        self.assertEqual(cli_error.exception.code, 2)
        client.assert_not_called()

    def test_duration_rejection_does_not_reopen_cross_transport_fallback(self):
        duration = SpeechError("LIMIT", "synthetic duration rejection", code="UTTERANCE_LIMIT")
        connection = SpeechError("TRANSPORT", "synthetic later connection", code="GRPC_UNREACHABLE")
        with patch("yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME), patch.object(
                self.client, "_synthesize_request", side_effect=[duration, connection]) as provider:
            with self.assertRaises(SpeechError) as caught:
                self.client.synthesize("a" * 200, self.root / "no-replay.pcm",
                    file_method="utterance", api_version="3", fmt="lpcm")
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(caught.exception.code, "GRPC_UNREACHABLE")
        self.assertTrue(all(call.args[2]["transport"] == "grpc" for call in provider.call_args_list))

    def test_public_file_path_accepts_hypothetical_v1_only_general(self):
        from types import SimpleNamespace
        from yandex_speech.tts_voices import CATALOG
        hypothetical = dict(CATALOG["ermil"], versions=frozenset({"1"}))
        with patch.dict(CATALOG, {"hypothetical-v1-only": hypothetical}), patch(
                "scripts.tts.requests.post",
                return_value=SimpleNamespace(status_code=200, headers={"Content-Type": "audio/L16"}, content=b"\0\0")) as post:
            self.client.synthesize("Тест.", self.root / "v1-only.pcm",
                voice="hypothetical-v1-only", model="general", file_method="utterance", fmt="lpcm")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.client.last_synthesis["api_version"], "1")

    def test_explicit_wav_rate_excludes_incapable_v3_candidates(self):
        from yandex_speech.tts_audio import wrap_pcm
        from types import SimpleNamespace
        data = b"\0\0" * 160
        with patch("scripts.tts.requests.post",
                return_value=SimpleNamespace(status_code=200, headers={"Content-Type": "audio/L16"}, content=data)) as post:
            self.client.synthesize("Тест.", self.root / "rate.wav",
                file_method="utterance", voice="ermil", model="general", fmt="wav", sample_rate=16000)
        self.assertEqual(self.client.last_synthesis["api_version"], "1")
        self.assertEqual(post.call_args.kwargs["data"]["sampleRateHertz"], "16000")
        with patch("scripts.tts.requests.post") as post:
            with self.assertRaises(SpeechError):
                self.client.synthesize("Тест.", self.root / "blocked.wav",
                    file_method="utterance", voice="sergey", fmt="wav", sample_rate=16000)
        post.assert_not_called()

    def test_selected_configuration_wav_rate_is_preserved(self):
        import json
        from types import SimpleNamespace
        path = self.root / ".config/yandex-speech/config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"speech": {"tts": {"default_format": "wav",
            "sample_rate_hertz": 16000, "model": "general", "file_method": "utterance"}}}))
        with patch("scripts.tts.requests.post", return_value=SimpleNamespace(
                status_code=200, headers={"Content-Type": "audio/L16"}, content=b"\0\0" * 160)) as post:
            self.client.synthesize("Тест.", self.root / "configured.wav")
        self.assertEqual(self.client.last_synthesis["api_version"], "1")
        self.assertEqual(post.call_args.kwargs["data"]["sampleRateHertz"], "16000")

    def test_duration_rejection_keeps_all_submitted_attempts_separate_from_output(self):
        rejected = SpeechError("LIMIT", "synthetic duration", code="UTTERANCE_LIMIT")
        with patch("yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME), patch.object(
                self.client, "_synthesize_request", side_effect=[rejected, b"\0\0" * 100, b"\0\0" * 100]):
            self.client.synthesize("a" * 200, self.root / "split.pcm",
                file_method="utterance", api_version="3", transport="grpc", fmt="lpcm")
        self.assertEqual(self.client.last_synthesis["submitted_part_lengths"], [200, 100, 100])
        self.assertEqual(self.client.last_synthesis["output_part_lengths"], [100, 100])
        self.assertEqual(self.client.last_synthesis["rpc_calls"], 3)
        self.assertEqual(self.client.last_synthesis["input_characters"], 200)
        self.assertEqual([row["outcome"] for row in self.client.last_synthesis["request_attempts"]],
                         ["rejected-duration", "complete", "complete"])
