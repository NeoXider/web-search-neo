"""Windows Firewall: whether other computers can reach this node, and the fix in one click.

Windows asks once whether python.exe may accept connections. On a network marked
Public - the usual state of a home Ethernet link - the prompt's default leaves
the node unreachable, and the computer that wants to drive it sees nothing at
all. Reading the rules needs no rights, so :func:`state` reports what applies
to the profile of the LAN this computer uses. Changing them does need rights:
:func:`open_ports` starts an elevated PowerShell, Windows shows its own UAC
prompt, and nothing changes unless the person accepts it. The rules it adds
admit the node's TCP port and the discovery UDP port from the local subnet only.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import time
from typing import Any

from web_search_neo.nodes import config

RULE_PREFIX = "web-search-neo"
NODE_RULE = f"{RULE_PREFIX} node"
DISCOVERY_RULE = f"{RULE_PREFIX} discovery"
CACHE_SECONDS = 120.0
READ_TIMEOUT = 20.0
ELEVATED_TIMEOUT = 150.0  # the UAC prompt waits for a person
_CREATE_NO_WINDOW = 0x08000000
_PROFILE_OF_CATEGORY = {"public": "public", "private": "private", "domainauthenticated": "domain"}

_cache: dict[str, Any] = {}
_lock = threading.Lock()
_refreshing = threading.Event()


def supported() -> bool:
    return sys.platform == "win32"


def _quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _powershell(script: str, timeout: float) -> subprocess.CompletedProcess[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
    )


def _read_script(port: int, lan_ip: str) -> str:
    return f"""
$ErrorActionPreference = 'SilentlyContinue'
$listen = Get-NetTCPConnection -LocalPort {port} -State Listen | Select-Object -First 1
$exe = if ($listen) {{ (Get-Process -Id $listen.OwningProcess).Path }} else {{ '' }}
$index = (Get-NetIPAddress -IPAddress {_quote(lan_ip)}).InterfaceIndex
$category = if ($index) {{ "$((Get-NetConnectionProfile -InterfaceIndex $index).NetworkCategory)" }} else {{ '' }}
$inbound = {{ param($r) "$($r.Enabled)" -eq 'True' -and "$($r.Direction)" -eq 'Inbound' }}
$named = @(Get-NetFirewallRule -DisplayName '{RULE_PREFIX}*' | Where-Object {{ & $inbound $_ }})
$app = @()
if ($exe) {{ $app = @(Get-NetFirewallApplicationFilter -Program $exe | Get-NetFirewallRule |
  Where-Object {{ & $inbound $_ }}) }}
[pscustomobject]@{{
  program = "$exe"; category = $category
  named = @($named | Where-Object {{ "$($_.Action)" -eq 'Allow' }} | ForEach-Object {{ "$($_.Profile)" }})
  allow = @($app | Where-Object {{ "$($_.Action)" -eq 'Allow' }} | ForEach-Object {{ "$($_.Profile)" }})
  block = @($app | Where-Object {{ "$($_.Action)" -eq 'Block' }} | ForEach-Object {{ "$($_.Profile)" }})
  enabled = @(Get-NetFirewallProfile | Where-Object {{ "$($_.Enabled)" -eq 'True' }} |
    ForEach-Object {{ "$($_.Name)" }})
}} | ConvertTo-Json -Compress
"""


def _covers(profiles: list[str], profile: str) -> bool:
    for entry in profiles:
        words = {word.strip().lower() for word in str(entry).split(",")}
        if "any" in words or not profile or profile in words:
            return True
    return False


def judge(facts: dict[str, Any]) -> dict[str, Any]:
    """What the rules mean for a connection from the LAN to this node."""
    category = str(facts.get("category") or "")
    profile = _PROFILE_OF_CATEGORY.get(category.lower(), "")
    program = str(facts.get("program") or "")
    as_list = lambda key: [str(item) for item in (facts.get(key) or []) if item]  # noqa: E731
    enabled = {name.lower() for name in as_list("enabled")}
    verdict: dict[str, Any] = {"supported": True, "network": category or "unknown",
                               "program": program}
    if profile and profile not in enabled:
        return {**verdict, "state": "open", "why": f"the firewall is off for {category} networks"}
    if _covers(as_list("block"), profile):
        return {**verdict, "state": "blocked", "why": "a Windows Firewall rule blocks this Python"}
    if _covers(as_list("named"), profile):
        return {**verdict, "state": "open", "why": f"the {RULE_PREFIX} rules admit the LAN"}
    if _covers(as_list("allow"), profile):
        return {**verdict, "state": "open", "why": "this Python may accept connections"}
    where = f" for {category} networks" if category else ""
    return {**verdict, "state": "blocked", "why": f"Windows Firewall has no rule{where}"}


def _read(lan_ip: str) -> dict[str, Any]:
    try:
        done = _powershell(_read_script(config.listen_port(), lan_ip), READ_TIMEOUT)
        facts = json.loads(done.stdout.strip() or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"supported": True, "state": "unknown", "why": f"{type(exc).__name__}: {exc}"[:200]}
    return judge(facts if isinstance(facts, dict) else {})


def state(lan_ip: str, wait: bool = True, refresh: bool = False) -> dict[str, Any]:
    """The cached verdict for this machine; ``wait=False`` answers at once and reads later."""
    if not supported():
        return {"supported": False, "state": "unknown"}
    with _lock:
        cached = _cache.get(lan_ip)
    if cached and not refresh and time.monotonic() - cached["at"] < CACHE_SECONDS:
        return cached["verdict"]
    if not wait:
        if not _refreshing.is_set():
            _refreshing.set()
            threading.Thread(target=_refresh, args=(lan_ip,), name="wsn-firewall",
                             daemon=True).start()
        return cached["verdict"] if cached else {"supported": True, "state": "checking"}
    return _refresh(lan_ip)


def _refresh(lan_ip: str) -> dict[str, Any]:
    try:
        verdict = _read(lan_ip)
        with _lock:
            _cache[lan_ip] = {"at": time.monotonic(), "verdict": verdict}
        return verdict
    finally:
        _refreshing.clear()


def _open_script(port: int, discovery: int, program: str) -> str:
    unblock = ""
    if program:
        # Windows writes these when its first prompt is dismissed; they would beat any allow rule.
        unblock = (f"Get-NetFirewallApplicationFilter -Program {_quote(program)} -ErrorAction SilentlyContinue | "
                   "Get-NetFirewallRule | Where-Object { \"$($_.Direction)\" -eq 'Inbound' -and "
                   "\"$($_.Action)\" -eq 'Block' } | Disable-NetFirewallRule\n")
    return f"""
$ErrorActionPreference = 'Stop'
Remove-NetFirewallRule -DisplayName {_quote(NODE_RULE)}, {_quote(DISCOVERY_RULE)} -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName {_quote(NODE_RULE)} -Direction Inbound -Protocol TCP -LocalPort {port} -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
New-NetFirewallRule -DisplayName {_quote(DISCOVERY_RULE)} -Direction Inbound -Protocol UDP -LocalPort {discovery} -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
{unblock}"""


def open_ports(lan_ip: str) -> dict[str, Any]:
    """Admit the node from the LAN, through Windows' own elevation prompt."""
    if not supported():
        raise RuntimeError("Only Windows Firewall is managed here; open TCP "
                           f"{config.listen_port()} and UDP {config.discovery_port()} yourself.")
    program = str(state(lan_ip).get("program") or "")
    inner = base64.b64encode(_open_script(config.listen_port(), config.discovery_port(),
                                          program).encode("utf-16-le")).decode("ascii")
    launcher = (
        "$p = Start-Process -FilePath powershell -Verb RunAs -WindowStyle Hidden -Wait -PassThru "
        f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{inner}'; exit $p.ExitCode"
    )
    try:
        done = _powershell(launcher, ELEVATED_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Windows did not answer the permission prompt in time.") from None
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        reason = next((line for line in detail if line.strip()), f"exit code {done.returncode}")
        if "cancel" in reason.lower() or "отмен" in reason.lower():
            raise RuntimeError("The Windows permission prompt was declined; nothing changed.")
        raise RuntimeError(f"The firewall rules were not added: {reason[:200]}")
    return state(lan_ip, refresh=True)
