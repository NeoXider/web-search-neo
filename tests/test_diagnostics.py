"""Console and network capture: the pure filters and the live Chrome path."""

from __future__ import annotations

import json
import time

import pytest
from selenium.common.exceptions import WebDriverException

import browser_tools
from chrome_bridge import ChromeBridgeError
import diagnostics


def _open_or_skip(url: str, session_id: str, **kwargs):
    # Keep the deterministic suite in the background while production defaults visible.
    kwargs.setdefault("headless", True)
    kwargs.setdefault("profile_mode", "temporary")
    try:
        return browser_tools.open_page(url, session_id=session_id, **kwargs)
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def _console_entry(seq: int, level: str, text: str, kind: str = "console", url: str | None = None):
    return {"seq": seq, "ts": seq, "kind": kind, "level": level, "text": text, "url": url}


CONSOLE_SAMPLE = [
    _console_entry(1, "debug", "starting up"),
    _console_entry(2, "info", "loaded level one"),
    _console_entry(3, "warn", "texture missing"),
    _console_entry(4, "error", "Uncaught TypeError: boom", kind="exception"),
    _console_entry(5, "error", "failed to load", kind="browser", url="http://host/sprite.png"),
    _console_entry(6, "info", "level one complete"),
]


def test_filter_console_selects_by_level():
    selected = diagnostics.filter_console(CONSOLE_SAMPLE, levels=["error"])
    assert [item["seq"] for item in selected] == [4, 5]
    assert [item["seq"] for item in diagnostics.filter_console(CONSOLE_SAMPLE, levels=["ERROR", "warn"])] == [3, 4, 5]
    assert diagnostics.filter_console(CONSOLE_SAMPLE, levels=["nothing"]) == []


def test_filter_console_selects_by_kind_and_substring():
    assert [
        item["seq"] for item in diagnostics.filter_console(CONSOLE_SAMPLE, kinds=["exception"])
    ] == [4]
    assert [
        item["seq"] for item in diagnostics.filter_console(CONSOLE_SAMPLE, kinds=["console", "browser"])
    ] == [1, 2, 3, 5, 6]  # input order is preserved, nothing is re-sorted
    # `contains` is case insensitive and also searches the url of an entry.
    assert [item["seq"] for item in diagnostics.filter_console(CONSOLE_SAMPLE, contains="LEVEL ONE")] == [2, 6]
    assert [item["seq"] for item in diagnostics.filter_console(CONSOLE_SAMPLE, contains="sprite.png")] == [5]
    assert diagnostics.filter_console(CONSOLE_SAMPLE, contains="nothing at all") == []


def test_filter_console_combines_filters_and_keeps_the_newest_entries():
    assert [
        item["seq"]
        for item in diagnostics.filter_console(CONSOLE_SAMPLE, levels=["error"], kinds=["browser"])
    ] == [5]
    assert [item["seq"] for item in diagnostics.filter_console(CONSOLE_SAMPLE, limit=2)] == [5, 6]
    assert [item["seq"] for item in diagnostics.filter_console(CONSOLE_SAMPLE, limit=0)] == [6]
    assert len(diagnostics.filter_console(CONSOLE_SAMPLE, limit=99)) == len(CONSOLE_SAMPLE)


NETWORK_SAMPLE = [
    {"id": "1", "method": "GET", "url": "http://host/index.html", "type": "Document",
     "status": 200, "ms": 12, "size": 2048},
    {"id": "2", "method": "GET", "url": "http://host/app.js", "type": "Script",
     "status": 304, "ms": 3, "size": 0},
    {"id": "3", "method": "POST", "url": "http://host/api/save", "type": "XHR",
     "status": 500, "ms": 40, "size": 120},
    {"id": "4", "method": "GET", "url": "http://host/sprite.png", "type": "Image",
     "status": 404, "ms": 7, "size": 30},
    {"id": "5", "method": "GET", "url": "http://cdn.host/track.js", "type": "Script",
     "status": None, "ms": 9, "failed": True, "error": "net::ERR_BLOCKED_BY_CLIENT"},
]


def test_filter_network_keeps_only_failures_and_bad_statuses():
    selected = diagnostics.filter_network(NETWORK_SAMPLE, only_errors=True)
    assert [item["id"] for item in selected] == ["3", "4", "5"]
    assert [item["id"] for item in diagnostics.filter_network(NETWORK_SAMPLE)] == [
        "1", "2", "3", "4", "5"
    ]


def test_filter_network_matches_urls_types_and_status_ranges():
    assert [item["id"] for item in diagnostics.filter_network(NETWORK_SAMPLE, url_pattern=r"\.js$")] == ["2", "5"]
    assert [item["id"] for item in diagnostics.filter_network(NETWORK_SAMPLE, url_pattern="CDN.HOST")] == ["5"]
    # An unusable regex must degrade to a literal search, never raise.
    assert diagnostics.filter_network(NETWORK_SAMPLE, url_pattern="api/save[") == []
    assert [item["id"] for item in diagnostics.filter_network(NETWORK_SAMPLE, types=["script"])] == ["2", "5"]
    assert [item["id"] for item in diagnostics.filter_network(NETWORK_SAMPLE, types=["Image", "XHR"])] == ["3", "4"]
    assert [item["id"] for item in diagnostics.filter_network(NETWORK_SAMPLE, status_min=400)] == ["3", "4"]
    assert [item["id"] for item in diagnostics.filter_network(NETWORK_SAMPLE, status_min=200, status_max=399)] == ["1", "2"]
    assert [
        item["id"]
        for item in diagnostics.filter_network(NETWORK_SAMPLE, types=["XHR"], only_errors=True, limit=1)
    ] == ["3"]


def test_format_network_renders_one_compact_line_per_request():
    lines = diagnostics.format_network(NETWORK_SAMPLE)
    assert len(lines) == len(NETWORK_SAMPLE)
    assert lines[0].split() == ["GET", "200", "Document", "12ms", "2.0KB", "http://host/index.html"]
    # A zero-byte response drops the size column instead of printing "0KB".
    assert lines[1].split() == ["GET", "304", "Script", "3ms", "http://host/app.js"]
    assert lines[3].split() == ["GET", "404", "Image", "7ms", "0.0KB", "http://host/sprite.png"]
    # A failure has no status, so the column reads ERR and carries the reason.
    assert lines[4].split() == [
        "GET", "ERR", "Script", "9ms", "net::ERR_BLOCKED_BY_CLIENT", "http://cdn.host/track.js"
    ]
    assert diagnostics.format_network([]) == []


class _FakeLogDriver:
    """Serves one canned ``get_log`` batch per call, like ChromeDriver does."""

    def __init__(self, batches: list[list[dict]], log_name: str = "performance"):
        self.batches = list(batches)
        self.log_name = log_name
        self.requested: list[str] = []

    def get_log(self, name: str) -> list[dict]:
        self.requested.append(name)
        if name != self.log_name:
            return []
        return self.batches.pop(0) if self.batches else []


def _perf(method: str, params: dict) -> dict:
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


def _will_be_sent(
    request_id: str,
    url: str,
    method: str = "GET",
    kind: str = "Document",
    timestamp: float = 100.0,
) -> dict:
    return _perf(
        "Network.requestWillBeSent",
        {
            "requestId": request_id,
            "wallTime": 1_700_000_000.5,
            "timestamp": timestamp,
            "type": kind,
            "documentURL": url,
            "initiator": {"type": "parser"},
            "request": {"method": method, "url": url},
        },
    )


def test_selenium_network_rows_folds_three_events_into_one_row():
    driver = _FakeLogDriver(
        [
            [
                _will_be_sent("REQ-1", "http://host/index.html"),
                _perf(
                    "Network.responseReceived",
                    {
                        "requestId": "REQ-1",
                        "type": "Document",
                        "response": {
                            "status": 200,
                            "mimeType": "text/html",
                            "remoteIPAddress": "127.0.0.1",
                            "fromDiskCache": False,
                        },
                    },
                ),
                _perf(
                    "Network.loadingFinished",
                    {"requestId": "REQ-1", "timestamp": 100.25, "encodedDataLength": 4096},
                ),
            ]
        ]
    )
    pending: dict = {}
    rows = diagnostics.selenium_network_rows(driver, pending)

    assert driver.requested == ["performance"]
    assert pending == {}  # a finished request leaves nothing behind
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "network"
    assert row["id"] == "REQ-1"
    assert row["method"] == "GET"
    assert row["url"] == "http://host/index.html"
    assert row["type"] == "Document"
    assert row["status"] == 200
    assert row["mime"] == "text/html"
    assert row["remote"] == "127.0.0.1"
    assert row["from_cache"] is False
    assert row["initiator"] == "parser"
    assert row["size"] == 4096
    assert row["ms"] == 250
    assert row["ts"] == 1_700_000_000_500
    assert row["done"] is True
    assert row["level"] == "info"
    assert row["text"] == "GET 200 http://host/index.html"


def test_a_row_carries_the_post_body_and_the_headers_that_govern_the_page():
    # What a form actually sent, and what the server allows, are the two things
    # a defence audit reads; neither is visible anywhere else in a row.
    sent = _perf(
        "Network.requestWillBeSent",
        {
            "requestId": "REQ-P",
            "wallTime": 1_700_000_000.5,
            "timestamp": 100.0,
            "type": "XHR",
            "documentURL": "https://host/apply",
            "initiator": {"type": "script"},
            "request": {
                "method": "POST",
                "url": "https://host/api/apply",
                "hasPostData": True,
                "postData": "resume=1&token=abc",
            },
        },
    )
    driver = _FakeLogDriver(
        [
            [
                sent,
                _perf(
                    "Network.responseReceived",
                    {
                        "requestId": "REQ-P",
                        "type": "XHR",
                        "response": {
                            "status": 403,
                            "mimeType": "application/json",
                            "headers": {
                                "Content-Security-Policy": "default-src 'self'",
                                "X-Frame-Options": "DENY",
                                "Set-Cookie": "sid=1; HttpOnly; Secure",
                                "Content-Length": "27",
                                "Date": "Tue, 19 Aug 2026 00:00:00 GMT",
                            },
                        },
                    },
                ),
                _perf(
                    "Network.loadingFinished",
                    {"requestId": "REQ-P", "timestamp": 100.1, "encodedDataLength": 27},
                ),
            ]
        ]
    )
    row = diagnostics.selenium_network_rows(driver, {})[0]

    assert row["post_data"] == "resume=1&token=abc"
    assert row["has_post_data"] is True
    # Header names come back lowercased, so a caller never guesses the casing.
    assert row["headers"]["content-security-policy"] == "default-src 'self'"
    assert row["headers"]["x-frame-options"] == "DENY"
    assert row["headers"]["set-cookie"] == "sid=1; HttpOnly; Secure"
    # Noise is dropped: every header would double the size of a network read.
    assert "content-length" not in row["headers"]
    assert "date" not in row["headers"]
    assert row["level"] == "error"


def test_a_get_without_a_body_says_so_and_a_huge_body_is_clipped():
    driver = _FakeLogDriver([[_will_be_sent("REQ-G", "https://host/page")]])
    diagnostics.selenium_network_rows(driver, pending := {})
    assert pending["REQ-G"]["post_data"] is None
    assert pending["REQ-G"]["has_post_data"] is False

    huge = "x" * (diagnostics.POST_DATA_LIMIT + 500)
    clipped = diagnostics._clip_post_data(huge)
    assert clipped.startswith("x" * 50)
    assert str(len(huge)) in clipped
    assert len(clipped) < len(huge)


def test_selenium_network_rows_carries_partial_rows_between_calls():
    driver = _FakeLogDriver(
        [
            [_will_be_sent("REQ-2", "http://host/api", method="POST", kind="XHR")],
            [
                _perf(
                    "Network.responseReceived",
                    {"requestId": "REQ-2", "type": "XHR", "response": {"status": 503}},
                ),
                _perf(
                    "Network.loadingFinished",
                    {"requestId": "REQ-2", "timestamp": 100.05, "encodedDataLength": 12},
                ),
            ],
        ]
    )
    pending: dict = {}

    assert diagnostics.selenium_network_rows(driver, pending) == []
    assert list(pending) == ["REQ-2"]  # the start survives until the request ends

    rows = diagnostics.selenium_network_rows(driver, pending)
    assert pending == {}
    assert [(row["method"], row["status"], row["level"], row["ms"]) for row in rows] == [
        ("POST", 503, "error", 50)
    ]


def test_selenium_network_rows_reports_a_failed_request():
    driver = _FakeLogDriver(
        [
            [
                _will_be_sent("REQ-3", "http://cdn/track.js", kind="Script"),
                _perf(
                    "Network.loadingFailed",
                    {
                        "requestId": "REQ-3",
                        "timestamp": 100.01,
                        "errorText": "net::ERR_BLOCKED_BY_CLIENT",
                        "canceled": False,
                        "blockedReason": "other",
                    },
                ),
            ]
        ]
    )
    rows = diagnostics.selenium_network_rows(driver, {})
    assert len(rows) == 1
    row = rows[0]
    assert row["failed"] is True
    assert row["done"] is True
    assert row["status"] is None
    assert row["error"] == "net::ERR_BLOCKED_BY_CLIENT"
    assert row["canceled"] is False
    assert row["blocked_reason"] == "other"
    assert row["level"] == "error"
    assert row["text"] == "GET --- http://cdn/track.js"
    assert diagnostics.format_network(rows)[0].endswith(
        "net::ERR_BLOCKED_BY_CLIENT http://cdn/track.js"
    )


def test_selenium_network_rows_ignores_unusable_messages():
    driver = _FakeLogDriver(
        [
            [
                {"message": "not json at all"},
                {"nothing": "useful"},
                _perf("Page.frameNavigated", {"requestId": "REQ-4"}),
                _perf("Network.loadingFinished", {"requestId": "unknown", "timestamp": 1.0}),
                _perf("Network.responseReceived", {"requestId": "unknown", "response": {}}),
            ]
        ]
    )
    pending: dict = {}
    assert diagnostics.selenium_network_rows(driver, pending) == []
    assert pending == {}


def test_selenium_network_rows_caps_the_pending_map_and_evicts_the_oldest():
    # SSE, websockets, long polls, and navigation-cancelled requests never send a
    # finishing event, so an uncapped map is an unbounded leak in a long session.
    limit = diagnostics.PENDING_REQUEST_LIMIT
    total = limit + 50
    driver = _FakeLogDriver(
        [
            [
                _will_be_sent(f"REQ-{index}", f"http://host/{index}", timestamp=float(index))
                for index in range(total)
            ]
        ]
    )
    pending: dict = {}
    assert diagnostics.selenium_network_rows(driver, pending) == []
    assert len(pending) == limit
    assert "REQ-0" not in pending  # the earliest starts are the ones dropped
    assert f"REQ-{total - 1}" in pending

    # Eviction must not corrupt the rows that survived: the newest still finishes.
    driver.batches.append(
        [
            _perf(
                "Network.loadingFinished",
                {"requestId": f"REQ-{total - 1}", "timestamp": float(total), "encodedDataLength": 7},
            )
        ]
    )
    rows = diagnostics.selenium_network_rows(driver, pending)
    assert [row["id"] for row in rows] == [f"REQ-{total - 1}"]
    assert rows[0]["ms"] == 1000
    assert len(pending) == limit - 1


def test_selenium_network_rows_survives_a_driver_without_a_performance_log():
    class _NoLogDriver:
        def get_log(self, name: str):
            raise WebDriverException("performance log is not enabled")

    assert diagnostics.selenium_network_rows(_NoLogDriver(), {}) == []


def test_selenium_browser_log_splits_the_location_prefix():
    driver = _FakeLogDriver(
        [
            [
                {
                    "level": "SEVERE",
                    "timestamp": 1700,
                    "source": "javascript",
                    "message": "http://host/app.js 12:34 Uncaught Error: boom",
                },
                {"level": "WARNING", "timestamp": 1701, "message": "deprecated api"},
            ]
        ],
        log_name="browser",
    )
    entries = diagnostics.selenium_browser_log(driver)
    assert entries[0] == {
        "seq": 0,
        "ts": 1700,
        "kind": "browser",
        "level": "error",
        "source": "javascript",
        "text": "Uncaught Error: boom",
        "url": "http://host/app.js",
        "line": 12,
        "col": 34,
        "args": [],
        "stack": [],
    }
    assert entries[1]["level"] == "warn"
    assert entries[1]["text"] == "deprecated api"
    assert entries[1]["url"] is None
    assert entries[1]["source"] == "other"


def _hook_entry(level: str, text: str, kind: str = "console") -> dict:
    return {"seq": 1, "ts": 10, "kind": kind, "level": level, "source": "console-api", "text": text}


def _browser_entry(level: str, text: str) -> dict:
    # Chrome labels its own copy console-api too, so only the kind separates them.
    return {"seq": 0, "ts": 10, "kind": "browser", "level": level, "source": "console-api", "text": text}


def test_dedupe_console_drops_the_json_quoted_browser_copy():
    entries = [_hook_entry("error", "marker-once"), _browser_entry("error", '"marker-once"')]
    assert [item["kind"] for item in diagnostics.dedupe_console(entries)] == ["console"]

    # console.error('a', 'b') reaches the browser log as two quoted arguments.
    several = [_hook_entry("error", "a b"), _browser_entry("error", '"a" "b"')]
    assert [item["kind"] for item in diagnostics.dedupe_console(several)] == ["console"]

    escaped = [
        _hook_entry("warn", 'say "hi"\nagain'),
        _browser_entry("warn", '"say \\"hi\\"\\nagain"'),
    ]
    assert [item["kind"] for item in diagnostics.dedupe_console(escaped)] == ["console"]


def test_dedupe_console_keeps_everything_the_hook_did_not_capture():
    entries = [
        _hook_entry("error", "marker-once"),
        _browser_entry("error", '"marker-twice"'),  # a different message
        _browser_entry("warn", '"marker-once"'),  # same text, other level
        _browser_entry("error", "Failed to load resource: 404"),  # no hook copy exists
        _hook_entry("error", "Uncaught Error: boom", kind="exception"),
    ]
    assert [item["text"] for item in diagnostics.dedupe_console(entries)] == [
        "marker-once",
        '"marker-twice"',
        '"marker-once"',
        "Failed to load resource: 404",
        "Uncaught Error: boom",
    ]
    assert diagnostics.dedupe_console([]) == []


def test_read_page_console_installs_the_hook_when_it_is_missing():
    class _HookDriver:
        def __init__(self):
            self.scripts: list[str] = []
            self.arguments: list[tuple] = []
            self.installed = False

        def execute_script(self, script: str, *args):
            self.scripts.append(script)
            if script is diagnostics.CONSOLE_HOOK_SCRIPT:
                self.installed = True
                return None
            self.arguments.append(args)
            if not self.installed:
                return {
                    "entries": [],
                    "next_seq": 0,
                    "doc": "",
                    "document_changed": False,
                    "dropped": 0,
                    "installed": False,
                }
            return {
                "entries": [{"seq": 7, "level": "info", "text": "hello"}],
                "next_seq": 7,
                "doc": "1700000000000-a1b2c3",
                "document_changed": False,
                "dropped": 2,
                "installed": True,
            }

    driver = _HookDriver()
    assert diagnostics.read_page_console(driver) == {
        "entries": [],
        "next_seq": 0,
        "doc": "",
        "document_changed": False,
        "dropped": 0,
    }
    assert driver.scripts[-1] is diagnostics.CONSOLE_HOOK_SCRIPT

    payload = diagnostics.read_page_console(
        driver, since_seq=3, clear=True, doc="1700000000000-a1b2c3"
    )
    assert payload["entries"][0]["text"] == "hello"
    assert payload["next_seq"] == 7
    assert payload["dropped"] == 2
    assert payload["doc"] == "1700000000000-a1b2c3"
    # The document a cursor was minted in travels with it, which is what lets the
    # page tell a cursor from a replaced document from one that is merely behind.
    assert driver.arguments[-1] == (3, "1700000000000-a1b2c3", True)


def test_console_reports_logs_errors_and_uncaught_exceptions(local_site):
    # No step mode here: gated timers would hold the setTimeout that throws.
    _open_or_skip(f"{local_site.base_url}/page", "console-live")
    driver = browser_tools._get_session("console-live").driver
    try:
        driver.execute_script(
            "console.log('hello-info-marker', 42);"
            "console.warn('careful-warn-marker');"
            "console.error('boom-error-marker');"
        )
        # Raised from an inline page script so the error event keeps its message
        # instead of collapsing into an opaque cross-origin "Script error.".
        driver.execute_script(
            "const script = document.createElement('script');"
            "script.textContent = \"setTimeout(function () {"
            " throw new Error('uncaught-marker'); }, 0);\";"
            "document.body.appendChild(script);"
        )

        entries: list[dict] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            payload = browser_tools.get_console("console-live", limit=200)
            assert payload["success"] is True
            entries.extend(payload["entries"])
            if any(item.get("kind") == "exception" for item in entries):
                break
            time.sleep(0.05)

        by_kind = {item["kind"] for item in entries}
        assert "console" in by_kind
        hooked = [item for item in entries if item["kind"] == "console"]
        levels = {item["text"]: item["level"] for item in hooked}
        assert levels["hello-info-marker 42"] == "info"
        assert levels["careful-warn-marker"] == "warn"
        assert levels["boom-error-marker"] == "error"
        assert [item for item in hooked if item["text"].startswith("hello")][0]["args"] == [
            "hello-info-marker",
            "42",
        ]

        crashes = [item for item in entries if item["kind"] == "exception"]
        assert crashes, "the uncaught exception never reached the console buffer"
        assert crashes[0]["level"] == "error"
        assert crashes[0]["source"] == "javascript"
        assert "uncaught-marker" in crashes[0]["text"]
    finally:
        browser_tools.close_session("console-live")


def test_console_level_filter_drops_everything_below_error(local_site):
    _open_or_skip(f"{local_site.base_url}/page", "console-filter")
    driver = browser_tools._get_session("console-filter").driver
    try:
        browser_tools.get_console("console-filter", limit=200)  # move the cursor past the load
        driver.execute_script(
            "console.log('quiet-info-marker');"
            "console.info('another-info-marker');"
            "console.error('loud-error-marker');"
        )
        payload = browser_tools.get_console("console-filter", levels=["error"], limit=50)
        assert payload["levels"] == ["error"]
        texts = " | ".join(item["text"] for item in payload["entries"])
        assert "loud-error-marker" in texts
        assert "quiet-info-marker" not in texts
        assert "another-info-marker" not in texts
        assert {item["level"] for item in payload["entries"]} == {"error"}
        assert payload["returned"] == len(payload["entries"])
    finally:
        browser_tools.close_session("console-filter")


def test_one_console_error_is_reported_once_by_a_live_chrome(local_site):
    _open_or_skip(f"{local_site.base_url}/page", "console-dedupe")
    driver = browser_tools._get_session("console-dedupe").driver
    try:
        diagnostics.read_page_console(driver)  # install the hook
        diagnostics.selenium_browser_log(driver)  # drop load-time noise
        driver.execute_script("console.error('marker-once');")

        entries: list[dict] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            entries.extend(diagnostics.read_page_console(driver)["entries"])
            entries.extend(diagnostics.selenium_browser_log(driver))
            if len([item for item in entries if "marker-once" in item["text"]]) >= 2:
                break
            time.sleep(0.1)

        marked = [item for item in entries if "marker-once" in item["text"]]
        if len(marked) < 2:
            pytest.skip("Chrome's browser log never delivered its copy of the message")
        # Both channels really do report it, and the texts really do differ.
        assert {item["text"] for item in marked} == {"marker-once", '"marker-once"'}
        deduped = diagnostics.dedupe_console(entries)
        assert [item["text"] for item in deduped if "marker-once" in item["text"]] == [
            "marker-once"
        ]
    finally:
        browser_tools.close_session("console-dedupe")


def _boot_log_url(local_site, marker: str) -> str:
    return f"{local_site.base_url}/boot-log?marker={marker}"


def _hooked_texts(payload: dict) -> set[str]:
    """The texts the in-page hook reported, which is where args and stacks live.

    Chrome's browser log carries its own JSON-quoted copy of the same messages, so
    a test that looked at the text alone would pass on that copy while the hook
    itself stayed silent.
    """
    return {item["text"] for item in payload["entries"] if item["kind"] == "console"}


def test_console_reports_what_a_replacement_document_logged_while_booting(local_site):
    _open_or_skip(_boot_log_url(local_site, "first"), "console-boot")
    try:
        first = browser_tools.get_console("console-boot", limit=200)
        assert "boot-error-first" in _hooked_texts(first)
        assert first["next_seq"] >= 3  # the cursor now sits past the first page

        # The page numbers its entries from one again with the new document, so a
        # cursor kept from the document that was replaced sits above everything
        # the new one logged while booting.
        browser_tools.open_page(
            _boot_log_url(local_site, "second"),
            session_id="console-boot",
            headless=True,
            profile_mode="temporary",
        )
        after = browser_tools.get_console("console-boot", limit=200)
        assert {"boot-log-second", "boot-warn-second", "boot-error-second"} <= _hooked_texts(after)
        assert after["cursor_reset"] is True

        # A reload is the same boundary with the same text on either side of it,
        # which is what a level restart looks like.
        browser_tools._get_session("console-boot").driver.refresh()
        reloaded = browser_tools.get_console("console-boot", limit=200)
        assert {"boot-log-second", "boot-warn-second", "boot-error-second"} <= _hooked_texts(
            reloaded
        )
        assert reloaded["cursor_reset"] is True

        # Reading twice inside one document still reports each entry once.
        repeated = browser_tools.get_console("console-boot", limit=200)
        assert _hooked_texts(repeated) == set()
        assert repeated["cursor_reset"] is False
    finally:
        browser_tools.close_session("console-boot")


def test_console_still_reports_what_a_page_logged_before_it_navigated_away(local_site):
    _open_or_skip(_boot_log_url(local_site, "alpha"), "console-farewell")
    driver = browser_tools._get_session("console-farewell").driver
    try:
        browser_tools.get_console("console-farewell", limit=200)
        driver.execute_script("console.error('doomed-before-navigation');")
        browser_tools.open_page(
            _boot_log_url(local_site, "beta"),
            session_id="console-farewell",
            headless=True,
            profile_mode="temporary",
        )

        # The hook's buffer dies with the document it lived in. Chrome's browser
        # log belongs to the browser rather than to the page, so the last thing a
        # page said before being replaced is still delivered.
        texts: list[str] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            payload = browser_tools.get_console("console-farewell", limit=200)
            texts.extend(item["text"] for item in payload["entries"])
            if any("doomed-before-navigation" in item for item in texts):
                break
            time.sleep(0.1)
        assert any("doomed-before-navigation" in item for item in texts), texts
    finally:
        browser_tools.close_session("console-farewell")


def test_game_probe_reports_the_boot_output_of_the_page_it_was_pointed_at(local_site):
    _open_or_skip(_boot_log_url(local_site, "one"), "probe-boot")
    try:
        browser_tools.game_probe("probe-boot", sample_seconds=0.1)
        browser_tools.open_page(
            _boot_log_url(local_site, "two"),
            session_id="probe-boot",
            headless=True,
            profile_mode="temporary",
        )
        probe = browser_tools.game_probe("probe-boot", sample_seconds=0.1)
        messages = {item["message"] for item in probe["console_messages"]}
        assert {"boot-warn-two", "boot-error-two"} <= messages, messages
        assert not any(item.endswith("-one") for item in messages)  # already reported
    finally:
        browser_tools.close_session("probe-boot")


class _FakeCompanionDriver:
    """The companion backend's ``events.get`` contract, without an extension.

    The extension buffers per tab in its service worker, so a navigation never
    disturbs its numbering; the counter restarts only when the worker itself is
    evicted, and its ``collectEvents`` then replays from the beginning and says
    ``reset``. Nothing in this suite can drive the real extension, so this stands
    in for the contract the reader has to honour.
    """

    def __init__(self):
        self.entries: list[dict] = []
        self.seq = 0

    def log(self, text: str) -> None:
        self.seq += 1
        self.entries.append(
            {"seq": self.seq, "ts": self.seq, "kind": "console", "level": "error", "text": text}
        )

    def evict_worker(self) -> None:
        self.entries = []
        self.seq = 0

    def get_events(self, *, kinds=None, since_seq=0, limit=200) -> dict:
        reset = since_seq > self.seq
        since = 0 if reset else since_seq
        entries = [item for item in self.entries if item["seq"] > since][:limit]
        return {"entries": entries, "next_seq": self.seq, "dropped": None, "reset": reset}

    def clear_events(self, kinds=None) -> dict:
        self.entries = []
        return {"cleared": []}


def test_console_replays_when_the_companion_backend_restarts_its_counter():
    driver = _FakeCompanionDriver()
    browser_tools._sessions["fake-companion"] = browser_tools.BrowserSession(
        driver=driver, headless=True, profile_mode="current"
    )
    try:
        driver.log("before-navigation")
        first = browser_tools.get_console("fake-companion", limit=200)
        assert [item["text"] for item in first["entries"]] == ["before-navigation"]
        assert first["cursor_reset"] is False

        # A navigation is not a boundary for this backend: one buffer serves the
        # whole tab, so the numbering carries on across it.
        driver.log("after-navigation")
        second = browser_tools.get_console("fake-companion", limit=200)
        assert [item["text"] for item in second["entries"]] == ["after-navigation"]
        assert second["cursor_reset"] is False

        # An evicted worker counts from one again. Going quiet until the new
        # counter passes the old cursor is the failure being fixed here.
        driver.evict_worker()
        driver.log("after-the-worker-restarted")
        third = browser_tools.get_console("fake-companion", limit=200)
        assert [item["text"] for item in third["entries"]] == ["after-the-worker-restarted"]
        assert third["cursor_reset"] is True
    finally:
        browser_tools._sessions.pop("fake-companion", None)


class _FakeCompanionBridge:
    """The companion's capture rules, without an extension.

    Two of them decide what the network topic can report, and both live in the
    service worker. A request is recorded only if the tab was already subscribed
    when it was made - ``Network.requestWillBeSent`` returns early otherwise - and
    each stream keeps the newest 500 records and counts what it dropped. Nothing
    in this suite can drive the real extension, so this stands in for those two
    rules and answers the handful of commands one page load needs.

    Traffic is produced the way a browser produces it: navigating makes the
    requests of a page load, and ``page_requests`` is the page acting on its own.
    Neither asks whether anyone is listening, which is the whole point.
    """

    KEPT = 500

    def __init__(self, tab_id: int = 77) -> None:
        self.tab_id = tab_id
        self.url = "https://example.test/already-open"
        self.group = "Existing"
        self.domains: set[str] = set()
        self.started_at = 0
        self.seq = 0
        self.entries: list[dict] = []
        self.dropped = {"console": 0, "network": 0}
        self.methods: list[str] = []

    def page_requests(self, *urls: str) -> None:
        """Requests the page makes; kept only while the tab is capturing."""
        if "network" not in self.domains:
            return
        for url in urls:
            self.seq += 1
            self.entries.append(
                {
                    "seq": self.seq,
                    "ts": self.seq,
                    "kind": "network",
                    "level": "info",
                    "id": f"request-{self.seq}",
                    "method": "GET",
                    "url": url,
                    "type": "Document" if url == self.url else "Script",
                    "status": 200,
                    "ms": 5,
                    "size": 512,
                    "done": True,
                }
            )
        network = [item for item in self.entries if item["kind"] == "network"]
        overflow = len(network) - self.KEPT
        if overflow > 0:
            for evicted in network[:overflow]:
                self.entries.remove(evicted)
            self.dropped["network"] += overflow

    def _tab(self) -> dict:
        return {"id": self.tab_id, "url": self.url, "title": "Example", "group": self.group}

    def _evaluate(self, params: dict) -> dict:
        expression = params["params"].get("expression", "")
        if "location.href" in expression:  # the one-round-trip page summary
            return {
                "result": {
                    "value": {
                        "url": self.url,
                        "title": "Example",
                        "viewport_width": 1440,
                        "viewport_height": 900,
                        "page_width": 1440,
                        "page_height": 900,
                        "ready_state": "complete",
                        "challenge": {},
                    }
                }
            }
        if "document.readyState" in expression:
            return {"result": {"value": "complete"}}
        return {"result": {"value": None}}

    def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
        params = params or {}
        self.methods.append(method)
        if method == "tabs.create":
            self.url = params["url"]
            self.group = params["group"]
            return self._tab()
        if method in {"tabs.get", "tabs.activate"}:
            return self._tab()
        if method == "tabs.navigate":
            self.url = params["url"]
            self.page_requests(self.url, "https://example.test/boot.js")
            return self._tab()
        if method == "cdp.send":
            if params["method"] == "Runtime.evaluate":
                return self._evaluate(params)
            return {}
        if method == "events.subscribe":
            self.domains.update(str(item).lower() for item in params["domains"])
            self.started_at = self.started_at or 1_700_000_000_000
            return {
                "started_at": self.started_at,
                "seq": self.seq,
                "domains": sorted(self.domains),
            }
        if method == "events.unsubscribe":
            for domain in params.get("domains") or list(self.domains):
                self.domains.discard(str(domain).lower())
            return {"domains": sorted(self.domains)}
        if method == "events.get":
            wanted = {
                str(kind).lower() for kind in (params.get("kinds") or ["console", "network"])
            }
            since = int(params.get("since_seq") or 0)
            selected = [
                item for item in self.entries if item["kind"] in wanted and item["seq"] > since
            ]
            return {
                "entries": selected[: int(params.get("limit") or 200)],
                "next_seq": self.seq,
                "dropped": dict(self.dropped),
                "started_at": self.started_at,
                "truncated": False,
                "reset": False,
                "pending": 0,
            }
        if method == "events.clear":
            self.entries = []
            return {"cleared": ["console", "network"], "seq": self.seq}
        if method == "debugger.detach":
            return {"detached": True}
        if method == "tabs.remove":
            return {"removed": True, "id": self.tab_id}
        raise AssertionError(method)


def test_network_reports_the_requests_of_the_very_first_navigation(monkeypatch):
    """The failure being fixed: an agent opens a page, the page misbehaves, and
    the network topic answers "no requests were made" because it only started
    recording when it was asked."""
    bridge = _FakeCompanionBridge()
    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: bridge)
    try:
        browser_tools.open_page(
            "https://example.test/first",
            session_id="companion-first-nav",
            profile_mode="current",
        )
        reported = browser_tools.get_network("companion-first-nav", output="json", limit=50)
        assert [row["url"] for row in reported["requests"]] == [
            "https://example.test/first",
            "https://example.test/boot.js",
        ]
        assert reported["dropped"] == 0

        # One capture serves the whole tab, so a navigation is not a boundary:
        # the subscription made at open still covers the pages that follow.
        browser_tools.open_page(
            "https://example.test/second",
            session_id="companion-first-nav",
            profile_mode="current",
        )
        after = browser_tools.get_network("companion-first-nav", output="json", limit=50)
        assert [row["url"] for row in after["requests"]] == [
            "https://example.test/first",
            "https://example.test/boot.js",
            "https://example.test/second",
            "https://example.test/boot.js",
        ]
    finally:
        browser_tools.close_session("companion-first-nav")


def test_network_records_a_claimed_tab_from_the_moment_it_was_claimed(monkeypatch):
    bridge = _FakeCompanionBridge()
    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: bridge)
    # Whatever the tab did before the attach was observed by nobody: no capture
    # was running, so there is nothing to recover and the docstring says so.
    bridge.page_requests("https://example.test/before-the-attach")
    try:
        browser_tools.attach_current_tab(bridge.tab_id, session_id="companion-attach")
        bridge.page_requests("https://example.test/after-the-attach")
        reported = browser_tools.get_network("companion-attach", output="json", limit=50)
        assert [row["url"] for row in reported["requests"]] == [
            "https://example.test/after-the-attach"
        ]
    finally:
        browser_tools.close_session("companion-attach")


def test_network_subscribes_again_when_the_subscription_at_open_failed(monkeypatch):
    """Capture at open is best-effort: losing it costs history, not the session."""

    class _RefusingBridge(_FakeCompanionBridge):
        def __init__(self) -> None:
            super().__init__()
            self.refusals = 1

        def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
            if method == "events.subscribe" and self.refusals:
                self.refusals -= 1
                raise ChromeBridgeError("the companion was busy")
            return super().request(method, params, timeout)

    bridge = _RefusingBridge()
    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: bridge)
    try:
        browser_tools.open_page(
            "https://example.test/first",
            session_id="companion-refused",
            profile_mode="current",
        )
        driver = browser_tools._get_session("companion-refused").driver
        assert driver.events_subscribed is False

        # Reading the topic repairs the subscription, so the session degrades to
        # the old behaviour - late - instead of staying silent for good.
        assert browser_tools.get_network("companion-refused", output="json")["requests"] == []
        assert driver.events_subscribed is True
        bridge.page_requests("https://example.test/after-the-repair")
        reported = browser_tools.get_network("companion-refused", output="json")
        assert [row["url"] for row in reported["requests"]] == [
            "https://example.test/after-the-repair"
        ]
    finally:
        browser_tools.close_session("companion-refused")


def test_network_says_how_many_records_a_long_session_lost(monkeypatch):
    """Capture now runs for the life of the session, so the buffer wraps. A gap
    that is not reported would be read as a quiet page."""
    bridge = _FakeCompanionBridge()
    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: bridge)
    try:
        browser_tools.open_page(
            "https://example.test/first",
            session_id="companion-wrap",
            profile_mode="current",
        )
        bridge.page_requests(*[f"https://example.test/ping-{index}" for index in range(600)])
        reported = browser_tools.get_network("companion-wrap", output="json", limit=1000)
        urls = [row["url"] for row in reported["requests"]]
        assert len(urls) == 500  # the newest, bounded, never the whole 602
        assert reported["dropped"] == 102
        assert urls[-1] == "https://example.test/ping-599"
        assert "https://example.test/first" not in urls  # wrapped out, and counted
    finally:
        browser_tools.close_session("companion-wrap")


def test_network_reports_what_the_selenium_buffer_had_to_drop():
    """The Selenium backend has no subscription to move; it must keep working."""
    batch: list[dict] = []
    for index in range(600):
        batch.append(
            _will_be_sent(f"REQ-{index}", f"http://host/asset-{index}.js", kind="Script")
        )
        batch.append(
            _perf(
                "Network.loadingFinished",
                {"requestId": f"REQ-{index}", "timestamp": 100.01, "encodedDataLength": 10},
            )
        )
    session = browser_tools.BrowserSession(
        driver=_FakeLogDriver([batch]), headless=True, profile_mode="temporary"
    )
    browser_tools._sessions["selenium-wrap"] = session
    try:
        reported = browser_tools.get_network("selenium-wrap", output="json", limit=1000)
        urls = [row["url"] for row in reported["requests"]]
        assert len(urls) == 500
        assert reported["dropped"] == 100
        assert urls[-1] == "http://host/asset-599.js"
    finally:
        browser_tools._sessions.pop("selenium-wrap", None)


def test_network_lists_the_document_and_isolates_the_failing_request(local_site):
    _open_or_skip(f"{local_site.base_url}/page", "network-live")
    driver = browser_tools._get_session("network-live").driver
    try:
        driver.execute_script(
            "window.__done = 0;"
            "fetch('/definitely-missing-path').then(r => r.text())"
            " .then(() => { window.__done += 1; });"
            "const image = new Image();"
            "image.onerror = image.onload = () => { window.__done += 1; };"
            "image.src = '/missing-image.png';"
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and driver.execute_script("return window.__done;") < 2:
            time.sleep(0.05)
        assert driver.execute_script("return window.__done;") == 2

        rows: list[dict] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = browser_tools.get_network("network-live", output="json", limit=100)["requests"]
            if len(rows) >= 3:
                break
            time.sleep(0.1)

        by_url = {row["url"].rsplit("/", 1)[-1]: row for row in rows}
        document = by_url["page"]
        assert document["method"] == "GET"
        assert document["status"] == 200
        assert document["type"] == "Document"
        assert document["level"] == "info"
        assert document["size"] > 0
        assert document["done"] is True
        assert by_url["definitely-missing-path"]["status"] == 404
        assert by_url["definitely-missing-path"]["level"] == "error"

        failures = browser_tools.get_network("network-live", only_errors=True, limit=50)
        assert failures["only_errors"] is True
        assert failures["format"] == "method status type ms size url"
        assert failures["returned"] == len(failures["requests"])
        assert failures["requests"]  # the 404s, and only those
        assert all("404" in line for line in failures["requests"])
        assert all("/page" not in line for line in failures["requests"])
        assert any("definitely-missing-path" in line for line in failures["requests"])
        assert any("missing-image.png" in line for line in failures["requests"])

        documents = browser_tools.get_network("network-live", types=["Document"], output="json")
        assert [row["url"] for row in documents["requests"]] == [document["url"]]
        assert browser_tools.get_network(
            "network-live", url_pattern="never-requested", output="json"
        )["requests"] == []
    finally:
        browser_tools.close_session("network-live")
