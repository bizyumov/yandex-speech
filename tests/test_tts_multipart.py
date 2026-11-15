import base64
import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from scripts.tts import TextToSpeech
from tests.test_tts_v3 import Response
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError
from yandex_speech.tts_audio import wrap_pcm


def audio_response():
    return Response([{"result":{"audioChunk":{"data":base64.b64encode(
        wrap_pcm(b"\0\0"*4800,48000)).decode()}}}])


class Rejected(Response):
    def __init__(self,status,message):
        self.status_code=status
        self.message=message
    def json(self):
        return {"message":self.message}


class MultipartSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.client=TextToSpeech(api_key="synthetic",
            _context=RuntimeContext(user_home=self.root))
        self.options=dict(api_version="3",transport="rest",model="livetts",
                          voice="sergey",fmt="wav")

    def test_long_text_preserves_order_and_returns_one_wav_header(self):
        text="я"*501
        target=self.root/"complete.wav"
        with patch("scripts.tts.requests.post",side_effect=[audio_response() for _ in range(3)]) as post:
            outputs=self.client.synthesize(text,target,**self.options)
        self.assertEqual(outputs,[target])
        self.assertEqual("".join(c.kwargs["json"]["text"] for c in post.call_args_list),text)
        self.assertTrue(all(len(c.kwargs["json"]["text"])<=250 for c in post.call_args_list))
        with wave.open(str(target),"rb") as result:
            self.assertEqual(result.getnframes(),3*4800)

    def test_duration_rejection_bisects_without_replaying_successful_audio(self):
        target=self.root/"complete.wav"
        text="Первая фраза. Вторая фраза."
        with patch("scripts.tts.requests.post",side_effect=[
                Rejected(400,"duration exceeds the limit"),audio_response(),audio_response()]) as post:
            self.client.synthesize(text,target,**self.options)
        submitted=[c.kwargs["json"]["text"] for c in post.call_args_list]
        self.assertEqual(submitted[0],text)
        self.assertEqual("".join(submitted[1:]),text)
        with wave.open(str(target),"rb") as result:
            self.assertEqual(result.getnframes(),2*4800)

    def test_failed_middle_part_preserves_prior_audio_as_incomplete(self):
        target=self.root/"complete.wav"
        with patch("scripts.tts.requests.post",side_effect=[
                audio_response(),Rejected(403,"sensitive response")]):
            with self.assertRaises(SpeechError) as cm:
                self.client.synthesize("я"*501,target,**self.options)
        self.assertFalse(target.exists())
        self.assertTrue(Path(cm.exception.partial_dir).is_dir())
        self.assertTrue(list(Path(cm.exception.partial_dir).glob("*.wav")))
        self.assertEqual(self.client.last_synthesis["status"],"incomplete")

    def test_native_file_sdk_assembles_real_ogg_parts_for_both_apis(self):
        from types import SimpleNamespace
        from tests.test_tts_audio import encoded_tone, decoded_pcm, tone_strength, measured_frame_samples
        parts = [encoded_tone(440), encoded_tone(880)]
        cases = [
            ("1", "я" * 3000, dict(model="general", voice="ermil", role="good")),
            ("3", "я" * 251, dict(model="livetts", voice="sergey", role="sales")),
        ]
        for version, text, selection in cases:
            with self.subTest(api=version):
                target = self.root / ("complete-" + version + ".ogg")
                if version == "1":
                    responses = [SimpleNamespace(status_code=200,
                        headers={"Content-Type": "audio/ogg"}, content=part) for part in parts]
                else:
                    responses = [Response([{"result": {"audioChunk": {
                        "data": base64.b64encode(part).decode()}}}]) for part in parts]
                with patch("scripts.tts.requests.post", side_effect=responses) as post:
                    outputs = self.client.synthesize(text, target, api_version=version,
                        transport="rest", fmt="oggopus", **selection)
                self.assertEqual(outputs, [target])
                self.assertEqual(post.call_count, 2)
                field = "data" if version == "1" else "json"
                self.assertEqual("".join(call.kwargs[field]["text"] for call in post.call_args_list), text)
                self.assertEqual(self.client.last_synthesis["api_version"], version)
                self.assertEqual(self.client.last_synthesis["parts"], 2)
                pcm = decoded_pcm(target.read_bytes())
                expected_frames = sum(len(decoded_pcm(part)) for part in parts)
                self.assertLessEqual(abs(len(pcm) - expected_frames),
                                     max(measured_frame_samples(part) for part in parts))
                self.assertGreater(tone_strength(pcm[2400:7200], 440),
                                   10 * tone_strength(pcm[2400:7200], 880))
                self.assertGreater(tone_strength(pcm[-7200:-2400], 880),
                                   10 * tone_strength(pcm[-7200:-2400], 440))
