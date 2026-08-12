"""Audit helper part 2: games/input/render/frames behaviour claims."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(r"C:\Git\PythonUrlFeatch")
sys.path.insert(0, str(ROOT))

import browser_tools  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "games"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(FIXTURES), **kw)

    def log_message(self, *_a):
        return


def show(label, value):
    print(f"\n----- {label} -----")
    print(json.dumps(value, indent=1, default=str)[:4000] if isinstance(value, (dict, list))
          else str(value)[:4000])


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("base:", base)
    try:
        run(base)
    finally:
        browser_tools.close_all_sessions()
        server.shutdown()
        server.server_close()


def status_line(sid):
    return browser_tools.get_page_text(session_id=sid, max_chars=400)["text"].splitlines()[-1]


def run(base):
    opts = dict(profile_mode="temporary", headless=True)
    browser_tools.open_page(f"{base}/platformer.html", session_id="g", **opts)

    # --- network text output shape ---
    net = browser_tools.get_network(session_id="g", limit=5)
    show("network default(text) keys", sorted(net))
    show("network requests (text lines)", net.get("requests"))
    show("network format field", net.get("format"))

    browser_tools.set_render_control("step", "g")

    # --- press_keys hold_frames keeps the key down N frames ---
    browser_tools.input_batch(key_actions=[{"key": "ARROW_RIGHT", "action": "hold"}],
                              session_id="g", target_selector="#game")
    before = status_line("g")
    pk = browser_tools.press_keys(["SPACE"], session_id="g", action="tap",
                                  hold_frames=3, focus_mode="none")
    show("press_keys keys", sorted(pk))
    show("press_keys report", {k: pk.get(k) for k in
         ("success", "keys", "action", "repeat", "hold_seconds", "hold_frames",
          "frames_advanced", "held_keys", "focus_mode")})
    after = status_line("g")
    show("status before press_keys", before)
    show("status after press_keys(hold_frames=3)", after)

    browser_tools.release_inputs("g")

    # --- key held as 'W' released by 'w' ---
    browser_tools.input_batch(key_actions=[{"key": "W", "action": "hold"}], session_id="g")
    show("held after hold W", browser_tools.get_status("g").get("held_keys"))
    r = browser_tools.input_batch(key_actions=[{"key": "w", "action": "release"}], session_id="g")
    show("held after release w", r.get("held_keys"))

    # --- throttled mode ---
    th = browser_tools.set_render_control("throttled", "g", target_fps=200)
    show("render(throttled, target_fps=200) clamp", {k: th.get(k) for k in
         ("mode", "target_fps", "time_frozen", "timers_gated", "input_advances_frame")})
    a = status_line("g")
    time.sleep(1.0)
    b = status_line("g")
    show("throttled: moved in 1s?", (a, b, a != b))

    # step in throttled mode should be refused
    try:
        browser_tools.render_step(1, "g")
        show("render_step in throttled", "NO ERROR (unexpected)")
    except Exception as exc:
        show("render_step in throttled raises", f"{type(exc).__name__}: {exc}")

    # --- frame_delta_ms custom ---
    browser_tools.set_render_control("step", "g", frame_delta_ms=100.0)
    v1 = browser_tools.render_step(1, "g")["virtual_now"]
    v2 = browser_tools.render_step(1, "g")["virtual_now"]
    show("custom frame_delta_ms=100 -> virtual_now delta", v2 - v1)

    # --- navigation re-arms the gate, releases held input ---
    browser_tools.input_batch(key_actions=[{"key": "ARROW_LEFT", "action": "hold"}], session_id="g")
    nav = browser_tools.open_page(f"{base}/platformer.html", session_id="g", **opts)
    show("open() after gate", {k: nav.get(k) for k in
         ("render_mode", "render_mode_restored")})
    show("held keys after navigation", browser_tools.get_status("g").get("held_keys"))
    st = browser_tools.render_step(1, "g")
    show("step right after navigation", {k: st.get(k) for k in
         ("success", "frames", "frame_count", "gate_reinstalled")})

    # --- touch emulation resets render mode ---
    te = browser_tools.set_touch_emulation(session_id="g", enabled=True, max_touch_points=5)
    show("touch_emulation keys", sorted(te))
    show("touch_emulation", {k: te.get(k) for k in
         ("success", "enabled", "max_touch_points", "reloaded", "reload_page",
          "render_mode", "max_touch_points_reported")})
    show("render mode after touch_emulation", browser_tools.get_status("g").get("render_mode"))

    # --- pointer_lock without selector ---
    pl = browser_tools.pointer_lock(operation="status", session_id="g")
    show("pointer_lock(status) keys", sorted(pl))
    show("pointer_lock(status)", pl)
    pl2 = browser_tools.pointer_lock(operation="acquire", session_id="g")
    show("pointer_lock(acquire)", {k: pl2.get(k) for k in
         ("success", "locked", "selector", "target", "operation", "element")})
    browser_tools.pointer_lock(operation="release", session_id="g")

    browser_tools.close_session("g")

    # --- iframe host: game_probe describes the host and lists iframes ---
    browser_tools.open_page(f"{base}/iframe_host.html", session_id="h", **opts)
    host = browser_tools.game_probe(session_id="h", sample_seconds=0.3)
    show("iframe host probe", {k: host.get(k) for k in
         ("canvas_count", "canvases", "iframe_count", "iframes")})
    inner = browser_tools.game_probe(session_id="h", frame_selector="#game-frame",
                                     sample_seconds=0.3)
    show("iframe inner probe", {k: inner.get(k) for k in
         ("canvas_count", "canvases", "frame_selector")})
    browser_tools.set_render_control("step", "h", frame_selector="#game-frame")
    hostprobe = browser_tools.game_probe(session_id="h", sample_seconds=0.3)
    show("host probe while frame gated", hostprobe.get("animation"))
    browser_tools.set_render_control("normal", "h")
    browser_tools.close_session("h")

    print("\nDONE2")


if __name__ == "__main__":
    main()
