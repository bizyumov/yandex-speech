"""Read-only runtime evidence, executed by sys.remote_exec in the target gateway."""
import json
import os
from pathlib import Path
import sys
import time
import uuid

import hashlib,json,types
def normalize(value):
 if isinstance(value,types.CodeType):
  return {"bytecode":value.co_code.hex(),"constants":normalize(value.co_consts),
   "names":value.co_names,"variables":value.co_varnames,"free":value.co_freevars,"cells":value.co_cellvars,
   "arguments":(value.co_argcount,value.co_posonlyargcount,value.co_kwonlyargcount),"flags":value.co_flags,
   "exception_table":value.co_exceptiontable.hex(),"stack_size":value.co_stacksize}
 if isinstance(value,tuple):return {"tuple":[normalize(item) for item in value]}
 if isinstance(value,frozenset):return {"set":sorted((normalize(item) for item in value),key=lambda item:json.dumps(item,sort_keys=True))}
 if isinstance(value,bytes):return {"bytes":value.hex()}
 if value is None or isinstance(value,(bool,int,float,str)):return {"type":type(value).__name__,"value":value}
 return {"type":type(value).__name__,"repr":repr(value)}
def semantic_stamp(function):
 function=getattr(function,"func",function)
 function=getattr(function,"__func__",function)
 code=getattr(function,"__code__",None)
 return None if code is None else hashlib.sha256(json.dumps(normalize(code),sort_keys=True,separators=(",",":")).encode()).hexdigest()

def probe_stamp(function):
    function = getattr(function, "func", function)
    function = getattr(function, "__func__", function)
    code = getattr(function, "__code__", None)
    return None if code is None else {"file": code.co_filename,
        "code_sha256": hashlib.sha256(__import__("marshal").dumps(code)).hexdigest(),
        "semantic_sha256": semantic_stamp(function)}

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
                "native_methods": {name: probe_stamp(getattr(receiver, name, None))
                                   for name in ("_on_packet", "check_silence", "map_ssrc")},
            })
        managers.append({
            "run_id": manager.run_id, "home": str(manager.workspace),
            "enabled": manager.enabled, "closed": manager.closed,
            "watch_running": manager.watch_task is not None and not manager.watch_task.done(),
            "events_path": str(manager.events_path), "counts": dict(manager.counts),
            "adapter_identity": id(manager.adapter), "native_identity": id(manager.native),
            "native_methods": {name: probe_stamp(getattr(manager.adapter, name, None))
                               for name in ("_voice_listen_loop", "_process_voice_input",
                                            "_is_allowed_user", "_voice_input_callback")},
            "plugin_methods": {name: probe_stamp(getattr(manager, name))
                               for name in ("_deliver", "_open_native", "allowed")},
            "observers": observers,
        })
home = Path(os.environ["HERMES_HOME"])
module_info = {}
import hashlib
import marshal
for name in ("yandex_speech", "yandex_speech.discord_ingress", "hermes_plugins.yandex_speech",
             "hermes_plugins.yandex_speech.provider", "integrations.hermes"):
    candidate = sys.modules.get(name)
    if candidate is not None:
        module_info[name] = {"file": getattr(candidate, "__file__", None),
                             "path": list(getattr(candidate, "__path__", [])),
                             "register": probe_stamp(getattr(candidate, "register", None))}
registry = sys.modules.get("agent.transcription_registry")
provider = registry.get_provider("yandex", scope=str(home)) if registry is not None else None
provider_info = None if provider is None else {
    "module": type(provider).__module__,
    "class_file": probe_stamp(type(provider).__init__),
    "has_ingress": getattr(provider, "_discord_ingress", None) is not None,
    "has_capture_context": hasattr(provider, "_opening_capture"),
    "workspace": str(getattr(provider, "workspace", "")),
    "ingress_state": None if getattr(provider, "_discord_ingress", None) is None else {
        "enabled": provider._discord_ingress.enabled, "closed": provider._discord_ingress.closed,
        "has_adapter": provider._discord_ingress.adapter is not None,
        "has_native": provider._discord_ingress.native is not None,
        "factory_name": provider._discord_ingress.factory.__qualname__,
    },
}
activation = []
manager_info = {}
config_info = {}
plugin_module = sys.modules.get("hermes_cli.plugins")
loader = sys.modules.get("hermes_cli.plugins_loader")
activation_module = sys.modules.get("hermes_cli.plugins_activation")
config_module = sys.modules.get("hermes_cli.config")
if plugin_module is not None and loader is not None:
    with loader._plugin_home_scope(home):
        current_home = plugin_module._plugin_home_key()
        with plugin_module._plugin_managers_lock:
            plugin_manager = plugin_module._plugin_managers_by_home.get(current_home)
        assert plugin_manager is not None, "the owning plugin manager is not already cached"
        manager_info = {
            "home": str(plugin_manager.home_path),
            "scope": str(plugin_manager.scope_key),
            "discord_factories": [{"plugin": name, "qualname": getattr(factory, "__qualname__", "")}
                                  for factory, name in plugin_manager.get_platform_handler_factories("discord")],
            "listeners": [],
        }
        for listener in getattr(plugin_manager, "_plugin_loaded_listeners", []):
            code = getattr(listener, "__code__", None)
            cells = dict(zip(code.co_freevars, listener.__closure__ or ())) if code else {}
            loop_cell = cells.get("loop")
            loop_value = loop_cell.cell_contents if loop_cell is not None else None
            manager_info["listeners"].append({"name": getattr(listener, "__qualname__", ""),
                "captures_loop": loop_cell is not None, "loop_none": loop_value is None,
                "loop_closed": loop_value.is_closed() if loop_value is not None else None,
                "loop_running": loop_value.is_running() if loop_value is not None else None,
                "profile_name": cells["profile_name"].cell_contents if "profile_name" in cells else None,
                "profile_home": str(cells["profile_home"].cell_contents) if "profile_home" in cells else None})
        if activation_module is not None:
            items = activation_module.activation_summaries(plugin_manager) + activation_module.dependency_blocked_summaries(plugin_manager)
            activation = [{key: item[key] for key in ("name", "plugin", "id", "status", "state", "loaded", "restart_required") if key in item}
                          for item in items if "yandex" in str(item.get("name", item.get("plugin", item.get("id", ""))))]
        if config_module is not None:
            cfg = config_module.load_config_readonly() or {}
            stt = cfg.get("stt") or {}
            config_info = {"provider": stt.get("provider"), "streaming": stt.get("streaming"),
                           "plugin_enabled": "yandex-speech" in ((cfg.get("plugins") or {}).get("enabled") or [])}
import gc
adapter_info = []
for candidate in gc.get_objects():
    kind = type(candidate)
    if kind.__name__ != "DiscordAdapter" or "discord" not in kind.__module__:
        continue
    fields = vars(candidate)
    client = fields.get("_client")
    wired = fields.get("_plugin_handlers_wired")
    adapter_info.append({
        "module": kind.__module__, "identity": id(candidate),
        "has_client": client is not None,
        "stored_native_is_current": fields.get("_plugin_handler_native") is client,
        "wired_initialized": wired is not None,
        "wired_count": len(wired) if wired is not None else None,
        "wired_yandex": [list(key) for key in (wired or ()) if "yandex" in str(key[0])],
        "voices": len(fields.get("_voice_clients", {})),
        "native_methods": {name: probe_stamp(getattr(candidate, name, None))
                           for name in ("_wire_plugin_handlers", "rewire_plugin_handlers", "_voice_listen_loop")},
    })
result = {"pid": os.getpid(), "at": time.monotonic(), "managers": managers,
          "plugin_manager": manager_info, "adapters": adapter_info,
          "module_info": module_info, "provider": provider_info,
          "activation": activation, "configuration": config_info}
output = home / "cache" / ("discord-runtime-" + str(os.getpid()) + "-" + uuid.uuid4().hex + ".json")
descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as target:
    json.dump(result, target, indent=2)
