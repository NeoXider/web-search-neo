"""Audit helper part 3: iframe probing, pointer lock, limits and clamps."""
from __future__ import annotations

import inspect
import json
import re
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(r"C:\Git\PythonUrlFeatch")
sys.path.insert(0, str(ROOT))

import browser_tools  # noqa: E402
import msp_search  # noqa: E402
import web_client  # noqa: E402

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


def static_checks():
    print("=== STATIC / SOURCE CONSTANTS ===")
    src = (ROOT / "browser_tools.py").read_text(encoding="utf-8")
    for label, pattern in (
        ("open_many limit", r".{80}open_many.{200}"),
        ("challenge 20x20", r".{140}>=\s*20.{60}"),
    ):
        pass
    for name in ("open_pages",):
        fn = getattr(browser_tools, name, None)
        if fn:
            print(name, inspect.signature(fn))
    # numeric guards
    for pat in (r"len\(urls\)\s*>\s*\d+", r"urls\[:\d+\]", r"max\(1,\s*min\(len", r"\b16\b.*urls",
                r"MAX_CONCURRENT\w*\s*=\s*\d+", r"_MAX\w*\s*=\s*\d+"):
        for m in re.finditer(pat, src):
            print("browser_tools:", src[:m.start()].count("\n") + 1, m.group(0))
    wc = (ROOT / "web_client.py").read_text(encoding="utf-8")
    for pat in (r"MAX_REDIRECT\w*\s*=\s*\d+", r"redirect\w*\s*=\s*\d+", r"range\(\d+\)"):
        for m in re.finditer(pat, wc):
            print("web_client:", wc[:m.start()].count("\n") + 1, m.group(0))
    ms = (ROOT / "msp_search.py").read_text(encoding="utf-8")
    for pat in (r"[A-Z_]*(TTL|CACHE|COOLDOWN)[A-Z_]*\s*=\s*[\d.]+", r"len\(urls\)\s*>\s*\d+"):
        for m in re.finditer(pat, ms):
            print("msp_search:", ms[:m.start()].count("\n") + 1, m.group(0))
    mn = (ROOT / "main.py").read_text(encoding="utf-8")
    for pat in (r"len\(urls\)\s*>\s*\d+", r"len\(session_ids\)", r"1-16", r"1-4", r"urls\[:\s*\d+\]"):
        for m in re.finditer(pat, mn):
            print("main:", mn[:m.start()].count("\n") + 1, m.group(0))
    # wait_challenge clamp
    print("wait_for_challenge_resolution:",
          inspect.signature(browser_tools.wait_for_challenge_resolution))
    seg = src[src.index("def wait_for_challenge_resolution"):]
    print(seg[:900])


def run(base):
    opts = dict(profile_mode="temporary", headless=True)
    browser_tools.open_page(f"{base}/platformer.html", session_id="g", **opts)
    browser_tools.set_render_control("step", "g")

    # held W released by w
    r1 = browser_tools.input_batch(key_actions=[{"key": "W", "action": "hold"}], session_id="g")
    show("held after hold 'W'", r1.get("held_keys"))
    r2 = browser_tools.input_batch(key_actions=[{"key": "w", "action": "release"}], session_id="g")
    show("held after release 'w'", r2.get("held_keys"))

    pl = browser_tools.pointer_lock(action="status", session_id="g")
    show("pointer_lock(status)", pl)
    pl2 = browser_tools.pointer_lock(action="acquire", session_id="g")
    show("pointer_lock(acquire)", pl2)
    browser_tools.pointer_lock(action="release", session_id="g")

    # relative pointer motion is unclamped
    p = browser_tools.pointer_action("move", 4000, -3000, session_id="g",
                                     coordinate_mode="relative")
    show("pointer move relative (unclamped)", {k: p.get(k) for k in
         ("success", "x", "y", "coordinate_mode", "pointer_x", "pointer_y")})
    browser_tools.set_render_control("normal", "g")
    browser_tools.close_session("g")

    # iframe host
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
    show("host probe while only the FRAME is gated -> animation", hostprobe.get("animation"))
    show("host probe top-level fps/animation_suspended/reason present?",
         {k: (k in hostprobe) for k in ("fps", "animation_suspended", "reason")})
    browser_tools.set_render_control("normal", "h")
    browser_tools.close_session("h")


def main():
    static_checks()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("\nbase:", base)
    try:
        run(base)
    finally:
        browser_tools.close_all_sessions()
        server.shutdown()
        server.server_close()
    print("\nDONE3")


if __name__ == "__main__":
    main()
