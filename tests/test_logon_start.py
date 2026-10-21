""""Start with Windows": on by default, kept by the daemon, off when the popup says so.

The registry is faked: these tests never read or write the real Run key.
"""

from __future__ import annotations

import json
import threading

import pytest

from web_search_neo import logon_start, node_link, run_modes
from web_search_neo.nodes import config


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DIR_ENV, str(tmp_path))
    monkeypatch.delenv(logon_start.DEFAULT_ENV, raising=False)
    store: dict[str, str] = {}
    monkeypatch.setattr(logon_start, "supported", lambda: True)
    monkeypatch.setattr(logon_start, "_read", lambda: store.get("value"))
    monkeypatch.setattr(logon_start, "_write", lambda value: store.__setitem__("value", value))
    monkeypatch.setattr(logon_start, "_delete", lambda: store.pop("value", None))
    return store


def _popup(message):
    answers, done = [], threading.Event()

    def send(text):
        answers.append(text)
        done.set()

    node_link.handle(send, {"id": 1, **message}, "9.9.9")
    assert done.wait(15)
    return json.loads(answers[0])


def test_the_command_runs_this_checkout_windowless_at_logon():
    command = logon_start.command()
    assert command.endswith("--bridge --at-logon")
    assert "main.py" in command or "web_search_neo.main" in command


def test_on_by_default_and_rewritten_when_the_checkout_moves(registry, monkeypatch):
    assert logon_start.wanted() is True
    assert logon_start.apply()["current"] is True and registry["value"] == logon_start.command()
    registry["value"] = '"C:\\old\\pythonw.exe" C:\\old\\main.py --bridge --at-logon'
    assert logon_start.state()["current"] is False
    assert logon_start.apply()["current"] is True, "a moved checkout is followed"
    monkeypatch.setenv(logon_start.DEFAULT_ENV, "0")
    assert logon_start.wanted() is False, "the environment can make off the default"


def test_switching_off_removes_the_entry_and_is_remembered(registry):
    logon_start.apply()
    answer = _popup({"op": "logon_start", "enabled": False})["result"]
    assert answer == {**answer, "enabled": False, "installed": False}
    assert "value" not in registry
    assert logon_start.apply()["installed"] is False, "a stored off is not undone on reconnect"
    assert _popup({"op": "logon_start", "enabled": True})["result"]["current"] is True


def test_the_daemon_writes_it_when_the_companion_connects(registry, monkeypatch):
    monkeypatch.setattr(node_link, "ensure_shared_node", lambda: None)
    monkeypatch.setenv(node_link.AUTOSTART_ENV, "0")
    node_link._on_companion_connected()
    assert "value" not in registry, "WEB_SEARCH_NEO_NODE_AUTOSTART=0 leaves the registry alone"
    monkeypatch.setenv(node_link.AUTOSTART_ENV, "1")
    node_link._on_companion_connected()
    assert registry["value"] == logon_start.command()


def test_a_registry_failure_is_reported_not_raised(registry, monkeypatch):
    def refuse(value):
        raise PermissionError("access denied")

    monkeypatch.setattr(logon_start, "_write", refuse)
    verdict = logon_start.apply()
    assert verdict["installed"] is False and "access denied" in verdict["error"]


def test_at_logon_keeps_the_daemon_up_and_brings_the_node(monkeypatch):
    started = []
    monkeypatch.setattr(run_modes.bridge_daemon, "run_forever", lambda version: 0)
    monkeypatch.setattr(node_link, "ensure_shared_node", lambda: started.append(1))
    monkeypatch.delenv("WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS", raising=False)

    class Now:
        def __init__(self, delay, function):
            self.function = function
            self.daemon = False

        def start(self):
            self.function()

    monkeypatch.setattr(threading, "Timer", Now)
    assert run_modes.run(["--bridge", "--at-logon"], "9.9.9") == 0
    import os

    assert os.environ["WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS"] == "0" and started == [1]
    monkeypatch.delenv("WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS")
