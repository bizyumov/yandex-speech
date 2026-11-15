"""Complete-file audio framing; provider stream headers must be finalized."""
import struct

from .errors import SpeechError


def finalize_wav(data):
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SpeechError("OUTPUT_PARTIAL", "provider did not return a RIFF/WAVE file")
    if len(data) - 8 > 0xFFFFFFFF:
        raise SpeechError("LIMIT", "WAV file exceeds the RIFF size budget")
    declared = struct.unpack_from("<I", data, 4)[0]
    if declared not in (0xFFFFFFFF, len(data) - 8):
        raise SpeechError("OUTPUT_PARTIAL", "known RIFF length disagrees with the complete response")
    fixed = bytearray(data)
    position, alignment, found_data = 12, None, False
    while position + 8 <= len(data):
        tag = data[position:position + 4]
        size = struct.unpack_from("<I", data, position + 4)[0]
        begin = position + 8
        if tag == b"fmt ":
            if size < 16 or begin + size > len(data):
                raise SpeechError("OUTPUT_PARTIAL", "incomplete WAV format chunk")
            encoding, channels, rate, byte_rate, alignment, bits = struct.unpack_from("<HHIIHH", data, begin)
            if (encoding != 1 or channels != 1 or bits != 16 or alignment != 2
                    or rate <= 0 or byte_rate != rate * alignment):
                raise SpeechError("OUTPUT_PARTIAL", "unexpected provider PCM format")
        if tag == b"data":
            if found_data or alignment is None:
                raise SpeechError("OUTPUT_PARTIAL", "invalid WAV data ordering")
            found_data = True
            if size == 0xFFFFFFFF:
                # Streaming WAV places the unknown-length data chunk last.
                size = len(data) - begin
                struct.pack_into("<I", fixed, position + 4, size)
            if size % alignment or begin + size > len(data):
                raise SpeechError("OUTPUT_PARTIAL", "WAV data ends inside a PCM frame or is truncated")
        if begin + size > len(data):
            raise SpeechError("OUTPUT_PARTIAL", "truncated WAV chunk")
        position = begin + size + size % 2
    if not found_data or position != len(data):
        raise SpeechError("OUTPUT_PARTIAL", "incomplete or malformed WAV response")
    struct.pack_into("<I", fixed, 4, len(data) - 8)
    return bytes(fixed)

def wrap_pcm(data, sample_rate):
    import io
    import wave
    if len(data)%2:
        raise SpeechError("OUTPUT_PARTIAL","PCM output ends inside a sample")
    stream=io.BytesIO()
    with wave.open(stream,"wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(data)
    return stream.getvalue()

def finalize_audio(data, fmt):
    """Validate complete provider framing without making a decoder a runtime dependency."""
    if not data:
        raise SpeechError("OUTPUT_PARTIAL", "provider returned no audio")
    if fmt == "wav":
        import io
        import wave
        result = finalize_wav(data)
        with wave.open(io.BytesIO(result), "rb") as audio:
            if audio.getnframes() == 0:
                raise SpeechError("OUTPUT_PARTIAL", "WAV contains no audio samples")
        return result
    if fmt == "lpcm":
        if len(data) % 2 or data.startswith((b"RIFF", b"OggS", b"ID3")):
            raise SpeechError("OUTPUT_PARTIAL", "invalid headerless PCM framing")
    elif fmt == "oggopus":
        _validate_ogg(data)
    elif fmt == "mp3":
        _validate_mp3(data)
    else:
        raise SpeechError("CONFIG", "unsupported output format")
    return data


def _validate_ogg(data):
    offset, serial, sequence, finished = 0, None, 0, False
    packet_size, packet_prefix, audio_packets = 0, b"", 0
    while offset < len(data):
        if finished or len(data) - offset < 27 or data[offset:offset+5] != b"OggS\x00":
            raise SpeechError("OUTPUT_PARTIAL", "incomplete Ogg page/header")
        flags = data[offset+5]
        current_serial, current_sequence = struct.unpack_from("<II", data, offset+14)
        segments = data[offset+26]
        body = offset + 27 + segments
        if body > len(data):
            raise SpeechError("OUTPUT_PARTIAL", "incomplete Ogg segment table")
        end = body + sum(data[offset+27:body])
        if end > len(data) or current_sequence != sequence:
            raise SpeechError("OUTPUT_PARTIAL", "incomplete or unordered Ogg pages")
        if serial is None:
            serial = current_serial
            if not flags & 2 or data[body:body+8] != b"OpusHead":
                raise SpeechError("OUTPUT_PARTIAL", "provider Ogg is not one complete Opus container")
        elif current_serial != serial or flags & 2:
            raise SpeechError("OUTPUT_PARTIAL", "independent Ogg streams require assembly")
        if bool(flags & 1) != bool(packet_size):
            raise SpeechError("OUTPUT_PARTIAL", "inconsistent Ogg packet continuation")
        position = body
        for size in data[offset+27:body]:
            if len(packet_prefix) < 8:
                packet_prefix += data[position:position+min(size, 8-len(packet_prefix))]
            packet_size += size
            position += size
            if size < 255:
                if packet_size and packet_prefix not in (b"OpusHead", b"OpusTags"):
                    audio_packets += 1
                packet_size, packet_prefix = 0, b""
        sequence += 1
        finished = bool(flags & 4)
        offset = end
    if not finished or packet_size or not audio_packets:
        raise SpeechError("OUTPUT_PARTIAL", "Ogg stream is incomplete or contains no audio packets")


def _validate_mp3(data):
    offset, frames = 0, 0
    if data.startswith(b"ID3"):
        if len(data) < 10 or any(value & 128 for value in data[6:10]):
            raise SpeechError("OUTPUT_PARTIAL", "invalid MP3 ID3 header")
        size = sum(value << shift for value, shift in zip(data[6:10], (21, 14, 7, 0)))
        offset = 10 + size + (10 if data[5] & 16 else 0)
    while offset < len(data):
        if len(data) - offset == 128 and data[offset:offset+3] == b"TAG":
            offset += 128
            break
        if len(data) - offset < 4:
            raise SpeechError("OUTPUT_PARTIAL", "truncated MP3 frame header")
        header = int.from_bytes(data[offset:offset+4], "big")
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate, rate = (header >> 12) & 15, (header >> 10) & 3
        if header >> 21 != 0x7ff or version == 1 or layer != 1 or bitrate in (0, 15) or rate == 3:
            raise SpeechError("OUTPUT_PARTIAL", "invalid MP3 Layer III frame")
        rates = (44100, 48000, 32000)
        sample_rate = rates[rate] // (1 if version == 3 else 2 if version == 2 else 4)
        bitrates = ((0,32,40,48,56,64,80,96,112,128,160,192,224,256,320)
                    if version == 3 else (0,8,16,24,32,40,48,56,64,80,96,112,128,144,160))
        length = (144000 if version == 3 else 72000) * bitrates[bitrate] // sample_rate + ((header >> 9) & 1)
        offset += length
        frames += 1
        if offset > len(data):
            raise SpeechError("OUTPUT_PARTIAL", "truncated MP3 audio frame")
    if not frames or offset != len(data):
        raise SpeechError("OUTPUT_PARTIAL", "MP3 contains no complete audio frames")

def join_audio(parts, fmt, sample_rate, *, timeout=120):
    if not parts:
        raise SpeechError("PROVIDER","no synthesized parts to assemble")
    if len(parts)==1 and fmt!="lpcm":
        return parts[0]
    if fmt=="lpcm":
        if any(len(p)%2 for p in parts):
            raise SpeechError("OUTPUT_PARTIAL","PCM part ends inside a sample")
        return b"".join(parts)
    if fmt=="wav":
        import io
        import wave
        pcm=[]
        parameters=None
        for part in parts:
            try:
                with wave.open(io.BytesIO(finalize_wav(part)),"rb") as audio:
                    current=(audio.getnchannels(),audio.getsampwidth(),audio.getframerate())
                    if parameters is not None and current!=parameters:
                        raise SpeechError("OUTPUT_PARTIAL","WAV parts have incompatible audio specifications")
                    parameters=current
                    pcm.append(audio.readframes(audio.getnframes()))
            except (wave.Error,EOFError):
                raise SpeechError("OUTPUT_PARTIAL","malformed WAV part") from None
        return wrap_pcm(b"".join(pcm),parameters[2])
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    executable=shutil.which("ffmpeg")
    if executable is None:
        raise SpeechError("PREREQUISITE","ffmpeg is required to assemble complete multipart encoded output")
    with tempfile.TemporaryDirectory(prefix="yandex-speech-assemble-") as directory:
        root=Path(directory)
        local_format = "ogg" if fmt == "oggopus" else fmt
        files=[]
        for i,part in enumerate(parts):
            path=root/f"part-{i:06d}.{local_format}"
            path.write_bytes(part)
            files.append(path)
        manifest=root/"concat.txt"
        if any("\n" in str(p) or "\r" in str(p) for p in files):
            raise SpeechError("CONFIG","temporary storage path contains unsupported line breaks")
        def quote(path):
            return str(path).replace("'","'\\''")
        manifest.write_text("".join("file '"+quote(p)+"'\n" for p in files))
        target=root/("complete."+local_format)
        try:
            result=subprocess.run([executable,"-nostdin","-v","error","-f","concat",
                "-safe","0","-i",str(manifest),"-c:a","copy",str(target)],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=max(.001,timeout))
        except subprocess.TimeoutExpired:
            raise SpeechError("TIMEOUT","multipart audio assembly exceeded its deadline") from None
        if result.returncode or not target.is_file() or target.stat().st_size==0:
            raise SpeechError("OUTPUT_PARTIAL","cannot assemble a complete encoded audio file")
        return target.read_bytes()
