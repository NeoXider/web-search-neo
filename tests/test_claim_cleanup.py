"""Tests for periodic cleanup of claims whose owner process has died.

These tests do not start a real bridge daemon; they exercise the
``BridgeDaemon._cleanup_dead_claims`` logic through its portable liveness
seam, and separately exercise the real platform's process probe.
"""

from __future__ import annotations

import pytest

from web_search_neo.bridge_daemon import BridgeDaemon


class _MockClaimObj:
    """Mock claim-like object used in tests."""

    def __init__(self, client_label: str, tab_id: int) -> None:
        self.client = type("Client", (), {"label": client_label})()
        self.tab_id = tab_id
        self.since = 0.0


# ---------------------------------------------------------------------------
# _cleanup_dead_claims
# ----------------------------------------------------------------------------


def test_cleanup_releases_claims_with_dead_pid(monkeypatch) -> None:
    """Claims whose owner PID no longer exists are released."""
    daemon = BridgeDaemon(port=19999, token="", version="")
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: pid != 99999)
    daemon._claims = {
        1: _MockClaimObj("main.py#12345", 1),
        2: _MockClaimObj("main.py#99999", 2),
    }

    daemon._cleanup_dead_claims()

    assert 1 in daemon._claims, "Live claim was unexpectedly removed"
    assert 2 not in daemon._claims, "Dead claim was not released"


def test_cleanup_keeps_claims_with_living_pid(monkeypatch) -> None:
    """Claims whose owner PID still exists are kept."""
    daemon = BridgeDaemon(port=19999, token="", version="")
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: True)
    daemon._claims = {1: _MockClaimObj("main.py#12345", 1)}

    daemon._cleanup_dead_claims()

    assert 1 in daemon._claims, "Living claim was unexpectedly removed"


def test_cleanup_skips_unrecognised_label_format(monkeypatch) -> None:
    """Labels without '#' or with non-numeric PID are left untouched."""
    daemon = BridgeDaemon(port=19999, token="", version="")

    def unexpected_probe(pid: int) -> bool:
        pytest.fail(f"Unrecognised label triggered a process probe: {pid}")

    monkeypatch.setattr(daemon, "_is_process_alive", unexpected_probe)
    daemon._claims = {
        1: _MockClaimObj("no-pid-here", 1),
        2: _MockClaimObj("main.py#not-a-number", 2),
        3: _MockClaimObj("main.py#-5", 3),
    }

    daemon._cleanup_dead_claims()

    assert set(daemon._claims) == {1, 2, 3}


def test_cleanup_broadcasts_state_after_releasing(monkeypatch) -> None:
    """After releasing dead claims, _broadcast_state is called."""
    daemon = BridgeDaemon(port=19999, token="", version="")
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: False)
    broadcasts: list[dict] = []
    monkeypatch.setattr(daemon, "_broadcast_state", lambda: broadcasts.append(dict(daemon._claims)))
    daemon._claims = {1: _MockClaimObj("main.py#99999", 1)}

    daemon._cleanup_dead_claims()

    assert broadcasts == [{}], "Expected one broadcast after removing dead claims"

# ---------------------------------------------------------------------------
# _is_process_alive – on the real platform, with nothing mocked
# ---------------------------------------------------------------------------


def test_process_alive_answers_for_real_pids() -> None:
    """The liveness probe must work on this platform without raising.

    The Windows regression this guards against: ``signal.CTRL_C_EVENT`` is 0
    there, so ``os.kill(pid, 0)`` does not probe anything — it tries to deliver
    a Ctrl+C — and for a PID that is gone it raises a bare ``OSError``
    (WinError 87). That escaped the probe, killed the reaper thread on its very
    first dead claim, and claims then sat for hours.
    """
    import os as _os

    assert BridgeDaemon._is_process_alive(_os.getpid()) is True
    # 0x7FFFFFFE: a PID far outside anything a running system hands out.
    assert BridgeDaemon._is_process_alive(0x7FFFFFFE) is False


def test_probe_never_signals_the_process() -> None:
    """On Windows the probe must not go through ``os.kill`` at all."""
    import os as _os
    import sys as _sys

    if _sys.platform != "win32":
        pytest.skip("os.kill is the correct probe outside Windows")

    called: list[int] = []

    original = _os.kill
    _os.kill = lambda pid, sig: called.append(pid)
    try:
        BridgeDaemon._is_process_alive(_os.getpid())
    finally:
        _os.kill = original
    assert called == [], "the probe signalled a live process instead of asking the kernel"


def test_reaper_survives_a_failing_scan() -> None:
    """A scan that raises must not take the reaper thread with it."""
    import threading

    daemon = BridgeDaemon(port=19999, token="", version="")
    attempts: list[int] = []

    def explode() -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("scan blew up")
        daemon._stopped.set()

    daemon._cleanup_dead_claims = explode  # type: ignore[method-assign]
    daemon._stopped = threading.Event()

    import web_search_neo.bridge_daemon as module

    original_interval = module.PROCESS_CHECK_INTERVAL
    module.PROCESS_CHECK_INTERVAL = 0.01
    try:
        daemon._cleanup_dead_claims_loop()
    finally:
        module.PROCESS_CHECK_INTERVAL = original_interval

    assert len(attempts) >= 3, "the reaper stopped after the first failure"


def test_release_log_names_the_right_owner(caplog) -> None:
    """Each released tab is logged with its own holder, not the last one seen."""
    import os as _os

    dead_pid = 0x7FFFFFFE
    daemon = BridgeDaemon(port=19999, token="", version="")
    daemon._claims = {
        11: _MockClaimObj(f"alpha.py#{dead_pid}", 11),
        22: _MockClaimObj(f"beta.py#{dead_pid}", 22),
    }
    with caplog.at_level("INFO"):
        daemon._cleanup_dead_claims()

    released = " ".join(r for r in caplog.messages if "Released tab" in r)
    assert "alpha.py" in released and "beta.py" in released
    assert daemon._claims == {}
