"""Yandex Speech agent-skill runtime; cloud dependencies are loaded on demand."""
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("yandex-speech")
except PackageNotFoundError:
    __version__ = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()

__all__ = ["SpeechTranscriber", "TextToSpeech", "ObjectStorageUploader", "UploadReceipt", "AudioInputSpec", "SpeechError", "__version__"]




def __getattr__(name):
    if name == "SpeechTranscriber":
        from .transcription import SpeechTranscriber
        return SpeechTranscriber
    if name == "TextToSpeech":
        from scripts.tts import TextToSpeech
        return TextToSpeech
    if name in {"ObjectStorageUploader", "UploadReceipt"}:
        from .storage import ObjectStorageUploader, UploadReceipt
        return {"ObjectStorageUploader": ObjectStorageUploader, "UploadReceipt": UploadReceipt}[name]
    if name == "AudioInputSpec":
        from .media import AudioInputSpec
        return AudioInputSpec
    if name == "SpeechError":
        from .errors import SpeechError
        return SpeechError
    raise AttributeError(name)
