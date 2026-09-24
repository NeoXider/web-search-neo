"""1.19.0 audit findings: script timeouts vs close, dialogs handed back, honest cuts."""

from __future__ import annotations

import pytest

from web_search_neo import browser_tools, page_guards
from web_search_neo.actions import scripts as script_guard
from web_search_neo.chrome_bridge import ChromeBridgeError

RUN = "run-audit"


class _CompanionTab:
    """The user's tab as the companion driver exposes it; records what reached it."""

    is_extension_bridge = True

    def __init__(self, tab_id=77, async_error=None):
        self.tab_id = tab_id
        self.async_error = async_error
        self.scripts: list[str] = []
        self.cdp: list[tuple[str, dict]] = []
        self.calls: list[str] = []
        self.title = "Page"
        self.url = "https://example.test/"

    class _Switch:
        def default_content(self):
            return None

    switch_to = _Switch()

    def execute_async_script(self, script, *args):
        if self.async_error:
            raise self.async_error
        return {"__wsn_ok": True, "value": 1, "undefined": False}

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if script.strip() == "return document.readyState":
            return "complete"
        return {"url": self.url, "title": self.title}

    def execute_cdp_cmd(self, method, params=None, **_kwargs):
        self.cdp.append((method, params or {}))
        if method == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": f"id-{len(self.cdp)}"}
        return {}

    def quit(self):
        self.calls.append("quit")
        return {"detached": True, "id": self.tab_id}

    def close_tab(self):
        self.calls.append("close_tab")
        return {"removed": True}


@pytest.fixture
def released(monkeypatch):
    freed: list[int] = []
    monkeypatch.setattr(browser_tools, "_release_claimed_tab", lambda tab_id: freed.append(tab_id))
    monkeypatch.setattr(browser_tools, "_current_browser_run", lambda: RUN)
    return freed


def _register(session_id, driver, **overrides):
    fields = {"driver": driver, "headless": False, "profile_mode": "current",
              "current_tab_id": driver.tab_id, "owns_browser": True, "owns_tab": True,
              "browser_run": RUN, "tab_group": browser_tools.DEFAULT_TAB_GROUP}
    fields.update(overrides)
    session = browser_tools.BrowserSession(**fields)
    browser_tools._sessions[session_id] = session
    return session


# ------------------------------------------------------------------ H1


def test_a_companion_timeout_never_marks_the_users_tab_and_close_releases_it(released):
    tab = _CompanionTab(async_error=ChromeBridgeError("cdp.send timed out after 15 s"))
    session = _register("companion-timeout", tab)
    answer = browser_tools.execute_js("await new Promise(() => {})", session_id="companion-timeout")
    assert answer["success"] is False and answer["timed_out"] is True
    assert "still responsive" in answer["error"]
    assert getattr(tab, "_wsn_hung", False) is False
    tab._wsn_hung = True  # even a stale mark must not skip the ordinary teardown
    assert script_guard.stop_hung_driver(session) is None
    closed = browser_tools.close_session("companion-timeout")
    assert "forced" not in closed
    assert "quit" in tab.calls and "close_tab" in tab.calls  # debugger detached, tab closed
    assert released == [77]  # the claim is given back


class _SeleniumLike:
    """A Selenium-shaped driver: script timeout, HTTP transport config, a service process."""

    def __init__(self, error=None):
        self.error = error
        self.timeouts: list[float] = []

        class _Config:
            timeout = 120.0

        class _Executor:
            client_config = _Config()

        self.command_executor = _Executor()
        self.service = type("S", (), {"process": None})()

    def set_script_timeout(self, seconds):
        self.timeouts.append(seconds)

    def execute_async_script(self, script, *args):
        if self.error:
            raise self.error
        return {"__wsn_ok": True, "value": 2, "undefined": False}


def test_a_slow_promise_is_a_timeout_not_a_frozen_browser():
    from selenium.common.exceptions import TimeoutException

    driver = _SeleniumLike(TimeoutException("script timeout"))
    with pytest.raises(script_guard.repl.ScriptError) as failure:
        script_guard._run_repl(driver, "await new Promise(() => {})", None, 2)
    assert failure.value.timed_out is True
    assert getattr(driver, "_wsn_hung", False) is False
    assert driver.timeouts == [2.0, script_guard.DEFAULT_SCRIPT_SECONDS]  # the limit comes back
    assert driver.command_executor.client_config.timeout == 120.0


def test_only_a_transport_timeout_marks_the_driver_and_success_clears_it():
    from urllib3.exceptions import ReadTimeoutError

    driver = _SeleniumLike(ReadTimeoutError(None, "http://127.0.0.1:1", "Read timed out."))
    with pytest.raises(script_guard.repl.ScriptError) as failure:
        script_guard._run_repl(driver, "while (true) {}", None, 1)
    assert failure.value.timed_out is True and driver._wsn_hung is True
    driver.error = None
    assert script_guard._run_repl(driver, "1 + 1", None, 1) == (2, False)
    assert driver._wsn_hung is False


def test_stop_hung_driver_needs_an_owned_browser_with_a_process():
    driver = _SeleniumLike()
    driver._wsn_hung = True
    owned = browser_tools.BrowserSession(driver=driver, headless=True, profile_mode="temporary")
    assert script_guard.stop_hung_driver(owned) is None  # no process: the ordinary teardown runs
    attached = browser_tools.BrowserSession(driver=driver, headless=True, profile_mode="attach",
                                            owns_browser=False)
    assert script_guard.stop_hung_driver(attached) is None


def test_a_scoped_profile_is_removed_and_a_persistent_one_never(tmp_path, monkeypatch):
    import tempfile

    scoped = tmp_path / "scoped_dir123_456"
    (scoped / "Default").mkdir(parents=True)
    assert script_guard._remove_scoped_profile(str(scoped), "temporary") is None and not scoped.exists()
    # A persistent profile_id is chosen by the agent and may be spelled scoped_dir*:
    # its cookies must survive a forced stop.
    named = tmp_path / "scoped_dir_work"
    (named / "Default").mkdir(parents=True)
    (named / "Default" / "Cookies").write_bytes(b"login")
    assert script_guard._remove_scoped_profile(str(named), "persistent") is None
    assert (named / "Default" / "Cookies").exists()
    # A temporary-looking profile outside the system temp directory is not ours either.
    outside = tmp_path / "elsewhere" / "scoped_dir999"
    outside.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "real-temp"))
    assert script_guard._remove_scoped_profile(str(outside), "temporary") is None and outside.exists()


def test_a_forced_stop_on_a_persistent_profile_keeps_the_profile(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(script_guard, "_kill_tree", lambda pid, process: killed.append(pid))
    profile = tmp_path / "scoped_dir_work"
    profile.mkdir()
    driver = _SeleniumLike()
    driver._wsn_hung = True
    driver.service.process = type("P", (), {"pid": 4242})()
    driver.capabilities = {"chrome": {"userDataDir": str(profile)}}
    session = browser_tools.BrowserSession(driver=driver, headless=True, profile_mode="persistent",
                                           profile_id="scoped_dir_work")
    outcome = script_guard.stop_hung_driver(session)
    assert outcome["forced"] and killed == [4242] and profile.exists()


# ------------------------------------------------------------------ H2


def test_dialogs_are_handed_back_with_an_attached_tab(released):
    tab = _CompanionTab(tab_id=91)
    session = _register("borrowed", tab, owns_tab=False)
    page_guards.dialogs_action(session, "accept", "x", False)
    identifier = session.dialog_script_id
    assert identifier and identifier in session.injected_scripts
    browser_tools.close_session("borrowed")
    assert "close_tab" not in tab.calls  # the user's tab stays open ...
    assert page_guards.RESTORE_DIALOGS_SCRIPT in tab.scripts  # ... with its own dialogs back
    assert ("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier}) in tab.cdp
    assert session.dialog_script_id is None and identifier not in session.injected_scripts


def test_leaving_a_borrowed_tab_restores_its_dialogs(released, monkeypatch):
    borrowed = _CompanionTab(tab_id=5)
    fresh = _CompanionTab(tab_id=6)
    fresh.actual_tab_group = browser_tools.DEFAULT_TAB_GROUP
    session = _register("leaving", borrowed, owns_tab=False)
    page_guards.dialogs_action(session, "dismiss", None, False)
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: fresh)
    monkeypatch.setattr(browser_tools, "_claim_tab", lambda tab_id: {})
    assert browser_tools._leave_claimed_tab(session, 800, 600, browser_tools.DEFAULT_TAB_GROUP) == 5
    assert page_guards.RESTORE_DIALOGS_SCRIPT in borrowed.scripts
    assert session.dialog_script_id is None and session.dialog_policy_set is True
    browser_tools._sessions.pop("leaving", None)


def test_restoring_dialogs_in_chrome_brings_back_the_native_functions(local_site):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(f"{local_site.base_url}/fixtures/perception/dialogs_downloads.html",
                                session_id="restore", headless=True, profile_mode="temporary")
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    session = browser_tools._get_session("restore")
    native = "return String(window.confirm).includes('[native code]') && !window.__wsnDialogs"
    assert session.driver.execute_script(native) is False  # the answerer is installed
    browser_tools._clear_injected_state(session, "restore")
    assert session.driver.execute_script(native) is True
    session.driver.refresh()  # and the registration is gone: the next document is untouched
    assert session.driver.execute_script(native) is True


# ------------------------------------------------------------------ M5, M6


def test_click_evidence_never_raises_after_the_click():
    class _Broken:
        dialog_script_id = "x"
        dialog_policy_set = True
        download_dir = "/nonexistent/for/sure"

        class driver:  # noqa: N801
            @staticmethod
            def execute_script(*_a):
                raise RuntimeError("page gone")

    assert page_guards.click_evidence(_Broken(), 0.0) == {}


def test_download_routing_refused_is_said(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))

    class _Refusing:
        def execute_cdp_cmd(self, method, params):
            if method.endswith("setDownloadBehavior"):
                raise RuntimeError("Browser context management is not supported")
            return {"identifier": "d"}

        def execute_script(self, *_a):
            return None

    session = browser_tools.BrowserSession(driver=_Refusing(), headless=True, profile_mode="temporary")
    page_guards.setup_owned(session, "refused")
    listed = page_guards.downloads_action(session)
    assert session.download_dir is None and listed["download_routing"] == "failed"
    assert "default download folder" in listed["note"]
    assert not list((tmp_path / "sessions").iterdir())  # the unused folder is not left behind


def test_old_session_download_folders_are_swept(tmp_path):
    import os
    import time

    old = tmp_path / "old-20200101"
    old.mkdir()
    os.utime(old, (time.time() - 30 * 24 * 3600,) * 2)
    new = tmp_path / "new"
    new.mkdir()
    assert page_guards.sweep_old_folders(tmp_path) == 1
    assert not old.exists() and new.exists()


# ------------------------------------------------------------------ M1: bounded script values


def test_internal_script_reads_keep_a_flagged_ceiling_and_save_never_overwrites(tmp_path, monkeypatch):
    import json

    from web_search_neo import script_results

    long_text = "x" * 250_000
    answer = {"success": True, "value": long_text, "value_json": json.dumps(long_text)}
    shaped = script_results.shape(answer)  # no max_chars: the internal callers' path
    assert shaped["truncated"] is True and shaped["total_length"] == 250_000
    assert shaped["next_offset"] == script_results.DEFAULT_CEILING_CHARS
    assert len(shaped["value"]) == script_results.DEFAULT_CEILING_CHARS

    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    saved = script_results.shape({"success": True, "value": [1, 2]}, save_to="v.json")
    assert saved["value_saved"] is True and "value" not in saved and "value_json" not in saved
    with pytest.raises(ValueError, match="choose another save_to name"):
        script_results.shape({"success": True, "value": [3]}, save_to="v.json")
    assert (tmp_path / "v.json").read_text(encoding="utf-8") == "[1, 2]"


# ------------------------------------------------------------------ M2, M3: fetch cuts


class _Response:
    def __init__(self, content: bytes, *, cut=False, headers=None, url="https://site.test/"):
        self.content = content
        self.headers = headers or {"content-type": "text/html"}
        self.url = url
        self.status_code = 200
        if cut:
            self.wsn_truncated = True

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


def test_fetch_links_says_when_the_page_or_the_list_was_cut():
    from web_search_neo.fetch import content

    page = b"".join(b'<a href="/p%d">p</a>' % index for index in range(10))
    links = content._fetch_page_links("https://site.test/", limit=3,
                                      request_client=lambda *a, **k: _Response(page, cut=True))
    assert links[:3] == ["https://site.test/p0", "https://site.test/p1", "https://site.test/p2"]
    assert links[3].startswith("# truncated=true: returned 3 of 10 links")
    assert links[4].startswith("# body_cut=true")
    assert not any(line.startswith("http") for line in links[3:])


def test_fetch_text_budget_reaches_the_window_it_was_asked_for():
    from web_search_neo.fetch import content

    budgets = []

    def client(url, **kwargs):
        budgets.append(kwargs["max_response_bytes"])
        return _Response(b"<p>hi</p>")

    content._fetch_url_text("https://site.test/", max_chars=20_000, offset=2_000_000,
                            request_client=client)
    assert budgets[0] >= (2_000_000 + 20_000) * 8 or budgets[0] == 20_000_000


def test_bing_rows_say_the_page_was_cut(monkeypatch):
    from web_search_neo import msp_search

    page = (b'<li class="b_algo"><h2><a href="https://docs.test/a">Docs a</a></h2>'
            b'<p>about a</p></li>')
    monkeypatch.setattr(msp_search, "request", lambda *a, **k: _Response(page, cut=True))
    rows = msp_search.BingHtmlSearchProvider().search("docs", 5, 5)
    assert rows[0]["page_cut_at_bytes"] == str(len(page))


# ------------------------------------------------------------------ M4: decoding order


def test_a_bom_wins_over_the_header_and_is_not_left_in_the_text():
    from web_search_neo.fetch.decoding import decode_response
    import codecs

    utf8 = _Response(codecs.BOM_UTF8 + "Привет".encode("utf-8"),
                     headers={"content-type": "text/html; charset=windows-1251"})
    assert decode_response(utf8) == ("Привет", "utf-8")
    utf16 = _Response(codecs.BOM_UTF16_LE + "Привет".encode("utf-16-le"),
                      headers={"content-type": "text/html; charset=utf-8"})
    assert decode_response(utf16) == ("Привет", "utf-16-le")


def test_a_meta_utf16_declaration_means_utf8():
    from web_search_neo.fetch.decoding import decode_response

    for label in (b"utf-16", b"utf-16le", b"UTF-16BE"):
        body = b'<meta charset="' + label + b'"><p>' + "Ёж".encode("utf-8")
        text, used = decode_response(_Response(body, headers={"content-type": "text/html"}))
        assert used == "utf-8" and "Ёж" in text


# ------------------------------------------------------------------ LOW


def test_text_windows_never_split_a_link_marker_and_always_move_on():
    from web_search_neo.perception import text_window

    kept, consumed, truncated = text_window.clip("abc [12] def " * 20, 6)
    assert kept == "abc" and consumed == 4 and truncated
    window = text_window.window("y" * 1000, [], 10, False)
    assert len(window["text"]) == text_window.MIN_WINDOW and window["next_offset"] == 200
    listed = text_window.window("see [1] " * 200, [{"index": 1, "text": "x" * 500, "url": "u"}], 10, True)
    assert listed["next_offset"] and listed["text"]  # an index too big to fit never stalls paging


def test_dom_codes_of_punctuation_and_the_plus_chord():
    from web_search_neo import key_table

    for code, key in (("Minus", "-"), ("Semicolon", ";"), ("Quote", "'"), ("BracketLeft", "["),
                      ("Backquote", "`")):
        assert browser_tools._normalize_game_key(code) == key
    assert browser_tools._normalize_game_key("NumpadAdd") == browser_tools._normalize_game_key("ADD")
    assert key_table.expand_chords(["Shift++"]) == ["Shift", "+"]
    with pytest.raises(ValueError, match="CapsLock"):
        browser_tools._normalize_game_key("CapsLock")


def test_quoted_semicolons_and_returns_do_not_make_a_statement():
    from web_search_neo.actions.repl import is_expression

    assert is_expression("document.title === 'a;b'")
    assert is_expression('"return"')
    assert not is_expression("debugger")
    assert not is_expression("with (document) title")
    assert not is_expression("a(); b()")


# ------------------------------------------------------------------ wrappers keep window flags


def test_local_storage_read_keeps_the_window_flags(monkeypatch):
    import json

    from web_search_neo import script_results

    big = "v" * 300_000
    monkeypatch.setattr(browser_tools, "execute_js", lambda *a, **k: script_results.shape(
        {"success": True, "value": big, "value_json": json.dumps(big)}))
    read = browser_tools.local_storage(op="read", key="blob")
    assert read["truncated"] is True and read["total_length"] == 300_000
    assert read["next_offset"] == 200_000 and len(read["value"]) == 200_000 and read["window_note"]

    whole = {"k%d" % i: "x" * 100 for i in range(3000)}
    monkeypatch.setattr(browser_tools, "execute_js", lambda *a, **k: script_results.shape(
        {"success": True, "value": whole, "value_json": json.dumps(whole)}))
    read = browser_tools.local_storage(op="read")
    assert read["value"] is None and read["value_windowed_as_text"] is True
    assert read["value_json_part"] and read["truncated"] is True and read["next_offset"]


def test_replay_request_never_turns_a_big_answer_into_none(monkeypatch):
    import json

    from web_search_neo import script_results

    huge = {"status": 200, "headers": {"h%d" % i: "y" * 100 for i in range(3000)}, "body": ""}
    monkeypatch.setattr(browser_tools, "execute_js", lambda *a, **k: script_results.shape(
        {"success": True, "value": huge, "value_json": json.dumps(huge)}))
    browser_tools._sessions["default"] = browser_tools.BrowserSession(
        driver=_SeleniumLike(), headless=True, profile_mode="temporary")
    try:
        answer = browser_tools.replay_request(url="https://host/api")
    finally:
        browser_tools._sessions.pop("default", None)
    assert answer["response"] is None and answer["response_windowed_as_text"] is True
    assert answer["response_json_part"].startswith('{"status": 200') and answer["truncated"] is True


# ------------------------------------------------------------------ LOW (round 3)


def test_close_after_a_companion_timeout_has_one_teardown_deadline(released, monkeypatch):
    tab = _CompanionTab(async_error=ChromeBridgeError("cdp.send timed out after 15 s"))
    limits = []
    tab.set_script_timeout = limits.append
    _register("slow-close", tab)
    browser_tools.execute_js("await new Promise(() => {})", session_id="slow-close")
    assert tab._wsn_slow is True
    monkeypatch.setattr(script_guard, "TEARDOWN_SECONDS", -1.0)  # the deadline has already passed
    closed = browser_tools.close_session("slow-close")
    assert limits[-1] == script_guard.TEARDOWN_COMMAND_SECONDS
    assert "quit" in tab.calls and "close_tab" in tab.calls and released == [77]  # never skipped
    assert "forced" not in closed


def test_posix_forced_stop_kills_the_whole_tree(monkeypatch):
    import subprocess
    import types

    children = {"100": "200\n201\n", "200": "300\n", "201": "", "300": ""}
    monkeypatch.setattr(script_guard.os, "name", "posix")
    monkeypatch.setattr(script_guard.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(
        args, 0, stdout=children.get(args[-1], ""), stderr=""))
    killed = []
    monkeypatch.setattr(script_guard.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(script_guard, "signal", types.SimpleNamespace(SIGKILL=9))
    script_guard._kill_tree(100, types.SimpleNamespace(kill=lambda: killed.append("driver")))
    assert sorted(killed[:-1]) == [100, 200, 201, 300] and killed[-1] == "driver"


def test_latin1_and_ascii_labels_mean_windows_1252():
    from web_search_neo.fetch.decoding import decode_response

    body = "“quoted” – dash".encode("cp1252")
    for label in ("iso-8859-1", "latin1", "us-ascii"):
        text, used = decode_response(_Response(body, headers={"content-type": f"text/html; charset={label}"}))
        assert used == "cp1252" and text == "“quoted” – dash"
