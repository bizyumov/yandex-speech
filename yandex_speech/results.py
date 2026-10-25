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
    try:
        number = float(value) / 1000
    except (ValueError, TypeError):
        raise SpeechError("PROVIDER", "invalid provider timing") from None
    if not math.isfinite(number) or number < 0:
        raise SpeechError("PROVIDER", "invalid provider timing")
    return number


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
        self.raw.append(event)
        channel = str(event.get("channel_tag") or "")
        if "partial" in event:
            alternatives = event["partial"].get("alternatives", [])
            self.preview = alternatives[0].get("text", "") if alternatives else ""
        if "final" in event:
            alternatives = event["final"].get("alternatives", [])
            if alternatives:
                index = int(event.get("audio_cursors", {}).get("final_index", 0))
                channel = channel or str(event["final"].get("channel_tag") or "")
                key = (index, channel)
                value = alternatives[0]
                if key in self.finals and self.finals[key] != value:
                    raise SpeechError("PROVIDER", "conflicting provider final index")
                self.finals[key] = value
        if "final_refinement" in event:
            update = event["final_refinement"]
            normalized = update.get("normalized_text", {})
            channel = channel or str(normalized.get("channel_tag") or "")
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
