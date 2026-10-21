"""The command-line modes that are not the stdio MCP server.

``--bridge`` runs the daemon behind the companion port, from the same file every
MCP config already points at; it speaks no MCP and touches no stdio, and an
agent's browser calls reach it over the loopback bridge instead. ``--bridge
--stop`` asks a running daemon to exit. ``--node`` publishes this machine's
``web_action``/``web_info`` to the LAN for other computers (``--node --stop``
ends it, ``--node --key`` prints the pairing key).
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


def run(arguments: list[str], version: str) -> int | None:
    """The exit code of a non-MCP mode, or None when the arguments start the MCP server.

    The bridge daemon publishes no MCP surface and never loads plugins; the node
    loads them itself, because it answers with the same actions the server would.
    """
    if "--bridge" in arguments:
        if "--stop" in arguments:
            return stop_bridge_daemon()
        return bridge_daemon.run_forever(version)
    if "--node" in arguments:
        from web_search_neo import node_actions  # imports the facade's browser stack

        return node_actions.serve(arguments)
    return None
