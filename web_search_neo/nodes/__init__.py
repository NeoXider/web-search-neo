"""Other computers running Web Search Neo, reachable from this one over the LAN.

A *node* is one machine's Web Search Neo - its own Chrome, its own companion,
its own sessions - published to the local network by ``wsn --node`` (or the
companion popup's "Share this computer" switch). Any MCP server that knows the
node's address and pairing key can then run ``web_action``/``web_info`` there
with ``node="<name>"``: the whole call executes on that machine and only the
answer travels back.

Nothing here touches a browser. The package is the transport and the
bookkeeping: who the nodes are (:mod:`.config`), what this machine looks like
from the network - LAN addresses, public IP, coarse geolocation
(:mod:`.identity`), how a request proves it knows the key (:mod:`.protocol`),
the HTTP server and its LAN discovery responder (:mod:`.server`), the client
side (:mod:`.client`), and the combined picture the ``nodes`` topic and the
popup show (:mod:`.overview`). The glue that hands a call to the facade lives
in ``web_search_neo/node_actions.py``.

The pairing key never crosses the wire: every request and every answer carries
an HMAC-SHA256 over its method, path, time, nonce and body digest, and a nonce
is accepted once. The traffic itself is plain HTTP, so page text and cookies a
call returns are readable on the LAN - use a trusted network or a VPN overlay.
"""
