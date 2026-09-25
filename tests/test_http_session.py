"""Named cookie sessions for http_request (``http_session``), unit and live.

The live tests start two local http.server instances on 127.0.0.1 (two ports,
so two origins sharing one host): the real web_client sends the requests, no
Chrome is involved.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.cookiejar import Cookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from web_search_neo import main, web_client
from web_search_neo.fetch import api
from web_search_neo.fetch import sessions as http_sessions


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.delenv(http_sessions.HTTP_SESSION_TTL_ENV, raising=False)
    monkeypatch.delenv("WEB_SEARCH_NEO_PROXY", raising=False)
    http_sessions.clear_all()
    yield
    http_sessions.clear_all()


class _Handler(BaseHTTPRequestHandler):
    issued: list[str] = []

    def log_message(self, *_args) -> None:
        pass

    def _reply(self, status: int, payload: dict, cookies: tuple[str, ...] = (),
               location: str | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/login":
            sid = secrets.token_hex(8)
            _Handler.issued.append(sid)
            self._reply(200, {"logged_in": True}, (
                f"sid={sid}; HttpOnly; Path=/; SameSite=Lax",
                "theme=dark; Path=/prefs",
            ))
        elif path == "/logout":
            self._reply(200, {"logged_out": True}, ("sid=; Max-Age=0; Path=/",))
        elif path == "/login-then-echo":
            sid = secrets.token_hex(8)
            _Handler.issued.append(sid)
            self._reply(302, {}, (f"sid={sid}; HttpOnly; Path=/",), location="/echo")
        elif path == "/to-other":
            target = parse_qs(parsed.query).get("to", [""])[0]
            self._reply(302, {}, location=target)
        else:  # /echo and anything else: say which cookies arrived
            self._reply(200, {"cookie": self.headers.get("Cookie"), "path": path})

    do_GET = _route
    do_POST = _route


@contextmanager
def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.fixture()
def origins():
    _Handler.issued = []
    with _serve() as first, _serve() as second:
        yield first, second


def _echoed(result: dict) -> str | None:
    return json.loads(result["body"])["cookie"]


def _names(entries: list[dict]) -> list[str]:
    return [entry["name"] for entry in entries]


# ---------------------------------------------------------------- live


def test_live_login_cookie_rides_on_the_next_request(origins):
    base, _ = origins
    login = api.http_request(f"{base}/login", method="POST", http_session="shop")
    sid = _Handler.issued[-1]
    block = login["http_session"]
    assert block["created"] is True and block["name"] == "shop" and block["agent_label"] is None
    received = {entry["name"]: entry for entry in block["received_cookies"]}
    assert received["sid"]["httponly"] is True
    assert received["sid"]["path"] == "/"
    assert received["sid"]["samesite"] == "Lax"
    assert received["sid"]["secure"] is False
    assert received["sid"]["expires"] is None  # a session cookie
    assert received["sid"]["stored"] is True
    assert received["theme"]["path"] == "/prefs"

    echo = api.http_request(f"{base}/echo", http_session="shop")
    assert _echoed(echo) == f"sid={sid}"  # theme is scoped to /prefs
    assert _names(echo["http_session"]["sent_cookies"]) == ["sid"]
    assert echo["http_session"]["created"] is False

    prefs = api.http_request(f"{base}/prefs/page", http_session="shop")
    assert sorted(_echoed(prefs).split("; ")) == sorted([f"sid={sid}", "theme=dark"])


def test_live_values_hidden_unless_show_values(origins):
    base, _ = origins
    hidden = api.http_request(f"{base}/login", http_session="s")
    sid = _Handler.issued[-1]
    text = json.dumps(hidden)
    assert sid not in text
    assert "sid=<redacted>" in text
    assert hidden["http_session"]["values_shown"] is False
    assert all("value" not in entry for entry in hidden["http_session"]["received_cookies"])

    sent = api.http_request(f"{base}/other", http_session="s")
    assert all("value" not in entry for entry in sent["http_session"]["sent_cookies"])

    shown = api.http_request(f"{base}/login", http_session="s", show_values=True)
    sid2 = _Handler.issued[-1]
    received = {entry["name"]: entry for entry in shown["http_session"]["received_cookies"]}
    assert received["sid"]["value"] == sid2
    assert any(sid2 in item for item in shown["set_cookies"])
    echo = api.http_request(f"{base}/echo", http_session="s", show_values=True)
    assert echo["http_session"]["sent_cookies"][0]["value"] == sid2


def test_live_other_name_and_other_agent_do_not_see_the_jar(origins):
    base, _ = origins
    api.http_request(f"{base}/login", http_session="shop", agent_label="agent-a")
    sid = _Handler.issued[-1]

    same = api.http_request(f"{base}/echo", http_session="shop", agent_label="agent-a")
    assert _echoed(same) == f"sid={sid}"
    other_name = api.http_request(f"{base}/echo", http_session="shop2", agent_label="agent-a")
    assert _echoed(other_name) is None
    other_agent = api.http_request(f"{base}/echo", http_session="shop", agent_label="agent-b")
    assert _echoed(other_agent) is None
    unlabelled = api.http_request(f"{base}/echo", http_session="shop")
    assert _echoed(unlabelled) is None
    no_session = api.http_request(f"{base}/echo")
    assert _echoed(no_session) is None
    assert "http_session" not in no_session
    # The jar survived the unrelated calls in between.
    again = api.http_request(f"{base}/echo", http_session="shop", agent_label="agent-a")
    assert _echoed(again) == f"sid={sid}"


def test_live_clear_empties_the_jar_before_the_request(origins):
    base, _ = origins
    api.http_request(f"{base}/login", http_session="c")
    cleared = api.http_request(f"{base}/echo", http_session="c", http_session_clear=True)
    assert cleared["http_session"]["cleared"] is True
    assert cleared["http_session"]["created"] is True
    assert _echoed(cleared) is None
    assert cleared["http_session"]["sent_cookies"] == []
    # Clearing a name that holds nothing is not an error.
    again = api.http_request(f"{base}/echo", http_session="c", http_session_clear=True)
    assert again["http_session"]["cleared"] is True


def test_live_server_deletion_removes_the_cookie(origins):
    base, _ = origins
    api.http_request(f"{base}/login", http_session="d")
    out = api.http_request(f"{base}/logout", http_session="d")
    deleted = [entry for entry in out["http_session"]["received_cookies"] if entry["name"] == "sid"]
    assert deleted and deleted[0]["deleted"] is True and deleted[0]["stored"] is False
    echo = api.http_request(f"{base}/echo", http_session="d")
    assert "sid=" not in (_echoed(echo) or "")


def test_live_cookie_set_on_a_redirect_hop_is_stored_and_sent_on_the_next_hop(origins):
    base, _ = origins
    result = api.http_request(f"{base}/login-then-echo", http_session="r")
    sid = _Handler.issued[-1]
    assert _echoed(result) == f"sid={sid}"
    block = result["http_session"]
    assert _names(block["received_cookies"]) == ["sid"]
    assert block["received_cookies"][0]["url"].endswith("/login-then-echo")
    assert _names(block["sent_cookies"]) == ["sid"]
    later = api.http_request(f"{base}/echo", http_session="r")
    assert _echoed(later) == f"sid={sid}"


def test_live_cross_origin_redirect_still_strips_session_cookies(origins):
    base, other = origins
    api.http_request(f"{base}/login", http_session="x")
    sid = _Handler.issued[-1]
    # Cookies are not port-scoped: a direct call to the other port carries it...
    direct = api.http_request(f"{other}/echo", http_session="x")
    assert _echoed(direct) == f"sid={sid}"
    # ...but a redirect onto another origin never does, as without a session.
    hopped = api.http_request(f"{base}/to-other?to={other}/echo", http_session="x")
    assert hopped["url"].startswith(other)
    assert not _echoed(hopped)
    sent = hopped["http_session"]["sent_cookies"]
    assert [(entry["name"], entry["url"].startswith(base)) for entry in sent] == [("sid", True)]
    # The jar is untouched by the stripped hop.
    back = api.http_request(f"{base}/echo", http_session="x")
    assert _echoed(back) == f"sid={sid}"


def test_live_without_session_nothing_carries_over(origins):
    base, _ = origins
    first = api.http_request(f"{base}/login")
    assert first["set_cookies"] and "http_session" not in first
    assert _echoed(api.http_request(f"{base}/echo")) is None


def test_live_ttl_expiry_starts_a_new_jar(origins, monkeypatch):
    base, _ = origins
    clock = [1000.0]
    monkeypatch.setattr(http_sessions, "_now", lambda: clock[0])
    monkeypatch.setenv(http_sessions.HTTP_SESSION_TTL_ENV, "60")
    api.http_request(f"{base}/login", http_session="t")
    sid = _Handler.issued[-1]
    clock[0] += 59
    kept = api.http_request(f"{base}/echo", http_session="t")
    assert _echoed(kept) == f"sid={sid}"
    clock[0] += 61
    expired = api.http_request(f"{base}/echo", http_session="t")
    assert _echoed(expired) is None
    assert expired["http_session"]["expired"] is True
    assert expired["http_session"]["created"] is True


def test_live_main_wrapper_passes_session_arguments(origins):
    base, _ = origins
    asyncio.run(main.http_request(f"{base}/login", "POST", http_session="w", agent_label="m"))
    sid = _Handler.issued[-1]
    echo = asyncio.run(main.http_request(f"{base}/echo", http_session="w", agent_label="m"))
    assert _echoed(echo) == f"sid={sid}"
    assert echo["http_session"]["agent_label"] == "m"


# ---------------------------------------------------------------- unit


def test_ttl_setting_has_a_floor_and_a_default(monkeypatch):
    monkeypatch.setenv(http_sessions.HTTP_SESSION_TTL_ENV, "5")
    assert http_sessions.ttl_seconds() == 60
    monkeypatch.setenv(http_sessions.HTTP_SESSION_TTL_ENV, "nonsense")
    assert http_sessions.ttl_seconds() == 1800
    monkeypatch.setenv(http_sessions.HTTP_SESSION_TTL_ENV, "7200")
    assert http_sessions.ttl_seconds() == 7200


def test_session_count_is_bounded_least_recently_used_first(monkeypatch):
    monkeypatch.setattr(http_sessions, "MAX_SESSIONS", 3)
    clock = [0.0]
    monkeypatch.setattr(http_sessions, "_now", lambda: clock[0])
    jars = {}
    for name in ("a", "b", "c"):
        clock[0] += 1
        jars[name], _ = http_sessions.open_jar(name)
    clock[0] += 1
    http_sessions.open_jar("a")  # a is now the most recent
    clock[0] += 1
    _, info = http_sessions.open_jar("d")
    assert info["evicted"] == ["b"]
    assert http_sessions.open_jar("a")[0] is jars["a"]
    assert http_sessions.open_jar("b")[1]["created"] is True


def test_eviction_of_another_agents_jar_is_not_disclosed(monkeypatch):
    monkeypatch.setattr(http_sessions, "MAX_SESSIONS", 1)
    http_sessions.open_jar("secret-name", "other-agent")
    _, info = http_sessions.open_jar("mine", "me")
    assert "evicted" not in info


@pytest.mark.parametrize("name", ["", " ", "a/b", "../x", "x" * 65, "-lead", None, "a b"])
def test_bad_session_names_are_refused(name):
    with pytest.raises(ValueError):
        http_sessions.open_jar(name)


def test_label_namespaces_are_independent():
    unlabelled, _ = http_sessions.open_jar("n")
    blank, _ = http_sessions.open_jar("n", "  ")
    labelled, _ = http_sessions.open_jar("n", "agent")
    assert unlabelled is blank
    assert labelled is not unlabelled


def test_clear_without_a_name_is_refused():
    with pytest.raises(ValueError, match="http_session_clear needs http_session"):
        api.http_request("http://127.0.0.1:9/x", http_session_clear=True)


def test_cookie_header_and_session_are_mutually_exclusive():
    with pytest.raises(ValueError, match="Cookie header"):
        api.http_request("http://127.0.0.1:9/x", headers={"cookie": "a=b"}, http_session="s")


def test_no_session_does_not_pass_a_jar(monkeypatch):
    seen = {}

    class _Response:
        url = "http://127.0.0.1:9/x"
        status_code = 200
        headers = {}
        content = b""
        text = ""

    def fake(url, **kwargs):
        seen.update(kwargs)
        return _Response()

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    api.http_request("http://127.0.0.1:9/x")
    assert "cookie_jar" not in seen
    api.http_request("http://127.0.0.1:9/x", http_session="s")
    assert isinstance(seen["cookie_jar"], http_sessions.SessionJar)


def _cookie(name: str, value: str, domain: str = "127.0.0.1", path: str = "/") -> Cookie:
    return Cookie(0, name, value, None, False, domain, False, False, path, True, False,
                  None, False, None, None, {"HttpOnly": None})


def test_merge_back_stores_changes_and_deletions_only():
    jar = http_sessions.SessionJar()
    jar.set_cookie(_cookie("keep", "1"))
    jar.set_cookie(_cookie("gone", "2"))
    jar.set_cookie(_cookie("change", "3"))
    call_jar = web_client.requests.cookies.RequestsCookieJar()
    seeded = web_client._seed_cookies(call_jar, jar)
    call_jar.clear("127.0.0.1", "/", "gone")
    call_jar.set_cookie(_cookie("change", "4"))
    call_jar.set_cookie(_cookie("new", "5"))
    # A concurrent call on the same jar added a cookie meanwhile: it survives.
    jar.set_cookie(_cookie("parallel", "6"))
    web_client._merge_cookies_back(call_jar, jar, seeded)
    assert {c.name: c.value for c in jar} == {"keep": "1", "change": "4", "new": "5",
                                                "parallel": "6"}
    assert list(call_jar) == []


def test_describe_cookie_flags():
    cookie = Cookie(0, "sid", "v", None, False, ".example.com", True, True, "/app", True,
                    True, 2_000_000_000, False, None, None, {"httponly": None, "SameSite": "Strict"})
    info = http_sessions.describe_cookie(cookie)
    assert info == {
        "name": "sid", "domain": ".example.com", "host_only": False, "path": "/app",
        "secure": True, "httponly": True, "samesite": "Strict",
        "expires": "2033-05-18T03:33:20+00:00",
    }
    assert http_sessions.describe_cookie(cookie, show_values=True)["value"] == "v"


def test_redact_set_cookie_keeps_attributes():
    assert http_sessions.redact_set_cookie("sid=abc; Path=/; HttpOnly") == (
        "sid=<redacted>; Path=/; HttpOnly"
    )
    assert http_sessions.redact_set_cookie("sid=; Max-Age=0") == "sid=<redacted>; Max-Age=0"


def test_action_schema_lists_the_session_parameters():
    schema = asyncio.run(main.web_info("action_schema", {"action": "http_request"}))
    properties = schema["input_schema"]["properties"]
    assert {"http_session", "agent_label", "http_session_clear", "show_values"} <= set(properties)
    assert "url" in schema["input_schema"]["required"]
    assert not {"http_session", "show_values"} & set(schema["input_schema"]["required"])
