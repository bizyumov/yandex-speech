import threading
import time
import unittest
from yandex.cloud.ai.tts.v3 import tts_pb2 as p

from yandex_speech.errors import SpeechError


class Call:
    def __init__(self,requests,transport):
        self.requests=requests
        self.transport=transport
        self.stopped=threading.Event()
    def cancel(self):
        self.stopped.set()
    def __iter__(self):
        for request in self.requests:
            self.transport.requests.append(request)
            if request.HasField("force_synthesis"):
                chunks=[b"\0",b"\0\1\0"] if not self.transport.odd else [b"\0"]
                for chunk in chunks:
                    if self.stopped.is_set():return
                    yield p.StreamSynthesisResponse(audio_chunk=p.AudioChunk(data=chunk))


class Transport:
    def __init__(self,odd=False):
        self.odd=odd
        self.requests=[]
        self.calls=[]
    def open_stream(self,requests,timeout):
        call=Call(requests,self)
        self.calls.append(call)
        return call
    def close(self):
        for call in self.calls:call.cancel()


def options():
    return p.SynthesisOptions(model="livetts",voice="sergey",
        output_audio_spec=p.AudioFormatOptions(raw_audio=p.RawAudio(
            audio_encoding=p.RawAudio.LINEAR16_PCM,sample_rate_hertz=24000)))


class ProgressiveSynthesisTests(unittest.TestCase):
    def test_audio_arrives_before_input_eof_and_samples_are_reassembled(self):
        from yandex_speech.tts_stream import AudioStream
        continue_text=threading.Event()
        source_eof=threading.Event()
        self.addCleanup(continue_text.set)
        def source():
            yield "Первая фраза. "
            continue_text.wait(2)
            yield "Контрольный хвост. "
            source_eof.set()
        transport=Transport()
        with AudioStream(source(),transport,options()) as stream:
            first=next(stream)
            self.assertFalse(source_eof.is_set())
            self.assertEqual(first,b"\0\0\1\0")
            continue_text.set()
            rest=b"".join(stream)
            self.assertTrue(rest)
            acknowledgements=sum(r.HasField("force_synthesis") for r in transport.requests)
            self.assertEqual(first+rest,b"\0\0\1\0"*acknowledgements)
            self.assertTrue(stream.input_complete)
        self.assertEqual(transport.requests[0].options.voice,"sergey")
        self.assertEqual(transport.requests[0].options.output_audio_spec.raw_audio.sample_rate_hertz,24000)
        text="".join(r.synthesis_input.text for r in transport.requests if r.HasField("synthesis_input"))
        self.assertEqual(text,"Первая фраза. Контрольный хвост. ")

    def test_rpc_rollover_preserves_every_character(self):
        from yandex_speech.tts_stream import AudioStream
        text="я"*5001
        transport=Transport()
        with AudioStream(text,transport,options()) as stream:
            data=b"".join(stream)
            self.assertTrue(data)
            self.assertTrue(stream.input_complete)
        self.assertEqual(len(transport.calls),2)
        self.assertEqual("".join(r.synthesis_input.text for r in transport.requests if r.HasField("synthesis_input")),text)
        size=0
        for request in transport.requests:
            if request.HasField("options"):
                self.assertLessEqual(size,5000);size=0
            if request.HasField("synthesis_input"):size+=len(request.synthesis_input.text)
        self.assertLessEqual(size,5000)

    def test_audio_empty_speech_tail_rpc_is_not_complete(self):
        from yandex_speech.tts_stream import AudioStream
        class EmptyCall(Call):
            def __iter__(self):
                for request in self.requests:self.transport.requests.append(request)
                return
                yield
        class MissingTail(Transport):
            def open_stream(self,requests,timeout):
                call=Call(requests,self) if not self.calls else EmptyCall(requests,self)
                self.calls.append(call)
                return call
        stream=AudioStream("я"*5001,MissingTail(),options())
        with self.assertRaises(SpeechError) as cm:
            with stream:b"".join(stream)
        self.assertEqual(cm.exception.category,"OUTPUT_PARTIAL")
        self.assertFalse(stream.input_complete)

    def test_explicit_no_speakable_nonspeech_tail_acknowledgement_is_complete(self):
        from yandex_speech.tts_stream import AudioStream
        marker=p.StreamSynthesisResponse.DESCRIPTOR.fields_by_name["chunk_type"].enum_type.values_by_name["NO_SPEAKABLE_TEXT"].number
        class Acknowledged(Call):
            def __iter__(self):
                for request in self.requests:self.transport.requests.append(request)
                yield p.StreamSynthesisResponse(chunk_type=marker)
        class NonspeechTail(Transport):
            def open_stream(self,requests,timeout):
                call=Call(requests,self) if not self.calls else Acknowledged(requests,self)
                self.calls.append(call);return call
        with AudioStream("a"*5000+".",NonspeechTail(),options()) as stream:
            self.assertTrue(b"".join(stream))
            self.assertTrue(stream.input_complete)

    def test_close_after_complete_preserves_success(self):
        from yandex_speech.tts_stream import AudioStream
        stream=AudioStream("Фраза.",Transport(),options())
        b"".join(stream)
        self.assertTrue(stream.input_complete)
        stream.close()
        self.assertTrue(stream.input_complete)
        self.assertEqual(stream.metadata["status"],"complete")

    def test_incomplete_final_sample_fails(self):
        from yandex_speech.tts_stream import AudioStream
        with AudioStream("Тест.",Transport(odd=True),options()) as stream:
            with self.assertRaises(SpeechError) as cm:
                b"".join(stream)
        self.assertEqual(cm.exception.category,"OUTPUT_PARTIAL")

    def test_cancel_stops_owned_threads_and_emits_no_later_audio(self):
        from yandex_speech.tts_stream import AudioStream
        transport=Transport()
        stream=AudioStream(["Фраза. "]*200,transport,options())
        next(stream)
        started=time.monotonic()
        stream.cancel()
        self.assertLess(time.monotonic()-started,2)
        self.assertFalse(any(t.is_alive() for t in stream.workers))
        with self.assertRaises(SpeechError):
            next(stream)
        self.assertFalse(stream.input_complete)

    def test_flushed_speech_without_audio_hits_idle_deadline(self):
        from yandex_speech.tts_stream import AudioStream
        class Stalled(Call):
            def __iter__(self):
                for request in self.requests:
                    if request.HasField("force_synthesis"):
                        self.stopped.wait(2)
                        return
                yield
        class StalledTransport(Transport):
            def open_stream(self,requests,timeout):
                call=Stalled(requests,self);self.calls.append(call);return call
        stream=AudioStream("Фраза.",StalledTransport(),options(),
                           deadline_seconds=5,_audio_idle_seconds=.05)
        started=time.monotonic()
        with self.assertRaises(SpeechError) as cm:
            with stream:next(stream)
        self.assertEqual(cm.exception.category,"TIMEOUT")
        self.assertLess(time.monotonic()-started,1)

class NativeMarkupStreamTests(unittest.TestCase):
    def test_fragmented_native_controls_reach_one_synthesis_input(self):
        from yandex_speech.tts_stream import AudioStream
        for fragments, protected in [
            (["Начало [[m ə ", "l ɐ k o]] конец. "], "[[m ə l ɐ k o]]"),
            (["Стоп! sil<[", "300]> Подумай. "], "Стоп! sil<[300]> Подумай."),
            (["Начало <[acc", "ented]> важное слово. "], "<[accented]> важное"),
        ]:
            with self.subTest(protected=protected):
                transport = Transport()
                with AudioStream(iter(fragments), transport, options()) as stream:
                    b"".join(stream)
                    self.assertTrue(stream.input_complete)
                texts = [r.synthesis_input.text for r in transport.requests
                         if r.HasField("synthesis_input")]
                self.assertEqual("".join(texts), "".join(fragments))
                self.assertTrue(any(protected in text for text in texts), texts)

    def test_rpc_rollover_keeps_a_pause_and_its_neighbors_together(self):
        from yandex_speech.tts_stream import AudioStream
        group = "Стоп! sil<[300]> Подумай."
        text = "а" * 4980 + ". " + group + " Хвост."
        transport = Transport()
        with AudioStream(iter([text]), transport, options()) as stream:
            b"".join(stream)
            self.assertTrue(stream.input_complete)
        texts = [r.synthesis_input.text for r in transport.requests
                 if r.HasField("synthesis_input")]
        self.assertEqual("".join(texts), text)
        self.assertTrue(any(group in value for value in texts), texts[-3:])

    def test_stream_rejects_oversized_phoneme_block(self):
        from yandex_speech.tts_stream import AudioStream
        transport = Transport()
        with AudioStream(iter(["[[" + "m " * 2600 + "]]"]), transport, options()) as stream:
            with self.assertRaises(SpeechError) as error:
                b"".join(stream)
            self.assertEqual(error.exception.category, "LIMIT")

class IncrementalNativeMarkupTests(unittest.TestCase):
    def test_a_later_pause_keeps_its_left_context_even_after_delay(self):
        from yandex_speech.tts_stream import AudioStream
        def source():
            yield "Стоп! "
            time.sleep(.7)
            yield "sil<[300]> Подумай. "
        transport = Transport()
        with AudioStream(source(), transport, options()) as stream:
            b"".join(stream)
            self.assertTrue(stream.input_complete)
        texts = [r.synthesis_input.text for r in transport.requests
                 if r.HasField("synthesis_input")]
        self.assertTrue(any("Стоп! sil<[300]> Подумай." in text for text in texts), texts)

    def test_an_opener_split_at_the_rpc_boundary_stays_intact(self):
        from yandex_speech.tts_stream import AudioStream
        text = "a" * 4999 + "[[m ə l ɐ k o]] конец. "
        transport = Transport()
        with AudioStream(iter([text[:5000], text[5000:]]), transport, options()) as stream:
            b"".join(stream)
            self.assertTrue(stream.input_complete)
        texts = [r.synthesis_input.text for r in transport.requests
                 if r.HasField("synthesis_input")]
        self.assertEqual("".join(texts), text)
        self.assertTrue(any("[[m ə l ɐ k o]]" in value for value in texts))

    def test_a_late_stress_mark_does_not_corrupt_an_oversized_word(self):
        from yandex_speech.tts_stream import AudioStream
        with AudioStream(iter(["я" * 5000, "+а конец. "]), Transport(), options()) as stream:
            with self.assertRaises(SpeechError) as error:
                b"".join(stream)
            self.assertEqual(error.exception.category, "LIMIT")

    def test_ambiguous_unfinished_lexical_buffer_is_bounded(self):
        from yandex_speech.tts_stream import AudioStream
        with AudioStream(iter(["я" * 5000] * 5), Transport(), options()) as stream:
            with self.assertRaises(SpeechError) as error:
                b"".join(stream)
            self.assertEqual(error.exception.category, "LIMIT")
