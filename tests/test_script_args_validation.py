"""A script that reads arguments[n] with nothing behind it runs anyway.

Both runtimes apply only `args`, so the index is `undefined` - and an
undefined written to a clipboard or a form arrives as the text 'undefined',
which reads like the tool misbehaved (a Sheets paste once failed validation
exactly this way). execute_js now refuses such a call before touching the
browser: quoted text that merely mentions arguments[0] does not trip it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_search_neo import browser_tools


class _CannedDriver:
    is_extension_bridge = False

    def __init__(self):
        self.scripts = []

    def execute_script(self, script, *args):
        self.scripts.append((script, list(args)))
        return None

    def quit(self) -> None:
        pass


def _register(driver, session_id: str) -> None:
    browser_tools._sessions[session_id] = browser_tools.BrowserSession(
        driver=driver, headless=False
    )


def _summary_free(monkeypatch) -> None:
    monkeypatch.setattr(
        browser_tools, "_page_summary", lambda driver, session_id: {"session_id": session_id}
    )


def test_missing_args_refuses_before_touching_the_page():
    driver = _CannedDriver()
    _register(driver, "args-missing")
    with pytest.raises(ValueError, match=r"arguments\[0\].*no `args` were passed"):
        browser_tools.execute_js(
            "await navigator.clipboard.writeText(arguments[0]); return true;",
            session_id="args-missing",
        )
    assert driver.scripts == []


def test_short_args_names_the_missing_index():
    driver = _CannedDriver()
    _register(driver, "args-short")
    with pytest.raises(ValueError, match=r"arguments\[2\].*only 2 `args`"):
        browser_tools.execute_js(
            "return arguments[0] + arguments[2];",
            args=["a", "b"],
            session_id="args-short",
        )
    assert driver.scripts == []


def test_covered_indices_run(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver()
    _register(driver, "args-covered")
    result = browser_tools.execute_js(
        "return arguments[0];", args=["hello"], session_id="args-covered"
    )
    assert result["success"] is True
    assert driver.scripts == [("return arguments[0];", ["hello"])]


def test_arguments_mention_inside_a_string_is_not_code(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver()
    _register(driver, "args-quoted")
    result = browser_tools.execute_js(
        "return 'read arguments[0] from args';", session_id="args-quoted"
    )
    assert result["success"] is True


def test_arguments_length_needs_no_args(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver()
    _register(driver, "args-length")
    result = browser_tools.execute_js(
        "return arguments.length;", session_id="args-length"
    )
    assert result["success"] is True
