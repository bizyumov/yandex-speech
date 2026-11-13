import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError


class ReadinessTests(unittest.TestCase):
    def check(self, **kwargs):
        from yandex_speech.readiness import check
        with tempfile.TemporaryDirectory() as home:
            return check(_context=RuntimeContext(user_home=Path(home)), **kwargs)

    def test_missing_key_is_distinct_from_unverified_permissions(self):
        report = self.check(_environment={})
        self.assertEqual(report["authentication"]["status"], "missing")
        self.assertEqual(report["permissions"]["status"], "unverified")
        self.assertIn("ai.speechkit-stt.user", json.dumps(report))
        self.assertIn("yc.ai.speechkitStt.execute", json.dumps(report))

    def test_synthesis_reports_local_route_and_separate_unknown_host_sink(self):
        from yandex_speech.config import resolve_settings
        from yandex_speech.readiness import synthesis_readiness
        from tests.test_tts_selection import RUNTIME
        with tempfile.TemporaryDirectory() as home:
            settings = resolve_settings(_context=RuntimeContext(user_home=Path(home)), _environment={},
                _explicit={"tts": {"file_method": "utterance", "api_version": "3", "transport": "rest"}})
            with patch("yandex_speech.tts_capabilities.probe_runtime", return_value=RUNTIME):
                report = synthesis_readiness(settings, key_present=True, text="Тест.")
        self.assertTrue(report["file_ready"])
        self.assertFalse(report["pcm_stream_ready"])
        self.assertIsNone(report["host_sink_ready"])
        self.assertEqual(report["operation"]["transport"], "rest")
        self.assertEqual(report["operation"]["api_version"], "3")
        self.assertEqual(report["operation"]["method"], "utterance")
        self.assertEqual(report["permissions"], "unverified")

    def test_synthesis_automatic_plan_reports_local_execution_readiness(self):
        from tests.test_tts_selection import RUNTIME
        with patch("yandex_speech.tts_capabilities.probe_runtime", return_value=RUNTIME):
            report = self.check(_environment={"YANDEX_SPEECH_API_KEY": "synthetic"})
        self.assertTrue(report["synthesis"]["file_ready"])
        self.assertTrue(report["synthesis"]["automatic_routing_active"])
        self.assertEqual(report["synthesis"]["selection_status"], "automatic complete-file operation")
        self.assertEqual(report["synthesis"]["operation"]["method"], "stream")
        self.assertFalse(report["synthesis"]["host_sink_ready"])
        self.assertEqual(report["synthesis"]["permissions"], "unverified")

    def test_configured_wav_rate_readiness_matches_actual_sdk_route(self):
        from scripts.tts import TextToSpeech
        from yandex_speech.config import resolve_settings
        from yandex_speech.readiness import synthesis_readiness
        from yandex_speech.tts_audio import wrap_pcm
        from tests.test_tts_selection import RUNTIME
        for version in ("auto", "3"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                config = home / ".config/yandex-speech/config.json"
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps({"speech": {"tts": {"default_voice": "ermil",
                    "model": "general", "default_format": "wav", "sample_rate_hertz": 16000,
                    "api_version": version}}}))
                context = RuntimeContext(user_home=home)
                settings = resolve_settings(_context=context, _environment={})
                client = TextToSpeech(api_key="synthetic", _context=context, _environment={})
                with patch("yandex_speech.tts_capabilities.probe_runtime", return_value=RUNTIME), patch(
                        "yandex_speech.tts_selection.probe_runtime", return_value=RUNTIME), patch.object(
                        client, "_synthesize_request", return_value=wrap_pcm(b"\0\0" * 160, 16000)) as provider:
                    report = synthesis_readiness(settings, key_present=True, text="Тест.")
                    if version == "auto":
                        client.synthesize("Тест.", home / "configured.wav")
                        self.assertTrue(report["file_ready"])
                        self.assertEqual(report["operation"]["api_version"], client.last_synthesis["api_version"])
                        self.assertEqual(report["operation"]["transport"], client.last_synthesis["transport"])
                        self.assertEqual(report["operation"]["api_version"], "1")
                        self.assertEqual(provider.call_count, 1)
                    else:
                        with self.assertRaises(SpeechError) as caught:
                            client.synthesize("Тест.", home / "blocked.wav")
                        self.assertFalse(report["file_ready"])
                        self.assertEqual(report["error"]["category"], caught.exception.category)
                        self.assertEqual(report["error"]["category"], "CONFIG")
                        provider.assert_not_called()

    def test_synthesis_missing_key_never_claims_execution_readiness(self):
        report = self.check(_environment={})
        self.assertFalse(report["synthesis"]["file_ready"])
        self.assertFalse(report["synthesis"]["pcm_stream_ready"])
        self.assertEqual(report["synthesis"]["error"]["category"], "AUTH")

    def test_missing_or_broken_base_import_is_reported_before_collector_import(self):
        program = (
            "import importlib.abc,json,sys,tempfile\n"
            "from pathlib import Path\n"
            "class BrokenRequests(importlib.abc.MetaPathFinder):\n"
            " def find_spec(self,name,path=None,target=None):\n"
            "  if name == 'requests':\n"
            "   if sys.argv[1] == 'absent': raise ModuleNotFoundError(name,name=name)\n"
            "   raise RuntimeError('private import sentinel')\n"
            "sys.meta_path.insert(0,BrokenRequests())\n"
            "from yandex_speech.readiness import synthesis_readiness\n"
            "from yandex_speech.config import resolve_settings,RuntimeContext\n"
            "with tempfile.TemporaryDirectory() as directory:\n"
            " settings=resolve_settings(_context=RuntimeContext(user_home=Path(directory)),_environment={})\n"
            " result=synthesis_readiness(settings,key_present=True)\n"
            " assert 'yandex_speech.tts_stream_file' not in sys.modules\n"
            " print(json.dumps(result))\n")
        for status in ("absent", "import-broken"):
            with self.subTest(status=status):
                run = subprocess.run([sys.executable, "-c", program, status],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                report = json.loads(run.stdout)
                self.assertEqual(report["dependencies"]["http"]["status"], status)
                self.assertEqual(report["error"]["category"], "PREREQUISITE")
                self.assertFalse(report["file_ready"])
                self.assertNotIn("private import sentinel", run.stdout)

    def test_ready_options_describe_delivered_partitions_and_account_setup(self):
        report = self.check(_environment={})
        option = next(x for x in report["options"] if x["route"] == "over-limit async partitioning")
        self.assertEqual(option["status"], "implemented")
        self.assertIn("prepare-key", report["permissions"]["remedy"])

    def test_key_presence_never_claims_permission_or_leaks_secret(self):
        report = self.check(_environment={"YANDEX_SPEECH_API_KEY": "sentinel-secret-783"})
        text = json.dumps(report)
        self.assertNotIn("sentinel-secret-783", text)
        self.assertEqual(report["authentication"]["status"], "present-unverified")
        self.assertEqual(report["permissions"]["status"], "unverified")
        self.assertEqual(report["effective"]["literature_text"], True)
        self.assertEqual(report["effective_route"]["api_version"], "3")

    def test_explicit_probe_denial_does_not_invent_missing_role(self):
        def probe():
            raise SpeechError("PERMISSION", "provider denied sentinel-secret-783")
        report = self.check(_environment={"YANDEX_SPEECH_API_KEY":"sentinel-secret-783"},
                            _probe=probe)
        self.assertEqual(report["permissions"]["status"], "denied-cause-unverified")
        self.assertNotIn("sentinel-secret-783", json.dumps(report))

    def test_actual_success_is_execution_evidence_not_iam_inventory(self):
        report = self.check(_environment={"YANDEX_SPEECH_API_KEY":"synthetic"},
                            _probe=lambda: {"input_complete":True})
        self.assertEqual(report["permissions"]["status"], "execution-verified")

    def test_transcription_probe_does_not_certify_synthesis_permission(self):
        report=self.check(_environment={"YANDEX_SPEECH_API_KEY":"synthetic"},
                          _probe=lambda:{"input_complete":True})
        self.assertEqual(report["permissions"]["verified_operation"],"speech-transcription")
        self.assertEqual(report["permissions"]["tts_execution_status"],"unverified")

    def test_selected_version_one_reports_rest_not_grpc_as_effective_route(self):
        from yandex_speech.readiness import check
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/".config/yandex-speech/config.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"speech":{"api_version":"1"}}')
            report=check(_context=RuntimeContext(user_home=root),_environment={})
        self.assertEqual(report["effective_route"]["api_version"],"1")
        self.assertEqual(report["effective_route"]["transport"],"rest")
        self.assertTrue(report["effective_route"]["available"])

    def test_missing_optional_grpc_selects_rest_recording_without_claiming_live_access(self):
        with patch("yandex_speech.readiness._available", side_effect=lambda module: module == "requests"):
            report = self.check(_environment={"YANDEX_SPEECH_API_KEY": "synthetic"})
        self.assertEqual(report["effective_route"]["transport"], "rest")
        self.assertTrue(report["effective_route"]["available"])
        self.assertTrue(report["effective_route"]["inputs"]["inline"])
        self.assertTrue(report["effective_route"]["inputs"]["caller_uri"])
        self.assertFalse(report["effective_route"]["inputs"]["helper_upload"])
        self.assertFalse(report["effective_route"]["streaming_available"])
        self.assertEqual(report["permissions"]["status"], "unverified")

    def test_rest_uri_readiness_does_not_require_boto_or_upload_credentials(self):
        from yandex_speech.readiness import check
        for version in ("2", "3"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / ".config/yandex-speech/config.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"speech": {"api_version": version, "transport": "rest",
                    "object_storage": {"bucket": "configured-private"}}}))
                with patch("yandex_speech.readiness._available", side_effect=lambda module: module == "requests"):
                    report = check(_context=RuntimeContext(user_home=root), _environment={})
                self.assertTrue(report["effective_route"]["available"])
                self.assertTrue(report["effective_route"]["inputs"]["caller_uri"])
                self.assertEqual(report["effective_route"]["inputs"]["inline"], version == "3")
                self.assertFalse(report["effective_route"]["inputs"]["helper_upload"])
                self.assertFalse(report["effective_route"]["streaming_available"])

    def test_diarization_excludes_arriving_streaming_in_readiness(self):
        from yandex_speech.readiness import check
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".config/yandex-speech/config.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"speech":{"enable_diarization":true}}')
            with patch("yandex_speech.readiness._available", return_value=True):
                report = check(_context=RuntimeContext(user_home=root), _environment={})
            self.assertTrue(report["effective_route"]["available"])
            self.assertFalse(report["effective_route"]["streaming_available"])

    def test_helper_runs_outside_project_without_key(self):
        script = Path("scripts/speech_setup.py").resolve()
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable,str(script),"--check"], cwd=home,
                env={"HOME":home,"PATH":"/usr/bin:/bin"},capture_output=True,text=True)
        self.assertEqual(r.returncode,2,r.stderr)
        self.assertEqual(json.loads(r.stdout)["authentication"]["status"],"missing")
