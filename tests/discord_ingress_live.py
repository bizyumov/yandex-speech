"""Real-provider/local-UDP preflight. This does NOT pass the real Discord canary."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
import wave


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def exercise(home, provider, fixture, seconds):
    import discord
    from discord.ext import commands
    from discord.voice_state import ConnectionFlowState
    import nacl.secret
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter, VoiceReceiver

    with wave.open(str(fixture), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        count = min(audio.getnframes(), int(seconds * 16000))
        audio.setpos(audio.getnframes() - count)
        mono = audio.readframes(count)
    raw = subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error",
        "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
        "-ar", "48000", "-ac", "2", "-f", "s16le", "pipe:1"],
        input=mono, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10).stdout
    assert len(raw) % 3840 == 0

    native = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    await native._async_setup_hook()
    channel = SimpleNamespace(id=70, guild=SimpleNamespace(id=7), members=[],
                              _get_voice_client_key=lambda: (7, "guild_id"))
    voice = discord.VoiceClient(native, channel)
    recv_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_socket.bind(("127.0.0.1", 0))
    recv_socket.setblocking(False)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    connection = voice._connection
    connection.socket = recv_socket
    connection.secret_key = bytes(range(32))  # local test transport, not a Discord credential
    connection.ssrc = 999
    connection._state = ConnectionFlowState.connected
    connection._connected.set()
    receiver = VoiceReceiver(voice, allowed_user_ids={"42"})
    adapter = DiscordAdapter(PlatformConfig(enabled=True))
    adapter._client = native
    adapter._voice_receivers = {7: receiver}
    adapter._voice_clients = {7: voice}
    adapter._voice_text_channels = {7: 10}
    adapter._allowed_user_ids = {"42"}
    adapter._allowed_role_ids = set()
    owned_voices = [(voice, recv_socket)]
    owned_receivers = [receiver]
    owned_listeners = []
    callbacks = []
    finished = asyncio.get_running_loop().create_future()

    async def record_callback(**payload):
        # Component fixture, not a fabricated live user turn.
        callbacks.append(payload)
        if not finished.done():
            finished.set_result(payload)

    adapter._voice_input_callback = record_callback
    manager = provider._discord_ingress
    assert manager is not None and manager.enabled
    listen = None
    try:
        receiver.start()
        receiver.map_ssrc(1234, 42)
        adapter._wire_plugin_handlers(native)  # actual normal PluginManager factories
        assert manager.adapter is adapter and manager.native is native
        assert manager.observers[7].receiver is receiver
        connection._socket_reader.resume(force=True)
        listen = asyncio.create_task(adapter._voice_listen_loop(7))
        adapter._voice_listen_tasks[7] = listen
        owned_listeners.append(listen)
        encoder = discord.opus.Encoder()
        box = nacl.secret.Aead(connection.secret_key)
        started = time.monotonic()
        packets = len(raw) // 3840
        for index in range(packets):
            await asyncio.sleep(max(0, started + index * .02 - time.monotonic()))
            frame = raw[index * 3840:(index + 1) * 3840]
            opus = encoder.encode(frame, 960)
            header = struct.pack(">BBHII", 0x80, 0x78, index & 0xffff, index * 960, 1234)
            suffix = struct.pack(">I", index + 1)
            encrypted = box.encrypt(opus, header, suffix + bytes(20)).ciphertext
            sender.sendto(header + encrypted + suffix, recv_socket.getsockname())
        source_end = time.monotonic()
        payload = await asyncio.wait_for(finished, 35)
        capture = next(item for item in manager.captures.values() if item.committed)
        capture.pipeline.wait(2)
        transcript = payload["transcript"]
        from yandex_speech.discord_ingress import _tail_digests
        report = {
            "scope": "component: actual SDK UDP, stock receiver/adapter, real Yandex; not Discord network",
            "goal_proven": False,
            "normal_plugin_loaded": True,
            "provider_module": type(provider).__module__,
            "real_provider_factory": provider._client_factory.__module__ == "yandex_speech.transcription",
            "source_seconds": len(raw) / 192000,
            "udp_packets_sent": packets, "native_packets_decoded": receiver._decode_ok,
            "native_decode_failures": receiver._decode_failed,
            "stream_count": manager.counts.get("native_stream_opened", 0),
            "first_partial_seconds": capture.pipeline.first_partial_at - started if capture.pipeline.first_partial_at else None,
            "partial_before_input_end": bool(capture.pipeline.first_partial_at and
                                              capture.pipeline.first_partial_at < capture.last_packet_at <= source_end + .2),
            "submitted_pcm_bytes": capture.pipeline.submitted_bytes,
            "accepted_pcm_bytes": capture.pipeline.accepted_bytes,
            "native_voice_wav_deferred": manager.counts.get("native_file_deferred", 0),
            "recorded_asr_calls": manager.counts.get("recorded_asr_started", 0),
            "fixture_callback_count": len(callbacks), "ordinary_inputs_submitted": 0,
            "final_transcript_characters": len(transcript),
            "tail_sha256": _tail_digests(transcript),
            "events_path": str(manager.events_path),
        }
        # Verify normal discovery/re-wiring on the SAME SDK client after the real stream.
        from hermes_cli.plugins import discover_plugins
        from agent.transcription_registry import get_provider
        original_receiver_callback = receiver._on_packet
        old_observer_callback = manager.observers[7].callback
        old_manager = manager
        discover_plugins(force=True)
        replacement = get_provider("yandex", scope=str(home))
        manager = replacement._discord_ingress
        adapter.rewire_plugin_handlers()
        assert old_manager.closed and manager is not old_manager
        assert manager.native is native and manager.adapter is adapter
        assert original_receiver_callback in connection._socket_reader._callbacks
        assert old_observer_callback not in connection._socket_reader._callbacks
        assert len(connection._socket_reader._callbacks) == 2
        report["normal_plugin_reload_same_client"] = True

        def test_packet(key, ssrc, sequence):
            test_encoder = discord.opus.Encoder()
            opus = test_encoder.encode(raw[-3840:], 960)
            header = struct.pack(">BBHII", 0x80, 0x78, sequence, sequence * 960, ssrc)
            suffix = struct.pack(">I", sequence + 1)
            encrypted = nacl.secret.Aead(key).encrypt(opus, header, suffix + bytes(20)).ciphertext
            return header + encrypted + suffix

        async def require_decode(target, expected):
            deadline = asyncio.get_running_loop().time() + 3
            while target._decode_ok < expected and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.01)
            assert target._decode_ok == expected and target._decode_failed == 0

        # Actual Native websocket hook refreshes the fixture transport key; denied audio must not open ASR.
        second_key = bytes(range(32, 64))
        connection.secret_key = second_key
        await connection.hook(None, {"op": 4, "d": {}})
        await connection.hook(None, {"op": 5, "d": {"ssrc": 4321, "user_id": "84"}})
        before_decode = receiver._decode_ok
        sender.sendto(test_packet(second_key, 4321, 500), recv_socket.getsockname())
        await require_decode(receiver, before_decode + 1)
        assert manager.counts.get("native_stream_opened", 0) == 0
        report["sdk_secret_rekey_verified"] = True
        report["denied_source_opened_no_stream"] = True

        # Rejoin uses the actual Native join method, with only the channel transport as a fixture.
        receiver.stop()
        connection._connected.clear()
        connection._state = ConnectionFlowState.disconnected
        next_voice = discord.VoiceClient(native, channel)
        next_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        next_socket.bind(("127.0.0.1", 0))
        next_socket.setblocking(False)
        owned_voices.append((next_voice, next_socket))
        next_connection = next_voice._connection
        next_connection.socket = next_socket
        next_connection.secret_key = second_key
        next_connection.ssrc = 1999
        next_connection._state = ConnectionFlowState.connected
        next_connection._connected.set()
        async def fixture_connect():
            return next_voice
        channel.connect = fixture_connect
        assert await adapter.join_voice_channel(channel, text_channel_id=10)
        next_receiver = adapter._voice_receivers[7]
        owned_receivers.append(next_receiver)
        owned_listeners.append(adapter._voice_listen_tasks[7])
        manager._synchronize()
        assert manager.observers[7].voice is next_voice
        assert manager.observers[7].receiver is next_receiver
        assert len(next_connection._socket_reader._callbacks) == 2
        await next_connection.hook(None, {"op": 5, "d": {"ssrc": 5432, "user_id": "84"}})
        next_connection._socket_reader.resume(force=True)
        sender.sendto(test_packet(second_key, 5432, 600), next_socket.getsockname())
        await require_decode(next_receiver, 1)
        assert manager.counts.get("native_stream_opened", 0) == 0
        report["sdk_rejoin_same_adapter_verified"] = True
        return report
    finally:
        manager.close()
        for task in owned_listeners:
            task.cancel()
        await asyncio.gather(*owned_listeners, return_exceptions=True)
        for task in getattr(adapter, "_voice_timeout_tasks", {}).values():
            task.cancel()
        for owned_receiver in owned_receivers:
            owned_receiver.stop()
        for owned_voice, owned_socket in owned_voices:
            owned_voice._connection._socket_reader.stop()
            await asyncio.to_thread(owned_voice._connection._socket_reader.join, 2)
            owned_socket.close()
            owned_voice.cleanup()
        sender.close()
        await native.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-provider", action="store_true", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-tail", required=True)
    parser.add_argument("--seconds", type=float, default=8)
    args = parser.parse_args()
    assert 1 <= args.seconds <= 30
    assert sys.prefix == os.environ["VIRTUAL_ENV"]
    assert not args.output.exists()
    from hermes_constants import get_hermes_home
    from hermes_cli.plugins_loader import _plugin_home_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from tui_gateway.launch_profile_policy import owner_secret_scope
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from agent.transcription_registry import get_provider
    from hermes_cli.plugins import discover_plugins
    home = Path(get_hermes_home()).resolve()
    assert os.geteuid() == home.stat().st_uid, "Run as the actual consuming principal"
    before = {name: file_hash(home / name) for name in ("config.yaml", ".env")}
    source_hash = file_hash(args.fixture)
    with _plugin_home_scope(home):
        hydrate_profile_secret_sources(home)
        secret_token = set_secret_scope(owner_secret_scope(home), profile_home=str(home))
        try:
            discover_plugins(force=True)
            provider = get_provider("yandex", scope=str(home))
            assert provider is not None and provider._profile_secrets
            assert provider._client_factory.__module__ == "yandex_speech.transcription"
            assert provider._secret_resolver.__name__ == "_scoped_secret"
            report = asyncio.run(exercise(home, provider, args.fixture, args.seconds))
        finally:
            reset_secret_scope(secret_token)
    from yandex_speech.discord_ingress import _tail_digests
    wanted = _tail_digests(args.expected_tail)
    count = str(len(args.expected_tail.split()))
    report["final_tail_verified"] = report["tail_sha256"].get(count) == wanted.get(count)
    report["profile_files_unchanged"] = all(file_hash(home / name) == digest for name, digest in before.items())
    report["fixture_unchanged"] = file_hash(args.fixture) == source_hash
    descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump(report, target, indent=2)
    print(json.dumps(report))
    assert report["partial_before_input_end"] and report["final_tail_verified"]
    assert report["native_decode_failures"] == 0 and report["native_packets_decoded"] == report["udp_packets_sent"]
    assert report["stream_count"] == 1 and report["fixture_callback_count"] == 1
    assert report["recorded_asr_calls"] == 0 and report["native_voice_wav_deferred"] >= 1
    assert report["profile_files_unchanged"] and report["fixture_unchanged"]


if __name__ == "__main__":
    main()
