"""Audit helper: dump published parameter defaults for every MCP action/topic."""
import inspect
import json
import sys

sys.path.insert(0, r"C:\Git\PythonUrlFeatch")

import main  # noqa: E402

out = {}
for name, spec in main._ACTIONS.items():
    sig = inspect.signature(spec.handler)
    params = {}
    for pname, p in sig.parameters.items():
        params[pname] = "REQUIRED" if p.default is inspect._empty else repr(p.default)
    out[name] = params

print("=== ACTIONS ===")
print(json.dumps(out, indent=1))

print("\n=== INFO TOPICS ===")
print(json.dumps(sorted(main._INFO_TOPICS), indent=1))
print("topic count:", len(main._INFO_TOPICS), "action count:", len(main._ACTIONS))

print("\n=== TOPIC HANDLER DEFAULTS ===")
tout = {}
for tname, h in main._TOPIC_HANDLERS.items():
    sig = inspect.signature(h)
    tout[tname] = {p: ("REQUIRED" if v.default is inspect._empty else repr(v.default)) for p, v in sig.parameters.items()}
print(json.dumps(tout, indent=1))
