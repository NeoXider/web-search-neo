"""Browsers the server launched that outlive it: ``persist=true`` on temporary/isolated.

A temporary or isolated session is one Chrome plus one chromedriver started by
this MCP server. Opened with ``persist=true``, both are started so they can
outlive the server (``process_probe.detached_launch``). At process exit the
session is not quit: chromedriver's handle is let go of, and a record is kept
here - chromedriver's URL and WebDriver session id, the pids with their start
stamps, the temporary profile, the window it drove. A later server process
continues it explicitly (``reattach``, or ``open`` with ``persist=true``) by
talking to the same chromedriver session again (:class:`ResumedChrome`).

Nothing is ever killed on a pid alone: a pid counts only while its start stamp
still matches the one recorded with it, because the number is handed to the
next program once the process is gone.

Every record has a watchdog: a small detached Python process
(``python -m web_search_neo.owned_parking --watch``) that retires the browser
when the record's TTL (``WEB_SEARCH_NEO_PARKED_SESSION_TTL``, the same setting
as current-Chrome parking) runs out while it is parked, or when the server
that holds it died without parking it. So a parked browser never outlives its
TTL even when no server is ever started again. Retiring quits the WebDriver
session (chromedriver closes Chrome and deletes the profile), stops
chromedriver, kills whatever of the recorded process tree is still there, and
removes the temporary ``scoped_dir*`` profile.

This module never imports ``browser_tools``; ``persist_actions.py`` does the
session bookkeeping on top of it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable
from urllib import request as _http
import uuid

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection
from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

from web_search_neo import process_probe
from web_search_neo.actions import scripts as _scripts
from web_search_neo.sessions import parking

logger = logging.getLogger(__name__)

OWNED_MODES = frozenset({"temporary", "isolated"})
POLL_SECONDS = 5.0
# How long retiring keeps trying to delete a profile whose files are still locked.
PROFILE_REMOVAL_SECONDS = 15.0
# A retirement that has not finished by then was interrupted; another may take it over.
_RETIRE_STALE_SECONDS = 120.0


def registry_path() -> Path:
    """Next to the current-Chrome registry (and moved with its override), in a file of its own."""
    base = parking.registry_path()
    return base.with_name(f"{base.stem}_owned{base.suffix or '.json'}")


# ---------------------------------------------------------------- records

def read(session_id: str) -> dict[str, Any] | None:
    """The record for ``session_id`` in any state, or None."""
    path = registry_path()
    if not path.exists():
        return None
    found = parking.transact(lambda records: records.get(session_id), path)
    return dict(found, session_id=session_id) if found else None


def records() -> list[dict[str, Any]]:
    path = registry_path()
    if not path.exists():
        return []
    rows = parking.transact(lambda every: [dict(value, session_id=key) for key, value in every.items()], path)
    return sorted(rows, key=lambda item: float(item.get("updated_at") or 0))


def write(session_id: str, record: dict[str, Any]) -> dict[str, Any]:
    stored = {**{k: v for k, v in record.items() if k != "session_id"}, "updated_at": time.time()}

    def put(every: dict[str, dict[str, Any]]) -> None:
        every[session_id] = stored

    parking.transact(put, registry_path())
    return dict(stored, session_id=session_id)


def claim(session_id: str, check: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any] | None:
    """Change one record atomically: ``check`` returns the fields to set, or None to refuse."""
    def change(every: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        current = every.get(session_id)
        if current is None:
            return None
        update = check(dict(current, session_id=session_id))
        if update is None:
            return None
        every[session_id] = {**current, **update}
        return dict(every[session_id], session_id=session_id)

    if not registry_path().exists():
        return None
    return parking.transact(change, registry_path())


def forget(session_id: str, token: str | None = None) -> bool:
    """Drop the record (only while it still carries ``token``, when one is given)."""
    def drop(every: dict[str, dict[str, Any]]) -> bool:
        current = every.get(session_id)
        if current is None or (token is not None and current.get("token") != token):
            return False
        del every[session_id]
        return True

    if not registry_path().exists():
        return False
    return bool(parking.transact(drop, registry_path()))


def new_token() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- liveness

def owner_alive(record: dict[str, Any]) -> bool:
    """The server process that holds the session live is still running.

    Unlike the browser's pids, an owner whose start stamp could not be read is
    taken to be alive: the wrong guess there retires a browser somebody is using.
    """
    pid = record.get("owner_pid")
    if not record.get("owner_started"):
        return isinstance(pid, int) and not isinstance(pid, bool) and process_probe.process_alive(pid)
    return process_probe.same_process(pid, record.get("owner_started"))


def driver_alive(record: dict[str, Any]) -> bool:
    return process_probe.same_process(record.get("driver_pid"), record.get("driver_started"))


def chrome_alive(record: dict[str, Any]) -> bool:
    return process_probe.same_process(record.get("chrome_pid"), record.get("chrome_started"))


def driver_answers(record: dict[str, Any], timeout: float = 2.0) -> bool:
    """chromedriver answers on its recorded URL (only a chromedriver speaks /status there)."""
    url = str(record.get("executor_url") or "")
    if not url.startswith(("http://localhost:", "http://127.0.0.1:")):
        return False
    try:
        with _http.urlopen(f"{url}/status", timeout=timeout) as answer:  # noqa: S310 - loopback only
            return answer.status == 200
    except Exception:
        return False


def retire_is_stale(record: dict[str, Any], now: float | None = None) -> bool:
    started = record.get("retiring_at")
    return not isinstance(started, (int, float)) or (now or time.time()) - float(started) > _RETIRE_STALE_SECONDS


def reapable(record: dict[str, Any], now: float | None = None) -> bool:
    """Retire now: past the TTL and nobody holds it live, or its browser is gone for good."""
    now = time.time() if now is None else now
    state = record.get("state")
    if state == "retiring":
        return retire_is_stale(record, now)
    if state == "live" and owner_alive(record):
        return False
    if not (driver_alive(record) or chrome_alive(record)):
        return True
    return not parking.is_fresh(record, now)


# ---------------------------------------------------------------- a driver that outlives its server

class _RecordedProcess:
    """A chromedriver process known by pid and start stamp, not by a Popen handle."""

    def __init__(self, pid: Any, stamp: Any):
        self.pid = pid if isinstance(pid, int) else None
        self.stamp = stamp

    def poll(self) -> int | None:
        return None if process_probe.same_process(self.pid, self.stamp) else 0

    def kill(self) -> None:
        if self.pid and process_probe.same_process(self.pid, self.stamp):
            _scripts.kill_process_tree(self.pid)


def shutdown_driver(url: str, process: _RecordedProcess, wait_seconds: float = 10.0) -> None:
    """Ask chromedriver to exit; kill it (by its proven pid) if it does not."""
    try:
        with _http.urlopen(f"{url}/shutdown", timeout=5):  # noqa: S310 - loopback only
            pass
    except Exception:
        pass
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.2)
    process.kill()


class DetachedService:
    """Stands in for Selenium's Service: the chromedriver an earlier server started."""

    def __init__(self, record: dict[str, Any]):
        self.service_url = str(record.get("executor_url") or "")
        self.path = ""
        self.log_output = None
        self.process = _RecordedProcess(record.get("driver_pid"), record.get("driver_started"))
        self.chrome_pid = record.get("chrome_pid")
        self.chrome_started = record.get("chrome_started")
        self.parked = False

    def stop(self) -> None:
        if not self.parked:
            shutdown_driver(self.service_url, self.process)


class ResumedChrome(webdriver.Chrome):
    """The WebDriver session of a parked browser, continued from a new process.

    Built without starting chromedriver or a new session: the recorded session id
    and capabilities are adopted as they are, and every command goes to the
    recorded chromedriver URL, so ``execute_cdp_cmd``, logs and ``quit`` behave
    exactly as on the original driver.
    """

    def __init__(self, record: dict[str, Any]):  # noqa: D107 - see class docstring
        self._resume = (str(record["webdriver_session_id"]), dict(record.get("capabilities") or {}))
        self.service = DetachedService(record)
        self.options = Options()
        executor = ChromiumRemoteConnection(
            remote_server_addr=self.service.service_url, browser_name="chrome", vendor_prefix="goog",
            keep_alive=True, ignore_proxy=True,
        )
        RemoteWebDriver.__init__(self, command_executor=executor, options=self.options)
        self._is_remote = False

    def start_session(self, capabilities: dict) -> None:  # noqa: ARG002 - the session already exists
        self.session_id, self.caps = self._resume


def describe_driver(driver: Any) -> dict[str, Any]:
    """What a later process needs to reach this driver's browser again, and to end it."""
    service = driver.service
    caps = dict(getattr(driver, "caps", None) or {})
    if isinstance(service, DetachedService):
        driver_pid, driver_started = service.process.pid, service.process.stamp
        chrome_pid, chrome_started = service.chrome_pid, service.chrome_started
    else:
        driver_pid = getattr(getattr(service, "process", None), "pid", None)
        driver_started = process_probe.started_at(driver_pid)
        chrome_pid = next((pid for pid, name in process_probe.children(driver_pid)
                           if "chrome" in name.lower() and "driver" not in name.lower()), None)
        chrome_started = process_probe.started_at(chrome_pid)
    try:
        handle = str(driver.current_window_handle)
    except Exception:
        handle = None
    return {
        "executor_url": str(service.service_url), "webdriver_session_id": str(driver.session_id),
        "capabilities": caps, "driver_pid": driver_pid, "driver_started": driver_started,
        "chrome_pid": chrome_pid, "chrome_started": chrome_started,
        "profile_dir": str((caps.get("chrome") or {}).get("userDataDir") or "") or None,
        "debugger_address": (caps.get("goog:chromeOptions") or {}).get("debuggerAddress"),
        "window_handle": handle,
    }


def let_go(driver: Any) -> None:
    """Make sure nothing in this process stops the driver: not quit, not Service.__del__."""
    service = getattr(driver, "service", None)
    if isinstance(service, DetachedService):
        service.parked = True
    elif service is not None:
        service.process = None


def outlives_client() -> str | None:
    """Why a browser launched now would die with the MCP client, or None when it will not."""
    if process_probe.job_breakaway() == "forbidden":
        return ("This MCP server runs inside a Windows job object that ends every process it "
                "started when the MCP client exits and lets none of them leave it, so the parked "
                "browser ends with the client. It survives a server restart under the same client "
                "and a client that does not use such a job (the Python MCP SDK's stdio client does).")
    return None


# ---------------------------------------------------------------- retiring

def retire(record: dict[str, Any]) -> dict[str, Any]:
    """End a parked browser for good; what was stopped and removed. Never raises.

    The polite way first - the WebDriver session is quit, so chromedriver closes
    Chrome and deletes its own profile, then chromedriver is asked to exit -
    and then whatever of the recorded tree is provably still running is killed
    and the ``scoped_dir*`` profile removed.
    """
    problems: list[str] = []
    how = "already_gone"
    # A chromedriver provably gone is not asked (a refused loopback connect costs
    # seconds on Windows); one whose start stamp was never read is asked over HTTP.
    if (driver_alive(record) or not record.get("driver_started")) and driver_answers(record):
        try:
            ResumedChrome(record).quit()
            how = "quit"
        except Exception as exc:
            problems.append(f"quitting the parked browser failed ({type(exc).__name__}: {exc})")
    deadline = time.monotonic() + 5.0
    while chrome_alive(record) and time.monotonic() < deadline:
        time.sleep(0.2)
    for label, alive, pid in (("Chrome", chrome_alive, record.get("chrome_pid")),
                              ("chromedriver", driver_alive, record.get("driver_pid"))):
        if alive(record):
            how = "killed"
            problem = _scripts.kill_process_tree(int(pid))
            if problem:
                problems.append(f"{label} {pid}: {problem}")
    left = None
    # A killed Chrome's helpers can hold profile files for seconds after the main pid is gone.
    profile_deadline = time.monotonic() + PROFILE_REMOVAL_SECONDS
    while record.get("profile_dir"):
        left = _scripts.remove_scoped_profile(str(record["profile_dir"]),
                                              str(record.get("profile_mode") or "temporary"))
        if not left or time.monotonic() >= profile_deadline:
            break
        time.sleep(0.5)
    if left:
        problems.append(f"its temporary profile could not be removed: {left}")
    stopped = not (chrome_alive(record) or driver_alive(record))
    if not stopped:
        problems.append("the parked browser is still running")
    answer = {"session_id": record.get("session_id"), "profile_mode": record.get("profile_mode"),
              "browser_stopped": stopped, "profile_removed": not left, "how": how,
              "problem": "; ".join(problems) or None}
    if problems:
        logger.warning("Retiring parked browser '%s': %s", record.get("session_id"), answer["problem"])
    return answer


def retire_and_forget(session_id: str, *, force: bool = False) -> dict[str, Any] | None:
    """Claim ``session_id`` for retirement (unless someone holds it live) and retire it."""
    token = new_token()

    def take(record: dict[str, Any]) -> dict[str, Any] | None:
        if not force and not reapable(record):
            return None
        if record.get("state") == "live" and owner_alive(record) and record.get("owner_pid") != os.getpid():
            return None  # another server drives it right now: never touched
        if record.get("state") == "retiring" and not retire_is_stale(record):
            return None
        return {"state": "retiring", "token": token, "retiring_at": time.time()}

    claimed = claim(session_id, take)
    if claimed is None:
        return None
    answer = retire(claimed)
    forget(session_id, token)
    return answer


def reap_expired() -> list[dict[str, Any]]:
    """Retire every record whose TTL ran out while parked or whose browser is gone."""
    return [answer for record in records()
            if reapable(record) and (answer := retire_and_forget(str(record["session_id"])))]


# ---------------------------------------------------------------- the watchdog

def spawn_watchdog(session_id: str, token: str) -> dict[str, Any]:
    """Start the detached process that enforces the TTL of this record; its pid and stamp."""
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        child = process_probe.popen_detached(
            [sys.executable, "-m", "web_search_neo.owned_parking", "--watch", session_id, token],
            windows=os.name == "nt", windows_flags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, cwd=root, close_fds=True,
        )
    except OSError as exc:
        logger.warning("Could not start the watchdog of parked browser '%s': %s", session_id, exc)
        return {}
    return {"watchdog_pid": child.pid, "watchdog_started": process_probe.started_at(child.pid)}


def watch(session_id: str, token: str, poll_seconds: float = POLL_SECONDS) -> int:
    """Watch one record until it is retired, handed on (new token) or dropped.

    A failed look (the registry locked by a busy writer, a file mid-replace) is
    retried on the next poll: the watchdog is the one thing that enforces the TTL
    when no server runs, so it must not end on a transient error.
    """
    while True:
        try:
            outcome = _watch_once(session_id, token, poll_seconds)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("Watchdog of parked browser '%s': %s", session_id, exc)
            outcome = poll_seconds
        if outcome is None:
            return 0
        time.sleep(outcome)


def _watch_once(session_id: str, token: str, poll_seconds: float) -> float | None:
    """One look: None when the watch is over, else how long to sleep."""
    record = read(session_id)
    if record is None or record.get("token") != token:
        return None
    if record.get("state") == "live" and not owner_alive(record):
        # The server died without parking it: parked from now, TTL from now.
        claim(session_id, lambda current: {"state": "parked", "updated_at": time.time(),
                                           "parked_at": time.time()}
              if current.get("token") == token and current.get("state") == "live" else None)
        return 0.0
    if reapable(record):
        retire_and_forget(session_id)
        return None
    if record.get("state") != "parked":
        return poll_seconds
    left = float(record.get("updated_at") or 0) + parking.ttl_seconds() - time.time()
    return max(0.2, min(poll_seconds, left))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == 3 and arguments[0] == "--watch":
        return watch(arguments[1], arguments[2])
    print("usage: python -m web_search_neo.owned_parking --watch SESSION_ID TOKEN", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
