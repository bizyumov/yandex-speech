import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from scripts.tts import TextToSpeech
from yandex_speech.config import RuntimeContext
from yandex_speech.errors import SpeechError


class SsmlSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client=TextToSpeech(api_key="synthetic",
            _context=RuntimeContext(user_home=Path(self.tmp.name)))

    def test_explicit_ssml_sends_only_ssml_through_v1(self):
        text='<speak><p><s>Тест.</s></p><break time="200ms"/><sub alias="пример">пр.</sub><phoneme alphabet="ipa" ph="o">О</phoneme></speak>'
        with patch("scripts.tts.requests.post") as post:
            post.return_value.status_code=200
            post.return_value.headers={"Content-Type":"audio/mpeg"}
            post.return_value.content=b"audio"
            self.client.synthesize_bytes(text,text_format="ssml",transport="rest")
        self.assertEqual(post.call_args.kwargs["data"]["ssml"],text)
        self.assertNotIn("text",post.call_args.kwargs["data"])

    def test_invalid_and_unsupported_xml_fails_before_provider(self):
        bad=['<speak>broken','<!DOCTYPE speak [<!ENTITY x "secret">]><speak>&x;</speak>',
             '<speak><audio src="external"/></speak>','<speak><break time="6s"/></speak>',
             '<speak><phoneme alphabet="unknown" ph="x">x</phoneme></speak>']
        with patch("scripts.tts.requests.post") as post:
            for text in bad:
                with self.subTest(text=text):
                    with self.assertRaises(SpeechError) as cm:
                        self.client.synthesize_bytes(text,text_format="ssml")
                    self.assertEqual(cm.exception.category,"CONFIG")
        post.assert_not_called()

    def test_fixed_v3_or_grpc_ssml_conflict_is_preflight_error(self):
        with patch("scripts.tts.requests.post") as post:
            for options in [{"api_version":"3"},{"transport":"grpc"}]:
                with self.assertRaises(SpeechError):
                    self.client.synthesize_bytes("<speak>Тест.</speak>",text_format="ssml",**options)
        post.assert_not_called()

    def test_plain_angle_brackets_are_not_auto_detected_as_ssml(self):
        with patch("scripts.tts.requests.post") as post:
            post.return_value.status_code=200
            post.return_value.headers={"Content-Type":"audio/mpeg"}
            post.return_value.content=b"audio"
            self.client.synthesize_bytes("a < b",text_format="plain")
        self.assertEqual(post.call_args.kwargs["data"]["text"],"a < b")
        self.assertNotIn("ssml",post.call_args.kwargs["data"])

    def test_long_nested_ssml_remains_well_formed_and_ordered(self):
        from yandex_speech.tts_text import partition_ssml,v1_fits
        options={"default_lang":"ru-RU","default_voice":"alena","default_format":"mp3",
                 "default_speed":1,"text_format":"ssml","_role":None}
        text='<speak><p><s>'+("Пример &amp; проверка. "*700)+'</s><sub alias="конец">финал</sub><break time="100ms"/></p></speak>'
        parts=partition_ssml(text,lambda p:v1_fits(p,options))
        self.assertGreater(len(parts),1)
        self.assertEqual("".join("".join(ET.fromstring(p).itertext()) for p in parts),
                         "".join(ET.fromstring(text).itertext()))
        self.assertTrue(all(v1_fits(p,options) for p in parts))
        self.assertTrue(all(ET.fromstring(p).find("p") is not None for p in parts))

    def test_distinct_adjacent_paragraphs_and_sentences_stay_distinct(self):
        from yandex_speech.tts_text import partition_ssml
        for tag in ["p","s"]:
            text=f"<speak><{tag}>one</{tag}><{tag}>two</{tag}></speak>"
            parts=partition_ssml(text,lambda value:True)
            root=ET.fromstring(parts[0])
            self.assertEqual(len(root.findall(tag)),2)
            self.assertEqual([node.text for node in root.findall(tag)],["one","two"])

    def test_indivisible_overflow_fails_instead_of_dropping_markup(self):
        from yandex_speech.tts_text import partition_ssml
        text='<speak><sub alias="'+("я"*2000)+'">word</sub></speak>'
        with self.assertRaises(SpeechError) as cm:
            partition_ssml(text,lambda p:len(p)<=250)
        self.assertEqual(cm.exception.category,"LIMIT")
