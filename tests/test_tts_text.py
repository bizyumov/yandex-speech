import unittest
from urllib.parse import urlencode

from yandex_speech.errors import SpeechError


class SynthesisTextTests(unittest.TestCase):
    def test_plain_partition_preserves_all_unicode_characters_in_order(self):
        from yandex_speech.tts_text import partition_plain
        for text in ["я"*251, ("Слово, пауза. " * 80), "🙂"*501, "a"*5001]:
            parts=partition_plain(text,lambda value:len(value)<=250)
            self.assertEqual("".join(parts),text)
            self.assertTrue(all(0<len(p)<=250 for p in parts))

    def test_encoded_form_size_is_separate_from_character_limit(self):
        from yandex_speech.tts_text import partition_plain, v1_fits
        options={"default_lang":"ru-RU","default_voice":"alena","default_format":"mp3",
                 "default_speed":1.0,"sample_rate_hertz":48000,"_role":None}
        text="я"*4999
        parts=partition_plain(text,lambda part:v1_fits(part,options))
        self.assertGreater(len(parts),1)
        self.assertEqual("".join(parts),text)
        self.assertTrue(all(v1_fits(p,options) for p in parts))

    def test_duration_bisection_keeps_text_and_never_truncates(self):
        from yandex_speech.tts_text import bisect_text
        text="Первая фраза. Вторая фраза."
        left,right=bisect_text(text)
        self.assertEqual(left+right,text)
        self.assertTrue(left and right)
        with self.assertRaises(SpeechError):
            bisect_text("я")
