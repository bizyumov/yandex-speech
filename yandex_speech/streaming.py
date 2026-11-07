"""Completed-utterance SpeechKit sessions with bounded input and owned workers."""
from __future__ import annotations

import hashlib
import os
import queue
import shutil
import signal
import subprocess
import threading
import time

from .errors import SpeechError
from .grpc_stt import build_stream_options, rpc_error, sdk
from .media import AudioInputSpec
from .results import ResultAccumulator

_END = object()
MAX_PCM_BYTES = 7_680_000


class _ByteQueue:
    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.closed = False
        self.queue = queue.Queue()
        self.lock = threading.Lock()

    def put(self, data):
        with self.lock:
            if self.closed:
                raise SpeechError("STATE", "audio queue is closed")
            if self.size + len(data) > self.limit:
                raise SpeechError("LIMIT", "bounded audio queue overflow; the utterance is incomplete")
            self.size += len(data)
            self.queue.put_nowait(data)

    def end(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.queue.put_nowait(_END)

    def abort(self):
        with self.lock:
            self.closed = True
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self.size = 0
            self.queue.put_nowait(_END)

    def get(self, stop):
        while not stop.is_set():
            try:
                item = self.queue.get(timeout=0.02)
            except queue.Empty:
                continue
            if item is _END:
                return item
            with self.lock:
                self.size -= len(item)
            return item
        return _END


class TranscriptionSession:
    def __init__(self, transport, *, audio_input=None, language="ru-RU",
                 literature_text=True, timestamps=False, _finalize_seconds=5, _max_pcm_bytes=MAX_PCM_BYTES,
                 _deadline_seconds=1800, _on_close=None):
        self.transport = transport
        self.audio_input = audio_input or AudioInputSpec()
        if self.audio_input.source_format not in {"auto", "lpcm"}:
            raise SpeechError("CONFIG", "stream source format must be auto or lpcm")
        self._source_frame = None
        self._raw_args = []
        self.direct = False
        if self.audio_input.source_format == "lpcm":
            fmt, rate, channels, width = self.audio_input.raw_values()
            self._source_frame = width * channels
            self._raw_args = ["-f", fmt, "-ar", str(rate), "-ac", str(channels)]
            self.direct = fmt == "s16le" and rate == 16000 and channels == 1
        if not self.direct and shutil.which("ffmpeg") is None:
            raise SpeechError("PREREQUISITE", "ffmpeg is required to decode this arriving source")
        self.language, self.literature_text = language, literature_text
        self._finalize_seconds = min(5.0, float(_finalize_seconds))
        self._max_pcm_bytes = min(MAX_PCM_BYTES, int(_max_pcm_bytes))
        if self._finalize_seconds <= 0 or self._max_pcm_bytes <= 0:
            raise SpeechError("CONFIG", "stream resource budgets must be positive")
        self._deadline_at = time.monotonic() + _deadline_seconds
        self._on_close = _on_close
        self._closed = False
        self._failure_cleanup_started = False
        self.state = "open"
        self.workers = []
        self._state_lock = threading.RLock()
        self._finalize_lock = threading.Lock()
        self._stop = threading.Event()
        self._source_closed = threading.Event()
        self._writer_done = threading.Event()
        self._prepared = threading.Event()
        self._request_drained = threading.Event()
        self._reader_done = threading.Event()
        self._pcm = _ByteQueue(64_000)
        self._source = _ByteQueue(1_048_576)
        self._source_count = 0
        self._pcm_count = 0
        self._source_hash = hashlib.sha256()
        self._tail = b""
        self._error = None
        self._cleanup_error = None
        self._cleanup_inflight = 0
        self._result = None
        self._started = False
        self._eou_sent = False
        self._response_eof = False
        self._process = None
        self._call = None
        self._accumulator = ResultAccumulator(literature_text=literature_text, timestamps=timestamps)
        if hasattr(self.transport, "bind_cancel_event"):
            self.transport.bind_cancel_event(self._stop)

    def _thread(self, target, name):
        thread = threading.Thread(target=target, name="yandex-speech-" + name, daemon=True)
        with self._state_lock:
            if self._closed:
                return
            thread.start()
            self.workers.append(thread)

    def _fail(self, error):
        with self._state_lock:
            if self.state == "completed":
                return
            if self._error is None:
                self._error = error
            if self.state != "cancelled":
                self.state = "failed"
            start_cleanup = not self._failure_cleanup_started and not self._closed
            self._failure_cleanup_started = True
        self._stop.set()
        self._source.abort()
        self._pcm.abort()
        if self._call is not None:
            self._call.cancel()
        if hasattr(self.transport, "cancel_all"):
            self.transport.cancel_all()
        if self._process is not None and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if start_cleanup:
            self._thread(self._cleanup_failure, "failure-cleanup")

    @property
    def failure(self):
        """The first failed/cancelled outcome, read under session coordination."""
        with self._state_lock:
            return self._error

    @property
    def cleanup_failed(self):
        with self._state_lock:
            return self._cleanup_error is not None

    @property
    def cleanup_pending(self):
        with self._state_lock:
            return self._cleanup_inflight > 0

    def _cleanup_recorded(self, deadline):
        with self._state_lock:
            self._cleanup_inflight += 1
        try:
            try:
                return self._cleanup(deadline)
            except Exception as error:
                failure = error if isinstance(error, SpeechError) else SpeechError(
                    "STATE", "owned stream cleanup failed")
                with self._state_lock:
                    if self._cleanup_error is None:
                        self._cleanup_error = failure
                    if self._error is None:
                        self._error = failure
                        self.state = "failed"
                raise failure from None
        finally:
            with self._state_lock:
                self._cleanup_inflight -= 1

    def _cleanup_failure(self):
        try:
            self._cleanup_recorded(time.monotonic() + 2)
        except SpeechError:
            pass  # Primary and secondary outcomes were retained by _cleanup_recorded.

    def _raise_error(self):
        if self._error is not None:
            raise self._error

    def _start(self):
        if self._started:
            return
        self._started = True
        if not self.direct:
            command = [shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-xerror",
                       "-probesize", "32768", "-analyzeduration", "0", *self._raw_args,
                       "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "16000",
                       "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]
            try:
                self._process = subprocess.Popen(command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                self._fail(SpeechError("PREREQUISITE", "cannot start the required audio decoder"))
                self._raise_error()
            self._thread(self._write_source, "source-writer")
            self._thread(self._read_decoder, "decoder-reader")
        self._thread(self._read_provider, "provider-reader")

    def _feed_pcm(self, data):
        if not data:
            return
        if self._pcm_count + len(data) > self._max_pcm_bytes:
            raise SpeechError("LIMIT", "utterance exceeds the 240-second native PCM budget")
        self._pcm.put(data)
        self._pcm_count += len(data)

    def push_audio(self, source_bytes):
        if not isinstance(source_bytes, (bytes, bytearray, memoryview)):
            raise SpeechError("CONFIG", "push_audio requires source bytes")
        data = bytes(source_bytes)
        with self._state_lock:
            self._raise_error()
            if self.state not in {"open", "receiving"}:
                raise SpeechError("STATE", "cannot feed a terminal or finalizing session")
            if time.monotonic() >= self._deadline_at:
                error = SpeechError("TIMEOUT", "arriving-audio operation deadline expired")
                self._fail(error)
                raise error
            if not data:
                return
            self._start()
            self.state = "receiving"
            self._source_count += len(data)
            self._source_hash.update(data)
            try:
                if self.direct:
                    combined = self._tail + data
                    count = len(combined) // 2 * 2
                    self._tail = combined[count:]
                    self._feed_pcm(combined[:count])
                else:
                    self._source.put(data)
            except SpeechError as error:
                self._fail(error)
                raise

    def partial_transcript(self):
        with self._state_lock:
            self._raise_error()
            return self._accumulator.preview if self.state in {"receiving", "finalizing"} else ""

    def _write_source(self):
        try:
            while not self._stop.is_set():
                data = self._source.get(self._stop)
                if data is _END:
                    break
                self._process.stdin.write(data)
            self._process.stdin.close()
        except (OSError, ValueError):
            if not self._stop.is_set():
                self._fail(SpeechError("UNSUPPORTED_AUDIO", "source decoder rejected arriving audio"))
        finally:
            self._writer_done.set()
            if self._process.stdin is not None and not self._process.stdin.closed:
                try:
                    self._process.stdin.close()
                except (OSError, ValueError):
                    pass

    def _read_decoder(self):
        tail = b""
        try:
            while not self._stop.is_set():
                data = self._process.stdout.read(4096)
                if not data:
                    break
                combined = tail + data
                count = len(combined) // 2 * 2
                tail = combined[count:]
                self._feed_pcm(combined[:count])
            while not self._source_closed.wait(0.02):
                if self._stop.is_set():
                    return
            while not self._writer_done.wait(0.02):
                if self._stop.is_set():
                    return
            if self._stop.is_set():
                return
            code = self._process.wait(timeout=self._finalize_seconds)
            if code != 0 or tail:
                raise SpeechError("UNSUPPORTED_AUDIO", "decoder did not finish a complete valid source")
            self._prepared.set()
            self._pcm.end()
        except subprocess.TimeoutExpired:
            self._fail(SpeechError("TIMEOUT", "decoder completion exceeded finalization budget"))
        except SpeechError as error:
            self._fail(error)
        except (OSError, ValueError):
            if not self._stop.is_set():
                self._fail(SpeechError("UNSUPPORTED_AUDIO", "arriving media could not be decoded completely"))
        finally:
            if self._process.stdout is not None:
                self._process.stdout.close()

    def _requests(self):
        _, p, *_ = sdk()
        yield build_stream_options(language=self.language, literature_text=self.literature_text)
        while not self._stop.is_set():
            data = self._pcm.get(self._stop)
            if data is _END:
                if self._stop.is_set() or not self._prepared.is_set():
                    return
                self._eou_sent = True
                yield p.StreamingRequest(eou=p.Eou())
                self._request_drained.set()
                return
            yield p.StreamingRequest(chunk=p.AudioChunk(data=data))

    def _read_provider(self):
        try:
            self._call = self.transport.open_stream(self._requests(), timeout=max(0.01, self._deadline_at - time.monotonic()))
            for event in self._call:
                if self._stop.is_set():
                    return
                self._accumulator.feed(event)
            self._response_eof = True
            if not self._eou_sent and not self._stop.is_set():
                self._fail(SpeechError("OUTPUT_PARTIAL", "provider ended before host utterance completion"))
        except SpeechError as error:
            if not self._stop.is_set():
                self._fail(error)
        except Exception as error:
            if not self._stop.is_set():
                self._fail(rpc_error(error))
        finally:
            self._reader_done.set()

    def _cleanup(self, deadline):
        self._stop.set()
        self._source.abort()
        self._pcm.abort()
        if self._call is not None:
            self._call.cancel()
        if hasattr(self.transport, "cancel_all"):
            self.transport.cancel_all()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=max(0.01, min(0.5, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            except ProcessLookupError:
                pass
        close_error = None
        try:
            self.transport.close()
        except Exception:
            close_error = SpeechError("STATE", "owned transport did not close successfully")
        while True:
            with self._state_lock:
                workers = [worker for worker in self.workers
                           if worker is not threading.current_thread() and worker.is_alive()]
                if not workers:
                    if process is not None and process.poll() is None:
                        raise SpeechError("STATE", "owned stream workers exceeded the two-second shutdown budget")
                    if close_error is not None:
                        raise close_error
                    # Close admission under the same lock used by _thread. A concurrent
                    # finalizer may register its failure cleanup after an earlier snapshot.
                    if not self._closed:
                        self._closed = True
                        if self._on_close is not None:
                            self._on_close()
                    return
            if time.monotonic() >= deadline:
                raise SpeechError("STATE", "owned stream workers exceeded the two-second shutdown budget")
            for worker in workers:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def _source_info(self):
        return {"metadata_origin": "streamed_input", "source_sha256": self._source_hash.hexdigest(),
                "source_bytes": self._source_count, "duration_ms": self._pcm_count / 32,
                "size_bytes": self._pcm_count, "converted": not self.direct,
                "timeline_offset_seconds": 0.0,
                "original": {"source_format": self.audio_input.source_format,
                             "sample_format": self.audio_input.sample_format,
                             "sample_rate_hertz": self.audio_input.sample_rate_hertz,
                             "channels": self.audio_input.channels},
                "prepared": {"container": "LPCM", "sample_rate_hertz": 16000, "channels": 1}}

    def finalize(self):
        with self._finalize_lock:
            if self._result is not None:
                return self._result
            deadline = min(self._deadline_at, time.monotonic() + self._finalize_seconds)
            candidate = None
            try:
                if time.monotonic() >= self._deadline_at:
                    raise SpeechError("TIMEOUT", "arriving-audio operation deadline expired")
                with self._state_lock:
                    self._raise_error()
                    if self.state not in {"open", "receiving"}:
                        raise SpeechError("STATE", "invalid stream finalization state")
                    self.state = "finalizing"
                    self._source_closed.set()
                    if self._source_frame and self._source_count % self._source_frame:
                        raise SpeechError("UNSUPPORTED_AUDIO", "raw source ends inside a declared frame")
                    if self.direct and self._tail:
                        raise SpeechError("UNSUPPORTED_AUDIO", "raw source ends inside a PCM sample")
                    if self._source_count == 0:
                        candidate = self._accumulator.result(
                            completion_kind="streamed-utterance", source_info=self._source_info())
                    elif self.direct:
                        self._prepared.set()
                        self._pcm.end()
                    else:
                        self._source.end()
                if candidate is None:
                    if not self._reader_done.wait(max(0.0, deadline - time.monotonic())):
                        raise SpeechError("TIMEOUT", "stream finalization exceeded five seconds")
                    with self._state_lock:
                        self._raise_error()
                        eou = self._accumulator.eou_ms
                        if (not self._prepared.is_set() or not self._eou_sent
                                or not self._request_drained.is_set() or not self._response_eof
                                or eou is None or not self._accumulator.eou_consistent
                                or not 0 <= eou <= self._pcm_count / 32):
                            raise SpeechError("OUTPUT_PARTIAL", "missing complete drain or matching EOU acknowledgement")
                        candidate = self._accumulator.result(
                            completion_kind="streamed-utterance", source_info=self._source_info())
                self._cleanup_recorded(min(deadline, time.monotonic() + 2))
                with self._state_lock:
                    self._raise_error()
                    if time.monotonic() > deadline:
                        raise SpeechError("TIMEOUT", "stream completion exceeded finalization deadline")
                    self._result = candidate
                    self.state = "completed"
                return self._result
            except SpeechError as error:
                self._fail(error)
                try:
                    self._cleanup_recorded(time.monotonic() + 2)
                except SpeechError:
                    pass  # Preserve the primary outcome; cleanup failure remains separate.
                self._raise_error()
            except BaseException:
                self.cancel()
                raise

    def cancel(self):
        deadline = time.monotonic() + 2
        with self._state_lock:
            if self.state == "completed":
                return
            if self._error is None:
                self.state = "cancelled"
                self._error = SpeechError("CANCELLED", "local utterance cancelled")
        self._stop.set()
        self._cleanup_recorded(deadline)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cancel()
