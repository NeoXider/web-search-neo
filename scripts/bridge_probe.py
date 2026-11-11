"""Time each step of the Chrome bridge on one fresh background tab.

    python scripts/bridge_probe.py

When `open` fails with "cdp.send timed out" the question is whether the bridge is broken or
Chrome is merely slow. This opens one blank tab in the agent group, attaches the debugger,
runs two trivial scripts, subscribes to the console and closes the tab, printing how long
each step took. On an idle machine every line is well under a second; on 2026-11-11, with
the CPU at 100 %, creating the tab took 10 s and attaching took 12.7 s, and nothing was
broken. It needs the companion connected and touches no other tab.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_search_neo.bridge_attach import attach_debugger, attach_timeout  # noqa: E402
from web_search_neo.chrome_bridge import DEFAULT_TAB_GROUP, get_chrome_bridge  # noqa: E402

SLOW_SECONDS = 5.0


def main() -> int:
    bridge = get_chrome_bridge()
    bridge.start()
    if not bridge.wait_connected(10.0):
        print(f"The companion is not connected: {bridge.startup_error or 'no answer on the bridge port'}")
        return 2
    slow: list[str] = []

    def step(label: str, call):
        started = time.monotonic()
        try:
            result = call()
        except Exception as exc:
            took = time.monotonic() - started
            print(f"{label:28} FAIL {took:6.2f} s  {type(exc).__name__}: {str(exc)[:150]}")
            slow.append(label)
            return None
        took = time.monotonic() - started
        print(f"{label:28} ok   {took:6.2f} s")
        if took >= SLOW_SECONDS:
            slow.append(label)
        return result

    def script(expression: str):
        return bridge.request("cdp.send", {"tabId": tab_id, "method": "Runtime.evaluate",
                                           "params": {"expression": expression, "returnByValue": True}}, timeout=30.0)

    step("tabs.list", lambda: bridge.request("tabs.list", {}, timeout=15.0))
    tab = step("tabs.create", lambda: bridge.request(
        "tabs.create", {"url": "about:blank", "group": DEFAULT_TAB_GROUP, "active": False}, timeout=60.0))
    if not tab:
        return 1
    tab_id = int(tab["id"])
    bridge.claim_tab(tab_id)
    try:
        timing = step("debugger.attach", lambda: attach_debugger(bridge, tab_id, created=False))
        if isinstance(timing, dict) and not timing.get("reused"):
            print(f"{'':28}      attach {timing.get('attach_ms', 0) / 1000:.2f} s, "
                  f"setup {timing.get('setup_ms', 0) / 1000:.2f} s (as the companion measured it)")
        elif timing is None:
            print(f"{'':28}      this companion is older than 1.30: reload it to attach once per tab")
        step("first script", lambda: script("1+1"))
        step("second script", lambda: script("2+2"))
        step("events.subscribe", lambda: bridge.request(
            "events.subscribe", {"tabId": tab_id, "domains": ["console"]}, timeout=15.0))
    finally:
        step("tabs.remove", lambda: bridge.request("tabs.remove", {"tabId": tab_id}, timeout=15.0))
        try:
            bridge.release_tab(tab_id)
        except Exception:
            pass
    if slow:
        print(f"\nSlow or failed: {', '.join(slow)}. Chrome is answering slowly; this usually means the "
              f"machine is busy with other work. The server waits up to {attach_timeout():.0f} s for the "
              "attach (WSN_ATTACH_TIMEOUT).")
        return 1
    print("\nEvery step answered quickly: the bridge is healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
