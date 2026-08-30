from __future__ import annotations

import re

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools


SESSION_ID = "closed-shadow"


def _open_or_skip(url: str) -> None:
    try:
        browser_tools.open_page(
            url,
            session_id=SESSION_ID,
            width=1024,
            height=768,
            headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def _nodes(result: dict) -> list[dict]:
    return [node for node in result["nodes"] if node.get("kind") == "node"]


def test_closed_shadow_roots_are_perceived_addressed_and_actionable(local_site):
    url = f"{local_site.base_url}/fixtures/perception/closed_shadow.html"
    _open_or_skip(url)
    try:
        elements = browser_tools.get_page_elements(session_id=SESSION_ID)
        buttons = {button["text"]: button for button in elements["buttons"]}
        assert {"Open action", "Closed action", "Nested closed action"} <= buttons.keys()
        assert buttons["Closed action"]["selector"] == "#closed-host >>> #closed-button"
        assert buttons["Nested closed action"]["selector"] == (
            "#closed-host >>> #nested-closed-host >>> #nested-closed-button"
        )

        outline = browser_tools.get_page_outline(
            session_id=SESSION_ID, output="json", limit=100
        )
        assert outline["closed_shadow_roots"] == 2
        by_name = {node.get("name"): node for node in _nodes(outline)}
        for name in ("Closed action", "Nested closed action"):
            assert re.fullmatch(r"ref:[0-9a-f]+:\d+", by_name[name]["ref"])

        clicked = browser_tools.click(by_name["Closed action"]["ref"], session_id=SESSION_ID)
        assert clicked["success"] is True

        after = browser_tools.get_page_outline(
            session_id=SESSION_ID, output="json", limit=100
        )
        assert after["closed_shadow_roots"] == 2
        assert any(
            node.get("kind") == "text" and "Closed state: clicked" in node.get("text", "")
            for node in after["nodes"]
        )
    finally:
        browser_tools.close_session(SESSION_ID)
