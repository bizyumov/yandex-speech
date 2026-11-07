"""Generated protobuf admission, including changing content-length varints."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.test_partition_audio import wav
from yandex_speech import grpc_stt
from yandex_speech.errors import SpeechError
from yandex_speech.media import PreparedAudio
from yandex_speech.partition_audio import inline_frame_capacity


class PartitionGrpcBudgetTests(unittest.TestCase):
    def test_actual_wire_below_at_and_above_the_calculated_budget(self):
        for limit in (4096, 16384, 16390):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.object(grpc_stt, "MAX_GRPC_BYTES", limit):
                    frames = inline_frame_capacity(
                        ("grpc",), root, language="ru-RU", diarize=False,
                        literature_text=True, channels=1)
                    path = root / "admitted.wav"
                    wav(path, frames=frames)
                    prepared = PreparedAudio(
                        path, "WAV", 16000, 1, frames / 16, {"converted": True})
                    request = grpc_stt.build_file_request(prepared)
                    self.assertLessEqual(request.ByteSize(), limit)
                    self.assertLess(limit - request.ByteSize(), 2)
                    with patch.object(grpc_stt, "MAX_GRPC_BYTES", request.ByteSize()):
                        self.assertEqual(grpc_stt.build_file_request(prepared).ByteSize(), request.ByteSize())
                    with patch.object(grpc_stt, "MAX_GRPC_BYTES", request.ByteSize() - 1):
                        with self.assertRaises(SpeechError):
                            grpc_stt.build_file_request(prepared)
                    over = root / "not-admitted.wav"
                    wav(over, frames=frames + 1)
                    with self.assertRaises(SpeechError) as caught:
                        grpc_stt.build_file_request(PreparedAudio(
                            over, "WAV", 16000, 1, (frames + 1) / 16, {"converted": True}))
                    self.assertEqual(caught.exception.category, "LIMIT")
