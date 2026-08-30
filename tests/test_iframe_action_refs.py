from __future__ import annotations

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools
from web_search_neo import page_perception


def _nodes(result: dict) -> list[dict]:
    return [node for node in result["nodes"] if node.get("kind") == "node"]


def test_click_and_fill_refs_inside_a_nested_same_origin_frame(local_site):
    session_id = "iframe-action-refs"
    try:
        browser_tools.open_page(
            f"{local_site.base_url}/fixtures/perception/nested_host.html",
            session_id=session_id,
            width=1024,
            height=768,
            headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")

    driver = browser_tools._get_session(session_id).driver
    try:
        outline = page_perception.outline(driver, format="json", limit=400)
        nodes = _nodes(outline)
        button = next(node for node in nodes if node.get("name") == "Confirm payment")
        field = next(node for node in nodes if node.get("name") == "Card holder")
        assert button["frame"] == "#outer >>> #inner"
        assert field["frame"] == "#outer >>> #inner"

        clicked = browser_tools.click(button["ref"], session_id=session_id, wait_seconds=0)
        filled = browser_tools.fill_fields({field["ref"]: "Ada Lovelace"}, session_id=session_id)

        assert clicked["success"] is True
        assert filled["success"] is True
        assert filled["field_values"][field["ref"]] == "Ada Lovelace"
        state = driver.execute_script(
            "const leaf = document.getElementById('outer').contentDocument"
            ".getElementById('inner').contentDocument;"
            "return {click: leaf.getElementById('pay-state').textContent,"
            " value: leaf.getElementById('leaf-field').value};"
        )
        assert state == {"click": "paid", "value": "Ada Lovelace"}
    finally:
        browser_tools.close_session(session_id)
