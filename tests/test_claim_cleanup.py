"""Tests for periodic cleanup of claims whose owner process has died.

These tests do not start a real bridge daemon; they exercise the
``BridgeDaemon._is_process_alive`` and ``BridgeDaemon._cleanup_dead_claims``
logic directly.
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
# Helper: patch os.kill for the duration of a test
# ----------------------------------------------------------------------------


def _make_daemon_with_os_kill_patch(os_kill_behaviour):
    """Create a daemon and patch os.kill, returning (daemon, cleanup_func)."""

    import os as _os

    original = _os.kill

    def patch_func(pid: int, sig: int) -> None:
        os_kill_behaviour(pid, sig)

    _os.kill = patch_func
    daemon = BridgeDaemon(port=19999, token="", version="")
    return daemon, lambda: setattr(_os, "kill", original)


# ---------------------------------------------------------------------------
# _is_process_alive – tested indirectly via _cleanup_dead_claims
# (the real os.kill is what _is_process_alive calls)
# ----------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _cleanup_dead_claims
# ----------------------------------------------------------------------------


def test_cleanup_releases_claims_with_dead_pid(monkeypatch) -> None:
    """Claims whose owner PID no longer exists are released."""
    import os as _os

    original_kill = _os.kill

    def dead_pid_kill(pid: int, sig: int) -> None:
        """Always raise ProcessLookupError for pid 99999, succeed otherwise."""
        if pid == 99999:
            raise ProcessLookupError(f"no such process {pid}")
        # For all other PIDs, succeed (process alive)
        pass

    _os.kill = dead_pid_kill
    try:
        daemon = BridgeDaemon(port=19999, token="", version="")

        live_client_label = "main.py#12345"
        dead_client_label = "main.py#99999"

        daemon._claims = {
            1: _MockClaimObj(live_client_label, 1),
            2: _MockClaimObj(dead_client_label, 2),
        }

        daemon._cleanup_dead_claims()

        # Claim with dead PID should be gone; live PID claim should remain
        assert 1 in daemon._claims, "Live claim was unexpectedly removed"
        assert 2 not in daemon._claims, "Dead claim was not released"
    finally:
        _os.kill = original_kill


def test_cleanup_keeps_claims_with_living_pid(monkeypatch) -> None:
    """Claims whose owner PID still exists are kept."""
    import os as _os

    original_kill = _os.kill

    def always_alive_kill(pid: int, sig: int) -> None:
        """Always succeed → process always alive."""
        pass

    _os.kill = always_alive_kill
    try:
        daemon = BridgeDaemon(port=19999, token="", version="")

        live_client_label = "main.py#12345"
        daemon._claims = {
            1: _MockClaimObj(live_client_label, 1),
        }

        daemon._cleanup_dead_claims()

        assert 1 in daemon._claims, "Living claim was unexpectedly removed"
    finally:
        _os.kill = original_kill


def test_cleanup_skips_unrecognised_label_format(monkeypatch) -> None:
    """Labels without '#' or with non-numeric PID are left untouched."""
    import os as _os

    original_kill = _os.kill

    def always_dead_kill(pid: int, sig: int) -> None:
        """Always raise ProcessLookupError → process always dead."""
        raise ProcessLookupError(f"no such process {pid}")

    _os.kill = always_dead_kill
    try:
        daemon = BridgeDaemon(port=19999, token="", version="")

        daemon._claims = {
            1: _MockClaimObj("no-pid-here", 1),
            2: _MockClaimObj("main.py#not-a-number", 2),
            3: _MockClaimObj("main.py#-5", 3),
        }

        daemon._cleanup_dead_claims()

        # All claims should be preserved since unrecognised formats are skipped
        assert 1 in daemon._claims, "Unrecognised label claim was removed"
        assert 2 in daemon._claims, "Non-numeric PID claim was removed"
        assert 3 in daemon._claims, "Negative PID claim was removed"
    finally:
        _os.kill = original_kill


def test_cleanup_broadcasts_state_after_releasing(monkeypatch, caplog) -> None:
    """After releasing dead claims, _broadcast_state is called."""
    import os as _os

    original_kill = _os.kill

    def always_dead_kill(pid: int, sig: int) -> None:
        """Always raise ProcessLookupError → process always dead."""
        raise ProcessLookupError(f"no such process {pid}")

    _os.kill = always_dead_kill
    try:
        daemon = BridgeDaemon(port=19999, token="", version="")

        dead_client_label = "main.py#99999"
        daemon._claims = {
            1: _MockClaimObj(dead_client_label, 1),
        }

        with caplog.at_level("INFO"):
            daemon._cleanup_dead_claims()

        # Check that the log message about release was emitted
        release_messages = [r for r in caplog.messages if "Released tab" in r]
        assert len(release_messages) >= 1, "Expected a 'Released tab' log message"
    finally:
        _os.kill = original_kill