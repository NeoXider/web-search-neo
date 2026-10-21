"""LAN nodes: addresses and keys, signed transport, discovery, location, and node= routing.

Everything runs on loopback with throwaway keys in a temporary state directory;
the public-IP lookup is either faked or switched off, so no test leaves the machine.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time

import pytest
from mcp.server.fastmcp import Image

from web_search_neo import main, node_actions, node_link
from web_search_neo.nodes import client, config, firewall, identity, overview, pairing, protocol
from web_search_neo.nodes.server import NodeServer


@pytest.fixture(autouse=True)
def node_home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DIR_ENV, str(tmp_path))
    monkeypatch.setenv(identity.GEOIP_ENV, "0")
    for name in (config.KEY_ENV, config.NODES_ENV, config.NAME_ENV, node_actions.DENY_ENV,
                 config.SHARE_ENV):
        monkeypatch.delenv(name, raising=False)
    # Reading the real Windows Firewall takes seconds; the tests that need it fake it.
    monkeypatch.setattr(firewall, "supported", lambda: False)
    # A real node may be running on this machine's 8766: point "this machine's node" elsewhere.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        monkeypatch.setenv(config.PORT_ENV, str(probe.getsockname()[1]))
    return tmp_path


def _server(call=None, key=None, info=None) -> NodeServer:
    server = NodeServer(
        call=call or (lambda tool, arguments, caller: {"tool": tool, "arguments": arguments,
                                                       "caller": caller}),
        info=info or (lambda: {"name": "fake", "browser": {"companion_connected": False}}),
        key=key or config.load_or_create_key(), name="fake", version="9.9.9",
        host="127.0.0.1", port=0, discovery_port=0,
    )
    server.start()
    return server


# --- addresses, names and keys ------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("192.168.1.115", "192.168.1.115:8766"),
    ("http://192.168.1.115/", "192.168.1.115:8766"),
    ("192.168.1.115:9000", "192.168.1.115:9000"),
    ("Office-PC", "office-pc:8766"),
    ("[fe80::1]:8800", "[fe80::1]:8800"),
])
def test_addresses_normalise_to_host_and_port(raw, expected):
    assert config.normalize_address(raw) == expected


@pytest.mark.parametrize("raw", ["", "bad host", "1.2.3.4:99999", "a:b:c", "http://"])
def test_bad_addresses_are_refused(raw):
    with pytest.raises(config.NodeConfigError):
        config.normalize_address(raw)


def test_keys_accept_the_grouped_form_people_copy():
    key = config.load_or_create_key()
    shown = config.display_key(key)
    assert "-" in shown and config.normalize_key(shown.upper()) == key
    assert config.load_or_create_key() == key, "a second read must not mint a new key"
    with pytest.raises(config.NodeConfigError):
        config.normalize_key("not-a-key")


def test_saved_nodes_round_trip_and_keep_their_key_on_readdress(node_home):
    key = "ab" * 16
    config.add_node("office", "192.168.1.115", key)
    assert [entry.name for entry in config.saved_nodes()] == ["office"]
    moved = config.add_node("Office", "192.168.1.116")
    assert moved.address == "192.168.1.116:8766" and moved.key == key
    assert "ab" * 16 not in json.dumps(moved.public()), "public() must never carry the key"
    assert config.remove_node("OFFICE") is True
    assert config.remove_node("office") is False
    assert config.saved_nodes() == []


def test_resolve_names_env_nodes_and_bare_addresses(monkeypatch):
    monkeypatch.setenv(config.NODES_ENV, "lab=10.0.0.5:9001; broken=; =x")
    assert config.resolve("LAB").address == "10.0.0.5:9001"
    direct = config.resolve("192.168.1.115")
    assert (direct.address, direct.source) == ("192.168.1.115:8766", "address")
    with pytest.raises(config.NodeConfigError, match="Known nodes"):
        config.resolve("no such node!")


@pytest.mark.parametrize("target", [None, "", "local", "self", "LOCALHOST", "127.0.0.1"])
def test_local_targets_never_leave_the_process(target):
    assert not node_actions.wants_remote(target)


def test_node_names_cannot_impersonate_this_machine():
    with pytest.raises(config.NodeConfigError):
        config.validate_name("local")


# --- signing --------------------------------------------------------------------------


def test_a_signed_request_verifies_once_and_only_unaltered():
    key, body = "cd" * 16, b'{"tool": "web_info"}'
    headers = protocol.sign_request(key, "POST", "/wsn/call", body)
    replay = protocol.ReplayCache()
    assert protocol.verify_request(key, "POST", "/wsn/call", headers, body, replay) is None
    assert "replayed" in protocol.verify_request(key, "POST", "/wsn/call", headers, body, replay)
    fresh = protocol.sign_request(key, "POST", "/wsn/call", body)
    assert "bad signature" in protocol.verify_request(key, "POST", "/wsn/call", fresh, body + b" ",
                                                      protocol.ReplayCache())
    assert "bad signature" in protocol.verify_request("ef" * 16, "POST", "/wsn/call", fresh, body,
                                                      protocol.ReplayCache())
    old = protocol.sign_request(key, "GET", "/wsn/info", b"", now=time.time() - 3600)
    assert "skew" in protocol.verify_request(key, "GET", "/wsn/info", old, b"", protocol.ReplayCache())
    assert protocol.verify_request(key, "GET", "/wsn/info", {}, b"", replay) == "unsigned request"


def test_answers_are_bound_to_the_request_nonce():
    signature = protocol.response_signature("12" * 16, "nonce1", 200, b"{}")
    assert protocol.verify_response("12" * 16, "nonce1", 200, b"{}", signature)
    assert not protocol.verify_response("12" * 16, "nonce2", 200, b"{}", signature)
    assert not protocol.verify_response("12" * 16, "nonce1", 200, b"{ }", signature)


# --- server and client over loopback ---------------------------------------------------


def test_hello_info_and_call_over_http():
    server = _server()
    try:
        entry = config.NodeEntry("fake", f"127.0.0.1:{server.port}", None, "address")
        assert client.hello(entry)["version"] == "9.9.9"
        assert client.info(entry)["name"] == "fake"
        answer = client.call(entry, "web_info", {"topic": "capabilities"})
        assert answer["tool"] == "web_info" and answer["arguments"] == {"topic": "capabilities"}
    finally:
        server.shutdown()


def test_a_wrong_key_is_refused_without_running_anything():
    calls = []
    server = _server(call=lambda *args: calls.append(args))
    try:
        stranger = config.NodeEntry("fake", f"127.0.0.1:{server.port}", "0f" * 16, "saved")
        with pytest.raises(client.NodeError, match="pairing key differs"):
            client.call(stranger, "web_action", {"actions": [{"action": "open"}]})
        assert calls == []
    finally:
        server.shutdown()


def test_only_the_two_public_tools_can_be_called():
    server = _server()
    try:
        entry = config.NodeEntry("fake", f"127.0.0.1:{server.port}", None, "address")
        with pytest.raises(client.NodeError, match="tool must be one of"):
            client.call(entry, "browser_run_script", {})
    finally:
        server.shutdown()


def test_an_offline_node_says_where_it_looked():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    entry = config.NodeEntry("gone", f"127.0.0.1:{port}", "ab" * 16, "saved")
    with pytest.raises(client.NodeError, match="unreachable"):
        client.call(entry, "web_info", {}, timeout=2)
    row = overview.probe(entry, timeout=1)
    assert row["state"] == "offline" and row["online"] is False


def test_a_node_stops_only_for_its_own_machine():
    server = _server()
    assert client.stop_local(server.port) is True
    deadline = time.monotonic() + 3
    while not server.stopped and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.stopped


def test_discovery_finds_a_node_by_its_udp_answer():
    server = _server()
    try:
        found = client.discover(["127.0.0.1"], timeout=1.0, port=server.discovery_port,
                                extra_targets=["127.0.0.1"])
        assert any(item["address"] == f"127.0.0.1:{server.port}" and item["self"] for item in found)
    finally:
        server.shutdown()


def test_probe_tells_unpaired_from_online():
    server = _server(key="aa" * 16)
    try:
        address = f"127.0.0.1:{server.port}"
        assert overview.probe(config.NodeEntry("x", address, "aa" * 16, "saved"))["state"] == "online"
        unpaired = overview.probe(config.NodeEntry("x", address, "bb" * 16, "saved"))
        assert unpaired["state"] == "unpaired" and unpaired["version"] == "9.9.9"
    finally:
        server.shutdown()


# --- public IP and location -------------------------------------------------------------


def test_public_lookup_falls_back_and_normalises(monkeypatch):
    monkeypatch.setenv(identity.GEOIP_ENV, "1")
    seen = []

    def fake(url):
        seen.append(url)
        if "ipwho.is" in url:
            raise OSError("down")
        return {"ip": "203.0.113.9", "country_name": "Exampleland", "country_code": "ru",
                "city": "Yekaterinburg", "latitude": "56.8389", "longitude": 60.6057,
                "org": "Example ISP"}

    lookup = identity.PublicLookup(fetch=fake)
    first = lookup.get()
    assert first["ip"] == "203.0.113.9" and first["source"] == "ipapi.co"
    assert first["latitude"] == 56.8389 and first["flag"] == "\U0001F1F7\U0001F1FA"
    assert lookup.get() == first and len(seen) == 2, "the answer must be cached"
    assert identity.place(first) == "Yekaterinburg, Exampleland"


def test_public_lookup_reports_failure_and_can_be_switched_off(monkeypatch):
    monkeypatch.setenv(identity.GEOIP_ENV, "1")
    failing = identity.PublicLookup(fetch=lambda url: (_ for _ in ()).throw(OSError("offline")))
    assert failing.get()["error"] == "public IP lookup failed"
    monkeypatch.setenv(identity.GEOIP_ENV, "0")
    assert identity.PublicLookup(fetch=lambda url: {}).get() == {"disabled": True,
                                                                 "reason": "WEB_SEARCH_NEO_GEOIP=0"}


def test_local_identity_lists_lan_addresses():
    me = identity.local_identity("1.0")
    assert me["name"] and me["version"] == "1.0" and me["public"]["disabled"] is True
    assert all(not address.startswith("127.") for address in me["lan_ips"])


# --- node= routing through the real facade -----------------------------------------------


@pytest.fixture
def facade_node(monkeypatch):
    key = config.load_or_create_key()
    monkeypatch.setenv(config.KEY_ENV, key)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = _server(call=node_actions.node_call(loop), key=key, info=node_actions.local_status)
    yield f"127.0.0.1:{server.port}"
    server.shutdown()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def test_web_info_runs_on_the_named_node(facade_node):
    result = asyncio.run(main.web_info(topic="capabilities", node=facade_node))
    assert result["server"] == "Web Search Neo"
    assert result["via_node"]["address"] == facade_node


def test_web_action_runs_on_the_named_node(facade_node):
    result = asyncio.run(main.web_action(actions=[{"action": "nodes", "op": "list"}],
                                         node=facade_node))
    assert result["via_node"]["address"] == facade_node
    assert result["results"][0]["success"] is True


def test_denied_actions_are_refused_on_the_node(facade_node):
    with pytest.raises(client.NodeError, match="disabled on this node"):
        asyncio.run(main.web_action(actions=[{"action": "update_companion"}], node=facade_node))


def test_a_node_does_not_relay_to_further_nodes():
    loop = asyncio.new_event_loop()
    call = node_actions.node_call(loop)
    with pytest.raises(ValueError, match="does not relay"):
        call("web_info", {"topic": "capabilities", "node": "elsewhere"}, "test")
    with pytest.raises(ValueError, match="unknown argument"):
        call("web_info", {"topic": "capabilities", "extra": 1}, "test")
    loop.close()


def test_images_survive_the_trip_as_base64():
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    encoded = node_actions.encode([Image(data=png, format="png"), "{}"])
    assert json.loads(json.dumps(encoded))[0][node_actions.IMAGE_KEY]["mime_type"] == "image/png"
    decoded = node_actions.decode(encoded)
    assert isinstance(decoded[0], Image) and decoded[0].data == png and decoded[1] == "{}"


def test_the_nodes_topic_and_action_are_published():
    capabilities = main._capabilities()
    assert "nodes" in capabilities["info_topics"] and "nodes" in capabilities["actions"]
    schema = main._capabilities("nodes")
    assert set(schema["params_schema"]["properties"]) == {"discover", "refresh", "timeout_seconds"}
    assert set(schema["action_form"]["input_schema"]["properties"]) >= {"op", "name", "address", "key"}
    tools = {tool.name: tool.parameters for tool in main.mcp._tool_manager.list_tools()}
    for name in ("web_action", "web_info"):
        assert "node" in tools[name]["properties"], f"{name} must accept node="


def test_the_nodes_action_saves_pings_and_forgets(facade_node):
    saved = asyncio.run(node_actions.browser_node_manage(op="add", name="loop", address=facade_node))
    assert saved["success"] and saved["status"]["state"] == "online"
    listed = asyncio.run(node_actions.browser_node_manage(op="list"))
    assert [row["name"] for row in listed["nodes"]] == ["loop"]
    assert asyncio.run(node_actions.browser_node_manage(op="ping", name="loop"))["success"]
    assert asyncio.run(node_actions.browser_node_manage(op="remove", name="loop"))["success"]


# --- the popup's channel through the daemon ------------------------------------------------


def _popup(message: dict) -> dict:
    answers: list[str] = []
    done = threading.Event()

    def send(text: str) -> None:
        answers.append(text)
        done.set()

    node_link.handle(send, {"id": 7, **message}, "9.9.9")
    assert done.wait(15), "the daemon never answered the popup"
    return json.loads(answers[0])


def test_popup_overview_add_and_remove(facade_node):
    added = _popup({"op": "add", "name": "desk", "address": facade_node})
    assert added["type"] == "nodes_result" and added["id"] == 7
    assert added["result"]["status"]["state"] == "online"
    seen = _popup({"op": "overview"})["result"]
    assert [row["name"] for row in seen["nodes"]] == ["desk"]
    # Shared by default, so the popup shows the key a person pairs with...
    assert seen["sharing"]["enabled"] is True and "pair_key" in seen["sharing"]
    config.set_share(False)
    # ...and only while this computer is shared.
    assert "pair_key" not in _popup({"op": "overview"})["result"]["sharing"]
    assert _popup({"op": "remove", "name": "desk"})["result"] == {"removed": True}


def test_sharing_is_on_by_default_and_the_popup_choice_wins(monkeypatch):
    assert config.share_enabled() is True
    monkeypatch.setenv(config.SHARE_ENV, "0")
    assert config.share_enabled() is False, "the environment can make off the default"
    config.set_share(True)
    assert config.share_enabled() is True, "a stored popup choice wins over the default"
    config.set_share(False)
    monkeypatch.delenv(config.SHARE_ENV)
    assert config.share_enabled() is False


def test_the_daemon_starts_the_shared_node_once_per_cooldown(monkeypatch):
    started = []
    monkeypatch.setattr(node_link, "start_node_process", lambda: started.append(1))
    monkeypatch.setattr(node_link, "_sharing", lambda include_key=True: {"running": False})
    monkeypatch.setattr(node_link, "_last_autostart", 0.0)
    monkeypatch.setenv(node_link.AUTOSTART_ENV, "0")
    node_link.ensure_shared_node()
    assert started == [], "WEB_SEARCH_NEO_NODE_AUTOSTART=0 leaves it to a person"
    monkeypatch.setenv(node_link.AUTOSTART_ENV, "1")
    node_link.ensure_shared_node()
    node_link.ensure_shared_node()
    assert started == [1], "a node that did not come up is not respawned on every reconnect"
    config.set_share(False)
    monkeypatch.setattr(node_link, "_last_autostart", 0.0)
    node_link.ensure_shared_node()
    assert started == [1], "switched off in the popup: nothing starts"


def test_popup_errors_come_back_as_text():
    answer = _popup({"op": "send_tab", "node": "x", "url": "file:///etc/passwd"})
    assert "http(s)" in answer["error"]
    assert "Unknown nodes operation" in _popup({"op": "explode"})["error"]


# --- button pairing --------------------------------------------------------------------


def _paired_server(notices=None) -> NodeServer:
    server = _server(key="cd" * 16)
    server.pairing.notify = (notices.append if notices is not None else None)
    return server


def test_button_pairing_hands_the_key_over_once_after_allow():
    notices = []
    server = _paired_server(notices)
    try:
        started = pairing.begin(f"127.0.0.1:{server.port}")
        assert started["name"] == "fake" and len(started["code"]) == 7
        # The person at B sees the code A shows - in the notification and in the popup.
        assert [notice["code"] for notice in notices] == [started["code"]]
        waiting = server.pairing.waiting()
        assert [(item["code"], item["address"]) for item in waiting] == [(started["code"], "127.0.0.1")]
        assert pairing.check(started["attempt"])["state"] == "waiting"
        assert config.saved_nodes() == [], "nothing is saved before Allow"
        server.pairing.decide(waiting[0]["request"], True)
        done = pairing.check(started["attempt"])
        assert done["state"] == "paired" and done["saved"]["has_own_key"] is True
        saved = config.resolve(done["saved"]["name"])
        assert saved.key == "cd" * 16 and overview.probe(saved)["state"] == "online"
        assert pairing.check(started["attempt"])["state"] == "expired", "handed over once"
        assert server.pairing.waiting() == []
    finally:
        server.shutdown()


def test_a_denied_pairing_shares_nothing():
    server = _paired_server()
    try:
        started = pairing.begin(f"127.0.0.1:{server.port}")
        server.pairing.decide(server.pairing.waiting()[0]["request"], False)
        assert pairing.check(started["attempt"])["state"] == "denied"
        assert config.saved_nodes() == []
    finally:
        server.shutdown()


def test_pairing_steps_refuse_strangers_and_floods():
    desk = pairing.PairingDesk("b", lambda: "ab" * 16)
    _private, public = pairing.new_secret()
    commit = pairing.commitment(public)
    started = desk.start({"name": "a", "commit": commit}, "10.0.0.1")
    with pytest.raises(pairing.PairingError, match="every few seconds"):
        desk.start({"name": "a", "commit": commit}, "10.0.0.1")
    with pytest.raises(pairing.PairingError, match="no such pairing request"):
        desk.reveal({"request": started["request"], "pub": format(public, "x")}, "10.0.0.2")
    _other_private, other_public = pairing.new_secret()
    with pytest.raises(pairing.PairingError, match="commitment"):
        desk.reveal({"request": started["request"], "pub": format(other_public, "x")}, "10.0.0.1")
    assert desk.poll({"request": started["request"], "proof": "x"}, "10.0.0.1") == {"state": "expired"}
    with pytest.raises(pairing.PairingError, match="out of range"):
        pairing.peer_value("1")
    for index in range(pairing.MAX_WAITING):
        desk.start({"name": "a", "commit": commit}, f"10.0.1.{index}")
    with pytest.raises(pairing.PairingError, match="too many"):
        desk.start({"name": "a", "commit": commit}, "10.0.2.1")


def test_the_answer_needs_the_shared_secret_and_the_seal_resists_tampering():
    desk = pairing.PairingDesk("b", lambda: "ef" * 16)
    private, public = pairing.new_secret()
    started = desk.start({"name": "a", "commit": pairing.commitment(public)}, "10.0.0.1")
    desk.reveal({"request": started["request"], "pub": format(public, "x")}, "10.0.0.1")
    desk.decide(desk.waiting()[0]["request"], True)
    with pytest.raises(pairing.PairingError, match="bad proof"):
        desk.poll({"request": started["request"], "proof": "00" * 32}, "10.0.0.1")
    b_public = pairing.peer_value(started["pub"])
    session = pairing.derive(private, b_public, request=started["request"], a_name="a",
                             b_name="b", a_public=public, b_public=b_public)
    answer = desk.poll({"request": started["request"], "proof": session.proof(started["request"])},
                       "10.0.0.1")
    assert answer["state"] == "allowed" and "ef" * 16 not in json.dumps(answer)
    assert session.unseal(answer) == "ef" * 16
    tampered = {**answer, "sealed": ("00" if answer["sealed"][:2] != "00" else "11") + answer["sealed"][2:]}
    with pytest.raises(pairing.PairingError, match="does not belong"):
        session.unseal(tampered)


def test_the_popup_lists_and_answers_requests_only_with_this_machines_key(monkeypatch):
    server = _server()  # this machine's own key, as the shared node runs with
    monkeypatch.setenv(config.PORT_ENV, str(server.port))
    try:
        pairing.begin(f"127.0.0.1:{server.port}")
        listed = client.local_call("GET", "/wsn/pair/waiting")
        assert len(listed) == 1 and listed[0]["name"] == config.node_name()
        stranger = config.NodeEntry("x", f"127.0.0.1:{server.port}", "0f" * 16, "saved")
        with pytest.raises(client.NodeError, match="pairing key differs"):
            client._exchange(stranger, "POST", "/wsn/pair/decide",
                             {"request": listed[0]["request"], "allow": True}, 3.0)
        decided = _popup({"op": "pair_decide", "request": listed[0]["request"], "allow": True})
        assert decided["result"]["decided"] == "allowed"
    finally:
        server.shutdown()


def test_an_older_node_says_how_to_pair_instead(monkeypatch):
    monkeypatch.setattr(client, "open_post", lambda *args, **kwargs: {"ok": False, "error": "not found"})
    with pytest.raises(pairing.PairingError, match="older Web Search Neo"):
        pairing.begin("192.0.2.10")


def test_the_daemon_follows_a_pairing_the_popup_may_close_on():
    server = _paired_server()
    try:
        started = _popup({"op": "pair", "address": f"127.0.0.1:{server.port}"})["result"]
        assert started["state"] == "waiting" and started["code"]
        server.pairing.decide(server.pairing.waiting()[0]["request"], True)
        deadline = time.monotonic() + 10
        answer = {}
        while time.monotonic() < deadline:
            answer = _popup({"op": "pair_poll", "attempt": started["attempt"]})["result"]
            if answer.get("state") != "waiting":
                break
            time.sleep(0.3)
        assert answer["state"] == "paired" and answer["saved"]["name"] == "fake"
        assert config.resolve("fake").key == "cd" * 16
    finally:
        server.shutdown()


def test_an_agent_pairs_in_two_calls(monkeypatch):
    monkeypatch.setattr(node_actions, "PAIR_WAIT_SECONDS", 5.0)
    server = _paired_server()
    try:
        address = f"127.0.0.1:{server.port}"
        first = asyncio.run(node_actions.browser_node_manage(op="pair", address=address))
        assert first["success"] and first["code"] in first["next"]
        server.pairing.decide(server.pairing.waiting()[0]["request"], True)
        second = asyncio.run(node_actions.browser_node_manage(op="pair", address=address))
        assert second["success"] and second["state"] == "paired"
        assert second["status"]["state"] == "online"
    finally:
        server.shutdown()


# --- Windows Firewall ---------------------------------------------------------------------


@pytest.mark.parametrize("facts, state", [
    ({"category": "Public", "enabled": ["Domain", "Private", "Public"], "allow": ["Private"]}, "blocked"),
    ({"category": "Public", "enabled": ["Public"], "allow": ["Private, Public"]}, "open"),
    ({"category": "Public", "enabled": ["Public"], "named": ["Any"]}, "open"),
    ({"category": "Public", "enabled": ["Public"], "named": ["Any"], "block": ["Public"]}, "blocked"),
    ({"category": "Private", "enabled": ["Public"]}, "open"),
    ({"category": "DomainAuthenticated", "enabled": ["Domain"], "allow": ["Domain"]}, "open"),
])
def test_the_firewall_verdict_follows_the_lan_profile(facts, state):
    assert firewall.judge(facts)["state"] == state


def test_opening_the_firewall_goes_through_windows_own_prompt(monkeypatch):
    scripts = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_powershell(script, timeout):
        scripts.append(script)
        return Done()

    monkeypatch.setenv(config.PORT_ENV, "8766")
    monkeypatch.setattr(firewall, "supported", lambda: True)
    monkeypatch.setattr(firewall, "_powershell", fake_powershell)
    monkeypatch.setattr(firewall, "_read", lambda lan_ip: {"supported": True, "state": "open",
                                                           "program": r"C:\Py\python.exe"})
    assert firewall.open_ports("192.168.1.115")["state"] == "open"
    launcher = scripts[0]
    assert "-Verb RunAs" in launcher, "only Windows' own elevation prompt can grant the change"
    inner = base64.b64decode(launcher.split("'-EncodedCommand','")[1].split("'")[0]).decode("utf-16-le")
    assert "-Protocol TCP -LocalPort 8766 -RemoteAddress LocalSubnet" in inner
    assert "-Protocol UDP -LocalPort 8767 -RemoteAddress LocalSubnet" in inner
    assert "-Program 'C:\\Py\\python.exe'" in inner and "Disable-NetFirewallRule" in inner

    class Declined:
        returncode, stdout = 1, ""
        stderr = "Start-Process : The operation was canceled by the user."

    monkeypatch.setattr(firewall, "_powershell", lambda script, timeout: Declined())
    with pytest.raises(RuntimeError, match="declined; nothing changed"):
        firewall.open_ports("192.168.1.115")
