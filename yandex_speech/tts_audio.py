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
