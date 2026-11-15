# Spec: deterministic voice-generation selection (#83)

**Status: Specify — proposed algorithm for operator review, 2026-11-15.**
This is the complete review projection of the revised #83 selection contract.
The operator retired the 65-case plan and specified three live generation tests.

## Objective

Select one executable voice-generation operation that preserves the requested
voice, model, role, pronunciation and output. Ordinary short replies use simple
REST v3 when compatible. Longer replies use v3 streaming when available, with
explicit fallbacks when optional dependencies are absent.

This revises #83's selection policy. The current code's unconditional gRPC-first
ranking is the baseline being replaced, not the proposed behavior. The policy
below is a single proposed algorithm for review; routing code is not changed by
publishing it.

| Decision factor | Effect |
|---|---|
| Selected voice/model/role and required pronunciation controls | Keep only routes that support those exact choices. Never change the voice to obtain another route. |
| Plain/native markup or XML SSML | SSML requires a compatible REST v1 route. Native stress/pronunciation marks alone do not force v1. |
| Completed file or progressive speech | Progressive speech requires a compatible gRPC v3 stream and the stock PCM sink. Completed files can use any eligible file producer. |
| Explicit API, transport, file method and unsafe mode | These constrain the candidate set. Contradictory choices produce an error. Unsafe mode is never enabled implicitly. |
| Text character count and complete v1 encoded form size | Determine request partitions. The v1 byte budget includes every form parameter. |
| Installed gRPC runtime, protobuf and method stubs | Missing streaming or utterance libraries takes the corresponding No branch. The baseline HTTP runtime is not an API-selection factor. |
| Requested format/rate and required output assembly | Exclude routes that cannot complete that output with available tools before sending paid work. |

| Possible outcome | Contract |
|---|---|
| REST v3 UtteranceSynthesis | Normal requests: at most 250 characters and the independent 24-second limit; explicit unsafe mode: up to 5,000 characters. Complete file. |
| gRPC v3 UtteranceSynthesis | Same normal/explicit-unsafe admission; used when the remaining explicit constraints/readiness select it. Complete file. |
| gRPC v3 StreamSynthesis | Progressive PCM, or complete-file collection. Up to 5,000 characters per planned provider request in this client. |
| REST v1 | Compatible voice and controls, including SSML; at most 5,000 characters and the client's 15,000-byte complete URL-encoded form budget. Complete file. |
| Actionable error | No compatible complete route, missing dependency/assembler/sink, or contradictory selections. No paid first-part experiment. |

Precedence is deterministic:

1. Apply explicit choices and voice/control/output compatibility. A priority never
   overrides these constraints.
2. Establish the complete operation for every remaining route: partitions, required
   assembly and actual interfaces. Remove operations that cannot finish.
3. For progressive speech, select the sole compatible v3 gRPC stream/sink contract.
4. For a file, take the **first remaining route** in the applicable order:

| File request | Ordered preference |
|---|---|
| Normal, at most 250 characters | **REST v3 utterance → gRPC v3 utterance → gRPC v3 stream → REST v1** |
| Normal, more than 250 characters | **gRPC v3 stream → REST v1 → REST v3 utterance → gRPC v3 utterance** |
| Explicit unsafe mode | **REST v3 unsafe utterance → gRPC v3 unsafe utterance**; other methods are excluded |
| SSML | Compatible **REST v1 only** |

Thus v1 compatibility alone never pushes a short reply onto v1. For longer plain
text, v1 is the fallback when the preferred v3 stream cannot complete the operation
and the selected voice/controls permit v1: it avoids ordinary v3's 250-character
partitioning. If v1 is also ineligible, normal v3 uses ordered 250-character parts.
An explicit API v3 choice excludes that v1 fallback.
The provider warns of possible quality degradation with unsafe mode in its
[v3 request contract](https://aistudio.yandex.ru/en/docs/speechkit/tts-v3/api-ref/Synthesizer/utteranceSynthesis),
so unsafe mode remains explicit opt-in.

Count the actual selector input after formatting. Record the whole logical reply
and any Hermes outer chunks separately; undisclosed text is not a selector input.

Text above 5,000 characters is partitioned for the selected route and assembled
into the requested complete file. V1 may require partitioning earlier because of
its encoded-byte budget. Independent MP3/Ogg outputs require an available encoded
joiner; WAV/LPCM assembly uses the existing standard-library path. One complete
provider container requires no joiner just to save it. Selection checks these
requirements before the first paid request.

[Open the full-size decision tree (SVG)](voice-generation-selection.svg) · [Mermaid source](voice-generation-selection.mmd)

```mermaid
flowchart LR
    MODE{"Automatic selection:<br/>requested output / input?"}
    MODE -- "Audio file: plain or native markup" --> UNSAFE{"unsafe mode explicitly enabled?"}
    MODE -- "Audio file: XML SSML" --> XMLVOICE{"Selected voice and controls support v1 SSML?"}
    MODE -- "Speak as audio is generated" --> PLIB{"gRPC streaming libraries installed?"}

    XMLVOICE -- Yes --> V1SIZE{"v1 request fits BOTH limits?<br/>5000 characters and 15000 encoded bytes"}
    XMLVOICE -- No --> STOP
    PLIB -- No --> STOP
    PLIB -- Yes --> PVOICE{"Selected voice / role / format support v3 PCM streaming?"}
    PVOICE -- No --> STOP
    PVOICE -- Yes --> SINK{"Compatible Hermes PCM sink available?"}
    SINK -- Yes --> PCM
    SINK -- No --> STOP

    UNSAFE -- Yes --> UVOICE{"Selected voice / controls support v3 unsafe utterance?"}
    UVOICE -- No --> STOP
    UVOICE -- Yes --> UTRAN{"Transport explicitly fixed to gRPC?"}
    UTRAN -- Yes --> GULIB
    UTRAN -- No --> ULEN{"Text at most 5000 characters?"}
    ULEN -- Yes --> UONE
    ULEN -- No --> UJOIN{"Required audio assembler available?"}
    UJOIN -- Yes --> UPARTS
    UJOIN -- No --> STOP

    UNSAFE -- No --> LEN{"Text length after formatting?"}
    LEN -- "At most 250" --> SV{"Selected voice API support?"}
    LEN -- "251 to 5000" --> MV{"Selected voice API support?"}
    LEN -- "More than 5000" --> LV{"Selected voice API support?"}

    SV -- "v1 only" --> V1VOICE
    SV -- "v3 only" --> SUT{"REST v3 utterance compatible with voice and selections?"}
    SV -- "v1 and v3" --> SUT
    SUT -- Yes --> R3ONE
    SUT -- No --> SGLIB{"gRPC utterance libraries installed?"}
    SGLIB -- No --> MLIB
    SGLIB -- Yes --> SGVOICE{"gRPC v3 utterance compatible with voice and selections?"}
    SGVOICE -- Yes --> GUONE
    SGVOICE -- No --> MLIB

    MV -- "v1 only" --> V1VOICE
    MV -- "v3 only" --> MLIB{"gRPC streaming libraries installed?"}
    MV -- "v1 and v3" --> MLIB
    MLIB -- Yes --> MVOICE{"gRPC v3 streaming compatible with voice and selections?"}
    MLIB -- No --> V1VOICE
    MVOICE -- Yes --> GONE
    MVOICE -- No --> V1VOICE

    LV -- "v1 only" --> V1VOICE
    LV -- "v3 only" --> LLIB{"gRPC streaming libraries installed?"}
    LV -- "v1 and v3" --> LLIB
    LLIB -- No --> V1VOICE
    LLIB -- Yes --> LVOICE{"gRPC v3 streaming compatible with voice and selections?"}
    LVOICE -- No --> V1VOICE
    LVOICE -- Yes --> GJOIN{"Required audio assembler available?"}
    GJOIN -- Yes --> GPARTS
    GJOIN -- No --> STOP

    V1VOICE{"REST v1 compatible with voice and selections?"}
    V1VOICE -- Yes --> V1SIZE
    V1VOICE -- No --> V3VOICE{"REST v3 utterance compatible with voice and selections?"}
    V1SIZE -- Yes --> R1ONE
    V1SIZE -- No --> V1JOIN{"Required audio assembler available?"}
    V1JOIN -- Yes --> R1PARTS
    V1JOIN -- No --> STOP
    V3VOICE -- No --> GULIB{"gRPC utterance libraries installed?"}
    GULIB -- No --> STOP
    GULIB -- Yes --> GUVOICE{"gRPC v3 utterance compatible with voice and selections?"}
    GUVOICE -- No --> STOP
    GUVOICE -- Yes --> GUSIZE{"Fits one utterance request?<br/>250 normal / 5000 explicitly unsafe"}
    GUSIZE -- Yes --> GUONE
    GUSIZE -- No --> GUJOIN{"Required audio assembler available?"}
    GUJOIN -- Yes --> GUPARTS
    GUJOIN -- No --> STOP
    V3VOICE -- Yes --> V3SIZE{"Text at most 250 characters?"}
    V3SIZE -- Yes --> R3ONE
    V3SIZE -- No --> V3JOIN{"Required audio assembler available?"}
    V3JOIN -- Yes --> R3PARTS
    V3JOIN -- No --> STOP

    SV -- "No supported API" --> STOP
    MV -- "No supported API" --> STOP
    LV -- "No supported API" --> STOP

    subgraph APIS["Selected API / generation scenario"]
        R3ONE["REST v3 UtteranceSynthesis<br/>One normal request"]
        GONE["gRPC v3 StreamSynthesis<br/>One request to a complete file"]
        GPARTS["gRPC v3 StreamSynthesis<br/>Ordered 5000-character parts + assembly"]
        R1ONE["REST v1<br/>One text or SSML request"]
        R1PARTS["REST v1<br/>Encoded-size-bounded parts + assembly"]
        R3PARTS["REST v3 UtteranceSynthesis<br/>Ordered 250-character parts + assembly"]
        UONE["REST v3 UtteranceSynthesis<br/>One explicitly unsafe request"]
        UPARTS["REST v3 UtteranceSynthesis<br/>Explicitly unsafe parts + assembly"]
        PCM["gRPC v3 StreamSynthesis<br/>PCM to Hermes playback"]
        GUONE["gRPC v3 UtteranceSynthesis<br/>One request; selected normal or unsafe mode"]
        GUPARTS["gRPC v3 UtteranceSynthesis<br/>Ordered parts + assembly; selected mode"]
        STOP["STOP<br/>Explain unsupported choice or missing prerequisite"]
    end
    classDef decision fill:#f0f5ff,stroke:#3762a6,color:#152b4a;
    classDef api fill:#e8f5e9,stroke:#2e7d32,color:#133b17;
    classDef error fill:#ffebee,stroke:#b71c1c,color:#6a1111;
    class MODE,UNSAFE,XMLVOICE,PLIB,PVOICE,SINK,UVOICE,UTRAN,ULEN,UJOIN,LEN,SV,MV,LV,SUT,SGLIB,SGVOICE,MLIB,MVOICE,LLIB,LVOICE,GJOIN,V1VOICE,V1SIZE,V1JOIN,V3VOICE,GULIB,GUVOICE,GUSIZE,GUJOIN,V3SIZE,V3JOIN decision;
    class R3ONE,GONE,GPARTS,R1ONE,R1PARTS,R3PARTS,UONE,UPARTS,PCM,GUONE,GUPARTS api;
    class STOP error;
```

Each diamond is an actual decision: a length band, the selected voice's API
support, installed gRPC interfaces, exact method compatibility or required assembly.
Each arrow names the factor's value. Each terminal box is one API/generation
scenario in the far-right column; there is no common output box.

The compatibility diamonds include the caller's explicit API, transport and method
constraints as well as the selected voice/model/role, markup and output. No branch
may override an explicit choice. The gRPC utterance path remains available for
compatible explicit gRPC/utterance selections or an excluded REST producer.

The base HTTP dependency, valid configuration and credentials are established
execution prerequisites, not reasons to choose one synthesis API over another.

A definite connection failure before the first synthesis RPC may remove that
transport and repeat this selection with unchanged user choices. Accepted or
uncertain work is not automatically replayed on another route. A definite normal
v3 duration rejection permits bounded splitting on that same method; the duration
limit cannot be inferred from character count alone.

## Tech Stack

Existing Python >=3.11 skill, requests HTTP runtime and optional installed
Yandex gRPC/protobuf interfaces. Already usable generated stubs do not require
installing the proto compiler. Use existing audio collection/assembly and stock
Hermes file/PCM interfaces; introduce no provider, runtime or host source patch.

Provider admission references: [limits](https://aistudio.yandex.ru/en/docs/speechkit/concepts/limits),
[v1 form](https://aistudio.yandex.ru/en/docs/speechkit/tts/request),
[v3 REST utterance](https://aistudio.yandex.ru/en/docs/speechkit/tts-v3/api-ref/Synthesizer/utteranceSynthesis),
and [v3 stream](https://aistudio.yandex.ru/en/docs/speechkit/tts-v3/api-ref/grpc/Synthesizer/streamSynthesis).
The v1 client enforces a 15,000-byte form budget; the vendor describes 15 KB.
Do not promote the client's per-RPC streaming policy into a different service-session limit.

## Commands

Inside the configured standard Hermes runtime:

```sh
python -m unittest tests.test_tts_selection tests.test_tts_dispatch -v
python -m unittest discover -s tests -v
git diff --check
GITMARK_NO_GIT=1 gitmark --root . index
gitmark --root . lint
```

These commands describe verification after the revised algorithm is implemented.
No project/private venv, hardcoded interpreter or dependency installation.

## Project Structure

```text
yandex_speech/tts_selection.py       Complete-route filtering and ordered selection
yandex_speech/tts_capabilities.py    Actual dependency and output-tool readiness
yandex_speech/tts_text.py            Character/encoded-form admission and partitioning
scripts/tts.py                     Selected operation execution and complete publication
integrations/hermes/tts_provider.py Stock file/PCM provider boundary
tests/test_tts_selection.py         Deterministic decision and unavailable-route checks
tests/test_tts_dispatch.py          End-to-end local dispatch/partition outcomes
references/synthesis-provider-testing.md Maintained agent guide after approval
```

Existing #83 requirements remain in its owning task area and GitHub tracker. #98
cleanup work remains independently authorized; it does not decide the TTS route.

## Code Style

Keep explicit immutable request values and ordered complete-route candidates.
Illustrative policy, not a claim of implementation:

```python
for candidate in ordered_routes(request):
    if candidate.supports(request) and candidate.can_complete(request, runtime):
        return candidate
raise SpeechError("PREREQUISITE", "no complete compatible route is ready")
```

Never mutate the selected voice or silently enable unsafe mode during fallback.

## Testing Strategy

Use local deterministic tests for explicit choices, voice/method compatibility,
missing/broken dependencies, SSML, unsafe mode, output formats, encoded v1 bytes,
250/5,000 boundaries and failure disposition. These do not create extra paid cases.

**Exactly three live voice-generation tests on One-ring:**

| Complete reply length | Question and expected default result |
|---|---|
| 200 characters: below 250 | Does the ordinary short reply use REST v3 and deliver the complete selected voice? |
| 1,000 characters: middle band below 5,000 | Does available compatible v3 gRPC streaming avoid ordinary 250-character splitting and deliver the complete reply? |
| 5,500 characters: above 5,000 | Does the selected route partition/assemble the whole reply, with its final tail delivered in the same voice? |

The long case must actually exercise a greater-than-5,000-character selector
input; upstream host chunking into smaller callbacks alone does not prove that
branch. Use the existing isolated One-ring test scope, preserving permanent
production configuration, and record the real call boundary.

Use the same selected voice/model/role and automatic API/transport/method settings
for these three. Record the actual route, whole-input length, completed output and
addressed delivery. Expected gRPC outcomes require the tested runtime's compatible
stream interface; unavailable dependencies are proved locally, not by multiplying
live runs. Staging cleanup can be checked within these same three TTS operations.
There is no live voice/API cross-product or extra 250/5,000 boundary run. The old
65-case matrix is retired, not paused or waiting to resume.

## Boundaries

- Always: preserve selected identity, controls and output; honor explicit constraints;
  preflight the complete operation; report selected route and exclusion reasons.
- Ask first: activation of a revised production policy, new dependencies or host
  changes, additional paid test cases or different user voice/quality choices.
- Never: change voices, enable unsafe implicitly, install a missing runtime during
  generation, return a prefix as complete, replay accepted/uncertain paid work,
  use S3 as a TTS routing requirement or resurrect the retired 65-case matrix.

## Success Criteria

- **AC83-03 revised:** apply the factors, outcomes and exact preference orders above;
  at most 250 characters uses REST v3 by default when it supports the request and
  is ready. Both-version voice support does not select v1 automatically. Explicit
  choices and exact method compatibility remain constraints.
- **AC83-04 retained:** calculate complete request partitions from the selected
  method's character, encoded-body and duration limits; retain order, final tail
  and requested output without unnecessary conversion or incomplete publication.
- **AC83-09/10 retained:** unavailable dependencies or output assembly exclude only
  affected complete routes; choose the first remaining route or diagnose the exact
  prerequisite before paid work. Never claim readiness from package metadata alone.
- **AC83-11 replaced:** exactly the three One-ring cases above prove short, middle
  and long complete replies. Local tests cover decision combinations and exact
  boundaries. Prior acceptance evidence remains historical; it does not validate
  a revised selector. Commissioning and merge require the applicable final evidence.

## Open Questions

The decision for review is the proposed automatic preference order, especially
REST v3 for short replies and compatible REST v1 as the long-file fallback when
usable v3 streaming is absent. Approval advances this revision to implementation
planning; no routing activation or additional live tests are inferred.
