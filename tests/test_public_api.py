import subprocess
import sys
import unittest


class PublicApiTests(unittest.TestCase):
    def test_version_and_public_interfaces(self):
        import yandex_speech
        from yandex_speech import AudioInputSpec, SpeechError, SpeechTranscriber, TextToSpeech
        from pathlib import Path
        self.assertEqual(yandex_speech.__version__, Path("VERSION").read_text().strip())
        self.assertTrue(callable(SpeechTranscriber))
        self.assertTrue(callable(TextToSpeech))
        self.assertTrue(callable(AudioInputSpec))
        self.assertTrue(issubclass(SpeechError, Exception))

    def test_base_interfaces_load_when_grpc_profile_is_unavailable(self):
        code = """
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'grpc', 'grpc_tools', 'yandex', 'yandexcloud'}:
            raise ImportError('optional profile unavailable')
sys.meta_path.insert(0, BlockOptional())
from yandex_speech import SpeechTranscriber, TextToSpeech, AudioInputSpec, SpeechError, __version__
assert __version__
assert 'grpc' not in sys.modules
from yandex_speech.grpc_stt import sdk
try:
    sdk()
except SpeechError as error:
    assert error.category == 'PREREQUISITE'
else:
    raise AssertionError('missing gRPC must not be reported available')
from integrations.hermes.provider import YandexTranscriptionProvider
from pathlib import Path
import tempfile
with tempfile.TemporaryDirectory() as directory:
    provider=YandexTranscriptionProvider(Path(directory),_secret_resolver=lambda:'synthetic')
    assert provider.is_available()
    assert not provider.streaming_capable
    assert 'gRPC' in provider.streaming_unavailable_reason

"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
