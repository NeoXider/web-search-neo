# Other computers: LAN nodes

One MCP server can drive the Chrome of other computers on the same network. Each
computer runs a **node** — and does so by default: when the Chrome companion connects
to its bridge, the bridge starts the node unless the popup's **Share this computer**
switch was turned off (`wsn --node` runs one by hand). Any MCP server that knows a
node's address and pairing key sends whole calls there:

```json
{"tool": "web_action", "arguments": {"node": "office-pc", "actions": [
  {"action": "open", "url": "https://example.com", "session_id": "office-1",
   "profile_mode": "current", "agent_label": "me"},
  {"action": "screenshot", "session_id": "office-1", "path": "office.png"}
]}}
```

```json
{"tool": "web_info", "arguments": {"node": "office-pc", "topic": "page_text",
  "params": {"session_id": "office-1"}}}
```

`node` names a saved computer or is an address (`192.168.1.115`, `192.168.1.115:8766`).
Leave it out, or pass `local`, to stay on this machine. The call runs on that computer
exactly as it would locally — its Chrome and companion, its sessions and session cap,
its files and its network — and only the answer comes back, with a `via_node` block
naming where it ran. Images (the `screenshot` topic) travel as base64 and arrive as
images.

## See what is out there: `web_info(topic="nodes")`

```json
{"tool": "web_info", "arguments": {"topic": "nodes", "params": {"discover": true}}}
```

returns:

- `self` — this computer: `name`, `hostname`, `os`, every LAN address (`lan_ips`,
  `primary_ip`), and `public`: the public IP and the coarse location registries attach
  to it (`country`, `country_code`, `region`, `city`, `latitude`, `longitude`,
  `timezone`, `isp`), plus `place` as one readable line and the companion link state.
- `sharing` — whether a node runs on this computer and the addresses others use.
- `nodes` — every saved, configured or discovered computer with its `state`:
  `online` (paired and answering, with `latency_ms`, its IPs, location and browser
  link), `unpaired` (it answers, but the key is missing or different) or `offline`.

`discover=true` broadcasts one UDP probe on every local subnet and waits about a
second for answers; `refresh=true` re-reads the public IP instead of the cached answer.

The location is that of the IP block, city level at best, looked up from keyless
public services and cached for 15 minutes. It is never the device's own position.
`WEB_SEARCH_NEO_GEOIP=0` switches the lookup off.

## Pair two computers

On the computer to be controlled (say `192.168.1.115`) nothing has to be started:
with Chrome and its companion running, its node is up. Its popup shows the address and
the **pairing key** under **Computers → Share this computer**, with copy buttons; the
same switch turns sharing off (and back on), and the choice is remembered. From a
terminal, `wsn --node --key` prints the key and `wsn --node` runs a node by hand, which
prints the addresses it listens on and the key.

On the computer that controls it, save it once — from an agent:

```json
{"tool": "web_action", "arguments": {"actions": [{"action": "nodes", "op": "add",
  "name": "office-pc", "address": "192.168.1.115", "key": "1f2e-3d4c-..."}]}}
```

or in the popup (**Computers → Add a computer**, or **Scan** and then the key icon on a
discovered row). `op='ping'` checks one, `op='remove'` forgets it, `op='list'` lists them.

A computer saved without a key of its own is called with this machine's key
(`WEB_SEARCH_NEO_NODE_KEY`, or its `node-key` file). So giving every computer the
same key — set `WEB_SEARCH_NEO_NODE_KEY` everywhere — pairs them all at once, and a
computer found by **Scan** works immediately.

## From the popup

- **Overview** shows this computer's LAN address, public IP and place in one strip.
- **Computers** lists this computer (copyable LAN and public IP, a map link for the
  place, the network operator), the Share switch with the address and pairing key,
  and every other computer with a coloured state dot, its IP, place, latency and
  whether its Chrome is linked. An online row has **Send this tab**: the page you are
  looking at opens in that computer's Chrome and stays there. Forgetting a computer
  takes two clicks.

## Security model

- The pairing key never crosses the network. Every request carries an HMAC-SHA256
  over its method, path, time, a one-time nonce and the body digest; the node refuses
  a signature that does not match, a time more than two minutes off its own clock,
  and a nonce it has already seen. Every answer is signed over the request's nonce,
  so the caller knows it came from the key holder and belongs to its own request.
- The traffic is plain HTTP: page text, cookies and screenshots a call returns are
  readable by anyone on the same network segment. Share only on a network you trust,
  or over a VPN overlay.
- A node runs anything the two public tools can do, as the user it runs as —
  including `run_script`, `cookies` and `upload` of that computer's files — for whoever
  holds the key. Treat the key like a password. **New key** in the popup (or deleting
  the `node-key` file) revokes every pairing at once.
- `update_companion` is refused remotely by default; `WEB_SEARCH_NEO_NODE_DENY` is the
  comma-separated list of actions a node refuses (empty allows everything).
- A node does not relay: a call that names another node from inside a node is refused.
- `POST /wsn/stop` is accepted only from the node's own machine.

## Settings

| Variable | Effect |
| --- | --- |
| `WEB_SEARCH_NEO_NODE_SHARE` | `0` makes "not shared" the default; the popup switch still overrides it. |
| `WEB_SEARCH_NEO_NODE_AUTOSTART` | `0` stops the bridge from starting the node when the companion connects. |
| `WEB_SEARCH_NEO_NODE_PORT` | HTTP port a node listens on, default `8766`. |
| `WEB_SEARCH_NEO_NODE_HOST` | Interface a node binds, default `0.0.0.0` (all). |
| `WEB_SEARCH_NEO_NODE_DISCOVERY_PORT` | UDP port of the LAN discovery answer, default `8767`. |
| `WEB_SEARCH_NEO_NODE_NAME` | The name a node reports, default the host name. |
| `WEB_SEARCH_NEO_NODE_KEY` | The pairing key, instead of the per-user `node-key` file. |
| `WEB_SEARCH_NEO_NODES` | Extra nodes without saving them: `office=192.168.1.115,lab=10.0.0.5:8766`. |
| `WEB_SEARCH_NEO_NODE_DENY` | Actions a node refuses, default `update_companion`. |
| `WEB_SEARCH_NEO_NODE_TIMEOUT` | Seconds a forwarded call may take, default `600`. |
| `WEB_SEARCH_NEO_GEOIP` | `0` turns the public IP and location lookup off. |
| `WEB_SEARCH_NEO_NODE_DIR` | Where `node-key` and `nodes.json` live (default: the bridge token's folder). |

Windows asks once whether Python may accept connections on private networks; allow
it, or open TCP 8766 and UDP 8767 in the firewall yourself. `wsn --node --stop` ends a
node started from a terminal or by the popup.

## Take over

Separate from nodes, but the same question — who is driving this Chrome right now —
has a direct answer in the popup: **Take over** (or **Alt+Shift+P** anywhere in Chrome)
pauses every agent in this browser at once. Commands that would change a page or a tab
are refused with a message telling the agent to wait for the user; reads of state the
companion already holds keep working. The toolbar badge shows `II` while paused, and
**Resume** (or the same shortcut) hands control back. A browser restart ends the pause.
