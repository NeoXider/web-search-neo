"""Bridge hardening: tab-claim enforcement on relay, the agent identity the
companion is told, connection caps, the dead-claim reaper's locking, and the
token file's atomic, owner-only storage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from web_search_neo import bridge_auth
from web_search_neo import bridge_daemon
from web_search_neo.bridge_daemon import BridgeDaemon, _Claim, _Client

from test_chrome_bridge import (
    TEST_TOKEN,
    _FakeCompanion,
    _companion_socket,
    _free_port,
    _handshake,
    _next_frame,
    _running_daemon,
)


def _raw_client(port: int, nonce: str, pid: int, name: str | None = None):
    websocket = connect(f"ws://127.0.0.1:{port}")
    websocket.send(
        json.dumps(
            {
                "type": "hello",
                "protocol": bridge_daemon.PROTOCOL,
                "role": "client",
                "nonce": nonce,
                "version": "9.9.9",
                "client": {"pid": pid, "program": "agent.py", "name": name},
            }
        )
    )
    challenge = json.loads(websocket.recv(timeout=5.0))
    proof = bridge_auth.sign(
        TEST_TOKEN, bridge_auth.client_proof_message(challenge["nonce"], nonce)
    )
    websocket.send(json.dumps({"type": "auth", "proof": proof}))
    assert _next_frame(websocket, "hello_ack")["type"] == "hello_ack"
    return websocket


def _control(websocket, method: str, **fields) -> dict:
    websocket.send(json.dumps({"type": "control", "id": method, "method": method, **fields}))
    return _next_frame(websocket, "control_result")["result"]


def _command(websocket, request_id: str, method: str, params: dict) -> None:
    websocket.send(
        json.dumps({"type": "command", "id": request_id, "method": method, "params": params})
    )


def test_a_command_on_another_agents_tab_is_refused_with_the_owner_named() -> None:
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port, run="one-browser")
        owner = _raw_client(daemon.port, "a1" * 16, pid=111, name="alpha")
        intruder = _raw_client(daemon.port, "b2" * 16, pid=222)
        try:
            assert _control(owner, "claim_tab", tab_id=41)["granted"] is True

            _command(intruder, "steal", "cdp.send", {"tabId": 41, "method": "Input.insertText"})
            refused = _next_frame(intruder, "result")
            assert refused["id"] == "steal"
            assert "agent.py#111" in refused["error"] and "claimed" in refused["error"]

            # The same refusal for a tab id sent as a string.
            _command(intruder, "steal2", "tabs.navigate", {"tabId": "41", "url": "about:blank"})
            assert "agent.py#111" in _next_frame(intruder, "result")["error"]

            # Harmless metadata reads and unclaimed tabs still go through.
            _command(intruder, "peek", "tabs.get", {"tabId": 41})
            peek = companion.take_command()
            assert peek["method"] == "tabs.get"
            assert peek["agent"]["label"] == "agent.py#222"
            assert peek["agent"]["claim_holder"] == "agent.py#111"
            companion.answer(peek, {"id": 41})
            assert _next_frame(intruder, "result")["result"] == {"id": 41}

            _command(intruder, "free", "tabs.get", {"tabId": 42})
            companion.answer(companion.take_command(), {"id": 42})
            assert _next_frame(intruder, "result")["result"] == {"id": 42}

            # The owner itself is relayed, and the companion learns who it is.
            _command(owner, "mine", "cdp.send", {"tabId": 41, "method": "Page.reload"})
            relayed = companion.take_command()
            assert relayed["agent"] == {
                "label": "agent.py#111",
                "name": "alpha",
                "pid": 111,
                "program": "agent.py",
                "version": "9.9.9",
                "claim_holder": "agent.py#111",
            }
            companion.answer(relayed, "ok")
            assert _next_frame(owner, "result")["result"] == "ok"
        finally:
            for socket_ in (owner, intruder):
                socket_.close()
            companion.close()


def test_a_claim_whose_owner_left_does_not_block_the_relay() -> None:
    daemon = BridgeDaemon(port=_free_port(), token=TEST_TOKEN)
    ghost = _Client(connection=None, version="", label="gone.py#1")
    live = _Client(connection=None, version="", label="live.py#2")
    daemon._clients = {live}
    daemon._claims = {7: _Claim(client=ghost, tab_id=7, browser_run=None, claimed_at=0.0)}
    holder, refusal = daemon._claim_check(live, "cdp.send", {"tabId": 7})
    # The orphaned claim moves to the live requester instead of dangling.
    assert refusal is None and holder == "live.py#2"
    assert daemon._claims[7].client is live
    assert daemon._claim_check(live, "tabs.list", {}) == (None, None)
    assert daemon._claim_check(live, "tabs.list", {"tabId": None}) == (None, None)


@pytest.mark.parametrize("spelling", [41, 41.0, "41", "41.0", " 41 ", "0x29", "4.1e1", "+41"])
def test_every_spelling_the_companion_reads_as_the_claimed_tab_is_refused(spelling) -> None:
    daemon = BridgeDaemon(port=_free_port(), token=TEST_TOKEN)
    owner = _Client(connection=None, version="", label="owner.py#1", pid=1, program="owner.py")
    intruder = _Client(connection=None, version="", label="other.py#2", pid=2, program="other.py")
    daemon._clients = {owner, intruder}
    daemon._claims = {41: _Claim(client=owner, tab_id=41, browser_run=None, claimed_at=0.0)}
    params = {"tabId": spelling}
    holder, refusal = daemon._claim_check(intruder, "cdp.send", params)
    assert holder == "owner.py#1" and "owner.py#1" in refusal
    # What the companion receives is the int the check was made against.
    assert params["tabId"] == 41 and type(params["tabId"]) is int


@pytest.mark.parametrize(
    "junk", [True, "x", "", "41.5", 41.5, float("nan"), float("inf"), "Infinity", "1_0",
             "0x", "-0x29", [41], {"id": 41}]
)
def test_a_tab_id_the_daemon_cannot_read_like_the_companion_is_refused(junk) -> None:
    daemon = BridgeDaemon(port=_free_port(), token=TEST_TOKEN)
    client = _Client(connection=None, version="", label="a.py#1")
    daemon._clients = {client}
    holder, refusal = daemon._claim_check(client, "tabs.get", {"tabId": junk})
    assert holder is None and "tabId must be an integer" in refusal


def test_a_reconnected_server_keeps_its_tabs_while_the_old_socket_lingers() -> None:
    daemon = BridgeDaemon(port=_free_port(), token=TEST_TOKEN)
    old = _Client(connection=None, version="", label="main.py#77", pid=77, program="main.py")
    new = _Client(connection=None, version="", label="main.py#77", pid=77, program="main.py")
    stranger = _Client(connection=None, version="", label="main.py#78", pid=78, program="main.py")
    daemon._clients = {old, new, stranger}  # the old half-open socket is still registered
    daemon._claims = {5: _Claim(client=old, tab_id=5, browser_run=None, claimed_at=0.0)}
    daemon._broadcast_state = lambda: None

    assert daemon._claim_check(new, "cdp.send", {"tabId": 5}) == ("main.py#77", None)
    assert daemon._claims[5].client is new
    # A different process with the same program is still a stranger.
    assert daemon._claim_check(stranger, "cdp.send", {"tabId": 5})[1] is not None

    daemon._claims[5].client = old
    assert daemon._claim_tab(new, 5)["granted"] is True
    assert daemon._claims[5].client is new
    daemon._claims[5].client = old
    assert daemon._release_tab(new, "5")["released"] is True
    assert 5 not in daemon._claims
    daemon._claims[6] = _Claim(client=old, tab_id=6, browser_run=None, claimed_at=0.0)
    assert daemon._claim_tab(stranger, 6)["granted"] is False
    assert daemon._release_tab(stranger, 6)["released"] is False


def test_two_bridges_in_one_process_stay_distinct_by_instance() -> None:
    from web_search_neo.bridge_claims import same_agent

    first = _Client(connection=None, version="", label="main.py#5", pid=5, program="main.py",
                    instance="a" * 32)
    again = _Client(connection=None, version="", label="main.py#5", pid=5, program="main.py",
                    instance="a" * 32)
    other = _Client(connection=None, version="", label="main.py#5", pid=5, program="main.py",
                    instance="b" * 32)
    assert same_agent(first, again)
    assert not same_agent(first, other)


def test_clients_without_a_pid_are_never_merged() -> None:
    from web_search_neo.bridge_claims import same_agent

    first = _Client(connection=None, version="", label="client#?")
    second = _Client(connection=None, version="", label="client#?")
    assert not same_agent(first, second)
    assert same_agent(first, first)


def test_pending_handshakes_are_capped(monkeypatch) -> None:
    monkeypatch.setattr(bridge_daemon, "MAX_PENDING_HANDSHAKES", 2)
    with _running_daemon() as daemon:
        idle = [connect(f"ws://127.0.0.1:{daemon.port}") for _ in range(2)]
        try:
            extra = connect(f"ws://127.0.0.1:{daemon.port}")
            with pytest.raises(ConnectionClosed) as rejection:
                extra.recv(timeout=5.0)
            assert rejection.value.rcvd.code == 1013
        finally:
            for websocket in idle:
                websocket.close()


def test_total_connections_are_capped(monkeypatch) -> None:
    monkeypatch.setattr(bridge_daemon, "MAX_CONNECTIONS", 1)
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port)
        try:
            with _companion_socket(daemon.port) as websocket:
                with pytest.raises(ConnectionClosed) as rejection:
                    _handshake(websocket, nonce="3c" * 16)
            assert rejection.value.rcvd.code == 1013
            assert daemon.connected
        finally:
            companion.close()


def test_the_reaper_spares_connected_clients_and_broadcasts_outside_the_lock(monkeypatch) -> None:
    daemon = BridgeDaemon(port=_free_port(), token=TEST_TOKEN)
    connected = _Client(connection=None, version="", label="main.py#99999")
    gone = _Client(connection=None, version="", label="main.py#99998")
    daemon._clients = {connected}
    daemon._claims = {
        1: _Claim(client=connected, tab_id=1, browser_run=None, claimed_at=0.0),
        2: _Claim(client=gone, tab_id=2, browser_run=None, claimed_at=0.0),
    }
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: False)
    free_during_broadcast: list[bool] = []

    def broadcast() -> None:
        # Another thread must be able to take the register while we broadcast.
        result: list[bool] = []

        def probe_lock() -> None:
            acquired = daemon._lock.acquire(timeout=1)
            result.append(acquired)
            if acquired:
                daemon._lock.release()

        probe = threading.Thread(target=probe_lock)
        probe.start()
        probe.join()
        free_during_broadcast.append(bool(result and result[0]))

    monkeypatch.setattr(daemon, "_broadcast_state", broadcast)
    daemon._cleanup_dead_claims()
    assert set(daemon._claims) == {1}
    assert free_during_broadcast == [True]


# -- token storage ------------------------------------------------------------


def test_token_writes_are_atomic_and_leave_no_temp_files(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)
    token = bridge_auth.load_or_create_token()
    assert target.read_text(encoding="utf-8") == token
    extension = tmp_path / "ext" / "bridge-token.js"
    monkeypatch.setattr(bridge_auth, "EXTENSION_TOKEN_FILE", extension)
    bridge_auth.write_extension_token(TEST_TOKEN)
    bridge_auth.write_extension_token(bridge_auth.load_or_create_token())
    leftovers = [path.name for path in tmp_path.rglob("*.tmp")]
    assert leftovers == []


def test_a_read_error_never_mints_a_new_token(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    target.write_text(TEST_TOKEN, encoding="utf-8")
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)
    monkeypatch.setattr(bridge_auth, "_READ_RETRY_SECONDS", 0.0)
    original = type(target).read_text
    calls: list[int] = []

    def locked(self, *args, **kwargs):
        if self == target:
            calls.append(1)
            raise PermissionError("held by a concurrent writer")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(target), "read_text", locked)
    with pytest.raises(PermissionError):
        bridge_auth.load_or_create_token()
    assert len(calls) == bridge_auth._READ_ATTEMPTS
    monkeypatch.setattr(type(target), "read_text", original)
    assert target.read_text(encoding="utf-8") == TEST_TOKEN


def test_a_briefly_unreadable_token_is_retried_not_replaced(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    target.write_text(TEST_TOKEN, encoding="utf-8")
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)
    monkeypatch.setattr(bridge_auth, "_READ_RETRY_SECONDS", 0.0)
    original = type(target).read_text
    answers = iter(["", "a1a1"])

    def flaky(self, *args, **kwargs):
        answer = next(answers, None) if self == target else None
        return original(self, *args, **kwargs) if answer is None else answer

    monkeypatch.setattr(type(target), "read_text", flaky)
    assert bridge_auth.load_or_create_token() == TEST_TOKEN


def test_concurrent_minting_agrees_on_one_token(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)
    real_write = bridge_auth._write_private

    def racing_write(path, text, *, exclusive=False):
        # Another process wins the race just before this one moves its file in.
        if exclusive and not target.exists():
            real_write(path, TEST_TOKEN)
        return real_write(path, text, exclusive=exclusive)

    monkeypatch.setattr(bridge_auth, "_write_private", racing_write)
    assert bridge_auth.load_or_create_token() == TEST_TOKEN
    assert target.read_text(encoding="utf-8") == TEST_TOKEN


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_token_files_are_readable_by_the_current_user_only(tmp_path, monkeypatch) -> None:
    extension = tmp_path / "bridge-token.js"
    monkeypatch.setattr(bridge_auth, "EXTENSION_TOKEN_FILE", extension)
    bridge_auth.write_extension_token(TEST_TOKEN)
    listing = subprocess.run(
        ["icacls", str(extension)], capture_output=True, text=True, check=True
    ).stdout
    entries = [line.replace(str(extension), "").strip() for line in listing.splitlines()]
    entries = [entry for entry in entries if ":" in entry and "Successfully" not in entry]
    assert entries, listing
    user = os.environ["USERNAME"].lower()
    assert all(user in entry.lower() for entry in entries), listing
    assert "(I)" not in listing, "inherited entries must be gone"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_token_files_are_mode_0600(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)
    bridge_auth.load_or_create_token()
    assert target.stat().st_mode & 0o777 == 0o600


def _record_restrictions(monkeypatch) -> list[Path]:
    calls: list[Path] = []

    def fake_restrict(path):
        calls.append(Path(path))
        return True

    monkeypatch.setattr(bridge_auth, "restrict_to_current_user", fake_restrict)
    monkeypatch.setattr(bridge_auth, "_RESTRICTED", set())
    return calls


def test_a_pre_existing_token_file_is_locked_down_again(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    target.write_text(TEST_TOKEN, encoding="utf-8")  # e.g. left by an older release
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)
    calls = _record_restrictions(monkeypatch)

    assert bridge_auth.load_or_create_token() == TEST_TOKEN

    assert calls == [target]
    assert target.read_text(encoding="utf-8") == TEST_TOKEN
    # Once per process per file: a second load does not respawn icacls.
    bridge_auth.load_or_create_token()
    assert calls == [target]


def test_an_unchanged_extension_token_file_is_locked_down_again(tmp_path, monkeypatch) -> None:
    extension = tmp_path / "ext" / "bridge-token.js"
    extension.parent.mkdir()
    extension.write_text(f'export const BRIDGE_TOKEN = "{TEST_TOKEN}";\n', encoding="utf-8")
    monkeypatch.setattr(bridge_auth, "EXTENSION_TOKEN_FILE", extension)
    calls = _record_restrictions(monkeypatch)
    before = extension.stat().st_mtime_ns

    assert bridge_auth.write_extension_token(TEST_TOKEN) == extension

    assert calls == [extension]
    assert extension.stat().st_mtime_ns == before  # not rewritten, only re-secured


def test_a_replaced_token_file_is_locked_down_again(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    target.write_text(TEST_TOKEN, encoding="utf-8")
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)
    calls = _record_restrictions(monkeypatch)
    bridge_auth.load_or_create_token()
    target.unlink()
    target.write_text("b2" * 32 + "\n", encoding="utf-8")

    assert bridge_auth.load_or_create_token() == "b2" * 32
    assert calls == [target, target]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_icacls_runs_hidden_on_an_existing_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    target.write_text(TEST_TOKEN, encoding="utf-8")
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)
    monkeypatch.setattr(bridge_auth, "_RESTRICTED", set())
    seen: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        seen.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(bridge_auth.subprocess, "run", fake_run)
    bridge_auth.load_or_create_token()
    assert len(seen) == 1
    args, kwargs = seen[0]
    assert args[:3] == ["icacls", str(target), "/inheritance:r"]
    assert kwargs["creationflags"] == bridge_auth._CREATE_NO_WINDOW


def test_an_acl_failure_is_logged_not_fatal(tmp_path, monkeypatch, caplog) -> None:
    def refuse(*args, **kwargs):
        raise OSError("icacls is missing")

    monkeypatch.setattr(bridge_auth.subprocess, "run", refuse)
    monkeypatch.setattr(bridge_auth.os, "chmod", refuse)
    monkeypatch.setattr(bridge_auth, "EXTENSION_TOKEN_FILE", tmp_path / "bridge-token.js")
    with caplog.at_level("WARNING"):
        bridge_auth.write_extension_token(TEST_TOKEN)
    assert (tmp_path / "bridge-token.js").is_file()
    assert any("restrict" in message for message in caplog.messages)
