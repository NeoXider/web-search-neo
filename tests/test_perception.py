from __future__ import annotations

import re

import pytest
from selenium.common.exceptions import WebDriverException

import browser_tools
import page_perception


SHADOW_FIXTURE = """
const host = document.createElement('div');
host.id = 'widget-host';
document.body.appendChild(host);
const root = host.attachShadow({mode: 'open'});
const button = document.createElement('button');
button.id = 'shadow-button';
button.textContent = 'Shadow Button';
const paragraph = document.createElement('p');
paragraph.textContent = 'Text living inside the shadow root.';
root.appendChild(button);
root.appendChild(paragraph);
return true;
"""


def _driver_or_skip(url: str, session_id: str):
    """Open a headless throwaway session and hand back its raw driver."""
    try:
        browser_tools.open_page(
            url,
            session_id=session_id,
            width=1024,
            height=768,
            headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    return browser_tools._get_session(session_id).driver


def _nodes(result: dict) -> list[dict]:
    return [node for node in result["nodes"] if node.get("kind") == "node"]


def _evaluate(driver, expression: str):
    return driver.execute_script(f"const target = {expression}; return target ? target.id : null;")


def test_outline_reports_roles_names_and_usable_refs(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/form?session=outline", "perception-outline")
    try:
        result = page_perception.outline(driver, format="json")
        nodes = _nodes(result)
        assert result["dom_epoch"]
        assert result["closed_shadow_roots"] == 0
        assert all(re.fullmatch(r"ref:\d+", node["ref"]) for node in nodes)

        named = {(node["role"], node["name"]) for node in nodes}
        assert ("textbox", "Candidate name") in named
        assert ("textbox", "Cover letter") in named
        assert ("combobox", "Role") in named
        assert ("button", "Apply") in named
        assert ("button", "Run action") in named
        assert ("link", "Fixture link") in named
        assert ("form", "") in named

        candidate = next(
            node for node in nodes
            if node["role"] == "textbox" and node["name"] == "Candidate name"
        )
        assert "required" in candidate["states"]
        assert candidate["visible"] is True
        assert candidate["occluded"] is False
        assert candidate["page_rect"]["w"] > 0 and candidate["page_rect"]["h"] > 0
        assert candidate["rect"] == candidate["page_rect"]
        assert candidate["center"]["x"] > 0

        # The ref survives the round-trip and points back at the very same element.
        assert _evaluate(driver, page_perception.ref_expression(candidate["ref"])) == "candidate-name"

        submit = next(node for node in nodes if node["name"] == "Apply")
        assert _evaluate(driver, page_perception.ref_expression(submit["ref"])) == "submit-button"
    finally:
        browser_tools.close_session("perception-outline")


def test_outline_text_format_stays_compact_and_skips_occlusion_when_asked(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/form?session=text", "perception-text-format")
    try:
        result = page_perception.outline(driver, include_occlusion=False)
        text = result["outline"]
        assert result["format"] == "text"
        assert result["occlusion_checked"] is False
        assert 'textbox "Candidate name"' in text
        assert 'button "Apply"' in text
        assert "ref:" in text
        assert re.search(r"@\d+,\d+ \d+x\d+", text)
        assert max(len(line) for line in text.splitlines()) < 160

        limited = page_perception.outline(driver, limit=3, format="json")
        assert len(limited["nodes"]) <= 3
        assert limited["truncated"] is True
    finally:
        browser_tools.close_session("perception-text-format")


def test_page_text_returns_rendered_text_without_scripts(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/page", "perception-page-text")
    try:
        result = page_perception.page_text(driver)
        text = result["text"]
        assert "Local fixture" in text
        assert "Visible body text." in text
        assert "script must not be returned" not in text
        assert "hidden noscript" not in text
        assert "## Local fixture" in text
        assert result["truncated"] is False
        assert result["total_chars"] == len(text)

        linked = page_perception.page_text(driver, include_links=True)
        assert linked["links"]
        assert any(link["text"] == "Relative" for link in linked["links"])
        assert "[1]" in linked["text"]
        assert "-> http" in linked["text"]

        clipped = page_perception.page_text(driver, max_chars=200)
        assert clipped["max_chars"] == 200
        assert len(clipped["text"]) <= 200
    finally:
        browser_tools.close_session("perception-page-text")


def test_find_ranks_the_requested_field_first(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/form?session=find", "perception-find")
    try:
        result = page_perception.find(driver, "Candidate name")
        assert result["low_confidence"] is False
        assert result["matches"]
        best = result["matches"][0]
        assert best["role"] == "textbox"
        assert best["matched_field"] == "name"
        assert _evaluate(driver, page_perception.ref_expression(best["ref"])) == "candidate-name"

        applied = page_perception.find(driver, "apply", limit=3)
        assert _evaluate(driver, page_perception.ref_expression(applied["matches"][0]["ref"])) == (
            "submit-button"
        )

        by_role = page_perception.find(driver, "role", role="combobox")
        assert by_role["matches"][0]["role"] == "combobox"
        assert _evaluate(driver, page_perception.ref_expression(by_role["matches"][0]["ref"])) == "role"

        missing = page_perception.find(driver, "zzz totally absent control")
        assert missing["low_confidence"] is True
        assert len(missing["matches"]) <= 3
    finally:
        browser_tools.close_session("perception-find")


def test_perception_walks_open_shadow_dom(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/page", "perception-shadow")
    try:
        assert driver.execute_script(SHADOW_FIXTURE) is True

        result = page_perception.outline(driver, format="json")
        nodes = _nodes(result)
        shadow_button = next(
            (node for node in nodes if node["name"] == "Shadow Button"), None
        )
        assert shadow_button is not None
        assert shadow_button["role"] == "button"
        assert _evaluate(driver, page_perception.ref_expression(shadow_button["ref"])) == (
            "shadow-button"
        )

        found = page_perception.find(driver, "Shadow Button")
        assert _evaluate(driver, page_perception.ref_expression(found["matches"][0]["ref"])) == (
            "shadow-button"
        )

        text = page_perception.page_text(driver, mode="full")["text"]
        assert "Text living inside the shadow root." in text

        expression = page_perception.resolve_locator_expression("#widget-host >>> #shadow-button")
        assert expression is not None
        assert _evaluate(driver, expression) == "shadow-button"
    finally:
        browser_tools.close_session("perception-shadow")


def test_closed_shadow_roots_are_counted_when_the_registry_boots_early(local_site):
    url = f"{local_site.base_url}/page"
    driver = _driver_or_skip(url, "perception-closed")
    try:
        registered = page_perception.register_ref_registry(driver)
        if not registered.get("registered"):
            pytest.skip(f"CDP bootstrap unavailable: {registered.get('error')}")
        driver.get(url)  # The bootstrap only applies to documents created afterwards.
        driver.execute_script(
            "const host = document.createElement('div');"
            "document.body.appendChild(host);"
            "host.attachShadow({mode: 'closed'});"
        )
        result = page_perception.outline(driver, format="json")
        assert result["closed_shadow_roots"] >= 1
    finally:
        browser_tools.close_session("perception-closed")


def test_resolve_locator_expression_handles_the_three_locator_forms():
    assert page_perception.resolve_locator_expression("ref:12") == (
        "window.__wsnRefs.nodes.get(12)"
    )
    assert page_perception.ref_expression(7) == "window.__wsnRefs.nodes.get(7)"
    assert page_perception.ref_expression("ref:7") == "window.__wsnRefs.nodes.get(7)"

    piercing = page_perception.resolve_locator_expression("my-app >>> #inner >>> button")
    assert piercing is not None
    assert '"my-app"' in piercing and '"#inner"' in piercing and '"button"' in piercing
    assert "shadowRoot" in piercing and "contentDocument" in piercing

    assert page_perception.resolve_locator_expression("#plain .css > button") is None
    assert page_perception.resolve_locator_expression("input[name='q']") is None
    assert page_perception.resolve_locator_expression("") is None
    assert page_perception.resolve_locator_expression("ref:abc") is None


def test_locator_parsing_never_splices_raw_input_into_javascript():
    expression = page_perception.resolve_locator_expression(
        "a\" >>> b'); alert(1); //"
    )
    assert expression is not None
    assert "alert(1)" not in expression.replace('\\"', "").split("const parts = ")[0]
    assert "\\\"" in expression or "\\u" in expression
    assert expression.startswith("(() => {const parts = [")


def test_outline_descends_into_same_origin_frames_with_page_coordinates(local_site):
    driver = _driver_or_skip(
        f"{local_site.base_url}/fixtures/games/iframe_host.html", "perception-frames"
    )
    try:
        result = page_perception.outline(driver, limit=400, format="json")
        nodes = _nodes(result)
        frame_node = next(node for node in nodes if node["role"] == "iframe")
        assert frame_node["same_origin"] is True
        assert frame_node["src"].endswith("platformer.html")

        canvas = next(node for node in nodes if node["role"] == "canvas")
        assert canvas["frame"] == "#game-frame"
        offset = driver.execute_script("return window.__frameOffset;")
        assert canvas["page_rect"]["x"] == round(canvas["rect"]["x"] + offset["x"])
        assert canvas["page_rect"]["y"] == round(canvas["rect"]["y"] + offset["y"])
        assert canvas["page_rect"]["x"] > canvas["rect"]["x"]

        heading = next(
            node for node in nodes
            if node["role"] == "heading" and node.get("frame") == "#game-frame"
        )
        assert heading["level"] == 1
        assert "platformer fixture" in heading["name"]
    finally:
        browser_tools.close_session("perception-frames")


@pytest.mark.parametrize("bad_format", ["html", "yaml"])
def test_outline_rejects_unknown_formats(bad_format):
    with pytest.raises(ValueError):
        page_perception.outline(object(), format=bad_format)


def test_page_text_rejects_unknown_modes():
    with pytest.raises(ValueError):
        page_perception.page_text(object(), mode="everything")


def test_find_rejects_empty_queries():
    with pytest.raises(ValueError):
        page_perception.find(object(), "   ")
