"""``--update``: pull, then move the daemon, the node and the companion onto the new files.

No git, no daemon, no Chrome: every outside part is a fake that records what was asked.
"""

from __future__ import annotations

import pytest

from web_search_neo import chrome_bootstrap, chrome_bridge, companion_update, run_modes
from web_search_neo.nodes import client


class FakeBridge:
    made: list["FakeBridge"] = []
    claims: list[int] = []
    connects = True

    def __init__(self, *args, spawn=None, **kwargs):
        self.spawn = spawn
        self.calls: list[str] = []
        FakeBridge.made.append(self)

    def status(self, wait):
        return {"daemon": {"claims": list(FakeBridge.claims)}}

    def stop_daemon(self, reason):
        self.calls.append("stop")
        return True

    def start(self):
        self.calls.append("start")

    def wait_connected(self, timeout):
        return FakeBridge.connects

    def shutdown(self):
        self.calls.append("shutdown")


@pytest.fixture
def parts(monkeypatch):
    FakeBridge.made, FakeBridge.claims, FakeBridge.connects = [], [], True
    stopped_nodes, reloads = [], []
    monkeypatch.setattr(chrome_bridge, "ChromeBridge", FakeBridge)
    monkeypatch.setattr(companion_update, "package_checkout", lambda path: "C:/repo")
    monkeypatch.setattr(companion_update, "update_checkout", lambda root: {
        "updated": True, "from_commit": "aaa", "to_commit": "bbb", "files_changed": 3})
    monkeypatch.setattr(client, "stop_local", lambda: stopped_nodes.append(1) or True)
    monkeypatch.setattr(chrome_bootstrap, "_reload_companion",
                        lambda bridge, version: reloads.append(version) or {"self_update": "done"})
    return stopped_nodes, reloads


def test_update_restarts_every_part_onto_the_pulled_files(parts, capsys):
    stopped_nodes, reloads = parts
    assert run_modes.run(["--update"], "9.9.9") == 0
    old, fresh = FakeBridge.made
    assert old.spawn is False, "stopping must never start a daemon first"
    assert old.calls == ["stop", "shutdown"] and fresh.calls == ["start", "shutdown"]
    assert stopped_nodes == [1] and len(reloads) == 1
    out = capsys.readouterr().out
    assert "pulled aaa -> bbb, 3 file(s)" in out and "Chrome companion: reloaded" in out


def test_update_leaves_agents_alone_unless_forced(parts, capsys):
    stopped_nodes, reloads = parts
    FakeBridge.claims = [101, 102]
    assert run_modes.run(["--update"], "9.9.9") == 1
    assert "2 tab(s) are driven by agents" in capsys.readouterr().out
    assert stopped_nodes == [] and reloads == [] and len(FakeBridge.made) == 1
    assert run_modes.run(["--update", "--force"], "9.9.9") == 0
    assert stopped_nodes == [1]


def test_update_says_when_chrome_is_not_there(parts, capsys):
    FakeBridge.connects = False
    assert run_modes.run(["--update"], "9.9.9") == 1
    assert "did not connect within 25 s" in capsys.readouterr().out
