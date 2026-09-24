"""Regression coverage for 1.19.0 (functional audit and the orchestrator's QA pains).

Keyboard events are checked in a real Chrome against what a real US keyboard
produces, for both input paths: the Selenium one (temporary/isolated/persistent
profiles) and the companion one (current Chrome), whose exact CDP parameters are
replayed here through the same Chrome.
"""

from __future__ import annotations

import pytest

from web_search_neo import browser_tools
from web_search_neo.chrome_bridge import ChromeBridgeDriver


def _open_or_skip(local_site, session_id, name):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(
            f"{local_site.base_url}/fixtures/perception/{name}", session_id=session_id,
            width=1024, height=768, headless=True, profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    return browser_tools._get_session(session_id).driver


# What a real US keyboard makes Chrome fire for each key, typed into a focused
# textarea: (keydown key, code, keyCode, location), keypress charCode or None,
# and the beforeinput inputType or None.
REAL_KEYBOARD = {
    "Enter": (("Enter", "Enter", 13, 0), 13, "insertLineBreak"),
    # Chrome reports the numpad Enter's location itself (1 on this build), so it is not pinned.
    "NumpadEnter": (("Enter", "NumpadEnter", 13, None), 13, "insertLineBreak"),
    "Tab": (("Tab", "Tab", 9, 0), None, None),
    "Backspace": (("Backspace", "Backspace", 8, 0), None, "deleteContentBackward"),
    "Delete": (("Delete", "Delete", 46, 0), None, "deleteContentForward"),
    "ArrowLeft": (("ArrowLeft", "ArrowLeft", 37, 0), None, None),
    "Home": (("Home", "Home", 36, 0), None, None),
    "End": (("End", "End", 35, 0), None, None),
    "Space": ((" ", "Space", 32, 0), 32, "insertText"),
    "a": (("a", "KeyA", 65, 0), 97, "insertText"),
    "1": (("1", "Digit1", 49, 0), 49, "insertText"),
}


class _ReplayBridge:
    """The companion path: its CDP parameters, executed by the same Chrome."""

    def __init__(self, selenium_driver):
        self.selenium = selenium_driver

    def as_driver(self) -> ChromeBridgeDriver:
        bridge_driver = ChromeBridgeDriver.__new__(ChromeBridgeDriver)
        bridge_driver._modifier_mask = 0
        bridge_driver.execute_cdp_cmd = lambda method, params, timeout=None: \
            self.selenium.execute_cdp_cmd(method, params)
        return bridge_driver


def _events_for(driver, send, key):
    driver.execute_script(
        "const t=document.getElementById('ta'); t.value='abc'; t.focus();"
        "t.setSelectionRange(2, 2); window.__events.length = 0;"
    )
    send(key)
    events = driver.execute_script("return window.__events.slice()")
    value = driver.execute_script("return document.getElementById('ta').value")
    return events, value


def _assert_like_a_keyboard(key, events, value):
    (dkey, code, key_code, location), press, input_type = REAL_KEYBOARD[key]
    down = next(e for e in events if e["t"] == "keydown")
    seen_location = down["location"] if location is not None else None
    assert (down["key"], down["code"], down["keyCode"], seen_location) == (dkey, code, key_code, location), (key, down)
    presses = [e for e in events if e["t"] == "keypress"]
    assert ([p["charCode"] for p in presses] or [None]) == [press], (key, events)
    inputs = [e["inputType"] for e in events if e["t"] == "beforeinput"]
    assert (inputs or [None]) == [input_type], (key, events)
    assert any(e["t"] == "keyup" for e in events), (key, events)
    if key in {"Enter", "NumpadEnter"}:
        assert value == "ab\nc"  # the new line really lands


@pytest.mark.parametrize("key", sorted(REAL_KEYBOARD))
def test_selenium_keys_match_a_real_keyboard(local_site, key):
    driver = _open_or_skip(local_site, "keys-selenium", "key_events.html")
    events, value = _events_for(
        driver,
        lambda k: browser_tools.press_keys([k], session_id="keys-selenium", focus_mode="none"),
        key,
    )
    _assert_like_a_keyboard(key, events, value)


@pytest.mark.parametrize("key", sorted(REAL_KEYBOARD))
def test_companion_keys_match_a_real_keyboard(local_site, key):
    driver = _open_or_skip(local_site, "keys-bridge", "key_events.html")
    bridge = _ReplayBridge(driver).as_driver()
    normalized = browser_tools._normalize_game_key(key if key != "Space" else " ")
    events, value = _events_for(
        driver,
        lambda _k: bridge.perform_key_events(
            [{"type": "down", "key": normalized}, {"type": "up", "key": normalized}]
        ),
        key,
    )
    _assert_like_a_keyboard(key, events, value)


def test_type_text_keys_mode_types_a_new_line_in_both_paths(local_site):
    driver = _open_or_skip(local_site, "keys-newline", "key_events.html")
    driver.execute_script("const t=document.getElementById('ta'); t.value=''; t.focus();")
    browser_tools.type_text("ab\ncd", session_id="keys-newline", mode="keys")
    assert driver.execute_script("return document.getElementById('ta').value") == "ab\ncd"
    driver.execute_script("const t=document.getElementById('ta'); t.value=''; t.focus();")
    from web_search_neo.actions import verification

    _ReplayBridge(driver).as_driver().perform_key_events(verification.text_key_events("x\ny"))
    assert driver.execute_script("return document.getElementById('ta').value") == "x\ny"


def test_key_names_accept_dom_spellings_and_chords():
    from web_search_neo import key_table

    assert browser_tools._normalize_game_key("KeyW") == "w"
    assert browser_tools._normalize_game_key("Digit1") == "1"
    assert browser_tools._normalize_game_key("ArrowLeft") == browser_tools._KEY_ALIASES["LEFT"]
    assert browser_tools._normalize_game_key("ShiftLeft") == browser_tools._KEY_ALIASES["SHIFT"]
    assert key_table.expand_chords(["Control+Shift+K", "+"]) == ["Control", "Shift", "K", "+"]
    with pytest.raises(ValueError, match="ArrowLeft"):
        browser_tools._normalize_game_key("NotAKey")


# ------------------------------------------------ network: unfinished requests are seen


def test_requests_whose_body_nobody_reads_are_listed(local_site):
    import time

    _open_or_skip(local_site, "net-pending", "react_like.html")
    browser_tools.execute_js(
        "fetch('/error'); fetch('/submit-here', {method: 'POST', body: 'x=1'}); return 1;",
        session_id="net-pending",
    )
    deadline = time.monotonic() + 10
    urls = []
    while time.monotonic() < deadline:
        rows = browser_tools.get_network("net-pending", output="json", limit=100)["requests"]
        urls = [(row["method"], row["url"].rsplit("/", 1)[-1], row.get("status")) for row in rows]
        if ("GET", "error", 503) in urls and any(m == "POST" for m, _, _ in urls):
            break
        time.sleep(0.2)
    assert ("GET", "error", 503) in urls, urls
    assert any(m == "POST" and u == "submit-here" for m, u, _ in urls), urls
    errors = browser_tools.get_network("net-pending", only_errors=True, output="json")
    assert any(row["url"].endswith("/error") for row in errors["requests"]), errors
    finished_only = browser_tools.get_network("net-pending", output="json", include_pending=False)
    assert all(row.get("done") is not False for row in finished_only["requests"])


def test_a_network_window_never_hides_rows_silently(monkeypatch):
    from web_search_neo import network_log

    class _Session:
        driver = object()
        network_rows = [{"method": "GET", "url": f"https://a.test/{i}", "status": 200, "done": True}
                        for i in range(7)]
        network_pending = {"p": {"method": "POST", "url": "https://a.test/post", "status": 500}}
        network_dropped = 0

    monkeypatch.setattr(network_log.diagnostics, "selenium_network_rows", lambda driver, pending: [])
    answer = network_log.report(_Session(), "s", url_pattern=None, types=None, status_min=None,
                                status_max=None, only_errors=False, limit=3, output="json",
                                include_pending=True)
    assert answer["matched"] == 8 and answer["returned"] == 3 and answer["omitted_older"] == 5
    assert answer["truncated"] is True and answer["in_flight"] == 1
    assert answer["requests"][-1]["state"] == "headers" and answer["requests"][-1]["level"] == "error"


# ------------------------------------------------ console: honest pages, filters after a read


def test_console_reads_move_nothing_and_page_forward(local_site):
    _open_or_skip(local_site, "console-pages", "react_like.html")
    browser_tools.execute_js(
        "for (let i = 0; i < 600; i++) console.log('spam same');"
        "for (let i = 0; i < 5; i++) console.warn('warn ' + i); console.error('the one error');"
        "return 1;",
        session_id="console-pages",
    )
    first = browser_tools.get_console("console-pages")
    assert first["returned"] == 50 and first["has_more"] is True
    # Filters still see everything after an unfiltered read.
    problems = browser_tools.get_console("console-pages", levels=["warn", "error"], limit=100)
    assert [e["text"] for e in problems["entries"]] == [f"warn {i}" for i in range(5)] + ["the one error"]
    # since_seq returns what comes AFTER it, in order, page by page.
    seen, since = [], 0
    while True:
        page = browser_tools.get_console("console-pages", since_seq=since, limit=250)
        seen += [e["seq"] for e in page["entries"]]
        since = page["next_seq"]
        if not page["has_more"]:
            break
    assert seen == sorted(seen) and len(seen) == len(set(seen)) and len(seen) >= 606
    assert all(isinstance(s, int) and s > 0 for s in seen)
    folded = browser_tools.get_console("console-pages", contains="spam", dedupe=True)
    assert folded["returned"] == 1 and folded["entries"][0]["count"] == 600
    newest = browser_tools.get_console("console-pages", order="desc", limit=1)
    assert newest["entries"][0]["text"] == "the one error"



# ------------------------------------------------ fetch: charset without a header, big pages cut


_PAGES = {
    "/meta-utf8": ("text/html", '<html><head><meta charset="utf-8"><title>Т</title></head>'
                   "<body><p>Большая страница</p></body></html>".encode("utf-8")),
    "/meta-cp1251": ("text/html", '<html><head><meta http-equiv="Content-Type" '
                     'content="text/html; charset=windows-1251"></head><body><p>Привет</p></body>'
                     "</html>".encode("cp1251")),
    "/plain": ("text/plain", "простой текст".encode("utf-8")),
    "/bom": ("text/plain", b"\xef\xbb\xbf" + "с меткой".encode("utf-8")),
    "/big": ("text/html; charset=utf-8", ("<html><body><p>" + "абв " * 400_000 + "</p></body></html>").encode("utf-8")),
}


def _serve_pages():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            content_type, body = _PAGES[self.path]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_fetch_decodes_pages_served_without_a_charset():
    import asyncio

    from web_search_neo import main

    server = _serve_pages()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert "Большая страница" in asyncio.run(main.fetch_url_text(base + "/meta-utf8"))
        assert "Привет" in asyncio.run(main.fetch_url_text(base + "/meta-cp1251"))
        plain = asyncio.run(main.fetch_url_text(base + "/plain", output="json"))
        assert plain["text"] == "простой текст" and plain["charset_used"] == "utf-8"
        assert "с меткой" in asyncio.run(main.fetch_url_text(base + "/bom"))
        many = asyncio.run(main.fetch_urls_text([base + "/plain", base + "/meta-utf8"]))
        assert "простой текст" in many[0]["text"] and "Большая страница" in many[1]["text"]
        http = asyncio.run(main.http_request(base + "/plain"))
        assert http["body"] == "простой текст"
    finally:
        server.shutdown()


def test_a_page_over_the_byte_budget_is_cut_and_says_so():
    import asyncio

    from web_search_neo import main

    server = _serve_pages()
    try:
        answer = asyncio.run(main.fetch_url_text(
            f"http://127.0.0.1:{server.server_port}/big", max_chars=1000, output="json"))
        assert answer["body_cut"] is True and answer["truncated"] is True
        assert answer["bytes_read"] == 1_000_000 and answer["next_offset"] == 1000
        text = asyncio.run(main.fetch_url_text(f"http://127.0.0.1:{server.server_port}/big", max_chars=1000))
        assert "[body_cut=true" in text and "next_offset=1000" in text
    finally:
        server.shutdown()


# ------------------------------------------------ MEDIUM: dialogs, downloads, navigate


def _out(driver):
    return driver.execute_script("return document.getElementById('out').textContent")


def test_dialogs_are_logged_and_answered_by_the_policy(local_site, tmp_path, monkeypatch):
    import asyncio

    from web_search_neo import extra_actions

    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    driver = _open_or_skip(local_site, "dialogs", "dialogs_downloads.html")

    clicked = browser_tools.click("#confirm", session_id="dialogs", wait_seconds=0.2)
    assert _out(driver) == "kept"  # dismiss is the default, as before
    assert [d["message"] for d in clicked["dialogs"]] == ["Delete the item?"]
    assert clicked["dialogs"][0]["answer"] is False

    changed = asyncio.run(extra_actions.browser_dialogs("dialogs", policy="accept", prompt_text="Ann", clear=True))
    assert changed["policy"] == "accept" and changed["dialogs"][0]["type"] == "confirm"
    browser_tools.click("#confirm", session_id="dialogs", wait_seconds=0.2)
    assert _out(driver) == "deleted"
    browser_tools.click("#prompt", session_id="dialogs", wait_seconds=0.2)
    assert _out(driver) == "name=Ann"
    browser_tools.click("#alert", session_id="dialogs", wait_seconds=0.2)
    logged = asyncio.run(extra_actions.browser_dialogs("dialogs"))["dialogs"]
    assert [(d["type"], d["message"]) for d in logged] == [
        ("confirm", "Delete the item?"), ("prompt", "Your name?"), ("alert", "Saved!")]

    # A JS dialog the click raised is its effect, whatever else did or did not change.
    assert clicked["verified"] is True and clicked["effect_confidence"] == "high"

    # The policy outlives the document: a reload keeps answering "accept".
    asyncio.run(extra_actions.browser_navigate(f"{local_site.base_url}/fixtures/perception/dialogs_downloads.html",
                                      session_id="dialogs"))
    browser_tools.click("#confirm", session_id="dialogs", wait_seconds=0.2)
    assert _out(driver) == "deleted"


def test_downloads_land_in_the_session_folder_not_the_owners(local_site, tmp_path, monkeypatch):
    import asyncio
    import time

    from web_search_neo import extra_actions

    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    _open_or_skip(local_site, "downloads", "dialogs_downloads.html")
    browser_tools.click("#download", session_id="downloads", wait_seconds=0.2)
    deadline = time.monotonic() + 10
    listed = asyncio.run(extra_actions.browser_downloads("downloads", wait_seconds=5))
    while not [f for f in listed["files"] if not f["in_progress"]] and time.monotonic() < deadline:
        time.sleep(0.2)
        listed = asyncio.run(extra_actions.browser_downloads("downloads"))
    folder = listed["download_dir"]
    assert folder.startswith(str(tmp_path)) and "downloads-" in folder
    done = [f for f in listed["files"] if not f["in_progress"]]
    assert [f["file"] for f in done] == ["report.txt"]
    with open(done[0]["path"], encoding="utf-8") as handle:
        assert handle.read() == "hello report"


def test_open_without_profile_mode_keeps_the_sessions_browser(local_site, monkeypatch):
    import asyncio

    from web_search_neo import extra_actions, main

    _open_or_skip(local_site, "inherit", "dialogs_downloads.html")
    again = asyncio.run(main.browser_open_page(
        f"{local_site.base_url}/fixtures/perception/key_events.html", session_id="inherit"))
    assert again["profile_mode"] == "temporary" and again["url"].endswith("key_events.html")
    driver = browser_tools._get_session("inherit").driver
    driver.set_window_size(900, 700)  # any size; navigate must keep it
    width = driver.execute_script("return innerWidth")
    moved = asyncio.run(extra_actions.browser_navigate(
        f"{local_site.base_url}/fixtures/perception/dialogs_downloads.html", session_id="inherit"))
    assert moved["profile_mode"] == "temporary" and moved["url"].endswith("dialogs_downloads.html")
    assert driver.execute_script("return innerWidth") == width
    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(extra_actions.browser_navigate(f"{local_site.base_url}/", session_id="nobody"))


# ------------------------------------------------ MEDIUM: one JS semantics, hard timeout


def test_scripts_share_one_semantics_with_top_level_await(local_site):
    _open_or_skip(local_site, "repl", "dialogs_downloads.html")
    run = lambda script, **kw: browser_tools.execute_js(script, session_id="repl", **kw)  # noqa: E731
    assert run("await new Promise(r => setTimeout(r, 30)); return 7")["value"] == 7
    assert run("document.title")["value"] == "Dialogs and downloads"  # an expression returns itself
    assert run("return arguments[0] + 1", args=[41])["value"] == 42
    missing = run("const x = 1")
    assert missing["value"] is None and "return" in missing["value_note"]
    broken = run("return (1 +")
    assert broken["success"] is False and broken["syntax_error"] is True
    thrown = run("throw new TypeError('boom')")
    assert thrown["success"] is False and "TypeError: boom" in thrown["error"]

    # wait.script: the same body, so `return` works and a syntax error fails at once.
    assert browser_tools.wait_for_condition(
        "await null; return document.readyState === 'complete'", session_id="repl",
        timeout_seconds=3)["success"] is True
    import time
    started = time.monotonic()
    with pytest.raises(ValueError, match="does not compile"):
        browser_tools.wait_for_condition("return (", session_id="repl", timeout_seconds=8)
    assert time.monotonic() - started < 4


def test_an_endless_script_times_out_and_close_does_not_hang(local_site):
    import time

    _open_or_skip(local_site, "frozen", "dialogs_downloads.html")
    started = time.monotonic()
    answer = browser_tools.execute_js("while (true) {}", session_id="frozen", timeout_seconds=2)
    assert answer["success"] is False and answer["timed_out"] is True
    assert time.monotonic() - started < 15
    started = time.monotonic()
    closed = browser_tools.close_session("frozen")
    assert time.monotonic() - started < 15
    assert closed.get("forced")


def test_click_verification_sees_modals_and_ignores_closed_dialogs(local_site):
    _open_or_skip(local_site, "modal", "dialogs_downloads.html")
    opened = browser_tools.click("#open-modal", session_id="modal", wait_seconds=0.1)
    assert opened["verified"] is True and opened["post_state"]["dialog_opened"] is True
    browser_tools.execute_js("document.getElementById('modal').style.display = 'none'", session_id="modal")
    # Only the hidden role=dialog is left: it is not "open".
    other = browser_tools.click("#alert", session_id="modal", wait_seconds=0.1)
    assert other["post_state"]["dialog_open"] is False
    # #alert also rewrote #out: an effect elsewhere, reported as such, not "nothing happened".
    assert other["effect_detected"] is True and "no_observable_change" not in other


# ------------------------------------------------ no silent truncation: page_text


def test_page_text_reads_a_long_page_to_the_end_in_windows(local_site):
    _open_or_skip(local_site, "long-text", "dialogs_downloads.html")
    browser_tools.execute_js(
        "document.body.innerHTML = Array.from({length: 120}, (_, i) =>"
        " `<p>Paragraph ${i} ` + 'word '.repeat(20) + '</p>').join('')",
        session_id="long-text")
    whole = browser_tools.get_page_text("long-text", max_chars=200_000, mode="full")
    assert whole["truncated"] is False and whole["next_offset"] is None
    pieces, offset, reads = [], 0, 0
    while offset is not None:
        window = browser_tools.get_page_text("long-text", max_chars=1000, mode="full", offset=offset)
        assert window["offset"] == offset and len(window["text"]) <= 1000
        pieces.append(window["text"])
        offset, reads = window["next_offset"], reads + 1
        assert reads < 40
    assert reads > 5 and "\n\n".join(pieces) == whole["text"]
    capped = browser_tools.get_page_text("long-text", max_chars=900_000, mode="full")
    assert capped["max_chars_capped"] is True and capped["max_chars"] == 200_000


def test_big_script_results_come_in_windows_or_as_a_file(local_site, tmp_path, monkeypatch):
    import asyncio
    import json

    from web_search_neo import main

    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    _open_or_skip(local_site, "big-js", "dialogs_downloads.html")

    def run(script, **kw):
        return asyncio.run(main.browser_run_script(script, session_id="big-js", **kw))

    rows = "Array.from({length: 3000}, (_, i) => ({i, name: 'row ' + i}))"
    first = run(rows)
    assert first["truncated"] is True and first["total_length"] == 3000
    assert len(first["value_json"]) <= 18_000 and first["value"][0] == {"i": 0, "name": "row 0"}
    seen, offset = [], 0
    while offset is not None:
        window = run(rows, offset=offset, max_chars=60_000)
        seen.extend(item["i"] for item in window["value"])
        offset = window["next_offset"]
    assert seen == list(range(3000))

    text = run("'x'.repeat(250000) + 'END'")
    assert text["total_length"] == 250_003 and text["next_offset"] == len(text["value"])
    tail = run("'x'.repeat(250000) + 'END'", offset=250_000)
    assert tail["value"] == "END" and tail["next_offset"] is None

    whole = run("({deep: Array.from({length: 2000}, (_, i) => 'item ' + i)})")
    assert whole["value"] is None and whole["value_windowed_as_text"] is True
    saved = run("({deep: Array.from({length: 2000}, (_, i) => 'item ' + i)})", save_to="big.json")
    assert saved["value_saved"] is True and saved["saved_to"].startswith(str(tmp_path))
    with open(saved["saved_to"], encoding="utf-8") as handle:
        assert len(json.load(handle)["deep"]) == 2000
    small = run("1 + 1")
    assert small["value"] == 2 and "truncated" not in small


def test_geolocation_override_is_readable_and_headers_refuse_non_ascii(local_site):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(
            f"{local_site.base_url}/fixtures/perception/dialogs_downloads.html", session_id="geo",
            headless=True, profile_mode="temporary", geolocation={"latitude": 55.75, "longitude": 37.62})
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    where = browser_tools.execute_js(
        "await new Promise((ok, no) => navigator.geolocation.getCurrentPosition("
        "p => ok([p.coords.latitude, p.coords.longitude]), e => no(new Error(e.message))))",
        session_id="geo", timeout_seconds=10)
    assert where["success"] is True, where
    assert where["value"] == [55.75, 37.62]
    with pytest.raises(ValueError, match="non-ASCII"):
        browser_tools.set_extra_headers({"X-Audit": "да-1"}, session_id="geo")
    assert browser_tools.set_extra_headers({"X-Audit": "%D0%B4%D0%B0-1"}, session_id="geo")["success"]


def test_http_request_lists_every_set_cookie_and_keeps_no_jar():
    import http.server
    import threading

    from web_search_neo.fetch.api import http_request

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Cookie"))
            self.send_response(200)
            self.send_header("Set-Cookie", "a=1; Expires=Wed, 21 Oct 2035 07:28:00 GMT; Path=/")
            self.send_header("Set-Cookie", "b=2; HttpOnly")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        first = http_request(url + "one")
        assert first["set_cookies"] == ["a=1; Expires=Wed, 21 Oct 2035 07:28:00 GMT; Path=/", "b=2; HttpOnly"]
        http_request(url + "two")
        assert seen == [None, None]  # the cookies of call one never rode along on call two
    finally:
        server.shutdown()


# ------------------------------------------------ orchestrator pains: frames, scale, aim


def _open_game_or_skip(local_site, session_id):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(f"{local_site.base_url}/fixtures/games/pointer.html",
                                session_id=session_id, width=1024, height=768,
                                headless=True, profile_mode="temporary")
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    return browser_tools._get_session(session_id)


def test_screenshots_carry_geometry_and_freshness(local_site):
    import asyncio
    import json

    from web_search_neo import main

    session = _open_game_or_skip(local_site, "shots")
    image, text = asyncio.run(main.browser_screenshot(session_id="shots", wait_frames=2))
    meta = json.loads(text)
    assert image.data.startswith(b"\x89PNG") and meta["frames_waited"] == 2
    assert meta["image_width"] == round(meta["viewport_css_width"] * meta["device_pixel_ratio"])
    assert meta["scale"] == meta["device_pixel_ratio"] and meta["frame_id"] == 1
    assert meta["changed_since_last"] is None
    _, again = asyncio.run(main.browser_screenshot(session_id="shots"))
    assert json.loads(again)["frame_id"] == 2 and isinstance(json.loads(again)["changed_since_last"], bool)

    # Aim in image pixels of a region capture: the pad's centre, clicked through the image.
    pad = session.driver.execute_script(
        "const r = document.getElementById('pad').getBoundingClientRect();"
        "return {x: r.x + scrollX, y: r.y + scrollY, w: r.width, h: r.height};")
    asyncio.run(main.browser_screenshot(session_id="shots", mode="region", x=pad["x"], y=pad["y"],
                                        width=int(pad["w"]), height=int(pad["h"])))
    scale = session.capture_geometry["scale"]
    asyncio.run(main.browser_pointer("click", pad["w"] / 2 * scale, pad["h"] / 2 * scale,
                                     session_id="shots", coordinate_space="image"))
    clicks = session.driver.execute_script("return window.__input.leftClicks")
    assert clicks == 1


def test_frame_health_wait_frames_and_unthrottle_in_a_visible_page(local_site):
    import asyncio

    from web_search_neo import extra_actions

    _open_game_or_skip(local_site, "frames")
    probe = browser_tools.game_probe("frames", sample_seconds=0.3)
    assert probe["frame_health"]["throttled"] is False, probe["frame_health"]
    waited = asyncio.run(extra_actions.browser_wait_frames("frames", frames=3))
    assert waited["success"] is True and waited["frames_waited"] == 3
    fixed = asyncio.run(extra_actions.browser_unthrottle("frames"))
    assert fixed["applied"] == ["Emulation.setFocusEmulationEnabled", "Page.setWebLifecycleState"]
    assert fixed["after"]["throttled"] is False


def test_frame_health_names_a_hidden_page():
    from web_search_neo import frame_health

    hidden = frame_health.assess(0.0, {"visibility": "hidden", "focused": False})
    assert hidden["throttled"] is True and hidden["throttle_reason"] == "hidden" and hidden["hint"]
    slow = frame_health.assess(1.0, {"visibility": "visible", "focused": False})
    assert slow["throttle_reason"] == "occluded_or_background_window"
    gated = frame_health.assess(0.0, {"visibility": "visible"}, "step")
    assert gated["throttled"] is None


def test_look_turns_a_locked_camera_by_exactly_the_asked_amount(local_site):
    import asyncio

    from web_search_neo import extra_actions

    session = _open_game_or_skip(local_site, "look")
    acquired = browser_tools.pointer_lock("acquire", "look", selector="#pad")
    if not acquired["locked"]:
        pytest.skip(f"Pointer lock is unavailable in this browser: {acquired}")
    before = session.driver.execute_script("return [window.__input.lockDelta.x, window.__input.lockDelta.y]")
    turned = asyncio.run(extra_actions.browser_look(403, -51, session_id="look", steps=7, duration_ms=70))
    assert turned["moved_x"] == 403 and turned["moved_y"] == -51 and turned["pointer_locked"] is True
    after = session.driver.execute_script("return [window.__input.lockDelta.x, window.__input.lockDelta.y]")
    assert [after[0] - before[0], after[1] - before[1]] == [403, -51]
    moves = session.driver.execute_script("return window.__input.lockMoves")
    assert moves >= 7
