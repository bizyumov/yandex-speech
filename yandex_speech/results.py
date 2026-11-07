"""Correlate raw finals/refinements and retain provider provenance."""
from __future__ import annotations

import copy
import math

from .errors import SpeechError


def event_dict(event) -> dict:
    if isinstance(event, dict):
        return copy.deepcopy(event)
    from google.protobuf.json_format import MessageToDict
    return MessageToDict(event, preserving_proto_field_name=True,
                         always_print_fields_with_no_presence=True)


def _seconds(value):
    if value is None:
        return None
    if type(value) not in (int, float, str):
        raise SpeechError("PROVIDER", "invalid provider timing")
    try:
        number = float(value) / 1000
    except (ValueError, TypeError):
        raise SpeechError("PROVIDER", "invalid provider timing") from None
    if not math.isfinite(number) or number < 0:
        raise SpeechError("PROVIDER", "invalid provider timing")
    return number

def _channel(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SpeechError("PROVIDER", "invalid provider channel tag")
    return value


class ResultAccumulator:
    def __init__(self, *, literature_text=True, diarize=False, timestamps=False):
        self.literature_text = literature_text
        self.diarize = diarize
        self.timestamps = timestamps
        self.raw = []
        self.finals = {}
        self.refinements = {}
        self.preview = ""
        self.eou_ms = None
        self.eou_consistent = True

    def feed(self, response):
        event = event_dict(response)
        self.raw.append(event.pop("_raw_provider_event") if "_raw_provider_event" in event else event)
        channel = _channel(event.get("channel_tag"))
        if "partial" in event:
            alternatives = event["partial"].get("alternatives", [])
            self.preview = alternatives[0].get("text", "") if alternatives else ""
        if "final" in event:
            alternatives = event["final"].get("alternatives", [])
            if alternatives:
                index = int(event.get("audio_cursors", {}).get("final_index", 0))
                channel = channel or _channel(event["final"].get("channel_tag"))
                key = (index, channel)
                value = alternatives[0]
                if key in self.finals and self.finals[key] != value:
                    raise SpeechError("PROVIDER", "conflicting provider final index")
                self.finals[key] = value
        if "final_refinement" in event:
            update = event["final_refinement"]
            normalized = update.get("normalized_text", {})
            channel = channel or _channel(normalized.get("channel_tag"))
            alternatives = normalized.get("alternatives", [])
            if alternatives:
                key = (int(update.get("final_index", 0)), channel)
                value = alternatives[0]
                if key in self.refinements and self.refinements[key] != value:
                    raise SpeechError("PROVIDER", "conflicting provider refinement index")
                self.refinements[key] = value
        if "eou_update" in event:
            try:
                estimate = float(event["eou_update"]["time_ms"])
                cursors = event.get("audio_cursors", {})
                eou_cursor = float(cursors["eou_time_ms"])
                final_cursor = float(cursors["final_time_ms"])
                consistent = (math.isfinite(estimate) and estimate >= 0
                              and estimate == eou_cursor == final_cursor)
            except (KeyError, TypeError, ValueError, OverflowError):
                estimate, consistent = None, False
            self.eou_ms = estimate
            self.eou_consistent &= consistent

    def result(self, *, completion_kind="recorded-file", source_info=None):
        origin = (source_info or {}).get("timeline_offset_seconds", 0.0)
        if type(origin) not in (int, float) or not math.isfinite(origin) or origin < 0:
            raise SpeechError("CONFIG", "invalid original-media timestamp origin")
        segments = []
        missing = False
        for (index, channel), raw in self.finals.items():
            refined = self.refinements.get((index, channel))
            selected = refined if self.literature_text and refined else raw
            missing |= self.literature_text and refined is None
            text = selected.get("text", "")
            if not text:
                continue
            timed = selected.get("words") or raw.get("words") or []
            words = [{"word": w.get("text", ""), "start": _seconds(w.get("start_time_ms")),
                      "end": _seconds(w.get("end_time_ms"))} for w in timed]
            start = _seconds(raw.get("start_time_ms"))
            end = _seconds(raw.get("end_time_ms"))
            if (start is None or end is None or end <= start) and words:
                start, end = words[0]["start"], words[-1]["end"]
            if start is not None and end is not None and end < start:
                raise SpeechError("PROVIDER", "provider segment ends before it starts")
            for word in words:
                if word["start"] is not None:
                    word["start"] += origin
                if word["end"] is not None:
                    word["end"] += origin
            start = start + origin if start is not None else None
            end = end + origin if end is not None else None
            speaker = channel if self.diarize and channel else None
            segments.append({"text": text, "start": start, "end": end,
                             "speaker": speaker, "speaker_tag": speaker, "words": words,
                             "words_representation": "literary" if refined and selected.get("words") else "raw",
                             "channel_tag": channel or None, "final_index": index,
                             "timing_source": "provider" if start is not None and end is not None else "none",
                             "speaker_source": "provider" if speaker is not None else "none"})
        segments.sort(key=lambda s: (s["start"] is None, s["start"] or 0,
                                     s["final_index"], s["channel_tag"] or ""))
        text = " ".join(s["text"].strip() for s in segments).strip()
        representation = "raw" if not self.literature_text else "raw-unfulfilled" if missing else "literary"
        return {
            "schema_version": 1, "status": "completed" if text else "no_speech",
            "input_complete": True, "completion_kind": completion_kind,
            "utterance_complete": completion_kind == "streamed-utterance",
            "text": text, "segments": segments, "mode": "stream" if completion_kind == "streamed-utterance" else "async",
            "api_version": "3", "transport": "grpc", "raw": copy.deepcopy(self.raw),
            "diarization_requested": self.diarize, "timestamps_requested": self.timestamps,
            "source_info": copy.deepcopy(source_info or {}),
            "metadata_availability": {
                "timing": "provider" if any(s["start"] is not None for s in segments) else "none",
                "speaker": "provider" if any(s["speaker"] is not None for s in segments) else "none",
                "speaker_scope": "job-local", "text_representation": representation,
                "timing_origin": "utterance" if completion_kind == "streamed-utterance" else "original-media",
                "timeline_offset_seconds": origin},
            "uncertainties": [{"kind": "literary-unfulfilled", "message": "requested refinement absent; raw text preserved"}] if missing else [],
        }

def normalize_v2_result(operation, *, parameters, source_info=None):
    """Adapt v2's operation-contained chunks without inventing speaker identity."""
    response = operation.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("chunks", []), list):
        raise SpeechError("PROVIDER", "completed v2 operation has no valid result response")

    def milliseconds(value):
        if value is None:
            return None
        if not isinstance(value, str) or not value.endswith("s"):
            raise SpeechError("PROVIDER", "invalid v2 word duration")
        try:
            seconds = float(value[:-1])
        except ValueError:
            raise SpeechError("PROVIDER", "invalid v2 word duration") from None
        if not math.isfinite(seconds) or seconds < 0:
            raise SpeechError("PROVIDER", "invalid v2 word duration")
        return seconds * 1000

    accumulator = ResultAccumulator(literature_text=False, diarize=False,
                                    timestamps=parameters["timestamps"])
    for index, chunk in enumerate(response.get("chunks", [])):
        if not isinstance(chunk, dict) or not isinstance(chunk.get("alternatives", []), list):
            raise SpeechError("PROVIDER", "invalid v2 result chunk")
        alternatives = chunk.get("alternatives", [])
        if not alternatives:
            continue
        alternative = alternatives[0]
        if not isinstance(alternative, dict) or not isinstance(alternative.get("words", []), list):
            raise SpeechError("PROVIDER", "invalid v2 recognition alternative")
        words = []
        for word in alternative.get("words", []):
            if not isinstance(word, dict):
                raise SpeechError("PROVIDER", "invalid v2 word")
            words.append({"text": word.get("word", ""), "start_time_ms": milliseconds(word.get("startTime")),
                          "end_time_ms": milliseconds(word.get("endTime"))})
        accumulator.feed({"audio_cursors": {"final_index": index},
            "final": {"channel_tag": _channel(chunk.get("channelTag")),
                      "alternatives": [{"text": alternative.get("text", ""), "words": words}]}})
    result = accumulator.result(source_info=source_info)
    result.update(api_version="2", transport="rest", raw=[copy.deepcopy(operation)])
    result["metadata_availability"]["text_representation"] = "literary" if parameters["literature_text"] else "raw"
    return result
