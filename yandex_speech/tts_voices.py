"""Published voice compatibility snapshot; explicit legacy default is retained.

Source: https://aistudio.yandex.ru/en/docs/speechkit/tts/voices
Retrieved 2026-10-25. Model families never substitute for each other.
"""
from .errors import SpeechError

CATALOG = {}


def _voice(name, language, versions, roles=(), default=None, model="general"):
    CATALOG[name] = {"language": language, "versions": frozenset(versions),
                     "roles": frozenset(roles), "default_role": default, "model": model}


for name, lang in [("lea","de-DE"),("john","en-US"),("amira","kk-KZ"),
                   ("madi","kk-KZ"),("filipp","ru-RU"),("madi_ru","ru-RU"),("nigora","uz-UZ")]:
    _voice(name,lang,["1","3"])
for name,roles in [("ermil",("neutral","good")),("jane",("neutral","good","evil")),
                   ("omazh",("neutral","evil")),("zahar",("neutral","good")),
                   ("marina",("neutral","whisper","friendly"))]:
    _voice(name,"ru-RU",["1","3"],roles,"neutral")
for name,roles,default in [
    ("dasha",("neutral","good","friendly"),"neutral"),
    ("julia",("neutral","strict"),"neutral"),
    ("lera",("neutral","friendly"),"neutral"),
    ("masha",("good","strict","friendly"),"good"),
    ("alexander",("neutral","good"),"neutral"),
    ("kirill",("neutral","strict","good"),"neutral"),
    ("anton",("neutral","good"),"neutral"),
    ("saule_ru",("neutral","strict","whisper"),"neutral"),
    ("zamira_ru",("neutral","strict","friendly"),"neutral"),
    ("zhanar_ru",("neutral","strict","friendly"),"neutral"),
    ("yulduz_ru",("neutral","strict","friendly","whisper"),"neutral"),
]:
    _voice(name,"ru-RU",["3"],roles,default)
_voice("naomi","he-IL",["3"],("modern","classic"),"modern")
_voice("saule","kk-KZ",["3"],("neutral","strict"),"neutral")
_voice("zhanar","kk-KZ",["3"],("neutral","friendly"),"neutral")
_voice("zamira","uz-UZ",["3"],("neutral","strict","friendly"),"neutral")
_voice("yulduz","uz-UZ",["3"],("neutral","strict","friendly","whisper"),"neutral")
for name,roles,default in [
    ("sofia",("casual","support"),"support"),("vera",("casual","support"),"support"),
    ("vasily",("support","sales"),"support"),("sergey",("support","sales"),"support"),
    ("denis",("casual","formal","neutral","sales"),"neutral"),
    ("irina",("formal","support","narrator","sales","neutral"),"neutral"),
]:
    _voice(name,"ru-RU",["3"],roles,default,"livetts")
# Explicitly retained historical v1 compatibility, not a published current-catalog claim.
_voice("alena","ru-RU",["1"],("neutral","good"),"neutral")


def resolve_voice(options, sources):
    result = dict(options)
    language = result["default_lang"]
    if language == "kk-KK":
        language = "kk-KZ"
        result["default_lang"] = language
    if sources.get("tts.default_voice") == "builtin":
        defaults = {"ru-RU":"alena", "en-US":"john", "kk-KZ":"amira"}
        if language in defaults:
            result["default_voice"] = defaults[language]
    voice = result["default_voice"]
    info = CATALOG.get(voice)
    if info is None:
        raise SpeechError("CONFIG", "voice is absent from the supported compatibility catalog; no voice was substituted")
    if voice=="alena" and language!="ru-RU":
        raise SpeechError("CONFIG", "the retained alena legacy voice requires ru-RU")
    # Catalog language describes the training/primary language, not an exhaustive
    # provider rejection rule. Preserve explicit cross-language voice requests.
    model = result.get("model", "auto")
    if model == "auto":
        model = info["model"]
    if model not in {"general","general:rc","general:deprecated","livetts"}:
        raise SpeechError("CONFIG","unknown synthesis model")
    if (model == "livetts") != (info["model"] == "livetts"):
        raise SpeechError("CONFIG", "voice and synthesis model families are incompatible")
    role = result.get("role", "auto")
    if role == "auto":
        role = result["default_emotion"] if sources.get("tts.default_emotion", "builtin") != "builtin" else None
    if role is not None and role not in info["roles"]:
        raise SpeechError("CONFIG", "role/emotion is incompatible with the selected voice")
    result.update(_model=model, _role=role, _voice_info=info)
    return result


def select_version(options):
    version = options["api_version"]
    if options["text_format"] == "ssml":
        if version == "3" or options["transport"] == "grpc" or options["_model"] == "livetts":
            raise SpeechError("CONFIG", "SSML requires a compatible REST v1 voice")
        version = "1"
    elif version == "auto":
        version = "3" if ("1" not in options["_voice_info"]["versions"]
                          or options["transport"] == "grpc"
                          or options.get("model","auto") != "auto" or options.get("volume",-19)!=-19
                          or options.get("pitch_shift",0)!=0 or options.get("duration_ms",0)>0
                          or options.get("unsafe_mode",False)) else "1"
    if version not in options["_voice_info"]["versions"]:
        raise SpeechError("CONFIG", "voice does not support the selected API version; no substitution occurred")
    if version=="1" and options.get("model","auto") not in {"auto","general"}:
        raise SpeechError("CONFIG","selected model control requires API v3")
    if version=="1" and (options.get("volume",-19)!=-19 or options.get("pitch_shift",0)!=0 or options.get("duration_ms",0)>0):
        raise SpeechError("CONFIG","selected controls require API v3")
    if version == "1" and options["transport"] == "grpc":
        raise SpeechError("CONFIG", "synthesis API v1 requires REST")
    return version
