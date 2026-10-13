"""The self-update path: what it pulls, what it refuses, and what it reloads.

Nothing here touches a real git or a real Chrome. The commands are recorded, so the
tests can prove the refusals happen *before* anything runs - a dirty tree is never
fetched, a busy companion is never pulled at all - and that a reinstall is asked for
only when the pull changed what a reinstall is for.
"""

from __future__ import annotations

import pathlib

import pytest

from web_search_neo import chrome_bootstrap, companion_update, main


ROOT = pathlib.Path("C:/checkout")


class FakeRunner:
    """Records every command and answers from canned git output."""

    def __init__(
        self,
        *,
        status_out: str = "",
        behind: str = "3",
        head_sequence: tuple[str, ...] = ("a1b2c3d4e5f6a1b2c3d4e5f6", "ffee00112233ffee00112233"),
        upstream: str = "origin/main\n",
        pull_code: int = 0,
        pull_out: str = "Fast-forward\n README.md | 2 +-\n",
        changed_out: str = "web_search_neo/main.py\nREADME.md\n",
        pip_code: int = 0,
    ) -> None:
        self.commands: list[list[str]] = []
        self.status_out = status_out
        self.behind = behind
        self.head_sequence = list(head_sequence)
        self.upstream = upstream
        self.pull_code = pull_code
        self.pull_out = pull_out
        self.changed_out = changed_out
        self.pip_code = pip_code

    def __call__(self, command: list[str], cwd: pathlib.Path) -> tuple[int, str]:
        assert cwd == ROOT
        self.commands.append(list(command))
        joined = " ".join(command)
        if joined.startswith("git status"):
            return 0, self.status_out
        if joined.startswith("git rev-parse HEAD"):
            index = min(len(self.head_sequence) - 1, sum(1 for c in self.commands if c[1:3] == ["pull", "--ff-only"]))
            return 0, self.head_sequence[index] + "\n"
        if joined.startswith("git rev-parse --abbrev-ref"):
            return (0, self.upstream) if self.upstream else (1, "")
        if joined.startswith("git fetch"):
            return 0, ""
        if joined.startswith("git rev-list"):
            return (0, self.behind + "\n") if self.behind is not None else (128, "no upstream\n")
        if joined.startswith("git pull"):
            return self.pull_code, self.pull_out
        if joined.startswith("git diff --name-only"):
            return 0, self.changed_out
        if "-m pip install" in joined:
            return self.pip_code, "Successfully installed web-search-neo\n"
        raise AssertionError(f"unexpected command: {joined}")

    def ran(self, fragment: str) -> bool:
        return any(fragment in " ".join(command) for command in self.commands)


class FakeBridge:
    def __init__(self, claims: list[dict] | None = None) -> None:
        self.claims = claims or []
        self.stopped: list[str] = []
        self.started = 0
        self.requested: list[str] = []

    def status(self, timeout: float) -> dict:
        return {"connected": True, "daemon": {"claims": self.claims}, "browser": {}}

    def stop_daemon(self, reason: str = "requested") -> bool:
        self.stopped.append(reason)
        return True

    def start(self) -> None:
        self.started += 1

    def request(self, method: str, params: dict, timeout: float) -> dict:
        self.requested.append(method)
        return {"reloading": True}


def test_clean_checkout_is_pulled_and_reinstalled_only_when_that_is_what_changed():
    runner = FakeRunner(changed_out="pyproject.toml\nweb_search_neo/main.py\n")
    result = companion_update.update_checkout(ROOT, runner=runner, python_executable="python")
    assert result["updated"] is True
    assert result["from_commit"] == "a1b2c3d4e5f6"
    assert result["to_commit"] == "ffee00112233"
    assert result["reinstalled"] is True
    assert result["python_code_changed"] is True
    assert runner.ran("-m pip install")


def test_a_pull_that_only_touched_docs_does_not_reinstall():
    runner = FakeRunner(changed_out="README.md\ndocs/complex-forms.md\n")
    result = companion_update.update_checkout(ROOT, runner=runner, python_executable="python")
    assert result["updated"] is True
    assert result["reinstalled"] is False
    assert not runner.ran("pip install")


def test_uncommitted_tracked_work_is_refused_before_anything_is_fetched():
    runner = FakeRunner(status_out=" M web_search_neo/main.py\n?? scratch.png\n")
    result = companion_update.update_checkout(ROOT, runner=runner)
    assert result["updated"] is False
    assert result["refused"] == "uncommitted_work"
    assert result["uncommitted_paths"] == ["web_search_neo/main.py"]
    assert result["uncommitted_count"] == 1
    assert not runner.ran("git fetch")
    assert not runner.ran("git pull")


def test_an_up_to_date_checkout_says_so_and_pulls_nothing():
    runner = FakeRunner(behind="0")
    result = companion_update.update_checkout(ROOT, runner=runner)
    assert result["updated"] is False
    assert result["already_current"] is True
    assert result["branch"] == "origin/main"
    assert not runner.ran("git pull")


def test_a_diverged_branch_is_named_and_left_alone():
    runner = FakeRunner(pull_code=1, pull_out="fatal: Not possible to fast-forward, aborting.\n")
    result = companion_update.update_checkout(ROOT, runner=runner)
    assert result["updated"] is False
    assert result["refused"] == "not_fast_forward"
    assert "diverged" in result["reason"]
    assert result["behind"] == 3
    assert not runner.ran("pip install")


def test_a_branch_without_upstream_cannot_be_updated_by_this_action():
    runner = FakeRunner(upstream="", behind=None)
    result = companion_update.update_checkout(ROOT, runner=runner)
    assert result["updated"] is False
    assert result["refused"] == "no_upstream"


def test_reinstall_can_be_declined_by_environment_without_losing_the_pull(monkeypatch):
    monkeypatch.setenv(companion_update.REINSTALL_ENV, "0")
    runner = FakeRunner(changed_out="pyproject.toml\n")
    result = companion_update.update_checkout(ROOT, runner=runner, python_executable="python")
    assert result["updated"] is True
    assert result["reinstalled"] is False
    assert "WEB_SEARCH_NEO_UPDATE_REINSTALL=0" in result["reinstall_note"]
    assert not runner.ran("pip install")


@pytest.mark.parametrize(
    "claims, local_sessions", [([{"tab_id": 4, "client": "agent"}], 0), ([], 2)],
)
def test_nothing_is_pulled_while_an_agent_is_driving_a_tab(monkeypatch, claims, local_sessions):
    monkeypatch.setattr(companion_update, "package_checkout", lambda path: ROOT)
    runner = FakeRunner()
    bridge = FakeBridge(claims=claims)
    result = companion_update.update_companion(
        runner=runner, bridge_getter=lambda: bridge, local_sessions=local_sessions,
        package_file=ROOT / "web_search_neo" / "__init__.py",
    )
    assert result["updated"] is False
    assert result["refused"] == "agent_driving_tab"
    assert runner.commands == []


def test_a_server_not_running_from_a_checkout_refuses_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(companion_update, "package_checkout", lambda path: None)
    runner = FakeRunner()
    result = companion_update.update_companion(runner=runner, bridge_getter=lambda: FakeBridge())
    assert result["updated"] is False
    assert result["refused"] == "not_a_checkout"
    assert runner.commands == []


def test_the_companion_is_reloaded_onto_the_pulled_files_and_the_daemon_replaced(monkeypatch):
    monkeypatch.setattr(companion_update, "package_checkout", lambda path: ROOT)
    reloaded: list[str] = []

    def fake_reload(bridge, expected_version: str) -> dict:
        reloaded.append(expected_version)
        return {"self_update": "done", "replaced_version": "1.25.0"}

    monkeypatch.setattr(chrome_bootstrap, "_reload_companion", fake_reload)
    bridge = FakeBridge()
    runner = FakeRunner(changed_out="web_search_neo/browser_tools.py\n")
    result = companion_update.update_companion(
        runner=runner, bridge_getter=lambda: bridge, package_file=ROOT / "web_search_neo" / "__init__.py",
    )
    assert result["companion_reloaded"] is True
    assert reloaded == [chrome_bootstrap.expected_extension_version()]
    assert bridge.stopped == ["companion update"] and bridge.started == 1
    assert result["daemon_restarted"] is True
    assert "the code it was started with" in result["server_restart_note"]


def test_a_docs_only_pull_leaves_the_daemon_and_this_server_where_they_were(monkeypatch):
    monkeypatch.setattr(companion_update, "package_checkout", lambda path: ROOT)
    monkeypatch.setattr(chrome_bootstrap, "_reload_companion", lambda bridge, version: {"self_update": "done"})
    bridge = FakeBridge()
    result = companion_update.update_companion(
        runner=FakeRunner(changed_out="README.md\n"), bridge_getter=lambda: bridge,
        package_file=ROOT / "web_search_neo" / "__init__.py",
    )
    assert bridge.stopped == [] and bridge.started == 0
    assert result["daemon_restarted"] is False
    assert result["companion_reloaded"] is True
    assert result["server_restart_note"] == ""


def test_an_unreachable_companion_still_gets_the_pull_and_says_reload_was_not_done(monkeypatch):
    monkeypatch.setattr(companion_update, "package_checkout", lambda path: ROOT)
    result = companion_update.update_companion(
        runner=FakeRunner(changed_out="chrome-extension/popup.js\n"),
        bridge_getter=lambda: (_ for _ in ()).throw(RuntimeError("no daemon")),
        package_file=ROOT / "web_search_neo" / "__init__.py",
    )
    assert result["updated"] is True
    assert result["companion_reloaded"] is False
    assert result["companion_reload"]["self_update"] == "not_attempted"


def test_the_action_is_offered_and_says_what_it_refuses():
    actions = main._capabilities()["actions"]
    assert "update_companion" in actions
    assert "refused" in actions["update_companion"]["summary"]
