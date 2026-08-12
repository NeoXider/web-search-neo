"""Audit helper part 4: locators, upload, latency ratios."""
from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(r"C:\Git\PythonUrlFeatch")
sys.path.insert(0, str(ROOT))

import browser_tools  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "games"

FORM_HTML = """<!doctype html>
<html><head><title>Form default</title></head><body>
<h1>Application</h1><p>Some readable prose that survives the main-mode filter and proves
that page_text keeps block structure across paragraphs.</p>
<a id="fixture-link" href="/relative">Fixture link</a>
<div id="host"></div>
<form id="application" action="/submit" method="post" enctype="multipart/form-data">
  <label for="candidate-name">Candidate name</label>
  <input id="candidate-name" name="candidate_name" required>
  <label for="resume">Resume</label>
  <input id="resume" name="resume" type="file">
  <label for="resumes">Resumes</label>
  <input id="resumes" name="resumes" type="file" multiple>
  <button id="submit-button" type="submit">Apply</button>
</form>
<script>
const host = document.getElementById('host');
const root = host.attachShadow({mode: 'open'});
root.innerHTML = "<label for='postcode'>Postcode</label><input id='postcode' name='postcode'>";
setTimeout(() => {
  const late = document.createElement('p');
  late.id = 'step-2';
  late.textContent = 'step two is here';
  document.body.appendChild(late);
}, 700);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        p = urlparse(self.path).path
        if p.startswith("/fixtures/"):
            c = (FIXTURES / p[len("/fixtures/"):]).resolve()
            if c.is_file():
                self._send(c.read_text(encoding="utf-8"))
                return
        if p == "/form":
            self._send(FORM_HTML)
            return
        if p == "/relative":
            self._send("<html><title>Relative</title><body>relative target</body></html>")
            return
        self._send("<html><body>nf</body></html>", 404)

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._send("<html><head><title>Submitted</title></head><body>ok</body></html>")

    def log_message(self, *_a):
        return


def show(label, value):
    print(f"\n----- {label} -----")
    print(json.dumps(value, indent=1, default=str)[:3000] if isinstance(value, (dict, list))
          else str(value)[:3000])


def run(base):
    opts = dict(profile_mode="temporary", headless=True)
    browser_tools.open_page(f"{base}/form", session_id="a", **opts)

    # page_text default mode on a normal page
    t = browser_tools.get_page_text(session_id="a")
    show("page_text default", {k: t.get(k) for k in
         ("mode", "mode_used", "fallback_used", "root_reason", "chars", "text")})

    # outline sees the open shadow root
    o = browser_tools.get_page_outline(session_id="a", limit=100)
    show("outline (shadow root visible?)", o["outline"])
    show("closed_shadow_roots", o.get("closed_shadow_roots"))

    # find -> click on a ref
    f = browser_tools.find_elements("apply button", session_id="a", role="button", limit=3)
    ref = f["matches"][0]["ref"]
    show("find -> ref", {"ref": ref, "role": f["matches"][0]["role"],
                         "name": f["matches"][0]["name"],
                         "low_confidence": f.get("low_confidence")})
    try:
        w = browser_tools.wait_for_element(ref, session_id="a", state="clickable",
                                           timeout_seconds=5)
        show("wait(ref, clickable)", {k: w.get(k) for k in ("success", "state", "selector",
                                                            "found", "matched")})
    except Exception as exc:
        show("wait(ref) raised", f"{type(exc).__name__}: {exc}")

    # piercing path fill
    try:
        pf = browser_tools.fill_fields({"#host >>> #postcode": "10115"}, session_id="a")
        show("fill via piercing path", {k: pf.get(k) for k in ("success", "filled", "errors")})
    except Exception as exc:
        show("piercing fill raised", f"{type(exc).__name__}: {exc}")

    # CSS containing ' >>> ' stays CSS
    try:
        cf = browser_tools.fill_fields({"input[data-op='a >>> b']": "x"}, session_id="a")
        show("css containing ' >>> '", {k: cf.get(k) for k in ("success", "errors")})
    except Exception as exc:
        show("css containing ' >>> ' raised", f"{type(exc).__name__}: {exc}")

    # submit form_selector as a ref
    fo = browser_tools.find_elements("application form", session_id="a", limit=5)
    show("find form matches", [(m["ref"], m["role"], m["name"]) for m in fo["matches"]])

    # wait for a late element
    lw = browser_tools.wait_for_element("#step-2", session_id="a", state="visible",
                                        timeout_seconds=5)
    show("wait css visible keys", sorted(lw))

    # upload rules
    tmp1 = ROOT / "_audit_docs_tmp1.txt"
    tmp2 = ROOT / "_audit_docs_tmp2.txt"
    tmp1.write_text("a", encoding="utf-8")
    tmp2.write_text("b", encoding="utf-8")
    try:
        up = browser_tools.upload_file("#resumes", [str(tmp1), str(tmp2)], session_id="a")
        show("upload 2 files (multiple)", {k: up.get(k) for k in
             ("success", "files_uploaded", "file_names")})
        try:
            browser_tools.upload_file("#resume", [str(tmp1), str(tmp2)], session_id="a")
            show("upload 2 into non-multiple", "NO ERROR (unexpected)")
        except Exception as exc:
            show("upload 2 into non-multiple", f"{type(exc).__name__}: {exc}")
        try:
            browser_tools.upload_file("#candidate-name", [str(tmp1)], session_id="a")
        except Exception as exc:
            show("upload into a text input", f"{type(exc).__name__}: {exc}")
        try:
            browser_tools.upload_file("#resume", [str(ROOT / "nope.txt")], session_id="a")
        except Exception as exc:
            show("upload a missing path", f"{type(exc).__name__}: {exc}")
    finally:
        tmp1.unlink(missing_ok=True)
        tmp2.unlink(missing_ok=True)

    browser_tools.close_session("a")

    # ---- latency ratios ----
    browser_tools.open_page(f"{base}/fixtures/games/platformer.html", session_id="g", **opts)
    browser_tools.set_render_control("step", "g")

    def bench(fn, n=20):
        samples = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - t0) * 1000)
        samples.sort()
        return round(statistics.median(samples), 1), round(samples[int(0.95 * (n - 1))], 1)

    res = {
        "step(1) summary=True": bench(lambda: browser_tools.render_step(1, "g")),
        "step(1) summary=False": bench(lambda: browser_tools.render_step(1, "g",
                                                                        include_summary=False)),
        "input 2 keys + pointer summary=True": bench(lambda: browser_tools.input_batch(
            key_actions=[{"key": "ARROW_RIGHT", "action": "tap"}, {"key": "SPACE", "action": "tap"}],
            pointer_actions=[{"action": "hover", "x": 100, "y": 100}], session_id="g")),
        "input 2 keys + pointer summary=False": bench(lambda: browser_tools.input_batch(
            key_actions=[{"key": "ARROW_RIGHT", "action": "tap"}, {"key": "SPACE", "action": "tap"}],
            pointer_actions=[{"action": "hover", "x": 100, "y": 100}], session_id="g",
            include_summary=False)),
        "press_keys tap summary=True": bench(lambda: browser_tools.press_keys(
            ["SPACE"], session_id="g", focus_mode="none")),
        "press_keys tap summary=False": bench(lambda: browser_tools.press_keys(
            ["SPACE"], session_id="g", focus_mode="none", include_summary=False)),
        "pointer hover summary=True": bench(lambda: browser_tools.pointer_action(
            "hover", 100, 100, session_id="g")),
    }
    show("latency median/p95 ms", res)

    browser_tools.set_render_control("normal", "g")
    browser_tools.close_session("g")


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
    print("\nDONE4")


if __name__ == "__main__":
    main()
