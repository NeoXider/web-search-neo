import json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import browser_tools as bt

HTML = """<html><body><h1>hi</h1>
<img src="/missing-image.png">
<script>console.error('marker-once'); console.warn('warn-a','warn-b');</script>
<form><label>User <input name=u></label><button>Go</button></form>
</body></html>"""

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/page":
            b = HTML.encode(); self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self.send_header("Content-Length","0"); self.end_headers()

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"

sid = "audit-console"
def texts(entries): return [(e.get("kind"), e.get("level"), (e.get("text") or "")[:60]) for e in entries]

try:
    bt.open_page(f"{base}/page", session_id=sid, headless=True, profile_mode="temporary")
    time.sleep(1.5)
    print("### 1. game_probe FIRST (used to eat the browser log) ###")
    p1 = bt.game_probe(session_id=sid, sample_seconds=0.3)
    print("  probe console:", json.dumps(p1.get("console"), ensure_ascii=False)[:600])
    print("  console_error:", p1.get("console_error"))
    print("  animation:", json.dumps(p1.get("animation")))

    print()
    print("### 2. get_console AFTER game_probe: is the 404 still there? ###")
    c1 = bt.get_console(session_id=sid)
    print("  count:", c1.get("count"))
    for row in texts(c1["entries"]): print("   ", row)

    print()
    print("### 3. get_console again (cursor must not repeat) ###")
    c2 = bt.get_console(session_id=sid)
    print("  count:", c2.get("count"), texts(c2["entries"]))

    print()
    print("### 4. game_probe again: does it repeat old messages? ###")
    p2 = bt.game_probe(session_id=sid, sample_seconds=0.3)
    print("  probe console:", json.dumps(p2.get("console"), ensure_ascii=False)[:600])

    print()
    print("### 5. duplicate suppression: one console.error -> how many rows? ###")
    d = bt._get_session(sid).driver
    d.execute_script("console.error('dupe-check-777');")
    time.sleep(1.5)
    c3 = bt.get_console(session_id=sid)
    rows = [r for r in c3["entries"] if "dupe-check-777" in (r.get("text") or "")]
    print("  rows for dupe-check-777:", len(rows), texts(rows))

    print()
    print("### 6. game_probe under the render gate: fps must not be measured ###")
    bt.set_render_control("step", session_id=sid)
    p3 = bt.game_probe(session_id=sid, sample_seconds=0.3)
    print("  animation:", json.dumps(p3.get("animation")))
    bt.set_render_control("normal", session_id=sid)

    print()
    print("### 7. page_text fallback on a form-only page ###")
    d.get("data:text/html;charset=utf-8,<html><body><form><label>Card <input name=c></label><button>Pay</button></form></body></html>")
    t = bt.get_page_text(session_id=sid)
    print("  ", json.dumps({k: v for k, v in t.items() if k in
        ("mode","mode_used","fallback_used","root_tag","root_reason","chars","total_chars","truncated","text")}, ensure_ascii=False)[:400])

    print()
    print("### 8. page_text with include_links and a tiny budget ###")
    d.get(f"{base}/page")
    for lim in (200, 400, 20000):
        t = bt.get_page_text(session_id=sid, max_chars=lim, include_links=True)
        print(f"   max_chars={lim}: chars={t['chars']} max_chars={t['max_chars']} truncated={t['truncated']} links={len(t.get('links',[]))} total={t['total_chars']}")
        assert t["chars"] <= t["max_chars"], "BUDGET OVERFLOW"
finally:
    bt.close_session(sid)
    srv.shutdown()
