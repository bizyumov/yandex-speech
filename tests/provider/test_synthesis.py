"""Explicit real synthesis acceptance as the configured Hermes profile owner."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from yandex_speech import TextToSpeech

ROOT=Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS")=="1",
                     "explicit provider opt-in required")
class LiveSynthesisTests(unittest.TestCase):
    def setUp(self):
        profile=Path(os.environ["HERMES_HOME"])
        self.assertEqual(os.geteuid(),profile.stat().st_uid)
        self.assertEqual(Path(sys.prefix).resolve(),Path(os.environ["VIRTUAL_ENV"]).resolve())
        self.assertTrue(os.environ.get("YANDEX_SPEECH_API_KEY"))
        destination=os.environ.get("YANDEX_SPEECH_TEST_OUTPUT_DIR")
        if destination:
            self.root=Path(destination)/("synthesis-"+uuid.uuid4().hex)
            self.root.mkdir(parents=True,mode=0o700)
        else:
            self.tmp=tempfile.TemporaryDirectory(prefix="speech-synthesis-acceptance-")
            self.addCleanup(self.tmp.cleanup)
            self.root=Path(self.tmp.name)
        self.client=TextToSpeech()

    def probe(self,path,raw=False):
        hints=["-f","s16le","-ar","48000","-ac","1"] if raw else []
        result=subprocess.run(["ffprobe","-v","error",*hints,"-show_entries",
            "format=duration,format_name:stream=codec_name","-of","json",str(path)],
            capture_output=True,text=True,check=True)
        data=json.loads(result.stdout)
        self.assertGreater(float(data["format"]["duration"]),0)
        return data

    def proof(self,record):
        record.update(case=self.id(),interpreter=sys.executable,principal_uid=os.geteuid())
        (self.root/"acceptance.json").write_text(json.dumps(record,ensure_ascii=False,indent=2))

    def test_actual_v1_rest_v3_rest_grpc_file_format_matrix(self):
        observations=[]
        for version,transport in [("1","rest"),("3","rest"),("3","grpc")]:
            for fmt in ["mp3","wav","oggopus","lpcm"]:
                with self.subTest(version=version,transport=transport,fmt=fmt):
                    output=self.root/f"{version}-{transport}.{fmt}"
                    self.client.synthesize("Проверка синтеза.",output,api_version=version,
                        transport=transport,voice="filipp",fmt=fmt)
                    data=self.probe(output,raw=fmt=="lpcm")
                    self.assertEqual(self.client.last_synthesis["api_version"],version)
                    self.assertEqual(self.client.last_synthesis["transport"],transport)
                    self.assertEqual(self.client.last_synthesis["voice"],"filipp")
                    if fmt=="mp3":self.assertEqual(data["format"]["format_name"],"mp3")
                    observations.append({**self.client.last_synthesis,"probe":data})
        self.proof({"matrix":observations})

    def test_published_style_ssml_cli_and_complete_multipart(self):
        source=self.root/"input.xml"
        source.write_text('<speak><p><s>Проверка разметки.</s></p><p>Вторая фраза.</p><break time="200ms"/><sub alias="пример">пр.</sub><phoneme alphabet="ipa" ph="o">О</phoneme></speak>')
        target=self.root/"ssml.wav"
        run=subprocess.run(["python",str(ROOT/"scripts/tts.py"),"--input-file",str(source),
            "--text-format","ssml","--api-version","1","--voice","ermil",
            "--format","wav","--output",str(target)],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr[-1000:])
        self.probe(target)
        long_output=self.root/"multipart.mp3"
        self.client.synthesize("Проверка длинного текста. "+("Слова сохраняются по порядку. "*25),
            long_output,api_version="3",transport="rest",model="livetts",voice="sergey",fmt="mp3")
        self.assertGreater(self.client.last_synthesis["parts"],1)
        self.assertEqual(self.probe(long_output)["format"]["format_name"],"mp3")
        decoded = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror",
            "-i", str(long_output), "-f", "null", "-"], capture_output=True, timeout=60)
        self.assertEqual(decoded.returncode, 0, "complete multipart MP3 must fully decode")
        self.proof({"ssml_cli_exit":run.returncode,"multipart":self.client.last_synthesis})

    def test_progressive_audio_precedes_input_eof(self):
        release=threading.Event()
        def fragments():
            yield "Первая фраза потокового синтеза. "
            if not release.wait(20):raise RuntimeError("provider did not emit progressive audio")
            yield "Контрольный хвост. "
        with self.client.stream_audio(fragments(),voice="sergey",model="livetts",
                                      deadline_seconds=60) as audio:
            first=next(audio)
            self.assertTrue(first)
            self.assertFalse(release.is_set())
            release.set()
            data=first+b"".join(audio)
            self.assertTrue(audio.input_complete)
            self.assertEqual(len(data)%2,0)
            self.assertEqual(audio.audio_spec["sample_rate_hertz"],24000)
            self.assertNotEqual(data[:4],b"RIFF")
        (self.root/"stream.pcm").write_bytes(data)
        self.proof({"before_input_eof":True,"bytes":len(data),"spec":audio.audio_spec})

    def test_livetts_voices_and_legacy_language_requests(self):
        results=[]
        for voice in ["sergey","vasily","denis"]:
            path=self.root/(voice+".mp3")
            self.client.synthesize("Проверка голоса.",path,api_version="3",transport="rest",
                                   model="livetts",voice=voice,fmt="mp3")
            self.assertEqual(self.probe(path)["format"]["format_name"],"mp3")
            self.assertEqual(self.client.last_synthesis["voice"],voice)
            self.assertIsNone(self.client.last_synthesis["conversion"])
            results.append(dict(self.client.last_synthesis))
        for lang,voice,text in [("ru-RU","alena","Проверка."),("en-US","john","Hello."),
                                 ("kk-KZ","amira","Сәлеметсіз бе."),("en-US","ermil","Hello.")]:
            path=self.root/(voice+"-"+lang+".mp3")
            self.client.synthesize(text,path,lang=lang,voice=voice,api_version="1",fmt="mp3")
            self.probe(path)
            results.append(dict(self.client.last_synthesis))
        self.proof({"requests":results})

    @unittest.skipUnless(os.environ.get("YANDEX_SPEECH_HOST_TESTS") == "1",
                         "explicit active-host tool opt-in required")
    def test_standard_hermes_tool_uses_configured_yandex_provider(self):
        import time
        from hermes_cli.config import load_config_readonly
        from tools.tts_tool import text_to_speech_tool
        profile = Path(os.environ["HERMES_HOME"])
        watched = [path for path in (profile / "config.yaml", profile / ".env",
            profile / ".config/yandex-speech/config.json") if path.is_file()]
        before = {path: path.read_bytes() for path in watched}
        selected = load_config_readonly()
        for kind in ("stt", "tts"):
            self.assertEqual(selected.get(kind, {}).get("provider"), "yandex")
            override = selected.get(kind, {}).get("providers", {}).get("yandex") or {}
            self.assertNotEqual(override.get("type"), "command", "native route must not be shadowed")
        started = time.monotonic()
        result = json.loads(text_to_speech_tool("Проверка штатного голосового ответа.",
                          output_path=str(self.root / "standard.ogg")))
        synthesis_seconds = time.monotonic() - started
        self.assertTrue(result.get("success"), "standard Hermes TTS failed")
        self.assertEqual(result["provider"], "yandex")
        self.assertTrue(result.get("voice_compatible"))
        self.assertEqual(self.probe(Path(result["file_path"]))["streams"][0]["codec_name"], "opus")
        from agent import transcription_registry, tts_registry
        for registry in (transcription_registry, tts_registry):
            registered = registry.get_provider("yandex")
            self.assertIsNotNone(registered, "paired native provider registration required")
            self.assertEqual(registered.workspace.resolve(), profile.resolve())
        for path, data in before.items():
            self.assertTrue(path.read_bytes() == data, "owning-profile settings/credentials changed during check")
        self.proof({"standard_tool": result, "owning_profile_readonly_check": True,
            "native_stt_tts_selected": True, "command_overrides_absent": True,
            "synthesis_seconds": synthesis_seconds, "delivery_seconds": None,
            "timing_basis": "standard text_to_speech tool wall time; actual Telegram delivery remains operator-driven"})

    def test_installed_skill_synthesis_commands_and_missing_grpc(self):
        import select
        import shlex
        import zipfile

        wheel = Path(os.environ["YANDEX_SPEECH_TEST_WHEEL"])
        installed = self.root / "installed"
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(installed)
        skill = next(installed.glob("*.data/data/share/yandex-speech"))
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "reply.txt").write_text("Проверка обычного голосового ответа.")
        (outside / "marked-up.xml").write_text(
            '<speak><p><s>Проверка разметки.</s></p><break time="200ms"/>'
            '<sub alias="пример">пр.</sub></speak>')
        recipes = [
            shlex.split(line) for line in (skill / "SKILL.md").read_text().splitlines()
            if line.startswith("python /absolute/skill/scripts/tts.py")
        ]
        self.assertEqual(len(recipes), 4, "every published synthesis recipe needs a fixture")
        env = dict(os.environ, PYTHONPATH=str(installed))
        # Hide only the optional SDK in this child, without changing installed packages.
        no_grpc = (
            "import importlib.abc,runpy,sys\n"
            "class MissingGrpc(importlib.abc.MetaPathFinder):\n"
            " def find_spec(self,fullname,path=None,target=None):\n"
            "  if fullname.split('.')[0] in {'grpc','yandex'}:"
            "raise ModuleNotFoundError('optional grpc SDK unavailable',name=fullname)\n"
            "sys.meta_path.insert(0,MissingGrpc())\n"
            "sys.argv=sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0],run_name='__main__')\n"
        )
        evidence = []
        for recipe in recipes:
            command = [value.replace("/absolute/skill", str(skill)) for value in recipe]
            if "--stream" in command:
                missing = subprocess.run(
                    ["python", "-B", "-c", no_grpc, *command[1:]], cwd=outside, env=env,
                    input="Проверка потока.\n", capture_output=True, text=True, timeout=30)
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn("grpc", missing.stderr.lower())
                self.assertFalse(missing.stdout)
                with subprocess.Popen(command, cwd=outside, env=env,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        bufsize=0) as process:
                    try:
                        process.stdin.write("Первая фраза.\n".encode())
                        process.stdin.flush()
                        ready, _, _ = select.select([process.stdout], [], [], 30)
                        self.assertTrue(ready, "installed CLI buffered until input EOF")
                        first = os.read(process.stdout.fileno(), 4096)
                        self.assertTrue(first)
                        self.assertIsNone(process.poll())
                        process.stdin.write("Последняя фраза.\n".encode())
                        process.stdin.close()
                        process.stdin = None
                        rest, error = process.communicate(timeout=60)
                        self.assertEqual(process.returncode, 0, error.decode()[-1000:])
                        self.assertEqual(len(first + rest) % 2, 0)
                        self.assertNotEqual(first[:4], b"RIFF")
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
                evidence.append({"operation": "installed stdin stream",
                    "pcm_before_input_eof": True, "bytes": len(first + rest),
                    "missing_grpc_exit": missing.returncode})
            else:
                result = subprocess.run(
                    ["python", "-B", "-c", no_grpc, *command[1:]], cwd=outside, env=env,
                    capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])
                output = outside / command[command.index("--output") + 1]
                probe = self.probe(output)
                metadata = next(json.loads(line) for line in reversed(result.stderr.splitlines())
                                if line.startswith("{"))
                self.assertEqual(metadata["transport"], "rest")
                evidence.append({"operation": "installed file recipe without grpc",
                    "api_version": metadata["api_version"], "format": metadata["format"],
                    "probe": probe})
        self.assertEqual([r["api_version"] for r in evidence if "api_version" in r],
                         ["1", "3", "1"])
        self.proof({"installed_recipes": evidence})
