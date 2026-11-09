"""Opt-in registered SSML tool acceptance in the actual owning Hermes profile."""
import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import requests


@unittest.skipUnless(
    os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1"
    and os.environ.get("YANDEX_SPEECH_HOST_TESTS") == "1",
    "explicit provider and owning-profile host opt-in required")
class LiveHermesSSMLTests(unittest.TestCase):
    def test_registered_ssml_tool_preserves_markup_and_voice_media(self):
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins import get_plugin_manager
        from tools.registry import registry
        from gateway.platforms.base import BasePlatformAdapter

        profile = get_hermes_home().resolve()
        self.assertEqual(profile, Path(os.environ["HERMES_HOME"]).resolve())
        self.assertEqual(os.geteuid(), profile.stat().st_uid)
        self.assertEqual(Path(sys.prefix).resolve(), Path(os.environ["VIRTUAL_ENV"]).resolve())
        watched = [path for path in (profile / "config.yaml", profile / ".env",
                   profile / ".config/yandex-speech/config.json") if path.is_file()]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in watched}
        config = load_config_readonly()
        for kind in ("stt", "tts"):
            self.assertEqual(config.get(kind, {}).get("provider"), "yandex")
            override = config.get(kind, {}).get("providers", {}).get("yandex") or {}
            self.assertNotEqual(override.get("type"), "command")
        manager = get_plugin_manager()
        manager.discover_and_load()
        entry = registry.get_entry("yandex_speech_ssml", scope=str(profile))
        self.assertIsNotNone(entry)
        import inspect
        import yandex_speech
        candidate = Path(__file__).resolve().parents[2]
        bindings = {}
        actual_files = {
            "integrations/hermes/ssml_tool.py": Path(inspect.getfile(entry.handler)).resolve(),
            "integrations/hermes/tts_provider.py":
                Path(inspect.getfile(type(entry.handler.__self__.provider))).resolve(),
            "scripts/tts.py": Path(inspect.getfile(entry.handler.__self__.provider._client_factory)).resolve(),
        }
        package = Path(yandex_speech.__file__).resolve().parent
        for expected in (candidate / "yandex_speech").rglob("*.py"):
            relative = str(expected.relative_to(candidate))
            actual_files[relative] = package / expected.relative_to(candidate / "yandex_speech")
        for relative, actual in actual_files.items():
            expected = candidate / relative
            self.assertTrue(actual.is_file(), "registered candidate module is missing")
            actual_digest = hashlib.sha256(actual.read_bytes()).hexdigest()
            self.assertEqual(actual_digest, hashlib.sha256(expected.read_bytes()).hexdigest(),
                             "discovered plugin/core source differs from candidate")
            bindings[relative] = actual_digest
        text = '<speak>Изучаем <sub alias="рэст">REST</sub>. Слово <phoneme alphabet="ipa" ph="məlɐko">молоко</phoneme>.</speak>'
        observed = []
        real_post = requests.post

        def observe(*args, **kwargs):
            url = args[0] if args else kwargs.get("url", "")
            if "/speech/v1/tts:synthesize" in str(url):
                data = kwargs.get("data", {})
                self.assertIn("ssml", data)
                self.assertNotIn("text", data)
                self.assertNotIn("folderId", data)
                root = ET.fromstring(data["ssml"])
                self.assertEqual(root.find("sub").attrib["alias"], "рэст")
                self.assertEqual(root.find("phoneme").attrib["ph"], "məlɐko")
                observed.append({"ssml_sha256": hashlib.sha256(data["ssml"].encode()).hexdigest(),
                                 "aliases_and_phonemes_preserved": True,
                                 "uses_ssml_field": True, "text_field_absent": True})
            return real_post(*args, **kwargs)

        started = time.monotonic()
        with patch("scripts.tts.requests.post", side_effect=observe):
            result = json.loads(registry.dispatch("yandex_speech_ssml", {
                "ssml": text, "voice": "ermil", "role": "good",
                "language": "ru-RU", "model": "general"}, scope=str(profile)))
        seconds = time.monotonic() - started
        self.assertTrue(result.get("success"), "registered SSML tool failed")
        self.assertEqual(len(observed), 1)
        self.assertEqual(result["delivery_status"], "awaiting_host_delivery")
        output = Path(result["file_path"])
        self.assertTrue(output.is_relative_to(profile) and output.is_file())
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
            "format=duration,format_name:stream=codec_name", "-of", "json", str(output)],
            capture_output=True, text=True, check=True)
        audio = json.loads(probe.stdout)
        self.assertEqual(audio["streams"][0]["codec_name"], "opus")
        self.assertGreater(float(audio["format"]["duration"]), 0)
        media, remaining = BasePlatformAdapter.extract_media(result["media_tag"])
        self.assertEqual(media, [(str(output), True)])
        self.assertEqual(remaining, "")
        self.assertEqual(BasePlatformAdapter.extract_media(json.dumps(result))[0], [])
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest,
                             "owning-profile configuration or credentials changed")
        destination = Path(os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR", output.parent))
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        proof = destination / ("ssml-host-" + uuid.uuid4().hex + ".json")
        with proof.open("x") as stream:
            json.dump({"case": self.id(), "registered_tool": "yandex_speech_ssml",
                "profile_owner_uid": os.geteuid(), "synthesis_seconds": seconds,
                "observations": observed, "audio": audio, "result": result,
                "candidate_source_bindings": bindings,
                "owning_profile_readonly": True, "generic_final_voice_protocol": True,
                "actual_telegram_delivery": False, "heard": False}, stream,
                ensure_ascii=False, indent=2)
