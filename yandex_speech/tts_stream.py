"""Bounded duplex synthesis of incremental text into native 24 kHz PCM."""
import queue
import re
import threading
import time

from .errors import SpeechError
from .tts_v3 import grpc_sdk
from .tts_text import native_boundary

_END=object()


class GrpcStreamingTransport:
    def __init__(self,api_key):
        grpc,_,service=grpc_sdk()
        self.grpc=grpc
        self.closed=threading.Event()
        self.ready=None
        self.channel=grpc.secure_channel("tts.api.cloud.yandex.net:443",grpc.ssl_channel_credentials(),
            options=[("grpc.max_receive_message_length",64*1024*1024)])
        self.stub=service.SynthesizerStub(self.channel)
        self.metadata=(("authorization","Api-Key "+api_key),)
        self.call=None
    def open_stream(self,requests,timeout):
        self.ready=self.grpc.channel_ready_future(self.channel)
        if self.closed.is_set():self.ready.cancel()
        try:self.ready.result(timeout=min(10,timeout))
        except self.grpc.FutureTimeoutError:
            raise SpeechError("TRANSPORT","gRPC streaming connection unavailable before submission",code="GRPC_UNREACHABLE") from None
        finally:self.ready=None
        self.call=self.stub.StreamSynthesis(requests,metadata=self.metadata,timeout=timeout)
        if self.closed.is_set():self.call.cancel()
        return self.call
    def close(self):
        self.closed.set()
        ready,call=self.ready,self.call
        if ready is not None:ready.cancel()
        if call is not None:call.cancel()
        self.channel.close()


class AudioStream:
    audio_spec={"encoding":"s16le","sample_rate_hertz":24000,"channels":1,"sample_width_bytes":2}
    def __init__(self,source,transport,options,*,deadline_seconds=1800,_audio_idle_seconds=30):
        import math
        if type(deadline_seconds) not in (int,float) or not math.isfinite(deadline_seconds) or not 0<deadline_seconds<=86400:
            transport.close()
            raise SpeechError("CONFIG","stream deadline must be finite and in (0, 86400] seconds")
        self.source=iter([source] if isinstance(source,str) else source)
        self.transport=transport
        self.options=options
        self.deadline=time.monotonic()+deadline_seconds
        self._text_queue=queue.Queue(maxsize=8)
        self._audio_queue=queue.Queue(maxsize=8)
        self._stop=threading.Event()
        self._done=threading.Event()
        self._lock=threading.RLock()
        self._error=None
        self._started=False
        self._closed=False
        self._cancelled=False
        self._buffer=""
        self._source_eof=False
        self._last_text=time.monotonic()
        self._remainder=""
        self._tail=b""
        self._prefix=b""
        self._header_checked=False
        self._bytes=0
        self._audio_idle_seconds=_audio_idle_seconds
        self._last_flush=0
        self._last_audio=0
        self._waiting_provider=0
        self.workers=[]
        self.input_complete=False
        self.metadata={"status":"prepared"}

    def _check(self):
        if self._stop.is_set():
            raise self._error or SpeechError("CANCELLED","synthesis stream closed")
        if time.monotonic()>=self.deadline:
            raise SpeechError("TIMEOUT","whole synthesis stream deadline expired")

    def _put(self,q,item):
        while True:
            self._check()
            try:q.put(item,timeout=.02);return
            except queue.Full:pass

    def _produce(self):
        try:
            for fragment in self.source:
                self._check()
                if not isinstance(fragment,str):
                    raise SpeechError("CONFIG","text fragments must be strings")
                while len(fragment)>5000:
                    n=native_boundary(fragment,5000,complete=False)
                    if not n:
                        raise SpeechError("LIMIT","an indivisible pronunciation unit exceeds "
                                          "the 5000-character stream request budget")
                    self._put(self._text_queue,fragment[:n])
                    fragment=fragment[n:]
                if fragment:
                    self._put(self._text_queue,fragment)
            self._put(self._text_queue,_END)
        except Exception as error:
            self._fail(error if isinstance(error,SpeechError) else SpeechError("CONFIG","text source failed"))

    def _phrase(self):
        if self._remainder:
            text=self._remainder;self._remainder="";return text
        while True:
            self._check()
            for match in re.finditer(r"(?<=[.!?;:])\s+|\n",self._buffer):
                n=native_boundary(self._buffer,match.end(),complete=self._source_eof)
                if n:
                    text=self._buffer[:n];self._buffer=self._buffer[n:];return text
            if len(self._buffer)>=5000:
                n=native_boundary(self._buffer,5000,complete=self._source_eof)
                if not n:
                    raise SpeechError("LIMIT","an indivisible pronunciation unit exceeds "
                                      "the 5000-character stream request budget")
                text=self._buffer[:n];self._buffer=self._buffer[n:];return text
            if self._source_eof:
                if self._buffer:
                    n=native_boundary(self._buffer,len(self._buffer))
                    text=self._buffer[:n];self._buffer=self._buffer[n:];return text
                return None
            if self._buffer and time.monotonic()-self._last_text>=.5:
                boundaries=list(re.finditer(r"\s+",self._buffer))
                if boundaries:
                    n=native_boundary(self._buffer,boundaries[-1].end(),complete=False)
                    if n:
                        text=self._buffer[:n];self._buffer=self._buffer[n:];return text
            try:fragment=self._text_queue.get(timeout=.02)
            except queue.Empty:continue
            if fragment is _END:self._source_eof=True
            else:self._buffer+=fragment;self._last_text=time.monotonic()

    def _requests(self,first,drained,receipt):
        _,p,_=grpc_sdk()
        yield p.StreamSynthesisRequest(options=self.options)
        used=0
        text=first
        while text is not None:
            self._check()
            available=5000-used
            if len(text)>available:
                n=native_boundary(text,available)
                self._remainder=text[n:]
                if not n:
                    break
                text=text[:n]
            if text:
                receipt["speech"] = receipt["speech"] or bool(text.strip())
                receipt["letters"] = receipt["letters"] or any(c.isalnum() for c in text)
                used+=len(text)
                yield p.StreamSynthesisRequest(synthesis_input=p.SynthesisInput(text=text))
                self._last_flush=time.monotonic()
                yield p.StreamSynthesisRequest(force_synthesis=p.ForceSynthesisEvent())
            if used==5000:break
            text=self._phrase()
        drained.set()

    def _emit(self,data):
        self._last_audio=time.monotonic()
        if not self._header_checked:
            self._prefix+=data
            if b"RIFF".startswith(self._prefix) or self._prefix.startswith(b"RIFF") and len(self._prefix)<12:
                return
            if self._prefix.startswith(b"RIFF") and self._prefix[8:12]==b"WAVE":
                raise SpeechError("OUTPUT_PARTIAL","provider returned a WAV header instead of native PCM")
            data=self._prefix;self._prefix=b"";self._header_checked=True
        data=self._tail+data
        whole=len(data)//2*2
        self._tail=data[whole:]
        for start in range(0,whole,12000):
            chunk=data[start:min(start+12000,whole)]
            self._put(self._audio_queue,chunk)
            self._bytes+=len(chunk)

    def _fail(self,error):
        with self._lock:
            if self._error is None:self._error=error
        self._stop.set()
        self.transport.close()

    def _run(self):
        try:
            first=self._phrase()
            while first is not None:
                drained=threading.Event()
                receipt={"speech":False,"letters":False}
                no_speakable=False
                previous_bytes=self._bytes
                self._waiting_provider=time.monotonic()
                call=self.transport.open_stream(self._requests(first,drained,receipt),
                    timeout=max(.001,self.deadline-time.monotonic()))
                for response in call:
                    self._check()
                    enumeration=response.DESCRIPTOR.fields_by_name["chunk_type"].enum_type
                    kind=enumeration.values_by_number.get(response.chunk_type)
                    if kind is not None and kind.name=="NO_SPEAKABLE_TEXT":no_speakable=True
                    if response.audio_chunk.data:self._emit(response.audio_chunk.data)
                if not drained.wait(.1):
                    raise SpeechError("OUTPUT_PARTIAL","provider ended before all current text was sent")
                if self._prefix:
                    pending=self._prefix;self._prefix=b"";self._header_checked=True
                    self._emit(pending)
                if self._tail:
                    raise SpeechError("OUTPUT_PARTIAL","provider PCM ends inside a sample")
                if receipt["speech"] and self._bytes==previous_bytes and not(no_speakable and not receipt["letters"]):
                    raise SpeechError("OUTPUT_PARTIAL","speech-bearing RPC returned no audio; tail is incomplete")
                self._header_checked=False
                first=self._phrase()
            if self._bytes==0:
                raise SpeechError("PROVIDER","stream produced no synthesized PCM")
        except Exception as error:
            if not self._stop.is_set():
                if isinstance(error,SpeechError):
                    failure=error
                else:
                    grpc,_,_=grpc_sdk()
                    code=error.code().name if isinstance(error,grpc.RpcError) else None
                    category={"UNAUTHENTICATED":"AUTH","PERMISSION_DENIED":"PERMISSION","RESOURCE_EXHAUSTED":"RATE_LIMIT","DEADLINE_EXCEEDED":"TIMEOUT","CANCELLED":"CANCELLED"}.get(code,"OUTPUT_PARTIAL")
                    if self._bytes:category="OUTPUT_PARTIAL"
                    failure=SpeechError(category,"synthesis transport failed before complete EOF",code=code)
                self._fail(failure)
        finally:
            self.transport.close()
            self._done.set()

    def _start(self):
        with self._lock:
            if self._started:return
            self._check();self._started=True
            for target,name in [(self._produce,"tts-text"),(self._run,"tts-audio")]:
                worker=threading.Thread(target=target,name="yandex-speech-"+name,daemon=True)
                worker.start();self.workers.append(worker)

    def __iter__(self):
        self._start();return self
    def __next__(self):
        self._start()
        while True:
            with self._lock:
                if self._error is not None:
                    error=self._error
                    break
                if self._cancelled:
                    error=SpeechError("CANCELLED","synthesis stream cancelled")
                    break
            try:
                chunk=self._audio_queue.get(timeout=.02)
                with self._lock:
                    if not self._cancelled and self._error is None:return chunk
            except queue.Empty:
                pending=self._last_flush>self._last_audio or self._waiting_provider>self._last_audio or self._source_eof and not self._done.is_set()
                since=max(self._last_flush,self._last_audio,self._waiting_provider)
                if pending and since and time.monotonic()-since>=self._audio_idle_seconds:
                    self._fail(SpeechError("TIMEOUT","provider audio idle deadline expired while flushed speech was pending"))
                    continue
                if self._done.is_set():
                    self._join()
                    if self._error is not None:raise self._error
                    self.input_complete=True
                    self.metadata["status"]="complete"
                    raise StopIteration
        self.close()
        raise error

    def _join(self):
        end=time.monotonic()+2
        for worker in self.workers:
            if worker is not threading.current_thread():worker.join(timeout=max(0,end-time.monotonic()))
        if any(t.is_alive() for t in self.workers if t is not threading.current_thread()):
            raise SpeechError("STATE","text source must be cancellation-cooperative")

    def close(self):
        with self._lock:
            if self._closed:return
            if self.input_complete:
                self._closed=True
                self.transport.close()
                self._join()
                return
            self._closed=True;self._cancelled=True;self.input_complete=False
            self.metadata["status"]="cancelled"
        self._stop.set();self.transport.close()
        try:
            close=getattr(self.source,"close",None)
            if close:close()
        except (ValueError,RuntimeError):pass
        while True:
            try:self._audio_queue.get_nowait()
            except queue.Empty:break
        self._join()
    cancel=close
    def __enter__(self):return self
    def __exit__(self,*args):
        if self.input_complete:self._join()
        else:self.close()

class StdinFragments:
    """Cancellation-cooperative UTF-8 newline fragments from an input descriptor."""
    def __init__(self,descriptor):
        import codecs
        self.descriptor=descriptor
        self.decoder=codecs.getincrementaldecoder("utf-8")()
        self.buffer=""
        self.stopped=threading.Event()
        self.eof=False
    def __iter__(self):return self
    def __next__(self):
        import os
        import select
        while not self.stopped.is_set():
            if "\n" in self.buffer:
                end=self.buffer.index("\n")+1
                value=self.buffer[:end];self.buffer=self.buffer[end:];return value
            if self.eof:
                if self.buffer:
                    value=self.buffer;self.buffer="";return value
                raise StopIteration
            ready,_,_=select.select([self.descriptor],[],[],.05)
            if not ready:continue
            data=os.read(self.descriptor,4096)
            if not data:
                self.eof=True
                self.buffer+=self.decoder.decode(b"",final=True)
            else:self.buffer+=self.decoder.decode(data)
        raise StopIteration
    def close(self):self.stopped.set()
