"""Stock Hermes setup uses its native CLI; the skill only checks readiness."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from yandex_speech.readiness import main


def exercise_native_setup(case, plugin, pythonpath, outside):
    """Admit an installed payload and configure two isolated profiles without a provider call."""
    profile = outside / "native-profile"
    profile.mkdir()
    other = outside / "other-profile"
    other.mkdir()
    config = profile / "config.yaml"
    config.write_text(
        "# retain operator comment\nmodel:\n  default: unrelated-model\n"
        "stt:\n  provider: old\ntts:\n  provider: old\n"
        "  voice: ermil\n  yandex:\n    role: good\n", encoding="utf-8")
    secret = profile / ".env"
    secret.write_text("TEST_ONLY_SECRET=synthetic-owned-value\n", encoding="utf-8")
    other_config = other / "config.yaml"
    other_config.write_text("stt:\n  provider: other\n", encoding="utf-8")
    other_secret = other / ".env"
    other_secret.write_text("TEST_ONLY_SECRET=synthetic-other-value\n", encoding="utf-8")
    preserved = {p: p.read_bytes() for p in (secret, other_config, other_secret)}
    plugin_link = profile / "plugins" / "yandex-speech"
    plugin_link.parent.mkdir()
    plugin_link.symlink_to(plugin.resolve(), target_is_directory=True)
    env = {k: os.environ[k] for k in ("PATH", "VIRTUAL_ENV", "LANG") if k in os.environ}
    env.update(HOME=str(outside), HERMES_HOME=str(profile), PYTHONPATH=str(pythonpath),
               YANDEX_SPEECH_API_KEY="synthetic-offline-only", NO_COLOR="1")

    def run(*args):
        result = subprocess.run([sys.executable, "-m", "hermes_cli.main", *args],
            cwd=outside, env=env, capture_output=True, text=True, timeout=60)
        case.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-5000:])

    run("plugins", "enable", "yandex-speech", "--no-allow-tool-override")
    commands = [
        ("config", "set", "stt.provider", "yandex"),
        ("config", "set", "stt.streaming", "true"),
        ("config", "set", "stt.yandex.language", "ru-RU"),
        ("config", "set", "tts.provider", "yandex"),
    ]
    for command in commands:
        run(*command)

    program = """
import inspect,json,sys
from pathlib import Path
from hermes_cli.plugins import discover_plugins
from hermes_cli.config import load_config_readonly
from agent import transcription_registry,tts_registry
from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider
import yandex_speech
assert Path(yandex_speech.__file__).resolve().is_relative_to(Path(sys.argv[2]).resolve())
discover_plugins()
for registry,base in ((transcription_registry,TranscriptionProvider),(tts_registry,TTSProvider)):
    provider=registry.get_provider("yandex")
    assert provider is not None
    cls=provider if isinstance(provider,type) else type(provider)
    assert issubclass(cls,base)
    assert Path(inspect.getfile(cls)).resolve().is_relative_to(Path(sys.argv[1]).resolve())
config=load_config_readonly()
assert config["stt"]["provider"]=="yandex" and config["stt"]["streaming"] is True
assert config["stt"]["yandex"]["language"]=="ru-RU"
assert config["tts"]["provider"]=="yandex" and config["tts"]["voice"]=="ermil"
assert config["tts"]["yandex"]["role"]=="good"
assert config["model"]["default"]=="unrelated-model"
from tools.registry import registry as tool_registry
ssml=tool_registry.get_entry("yandex_speech_ssml")
assert ssml is not None
assert Path(inspect.getfile(ssml.handler)).resolve().is_relative_to(Path(sys.argv[1]).resolve())
print(json.dumps({"stt_registered":True,"tts_registered":True,"ssml_registered":True,"settings_verified":True}))
"""
    result = subprocess.run([sys.executable, "-c", program, str(plugin.resolve()), str(pythonpath)],
        cwd=outside, env=env, capture_output=True, text=True, timeout=60)
    case.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-5000:])
    case.assertEqual(json.loads(result.stdout),
        {"stt_registered": True, "tts_registered": True, "ssml_registered": True, "settings_verified": True})
    before_repeat = config.read_bytes()
    for command in commands:
        run(*command)
    case.assertEqual(config.read_bytes(), before_repeat)
    case.assertIn("# retain operator comment", config.read_text())
    for path, content in preserved.items():
        case.assertEqual(path.read_bytes(), content)
    case.assertEqual(plugin_link.resolve(), plugin.resolve())


class ReadinessCliTests(unittest.TestCase):
    def test_help_exposes_readiness_without_cancelled_enrollment(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        for option in ("--check", "--config", "--probe-audio"):
            self.assertIn(option, output.getvalue())
        for option in ("--hermes-plan", "--hermes-apply",
                       "--enable-stt-streaming", "--enable-tts-pcm"):
            self.assertNotIn(option, output.getvalue())

    def test_cancelled_writer_options_fail_before_readiness_or_probe(self):
        for option in ("--hermes-plan", "--hermes-apply",
                       "--enable-stt-streaming", "--enable-tts-pcm"):
            with self.subTest(option=option), patch("yandex_speech.readiness.check") as check:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main([option])
                self.assertEqual(caught.exception.code, 2)
                check.assert_not_called()


class NativeCliSetupTests(unittest.TestCase):
    def test_stock_cli_admits_plugin_and_preserves_profile_boundaries(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="speech-native-cli-") as directory:
            exercise_native_setup(self, root / "integrations/hermes", root, Path(directory))

    def test_native_cli_migrates_known_literal_preferences_without_an_enrollment_writer(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="speech-cli-migration-") as temporary:
            home = Path(temporary)
            profile = home / "profile"
            profile.mkdir()
            config = profile / "config.yaml"
            known = "python " + str(root / "scripts/tts.py") + " --input-file {input_path} --output {output_path} --voice ermil --emotion good"
            config.write_text("# retain migration comment\n" + json.dumps({
                "model": {"default": "unrelated-model"},
                "tts": {"providers": {"yandex": {"type": "command", "command": known},
                                     "other": {"type": "command", "command": "do-not-touch"}}}}))
            secret = profile / ".env"
            secret.write_text("TEST_ONLY_SECRET=synthetic-preserved\n")
            before_secret = secret.read_bytes()
            env = {k: os.environ[k] for k in ("PATH", "VIRTUAL_ENV", "LANG") if k in os.environ}
            env.update(HOME=str(home), HERMES_HOME=str(profile))
            commands = [
                ("config", "set", "tts.voice", "ermil", "--force"),
                ("config", "set", "tts.yandex.role", "good", "--force"),
                ("config", "set", "tts.yandex.language", "ru-RU", "--force"),
                ("config", "unset", "tts.providers.yandex"),
                ("config", "set", "tts.provider", "yandex"),
            ]
            for args in commands:
                result = subprocess.run([sys.executable, "-m", "hermes_cli.main", *args],
                    cwd=home, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-2500:])
            from hermes_cli.config import fast_safe_load
            selected = fast_safe_load(config.read_text())
            self.assertEqual(selected["tts"]["voice"], "ermil")
            self.assertEqual(selected["tts"]["yandex"]["role"], "good")
            self.assertEqual(selected["tts"]["yandex"]["language"], "ru-RU")
            self.assertNotIn("yandex", selected["tts"]["providers"])
            self.assertEqual(selected["tts"]["providers"]["other"]["command"], "do-not-touch")
            self.assertEqual(selected["model"]["default"], "unrelated-model")
            self.assertEqual(secret.read_bytes(), before_secret)
            self.assertIn("# retain migration comment", config.read_text())
