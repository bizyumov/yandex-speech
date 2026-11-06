import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


class BaselineTests(unittest.TestCase):
    def test_skill_entry_and_links_are_self_contained(self):
        from ruamel.yaml import YAML
        skill=(ROOT/"SKILL.md").read_text()
        front=YAML(typ="safe").load(skill.split("---",2)[1])
        self.assertEqual(front["name"],"yandex-speech")
        self.assertTrue(front["description"].strip())
        for name in ["SKILL.md","README.md","references/transcription.md",
                     "references/media-preparation.md","references/hermes.md",
                     "references/yandex-cloud-setup.md"]:
            text=(ROOT/name).read_text()
            self.assertNotIn(".agent/",text)
            for target in re.findall(r"\]\(([^)]+)\)",text):
                if "://" in target or target.startswith("#"):
                    continue
                local=target.split("#",1)[0]
                self.assertTrue((ROOT/name).parent.joinpath(local).exists(),
                                f"{name} has missing payload link {target}")

    def test_version_changelog_mit_and_plugin_projection(self):
        from ruamel.yaml import YAML
        from yandex_speech import __version__
        version=(ROOT/"VERSION").read_text().strip()
        self.assertRegex(version,r"^\d+\.\d+\.\d+$")
        self.assertEqual(__version__,version)
        self.assertIn("## Unreleased",(ROOT/"CHANGELOG.md").read_text())
        license_text=(ROOT/"LICENSE").read_text()
        self.assertIn("MIT License",license_text)
        self.assertIn("Boris Izyumov",license_text)
        manifest=YAML(typ="safe").load((ROOT/"integrations/hermes/plugin.yaml").read_text())
        self.assertEqual(manifest["version"],version)
        from hermes_cli.plugins_manifest import parse_manifest_file
        parsed=parse_manifest_file(ROOT/"integrations/hermes/plugin.yaml",
            ROOT/"integrations/hermes","user","")
        self.assertIsNotNone(parsed)

    def test_wheel_and_sdist_payload_work_outside_checkout(self):
        with tempfile.TemporaryDirectory(prefix="speech-package-") as directory:
            directory=Path(directory)
            build=subprocess.run([sys.executable,"-m","build","--no-isolation",
                "--outdir",str(directory)],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(build.returncode,0,(build.stdout+build.stderr)[-2500:])
            wheel=next(directory.glob("*.whl"))
            sdist=next(directory.glob("*.tar.gz"))
            with zipfile.ZipFile(wheel) as artifact:
                names=artifact.namelist()
                for required in ["SKILL.md","README.md","VERSION","CHANGELOG.md","LICENSE",
                                 "references/transcription.md","scripts/speech_setup.py",
                                 "integrations/hermes/plugin.yaml","tests/acceptance.json"]:
                    self.assertTrue(any(n.endswith("/share/yandex-speech/"+required) for n in names),required)
                self.assertFalse(any("/.agent/" in n or "/.gitmark/" in n or "/.env" in n for n in names))
                extracted=directory/"installed"
                artifact.extractall(extracted)
            with tarfile.open(sdist) as artifact:
                self.assertFalse(any("/.agent/" in n or "/.serena/" in n for n in artifact.getnames()))
            skill=next(extracted.glob("*.data/data/share/yandex-speech"))
            outside=directory/"outside"
            outside.mkdir()
            for command in [
                [str(skill/"scripts/transcribe.py"),"--version"],
                [str(skill/"scripts/tts.py"),"--version"],
                [str(skill/"scripts/speech_setup.py"),"--check"],
            ]:
                result=subprocess.run([sys.executable,*command],cwd=outside,
                    env={"HOME":str(outside),"PATH":os.environ["PATH"],"PYTHONPATH":str(extracted)},
                    capture_output=True,text=True)
                expected=2 if "--check" in command else 0
                self.assertEqual(result.returncode,expected,result.stderr)

            # Native enrollment must resolve the plugin from this installed payload.
            profile=outside/"native-profile"
            profile.mkdir()
            (profile/"config.yaml").write_text("tts:\n  voice: ermil\n")
            environment={"HOME":str(outside),"PATH":os.environ["PATH"],"PYTHONPATH":str(extracted),
                         "HERMES_HOME":str(profile),"YANDEX_SPEECH_API_KEY":"synthetic-offline-only",
                         "HERMES_TTS_VOICE":"ermil","HERMES_TTS_LANG":"ru-RU"}
            import json
            reports=[]
            for operation in ["--hermes-plan","--hermes-apply","--hermes-apply"]:
                result=subprocess.run([sys.executable,str(skill/"scripts/speech_setup.py"),operation],
                    cwd=outside,env=environment,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr or result.stdout)
                reports.append(json.loads(result.stdout))
            self.assertFalse(reports[-1]["changed"])
            from hermes_cli.config import fast_safe_load
            self.assertEqual(fast_safe_load((profile/"config.yaml").read_text())["tts"]["voice"],"ermil")
            self.assertEqual((profile/"plugins/yandex-speech").resolve(),
                             (extracted/"integrations/hermes").resolve())
