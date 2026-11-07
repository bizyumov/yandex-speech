"""Invoke the existing Native rewire lifecycle on the current adapter; no patching."""
import gc
import json
import os
from pathlib import Path
import uuid

home = Path(os.environ["HERMES_HOME"]).resolve()
assert os.geteuid() == home.stat().st_uid
candidates = [obj for obj in gc.get_objects()
              if type(obj).__name__ == "DiscordAdapter"
              and "discord" in type(obj).__module__
              and getattr(obj, "_client", None) is not None]
assert len(candidates) == 1, "activation requires one unambiguous existing Discord adapter"
adapter = candidates[0]
for name in ("rewire_plugin_handlers", "_wire_plugin_handlers"):
    function = getattr(getattr(adapter, name), "__func__")
    assert Path(function.__code__.co_filename).resolve() == Path("/opt/hermes/src/gateway/platforms/base.py")
assert getattr(adapter, "_plugin_handler_native", None) is adapter._client
assert getattr(adapter, "_plugin_handlers_wired", None) is not None
loop = adapter._client.loop
assert loop.is_running() and not loop.is_closed()

def activate_on_owner_loop():
    from hermes_cli.plugins_loader import _plugin_home_scope
    from agent.transcription_registry import get_provider
    result = {"pid": os.getpid(), "adapter_identity": id(adapter)}
    try:
        with _plugin_home_scope(home):
            adapter.rewire_plugin_handlers()
            provider = get_provider("yandex", scope=str(home))
            ingress = getattr(provider, "_discord_ingress", None)
            result["attached"] = ingress is not None and ingress.adapter is adapter and ingress.native is adapter._client
            result["run_id"] = ingress.run_id if ingress is not None else None
            result["observers"] = len(ingress.observers) if ingress is not None else 0
    except Exception as error:
        result["attached"] = False
        result["error_type"] = type(error).__name__
    target = home / "cache" / ("discord-wired-" + str(os.getpid()) + "-" + uuid.uuid4().hex + ".json")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(result, output, indent=2)

loop.call_soon_threadsafe(activate_on_owner_loop)
