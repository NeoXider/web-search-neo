"""A companion reload the owner forced goes through although agents hold tabs.

Before 1.30.1 ``python main.py --update --force`` restarted the bridge daemon and then
had its reload refused all the same: an idle agent's server had reconnected and claimed
its tab again before the companion was back (2026-11-11, a tab held for almost four
hours, and no way to update the companion short of pressing Reload in Chrome).
"""

from __future__ import annotations

import logging
import threading

from web_search_neo import bridge_claims, chrome_bootstrap
from web_search_neo.bridge_claims import held_tabs, reload_refusal
from web_search_neo.bridge_daemon import BridgeDaemon
from web_search_neo.log_setup import LazyRotatingFileHandler


class _Client:
    label = "main.py#412"


class _Claim:
    client = _Client()

    def held_seconds(self) -> float:
        return 13560.0


def test_a_reload_waits_while_a_tab_is_held_and_says_who_holds_it():
    refusal = reload_refusal({7: _Claim()}, {})
    assert "tab 7: main.py#412, held 226 min" in refusal
    assert "--update --force" in refusal
    assert reload_refusal({}, {}) is None


def test_a_forced_reload_goes_through_and_the_log_names_who_was_detached():
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    bridge_claims.LOGGER.addHandler(handler)
    try:
        assert reload_refusal({7: _Claim()}, {"force": True}) is None
    finally:
        bridge_claims.LOGGER.removeHandler(handler)
    assert "tab 7: main.py#412" in records[0].getMessage()


def test_only_a_true_force_counts():
    for params in ({"force": "yes"}, {"force": 1}, {"force": False}, None, []):
        assert reload_refusal({7: _Claim()}, params) is not None


def test_a_claim_without_details_is_still_named():
    assert held_tabs({5: object()}) == "tab 5: an agent"


def test_the_daemon_sends_a_forced_reload_past_its_claims():
    daemon = BridgeDaemon.__new__(BridgeDaemon)
    daemon._lock = threading.Lock()
    daemon._claims = {7: _Claim()}
    assert daemon._claim_check(object(), "runtime.reload", {"force": True}) == (None, None)
    holder, refusal = daemon._claim_check(object(), "runtime.reload", {})
    assert holder is None and "tab 7" in refusal


class _Bridge:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def request(self, method, params, timeout):
        self.sent.append((method, params))
        return {"version": "1.0"}

    def status(self, wait):
        return {"connected": True, "browser": {"extension_version": "2.0"}}


def test_the_reload_request_carries_force_only_when_told(monkeypatch):
    monkeypatch.setattr(chrome_bootstrap, "expected_code_hash", lambda: None)
    bridge = _Bridge()
    assert chrome_bootstrap._reload_companion(bridge, "2.0")["self_update"] == "done"
    assert chrome_bootstrap._reload_companion(bridge, "2.0", force=True)["self_update"] == "done"
    assert bridge.sent == [("runtime.reload", {}), ("runtime.reload", {"force": True})]


def _refuse(source, dest):
    raise PermissionError(13, "The process cannot access the file because it is being used")


def test_a_rotation_another_process_blocks_is_not_an_error(tmp_path, monkeypatch):
    """Every server and the daemon share one log; only one of them wins the rename."""
    handler = LazyRotatingFileHandler(tmp_path / "server.log", max_bytes=200, backup_count=2)
    handler.setFormatter(logging.Formatter("%(message)s"))
    errors: list[logging.LogRecord] = []
    monkeypatch.setattr(handler, "handleError", errors.append)
    monkeypatch.setattr(handler, "rotate", _refuse)
    for number in range(6):
        handler.emit(logging.LogRecord("x", logging.INFO, __file__, 1, f"record {number} " + "x" * 60, None, None))
    handler.close()
    text = (tmp_path / "server.log").read_text(encoding="utf-8")
    assert errors == []
    assert all(f"record {number} " in text for number in range(6))
