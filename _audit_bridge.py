import json, socket as pysocket, threading, time, traceback
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed
import bridge_auth
from chrome_bridge import CHROME_EXTENSION_ID, ChromeBridge

TOK = "a1" * 32
OTHER = "b2" * 32

def free_port():
    with pysocket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

def sock(port, origin=f"chrome-extension://{CHROME_EXTENSION_ID}"):
    return connect(f"ws://127.0.0.1:{port}", origin=origin)

def hello(token=TOK, nonce="0f"*16, **extra):
    m = {"type": "hello", "protocol": 1, "browser": {"name": "T"}}
    if token is not None: m["token"] = token
    if nonce is not None: m["nonce"] = nonce
    m.update(extra); return json.dumps(m)

def section(t): print("\n### " + t + " " + "#"*(60-len(t)))

# ---- 1. simultaneous connect race -------------------------------------
section("1. two valid clients connect simultaneously")
b = ChromeBridge(port=free_port(), token=TOK); b.start()
results = []
barrier = threading.Barrier(2)
def client(tag):
    try:
        ws = sock(b.port)
        barrier.wait()
        ws.send(hello(nonce=tag*8))
        ack = json.loads(ws.recv(timeout=5))
        ok = bridge_auth.verify(TOK, tag*8, ack.get("proof"))
        results.append((tag, "ack", ok))
        return ws
    except Exception as e:
        results.append((tag, "exc", f"{type(e).__name__}: {e}"))
threads = [threading.Thread(target=client, args=(t,)) for t in ("aaaa", "bbbb")]
for t in threads: t.start()
for t in threads: t.join(10)
print("results:", results)
print("connected:", b.connected, "browser:", b.browser_info)
b.shutdown(); time.sleep(0.2)

# ---- 2. evicted handler must not wipe the new connection ---------------
section("2. old socket lingers, then dies: does it clear the live connection?")
b = ChromeBridge(port=free_port(), token=TOK); b.start()
first = sock(b.port); first.send(hello(nonce="1"*16))
print("first ack:", json.loads(first.recv(timeout=5))["type"], "connected:", b.wait_connected(2))
second = sock(b.port); second.send(hello(nonce="2"*16))
print("second ack:", json.loads(second.recv(timeout=5))["type"])
time.sleep(0.5)
print("after eviction, connected:", b.connected)
try:
    first.recv(timeout=2)
except Exception as e:
    print("first socket:", type(e).__name__)
time.sleep(1.0)
print("connected after old handler finished:", b.connected)
answers = []
th = threading.Thread(target=lambda: answers.append(("ok", b.request("tabs.list", timeout=6))))
th.start()
cmd = json.loads(second.recv(timeout=5))
second.send(json.dumps({"type": "result", "id": cmd["id"], "result": ["live"]}))
th.join(8); print("request through the surviving socket:", answers)
second.close(); first.close(); b.shutdown(); time.sleep(0.2)

# ---- 3. nonce edge cases ----------------------------------------------
section("3. nonce validation")
b = ChromeBridge(port=free_port(), token=TOK); b.start()
for label, n in [("missing", None), ("empty", ""), ("int", 12345), ("list", ["a"]),
                 ("257 chars", "x"*257), ("256 chars", "x"*256), ("bool", True), ("null-json", None)]:
    ws = sock(b.port)
    m = {"type": "hello", "protocol": 1, "token": TOK}
    if n is not None: m["nonce"] = n
    ws.send(json.dumps(m))
    try:
        r = ws.recv(timeout=3)
        print(f"  nonce={label:10s} -> ACCEPTED {json.loads(r)}")
    except ConnectionClosed as e:
        print(f"  nonce={label:10s} -> closed {e.rcvd.code} {e.rcvd.reason!r}")
    except Exception as e:
        print(f"  nonce={label:10s} -> {type(e).__name__}: {e}")
    try: ws.close()
    except Exception: pass
b.shutdown(); time.sleep(0.2)

# ---- 4. replay of the same nonce ---------------------------------------
section("4. replaying the same nonce twice")
b = ChromeBridge(port=free_port(), token=TOK); b.start()
proofs = []
for i in range(2):
    ws = sock(b.port); ws.send(hello(nonce="deadbeef"*4))
    proofs.append(json.loads(ws.recv(timeout=5)).get("proof")); ws.close(); time.sleep(0.2)
print("identical proofs for the same nonce:", proofs[0] == proofs[1])
b.shutdown(); time.sleep(0.2)

# ---- 5. garbage first frames -------------------------------------------
section("5. malformed / hostile first frames")
b = ChromeBridge(port=free_port(), token=TOK); b.start()
for label, payload in [("not json", "hello"), ("json scalar", "123"), ("json list", "[1,2]"),
                       ("json null", "null"), ("wrong protocol", json.dumps({"type":"hello","protocol":2,"token":TOK,"nonce":"aa"})),
                       ("wrong token", hello(token=OTHER)), ("no token", hello(token=None)),
                       ("token int", json.dumps({"type":"hello","protocol":1,"token":123,"nonce":"aa"}))]:
    ws = sock(b.port); ws.send(payload)
    try:
        r = ws.recv(timeout=3); print(f"  {label:16s} -> ACCEPTED {r[:120]}")
    except ConnectionClosed as e:
        print(f"  {label:16s} -> closed {e.rcvd.code} {e.rcvd.reason!r}")
    except Exception as e:
        print(f"  {label:16s} -> {type(e).__name__}: {e}")
    try: ws.close()
    except Exception: pass
print("still accepting a good client afterwards:", end=" ")
ws = sock(b.port); ws.send(hello(nonce="ff"*16))
print(json.loads(ws.recv(timeout=5))["type"]); ws.close()
b.shutdown(); time.sleep(0.2)

# ---- 6. leak checks -----------------------------------------------------
section("6. does the token appear in status / errors?")
b = ChromeBridge(port=free_port(), token=TOK); b.start()
st = json.dumps(b.status(0.1))
print("status:", st)
print("TOKEN in status:", TOK in st)
try:
    b.request("tabs.list", timeout=0.2)
except Exception as e:
    print("not-connected error contains token:", TOK in str(e))
    print("error text:", str(e)[:200])
b.shutdown(); time.sleep(0.2)

# ---- 7. origin enforcement ---------------------------------------------
section("7. origin enforcement (a web page cannot connect)")
b = ChromeBridge(port=free_port(), token=TOK); b.start()
for origin in ["http://evil.test", "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", None]:
    try:
        ws = connect(f"ws://127.0.0.1:{b.port}", origin=origin) if origin else connect(f"ws://127.0.0.1:{b.port}")
        ws.send(hello()); print(f"  origin={origin!r:50s} -> {ws.recv(timeout=3)[:60]}"); ws.close()
    except Exception as e:
        print(f"  origin={origin!r:50s} -> {type(e).__name__}: {str(e)[:80]}")
b.shutdown(); time.sleep(0.2)

# ---- 8. silent connection holds a server thread -------------------------
section("8. clients that connect and stay silent")
b = ChromeBridge(port=free_port(), token=TOK); b.start()
idle = [sock(b.port) for _ in range(12)]
t0 = time.time()
good = sock(b.port); good.send(hello(nonce="ab"*16))
print("good client ack while 12 idle sockets are open:", json.loads(good.recv(timeout=6))["type"],
      f"({time.time()-t0:.2f}s)")
for ws in idle:
    try: ws.close()
    except Exception: pass
good.close(); b.shutdown()
