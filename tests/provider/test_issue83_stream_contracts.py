"""Opt-in provider contract probes; these do not commission a native agent."""
import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1",
                     "explicit provider opt-in required")
class LiveStreamContractTests(unittest.TestCase):
    def test_selected_model_role_and_output_framing(self):
        import grpc
        from yandex.cloud.ai.tts.v3 import tts_pb2 as p, tts_service_pb2_grpc as service
        from yandex_speech.tts_audio import finalize_wav
        profile = Path(os.environ["HERMES_HOME"])
        self.assertEqual(os.geteuid(), profile.stat().st_uid)
        self.assertEqual(Path(sys.prefix).resolve(), Path(os.environ["VIRTUAL_ENV"]).resolve())
        destination = Path(os.environ["YANDEX_SPEECH_TEST_OUTPUT_DIR"]) / ("stream-contract-" + uuid.uuid4().hex)
        destination.mkdir(parents=True)
        text = ("Проверяем полный текст, произношение и порядок фраз. " * 9 +
                "Последняя фраза: контрольный хвост сохранён.")
        observations = []
        for voice, model, role in (("ermil", "general", "good"),
                                   ("sergey", "livetts", "sales"),
                                   ("alena", "general", "neutral")):
            for fmt in ("oggopus", "mp3", "wav", "lpcm"):
                with self.subTest(voice=voice, model=model, role=role, format=fmt):
                    name = voice + "-" + fmt
                    directory = destination / name
                    directory.mkdir()
                    row = {"voice": voice, "model": model, "role": role,
                           "format": fmt, "text_characters": len(text),
                           "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "status": "started"}
                    observations.append(row)
                    started = time.monotonic()
                    channel = grpc.secure_channel("tts.api.cloud.yandex.net:443",
                        grpc.ssl_channel_credentials(),
                        options=[("grpc.max_receive_message_length", 64 * 1024 * 1024)])
                    call = None
                    drained = False
                    try:
                        grpc.channel_ready_future(channel).result(timeout=10)
                        specification = (p.AudioFormatOptions(raw_audio=p.RawAudio(
                            audio_encoding=p.RawAudio.LINEAR16_PCM, sample_rate_hertz=48000))
                            if fmt == "lpcm" else p.AudioFormatOptions(container_audio=p.ContainerAudio(
                                container_audio_type={"oggopus": p.ContainerAudio.OGG_OPUS,
                                    "mp3": p.ContainerAudio.MP3, "wav": p.ContainerAudio.WAV}[fmt])))
                        options = p.SynthesisOptions(model=model, voice=voice, role=role,
                                                      output_audio_spec=specification)
                        def requests():
                            nonlocal drained
                            yield p.StreamSynthesisRequest(options=options)
                            yield p.StreamSynthesisRequest(synthesis_input=p.SynthesisInput(text=text))
                            yield p.StreamSynthesisRequest(force_synthesis=p.ForceSynthesisEvent())
                            drained = True
                        call = service.SynthesizerStub(channel).StreamSynthesis(requests(),
                            metadata=(("authorization", "Api-Key " + os.environ["YANDEX_SPEECH_API_KEY"]),),
                            timeout=90)
                        chunks = []
                        records = []
                        first_audio = None
                        for response in call:
                            audio = bytes(response.audio_chunk.data)
                            records.append({"bytes": len(audio), "text_characters": len(response.text_chunk.text),
                                "chunk_type": int(response.chunk_type), "word_timings": len(response.word_timings)})
                            if audio:
                                if first_audio is None:
                                    first_audio = time.monotonic() - started
                                chunks.append(audio)
                        self.assertTrue(drained, "provider ended before request input EOF")
                        self.assertTrue(chunks, "provider returned no audio")
                        data = b"".join(chunks)
                        if fmt == "wav":
                            data = finalize_wav(data)
                        if fmt == "lpcm":
                            self.assertEqual(len(data) % 2, 0)
                        output = directory / ("complete." + {"oggopus": "ogg", "lpcm": "pcm"}.get(fmt, fmt))
                        output.write_bytes(data)
                        hints = ["-f", "s16le", "-ar", "48000", "-ac", "1"] if fmt == "lpcm" else []
                        decode = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror",
                            *hints, "-i", str(output), "-f", "null", "-"],
                            capture_output=True, timeout=30)
                        self.assertEqual(decode.returncode, 0, "complete stream failed strict decoding")
                        probe = subprocess.run(["ffprobe", "-v", "error", *hints,
                            "-show_entries", "format=duration,format_name:stream=codec_name,sample_rate",
                            "-of", "json", str(output)], capture_output=True, check=True, timeout=15)
                        row.update(status="passed", bytes=len(data), audio_records=len(chunks),
                            records=records, first_audio_seconds=first_audio,
                            final_seconds=time.monotonic() - started, input_eof=True,
                            sha256=hashlib.sha256(data).hexdigest(), probe=json.loads(probe.stdout),
                            ogg_bos_pages=sum(1 for i in range(len(data) - 5)
                                if data[i:i+4] == b"OggS" and data[i+5] & 2),
                            riff_headers=data.count(b"RIFF"))
                    except BaseException as error:
                        row.update(status="failed", error_type=type(error).__name__,
                            grpc_code=error.code().name if isinstance(error, grpc.RpcError) else None)
                        raise
                    finally:
                        if call is not None:
                            call.cancel()
                        channel.close()
                        (destination / "receipt.json").write_text(json.dumps({
                            "contract_only": True, "native_commissioned": False,
                            "interpreter": sys.executable, "principal_uid": os.geteuid(),
                            "observations": observations}, ensure_ascii=False, indent=2))
                        print(json.dumps({key: row.get(key) for key in
                            ("voice", "model", "role", "format", "status", "bytes", "audio_records",
                             "ogg_bos_pages", "riff_headers", "grpc_code")}), flush=True)
