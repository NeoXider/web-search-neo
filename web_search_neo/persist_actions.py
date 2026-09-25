"""``persist=true`` for temporary and isolated sessions: the session side of ``owned_parking``.

``browser_tools`` parks tabs of the user's Chrome (current mode). This module
parks the other kind a server can keep alive: a whole browser it launched
itself. main's ``open``, ``reattach``, ``close``, ``close_all`` and
``browser_status`` wrappers come through here; anything that is not an owned
persist session is handed to ``browser_tools`` unchanged.

- ``open`` with ``persist=true`` and ``profile_mode`` temporary/isolated starts
  chromedriver so it can outlive the server, records the session live and starts
  the record's watchdog. An existing parked record of that ``session_id`` is
  re-attached first (``open`` of a parked session continues it).
- At process exit (an ``atexit`` hook that runs before ``browser_tools``' own
  close-everything hook, because it is registered after it) every such session
  is parked instead of quit.
- ``reattach`` continues a parked browser explicitly; ``close`` of a parked one
  retires it (browser stopped, profile removed); ``close_all`` drops the
  records of the live ones it closed.

A record is never taken over while another server process holds its session
live, and nothing is killed on a pid that cannot be proven to be the one recorded.
"""
from __future__ import annotations

import atexit
import logging
import os
import time
from typing import Any

from web_search_neo import browser_tools, owned_parking, page_guards, process_probe
from web_search_neo.fetch.safety import redact_url

logger = logging.getLogger(__name__)

OWNED_MODES = owned_parking.OWNED_MODES
# Session fields a parked record carries across processes, so the continued
# session neither registers its page scripts twice nor re-reports old tabs.
_CARRIED = ("dialog_script_id", "dialog_policy", "dialog_prompt_text", "dialog_policy_set",
            "render_bootstrap_registered", "download_dir")


class ReattachRefused(ValueError):
    """A parked owned browser could not be continued."""


def _me() -> dict[str, Any]:
    return {"owner_pid": os.getpid(), "owner_started": process_probe.started_at(os.getpid())}


def _live(session_id: str) -> Any:
    with browser_tools._sessions_lock:
        return browser_tools._sessions.get(session_id)


def _session_fields(session: Any) -> dict[str, Any]:
    return {
        "profile_mode": session.profile_mode, "headless": bool(session.headless),
        "agent_label": session.agent_label, "label_tab": bool(session.label_tab),
        "url": redact_url(session.last_url) if session.last_url else None,
        **{name: getattr(session, name) for name in _CARRIED},
        "known_windows": sorted(session.known_windows) if session.known_windows is not None else None,
        "window_openers": dict(session.window_openers), "opened_windows": list(session.opened_windows),
    }


def _ensure_watchdog(session_id: str, record: dict[str, Any]) -> None:
    """One running watchdog per record, watching the record's current token."""
    token = str(record["token"])
    if record.get("watchdog_token") == token and process_probe.same_process(
            record.get("watchdog_pid"), record.get("watchdog_started")):
        return
    started = owned_parking.spawn_watchdog(session_id, token)
    if started:
        owned_parking.claim(session_id, lambda current: {**started, "watchdog_token": token}
                            if current.get("token") == token else None)


def _held_elsewhere(record: dict[str, Any]) -> bool:
    """Another server process holds this record's session live right now."""
    return (record.get("state") in {"live", "retiring"} and record.get("owner_pid") != os.getpid()
            and owned_parking.owner_alive(record))


def record_live(session_id: str, session: Any) -> None:
    """Write the live record of an owned persist session (once) and start its watchdog."""
    existing = owned_parking.read(session_id)
    ours = (existing and existing.get("owner_pid") == os.getpid() and existing.get("state") == "live"
            and existing.get("webdriver_session_id") == getattr(session.driver, "session_id", None))
    if ours:
        return
    if existing and _held_elsewhere(existing):
        raise RuntimeError(f"session '{session_id}' is recorded as live in another MCP server process")
    record = owned_parking.write(session_id, {
        "kind": "owned", "state": "live", "token": owned_parking.new_token(), **_me(),
        **owned_parking.describe_driver(session.driver), **_session_fields(session),
    })
    _ensure_watchdog(session_id, record)


def _refuse_and_clean(session_id: str, reason: str) -> ReattachRefused:
    cleaned = owned_parking.retire_and_forget(session_id, force=True)
    return ReattachRefused(
        f"Parked session '{session_id}' cannot be re-attached: {reason}. "
        + ("Its record was dropped and what was left of it cleaned up. " if cleaned else "")
        + f'Open a new page instead: web_action [{{"action":"open","url":...,"session_id":"{session_id}"}}].')


def _register(session_id: str, session: Any) -> str | None:
    """Put a continued session into the registry; why not, when it cannot go there."""
    with browser_tools._sessions_condition:
        if session_id in browser_tools._sessions or session_id in browser_tools._pending_sessions:
            return "a session of that name is being opened in this server right now"
        cap, _source = browser_tools.effective_max_sessions()
        if len(browser_tools._sessions) + len(browser_tools._pending_sessions) >= cap:
            return (f"this server already holds its maximum of {cap} browser sessions; close one "
                    "and reattach again (the parked browser keeps running)")
        browser_tools._sessions[session_id] = session
        browser_tools._sessions_condition.notify_all()
    return None


def adopt(session_id: str, record: dict[str, Any]) -> Any:
    """Continue a parked owned browser in this process, or raise :class:`ReattachRefused`."""
    if record.get("state") == "retiring" and not owned_parking.retire_is_stale(record):
        raise ReattachRefused(f"Parked session '{session_id}' is being retired right now.")
    if (record.get("state") == "live" and record.get("owner_pid") != os.getpid()
            and owned_parking.owner_alive(record)):
        raise ReattachRefused(
            f"Parked session '{session_id}' is live in another MCP server process "
            f"(pid {record.get('owner_pid')}); it was not touched.")
    if not owned_parking.driver_answers(record):
        raise _refuse_and_clean(session_id, "its browser is no longer running")
    token = owned_parking.new_token()
    expected = record.get("token")
    claimed = owned_parking.claim(session_id, lambda current: (
        {"state": "live", "token": token, **_me()}
        if current.get("token") == expected and current.get("state") in {"parked", "live"} else None))
    if claimed is None:
        raise ReattachRefused(f"Parked session '{session_id}' changed while it was being re-attached; try again.")
    try:
        driver = owned_parking.ResumedChrome(claimed)
        handles = [str(item) for item in driver.window_handles]
        wanted = claimed.get("window_handle")
        driver.switch_to.window(wanted if wanted in handles else handles[0])
    except Exception as exc:
        raise _refuse_and_clean(session_id, f"its WebDriver session is gone ({type(exc).__name__})") from None
    session = browser_tools.BrowserSession(
        driver=driver, headless=bool(claimed.get("headless", True)),
        profile_mode=str(claimed.get("profile_mode") or "temporary"), owns_browser=True,
        agent_label=claimed.get("agent_label"), persist=True, label_tab=bool(claimed.get("label_tab", True)),
    )
    for name in _CARRIED:
        if claimed.get(name) is not None:
            setattr(session, name, claimed[name])
    if claimed.get("known_windows") is not None:
        session.known_windows = set(claimed["known_windows"])
    session.window_openers = dict(claimed.get("window_openers") or {})
    session.opened_windows = list(claimed.get("opened_windows") or [])
    session.last_url = claimed.get("url")
    refused = _register(session_id, session)
    if refused:
        owned_parking.let_go(driver)
        owned_parking.claim(session_id, lambda current: {"state": "parked", "owner_pid": None}
                            if current.get("token") == token else None)
        raise ReattachRefused(f"Parked session '{session_id}' cannot be re-attached now: {refused}.")
    try:
        with session.lock:
            page_guards.setup_owned(session, session_id)
    except Exception as exc:
        logger.debug("Re-attached session '%s': page guards not restored: %s", session_id, exc)
    session.pending_notice = {"reattached": True, "reattach_note": (
        f"Session '{session_id}' was re-attached to its parked {session.profile_mode} browser, left "
        "running by an earlier MCP server process.")}
    _ensure_watchdog(session_id, claimed)
    return session


def open_page(url: str, session_id: str = "default", **options: Any) -> dict[str, Any]:
    """``browser_tools.open_page``, plus ``persist=true`` for temporary/isolated sessions."""
    resolved = (browser_tools.resolve_profile_mode(options.get("profile_mode", "auto"), options.get("headless"))
                if options.get("persist") else None)
    if resolved not in OWNED_MODES:
        return browser_tools.open_page(url, session_id=session_id, **options)
    session_id = browser_tools._validate_session_id(session_id)
    if session_id == "default":
        raise ValueError("persist=true needs an explicit session_id, not 'default'")
    if options.get("current_tab_id") is not None:
        raise ValueError("current_tab_id names a tab of the user's Chrome; it has no place in a "
                         f"{resolved} session")
    notice: dict[str, Any] = {}
    expired = owned_parking.reap_expired()
    if _live(session_id) is None:
        record = owned_parking.read(session_id)
        if record and _held_elsewhere(record):
            raise ValueError(
                f"Session '{session_id}' is a persist=true browser live in another MCP server process "
                f"(pid {record.get('owner_pid')}); it is that server's. Use another session_id.")
        if record:
            try:
                adopt(session_id, record)
            except ReattachRefused as refused:
                notice["parked_refused"] = str(refused)
    try:
        with process_probe.detached_launch():
            answer = browser_tools.open_page(url, session_id=session_id,
                                             **{**options, "profile_mode": resolved, "persist": False})
    except Exception:
        # open closed the session on the way out; a record it adopted must not outlive it.
        record = owned_parking.read(session_id)
        if record and record.get("owner_pid") == os.getpid() and _live(session_id) is None:
            _finish(session_id, record)
        raise
    session = _live(session_id)
    if session is not None:
        session.persist = True
        try:
            record_live(session_id, session)
        except Exception as exc:
            logger.warning("Could not record persistent session '%s': %s", session_id, exc)
            notice["persist_warning"] = f"the session could not be recorded for parking ({exc})"
    warning = owned_parking.outlives_client()
    return {**answer, "persist": True, **notice,
            **({"parked_expired": expired} if expired else {}),
            **({"persist_warning": warning} if warning and "persist_warning" not in notice else {})}


def reattach(session_id: str) -> dict[str, Any]:
    """``reattach``: a parked owned browser first, else the current-Chrome registry."""
    session_id = browser_tools._validate_session_id(session_id)
    reaped = next((row for row in owned_parking.reap_expired() if row.get("session_id") == session_id), None)
    if reaped is not None and _live(session_id) is None:
        return {"session_id": session_id, "success": False, "retired_parked": reaped, "error": (
            f"Parked session '{session_id}' cannot be re-attached: its browser is no longer running or "
            "its TTL ran out, so what was left of it was cleaned up. Open a new page instead: "
            f'web_action [{{"action":"open","url":...,"session_id":"{session_id}"}}].')}
    if _live(session_id) is None:
        record = owned_parking.read(session_id)
        if record:
            try:
                live = adopt(session_id, record)
            except ReattachRefused as refused:
                return {"session_id": session_id, "success": False, "error": str(refused)}
            with live.lock:
                return {**browser_tools._page_summary(live.driver, session_id), "success": True,
                        "reattached": True, "persist": True, "profile_mode": live.profile_mode}
    return browser_tools.reattach_session(session_id)


def close_session(session_id: str = "default", close_tab: bool | None = None) -> dict[str, Any]:
    """``close``; a parked owned browser is retired (stopped, profile removed)."""
    session_id = browser_tools._validate_session_id(session_id)
    was_live = _live(session_id) is not None
    answer = browser_tools.close_session(session_id, close_tab)
    record = owned_parking.read(session_id)
    if record is None:
        return answer
    if was_live:
        if record.get("owner_pid") == os.getpid():
            _finish(session_id, record)
        return answer
    retired = owned_parking.retire_and_forget(session_id, force=True)
    if retired is None:
        return {**answer, "parked_note": (
            f"Session '{session_id}' is live in another MCP server process; it was left alone.")}
    answer = {key: value for key, value in answer.items() if key != "note"}
    return {**answer, "closed": True, "retired_parked": retired}


def close_all_sessions(agent_label: str | None = None, scope: str = "mine", include_foreign: bool = False,
                       idle_for_seconds: float | None = None) -> dict[str, Any]:
    """``close_all``; the live owned persist sessions it closed lose their records too."""
    answer = browser_tools.close_all_sessions(agent_label, scope, include_foreign, idle_for_seconds)
    for session_id in answer.get("closed_sessions") or []:
        record = owned_parking.read(session_id)
        if record and record.get("owner_pid") == os.getpid():
            _finish(session_id, record)
    return answer


def _finish(session_id: str, record: dict[str, Any]) -> None:
    """After a live owned persist session was closed: nothing of it may stay behind.

    Quitting normally removes everything; a continued session's chromedriver may
    leave its profile (Chrome was still letting go of it), so the recorded tree
    and profile are checked once more before the record goes.
    """
    try:
        leftover = owned_parking.retire(record)
        if leftover.get("problem"):
            logger.warning("Closing '%s' left something behind: %s", session_id, leftover["problem"])
    finally:
        owned_parking.forget(session_id)


def parked_rows() -> list[dict[str, Any]]:
    """Parked owned browsers for ``browser_status`` (not the ones live in this server)."""
    with browser_tools._sessions_lock:
        live = set(browser_tools._sessions)
    rows = []
    for record in owned_parking.records():
        if record.get("session_id") in live:
            continue
        rows.append({"session_id": record.get("session_id"), "profile_mode": record.get("profile_mode"),
                     "agent_label": record.get("agent_label"), "state": record.get("state"),
                     "url": redact_url(record["url"]) if record.get("url") else None,
                     "parked_at": browser_tools._iso_local(record.get("parked_at") or record.get("updated_at"))})
    return rows


def get_status(session_id: str = "default") -> dict[str, Any]:
    """``browser_status`` with the parked owned browsers listed next to the parked tabs."""
    answer = browser_tools.get_status(session_id)
    try:
        expired = owned_parking.reap_expired()
        rows = parked_rows()
    except Exception as exc:
        logger.debug("Parked owned browsers could not be listed: %s", exc)
        return answer
    if rows:
        answer["parked_sessions"] = list(answer.get("parked_sessions") or []) + rows
    if expired:
        answer["parked_expired"] = list(answer.get("parked_expired") or []) + expired
    return answer


def park(session_id: str, session: Any) -> None:
    """Leave one owned persist session running and record it as parked."""
    try:
        browser_tools._reset_session_runtime_state(session)
    except Exception:
        pass
    previous = owned_parking.read(session_id) or {}
    token = previous.get("token") if previous.get("owner_pid") == os.getpid() else None
    record = owned_parking.write(session_id, {
        **{key: value for key, value in previous.items() if key not in {"session_id", "updated_at"}},
        "kind": "owned", "state": "parked", "token": token or owned_parking.new_token(),
        "owner_pid": None, "owner_started": None, "parked_at": time.time(),
        **owned_parking.describe_driver(session.driver), **_session_fields(session),
    })
    owned_parking.let_go(session.driver)
    _ensure_watchdog(session_id, record)


def _park_at_exit() -> list[str]:
    """Before ``browser_tools`` closes everything: park the owned persist sessions."""
    with browser_tools._sessions_lock:
        chosen = [(name, item) for name, item in browser_tools._sessions.items()
                  if item.persist and item.profile_mode in OWNED_MODES
                  and not getattr(item.driver, "_wsn_hung", False)]
        for name, _item in chosen:
            browser_tools._sessions.pop(name, None)
    parked: list[str] = []
    for name, item in chosen:
        if not item.lock.acquire(timeout=5.0):
            with browser_tools._sessions_lock:  # busy at exit: closed with the rest instead
                browser_tools._sessions[name] = item
            continue
        try:
            park(name, item)
            parked.append(name)
        except Exception as exc:
            logger.warning("Could not park session '%s'; it is closed instead: %s", name, exc)
            with browser_tools._sessions_lock:
                browser_tools._sessions[name] = item
        finally:
            item.lock.release()
    return parked


# Registered after browser_tools' own hook (imported above), so it runs first.
atexit.register(_park_at_exit)

__all__ = ["OWNED_MODES", "ReattachRefused", "adopt", "close_all_sessions", "close_session", "get_status",
           "open_page", "park", "parked_rows", "reattach", "record_live"]
