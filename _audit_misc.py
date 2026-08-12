import json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import browser_tools as bt
import page_perception as pp

LINKS = "".join(f'<p>Paragraph number {i} with some filler text to spend the budget.</p><a href="/x{i}">Link number {i}</a>' for i in range(60))
HTML = f"""<html><body><main><h1>Big</h1>{LINKS}</main>
<script>console.error('boot-error');</script>
<img src="/missing.png">
</body></html>"""
FORM = """<html><body><h2>Login</h2><form id=f><label>User <input id=u name=u></label>
<label>Pass <input id=p type=password name=p></label><button id=go type=button>Sign in</button></form>
<div id=host></div><script>
const h=document.getElementById('host');const r=h.attachShadow({mode:'open'});
r.innerHTML='<div class="in"><button id="deep">Deep</button><slot></slot></div>';
h.insertAdjacentHTML('beforeend','<span>slotted-text</span>');
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def log_message(self,*a): pass
    def do_GET(self):
        body = {"/big":HTML, "/form":FORM}.get(self.path)
        if body is None:
            self.send_response(404); self.send_header("Content-Length","0"); self.end_headers(); return
        b=body.encode(); self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)

srv=ThreadingHTTPServer(("127.0.0.1",0),H)
threading.Thread(target=srv.serve_forever,daemon=True).start()
base=f"http://127.0.0.1:{srv.server_port}"
sid="audit-misc"
try:
    bt.open_page(f"{base}/big", session_id=sid, headless=True, profile_mode="temporary")
    d = bt._get_session(sid).driver
    time.sleep(1.5)

    print("### A. game_probe console_messages repetition ###")
    for i in range(3):
        p = bt.game_probe(session_id=sid, sample_seconds=0.2)
        msgs = p.get("console_messages")
        print(f"  call {i+1}: n={len(msgs)}", [m['message'][:45] for m in msgs])
    print("  get_console then game_probe again:")
    c = bt.get_console(session_id=sid)
    print("   get_console n=", len(c["entries"]))
    p = bt.game_probe(session_id=sid, sample_seconds=0.2)
    print("   game_probe n=", len(p.get("console_messages")), [m['message'][:45] for m in p.get("console_messages")])

    print()
    print("### B. page_text budget with links ###")
    for lim in (200, 500, 1000, 3000, 100000):
        t = bt.get_page_text(session_id=sid, max_chars=lim, include_links=True)
        ok = t["chars"] <= t["max_chars"]
        print(f"  max={lim:6d} -> chars={t['chars']:6d} limit={t['max_chars']:6d} trunc={t['truncated']} links={len(t.get('links',[]))} total={t['total_chars']} within_budget={ok}")
    t0 = bt.get_page_text(session_id=sid, max_chars=500, include_links=False)
    print(f"  no-links max=500 -> chars={t0['chars']} trunc={t0['truncated']}")

    print()
    print("### C. refs: epoch, staleness, legacy form ###")
    d.get(f"{base}/form")
    time.sleep(0.5)
    out = bt.get_page_outline(session_id=sid, limit=100, output="json")
    refs = [n.get("ref") for n in out.get("nodes", []) if n.get("ref")]
    print("  sample refs:", refs[:6])
    target = next((n for n in out["nodes"] if n.get("tag") == "button"), None)
    print("  button node:", {k: target.get(k) for k in ("ref","tag","name")} if target else None)
    ref = target["ref"]
    epoch, num = pp.parse_ref(ref)
    print("  parsed:", epoch, num)
    print("  resolve current ref ->", bool(d.execute_script("return " + pp.ref_expression(ref) + ";")))
    print("  resolve legacy ref:%d ->" % num, bool(d.execute_script("return " + pp.ref_expression(f"ref:{num}") + ";")))
    print("  resolve wrong epoch ->", d.execute_script("return " + pp.ref_expression(f"ref:deadbeefdeadbeef:{num}") + ";"))
    # click through the ref
    r = bt.click(ref, session_id=sid, wait_seconds=0.0)
    print("  click via ref ok:", r.get("success"))
    # detach the element -> ref must go stale
    d.execute_script("document.getElementById('go').remove();")
    try:
        bt.click(ref, session_id=sid, wait_seconds=0.0)
        print("  click after detach: NO ERROR  <-- unexpected")
    except Exception as e:
        print("  click after detach:", type(e).__name__, str(e)[:90])
    # navigate -> epoch changes
    old_ref = ref
    d.get(f"{base}/big")
    time.sleep(0.5)
    out2 = bt.get_page_outline(session_id=sid, limit=50, output="json")
    new_epoch, _ = pp.parse_ref(next(n["ref"] for n in out2["nodes"] if n.get("ref")))
    print("  epoch changed after navigation:", new_epoch != epoch, epoch, "->", new_epoch)
    try:
        bt.click(old_ref, session_id=sid, wait_seconds=0.0)
        print("  stale cross-page ref click: NO ERROR <-- unexpected")
    except Exception as e:
        print("  stale cross-page ref:", type(e).__name__, str(e)[:70])
    try:
        bt.click(f"ref:{num}", session_id=sid, wait_seconds=0.0)
        print("  LEGACY ref:%d on a DIFFERENT page: resolved and clicked (no epoch guard)" % num)
    except Exception as e:
        print("  legacy ref on new page:", type(e).__name__, str(e)[:70])

    print()
    print("### D. shadow DOM + slotted text in page_text ###")
    d.get(f"{base}/form"); time.sleep(0.4)
    t = bt.get_page_text(session_id=sid, mode="full")
    print("  text:", json.dumps(t["text"])[:300])
    print("  contains 'Deep':", "Deep" in t["text"], "| contains 'slotted-text':", "slotted-text" in t["text"],
          "| slotted count:", t["text"].count("slotted-text"))

    print()
    print("### E. piercing path + wait_for_element on a ref ###")
    print("  piercing:", bt.click("#host >>> #deep", session_id=sid, wait_seconds=0.0).get("success"))
    o = bt.get_page_outline(session_id=sid, limit=100, output="json")
    r2 = next(n["ref"] for n in o["nodes"] if n.get("tag") == "input")
    w = bt.wait_for_element(r2, session_id=sid, state="visible", timeout_seconds=3)
    print("  wait_for_element on ref:", w.get("success"))
    try:
        bt.wait_for_element("ref:99999", session_id=sid, state="visible", timeout_seconds=1)
        print("  wait on a bogus ref: NO ERROR <-- unexpected")
    except Exception as e:
        print("  wait on a bogus ref:", type(e).__name__, str(e)[:80])
finally:
    bt.close_session(sid); srv.shutdown()
