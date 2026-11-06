"""Opt-in synthetic evidence for the continuous-stream prerequisite (#38).

Reports contain event counts and timing checks, never credentials or transcripts.
This suite establishes a server contract, not continuous or host support.
"""
import importlib.metadata
import json
import os
import threading
import time
import unittest
import wave

from tests.provider import test_recording
from yandex_speech.grpc_stt import GrpcTransport, build_stream_options, sdk
from yandex_speech.results import event_dict


@unittest.skipUnless(os.environ.get("YANDEX_SPEECH_LIVE_TESTS") == "1",
                     "explicit synthetic provider opt-in required")
class ServerStreamingContractTests(unittest.TestCase):
    setUp = test_recording.LiveRecordingTests.setUp

    def _probe(self, *, external, full_data=False):
        _, p, *_ = sdk()
        with wave.open(str(self.fixtures / "single.wav"), "rb") as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()),
                             (16000, 1, 2))
            spoken = audio.readframes(audio.getnframes())
        # Two identical utterances test index correlation without text deduplication.
        pcm = spoken + b"\0" * 96000 + spoken + b"\0" * 96000
        options = build_stream_options()
        if not external:
            options.session_options.eou_classifier.Clear()
            options.session_options.eou_classifier.default_classifier.SetInParent()
        if full_data:
            options.session_options.recognition_model.audio_processing_type = (
                p.RecognitionModelOptions.FULL_DATA)
        drained = threading.Event()
        sent = 0
        started = time.monotonic()

        def requests():
            nonlocal sent
            yield options
            for offset in range(0, len(pcm), 3200):
                packet = pcm[offset:offset + 3200]
                yield p.StreamingRequest(chunk=p.AudioChunk(data=packet))
                sent += len(packet)
                target = started + sent / 32000
                time.sleep(max(0, target - time.monotonic()))
            if external:
                yield p.StreamingRequest(eou=p.Eou())
            drained.set()

        events = []
        before_eof = 0
        transport = GrpcTransport(self.key)
        try:
            call = transport.open_stream(requests(), timeout=90)
            for response in call:
                item = event_dict(response)
                events.append(item)
                if not drained.is_set() and ("partial" in item or "final" in item):
                    before_eof += 1
        finally:
            transport.close()
        finals = {}
        refined = {}
        for item in events:
            if "final" in item:
                index = int(item.get("audio_cursors", {}).get("final_index", 0))
                alternatives = item["final"].get("alternatives", [])
                if alternatives:
                    finals[index] = alternatives[0]
            if "final_refinement" in item:
                value = item["final_refinement"]
                alternatives = value.get("normalized_text", {}).get("alternatives", [])
                if alternatives:
                    refined[int(value.get("final_index", 0))] = alternatives[0]
        report = {
            "case": self.id(),
            "yandexcloud": importlib.metadata.version("yandexcloud"),
            "grpcio": importlib.metadata.version("grpcio"),
            "processing": "FULL_DATA" if full_data else "REAL_TIME",
            "eou": "external" if external else "server/default",
            "input_bytes": len(pcm),
            "requests_drained": drained.is_set(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "progress_observations_before_eof": before_eof,
            "events": {name: sum(name in item for item in events)
                       for name in ("partial", "final", "final_refinement", "eou_update")},
            "final_indices": sorted(finals),
            "refined_indices": sorted(refined),
            "final_word_counts": {str(i): len(v.get("words", [])) for i, v in finals.items()},
            "refined_word_counts": {str(i): len(v.get("words", [])) for i, v in refined.items()},
            "timing_fields": all("start_time_ms" in v and "end_time_ms" in v
                                 for v in finals.values()),
        }
        # Persist the measured contract even when a prerequisite assertion fails.
        (self.root / "server-contract.json").write_text(json.dumps(report, indent=2))
        self.assertTrue(drained.is_set(), "provider terminated before the input was drained")
        self.assertTrue(finals, "server produced no final alternatives")
        self.assertEqual(set(finals), set(refined),
                         "literary refinements do not correlate to every raw final")
        spoken_finals = {i: value for i, value in finals.items()
                         if value.get("text", "").strip()}
        self.assertTrue(spoken_finals, "server finals contain no speech")
        self.assertTrue(all(v.get("words") for v in spoken_finals.values()),
                        "spoken raw finals lack genuine word timing")
        for value in finals.values():
            for word in value["words"]:
                self.assertLessEqual(float(word.get("start_time_ms", 0)),
                                     float(word.get("end_time_ms", 0)))
        text = " ".join(refined[i].get("text", "") for i in sorted(refined)).casefold()
        self.assertEqual(text.count("контрольный хвост"), 2,
                         "the complete repeated synthetic tail was not retained twice")
        if not full_data:
            self.assertGreater(before_eof, 0, "REAL_TIME returned no progressive results")
        return report

    def test_real_time_server_eou_timing_and_refinement(self):
        self._probe(external=False)

    def test_real_time_external_eou_timing_and_final_drain(self):
        self._probe(external=True)

    def test_full_data_constraints_are_measured_separately(self):
        self._probe(external=False, full_data=True)
