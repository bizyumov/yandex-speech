import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from yandex_speech.errors import SpeechError
from yandex_speech.hermes_setup import _NativeBackend, plan_native


class LocalProvider:
    streaming_capable = True
    streams_pcm = True
    available = True

    def __init__(self, workspace, profile=None):
        self.workspace, self.profile = workspace, profile

    def is_available(self):
        return self.available


class NativeSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = self.home / "config.yaml"
        self.config.write_text("# retain operator comment\nmodel:\n  default: unrelated-model\nstt:\n  provider: old\ntts:\n  provider: old\n")
        self.secret = self.home / ".env"
        self.secret.write_text("TEST_ONLY_SECRET=synthetic-owned-value\n")
        with patch("hermes_constants.get_hermes_home", return_value=self.home):
            self.backend = _NativeBackend()
        self.root = Path(__file__).resolve().parents[1]
        self.plugin = self.root / "integrations/hermes"

    def plan(self, **kwargs):
        return plan_native(_backend=self.backend,
                           _providers=(LocalProvider, LocalProvider),
                           _plugin=self.plugin, **kwargs)

    def write(self, value):
        self.backend.write(value)

    def admit(self):
        """Fixture an existing native installation; setup does not own dependency admission."""
        data = self.backend.read()
        data.setdefault("plugins", {})["enabled"] = ["yandex-speech"]
        self.write(data)
        directory = self.home / "plugins"
        directory.mkdir(exist_ok=True)
        if not (directory / "yandex-speech").exists():
            (directory / "yandex-speech").symlink_to(self.plugin, target_is_directory=True)

    def test_plan_is_read_only_and_does_not_dump_unrelated_config(self):
        value = self.backend.read()
        value["private_operational_field"] = "synthetic-private-payload"
        self.write(value)
        before = self.config.read_bytes(), self.secret.read_bytes()
        report = self.plan().summary()
        self.assertEqual((self.config.read_bytes(), self.secret.read_bytes()), before)
        self.assertFalse((self.home / "plugins").exists())
        self.assertNotIn("synthetic-private-payload", json.dumps(report))
        self.assertTrue(report["changed"])

    def test_apply_twice_preserves_unrelated_values_comments_and_credentials(self):
        self.admit()
        secret = self.secret.read_bytes()
        first = self.plan().apply()
        data = self.backend.read()
        self.assertTrue(first["changed"])
        self.assertEqual(data["model"]["default"], "unrelated-model")
        self.assertEqual((data["stt"]["provider"], data["tts"]["provider"]), ("yandex", "yandex"))
        self.assertIn("# retain operator comment", self.config.read_text())
        self.assertEqual(self.secret.read_bytes(), secret)
        before = self.config.read_bytes()
        self.assertFalse(self.plan().apply()["changed"])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual((self.home / "plugins/yandex-speech").resolve(), self.plugin.resolve())

    def test_legacy_migration_is_previewed_and_refused_without_commands(self):
        self.admit()
        data = self.backend.read()
        data["stt"]["providers"] = {"yandex": {
            "type": "command", "command": str(self.root / "scripts/hermes-stt-wrapper.sh") +
            " {input_path} {output_dir} {language}", "language": "ru-RU"}}
        data["tts"]["providers"] = {"yandex": {
            "type": "command", "command": "env HERMES_TTS_VOICE=ermil HERMES_TTS_LANG=ru-RU " +
            str(self.root / "scripts/hermes-tts-wrapper.sh") + " {input_path} {output_path}",
            "output_format": "ogg", "voice_compatible": True}}
        self.write(data)
        before = self.config.read_bytes()
        with patch("subprocess.run", side_effect=AssertionError("setup must not execute commands")):
            plan = self.plan()
            self.assertNotIn("HERMES_TTS_VOICE", json.dumps(plan.summary()))
            self.assertFalse(plan.summary()["safe_apply_available"])
            with self.assertRaises(SpeechError) as raised:
                plan.apply()
        self.assertEqual(raised.exception.category, "PREREQUISITE")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(plan.after["tts"]["voice"], "ermil")
        self.assertEqual(plan.after["tts"]["yandex"]["language"], "ru-RU")
        self.assertEqual(plan.after["stt"]["yandex"]["language"], "ru-RU")

    def test_unknown_command_or_conflicting_preferences_fail_without_mutation(self):
        for command, preference in [
            ("unknown-speech-provider {input_path}", {}),
            ("env HERMES_TTS_VOICE=ermil " +
             str(self.root / "scripts/hermes-tts-wrapper.sh") + " {input_path} {output_path}",
             {"voice": "jane"})]:
            data = self.backend.read()
            data["tts"].update(preference)
            data["tts"]["providers"] = {"yandex": {"type": "command", "command": command}}
            self.write(data)
            before = self.config.read_bytes()
            with self.assertRaises(SpeechError):
                self.plan()
            self.assertEqual(self.config.read_bytes(), before)
            self.assertFalse((self.home / "plugins").exists())

    def test_streaming_changes_require_explicit_enrollment_and_exact_provider(self):
        data = self.backend.read()
        data["stt"]["streaming"] = False
        data["tts"]["streaming"] = {"provider": "previous", "unrelated": 123}
        self.write(data)
        plan = self.plan()
        self.assertFalse(plan.after["stt"]["streaming"])
        self.assertEqual(plan.after["tts"]["streaming"]["provider"], "previous")
        plan = self.plan(enable_stt_streaming=True, enable_tts_pcm=True)
        self.assertTrue(plan.after["stt"]["streaming"])
        self.assertEqual(plan.after["tts"]["streaming"], {"provider": "yandex", "unrelated": 123})

    def test_missing_local_native_capability_fails_before_any_write(self):
        class NoStreaming(LocalProvider):
            streaming_capable = False
            streams_pcm = False
        before = self.config.read_bytes()
        with self.assertRaises(SpeechError) as error:
            plan_native(_backend=self.backend, _plugin=self.plugin,
                        _providers=(NoStreaming, NoStreaming), enable_stt_streaming=True)
        self.assertEqual(error.exception.category, "PREREQUISITE")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.home / "plugins").exists())

    def test_foreign_plugin_entry_and_parent_redirection_are_preserved(self):
        parent = self.home / "plugins"
        parent.mkdir()
        link = parent / "yandex-speech"
        link.symlink_to(self.home / "foreign-install")
        before = self.config.read_bytes()
        with self.assertRaises(SpeechError):
            self.plan()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(link.readlink(), self.home / "foreign-install")

    def test_config_changed_after_plan_is_preserved(self):
        plan = self.plan()
        changed = self.backend.read()
        changed["model"]["default"] = "new-operator-choice"
        self.write(changed)
        with self.assertRaises(SpeechError) as error:
            plan.apply()
        self.assertEqual(error.exception.category, "STATE")
        self.assertEqual(self.backend.read(), changed)
        self.assertFalse((self.home / "plugins").exists())

    def test_failed_native_write_keeps_admitted_plugin(self):
        self.admit()
        plan = self.plan()
        before = self.config.read_bytes()
        with patch.object(self.backend, "write", side_effect=SpeechError("STATE", "synthetic write failure")):
            with self.assertRaises(SpeechError):
                plan.apply()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(plan.plugin_link.resolve(), self.plugin.resolve())
        self.assertEqual(self.secret.read_text(), "TEST_ONLY_SECRET=synthetic-owned-value\n")

    def test_malformed_config_and_config_symlink_fail_closed(self):
        self.config.write_text("not: [valid\n")
        with self.assertRaises(SpeechError):
            self.plan()
        self.assertEqual(self.config.read_text(), "not: [valid\n")
        self.assertFalse((self.home / "backups").exists(), "a plan must not create config backups")
        self.config.unlink()
        other = self.home / "other.yaml"
        other.write_text("model: untouched\n")
        self.config.symlink_to(other)
        with self.assertRaises(SpeechError):
            self.plan()
        self.assertEqual(other.read_text(), "model: untouched\n")

    def test_yaml_aliases_do_not_mutate_unrelated_settings(self):
        self.config.write_text("stt: &shared\n  provider: previous\nunrelated: *shared\ntts:\n  provider: previous\n")
        before = self.config.read_bytes()
        with self.assertRaises(SpeechError) as error:
            self.plan()
        self.assertEqual(error.exception.category, "CONFIG")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.home / "plugins").exists())

    def test_silent_native_writer_failure_is_detected_by_readback(self):
        self.admit()
        plan = self.plan()
        before = self.config.read_bytes()
        with patch.object(self.backend, "write"):
            with self.assertRaises(SpeechError) as error:
                plan.apply()
        self.assertEqual(error.exception.category, "STATE")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(plan.plugin_link.resolve(), self.plugin.resolve())

    def test_plugin_parent_redirection_is_refused_without_foreign_writes(self):
        foreign = self.home / "foreign-profile"
        foreign.mkdir()
        (self.home / "plugins").symlink_to(foreign, target_is_directory=True)
        before = self.config.read_bytes()
        with self.assertRaises(SpeechError):
            self.plan()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(foreign.iterdir()), [])

    def test_user_file_preferences_stay_above_migrated_command_defaults(self):
        from integrations.hermes.provider import YandexTranscriptionProvider
        from integrations.hermes.tts_provider import YandexTTSProvider
        data = self.backend.read()
        data["tts"]["providers"] = {"yandex": {"type": "command", "command":
            "env HERMES_TTS_VOICE=ermil " + str(self.root / "scripts/hermes-tts-wrapper.sh") +
            " {input_path} {output_path}"}}
        data["tts"]["yandex"] = {"role": "good"}
        self.write(data)
        path = self.home / ".config/yandex-speech/config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"speech": {"tts": {
            "default_voice": "marina", "default_emotion": "friendly"}}}))
        made = []
        def tts(home, profile):
            provider = YandexTTSProvider(home, profile,
                _secret_resolver=lambda name: "synthetic" if name == "YANDEX_SPEECH_API_KEY" else None)
            made.append(provider)
            return provider
        def stt(home, profile):
            return YandexTranscriptionProvider(home, profile, _secret_resolver=lambda: "synthetic")
        plan_native(_backend=self.backend, _plugin=self.plugin, _providers=(stt, tts))
        options = made[0]._client()._options()[1]
        self.assertEqual((options["default_voice"], options["_role"]), ("marina", "friendly"))

    def test_documented_cli_plan_apply_check_and_repeat_in_an_isolated_profile(self):
        self.admit()
        import os
        import subprocess
        import sys
        data = self.backend.read()
        data["tts"]["voice"] = "ermil"
        data["stt"]["yandex"] = {"api_version": "1", "transport": "rest", "literature_text": False}
        self.write(data)
        user_file = self.home / ".config/yandex-speech/config.json"
        user_file.parent.mkdir(parents=True)
        user_file.write_text(json.dumps({"speech": {"api_version": "1", "transport": "rest",
                                                   "literature_text": False}}))
        before = self.config.read_bytes()
        secret = self.secret.read_bytes()
        environment = dict(os.environ, HERMES_HOME=str(self.home),
                           YANDEX_SPEECH_API_KEY="synthetic-offline-only",
                           HERMES_TTS_VOICE="ermil", HERMES_TTS_LANG="ru-RU")
        script = str(self.root / "scripts/speech_setup.py")
        def run(*args):
            result = subprocess.run([sys.executable, script, *args], cwd=self.home,
                env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return json.loads(result.stdout)
        def profile_state():
            return {str(path.relative_to(self.home)):
                    ("link", str(path.readlink())) if path.is_symlink() else
                    ("directory", None) if path.is_dir() else ("file", path.read_bytes())
                    for path in self.home.rglob("*")}
        before_tree = profile_state()
        self.assertEqual(run("--hermes-plan")["status"], "planned")
        self.assertEqual(profile_state(), before_tree)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(run("--hermes-apply")["status"], "applied")
        committed = self.config.read_bytes()
        self.assertFalse(run("--hermes-apply")["changed"])
        self.assertEqual(self.config.read_bytes(), committed)
        self.assertEqual(self.secret.read_bytes(), secret)
        self.assertFalse(run("--check")["permissions"]["status"] == "execution-verified")

    def test_unknown_template_fields_and_wrong_operation_flags_are_not_dropped(self):
        import copy
        baseline = self.backend.read()
        for kind, command in [
            ("tts", str(self.root / "scripts/hermes-tts-wrapper.sh") + " {foreign_input} {output_path}"),
            ("stt", "python " + str(self.root / "scripts/transcribe.py") +
             " {input_path} --voice ermil"),
            ("tts", "python " + str(self.root / "scripts/tts.py") +
             " --input-file {input_path} --output {output_path} --text-format ssml")]:
            data = copy.deepcopy(baseline)
            data[kind]["providers"] = {"yandex": {"type": "command", "command": command}}
            self.write(data)
            before = self.config.read_bytes()
            with self.assertRaises(SpeechError):
                self.plan()
            self.assertEqual(self.config.read_bytes(), before)
            self.assertFalse((self.home / "plugins").exists())

    def test_noncanonical_command_shapes_are_rejected_before_migration(self):
        import copy
        baseline = self.backend.read()
        commands = [
            "python " + str(self.root / "scripts/tts.py") +
            " --input-file {input_path} --output {output_path} --mode async",
            str(self.root / "scripts/hermes-tts-wrapper.sh") + " {output_path} {input_path}",
            str(self.root / "scripts/hermes-tts-wrapper.sh") + " {input_path}/other.txt {output_path}",
        ]
        for command in commands:
            data = copy.deepcopy(baseline)
            data["tts"]["providers"] = {"yandex": {"type": "command", "command": command}}
            self.write(data)
            before = self.config.read_bytes()
            with self.assertRaises(SpeechError):
                self.plan()
            self.assertEqual(self.config.read_bytes(), before)
            self.assertFalse((self.home / "plugins").exists())

    def test_migration_rejects_output_and_language_aliases_rejected_by_real_parsers(self):
        import contextlib
        import copy
        import io
        from scripts.tts import build_parser as tts_parser
        from scripts.transcribe import build_parser as stt_parser

        baseline = self.backend.read()
        for kind, parser, arguments in [
            ("tts", tts_parser, ["--input-file", "{input_path}", "-o", "{output_path}"]),
            ("tts", tts_parser, ["--input-file", "{input_path}", "--output", "{output_path}",
                                 "--language", "ru-RU"]),
            ("tts", tts_parser, ["--input-file", "{input_path}", "--output-dir", "{output_path}"]),
            ("stt", stt_parser, ["{input_path}", "-o", "{output_dir}"]),
            ("stt", stt_parser, ["{input_path}", "--output", "{output_dir}", "--language", "ru-RU"]),
            ("stt", stt_parser, ["{input_path}", "--output-dir", "{output_dir}"]),
        ]:
            with self.subTest(kind=kind, arguments=arguments):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
                    parser().parse_args(arguments)
                self.assertEqual(rejected.exception.code, 2)
                script = "tts.py" if kind == "tts" else "transcribe.py"
                data = copy.deepcopy(baseline)
                data[kind]["providers"] = {"yandex": {"type": "command", "command":
                    "python " + str(self.root / "scripts" / script) + " " + " ".join(arguments)}}
                self.write(data)
                before = self.config.read_bytes()
                with self.assertRaises(SpeechError):
                    self.plan()
                self.assertEqual(self.config.read_bytes(), before)
                self.assertFalse((self.home / "plugins").exists())

    def test_canonical_direct_language_flags_remain_migratable(self):
        data = self.backend.read()
        data["tts"]["providers"] = {"yandex": {"type": "command", "command":
            "python " + str(self.root / "scripts/tts.py") +
            " --input-file {input_path} --output {output_path} --lang ru-RU"}}
        data["stt"]["providers"] = {"yandex": {"type": "command", "command":
            "python " + str(self.root / "scripts/transcribe.py") +
            " {input_path} --output {output_dir}/transcript.txt --lang {language} --format txt"}}
        self.write(data)
        self.assertEqual(self.plan().after["tts"]["yandex"]["language"], "ru-RU")

    def test_known_success_cat_and_adapter_flags_migrate_the_literal_preferences(self):
        data = self.backend.read()
        data["stt"]["providers"] = {"yandex": {"type": "command", "command":
            str(self.root / "scripts/hermes-stt-wrapper.sh") +
            " {input_path} {output_dir} {language} && cat {output_dir}/transcript.txt"}}
        data["tts"]["providers"] = {"yandex": {"type": "command", "output_format": "ogg",
            "command": "python3 " + str(self.root / "scripts/tts.py") +
            " --input-file {input_path} --output {output_path} --format oggopus"
            " --adapter-voice ermil --adapter-emotion good"}}
        self.write(data)
        plan = self.plan()
        self.assertEqual(plan.after["tts"]["voice"], "ermil")
        self.assertEqual(plan.after["tts"]["output_format"], "ogg")
        self.assertEqual(plan.after["tts"]["yandex"]["role"], "good")

    def test_parent_redirect_after_the_last_path_guard_never_writes_foreign_directory(self):
        self.admit()
        foreign = self.home / "foreign-profile"
        foreign.mkdir()
        plan = self.plan()
        original = plan._link_state
        calls = []
        def late_redirect():
            state = original()
            calls.append(state)
            if len(calls) == 2:
                (self.home / "plugins").rename(self.home / "admitted-before-redirect")
                (self.home / "plugins").symlink_to(foreign, target_is_directory=True)
            return state
        before = self.config.read_bytes()
        with patch.object(plan, "_link_state", side_effect=late_redirect):
            with self.assertRaises(SpeechError):
                plan.apply()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(foreign.iterdir()), [])

    def test_native_key_conflict_preserves_unrelated_changes(self):
        self.admit()
        plan = self.plan()
        publish = self.backend._write
        def intervening_write(path, updates):
            publish(path, {"model.default": "late-operator-choice", "new_unrelated_field": "preserve"})
            return publish(path, updates)
        with patch.object(self.backend, "_write", side_effect=intervening_write):
            with self.assertRaises(SpeechError) as error:
                plan.apply()
        self.assertEqual(error.exception.category, "STATE")
        current = self.backend.read()
        self.assertEqual(current["model"]["default"], "late-operator-choice")
        self.assertEqual(current["new_unrelated_field"], "preserve")
        self.assertEqual(plan.plugin_link.resolve(), self.plugin.resolve())
        self.assertEqual(self.secret.read_text(), "TEST_ONLY_SECRET=synthetic-owned-value\n")

    def test_missing_native_key_api_blocks_changes(self):
        self.admit()
        plan = self.plan()
        before = self.config.read_bytes()
        with patch.object(self.backend, "supports_key_updates", False):
            with self.assertRaises(SpeechError) as error:
                plan.apply()
        self.assertEqual(error.exception.category, "PREREQUISITE")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(plan.plugin_link.resolve(), self.plugin.resolve())

    def test_stock_noop_needs_no_writer_and_changes_no_profile_bytes(self):
        self.admit()
        desired = self.plan().after
        self.write(desired)
        before = self.config.read_bytes(), self.secret.read_bytes()
        with patch.object(self.backend, "supports_key_updates", False), patch.object(
                self.backend, "_write", side_effect=AssertionError("no-op must not write")):
            report = self.plan().apply()
        self.assertEqual(report["status"], "applied")
        self.assertFalse(report["changed"])
        self.assertTrue(report["safe_apply_available"])
        self.assertEqual((self.config.read_bytes(), self.secret.read_bytes()), before)

    def test_stock_noop_constructs_without_a_native_writer(self):
        self.admit()
        self.write(self.plan().after)
        from hermes_cli import config_backend
        before = self.config.read_bytes(), self.secret.read_bytes()
        selected = config_backend.get_config_backend()
        with patch("hermes_constants.get_hermes_home", return_value=self.home), patch.object(
                selected, "write_changes", None):
            readonly = _NativeBackend()
            self.assertFalse(readonly.supports_key_updates)
            report = plan_native(_backend=readonly, _providers=(LocalProvider, LocalProvider),
                                 _plugin=self.plugin).apply()
        self.assertFalse(report["changed"])
        self.assertEqual((self.config.read_bytes(), self.secret.read_bytes()), before)

    def test_remote_backend_is_refused_before_any_config_io(self):
        from hermes_cli import config_backend
        from unittest.mock import Mock
        remote = Mock()
        remote.name = "remote"
        before = self.config.read_bytes(), self.secret.read_bytes()
        with patch.object(config_backend, "get_config_backend", return_value=remote):
            with self.assertRaises(SpeechError) as raised:
                _NativeBackend()
        self.assertEqual(raised.exception.category, "PREREQUISITE")
        remote.read_user_doc.assert_not_called()
        remote.write_changes.assert_not_called()
        self.assertEqual((self.config.read_bytes(), self.secret.read_bytes()), before)

    def test_backend_switch_after_plan_cannot_publish_settings(self):
        from unittest.mock import Mock
        self.admit()
        plan = self.plan()
        before = self.config.read_bytes()
        remote = Mock()
        remote.name = "remote"
        with patch.object(self.backend, "_get_backend", return_value=remote):
            with self.assertRaises(SpeechError) as raised:
                plan.apply()
        self.assertEqual(raised.exception.category, "STATE")
        remote.write_changes.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_bound_file_writer_cannot_be_rerouted_by_ambient_dispatch(self):
        from hermes_cli import config_backend
        from unittest.mock import Mock
        remote = Mock()
        remote.name = "remote"
        with patch.object(config_backend, "get_config_backend", return_value=remote):
            self.assertIs(config_backend.get_config_backend(), remote)
            self.backend._write(self.config, {"tts.voice": "ermil"})
        remote.write_changes.assert_not_called()
        self.assertEqual(self.backend.read()["tts"]["voice"], "ermil")
        self.assertEqual(self.backend.read()["model"]["default"], "unrelated-model")

    def test_native_installation_is_a_separate_admitted_prerequisite(self):
        before = self.config.read_bytes(), self.secret.read_bytes()
        plan = self.plan()
        summary = plan.summary()
        self.assertFalse(summary["safe_apply_available"])
        self.assertTrue(any("hermes plugins enable" in step for step in summary["native_setup_steps"]))
        with self.assertRaises(SpeechError) as raised:
            plan.apply()
        self.assertEqual(raised.exception.category, "PREREQUISITE")
        self.assertEqual((self.config.read_bytes(), self.secret.read_bytes()), before)
        self.assertFalse((self.home / "plugins").exists())

    def test_matching_native_installed_directory_is_preserved(self):
        import shutil
        self.admit()
        link = self.home / "plugins/yandex-speech"
        link.unlink()
        shutil.copytree(self.plugin, link, ignore=shutil.ignore_patterns("__pycache__"))
        report = self.plan().apply()
        self.assertEqual(report["status"], "applied")
        self.assertFalse(link.is_symlink())
        self.assertEqual(report["plugin"]["action"], "keep-native-installation")

    def test_second_key_failure_reports_partial_state_without_snapshot_rollback(self):
        self.admit()
        plan = self.plan()
        native = self.backend._write
        calls = []
        def fail_second(path, updates):
            calls.append(updates)
            if len(calls) == 2:
                raise OSError("synthetic second-key failure")
            return native(path, updates)
        with patch.object(self.backend, "_write", side_effect=fail_second):
            with self.assertRaises(SpeechError) as raised:
                plan.apply()
        self.assertIn("partially", str(raised.exception))
        current = self.backend.read()
        self.assertEqual(current["stt"]["provider"], "yandex")
        self.assertEqual(current["tts"]["provider"], "old")
        self.assertEqual(current["model"]["default"], "unrelated-model")
        self.assertEqual(plan.plugin_link.resolve(), self.plugin.resolve())

    def test_migration_commands_preserve_literal_preferences_before_removal(self):
        self.admit()
        data = self.backend.read()
        data["tts"]["providers"] = {"yandex": {"type": "command",
            "command": "env HERMES_TTS_VOICE=ermil HERMES_TTS_LANG=ru-RU " +
                str(self.root / "scripts/hermes-tts-wrapper.sh") + " {input_path} {output_path}",
            "output_format": "ogg", "voice_compatible": True}}
        self.write(data)
        steps = self.plan().summary()["native_migration_steps"]
        import os
        import shlex
        import subprocess
        import sys
        parsed = [shlex.split(step) for step in steps]
        voice = next(index for index, args in enumerate(parsed) if args[2:4] == ["set", "tts.voice"])
        remove = next(index for index, args in enumerate(parsed) if args[2:] == ["unset", "tts.providers.yandex"])
        select = next(index for index, args in enumerate(parsed) if args[2:4] == ["set", "tts.provider"])
        self.assertEqual(parsed[voice][4], "ermil")
        self.assertLess(voice, remove)
        self.assertLess(remove, select)
        environment = dict(os.environ, HERMES_HOME=str(self.home), HERMES_CONFIG_BACKEND="file",
                           HERMES_SKIP_CHMOD="1", YANDEX_SPEECH_API_KEY="synthetic-offline-only")
        for args in parsed:
            result = subprocess.run([sys.executable, "-m", "hermes_cli.main", *args[1:]],
                env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        current = self.backend.read()
        self.assertEqual(current["tts"]["voice"], "ermil")
        self.assertEqual(current["tts"]["provider"], "yandex")
        self.assertNotIn("yandex", current["tts"].get("providers", {}))
        self.assertEqual(current["model"]["default"], "unrelated-model")
        self.assertEqual(self.secret.read_text(), "TEST_ONLY_SECRET=synthetic-owned-value\n")

    def test_foreign_native_installed_directory_is_never_rewritten(self):
        import shutil
        self.admit()
        entry = self.home / "plugins/yandex-speech"
        entry.unlink()
        shutil.copytree(self.plugin, entry, ignore=shutil.ignore_patterns("__pycache__"))
        foreign = entry / "provider.py"
        foreign.write_text("# foreign installation preserved\n")
        before = self.config.read_bytes()
        with self.assertRaises(SpeechError):
            self.plan()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(foreign.read_text(), "# foreign installation preserved\n")


class NativeSetupCliTests(unittest.TestCase):
    def test_cli_routes_plan_apply_and_explicit_streaming_without_a_probe(self):
        from yandex_speech.readiness import main
        plan = Mock()
        plan.summary.return_value = {"status": "planned"}
        plan.apply.return_value = {"status": "applied"}
        with patch("yandex_speech.hermes_setup.plan_native", return_value=plan) as factory:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--hermes-plan"]), 0)
                self.assertEqual(main(["--hermes-apply", "--enable-stt-streaming", "--enable-tts-pcm"]), 0)
        self.assertEqual(factory.call_args.kwargs, {"enable_stt_streaming": True, "enable_tts_pcm": True})
        plan.summary.assert_called_once()
        plan.apply.assert_called_once()

    def test_cli_rejects_ambiguous_or_accidentally_billable_combinations(self):
        from yandex_speech.readiness import main
        for args in [["--enable-stt-streaming"], ["--check", "--hermes-apply"],
                     ["--hermes-plan", "--probe-audio", "synthetic.wav"],
                     ["--hermes-apply", "--config", "other.json"]]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
