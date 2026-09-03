"""Tab-strip labels: prefix building, CDP registration, and title stripping.

A session in profile_mode="current" labels its tab `[agent_label] ` (or
`[session_id] `) via Page.addScriptToEvaluateOnNewDocument, so the label
survives navigations and reloads. Read topics must keep reporting the page's
own title, and handing a borrowed tab back must take the prefix with it.
"""

from __future__ import annotations

import inspect

import pytest

from web_search_neo import browser_tools
from web_search_neo import main


class _LabelDriver:
    is_extension_bridge = True

    def __init__(self, probe):
        self.probe = dict(probe)
        self.cdp_calls: list[tuple[str, dict]] = []
        self.scripts: list[str] = []

    def execute_cdp_cmd(self, command, params):
        self.cdp_calls.append((command, params))
        if command == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": "lbl-1"}
        return {}

    def execute_script(self, script, *args):
        self.scripts.append(script)
        return dict(self.probe)

    def quit(self):
        return None


_PROBE = {
    "url": "https://example.test/page",
    "title": "Fixture page",
    "viewport_width": 1440,
    "viewport_height": 900,
    "page_width": 1440,
    "page_height": 2000,
    "ready_state": "complete",
    "challenge": {},
}


def _register(driver, session_id="label-case", **kwargs) -> browser_tools.BrowserSession:
    session = browser_tools.BrowserSession(
        driver=driver, headless=False, profile_mode="current", **kwargs
    )
    browser_tools._sessions[session_id] = session
    return session


def _label_env(monkeypatch, value=None):
    if value is None:
        monkeypatch.delenv(browser_tools._TAB_LABEL_ENV, raising=False)
    else:
        monkeypatch.setenv(browser_tools._TAB_LABEL_ENV, value)


# --- prefix building --------------------------------------------------------


def test_prefix_uses_the_agent_label_first():
    assert browser_tools._tab_label_prefix("work", "ag-mail") == "[ag-mail] "


def test_prefix_falls_back_to_the_session_id():
    assert browser_tools._tab_label_prefix("work", None) == "[work] "


def test_prefix_cannot_break_the_bracket_shape_and_stays_short():
    assert browser_tools._tab_label_prefix("s", "a[b]c") == "[abc] "
    assert browser_tools._tab_label_prefix("s", "two words") == "[two-words] "
    long_label = "x" * 100
    prefix = browser_tools._tab_label_prefix("s", long_label)
    assert prefix == f"[{'x' * browser_tools._TAB_LABEL_LIMIT}] "


# --- applying ---------------------------------------------------------------


def test_apply_registers_the_label_script_and_runs_it_live(monkeypatch):
    _label_env(monkeypatch)
    driver = _LabelDriver(_PROBE)
    session = _register(driver, agent_label="ag-mail")
    with session.lock:
        browser_tools._apply_tab_label(session, "label-case")
    assert session.tab_label_prefix == "[ag-mail] "
    assert session.tab_label_script_id == "lbl-1"
    command, params = driver.cdp_calls[0]
    assert command == "Page.addScriptToEvaluateOnNewDocument"
    assert "[ag-mail] " in params["source"]
    assert "MutationObserver" in params["source"]
    assert "DOMContentLoaded" in params["source"]
    # No navigation follows an attach, so the live document gets it now.
    assert any("[ag-mail] " in script for script in driver.scripts)


def test_apply_is_idempotent_for_the_same_prefix(monkeypatch):
    _label_env(monkeypatch)
    driver = _LabelDriver(_PROBE)
    session = _register(driver, agent_label="ag-mail")
    with session.lock:
        browser_tools._apply_tab_label(session, "label-case")
        browser_tools._apply_tab_label(session, "label-case")
    adds = [
        call
        for call in driver.cdp_calls
        if call[0] == "Page.addScriptToEvaluateOnNewDocument"
    ]
    assert len(adds) == 1


def test_apply_relabels_when_the_owner_changes(monkeypatch):
    _label_env(monkeypatch)
    driver = _LabelDriver(_PROBE)
    session = _register(driver, agent_label="ag-one")
    with session.lock:
        browser_tools._apply_tab_label(session, "label-case")
        session.agent_label = "ag-two"
        browser_tools._apply_tab_label(session, "label-case")
    assert session.tab_label_prefix == "[ag-two] "
    removals = [
        call
        for call in driver.cdp_calls
        if call[0] == "Page.removeScriptToEvaluateOnNewDocument"
    ]
    assert len(removals) == 1


@pytest.mark.parametrize("mode", ["temporary", "persistent", "attach"])
def test_apply_leaves_non_current_modes_alone(monkeypatch, mode):
    _label_env(monkeypatch)
    driver = _LabelDriver(_PROBE)
    session = _register(driver, "label-other", agent_label="ag-mail")
    session.profile_mode = mode
    with session.lock:
        browser_tools._apply_tab_label(session, "label-other")
    assert driver.cdp_calls == []
    assert driver.scripts == []
    assert session.tab_label_prefix is None


def test_apply_honours_label_tab_false(monkeypatch):
    _label_env(monkeypatch)
    driver = _LabelDriver(_PROBE)
    session = _register(driver, agent_label="ag-mail")
    with session.lock:
        browser_tools._apply_tab_label(session, "label-case", label_tab=False)
    assert driver.cdp_calls == []
    assert session.tab_label_prefix is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_apply_honours_the_environment_kill_switch(monkeypatch, value):
    _label_env(monkeypatch, value)
    driver = _LabelDriver(_PROBE)
    session = _register(driver, agent_label="ag-mail")
    with session.lock:
        browser_tools._apply_tab_label(session, "label-case")
    assert driver.cdp_calls == []
    assert session.tab_label_prefix is None


# --- stripping on read ------------------------------------------------------


def test_strip_removes_only_the_session_prefix():
    assert (
        browser_tools._strip_tab_label("[ag-mail] Fixture", "[ag-mail] ")
        == "Fixture"
    )
    assert (
        browser_tools._strip_tab_label("[ag-other] Fixture", "[ag-mail] ")
        == "[ag-other] Fixture"
    )
    assert browser_tools._strip_tab_label("Fixture", "[ag-mail] ") == "Fixture"
    assert browser_tools._strip_tab_label("Fixture", None) == "Fixture"


def test_page_summary_reports_the_unlabelled_title(monkeypatch):
    _label_env(monkeypatch)
    probe = dict(_PROBE, title="[ag-mail] Fixture page")
    driver = _LabelDriver(probe)
    session = _register(driver, agent_label="ag-mail")
    session.tab_label_prefix = "[ag-mail] "
    summary = browser_tools._page_summary(driver, "label-case")
    assert summary["title"] == "Fixture page"
    assert session.last_title == "Fixture page"


# --- removal on handover ----------------------------------------------------
#
# _leave_claimed_tab (the detach half of attach/open) calls _remove_tab_label
# on the borrowed driver before the debugger lets go; what it must do is
# covered here directly.


def test_remove_restores_the_live_title_and_forgets_the_script(monkeypatch):
    _label_env(monkeypatch)
    driver = _LabelDriver(_PROBE)
    session = _register(driver, agent_label="ag-mail")
    with session.lock:
        browser_tools._apply_tab_label(session, "label-case")
        browser_tools._remove_tab_label(session)
    assert session.tab_label_prefix is None
    assert session.tab_label_script_id is None
    assert any("__wsnTabLabel" in script for script in driver.scripts)
    assert (
        "Page.removeScriptToEvaluateOnNewDocument",
        {"identifier": "lbl-1"},
    ) in driver.cdp_calls


def test_remove_without_a_label_is_a_no_op():
    driver = _LabelDriver(_PROBE)
    session = _register(driver, "label-bare")
    with session.lock:
        browser_tools._remove_tab_label(session)
    assert driver.cdp_calls == []
    assert driver.scripts == []


# --- wiring -----------------------------------------------------------------


def test_open_attach_and_open_many_publish_label_tab():
    for tool in (
        main.browser_open_page,
        main.browser_attach_tab,
        main.browser_open_pages,
    ):
        assert "label_tab" in inspect.signature(tool).parameters
    assert inspect.signature(browser_tools.open_page).parameters["label_tab"].default is True
    assert (
        inspect.signature(browser_tools.attach_current_tab).parameters["label_tab"].default
        is True
    )
