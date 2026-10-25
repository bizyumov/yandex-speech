"""Transcript representations with real timing and atomic outputs."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .artifacts import atomic_write
from .errors import SpeechError


def _cue(segment):
    start, end = segment.get("start"), segment.get("end")
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start):
        raise SpeechError("CONFIG", "subtitles require genuine positive-duration provider timing")
    return start, end


def _stamp(seconds, separator=","):
    total = round(seconds * 1000)
    hours, rem = divmod(total, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}"


def _text(segment):
    text = segment.get("text", "")
    return f"Speaker {segment['speaker']}: {text}" if segment.get("speaker") is not None else text


def render_txt(result):
    if not result.get("timestamps_requested") and not result.get("diarization_requested"):
        return result.get("text", "")
    lines = []
    for segment in result.get("segments", []):
        prefix = ""
        if result.get("timestamps_requested"):
            start, _ = _cue(segment)
            prefix = "[" + _stamp(start).split(",")[0] + "] "
        lines.append(prefix + _text(segment))
    return "\n".join(lines)


def _subtitles(result, vtt=False):
    cues, previous = [], -1
    for index, segment in enumerate(result.get("segments", []), 1):
        start, end = _cue(segment)
        if start < previous:
            raise SpeechError("PROVIDER", "subtitle cues are not in source-time order")
        previous = start
        sep = "." if vtt else ","
        cue = f"{_stamp(start, sep)} --> {_stamp(end, sep)}\n{_text(segment)}"
        cues.append(cue if vtt else f"{index}\n{cue}")
    return ("WEBVTT\n\n" if vtt else "") + "\n\n".join(cues) + ("\n" if cues else "")


def render_srt(result):
    return _subtitles(result)


def render_vtt(result):
    return _subtitles(result, vtt=True)


def write_output(result, path, fmt="json", *, overwrite=False):
    target = Path(path)
    original = result.get("source_info", {}).get("original_path")
    if original:
        source = Path(original)
        if target.resolve() == source.resolve() or (target.exists() and source.exists() and target.samefile(source)):
            raise SpeechError("CONFIG", "the output must not replace the original recording")
    if fmt == "json":
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    elif fmt == "txt":
        text = render_txt(result)
    elif fmt == "srt":
        text = render_srt(result)
    elif fmt == "vtt":
        text = render_vtt(result)
    else:
        raise SpeechError("CONFIG", "unknown transcript output format")
    return atomic_write(target, text.encode("utf-8"), overwrite=overwrite)
