"""Offline regressions for stock Discord ingress; these do not pass the live canary."""
import asyncio
import math
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import wave

from tools.transcription_streaming import _PluginSession


class FixtureInner:
    """An explicitly offline byte sink; never evidence of Yandex recognition."""
    def __init__(self, capture, gate=None):
        self.capture = capture
        self.gate = gate
        self.audio = bytearray()
        self.started = threading.Event()
        self.cancelled = threading.Event()

    @property
    def failure(self):
        return None

    def push_audio(self, data):
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.cancelled.is_set():
            return
        self.audio.extend(data)
        self.capture.acknowledge(len(data))

    def partial_transcript(self):
        return "OFFLINE FIXTURE" if self.audio else ""

    def finalize(self):
        return {"success": True, "transcript": "OFFLINE FIXTURE", "provider": "fixture"}

    def cancel(self):
        self.cancelled.set()
        if self.gate is not None:
            self.gate.set()


def stereo_pcm(seconds=1):
    frames = 48000 * seconds
    return b"".join(struct.pack("<hh",
        int(2000 * math.sin(2 * math.pi * 440 * index / 48000)),
        int(3000 * math.cos(2 * math.pi * 660 * index / 48000)))
        for index in range(frames))


class CapturePipelineTests(unittest.TestCase):
    def make_pipeline(self, *, gate=None, **kwargs):
        from yandex_speech.discord_ingress import CapturePipeline
        opened = []
        def opener(capture):
            inner = FixtureInner(capture, gate)
            capture.bind_inner(inner)
            opened.append(inner)
            return _PluginSession("fixture", inner, capture.observe_partial).start()
        pipeline = CapturePipeline(opener, **kwargs)
        self.addCleanup(pipeline.cancel)
        return pipeline, opened

    def test_conversion_matches_actual_native_wav_and_finishes_one_session(self):
        from plugins.platforms.discord.adapter import VoiceReceiver
        pcm = stereo_pcm()
        pipeline, opened = self.make_pipeline()
        for offset in range(0, len(pcm), 3840):
            pipeline.feed(pcm[offset:offset + 3840])
        pipeline.end_audio()
        result = pipeline.wait(10)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(opened), 1)
        with tempfile.TemporaryDirectory() as folder:
            target = str(Path(folder) / "native.wav")
            VoiceReceiver.pcm_to_wav(pcm, target)
            with wave.open(target, "rb") as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()),
                                 (16000, 1, 2))
                expected = audio.readframes(audio.getnframes())
        self.assertEqual(bytes(opened[0].audio), expected)
        self.assertEqual(pipeline.submitted_bytes, len(expected))
        self.assertEqual(pipeline.accepted_bytes, len(expected))
        self.assertFalse(pipeline.workers_alive)
        self.assertIsNotNone(pipeline.process.poll())

    def test_native_unbounded_queue_is_limited_by_inner_ack_budget(self):
        gate = threading.Event()
        pipeline, opened = self.make_pipeline(gate=gate, pending_limit=1280,
                                               pending_timeout=.2)
        pipeline.feed(stereo_pcm())
        pipeline.end_audio()
        result = pipeline.wait(5)
        self.assertFalse(result["success"])
        self.assertEqual(len(opened), 1)
        self.assertLessEqual(pipeline.max_pending_bytes, 1280)
        self.assertTrue(opened[0].cancelled.is_set())
        self.assertFalse(pipeline.workers_alive)

    def test_cancellation_wakes_a_waiting_budget_and_closes_inner(self):
        gate = threading.Event()
        pipeline, opened = self.make_pipeline(gate=gate, pending_limit=1280,
                                               pending_timeout=5)
        pipeline.feed(stereo_pcm())
        pipeline.end_audio()
        deadline = time.monotonic() + 3
        while not opened and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(opened)
        self.assertTrue(opened[0].started.wait(3))
        started = time.monotonic()
        pipeline.cancel()
        result = pipeline.wait(3)
        self.assertFalse(result["success"])
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(opened[0].cancelled.is_set())
        self.assertFalse(pipeline.workers_alive)


class ActualSDKDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_socket_reader_preserves_registration_order(self):
        import discord
        client = discord.Client(intents=discord.Intents.none())
        await client._async_setup_hook()
        channel = SimpleNamespace(guild=SimpleNamespace(id=7),
                                  _get_voice_client_key=lambda: (7, "guild_id"))
        voice = discord.VoiceClient(client, channel)
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.setblocking(False)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        voice._connection.socket = receiver
        seen, done = [], threading.Event()
        def first(data):
            seen.append(("native", data))
        def second(data):
            seen.append(("observer", data))
            if len(seen) == 200:
                done.set()
        try:
            voice._connection.add_socket_listener(first)
            voice._connection.add_socket_listener(second)
            voice._connection._socket_reader.resume(force=True)
            for index in range(100):
                sender.sendto(index.to_bytes(2, "big"), receiver.getsockname())
            self.assertTrue(await asyncio.to_thread(done.wait, 3))
            expected = [(label, index.to_bytes(2, "big"))
                        for index in range(100) for label in ("native", "observer")]
            self.assertEqual(seen, expected)
        finally:
            voice._connection.remove_socket_listener(first)
            voice._connection.remove_socket_listener(second)
            voice._connection._socket_reader.stop()
            await asyncio.to_thread(voice._connection._socket_reader.join, 2)
            receiver.close()
            sender.close()
            voice.cleanup()
            await client.close()


class VoiceFilePolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.file = self.root / "vc_listen_offline.wav"
        with self.file.open("xb") as target:
            target.write(b"offline classification fixture")
        self.file.chmod(0o600)

    def test_managed_voice_file_is_deferred_and_never_selects_another_capture(self):
        from yandex_speech.discord_ingress import VoiceFilePolicy
        policy = VoiceFilePolicy(True, temp_root=self.root)
        policy.observe(self.file, source=None)
        first = policy.decide(self.file)
        repeated = policy.decide(self.file)
        self.assertTrue(first["deferred_to_stream"])
        self.assertFalse(first["success"])
        self.assertEqual(first, repeated)
        # There is no content-addressed live-result lookup for a delayed WAV to consume.

    def test_disabled_mode_preserves_native_file_route(self):
        from yandex_speech.discord_ingress import VoiceFilePolicy
        policy = VoiceFilePolicy(False, temp_root=self.root)
        policy.observe(self.file, source=None)
        self.assertIsNone(policy.decide(self.file))

    def test_attachment_with_coincidental_basename_keeps_recording_route(self):
        from yandex_speech.discord_ingress import VoiceFilePolicy
        policy = VoiceFilePolicy(True, temp_root=self.root)
        policy.observe(self.file, source="gateway")
        self.assertIsNone(policy.decide(self.file))

    def test_only_actual_temp_voice_allocations_are_deferred(self):
        from yandex_speech.discord_ingress import VoiceFilePolicy
        policy = VoiceFilePolicy(True, temp_root=self.root / "other")
        self.assertIsNone(policy.decide(self.file))

    def test_symlink_cannot_be_a_trusted_voice_allocation(self):
        from yandex_speech.discord_ingress import VoiceFilePolicy
        policy = VoiceFilePolicy(True, temp_root=self.root)
        link = self.root / "vc_listen_link.wav"
        link.symlink_to(self.file)
        self.assertTrue(policy.decide(link)["deferred_to_stream"])


class OfflinePipeline:
    """Immediate offline control fixture; not a recognizer."""
    def __init__(self, opener, *, on_result=None, on_partial=None):
        self.on_result = on_result
        self.done = threading.Event()
        self.error = None
        self.input_bytes = self.submitted_bytes = self.accepted_bytes = 0
        self.first_partial_at = None
        self.ends = 0

    def feed(self, data):
        self.input_bytes += len(data)
        self.submitted_bytes = self.accepted_bytes = self.input_bytes // 6
        return True

    def end_audio(self):
        self.ends += 1
        self.done.set()
        self.on_result(self, {"success": True, "transcript": "OFFLINE CONTROL", "provider": "fixture"})

    def cancel(self):
        from yandex_speech.errors import SpeechError
        self.error = SpeechError("CANCELLED", "offline control cancelled")
        self.done.set()


class OfflineAdapter:
    """Source/binding fixtures for coordinator logic; never a live-adapter proof."""
    def __init__(self, native, receiver, voice):
        self._client = native
        self._voice_receivers = {7: receiver}
        self._voice_clients = {7: voice}
        self._voice_text_channels = {7: 10}
        self.allowed_users = {"42"}
        self.echo_started = asyncio.Event()
        self.echo_gate = None
        self.routes = []
        self.manager = None
        self._voice_input_callback = self.voice_callback

    def _is_allowed_user(self, user, **kwargs):
        return user in self.allowed_users

    async def voice_callback(self, *, guild_id, user_id, transcript):
        # Match the inspected Native synchronous source snapshot before its first await.
        route = self._voice_text_channels[guild_id]
        self.echo_started.set()
        if self.echo_gate is not None:
            await self.echo_gate.wait()
        self.routes.append(route)
        event = SimpleNamespace(text=transcript,
            raw_message=SimpleNamespace(guild_id=guild_id),
            source=SimpleNamespace(user_id=str(user_id), chat_id=str(route)))
        self.manager.observe_admission(event=event)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.listeners = [lambda data: None]
        self.connection = SimpleNamespace(add_socket_listener=self.listeners.append,
                                          remove_socket_listener=self.listeners.remove)
        self.voice = SimpleNamespace(_connection=self.connection, is_connected=lambda: True)
        self.receiver = SimpleNamespace(_buffers={}, _ssrc_to_user={1234: 42},
                                        _lock=threading.Lock(), _running=True, _paused=False)
        self.native = SimpleNamespace(get_guild=lambda guild: SimpleNamespace(id=guild))
        self.adapter = OfflineAdapter(self.native, self.receiver, self.voice)
        self.provider = SimpleNamespace(workspace=Path(self.folder.name))
        self.sequence = 0
        self.managers = []

    async def asyncTearDown(self):
        for manager in reversed(self.managers):
            manager.close()
        await asyncio.sleep(0)

    def make_manager(self, *, attach=True, streaming=True):
        from yandex_speech.discord_ingress import DiscordIngress
        manager = DiscordIngress(self.folder.name, self.provider,
            {"provider": "yandex", "streaming": streaming}, telemetry=False,
            pipeline_type=OfflinePipeline)
        self.managers.append(manager)
        self.adapter.manager = manager
        if attach:
            manager.attach(self.native, self.adapter)
        return manager

    def packet(self, manager, *, user_id=42):
        self.receiver._ssrc_to_user[1234] = user_id
        self.receiver._buffers.setdefault(1234, bytearray()).extend(bytes(3840))
        self.sequence += 1
        packet = struct.pack(">BBHII", 0x80, 0x78, self.sequence, self.sequence * 960, 1234)
        manager.observers[7].on_packet(packet)

    def end_native_buffer(self, manager):
        self.receiver._buffers[1234] = bytearray()
        manager._synchronize()

    async def test_one_native_boundary_means_one_callback_and_one_admission(self):
        manager = self.make_manager()
        self.packet(manager)
        capture = next(iter(manager.captures.values()))
        self.end_native_buffer(manager)
        manager._synchronize()
        await asyncio.sleep(.05)
        self.assertEqual(capture.pipeline.ends, 1)
        self.assertEqual(self.adapter.routes, [10])
        self.assertEqual(manager.counts.get("voice_admitted"), 1)
        self.assertTrue(capture.committed)

    async def test_nonempty_late_prefix_is_never_uploaded(self):
        self.receiver._buffers[1234] = bytearray(bytes(3840))
        manager = self.make_manager()
        self.packet(manager)
        self.assertFalse(manager.captures)
        self.assertTrue(manager.observers[7].states[1234].rejected)

    async def test_denied_user_cannot_open_a_capture(self):
        self.adapter.allowed_users.clear()
        manager = self.make_manager()
        self.packet(manager)
        self.assertFalse(manager.captures)
        self.assertTrue(manager.observers[7].states[1234].rejected)

    async def test_remapped_buffer_is_cancelled_instead_of_relabelled(self):
        manager = self.make_manager()
        self.packet(manager)
        capture = next(iter(manager.captures.values()))
        self.adapter.allowed_users.add("99")
        self.packet(manager, user_id=99)
        self.assertTrue(capture.cancelled)
        self.assertIsNotNone(capture.pipeline.error)
        self.end_native_buffer(manager)
        await asyncio.sleep(.05)
        self.assertFalse(self.adapter.routes)

    async def test_binding_move_while_completion_is_queued_refuses_delivery(self):
        manager = self.make_manager()
        self.packet(manager)
        self.end_native_buffer(manager)
        self.adapter._voice_text_channels[7] = 20
        await asyncio.sleep(.05)
        self.assertFalse(self.adapter.routes)
        self.assertEqual(manager.counts.get("delivery_refused"), 1)

    async def test_move_during_echo_keeps_already_committed_original_source(self):
        manager = self.make_manager()
        self.adapter.echo_gate = asyncio.Event()
        self.packet(manager)
        self.end_native_buffer(manager)
        await asyncio.wait_for(self.adapter.echo_started.wait(), 1)
        self.adapter._voice_text_channels[7] = 20
        self.adapter.echo_gate.set()
        await asyncio.sleep(.05)
        self.assertEqual(self.adapter.routes, [10])
        self.assertEqual(manager.counts.get("voice_admitted"), 1)

    async def test_join_after_plugin_load_is_observed(self):
        self.adapter._voice_clients.clear()
        self.adapter._voice_receivers.clear()
        manager = self.make_manager()
        self.assertFalse(manager.observers)
        self.adapter._voice_clients[7] = self.voice
        self.adapter._voice_receivers[7] = self.receiver
        deadline = time.monotonic() + 1
        while not manager.observers and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertIn(7, manager.observers)
        self.assertEqual(len(self.listeners), 2)

    async def test_repeated_load_closes_old_observer_before_attaching_new(self):
        first = self.make_manager()
        second = self.make_manager()
        self.assertTrue(first.closed)
        self.assertNotEqual(first.factory.__qualname__, second.factory.__qualname__)
        self.assertEqual(len(self.listeners), 2)
        second.close()
        self.assertEqual(len(self.listeners), 1)

    async def test_disabled_live_mode_does_not_attach_or_defer_voice_files(self):
        manager = self.make_manager(streaming=False)
        self.assertFalse(manager.observers)
        self.assertEqual(len(self.listeners), 1)
        self.assertIsNone(manager.file_policy.decide("/tmp/vc_listen_offline.wav"))


if __name__ == "__main__":
    unittest.main()
