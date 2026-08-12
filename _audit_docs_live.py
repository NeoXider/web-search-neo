"""Audit helper: run real Chrome sessions and dump the response shapes the docs promise."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(r"C:\Git\PythonUrlFeatch")
sys.path.insert(0, str(ROOT))

import browser_tools  # noqa: E402

FIXTURE_ROOT = (ROOT / "tests" / "fixtures").resolve()

FORM_HTML = """<!doctype html>
<html><head><title>Form default</title></head><body>
<a id="fixture-link" href="/relative">Fixture link</a>
<p id="session-marker">default</p>
<p id="click-state">not clicked</p>
<button id="action-button" type="button"
  onclick="document.getElementById('click-state').textContent='clicked'">
  Run action
</button>
<form id="application" action="/submit" method="post" enctype="multipart/form-data">
  <label for="candidate-name">Candidate name</label>
  <input id="candidate-name" name="candidate_name" required>
  <label for="cover-letter">Cover letter</label>
  <textarea id="cover-letter" name="cover_letter"></textarea>
  <label for="role">Role</label>
  <select id="role" name="role">
    <option value="python">Python</option>
    <option value="unity">Unity Developer</option>
  </select>
  <label><input id="remote" name="remote" type="checkbox">Remote</label>
  <label for="resume">Resume</label>
  <input id="resume" name="resume" type="file">
  <button id="submit-button" type="submit">Apply</button>
</form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/fixtures/"):
            candidate = (FIXTURE_ROOT / parsed.path[len("/fixtures/"):]).resolve()
            if candidate.is_file():
                payload = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._send_html("missing", 404)
            return
        if parsed.path == "/form":
            self._send_html(FORM_HTML)
            return
        if parsed.path == "/relative":
            self._send_html("<html><title>Relative</title><body>relative target</body></html>")
            return
        if parsed.path == "/formonly":
            # A document that is nothing but form chrome, to exercise the main->full fallback.
            self._send_html(
                "<!doctype html><html><head><title>Login</title></head><body>"
                "<form id='login'><label for='u'>User</label><input id='u'>"
                "<label for='p'>Password</label><input id='p' type='password'>"
                "<button id='go'>Sign in</button></form></body></html>"
            )
            return
        self._send_html("<html><body>not found</body></html>", 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._send_html(
            "<html><head><title>Submitted</title></head>"
            "<body><h1 id='result'>Application submitted</h1></body></html>"
        )

    def log_message(self, *_a):
        return


def show(label, value, depth=None):
    print(f"\n----- {label} -----")
    if isinstance(value, dict):
        printable = {}
        for k, v in value.items():
            if isinstance(v, str) and len(v) > 600:
                printable[k] = v[:600] + f"...<{len(v)} chars>"
            else:
                printable[k] = v
        print(json.dumps(printable, indent=1, default=str)[:6000])
    else:
        print(str(value)[:6000])


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


def run(base):
    opts = dict(profile_mode="temporary", headless=True)

    # ---------------- FORMS ----------------
    res = browser_tools.open_page(f"{base}/form", session_id="apply", **opts)
    show("open() keys", sorted(res))
    show("open() challenge fields", {k: res.get(k) for k in
         ("challenge_detected", "challenge_type", "challenge_evidence", "title", "url")})

    outline = browser_tools.get_page_outline(session_id="apply", limit=80)
    show("page_outline (text) keys", sorted(outline))
    show("page_outline text", outline.get("text") or outline.get("outline"))
    show("page_outline scalars", {k: v for k, v in outline.items()
                                  if not isinstance(v, (list, dict)) and k not in ("text", "outline")})

    outline_json = browser_tools.get_page_outline(session_id="apply", limit=80, output="json")
    nodes = outline_json.get("nodes") or []
    show("page_outline json top keys", sorted(outline_json))
    show("page_outline json node[3]", nodes[3] if len(nodes) > 3 else nodes)

    elements = browser_tools.get_page_elements(session_id="apply")
    show("page_elements top keys", sorted(elements))
    show("page_elements fields", elements.get("fields"))
    show("page_elements forms", elements.get("forms"))
    show("page_elements iframes present?", "iframes" in elements)

    el_nolinks = browser_tools.get_page_elements(session_id="apply", include_links=False)
    show("page_elements include_links=False keys", sorted(el_nolinks))

    found = browser_tools.find_elements("cover letter", session_id="apply", limit=5)
    show("find keys", sorted(found))
    show("find matches", found.get("matches"))

    ref = (found.get("matches") or [{}])[0].get("ref")
    print("\nfirst ref:", ref)

    filled = browser_tools.fill_fields(
        {"#candidate-name": "Neo Candidate", "#cover-letter": "Unity and C# experience",
         "#role": "unity", "#remote": True, "#missing-field": "x"},
        session_id="apply",
    )
    show("fill (partial failure) keys", sorted(filled))
    show("fill result", {k: filled.get(k) for k in ("success", "filled", "files_uploaded", "errors")})

    filefail = browser_tools.fill_fields({"#resume": "C:/tmp/x.pdf"}, session_id="apply")
    show("fill file-in-fields errors", filefail.get("errors"))

    # select by visible text
    sel_text = browser_tools.fill_fields({"#role": "Unity Developer"}, session_id="apply")
    show("fill select by visible text", {k: sel_text.get(k) for k in ("success", "filled", "errors")})

    # radio refusal message is not in this fixture; check the source string instead.

    # submit with a missing required field cleared
    browser_tools.fill_fields({"#candidate-name": ""}, session_id="apply")
    bad = browser_tools.submit_form("#application", session_id="apply")
    show("submit (validation fails) keys", sorted(bad))
    show("submit validation", {k: bad.get(k) for k in
         ("success", "validation_passed", "submit_triggered", "validation_errors")})

    browser_tools.fill_fields({"#candidate-name": "Neo Candidate"}, session_id="apply")
    good = browser_tools.submit_form("#application", session_id="apply",
                                     submit_selector="#submit-button")
    show("submit (ok)", {k: good.get(k) for k in
         ("success", "validation_passed", "submit_triggered", "submit_event_fired",
          "navigation_observed", "title", "url")})

    # ref staleness after navigation
    if ref:
        try:
            stale = browser_tools.click(ref, session_id="apply")
            show("stale ref click result", stale)
        except Exception as exc:
            show("stale ref click raised", f"{type(exc).__name__}: {exc}")

    # network + console
    net = browser_tools.get_network(session_id="apply", limit=20)
    show("network keys", sorted(net))
    show("network text", net.get("text") or net.get("lines"))
    netj = browser_tools.get_network(session_id="apply", limit=5, output="json")
    show("network json first row", (netj.get("requests") or netj.get("entries") or [None])[0])

    con = browser_tools.get_console(session_id="apply", limit=10)
    show("console keys", sorted(con))

    # page_text fallback
    browser_tools.open_page(f"{base}/formonly", session_id="apply", **opts)
    txt = browser_tools.get_page_text(session_id="apply", max_chars=2000)
    show("page_text (form-only page)", {k: txt.get(k) for k in
         ("mode", "mode_used", "fallback_used", "chars", "total_chars", "truncated",
          "max_chars", "root_reason", "text")})

    txt2 = browser_tools.get_page_text(session_id="apply", max_chars=400, include_links=True)
    show("page_text include_links keys", sorted(txt2))
    print("chars<=max_chars?", txt2["chars"], txt2["max_chars"], len(txt2["text"]))

    browser_tools.close_session("apply")

    # ---------------- GAMES ----------------
    game_url = f"{base}/fixtures/games/platformer.html"
    browser_tools.open_page(game_url, session_id="game", **opts)

    probe = browser_tools.game_probe(session_id="game", sample_seconds=0.5)
    show("game_probe top keys", sorted(probe))
    show("game_probe", {k: probe.get(k) for k in
         ("canvas_count", "canvases", "iframes", "document_has_focus", "navigation_ms",
          "animation", "held_inputs", "console")})

    rend = browser_tools.set_render_control("step", "game")
    show("render(step) keys", sorted(rend))
    show("render(step)", {k: rend.get(k) for k in
         ("mode", "target_fps", "frame_delta_ms", "time_frozen", "timers_gated",
          "pending_callbacks", "input_advances_frame", "key_repeat", "frame_selector")})

    probe_gated = browser_tools.game_probe(session_id="game", sample_seconds=0.5)
    show("game_probe under gate", {k: probe_gated.get(k) for k in
         ("animation", "fps", "animation_suspended", "reason")})

    st = browser_tools.render_step(3, "game")
    show("step keys", sorted(st))
    show("step", {k: st.get(k) for k in
         ("frames", "callbacks", "pending_callbacks", "pending_timers", "frame_count",
          "virtual_now", "gate_reinstalled", "mode")})

    # nothing moves between steps
    before = browser_tools.get_page_text(session_id="game", max_chars=300)["text"]
    import time as _t
    _t.sleep(1.5)
    after = browser_tools.get_page_text(session_id="game", max_chars=300)["text"]
    show("step-mode frozen? before", before)
    show("step-mode frozen? after 1.5s", after)
    print("FROZEN:", before == after)

    v1 = browser_tools.render_step(1, "game")["virtual_now"]
    v2 = browser_tools.render_step(1, "game")["virtual_now"]
    print("virtual_now delta for 1 frame:", v2 - v1)

    inp = browser_tools.input_batch(
        key_actions=[{"key": "ARROW_RIGHT", "action": "hold"}],
        session_id="game", target_selector="#game",
    )
    show("input keys", sorted(inp))
    show("input", {k: inp.get(k) for k in ("success", "keys", "held_keys", "frames", "pointers")})

    st_after = browser_tools.render_step(1, "game")
    show("frame_count after input+step", st_after.get("frame_count"))

    rel = browser_tools.release_inputs("game")
    show("release_inputs", {k: rel.get(k) for k in ("success", "held_keys", "held_buttons")})

    pk = browser_tools.press_keys(["SPACE"], session_id="game", key_action="tap",
                                  hold_frames=3, focus_mode="none")
    show("press_keys keys", sorted(pk))
    show("press_keys", {k: pk.get(k) for k in
         ("success", "keys", "key_action", "repeat", "hold_seconds", "hold_frames", "frames")})

    # include_summary=false
    st_lean = browser_tools.render_step(1, "game", include_summary=False)
    show("step include_summary=False keys", sorted(st_lean))

    # navigation resets / reports render_mode
    nav = browser_tools.open_page(game_url, session_id="game", **opts)
    show("open() after gate: render fields", {k: nav.get(k) for k in
         ("render_mode", "render_mode_restored", "held_keys_released")})

    browser_tools.set_render_control("normal", "game")
    browser_tools.close_session("game")
    print("\nDONE")


if __name__ == "__main__":
    main()
