"""Plugin-owned arriving Discord audio; Native callables are never replaced."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from .errors import SpeechError
from .streaming import _ByteQueue, _END


class CapturePipeline:
    """Bound raw and Native-wrapper backlog while converting the source progressively."""
    def __init__(self, opener, *, on_result=None, on_partial=None,
                 pending_limit=65536, pending_timeout=3.0, raw_limit=524288):
        if pending_limit < 640 or pending_timeout <= 0:
            raise ValueError("invalid live audio budget")
        self.opener = opener
        self.on_result = on_result
        self.on_partial = on_partial
        self.pending_limit = pending_limit
        self.pending_timeout = pending_timeout
        self.raw = _ByteQueue(raw_limit)
        self.stop = threading.Event()
        self.done = threading.Event()
        self.condition = threading.Condition(threading.RLock())
        self.inner = self.wrapper = self.process = None
        self.reader = self.cleanup_thread = None
        self.error = self.result = None
        self.submitted_bytes = self.accepted_bytes = self.max_pending_bytes = 0
        self.input_bytes = 0
        self.first_partial_at = self.input_eof_at = None
        self.last_partial = ""
        self.worker = threading.Thread(target=self._run, name="discord-live-source", daemon=True)
        self.worker.start()

    def bind_inner(self, inner):
        with self.condition:
            if self.inner is not None:
                raise SpeechError("STATE", "a capture cannot own two recognizer sessions")
            self.inner = inner
            self.condition.notify_all()

    def acknowledge(self, count):
        with self.condition:
            self.accepted_bytes += count
            if self.accepted_bytes > self.submitted_bytes:
                raise SpeechError("STATE", "recognizer acknowledged unsubmitted audio")
            self.condition.notify_all()

    def observe_partial(self, text):
        if not text or self.stop.is_set():
            return
        with self.condition:
            if self.first_partial_at is None:
                self.first_partial_at = time.monotonic()
            changed = text != self.last_partial
            self.last_partial = text
        if changed and self.on_partial:
            self.on_partial(self, text)

    def feed(self, pcm):
        if self.stop.is_set() or self.done.is_set():
            return False
        if len(pcm) % 4:
            self._fail(SpeechError("INPUT", "Discord PCM must contain complete stereo frames"))
            return False
        try:
            self.raw.put(bytes(pcm))
            self.input_bytes += len(pcm)
            return True
        except SpeechError as error:
            self._fail(error)
            return False

    def end_audio(self):
        with self.condition:
            if self.input_eof_at is None:
                self.input_eof_at = time.monotonic()
        self.raw.end()

    def _inner_failure(self):
        inner = self.inner
        failure = getattr(inner, "failure", None) if inner is not None else None
        return failure if isinstance(failure, SpeechError) else None

    def _fail(self, error):
        with self.condition:
            if self.error is None:
                self.error = self._inner_failure() or (
                    error if isinstance(error, SpeechError)
                    else SpeechError("PROVIDER", "live capture operation failed"))
            first = not self.stop.is_set()
            self.stop.set()
            self.condition.notify_all()
        self.raw.abort()
        if first:
            self.cleanup_thread = threading.Thread(
                target=self._cancel_io, name="discord-live-cancel", daemon=True)
            self.cleanup_thread.start()

    def cancel(self):
        if not self.done.is_set():
            self._fail(SpeechError("CANCELLED", "live capture cancelled"))

    def _cancel_io(self):
        wrapper, inner, process = self.wrapper, self.inner, self.process
        if wrapper is not None:
            try:
                wrapper.cancel()
            except Exception:
                pass
        if inner is not None:
            try:
                inner.cancel()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _submit(self, data):
        deadline = time.monotonic() + self.pending_timeout
        with self.condition:
            while self.submitted_bytes - self.accepted_bytes + len(data) > self.pending_limit:
                failure = self._inner_failure()
                if failure:
                    raise failure
                if self.stop.is_set():
                    raise self.error or SpeechError("CANCELLED", "live capture cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SpeechError("LIMIT", "recognizer backlog exceeded the live audio budget")
                self.condition.wait(min(.05, remaining))
            if self.stop.is_set():
                raise self.error or SpeechError("CANCELLED", "live capture cancelled")
            failure = self._inner_failure()
            if failure:
                raise failure
            self.submitted_bytes += len(data)
            self.max_pending_bytes = max(self.max_pending_bytes,
                                          self.submitted_bytes - self.accepted_bytes)
        self.wrapper.push_audio(data)

    def _read_pcm(self):
        try:
            while not self.stop.is_set():
                chunk = self.process.stdout.read(640)
                if not chunk:
                    return
                self._submit(chunk)
        except Exception as error:
            if not self.stop.is_set():
                self._fail(error)

    def _run(self):
        try:
            self.wrapper = self.opener(self)
            if self.wrapper is None or self.inner is None:
                raise SpeechError("PREREQUISITE", "the selected real live provider did not open")
            if self.stop.is_set():
                raise self.error
            executable = shutil.which("ffmpeg")
            if not executable:
                raise SpeechError("PREREQUISITE", "ffmpeg is required for Discord PCM conversion")
            self.process = subprocess.Popen(
                [executable, "-nostdin", "-loglevel", "error", "-xerror",
                 "-f", "s16le", "-ar", "48000", "-ac", "2",
                 "-probesize", "32768", "-analyzeduration", "0", "-i", "pipe:0",
                 "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                 "-f", "s16le", "-flush_packets", "1", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
            self.reader = threading.Thread(target=self._read_pcm,
                                            name="discord-live-pcm", daemon=True)
            self.reader.start()
            while not self.stop.is_set():
                chunk = self.raw.get(self.stop)
                if chunk is _END:
                    break
                pending = memoryview(chunk)
                while pending and not self.stop.is_set():
                    written = self.process.stdin.write(pending)
                    if not written:
                        raise SpeechError("STATE", "live PCM converter stopped accepting input")
                    pending = pending[written:]
            self.process.stdin.close()
            self.reader.join(10)
            if self.reader.is_alive():
                raise SpeechError("STATE", "live PCM converter did not drain")
            if self.process.wait(timeout=3) != 0 and not self.stop.is_set():
                raise SpeechError("INPUT", "live PCM conversion failed")
            if self.stop.is_set():
                raise self.error
            self.result = self.wrapper.finalize(timeout=20)
            if not self.result.get("success"):
                self._fail(self._inner_failure() or SpeechError("PROVIDER", "live recognition failed"))
            if self.error is None and self.on_result:
                self.on_result(self, self.result)
        except Exception as error:
            if self.error is None:
                self._fail(error)
        finally:
            if self.error is not None:
                self._cancel_io()
                self.result = {"success": False, "transcript": "", "provider": "yandex",
                               "error": str(self.error)}
            process = self.process
            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                        process.wait(timeout=2)
                if self.reader is not None and self.reader is not threading.current_thread():
                    self.reader.join(2)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            self.done.set()

    def wait(self, timeout=25):
        if not self.done.wait(timeout):
            self._fail(SpeechError("STATE", "live capture did not finish before its deadline"))
            self.done.wait(3)
        if self.worker is not threading.current_thread():
            self.worker.join(.5)
        if self.cleanup_thread is not None and self.cleanup_thread is not threading.current_thread():
            self.cleanup_thread.join(.5)
        return self.result or {"success": False, "transcript": "", "provider": "yandex",
                               "error": str(self.error or "live capture unfinished")}

    @property
    def workers_alive(self):
        return any(thread is not None and thread.is_alive()
                   for thread in (self.worker, self.reader, self.cleanup_thread))


class VoiceFilePolicy:
    """Defer only managed Native voice allocations; WAVs never select stream results."""
    def __init__(self, enabled, *, temp_root=None):
        self.enabled = bool(enabled)
        self.temp_root = Path(temp_root or tempfile.gettempdir()).resolve()
        self.claims = {}
        self.lock = threading.Lock()

    def _candidate(self, path):
        path = Path(path)
        return (path.name.startswith("vc_listen_") and path.suffix == ".wav"
                and path.parent.resolve() == self.temp_root)

    @staticmethod
    def _identity(path):
        info = Path(path).lstat()
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size), info

    def observe(self, file_path, *, source=None, **ignored):
        if not self.enabled or not self._candidate(file_path):
            return
        try:
            identity, _ = self._identity(file_path)
        except OSError:
            return
        with self.lock:
            self.claims[identity] = source
            while len(self.claims) > 256:
                self.claims.pop(next(iter(self.claims)))

    def decide(self, file_path):
        if not self.enabled or not self._candidate(file_path):
            return None
        try:
            identity, info = self._identity(file_path)
        except OSError:
            return self._deferred("native voice allocation became unavailable")
        with self.lock:
            source = self.claims.get(identity)
        if source in {"gateway", "voice_mode"}:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            return self._deferred("native-looking voice allocation has untrusted provenance")
        return self._deferred("the managed live capture owns this voice completion")

    @staticmethod
    def _deferred(reason):
        return {"success": False, "transcript": "", "provider": "yandex",
                "error": reason, "deferred_to_stream": True}


class _Capture:
    def __init__(self, observer, ssrc, user_id, buffer, binding, *, rejected=False):
        self.id = __import__("uuid").uuid4().hex
        self.observer = observer
        self.ssrc, self.user_id, self.binding = ssrc, user_id, binding
        self.buffer = buffer
        self.offset = 0
        self.rejected = rejected
        self.ended = self.committed = self.admitted = self.cancelled = False
        self.pipeline = self.delivery_task = None
        self.delivery_pending = False
        self.result = None
        self.packets = 0
        self.sequence_gaps = self.reordered_or_duplicate = 0
        self.first_sequence = self.last_sequence = self.last_rtp_time = None
        self.last_packet_at = None
        self.text_hash = None
        self.created_at = time.monotonic()


class ReceiverObserver:
    """Read the stock receiver after its SDK callback; never alter its buffers."""
    def __init__(self, manager, guild_id, voice, receiver):
        self.manager, self.guild_id, self.voice, self.receiver = manager, guild_id, voice, receiver
        self.connection = voice._connection
        self.channel_id = getattr(getattr(voice, "channel", None), "id", None)
        self.states = {}
        self.lock = threading.RLock()
        self.closed = False
        self.callback = self.on_packet

    def attach(self):
        with self.lock, self.receiver._lock:
            for ssrc, buffer in self.receiver._buffers.items():
                if buffer:
                    uid = self.receiver._ssrc_to_user.get(ssrc, 0)
                    state = _Capture(self, ssrc, uid, buffer, self.manager.binding(self.guild_id),
                                     rejected=True)
                    self.states[ssrc] = state
        self.connection.add_socket_listener(self.callback)
        self.manager.emit("observer_attached", guild=self.guild_id,
                          receiver_identity=id(self.receiver), voice_identity=id(self.voice),
                          voice_channel=self.channel_id,
                          quarantined_prefixes=len(self.states),
                          native_methods={name: _function_stamp(getattr(self.receiver, name, None))
                              for name in ("_on_packet", "check_silence", "map_ssrc")})

    def on_packet(self, data):
        if self.closed or self.manager.closed or len(data) < 12:
            return
        import struct
        if data[0] >> 6 != 2 or data[1] & 0x7f != 0x78:
            return
        _, _, sequence, rtp_time, ssrc = struct.unpack_from(">BBHII", data)
        try:
            with self.lock:
                with self.receiver._lock:
                    buffer = self.receiver._buffers.get(ssrc)
                    uid = self.receiver._ssrc_to_user.get(ssrc, 0)
                    if not buffer:
                        return
                    state = self.states.get(ssrc)
                    if state is not None and state.buffer is not buffer:
                        self._end_old(state, replaced=True)
                        self.states.pop(ssrc, None)
                        state = None
                    if state is None:
                        if len(self.states) >= 256:
                            return
                        # A newly observed source must start with the one current20ms Native frame.
                        rejected = len(buffer) != 3840 or not uid
                        state = _Capture(self, ssrc, uid, buffer,
                                         self.manager.binding(self.guild_id), rejected=rejected)
                        self.states[ssrc] = state
                    if state.user_id != uid:
                        self._reject(state, "SSRC identity changed before a fresh buffer boundary")
                    if state.rejected or state.ended:
                        return
                    if len(buffer) <= state.offset:
                        return
                    chunk = bytes(buffer[state.offset:])
                    state.offset = len(buffer)
                if not self.manager.allowed(self, state.user_id, state.binding):
                    self._reject(state, "voice source is not currently authorized")
                    return
                if state.pipeline is None:
                    if not self.manager.start_capture(state):
                        self._reject(state, "live capture capacity unavailable")
                        return
                state.packets += 1
                if state.last_sequence is not None:
                    distance = (sequence - state.last_sequence) & 0xffff
                    if 1 < distance < 0x8000:
                        state.sequence_gaps += distance - 1
                    elif distance != 1:
                        state.reordered_or_duplicate += 1
                if state.first_sequence is None:
                    state.first_sequence = sequence
                state.last_sequence, state.last_rtp_time = sequence, rtp_time
                state.last_packet_at = time.monotonic()
                if not state.pipeline.feed(chunk):
                    self._reject(state, "live input was not accepted completely")
        except Exception as error:
            self.manager.emit("observer_error", category=type(error).__name__)

    def _reject(self, state, reason):
        state.rejected = state.cancelled = True
        if state.pipeline is not None:
            state.pipeline.cancel()
        self.manager.emit("capture_rejected", capture=state.id, reason=reason)

    def _end_old(self, state, *, replaced):
        if state.ended:
            return
        state.ended = True
        healthy = (replaced and not state.rejected and not self.receiver._paused
                   and state.pipeline is not None and state.offset == len(state.buffer)
                   and self.manager.allowed(self, state.user_id, state.binding))
        if state.pipeline is not None:
            if healthy:
                state.pipeline.end_audio()
                self.manager.emit("source_eof", capture=state.id, packets=state.packets,
                                  input_bytes=state.offset,
                                  last_packet_at=state.last_packet_at,
                                  first_sequence=state.first_sequence, last_sequence=state.last_sequence,
                                  last_rtp_time=state.last_rtp_time, sequence_gaps=state.sequence_gaps,
                                  reordered_or_duplicate=state.reordered_or_duplicate)
            else:
                state.cancelled = True
                state.pipeline.cancel()
        state.buffer = None

    def synchronize(self):
        with self.lock:
            with self.receiver._lock:
                current = dict(self.receiver._buffers)
                users = dict(self.receiver._ssrc_to_user)
            for ssrc, state in list(self.states.items()):
                buffer = current.get(ssrc)
                if buffer is not state.buffer:
                    self._end_old(state, replaced=buffer is not None and len(buffer) == 0)
                    self.states.pop(ssrc, None)
                elif not state.ended and (
                        users.get(ssrc, 0) != state.user_id
                        or not self.manager.allowed(self, state.user_id, state.binding)):
                    self._reject(state, "voice ownership or binding changed")
            # Data already present without an observed packet has no trustworthy prefix provenance.
            for ssrc, buffer in current.items():
                if buffer and ssrc not in self.states and len(self.states) < 256:
                    self.states[ssrc] = _Capture(
                        self, ssrc, users.get(ssrc, 0), buffer,
                        self.manager.binding(self.guild_id), rejected=True)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.connection.remove_socket_listener(self.callback)
            except (ValueError, OSError):
                pass
            for state in self.states.values():
                if not state.committed:
                    state.cancelled = True
                    if state.pipeline is not None:
                        state.pipeline.cancel()


_INGRESS_OWNERS = {}
_INGRESS_LOCK = threading.RLock()


def _text_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tail_digests(text):
    import re
    words = re.findall(r"\w+", text.casefold().replace("ё", "е"))
    return {str(count): _text_digest(" ".join(words[-count:]))
            for count in range(1, min(12, len(words)) + 1)}

def _function_stamp(function):
    import marshal
    function = getattr(function, "func", function)
    function = getattr(function, "__func__", function)
    code = getattr(function, "__code__", None)
    if code is None:
        return None
    return {"file": code.co_filename,
            "code_sha256": hashlib.sha256(marshal.dumps(code)).hexdigest()}


class DiscordIngress:
    """Own streaming results, then hand genuine text to the unchanged Native callback."""
    def __init__(self, workspace, provider, stt_config, *, telemetry=True,
                 pipeline_type=CapturePipeline):
        import uuid
        from tools.transcription_streaming import streaming_enabled
        self.workspace = Path(workspace).resolve()
        if Path(provider.workspace).resolve() != self.workspace:
            raise SpeechError("AUTH", "Discord streaming provider belongs to another profile")
        import contextvars
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        from hermes_cli.plugins_loader import _plugin_home_scope
        from tui_gateway.launch_profile_policy import owner_secret_scope
        with _plugin_home_scope(self.workspace):
            secret_token = set_secret_scope(owner_secret_scope(self.workspace), profile_home=str(self.workspace))
            try:
                self.scope = contextvars.copy_context()
            finally:
                reset_secret_scope(secret_token)
        self.provider = provider
        self.stt_config = dict(stt_config)
        self.enabled = (streaming_enabled(self.stt_config)
                        and str(self.stt_config.get("provider", "")).lower() == "yandex"
                        and self.stt_config.get("enabled", True) is not False)
        self.file_policy = VoiceFilePolicy(self.enabled)
        self.pipeline_type = pipeline_type
        self.run_id = uuid.uuid4().hex
        self.native = self.adapter = self.loop = self.watch_task = None
        self.observers = {}
        self.captures = {}
        self.closed = False
        self.lock = threading.RLock()
        self.event_lock = threading.Lock()
        self.counts = {}
        self.events_path = self.log_fd = None
        if telemetry and self.enabled:
            cache = self.workspace / "cache"
            cache.mkdir(exist_ok=True)
            run = Path(tempfile.mkdtemp(prefix="discord-stream-", dir=cache))
            self.events_path = run / "events.jsonl"
            self.log_fd = os.open(self.events_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND, 0o600)
        def factory(native, adapter):
            self.attach(native, adapter)
        factory.__qualname__ = "yandex_speech.discord_ingress.attach_" + self.run_id
        self.factory = factory
        provider._discord_ingress = self

    def emit(self, event, **fields):
        import json
        record = {"event": event, "run_id": self.run_id, "pid": os.getpid(),
                  "at": time.monotonic(), **fields}
        with self.event_lock:
            self.counts[event] = self.counts.get(event, 0) + 1
            if self.log_fd is not None:
                os.write(self.log_fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())

    def attach(self, native, adapter):
        import asyncio
        if not self.enabled or self.closed:
            return
        if getattr(adapter, "_client", None) is not native:
            raise SpeechError("STATE", "Discord handler did not receive its owning client")
        with _INGRESS_LOCK:
            key = (str(self.workspace), id(native))
            previous = _INGRESS_OWNERS.get(key)
            if previous is not None and previous is not self:
                previous.close()
            _INGRESS_OWNERS[key] = self
        self.native, self.adapter = native, adapter
        self.loop = asyncio.get_running_loop()
        self.emit("handler_attached", adapter_module=type(adapter).__module__,
                  adapter_identity=id(adapter), native_identity=id(native),
                  events_path=str(self.events_path),
                  native_methods={name: _function_stamp(getattr(adapter, name, None))
                      for name in ("_voice_listen_loop", "_process_voice_input", "_is_allowed_user",
                                   "_voice_input_callback")})
        self._synchronize()
        self.watch_task = self.loop.create_task(self._watch())

    def binding(self, guild_id):
        return getattr(self.adapter, "_voice_text_channels", {}).get(guild_id)

    def allowed(self, observer, user_id, binding):
        if (self.closed or not self.enabled or not user_id or binding is None
                or self.binding(observer.guild_id) != binding
                or self.observers.get(observer.guild_id) is not observer
                or getattr(self.adapter, "_voice_receivers", {}).get(observer.guild_id) is not observer.receiver
                or getattr(self.adapter, "_voice_clients", {}).get(observer.guild_id) is not observer.voice
                or observer.channel_id is None
                or getattr(getattr(observer.voice, "channel", None), "id", None) != observer.channel_id
                or not observer.receiver._running or observer.receiver._paused
                or not observer.voice.is_connected()):
            return False
        try:
            return self.scope.copy().run(self._allowed_user, observer.guild_id, user_id)
        except Exception:
            return False

    def _allowed_user(self, guild_id, user_id):
        guild = self.native.get_guild(guild_id)
        return self.adapter._is_allowed_user(str(user_id), guild=guild, is_dm=False) is True

    def _synchronize(self):
        voices = dict(getattr(self.adapter, "_voice_clients", {}))
        receivers = dict(getattr(self.adapter, "_voice_receivers", {}))
        for guild_id, observer in list(self.observers.items()):
            if (receivers.get(guild_id) is not observer.receiver
                    or voices.get(guild_id) is not observer.voice
                    or getattr(getattr(observer.voice, "channel", None), "id", None) != observer.channel_id
                    or not observer.receiver._running or not observer.voice.is_connected()):
                self.observers.pop(guild_id, None)
                observer.close()
                self.emit("observer_detached", guild=guild_id)
        for guild_id, receiver in receivers.items():
            voice = voices.get(guild_id)
            if guild_id not in self.observers and voice and voice.is_connected() and receiver._running:
                observer = ReceiverObserver(self, guild_id, voice, receiver)
                self.observers[guild_id] = observer
                observer.attach()
        for observer in tuple(self.observers.values()):
            observer.synchronize()
        with self.lock:
            for key, capture in list(self.captures.items()):
                pipe = capture.pipeline
                if pipe and pipe.done.is_set() and pipe.error and not getattr(capture, "error_reported", False):
                    capture.error_reported = True
                    self.emit("capture_failed", capture=capture.id, error=str(pipe.error))
                task = capture.delivery_task
                if pipe and pipe.done.is_set() and (task is None or task.done()) and time.monotonic() - capture.created_at > 60:
                    self.captures.pop(key, None)

    async def _watch(self):
        import asyncio
        try:
            while not self.closed:
                self._synchronize()
                await asyncio.sleep(.05)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.emit("watch_failed", category=type(error).__name__)
            self.close()

    def start_capture(self, capture):
        with self.lock:
            active = sum(not item.pipeline or not item.pipeline.done.is_set()
                         or item.delivery_pending
                         for item in self.captures.values())
            if active >= 8 or self.closed:
                return False
            self.captures[capture.id] = capture
        capture.pipeline = self.pipeline_type(
            self._open_native,
            on_result=lambda pipe, result: self._finished(capture, result),
            on_partial=lambda pipe, text: self._partial(capture, text))
        self.emit("capture_started", capture=capture.id, guild=capture.observer.guild_id,
                  user=capture.user_id, ssrc=capture.ssrc, binding=capture.binding)
        return True

    def _open_native(self, pipe):
        return self.scope.copy().run(self._open_in_scope, pipe)

    def _open_in_scope(self, pipe):
        from agent.transcription_registry import get_provider
        from tools.transcription_streaming import open_streaming_session
        if get_provider("yandex", scope=str(self.workspace)) is not self.provider:
            raise SpeechError("STATE", "the selected Yandex registration changed")
        token = self.provider._opening_capture.set(pipe)
        try:
            wrapper = open_streaming_session(
                on_partial=pipe.observe_partial, stt_config=self.stt_config)
        finally:
            self.provider._opening_capture.reset(token)
        self.emit("native_stream_opened", inner_module=type(pipe.inner).__module__ if pipe.inner else None)
        return wrapper

    def _partial(self, capture, text):
        self.emit("partial", capture=capture.id, characters=len(text),
                  text_sha256=_text_digest(text),
                  source_last_packet_at=capture.last_packet_at,
                  first_partial_at=capture.pipeline.first_partial_at if capture.pipeline else None)

    def _finished(self, capture, result):
        if self.closed or capture.cancelled:
            return
        capture.result = result
        text = str(result.get("transcript", "")).strip()
        self.emit("stream_final", capture=capture.id, characters=len(text),
                  text_sha256=_text_digest(text), tail_sha256=_tail_digests(text),
                  submitted_bytes=capture.pipeline.submitted_bytes,
                  accepted_bytes=capture.pipeline.accepted_bytes,
                  first_partial_at=capture.pipeline.first_partial_at,
                  source_last_packet_at=capture.last_packet_at)
        from tools.voice_mode_transcript import is_whisper_hallucination
        if text and not is_whisper_hallucination(text):
            capture.delivery_pending = True
            self.loop.call_soon_threadsafe(self._schedule_delivery, capture, context=self.scope.copy())
        else:
            capture.delivery_pending = False
            self.emit("stream_no_input", capture=capture.id)

    def _schedule_delivery(self, capture):
        if not self.closed and not capture.cancelled:
            capture.delivery_task = self.loop.create_task(self._deliver(capture), context=self.scope.copy())
        else:
            capture.delivery_pending = False

    async def _deliver(self, capture):
        from hermes_cli.plugins_loader import _plugin_home_scope
        try:
            with _plugin_home_scope(self.workspace):
                # No await may intervene before Native takes its synchronous source snapshot.
                if capture.cancelled or capture.committed or not self.allowed(
                        capture.observer, capture.user_id, capture.binding):
                    self.emit("delivery_refused", capture=capture.id)
                    return
                callback = getattr(self.adapter, "_voice_input_callback", None)
                if not callable(callback):
                    self.emit("delivery_refused", capture=capture.id, reason="no native voice callback")
                    return
                text = str(capture.result.get("transcript", "")).strip()
                capture.text_hash = _text_digest(text)
                capture.committed = True
                self.emit("delivery_committed", capture=capture.id)
                await callback(guild_id=capture.observer.guild_id,
                               user_id=capture.user_id, transcript=text)
        finally:
            capture.delivery_pending = False

    def observe_pre_transcription(self, file_path, *, source=None, provider=None, **kwargs):
        if provider == "yandex":
            self.file_policy.observe(file_path, source=source)

    def note_file_deferred(self):
        self.emit("native_file_deferred")

    def note_recorded_asr(self):
        self.emit("recorded_asr_started")

    def observe_admission(self, event=None, **kwargs):
        if event is None:
            return
        raw, source = getattr(event, "raw_message", None), getattr(event, "source", None)
        guild = getattr(raw, "guild_id", None)
        if guild is None or source is None:
            return
        digest = _text_digest(str(getattr(event, "text", "")).strip())
        with self.lock:
            matching = [capture for capture in self.captures.values()
                        if capture.committed and not capture.admitted
                        and capture.observer.guild_id == guild
                        and str(capture.user_id) == str(getattr(source, "user_id", ""))
                        and str(capture.binding) == str(getattr(source, "chat_id", ""))
                        and capture.text_hash == digest]
            if len(matching) == 1:
                matching[0].admitted = True
                self.emit("voice_admitted", capture=matching[0].id)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for observer in tuple(self.observers.values()):
            observer.close()
        for capture in tuple(self.captures.values()):
            if not capture.committed:
                capture.cancelled = True
                if capture.pipeline:
                    capture.pipeline.cancel()
                if capture.delivery_task and not capture.delivery_task.done():
                    self.loop.call_soon_threadsafe(capture.delivery_task.cancel)
        if self.watch_task and not self.watch_task.done():
            self.loop.call_soon_threadsafe(self.watch_task.cancel)
        with _INGRESS_LOCK:
            for key, owner in list(_INGRESS_OWNERS.items()):
                if owner is self:
                    _INGRESS_OWNERS.pop(key, None)
        self.emit("coordinator_closed")
        with self.event_lock:
            if self.log_fd is not None:
                os.close(self.log_fd)
                self.log_fd = None
