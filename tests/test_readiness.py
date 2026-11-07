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
