import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from yandex_speech.config import RuntimeContext, resolve_settings
from yandex_speech.errors import SpeechError
from yandex_speech.tts_capabilities import Capability, RuntimeCapabilities, READY
from yandex_speech.tts_selection import plan_synthesis
from yandex_speech.tts_voices import resolve_voice

MISSING = Capability("absent", "synthetic missing interface")
RUNTIME = RuntimeCapabilities(READY, READY, READY, READY)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def options(self, **values):
        settings = resolve_settings(_context=RuntimeContext(user_home=Path(self.tmp.name)),
            _environment={}, _explicit={"tts": values})
        return resolve_voice(settings.value("tts"), settings.sources)

    def plan(self, text="Тест.", runtime=RUNTIME, stream=READY, joiner=lambda fmt: READY, **values):
        return plan_synthesis(text, self.options(**values), runtime=runtime,
            stream_contract=stream, joiner=joiner)

    def test_filter_before_rank_with_explicit_choices(self):
        cases = [
            ({}, "grpc-v3-stream"),
            ({"file_method": "utterance"}, "grpc-v3-utterance"),
            ({"transport": "rest"}, "rest-v3-utterance"),
            ({"api_version": "1"}, "rest-v1-utterance"),
            ({"api_version": "3"}, "grpc-v3-stream"),
            ({"unsafe_mode": True}, "grpc-v3-utterance"),
            ({"unsafe_mode": False}, "grpc-v3-stream"),
            ({"text_format": "ssml"}, "rest-v1-utterance"),
        ]
        for choices, expected in cases:
            with self.subTest(choices=choices):
                text = "<speak>Тест.</speak>" if choices.get("text_format") == "ssml" else "Тест."
                result = self.plan(text, **choices)
                self.assertEqual(result.candidate, expected)
                self.assertEqual("".join(result.parts), text)
                with self.assertRaises(FrozenInstanceError):
                    result.transport = "other"

    def test_partial_runtime_interfaces_select_independently(self):
        cases = [
            (RuntimeCapabilities(READY, MISSING, MISSING, MISSING), {}, "rest-v3-utterance"),
            (RuntimeCapabilities(READY, READY, READY, MISSING), {"transport": "grpc"}, "grpc-v3-utterance"),
            (RuntimeCapabilities(READY, READY, MISSING, READY), {}, "grpc-v3-stream"),
            (RuntimeCapabilities(READY, READY, MISSING, READY), {"file_method": "utterance"}, "rest-v3-utterance"),
            (RuntimeCapabilities(READY, READY, MISSING, READY), {"unsafe_mode": True}, "rest-v3-utterance"),
        ]
        for runtime, choices, expected in cases:
            with self.subTest(expected=expected, choices=choices):
                result = self.plan(runtime=runtime, **choices)
                self.assertEqual(result.candidate, expected)
                self.assertTrue(all(item.reason for item in result.exclusions))

    def test_unverified_collector_is_not_ready_even_with_sdk(self):
        result = self.plan(stream=Capability("unverified", "Alena stream framing unverified"))
        self.assertEqual(result.candidate, "grpc-v3-utterance")
        self.assertIn("unverified", result.exclusions[0].reason)
        with self.assertRaises(SpeechError) as caught:
            self.plan(stream=MISSING, file_method="stream")
        self.assertEqual(caught.exception.category, "PREREQUISITE")

    def test_missing_joiner_excludes_v3_parts_before_selecting_single_v1(self):
        result = self.plan("a" * 1000, stream=MISSING, joiner=lambda fmt: MISSING,
                           default_voice="ermil", role="good", default_format="oggopus")
        self.assertEqual(result.candidate, "rest-v1-utterance")
        self.assertEqual(len(result.parts), 1)
        for strict in ({"api_version": "3"}, {"transport": "grpc"}):
            with self.subTest(strict=strict), self.assertRaises(SpeechError) as caught:
                self.plan("a" * 1000, stream=MISSING, joiner=lambda fmt: MISSING, **strict)
            self.assertEqual(caught.exception.category, "PREREQUISITE")

    def test_length_boundaries_preserve_all_text_and_stream_aggregate(self):
        for length in (1, 249, 250, 251, 4999, 5000, 5001, 5500):
            for method, limit in (("stream", 5000), ("utterance", 250)):
                with self.subTest(length=length, method=method):
                    result = self.plan("я" * length, api_version="3", file_method=method)
                    self.assertEqual("".join(result.parts), "я" * length)
                    self.assertTrue(all(len(part) <= limit for part in result.parts))
                    self.assertEqual(len(result.parts), (length + limit - 1) // limit)

    def test_output_work_matters_for_stream_rpc_count_but_not_pcm_joiner(self):
        def joiner(fmt):
            return READY if fmt in {"wav", "lpcm"} else MISSING
        self.assertEqual(len(self.plan("a" * 5000, joiner=joiner).parts), 1)
        with self.assertRaises(SpeechError):
            self.plan("a" * 5001, file_method="stream", joiner=joiner)
        for fmt in ("wav", "lpcm"):
            self.assertEqual(len(self.plan("a" * 5001, default_format=fmt, joiner=joiner).parts), 2)

    def test_voice_classes_and_explicit_unsupported_controls(self):
        for voice in ("alena", "ermil", "alexander", "sergey"):
            self.assertEqual(self.plan(default_voice=voice).candidate, "grpc-v3-stream")
        # Hypothetical v1-only fixture: no maintained real voice is claimed as v1-only.
        options = self.options(default_voice="ermil")
        options["_voice_info"] = dict(options["_voice_info"], versions=frozenset({"1"}))
        self.assertEqual(plan_synthesis("Тест.", options, runtime=RUNTIME).candidate, "rest-v1-utterance")
        for values in ({"default_voice": "sergey", "text_format": "ssml"},
                       {"default_voice": "sergey", "default_speed": 1.1},
                       {"file_method": "stream", "duration_ms": 1000}):
            with self.subTest(values=values), self.assertRaises(SpeechError) as caught:
                self.plan(**values)
            self.assertEqual(caught.exception.category, "CONFIG")

    def test_base_failure_cannot_be_hidden_by_available_grpc(self):
        with self.assertRaises(SpeechError) as caught:
            self.plan(runtime=RuntimeCapabilities(MISSING, READY, READY, READY))
        self.assertEqual(caught.exception.category, "PREREQUISITE")
        self.assertIn("base TTS dependency", str(caught.exception))

    def test_indivisible_native_control_fails_without_losing_error(self):
        with self.assertRaises(SpeechError) as caught:
            self.plan("[[" + "я" * 6000 + "]]", file_method="stream")
        self.assertEqual(caught.exception.category, "LIMIT")
        self.assertTrue(any(item.category == "LIMIT" for item in caught.exception.exclusions))

    def test_request_snapshot_owns_identity_controls_provenance_and_scope(self):
        options = self.options(default_voice="ermil", role="good", default_format="lpcm",
            sample_rate_hertz=16000, default_speed=1.2, volume=-17)
        options["_voice_info"] = dict(options["_voice_info"])
        sources = {"tts.default_voice": "config", "tts.role": "call"}
        result = plan_synthesis("Тест.", options, runtime=RUNTIME, stream_contract=READY,
            sources=sources, config_path="/synthetic/profile/.config/yandex-speech/config.json",
            profile_scope="/synthetic/profile")
        before = result.request.as_options()
        options["default_voice"] = "sergey"
        options["_voice_info"]["default_role"] = "sales"
        sources["tts.role"] = "other"
        exported = result.request.as_options()
        exported["_voice_info"]["default_role"] = "other"
        self.assertEqual(result.request.as_options(), before)
        self.assertEqual(before["default_voice"], "ermil")
        self.assertEqual(before["_model"], "general")
        self.assertEqual(before["_role"], "good")
        self.assertEqual(before["sample_rate_hertz"], 16000)
        self.assertEqual(before["default_speed"], 1.2)
        self.assertEqual(before["volume"], -17)
        self.assertEqual(dict(result.request.sources)["tts.role"], "call")
        self.assertEqual(result.request.profile_scope, "/synthetic/profile")
        self.assertTrue(result.request.config_path.endswith("config.json"))

    def test_hypothetical_v1_only_general_is_not_forced_to_v3(self):
        options = self.options(default_voice="ermil", model="general")
        options["_voice_info"] = dict(options["_voice_info"], versions=frozenset({"1"}))
        result = plan_synthesis("Тест.", options, runtime=RUNTIME)
        self.assertEqual(result.candidate, "rest-v1-utterance")

    def test_invalid_control_is_config_before_any_dependency_probe(self):
        from unittest.mock import patch
        options = self.options(default_voice="sergey", default_speed=1.1)
        with patch("yandex_speech.tts_selection.probe_runtime") as probe:
            with self.assertRaises(SpeechError) as caught:
                plan_synthesis("Тест.", options)
        self.assertEqual(caught.exception.category, "CONFIG")
        probe.assert_not_called()
