"""Text budgets are measured against the actual selected request representation."""
import re
from urllib.parse import urlencode

from .errors import SpeechError


def v1_form(text, options):
    fmt=options["default_format"]
    data={"ssml" if options.get("text_format")=="ssml" else "text":text,
          "lang":options["default_lang"],"voice":options["default_voice"],
          "format":"lpcm" if fmt=="wav" else fmt,"speed":str(options["default_speed"])}
    role=options.get("_role")
    if role is not None:
        data["emotion"]=role
    if fmt in {"lpcm","wav"}:
        rate=options.get("sample_rate_hertz",48000)
        if rate not in {8000,16000,48000}:
            raise SpeechError("CONFIG","API v1 PCM sample rate must be 8000, 16000 or 48000")
        data["sampleRateHertz"]=str(rate)
    return data


def v1_fits(text,options):
    return len(text)<=5000 and len(urlencode(v1_form(text,options)).encode("utf-8"))<=15000


def _boundary(text,maximum):
    if maximum>=len(text):
        return len(text)
    matches=list(re.finditer(r"(?<=[.!?;:])\s+|\s+",text[:maximum]))
    return matches[-1].end() if matches and matches[-1].end()>0 else maximum


def partition_plain(text,fits):
    if not isinstance(text,str) or not text.strip():
        raise SpeechError("CONFIG","synthesis input must contain speakable text")
    parts=[]
    while text:
        low,high,best=1,len(text),0
        while low<=high:
            middle=(low+high)//2
            if fits(text[:middle]):
                best=middle;low=middle+1
            else:
                high=middle-1
        if best==0:
            raise SpeechError("LIMIT","a minimum text fragment exceeds the selected request budget")
        count=_boundary(text,best)
        parts.append(text[:count])
        text=text[count:]
    return parts


def bisect_text(text):
    if len(text)<=1:
        raise SpeechError("LIMIT","minimum utterance still exceeds the provider duration budget")
    at=_boundary(text,len(text)//2)
    if at<=0 or at>=len(text):
        at=len(text)//2
    return text[:at],text[at:]

def validate_ssml(text):
    import xml.etree.ElementTree as ET
    if not isinstance(text,str) or re.search(r"<!\s*(DOCTYPE|ENTITY)",text,re.I):
        raise SpeechError("CONFIG","DTD/entity declarations are forbidden in SSML")
    try:
        root=ET.fromstring(text)
    except (ET.ParseError,ValueError):
        raise SpeechError("CONFIG","SSML is not well-formed XML") from None
    if root.tag!="speak":
        raise SpeechError("CONFIG","SSML requires one speak root")
    xml_lang="{http://www.w3.org/XML/1998/namespace}lang"
    attributes={"speak":{"version",xml_lang},"p":{xml_lang},"s":{xml_lang},
                "break":{"time","strength"},"phoneme":{"alphabet","ph"},"sub":{"alias"}}
    children={"speak":{"p","s","break","phoneme","sub"},
              "p":{"s","break","phoneme","sub"},"s":{"break","phoneme","sub"}}
    for node in root.iter():
        if node.tag not in attributes or set(node.attrib)-attributes[node.tag]:
            raise SpeechError("CONFIG","unsupported SSML element or attribute")
        if any(child.tag not in children.get(node.tag,set()) for child in node):
            raise SpeechError("CONFIG","unsupported SSML nesting")
        if node is not root and node.tag=="speak":
            raise SpeechError("CONFIG","nested speak elements are unsupported")
        if node.tag=="phoneme" and (node.get("alphabet") not in {"ipa","x-sampa"} or not node.get("ph")):
            raise SpeechError("CONFIG","phoneme requires supported alphabet and pronunciation")
        if node.tag=="sub" and not node.get("alias"):
            raise SpeechError("CONFIG","sub requires a nonempty alias")
        if node.tag=="break":
            if node.get("strength") not in {None,"none","x-weak","weak","medium","strong","x-strong"}:
                raise SpeechError("CONFIG","unsupported break strength")
            if node.get("time") is not None:
                match=re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s)",node.get("time"))
                if match is None or float(match[1])*(1 if match[2]=="ms" else 1000)>5000:
                    raise SpeechError("CONFIG","SSML break time must not exceed five seconds")
    return root


def _ssml_tokens(root):
    import copy
    import xml.etree.ElementTree as ET
    result=[]
    def walk(node,context):
        if node.text:
            result.append((context,node.text,False))
        for child in node:
            if child.tag in {"phoneme","sub","break"} or not len(child) and not child.text:
                isolated=copy.deepcopy(child)
                isolated.tail=None
                result.append((context,ET.tostring(isolated,encoding="unicode"),True))
            else:
                frame=(child.tag,tuple(sorted(child.attrib.items())),id(child))
                walk(child,context+(frame,))
            if child.tail:
                result.append((context,child.tail,False))
    walk(root,())
    return result


def _render_ssml(root_attributes,tokens):
    from xml.sax.saxutils import escape,quoteattr
    def opening(tag,attributes):
        fields=[]
        for name,value in attributes:
            if name=="{http://www.w3.org/XML/1998/namespace}lang":
                name="xml:lang"
            fields.append(" "+name+"="+quoteattr(value))
        return "<"+tag+"".join(fields)+">"
    output=[opening("speak",root_attributes)]
    active=()
    for context,value,atomic in tokens:
        same=0
        while same<min(len(active),len(context)) and active[same]==context[same]:
            same+=1
        output.extend("</"+frame[0]+">" for frame in reversed(active[same:]))
        output.extend(opening(frame[0],frame[1]) for frame in context[same:])
        output.append(value if atomic else escape(value))
        active=context
    output.extend("</"+frame[0]+">" for frame in reversed(active))
    output.append("</speak>")
    return "".join(output)


def partition_ssml(text,fits):
    root=validate_ssml(text)
    attributes=tuple(sorted(root.attrib.items()))
    tokens=_ssml_tokens(root)
    parts=[]
    current=[]
    render=lambda items:_render_ssml(attributes,items)
    for context,value,atomic in tokens:
        if atomic:
            if not fits(render(current+[(context,value,True)])) and current:
                parts.append(render(current));current=[]
            if not fits(render([(context,value,True)])):
                raise SpeechError("LIMIT","indivisible SSML element exceeds the request budget")
            current.append((context,value,True))
            continue
        while value:
            low,high,best=1,len(value),0
            while low<=high:
                middle=(low+high)//2
                if fits(render(current+[(context,value[:middle],False)])):
                    best=middle;low=middle+1
                else:
                    high=middle-1
            if best==0:
                if not current:
                    raise SpeechError("LIMIT","minimum SSML context exceeds the request budget")
                parts.append(render(current));current=[]
                continue
            count=_boundary(value,best)
            current.append((context,value[:count],False))
            value=value[count:]
            if value:
                parts.append(render(current));current=[]
    if current:
        parts.append(render(current))
    if not parts:
        empty=render([])
        if not fits(empty):
            raise SpeechError("LIMIT","SSML wrapper exceeds the request budget")
        parts=[empty]
    return parts


def bisect_ssml(text,fits):
    root=validate_ssml(text)
    import xml.etree.ElementTree as ET
    def weight(document):
        total=0
        for _,value,atomic in _ssml_tokens(document):
            total+=max(1,len("".join(ET.fromstring(value).itertext()))) if atomic else len(value)
        return total
    count=weight(root)
    if count<=1:
        raise SpeechError("LIMIT","minimum SSML utterance cannot satisfy the duration budget")
    parts=partition_ssml(text,lambda s:fits(s) and weight(ET.fromstring(s))<=max(1,count//2))
    if len(parts)<=1:
        raise SpeechError("LIMIT","SSML utterance cannot be partitioned further")
    return parts
