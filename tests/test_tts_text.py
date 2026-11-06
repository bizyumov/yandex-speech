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

class NativeMarkupBoundaryTests(unittest.TestCase):
    def test_native_controls_and_stressed_words_are_indivisible(self):
        from yandex_speech.tts_text import partition_plain
        controls = ["[[m ə l ɐ k o]]", "**важное слово**",
                    "<[accented]> важное", "сверхдлинное+ударениеслова"]
        for control in controls:
            text = "Начало. " + control + " конец."
            with self.subTest(control=control):
                parts = partition_plain(text, lambda value: len(value) <= len(control) + 2)
                self.assertEqual("".join(parts), text)
                self.assertTrue(any(control in part for part in parts), parts)

    def test_internal_pauses_keep_both_spoken_neighbors(self):
        from yandex_speech.tts_text import partition_plain
        for pause in ["sil<[300]>", "<[medium]>"]:
            group = "Стоп! " + pause + " Подумай."
            text = "Вступление. " + group + " Завершение."
            parts = partition_plain(text, lambda value: len(value) <= len(group) + 1)
            self.assertEqual("".join(parts), text)
            self.assertTrue(any(group in part for part in parts), parts)

    def test_punctuation_is_not_a_spoken_pause_neighbor(self):
        from yandex_speech.tts_text import partition_plain
        for text, limit in [("Стоп! sil<[300]> — Подумай.", 20),
                            ("Стоп! — sil<[300]> Подумай.", 22)]:
            with self.subTest(text=text):
                with self.assertRaises(SpeechError) as error:
                    partition_plain(text, lambda value: len(value) <= limit)
                self.assertEqual(error.exception.category, "LIMIT")

    def test_duration_bisection_preserves_native_blocks_and_pause_context(self):
        from yandex_speech.tts_text import bisect_text
        for block in ["[[m ə l ɐ k o]]", "Стоп! sil<[300]> Подумай.",
                      "<[accented]> важное", "**важное слово**"]:
            text = "До. " + block + " После."
            left, right = bisect_text(text)
            self.assertEqual(left + right, text)
            self.assertTrue(block in left or block in right, (left, right))
        with self.assertRaises(SpeechError):
            bisect_text("[[m ə l ɐ k o]]")

    def test_oversized_indivisible_markup_fails_instead_of_corruption(self):
        from yandex_speech.tts_text import partition_plain
        for text in ["[[" + "m " * 200 + "]]", "я" * 250 + "+а",
                     "**" + "важное " * 60 + "**"]:
            with self.subTest(text=text[:25]):
                with self.assertRaises(SpeechError) as error:
                    partition_plain(text, lambda value: len(value) <= 250)
                self.assertEqual(error.exception.category, "LIMIT")
