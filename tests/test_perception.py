from __future__ import annotations

import re

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools
from web_search_neo import page_perception


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
        epoch = result["dom_epoch"]
        assert epoch
        assert result["closed_shadow_roots"] == 0
        assert all(node["ref"].startswith(f"ref:{epoch}:") for node in nodes)
        assert all(re.fullmatch(r"ref:[0-9a-f]+:\d+", node["ref"]) for node in nodes)

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


def test_element_text_reads_overflow_hidden_tails_with_full_text(local_site):
    driver = _driver_or_skip(
        f"{local_site.base_url}/fixtures/perception/overflow_text.html", "element-text-overflow"
    )
    try:
        element = driver.find_element("css selector", "#code")
        invisible = driver.find_element("css selector", "#invisible")

        rendered = page_perception.element_text(driver, element)
        assert rendered["found"] is True
        assert rendered["tag"] == "div"
        assert "line 01 visible" in rendered["text"]

        whole = page_perception.element_text(driver, element, full_text=True)
        assert "line 01 visible" in whole["text"]
        assert "line 15 hidden tail" in whole["text"]
        assert whole["truncated"] is False

        # textContent is the promise of full_text: no rendering filter may drop
        # a node, so even a display:none subtree gives up its text.
        whole_invisible = page_perception.element_text(driver, invisible, full_text=True)
        assert "hidden by display none" in whole_invisible["text"]

        clipped = page_perception.element_text(driver, element, full_text=True, max_chars=200)
        assert clipped["max_chars"] == 200
        assert len(clipped["text"]) <= 200
        assert clipped["truncated"] is True

        markup = page_perception.element_text(driver, element, mode="html")
        assert markup["html"].startswith("line 01 visible")
        assert "line 15 hidden tail" in markup["html"]

        outer = page_perception.element_text(driver, element, mode="outer")
        assert outer["outer_html"].startswith("<div id=\"code\"")
        assert "line 15 hidden tail" in outer["outer_html"]

        both = page_perception.element_text(driver, element, mode="both", full_text=True)
        assert "line 15 hidden tail" in both["text"]
        assert "line 15 hidden tail" in both["html"]
        assert both["outer_html"].startswith("<div id=\"code\"")
    finally:
        browser_tools.close_session("element-text-overflow")


def test_element_text_by_selector_through_browser_tools(local_site):
    session = browser_tools.open_page(
        f"{local_site.base_url}/fixtures/perception/overflow_text.html",
        session_id="element-text-tool",
        width=1024,
        height=768,
        headless=True,
        profile_mode="temporary",
    )
    try:
        result = browser_tools.get_element_text(
            session_id="element-text-tool", selector="#code", full_text=True
        )
        assert result["found"] is True
        assert "line 15 hidden tail" in result["text"]

        missing = browser_tools.get_element_text(
            session_id="element-text-tool", selector="#does-not-exist"
        )
        assert missing["found"] is False
    finally:
        browser_tools.close_session("element-text-tool")


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
    scoped = page_perception.resolve_locator_expression("ref:a1b2c3d4e5f60718:12")
    assert '!== "a1b2c3d4e5f60718"' in scoped
    assert "nodes.get(12)" in scoped
    assert "isConnected" in scoped  # a ref may only answer for a live node
    assert page_perception.parse_ref("ref:a1b2c3d4e5f60718:12") == (
        "a1b2c3d4e5f60718",
        12,
    )
    # The epoch is minted lowercase, so a shouted copy has to be folded, not missed.
    assert page_perception.parse_ref("ref:A1B2C3D4E5F60718:12") == (
        "a1b2c3d4e5f60718",
        12,
    )
    assert page_perception.ref_expression("ref:A1B2C3D4E5F60718:12") == scoped

    piercing = page_perception.resolve_locator_expression("my-app >>> #inner >>> button")
    assert piercing is not None
    assert '"my-app"' in piercing and '"#inner"' in piercing and '"button"' in piercing
    assert "shadowRoot" in piercing and "contentDocument" in piercing

    assert page_perception.resolve_locator_expression("#plain .css > button") is None
    assert page_perception.resolve_locator_expression("input[name='q']") is None
    assert page_perception.resolve_locator_expression("") is None
    assert page_perception.resolve_locator_expression("ref:abc") is None


@pytest.mark.parametrize("handle", ["ref:12", "ref:1", 7])
def test_a_ref_without_an_epoch_is_refused_instead_of_guessed(handle):
    """`ref:N` names a number, and every document numbers its elements from 1."""
    with pytest.raises(ValueError, match="page_outline"):
        page_perception.parse_ref(handle)
    with pytest.raises(ValueError, match="page_outline"):
        page_perception.ref_expression(handle)
    if isinstance(handle, str):
        with pytest.raises(ValueError, match="page_outline"):
            page_perception.resolve_locator_expression(handle)


@pytest.mark.parametrize("selector", ["a >>> ", " >>> a", "a >>>  >>> b", " >>> "])
def test_a_piercing_path_with_an_empty_segment_is_refused(selector):
    with pytest.raises(ValueError, match="empty segment"):
        page_perception.split_piercing_path(selector)
    with pytest.raises(ValueError, match="empty segment"):
        page_perception.resolve_locator_expression(selector)


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


def test_a_ref_from_another_document_never_resolves(local_site):
    """Ref numbers restart at 1 per document, so an old handle must not resolve."""
    driver = _driver_or_skip(f"{local_site.base_url}/form", "perception-epoch")
    try:
        first = page_perception.outline(driver, format="json")
        button = next(
            node for node in _nodes(first) if node.get("name") == "Run action"
        )
        assert _evaluate(driver, page_perception.ref_expression(button["ref"])) == (
            "action-button"
        )

        driver.get(f"{local_site.base_url}/page")
        second = page_perception.outline(driver, format="json")
        assert second["dom_epoch"] != first["dom_epoch"]
        # The new document happily hands out the very same number.
        assert any(
            node["ref"].endswith(":" + button["ref"].rsplit(":", 1)[1])
            for node in _nodes(second)
        )
        assert driver.execute_script(
            f"return {page_perception.ref_expression(button['ref'])};"
        ) is None

        with pytest.raises(ValueError, match="stale"):
            browser_tools._resolve_element(driver, button["ref"])

        # The same number written the legacy way used to resolve happily - to
        # whichever element of the new page happens to hold it.
        number = button["ref"].rsplit(":", 1)[1]
        assert any(node["ref"].endswith(f":{number}") for node in _nodes(second))
        with pytest.raises(ValueError, match="page_outline"):
            browser_tools._resolve_element(driver, f"ref:{number}")
    finally:
        browser_tools.close_session("perception-epoch")


def test_a_ref_whose_element_was_removed_reports_instead_of_resolving(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/form", "perception-detached")
    try:
        found = page_perception.find(driver, "Run action")
        handle = found["matches"][0]["ref"]
        assert browser_tools._resolve_element(driver, handle).get_attribute("id") == (
            "action-button"
        )

        driver.execute_script("document.getElementById('action-button').remove();")
        with pytest.raises(ValueError, match="stale"):
            browser_tools._resolve_element(driver, handle)
    finally:
        browser_tools.close_session("perception-detached")


def test_the_ref_registry_drops_nodes_the_page_threw_away(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/page", "perception-registry")
    try:
        for _ in range(4):
            driver.execute_script(
                "const host = document.createElement('div');"
                "host.id = 'ref-batch';"
                "for (let i = 0; i < 30; i += 1) {"
                "  const button = document.createElement('button');"
                "  button.textContent = 'Batch ' + i;"
                "  host.appendChild(button);"
                "}"
                "document.body.appendChild(host);"
            )
            page_perception.outline(driver, limit=400, format="json")
            driver.execute_script("document.getElementById('ref-batch').remove();")

        page_perception.outline(driver, limit=400, format="json")
        held = driver.execute_script("return window.__wsnRefs.nodes.size;")
        detached = driver.execute_script(
            "let count = 0;"
            "for (const node of window.__wsnRefs.nodes.values()) {"
            "  if (!node.isConnected) count += 1;"
            "}"
            "return count;"
        )
        assert detached == 0, f"{detached} detached nodes are still held"
        assert held < 60, f"the registry kept {held} nodes for a page with a few"
    finally:
        browser_tools.close_session("perception-registry")


@pytest.mark.parametrize(
    "selector",
    [
        "div[data-op='a >>> b']",
        '[aria-label="Reports >>> Q4"]',
        "a[href$='>>>']",
    ],
)
def test_valid_css_containing_the_separator_is_not_a_piercing_path(selector):
    assert page_perception.resolve_locator_expression(selector) is None
    assert page_perception.split_piercing_path(selector) is None


def test_a_quoted_separator_still_resolves_as_plain_css(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/page", "perception-quoted")
    try:
        driver.execute_script(
            "document.body.insertAdjacentHTML('beforeend',"
            "  '<button id=quoted-target data-op=\"a &gt;&gt;&gt; b\">Quoted</button>');"
        )
        selector = 'button[data-op="a >>> b"]'
        assert driver.find_element("css selector", selector).get_attribute("id") == (
            "quoted-target"
        )
        element = browser_tools._resolve_element(driver, selector)
        assert element.get_attribute("id") == "quoted-target"
    finally:
        browser_tools.close_session("perception-quoted")


def test_main_mode_falls_back_instead_of_returning_an_empty_page(local_site):
    """A registration page is all <form>, which mode='main' treats as chrome."""
    driver = _driver_or_skip(f"{local_site.base_url}/form?session=fallback", "perception-form")
    try:
        driver.execute_script(
            "document.querySelectorAll('a, p, button:not(#submit-button)')"
            "  .forEach(node => node.remove());"
        )
        full = page_perception.page_text(driver, mode="full")
        assert "Candidate name" in full["text"]
        assert full["fallback_used"] is False
        assert full["mode_used"] == "full"

        main = page_perception.page_text(driver, mode="main")
        assert main["chars"] > 0, "mode='main' returned an empty page with no error"
        assert "Candidate name" in main["text"]
        assert main["fallback_used"] is True
        assert main["mode_used"] == "full"
        assert main["mode"] == "main"
    finally:
        browser_tools.close_session("perception-form")


def test_page_text_keeps_links_inside_its_own_budget(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/page", "perception-budget")
    try:
        driver.execute_script(
            "for (let i = 0; i < 40; i += 1) {"
            "  document.body.insertAdjacentHTML('beforeend',"
            "    '<p><a href=\"/target-number-' + i + '\">Link number ' + i + '</a></p>');"
            "}"
        )
        result = page_perception.page_text(driver, max_chars=400, include_links=True)
        assert len(result["text"]) <= 400
        assert result["chars"] == len(result["text"])
        assert result["truncated"] is True
        # Every marker left in the text still has its URL listed, and nothing else.
        assert result["links"]
        markers = {int(item) for item in re.findall(r"\[(\d+)\]", result["text"])}
        listed = {int(link["index"]) for link in result["links"]}
        assert listed == markers
    finally:
        browser_tools.close_session("perception-budget")


def test_page_text_does_not_repeat_slotted_content(local_site):
    driver = _driver_or_skip(f"{local_site.base_url}/page", "perception-slot")
    try:
        driver.execute_script(
            "const host = document.createElement('div');"
            "host.innerHTML = '<span>SLOTTED-MARKER-ONCE</span>';"
            "document.body.appendChild(host);"
            "host.attachShadow({mode: 'open'}).innerHTML ="
            "  '<section>SHADOWLEAD<slot></slot></section>';"
        )
        text = page_perception.page_text(driver, mode="full")["text"]
        assert text.count("SLOTTED-MARKER-ONCE") == 1, text
        # Slotted light-DOM text carries no whitespace of its own, so the shadow
        # tree's last word would otherwise run straight into it.
        assert "SHADOWLEAD SLOTTED-MARKER-ONCE" in text, text
    finally:
        browser_tools.close_session("perception-slot")


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
