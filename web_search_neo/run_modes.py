"""The command-line modes that are not the stdio MCP server.

``--bridge`` runs the daemon behind the companion port, from the same file every
MCP config already points at; it speaks no MCP and touches no stdio, and an
agent's browser calls reach it over the loopback bridge instead. ``--bridge
--stop`` asks a running daemon to exit. ``--node`` publishes this machine's
``web_action``/``web_info`` to the LAN for other computers (``--node --stop``
ends it, ``--node --key`` prints the pairing key). ``--update`` pulls this
checkout and puts the daemon, the node and the Chrome companion on the new code.
"""

from __future__ import annotations

from web_search_neo import bridge_daemon, chrome_bridge


def stop_bridge_daemon() -> int:
    """Ask a running daemon to exit; the answer to "why is my edit not live?"."""
    # Not the shared instance: stopping a daemon must never start one first.
    bridge = chrome_bridge.ChromeBridge(spawn=False)
    try:
        if bridge.stop_daemon("stopped from the command line"):
            print("The bridge daemon was asked to stop.")
            return 0
        problem = getattr(bridge, "stop_problem", None)
        if problem:  # a listener that is not (or not yet) our daemon: say so
            print(f"Could not stop the bridge daemon: {problem}.")
            return 1
        print(f"No bridge daemon is listening on {bridge.host}:{bridge.port}.")
        return 0
    finally:
        bridge.shutdown()


def update_here(arguments: list[str]) -> int:
    """``--update``: pull this checkout, then move every long-running part onto it.

    A pull only changes files: the bridge daemon, this computer's LAN node and the
    Chrome companion keep running what they loaded. So this stops the node and the
    daemon, starts a fresh daemon, and reloads the companion onto the files; the
    daemon starts the node again when the companion is back (if sharing is on). An
    MCP server already running keeps its code until its client restarts it. Agents
    driving tabs here are not cut off unless ``--force`` says so.
    """
    from pathlib import Path

    from web_search_neo import companion_update
    from web_search_neo.chrome_bootstrap import _reload_companion, expected_extension_version
    from web_search_neo.nodes import client

    root = companion_update.package_checkout(Path(companion_update.__file__).with_name("__init__.py"))
    if root is None:
        print("Not a git checkout: nothing to pull; restarting onto the installed files.")
    else:
        pulled = companion_update.update_checkout(root)
        if pulled.get("updated"):
            outcome = (f"pulled {pulled.get('from_commit')} -> {pulled.get('to_commit')}, "
                       f"{pulled.get('files_changed')} file(s)")
        elif pulled.get("already_current"):
            outcome = f"already current ({pulled.get('commit')})"
        else:
            outcome = f"not pulled: {pulled.get('reason') or pulled.get('refused')}"
        print(f"Checkout {root}: {outcome}")
    current = chrome_bridge.ChromeBridge(spawn=False)
    try:
        claims = list(((current.status(2.0) or {}).get("daemon") or {}).get("claims") or [])
        if claims and "--force" not in arguments:
            print(f"{len(claims)} tab(s) are driven by agents right now; a restart would cut them "
                  "off. Run again when they are done, or add --force.")
            return 1
        print("LAN node: " + ("stopped" if client.stop_local() else "was not running"))
        print("Bridge daemon: " + ("stopped" if current.stop_daemon("update from the command line")
                                   else "was not running"))
    finally:
        current.shutdown()
    fresh = chrome_bridge.ChromeBridge()  # spawns a daemon from the files on disk
    try:
        fresh.start()
        if not fresh.wait_connected(25.0):
            print("A new bridge daemon runs, but Chrome's companion did not connect within 25 s; "
                  "open Chrome (the companion reconnects by itself).")
            return 1
        reload = _reload_companion(fresh, expected_extension_version())
        print("Chrome companion: " + ("reloaded" if reload.get("self_update") == "done"
                                      else f"not reloaded ({reload})"))
    finally:
        fresh.shutdown()
    print("Done. The node starts again in a few seconds while sharing is on.")
    return 0


def run(arguments: list[str], version: str) -> int | None:
    """The exit code of a non-MCP mode, or None when the arguments start the MCP server.

    The bridge daemon publishes no MCP surface and never loads plugins; the node
    loads them itself, because it answers with the same actions the server would.
    """
    if "--bridge" in arguments:
        if "--stop" in arguments:
            return stop_bridge_daemon()
        return bridge_daemon.run_forever(version)
    if "--update" in arguments:
        return update_here(arguments)
    if "--node" in arguments:
        from web_search_neo import node_actions  # imports the facade's browser stack

        return node_actions.serve(arguments)
    return None
