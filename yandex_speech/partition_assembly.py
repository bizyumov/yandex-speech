"""Conservative reconciliation of adjacent, timed asynchronous recording parts."""
from __future__ import annotations

import copy
import math

from .errors import SpeechError
from .overlap import _tokens
from .partition_jobs import MODE

_TOLERANCE = 0.25
_EDGE_WORDS = 256


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _shift(segment, offset, part, diarize):
    result = copy.deepcopy(segment)
    for item in [result, *result.get("words", [])]:
        for field in ("start", "end"):
            value = item.get(field)
            if value is not None:
                if not _time(value):
                    raise SpeechError("PROVIDER", "partition returned an invalid word or segment time")
                item[field] = value + offset
    speaker = result.get("speaker")
    if speaker is not None:
        label = f"part-{part['part_id']}/{speaker}"
        result.update(speaker=label, speaker_tag=label, speaker_scope=part["child_job_id"],
                      provider_speaker_tag=speaker)
    result.update(part_id=part["part_id"], child_job_id=part["child_job_id"])
    return result


def _units(segment):
    tokens, words = _tokens(segment.get("text", "")), segment.get("words", [])
    if len(tokens) != len(words) or not tokens:
        return None
    units = []
    previous = -1
    for token, word in zip(tokens, words):
        form = _tokens(word.get("word", ""))
        start, end = word.get("start"), word.get("end")
        if (len(form) != 1 or not token[2] or form[0][2] != token[2]
                or not _time(start) or not _time(end) or end <= start or start < previous):
            return None
        previous = start
        units.append((token[2], start, end, segment.get("channel_tag")))
    return units


def _edge(segments, start, end, *, suffix):
    found = []
    ordered = reversed(segments) if suffix else iter(segments)
    for segment in ordered:
        units = _units(segment)
        if units is None:
            break
        selected = []
        for unit in reversed(units) if suffix else units:
            if unit[2] <= start - _TOLERANCE or unit[1] >= end + _TOLERANCE:
                break
            selected.append(unit)
            if len(found) + len(selected) >= _EDGE_WORDS:
                break
        found.extend(selected)
        if len(selected) != len(units) or len(found) >= _EDGE_WORDS:
            break
    return list(reversed(found)) if suffix else found


def _same_time(left, right, diarize, *, clipped_at=None):
    if abs(left[1] - right[1]) > _TOLERANCE or not diarize and left[3] != right[3]:
        return False
    shared = min(left[2], right[2]) - max(left[1], right[1])
    duration = min(left[2] - left[1], right[2] - right[1]) if clipped_at is not None else max(
        left[2] - left[1], right[2] - right[1])
    if shared <= 0 or shared < duration * 0.5:
        return False
    if clipped_at is not None:
        return abs(left[2] - clipped_at) <= _TOLERANCE and right[2] >= left[2]
    return abs(left[2] - right[2]) <= _TOLERANCE


def _trim(segments, count, *, suffix=False):
    while count:
        index = -1 if suffix else 0
        segment = segments[index]
        tokens = _tokens(segment["text"])
        take = min(count, len(tokens))
        count -= take
        if take == len(tokens):
            segments.pop(index)
            continue
        if suffix:
            segment["text"] = segment["text"][:tokens[-take][0]].rstrip()
            segment["words"] = segment["words"][:-take]
        else:
            segment["text"] = segment["text"][tokens[take - 1][1]:].lstrip()
            segment["words"] = segment["words"][take:]
        segment["start"] = segment["words"][0]["start"]
        segment["end"] = segment["words"][-1]["end"]
        segment["assembly_selection"] = "provider-word-time-overlap"


def _reconcile(previous, current, start, end, diarize):
    left = _edge(previous, start, end, suffix=True)
    right = _edge(current, start, end, suffix=False)
    candidates = []
    for count in range(2, min(len(left), len(right)) + 1):
        before, after = left[-count:], right[:count]
        if all(a[0] == b[0] and _same_time(a, b, diarize) for a, b in zip(before, after)):
            candidates.append(("overlap-word-time", count))
    if not candidates:
        for count in range(3, min(len(left), len(right)) + 1):
            before, after = left[-count:], right[:count]
            if (all(a[0] == b[0] and _same_time(a, b, diarize)
                    for a, b in zip(before[:-1], after[:-1]))
                    and len(before[-1][0]) >= 4 and after[-1][0] != before[-1][0]
                    and after[-1][0].startswith(before[-1][0])
                    and _same_time(before[-1], after[-1], diarize, clipped_at=end)):
                candidates.append(("overlap-cut-word-time", count))
    if len(candidates) != 1:
        return {"kind": "overlap-ambiguous" if candidates else "overlap-unresolved",
                "matched_words": 0, "selection": "retain-both",
                "message": "No unique adjacent match with provider word times; both hypotheses retained."}
    kind, count = candidates[0]
    if kind == "overlap-cut-word-time":
        _trim(previous, 1, suffix=True)
        _trim(current, count - 1)
    else:
        _trim(current, count)
    return {"kind": kind, "matched_words": count, "selection": "provider-word-time",
            "time_tolerance_seconds": _TOLERANCE, "minimum_word_overlap_ratio": 0.5}


def assemble_parts(job, results):
    parts = job.parts
    if (not job.data.get("preparation_complete") or len(parts) != len(results)
            or any(part["status"] not in {"completed", "no_speech"} for part in parts)):
        raise SpeechError("OUTPUT_PARTIAL", "every required recording part must complete before assembly")
    origin = job.data["source_info"]["timeline_offset_seconds"]
    diarize = job.data["parameters"]["diarize"]
    segments, uncertainties, boundaries, raw = [], [], [], []
    representations, transports = set(), set()
    for index, (part, child) in enumerate(zip(parts, results)):
        if (child.get("input_complete") is not True or child.get("status") not in {"completed", "no_speech"}
                or child.get("api_version") != "3" or child.get("mode") != "async"
                or child.get("operation_id") != part["operation_id"]
                or child.get("job_id") != part["child_job_id"]
                or child.get("source_info", {}).get("timeline_offset_seconds", 0) != 0):
            raise SpeechError("OUTPUT_PARTIAL", "child result is incomplete or belongs to a different operation")
        shifted = [_shift(segment, origin + part["offset"], part, diarize)
                   for segment in child.get("segments", [])]
        if child.get("text", "").strip() and not shifted:
            raise SpeechError("OUTPUT_PARTIAL", "a nonempty child result has no selected text segments")
        if index and segments and shifted:
            prior = parts[index - 1]
            start, end = origin + part["offset"], origin + prior["end_frame"] / 16000
            note = _reconcile(segments, shifted, start, end, diarize)
            note.update(left_part_id=prior["part_id"], right_part_id=part["part_id"],
                        overlap_start=start, overlap_end=end)
            boundaries.append(note)
            if note["selection"] == "retain-both":
                uncertainties.append(copy.deepcopy(note))
        segments.extend(shifted)
        uncertainties.extend(dict(copy.deepcopy(note), part_id=part["part_id"])
                             for note in child.get("uncertainties", []))
        representations.add(child.get("metadata_availability", {}).get("text_representation", "raw-unfulfilled"))
        transports.add(child["transport"])
        raw.append({"part_id": part["part_id"], "child_job_id": part["child_job_id"],
                    "operation_id": part["operation_id"], "result": copy.deepcopy(child)})
    segments.sort(key=lambda s: (s.get("start") is None, s.get("start") or 0,
                                 int(s["part_id"]), str(s.get("channel_tag") or "")))
    text = " ".join(segment["text"].strip() for segment in segments if segment.get("text", "").strip())
    representation = next(iter(representations)) if len(representations) == 1 else "mixed"
    return {"schema_version": 1, "status": "completed" if text else "no_speech",
            "input_complete": True, "completion_kind": "recorded-file", "utterance_complete": False,
            "mode": MODE, "api_version": "3",
            "transport": next(iter(transports)) if len(transports) == 1 else "mixed" if transports else job.data["transport"],
            "text": text, "segments": segments, "raw": raw, "uncertainties": uncertainties,
            "assembly_boundaries": boundaries, "parts": copy.deepcopy(parts),
            "operation_ids": list(job.operation_ids), "job_id": job.job_id,
            "job_manifest_path": str(job.manifest_path),
            "source_info": copy.deepcopy(job.data["source_info"]),
            "routing": copy.deepcopy(job.data.get("routing", {})),
            "timestamps_requested": job.data["parameters"]["timestamps"], "diarization_requested": diarize,
            "metadata_availability": {
                "timing": "provider" if any(segment.get("start") is not None for segment in segments) else "none",
                "speaker": "provider" if any(segment.get("speaker") is not None for segment in segments) else "none",
                "speaker_scope": "partition-job-local", "text_representation": representation,
                "timing_origin": "original-media", "timeline_offset_seconds": origin}}
