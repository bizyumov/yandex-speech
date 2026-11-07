"""Sample-exact v1 windows and conservative adjacent raw-text reconciliation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import SpeechError

SAMPLE_RATE = 16000
_WINDOW_SAMPLES = 30 * SAMPLE_RATE
_STRIDE_SAMPLES = 25 * SAMPLE_RATE
_EDGE_PUNCTUATION = '.,!?;:()[]{}"«»„“”…'


@dataclass(frozen=True)
class PCMWindow:
    index: int
    start_sample: int
    end_sample: int

    @property
    def byte_length(self) -> int:
        return (self.end_sample - self.start_sample) * 2

    def metadata(self, timeline_offset: float = 0.0) -> dict:
        return {"index": self.index, "start_sample": self.start_sample,
                "end_sample": self.end_sample, "start_seconds": self.start_sample / SAMPLE_RATE,
                "end_seconds": self.end_sample / SAMPLE_RATE,
                "source_start_seconds": timeline_offset + self.start_sample / SAMPLE_RATE,
                "source_end_seconds": timeline_offset + self.end_sample / SAMPLE_RATE,
                "size_bytes": self.byte_length, "format": "LPCM",
                "sample_rate_hertz": SAMPLE_RATE, "channels": 1}


def pcm_windows(total_samples: int):
    if type(total_samples) is not int or total_samples < 0:
        raise SpeechError("STATE", "prepared sample count must be a nonnegative integer")
    start = index = 0
    while start < total_samples:
        end = min(start + _WINDOW_SAMPLES, total_samples)
        yield PCMWindow(index, start, end)
        if end == total_samples:
            break
        start += _STRIDE_SAMPLES
        index += 1


def _tokens(text: str):
    return [(match.start(), match.end(), match.group().strip(_EDGE_PUNCTUATION).casefold())
            for match in re.finditer(r"\S+", text)]


def _alignment(previous: str, current: str):
    left, right = _tokens(previous), _tokens(current)
    before, after = [t[2] for t in left], [t[2] for t in right]
    maximum = min(len(before), len(after))
    exact = [size for size in range(1, maximum + 1)
             if all(before[-size:]) and before[-size:] == after[:size]]
    if len(exact) == 1 and exact[0] >= 2:
        return "overlap-text-only", exact[0], left, right
    if exact:
        return "overlap-ambiguous", 0, left, right
    partial = []
    for size in range(3, maximum + 1):
        tail, head = before[-size:], after[:size]
        if (all(tail) and tail[:-1] == head[:-1] and len(tail[-1]) >= 4
                and head[-1] != tail[-1] and head[-1].startswith(tail[-1])):
            partial.append(size)
    if len(partial) == 1:
        return "overlap-cut-word", partial[0], left, right
    return "overlap-unresolved", 0, left, right


def assemble_text(hypotheses: list[str]) -> dict:
    if any(not isinstance(text, str) for text in hypotheses):
        raise SpeechError("PROVIDER", "v1 recognition returned a non-text hypothesis")
    pieces = []
    uncertainties = []
    for index, original in enumerate(hypotheses):
        text = original.strip()
        previous = hypotheses[index - 1].strip() if index else ""
        if not previous or not text:
            pieces.append(text)
            continue
        kind, count, left, right = _alignment(previous, text)
        selected = text
        if kind == "overlap-text-only":
            selected = text[right[count - 1][1]:].lstrip()
        elif kind == "overlap-cut-word":
            last_piece = next((i for i in range(len(pieces) - 1, -1, -1) if pieces[i]), None)
            tokens = _tokens(pieces[last_piece]) if last_piece is not None else []
            if tokens and tokens[-1][2] == left[-1][2]:
                pieces[last_piece] = pieces[last_piece][:tokens[-1][0]].rstrip()
                selected = text[right[count - 1][0]:]
            else:
                kind, count = "overlap-unresolved", 0
        pieces.append(selected)
        uncertainties.append({
            "kind": kind, "window_index": index, "matched_tokens": count,
            "timing_source": "none", "left_hypothesis": hypotheses[index - 1],
            "right_hypothesis": original,
            "selection": "adjacent-match" if count else "retain-both",
            "message": "v1 text has no word-time proof; raw alternatives are retained",
        })
    return {"text": " ".join(piece for piece in pieces if piece),
            "uncertainties": uncertainties, "raw_hypotheses": list(hypotheses)}
