import subprocess
import yandex_speech.tts_v3  # Load base imports before controlled import-module fixtures.
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from yandex_speech.tts_capabilities import (
    MESSAGE_FIELDS, MODULES, probe_joiner, probe_runtime,
)


def runtime_modules():
    messages = NS(**{name: type(name, (), {"DESCRIPTOR": NS(fields_by_name=dict.fromkeys(fields))})
                     for name, fields in MESSAGE_FIELDS.items()})
    service = NS(SynthesizerStub=lambda channel: NS(
        UtteranceSynthesis=channel.unary_stream("utterance"),
        StreamSynthesis=channel.stream_stream("stream")))
    return {
        "requests": NS(post=lambda: None),
        MODULES["grpc"]: NS(secure_channel=lambda: None, ssl_channel_credentials=lambda: None,
                            channel_ready_future=lambda: None),
        MODULES["protobuf"]: NS(ParseDict=lambda document, message: message),
        MODULES["messages"]: messages,
        MODULES["service"]: service,
    }


class RuntimeProbeTests(unittest.TestCase):
    def probe(self, modules):
        with patch("yandex_speech.tts_capabilities.importlib.import_module",
                   side_effect=lambda name: modules[name]):
            return probe_runtime()

    def test_working_generated_stubs_do_not_require_compiler(self):
        modules = runtime_modules()
        with patch("yandex_speech.tts_capabilities.importlib.import_module",
                   side_effect=lambda name: modules[name]) as imports:
            result = probe_runtime()
        self.assertTrue(all(getattr(result, name).ready for name in ("http", "grpc", "utterance", "stream")))
        self.assertFalse(any("grpc_tools" in call.args[0] for call in imports.call_args_list))

    def test_missing_and_broken_imports_are_distinct_and_http_survives(self):
        for module in MODULES.values():
            for missing in (True, False):
                with self.subTest(module=module, missing=missing):
                    modules = runtime_modules()
                    def load(name):
                        if name == module:
                            if missing:
                                raise ModuleNotFoundError(name, name=name)
                            raise RuntimeError("private sentinel")
                        return modules[name]
                    with patch("yandex_speech.tts_capabilities.importlib.import_module", side_effect=load):
                        result = probe_runtime()
                    self.assertTrue(result.http.ready)
                    self.assertEqual(result.grpc.status, "absent" if missing else "import-broken")
                    self.assertNotIn("private sentinel", result.grpc.reason)

    def test_base_dependency_failure_is_independent(self):
        modules = runtime_modules()
        def load(name):
            if name == "requests":
                raise ModuleNotFoundError(name, name=name)
            return modules[name]
        with patch("yandex_speech.tts_capabilities.importlib.import_module", side_effect=load):
            result = probe_runtime()
        self.assertEqual(result.http.status, "absent")
        self.assertTrue(result.stream.ready)

    def test_each_rpc_interface_is_checked_independently(self):
        for missing, ready in (("StreamSynthesis", "utterance"), ("UtteranceSynthesis", "stream")):
            with self.subTest(missing=missing):
                modules = runtime_modules()
                modules[MODULES["service"]] = NS(SynthesizerStub=lambda channel: NS(
                    **{method: lambda: None for method in ("StreamSynthesis", "UtteranceSynthesis")
                       if method != missing}))
                result = self.probe(modules)
                self.assertTrue(getattr(result, ready).ready)
                broken = result.stream if ready == "utterance" else result.utterance
                self.assertEqual(broken.status, "schema-incompatible")

    def test_missing_fields_or_parser_are_not_package_readiness(self):
        for target, name in (("messages", "SynthesisOptions"), ("protobuf", "ParseDict")):
            with self.subTest(target=target):
                modules = runtime_modules()
                delattr(modules[MODULES[target]], name)
                result = self.probe(modules)
                self.assertFalse(result.stream.ready)
                if target == "messages":
                    self.assertTrue(result.utterance.ready)

    def test_missing_force_message_disables_only_stream(self):
        modules = runtime_modules()
        if hasattr(modules[MODULES["messages"]], "ForceSynthesisEvent"):
            delattr(modules[MODULES["messages"]], "ForceSynthesisEvent")
        result = self.probe(modules)
        self.assertFalse(result.stream.ready)
        self.assertEqual(result.stream.status, "schema-incompatible")
        self.assertTrue(result.utterance.ready)

    def test_selected_nested_schema_is_validated_without_unselected_formats(self):
        from yandex_speech.config import DEFAULTS
        for broken in ("rawAudio", "hints", "streamVoice"):
            modules = runtime_modules()
            seen = []
            def parse(document, message):
                seen.append((type(message).__name__, document))
                if (broken == "rawAudio" and "rawAudio" in document.get("outputAudioSpec", {})
                        or broken == "hints" and "hints" in document
                        or broken == "streamVoice" and "voice" in document):
                    raise ValueError("synthetic incompatible selected field")
                return message
            modules[MODULES["protobuf"]] = NS(ParseDict=parse)
            with patch("yandex_speech.tts_capabilities.importlib.import_module",
                       side_effect=lambda name: modules[name]):
                mp3 = probe_runtime(dict(DEFAULTS["tts"], default_voice="ermil", default_format="mp3"))
                pcm = probe_runtime(dict(DEFAULTS["tts"], default_voice="ermil", default_format="lpcm"))
            with self.subTest(broken=broken):
                self.assertTrue(seen)
                if broken == "rawAudio":
                    self.assertTrue(mp3.utterance.ready and mp3.stream.ready)
                    self.assertFalse(pcm.utterance.ready or pcm.stream.ready)
                elif broken == "hints":
                    self.assertFalse(mp3.utterance.ready)
                    self.assertTrue(mp3.stream.ready)
                else:
                    self.assertTrue(mp3.utterance.ready)
                    self.assertFalse(mp3.stream.ready)

    def test_resolved_and_default_stream_roles_are_in_preflight_shape(self):
        from yandex_speech.config import DEFAULTS
        for configured in (True, False):
            modules = runtime_modules()
            seen = []
            def parse(document, message):
                seen.append(document)
                if "role" in document or any("role" in hint for hint in document.get("hints", [])):
                    raise ValueError("synthetic unsupported role field")
                return message
            modules[MODULES["protobuf"]] = NS(ParseDict=parse)
            options = dict(DEFAULTS["tts"], default_voice="ermil", role="auto",
                default_emotion="good", _role="good" if configured else None,
                _voice_info={"default_role": "neutral"})
            with patch("yandex_speech.tts_capabilities.importlib.import_module",
                       side_effect=lambda name: modules[name]):
                result = probe_runtime(options)
            with self.subTest(configured=configured):
                self.assertFalse(result.stream.ready)
                self.assertEqual(result.utterance.ready, not configured)
                self.assertEqual(seen[-1]["role"], "good" if configured else "neutral")


class JoinerProbeTests(unittest.TestCase):
    def test_pcm_assembly_has_no_external_dependency(self):
        with patch("yandex_speech.tts_capabilities.shutil.which") as which:
            for fmt in ("wav", "lpcm"):
                self.assertTrue(probe_joiner(fmt).ready)
        which.assert_not_called()

    def test_encoded_joiner_checks_demuxers_and_muxers_without_encoder(self):
        outputs = [" D concat concat\n D ogg Ogg\n D mp3 MP3\n", " E ogg Ogg\n E mp3 MP3\n"]
        with patch("yandex_speech.tts_capabilities.shutil.which", return_value="/synthetic/ffmpeg"), patch(
                "yandex_speech.tts_capabilities.subprocess.run",
                side_effect=[NS(returncode=0, stdout=s) for s in outputs]) as run:
            self.assertTrue(probe_joiner("oggopus").ready)
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["-demuxers", "-muxers"])

    def test_absent_unusable_and_missing_format_are_distinct(self):
        with patch("yandex_speech.tts_capabilities.shutil.which", return_value=None):
            self.assertEqual(probe_joiner("mp3").status, "absent")
        for response in (OSError(), subprocess.TimeoutExpired("ffmpeg", 5), NS(returncode=1), NS(returncode=0, stdout="")):
            with self.subTest(response=type(response).__name__), patch(
                    "yandex_speech.tts_capabilities.shutil.which", return_value="/synthetic/ffmpeg"), patch(
                    "yandex_speech.tts_capabilities.subprocess.run",
                    side_effect=response if isinstance(response, BaseException) else None,
                    return_value=response):
                self.assertEqual(probe_joiner("mp3").status, "executable-unusable")
