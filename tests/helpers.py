"""Public synthetic fixtures; no operational audio."""
import math
import struct
import wave
from pathlib import Path


def make_wave(path, seconds=0.1, rate=16000, channels=1, width=2):
    path = Path(path)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        values = []
        for i in range(int(rate * seconds)):
            value = int(5000 * math.sin(2 * math.pi * 440 * i / rate))
            values.append(struct.pack("<h", value) * channels)
        stream.writeframes(b"".join(values))
    return path
