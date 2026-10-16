"""v1.28: a preflight's declared headers/methods get named, not just '*' or silence.

The hunt found undocumented routing headers on a hosted gateway only because a
CORS preflight publishes the accepted set; an audit has to hand that set to the
reader instead of leaving it hidden in raw headers. Pure analysis, no network.
"""
from web_search_neo.audit import active

ORIGIN = "https://console.test"


def ids(items):
    return {item["id"]: item for item in items}


def test_declared_custom_headers_are_named():
    declared = {"access-control-allow-origin": ORIGIN,
                "access-control-allow-headers": "Origin,Content-Type,X-Om-Channel,X-Api-Key",
                "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS"}
    got = ids(active.preflight_findings("https://api.test/v1/messages", ORIGIN, declared))
    named = got["active-cors-declared-access"]
    assert named["status"] == "info"
    assert named["evidence"]["headers"]["items"] == ["X-Om-Channel", "X-Api-Key"]
    assert named["evidence"]["methods"]["items"] == ["POST", "PUT", "PATCH", "DELETE"]


def test_simple_headers_add_nothing():
    plain = {"access-control-allow-origin": ORIGIN,
             "access-control-allow-headers": "Accept,Accept-Language,Content-Type,Origin,Range",
             "access-control-allow-methods": "GET,HEAD,OPTIONS"}
    assert "active-cors-declared-access" not in ids(
        active.preflight_findings("https://api.test/v1", ORIGIN, plain))


def test_truncation_is_reported_not_silent():
    many = {"access-control-allow-origin": ORIGIN,
            "access-control-allow-headers": ",".join(f"X-H{i}" for i in range(20))}
    named = ids(active.preflight_findings("https://api.test/v1", ORIGIN, many))[
        "active-cors-declared-access"]
    assert named["evidence"]["headers"]["total"] == 20
    assert named["evidence"]["headers"]["truncated"] is True
    assert named["evidence"]["headers"]["omitted"] == 8


def test_closed_preflight_stays_first():
    assert ids(active.preflight_findings("https://api.test/v1", ORIGIN, {}))[
        "active-cors-closed"]["status"] == "pass"
