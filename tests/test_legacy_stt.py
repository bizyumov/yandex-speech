import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests.helpers import make_wave
from yandex_speech import SpeechTranscriber, SpeechError
from yandex_speech.config import RuntimeContext


class LegacySttTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=make_wave(self.root/"input.wav")
        self.client=SpeechTranscriber(api_key="synthetic",_context=RuntimeContext(user_home=self.root))
        self.client.session=Mock()
        response=self.client.session.post.return_value
        response.status_code=200
        response.json.return_value={"result":"тест"}

    def test_direct_compatibility_sends_raw_pcm_without_folder(self):
        result=self.client.transcribe_direct(self.source,language_code="ru-RU",
                                            diarize=False,literature_text=False)
        self.assertEqual(result["text"],"тест")
        call=self.client.session.post.call_args
        self.assertNotIn("folderId",call.kwargs["params"])
        self.assertEqual(call.kwargs["params"]["format"],"lpcm")
        self.assertEqual(call.kwargs["params"]["rawResults"],"true")
        self.assertIsNone(result["segments"][0]["start"])
        self.assertEqual(result["api_version"],"1")

    def test_v1_still_downmixes_native_stereo_wav_and_ogg(self):
        wav = make_wave(self.root / "stereo.wav", channels=2)
        ogg = self.root / "stereo.ogg"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(wav),
                        "-c:a", "libopus", str(ogg)], check=True)
        for path in (wav, ogg):
            with self.subTest(suffix=path.suffix):
                self.client.transcribe_direct(path, language_code="ru-RU",
                                              diarize=False, literature_text=False)
                call = self.client.session.post.call_args.kwargs
                expected = subprocess.check_output([
                    "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                    "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
                ])
                self.assertEqual(call["params"]["format"], "lpcm")
                self.assertEqual(call["data"], expected)

    def test_direct_does_not_silently_drop_required_features(self):
        for options in [{"literature_text":False,"diarize":True},
                        {"literature_text":False,"timestamps":True}]:
            with self.assertRaises(SpeechError):
                self.client.transcribe_direct(self.source,**options)
        self.client.session.post.assert_not_called()

    def test_explicit_v1_auto_routes_to_preserved_rest_and_reports_literary_limit(self):
        result=self.client.transcribe_auto(self.source,api_version="1")
        self.assertEqual(result["api_version"],"1")
        self.assertEqual(result["transport"],"rest")
        self.assertEqual(result["metadata_availability"]["text_representation"],"raw-unfulfilled")
        self.assertTrue(result["uncertainties"])
        self.client.session.post.assert_called_once()

    def test_selected_legacy_track_is_the_addressed_source(self):
        import subprocess
        first=make_wave(self.root/"short.wav")
        # 0.1 s and 0.2 s tracks make the submitted source measurable.
        import wave
        second=self.root/"long.wav"
        with wave.open(str(second),"wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0"*6400)
        source=self.root/"tracks.mka"
        subprocess.run(["ffmpeg","-nostdin","-v","error","-i",str(first),"-i",str(second),
                        "-map","0:a","-map","1:a","-c:a","pcm_s16le",str(source)],check=True)
        self.client.transcribe_direct(source,audio_track=1)
        self.assertEqual(len(self.client.session.post.call_args.kwargs["data"]),6400)

    def test_http_error_categories_and_status_are_retained_without_payload(self):
        for status,category in [(401,"AUTH"),(403,"PERMISSION"),(429,"RATE_LIMIT"),(500,"PROVIDER")]:
            with self.subTest(status=status):
                self.client.session.post.return_value.status_code=status
                self.client.session.post.return_value.text="sentinel-private-provider-payload"
                with self.assertRaises(SpeechError) as cm:
                    self.client.transcribe_direct(self.source)
                self.assertEqual(cm.exception.category,category)
                self.assertEqual(cm.exception.code,str(status))
                self.assertNotIn("sentinel-private",str(cm.exception))

    def test_source_module_keeps_public_class_name(self):
        from scripts.transcribe import SpeechTranscriber as LegacyImport
        self.assertIs(LegacyImport,SpeechTranscriber)
