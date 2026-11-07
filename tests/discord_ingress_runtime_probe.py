"""Read-only runtime evidence, executed by sys.remote_exec in the target gateway."""
import json
import os
from pathlib import Path
import sys
import time
import uuid

module = sys.modules.get("yandex_speech.discord_ingress")
managers = []
if module is not None:
    with module._INGRESS_LOCK:
        owners = list(module._INGRESS_OWNERS.values())
    seen = set()
    for manager in owners:
        if id(manager) in seen:
            continue
        seen.add(id(manager))
        observers = []
        for guild_id, observer in list(manager.observers.items()):
            receiver = observer.receiver
            with receiver._lock:
                sizes = [len(buffer) for buffer in receiver._buffers.values()]
            observers.append({
                "guild_id": guild_id, "voice_channel": observer.channel_id,
                "text_channel": manager.binding(guild_id),
                "connected": observer.voice.is_connected(),
                "receiver_running": receiver._running, "receiver_paused": receiver._paused,
                "buffered_bytes": sum(sizes), "nonempty_buffers": sum(bool(size) for size in sizes),
                "decode_ok": receiver._decode_ok, "decode_failed": receiver._decode_failed,
                "dave_protocol_version": getattr(observer.connection, "dave_protocol_version", 0),
                "native_methods": {name: module._function_stamp(getattr(receiver, name, None))
                                   for name in ("_on_packet", "check_silence", "map_ssrc")},
            })
        managers.append({
            "run_id": manager.run_id, "home": str(manager.workspace),
            "enabled": manager.enabled, "closed": manager.closed,
            "watch_running": manager.watch_task is not None and not manager.watch_task.done(),
            "events_path": str(manager.events_path), "counts": dict(manager.counts),
            "adapter_identity": id(manager.adapter), "native_identity": id(manager.native),
            "native_methods": {name: module._function_stamp(getattr(manager.adapter, name, None))
                               for name in ("_voice_listen_loop", "_process_voice_input",
                                            "_is_allowed_user", "_voice_input_callback")},
            "plugin_methods": {name: module._function_stamp(getattr(manager, name))
                               for name in ("_deliver", "_open_native", "allowed")},
            "observers": observers,
        })
home = Path(os.environ["HERMES_HOME"])
result = {"pid": os.getpid(), "at": time.monotonic(), "managers": managers}
output = home / "cache" / ("discord-runtime-" + str(os.getpid()) + "-" + uuid.uuid4().hex + ".json")
descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as target:
    json.dump(result, target, indent=2)
