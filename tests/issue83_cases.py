"""Stable complete-reply cases; length silos are independent of host/RPC partitions."""
from dataclasses import dataclass

TAIL = "Контрольный хвост завершён."
LENGTHS = (200, 1000, 5500, 250, 5000)


@dataclass(frozen=True)
class Route:
    name: str
    api_version: str
    transport: str
    file_method: str
    unsafe_mode: bool = False


ROUTES = (
    Route("rest-v1", "1", "rest", "utterance"),
    Route("rest-v3-normal", "3", "rest", "utterance"),
    Route("grpc-v3-normal", "3", "grpc", "utterance"),
    Route("rest-v3-unsafe", "3", "rest", "utterance", True),
    Route("grpc-v3-unsafe", "3", "grpc", "utterance", True),
    Route("grpc-v3-stream", "3", "grpc", "stream"),
)


def length_silo(length):
    if length < 1:
        raise ValueError("speech input must be nonempty")
    if length < 250:
        return "<250"
    if length == 250:
        return "boundary-250"
    if length < 5000:
        return "250-4999"
    if length == 5000:
        return "boundary-5000"
    return ">5000"


def speech_text(length):
    if length < len(TAIL) + 4:
        raise ValueError("comparison input needs room for its tail marker")
    phrase = "Сегодня мы проверяем качество речи и порядок предложений."
    prefix_length = length - len(TAIL) - 1
    count = (prefix_length + 1) // (len(phrase) + 1)
    while True:
        prefix = " ".join([phrase] * count)
        remaining = prefix_length - len(prefix)
        if remaining == 0:
            break
        if remaining >= 3:
            filler_length = remaining - (1 if prefix else 0)
            for final in ("Да.", "Так.", "Итак.", "Верно."):
                rest = filler_length - len(final)
                if rest >= 0 and rest % 4 == 0:
                    prefix += (" " if prefix else "") + "Да. " * (rest // 4) + final
                    break
            else:
                count -= 1
                continue
            break
        count -= 1
    return prefix + " " + TAIL


def native_cases():
    """Matched compatible/general and v3-only/LiveTTS comparisons: 55 positive cases."""
    cases = []
    for voice, model, role in (("ermil", "general", "good"), ("sergey", "livetts", "sales")):
        for length in LENGTHS:
            for route in ROUTES:
                if model == "livetts" and route.api_version == "1":
                    continue
                cases.append({"id": voice + "-" + route.name + "-" + str(length),
                    "voice": voice, "model": model, "role": role, "length": length,
                    "silo": length_silo(length), "text": speech_text(length),
                    "route": route})
    return cases


def alena_cases():
    """Additional changed-capability checks, without inventing a v1-only real voice."""
    return [{"id": "alena-" + route.name + "-200", "voice": "alena", "model": "general",
        "role": "neutral", "length": 200, "silo": "<250", "text": speech_text(200),
        "route": route} for route in ROUTES]



def automatic_cases():
    expected = next(route for route in ROUTES if route.name == "grpc-v3-stream")
    automatic = Route("auto", "auto", "auto", "auto")
    return [{"id": voice + "-auto-" + str(length), "voice": voice, "model": model,
        "role": role, "length": length, "silo": length_silo(length), "text": speech_text(length),
        "route": automatic, "expected_route": expected}
        for voice, model, role in (("ermil", "general", "good"),
                                    ("sergey", "livetts", "sales"),
                                    ("alena", "general", "neutral"))
        for length in LENGTHS]



def rollover_cases():
    route = next(route for route in ROUTES if route.name == "grpc-v3-stream")
    return [{"id": voice + "-sdk-stream-" + str(length), "voice": voice, "model": model,
        "role": role, "length": length, "silo": length_silo(length), "text": speech_text(length),
        "route": route, "outer_max": 10000}
        for voice, model, role in (("ermil", "general", "good"), ("sergey", "livetts", "sales"))
        for length in (5000, 5500)]

def rejection_cases():
    return [
        {"id": "reject-sergey-v1", "voice": "sergey", "model": "livetts", "role": "sales",
         "length": 200, "text": speech_text(200), "route": ROUTES[0]},
        {"id": "reject-stream-over-rest", "voice": "ermil", "model": "general", "role": "good",
         "length": 200, "text": speech_text(200),
         "route": Route("rest-stream", "3", "rest", "stream")},
        {"id": "reject-unsupported-role", "voice": "ermil", "model": "general", "role": "not-a-role",
         "length": 200, "text": speech_text(200), "route": ROUTES[1]},
    ]

def comparison_case_ids():
    return {case["id"] for case in native_cases() + alena_cases() + rollover_cases() + automatic_cases()}
