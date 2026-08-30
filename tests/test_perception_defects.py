"""Regressions for perception answers that were confident and wrong.

Every test here reproduces a defect where the tool answered without hedging and
the answer was false: a ref no action could ever use, a frame path that opened a
different document, a sub-tree returned as the whole page, a hidden control
ranked above the real one, and a page of controls reported as empty. An agent
cannot recover from those on its own, because nothing in the result says the
result is wrong.
"""
from __future__ import annotations

import time

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from web_search_neo import browser_tools
from web_search_neo import page_perception


def _open_or_skip(path: str, session_id: str):
    try:
        browser_tools.open_page(
            path,
            session_id=session_id,
            width=1024,
            height=768,
            headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    return browser_tools._get_session(session_id).driver


def _fixture(local_site, name: str) -> str:
    return f"{local_site.base_url}/fixtures/perception/{name}"


def _nodes(result: dict) -> list[dict]:
    return [node for node in result["nodes"] if node.get("kind") == "node"]


def _named(result: dict, name: str) -> dict:
    return next(node for node in _nodes(result) if node.get("name") == name)


def _settle(driver, expression: str, timeout: float = 5.0) -> bool:
    """Wait for a frame that only starts loading once its host has been parsed.

    The probe returns a boolean on purpose: handing a node from a nested document
    back to the driver is the very thing these tests are about.
    """
    deadline = time.monotonic() + timeout
    while True:
        ready = bool(
            driver.execute_script(
                f"try {{ return !!({expression}); }} catch (error) {{ return false; }}"
            )
        )
        if ready or time.monotonic() >= deadline:
            return ready
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# A ref read inside a frame has to be actionable, and unreachable has to say why
# ---------------------------------------------------------------------------


def test_a_ref_read_inside_a_frame_can_be_clicked(local_site):
    driver = _open_or_skip(_fixture(local_site, "checkout.html"), "defect-frame-click")
    try:
        outline = page_perception.outline(driver, format="json", limit=400)
        button = _named(outline, "Confirm payment")
        assert button["frame"] == "#pay"

        result = browser_tools.click(button["ref"], session_id="defect-frame-click")
        assert result["success"] is True
        state = driver.execute_script(
            "return document.getElementById('pay').contentDocument"
            ".getElementById('pay-state').textContent;"
        )
        assert state == "paid", "the click never reached the element the outline offered"
        # The action is over; everything after it is about the page again.
        assert result["title"] == "Checkout"
        assert driver.execute_script("return document.title;") == "Checkout"
    finally:
        browser_tools.close_session("defect-frame-click")


def test_find_inside_a_frame_mints_a_ref_the_action_tools_accept(local_site):
    """The documented workaround handed back a ref that click called stale."""
    driver = _open_or_skip(_fixture(local_site, "checkout.html"), "defect-frame-find")
    try:
        found = browser_tools.find_elements(
            "Confirm payment", session_id="defect-frame-find", frame_selector="#pay"
        )
        handle = found["matches"][0]["ref"]
        assert browser_tools.click(handle, session_id="defect-frame-find")["success"] is True
        assert driver.execute_script(
            "return document.getElementById('pay').contentDocument"
            ".getElementById('pay-state').textContent;"
        ) == "paid"
    finally:
        browser_tools.close_session("defect-frame-find")


def test_a_piercing_path_can_fill_a_field_inside_a_frame(local_site):
    driver = _open_or_skip(_fixture(local_site, "checkout.html"), "defect-frame-fill")
    try:
        result = browser_tools.fill_fields(
            {"#pay >>> #leaf-field": "Ada Lovelace"}, session_id="defect-frame-fill"
        )
        assert result["errors"] == {}
        assert result["success"] is True
        assert driver.execute_script(
            "return document.getElementById('pay').contentDocument"
            ".getElementById('leaf-field').value;"
        ) == "Ada Lovelace"
    finally:
        browser_tools.close_session("defect-frame-fill")


def test_a_handle_whose_frame_is_gone_fails_at_once_and_names_the_reason(local_site):
    driver = _open_or_skip(_fixture(local_site, "checkout.html"), "defect-frame-gone")
    try:
        outline = page_perception.outline(driver, format="json", limit=400)
        handle = _named(outline, "Confirm payment")["ref"]
        driver.execute_script("document.getElementById('pay').remove();")

        started = time.monotonic()
        with pytest.raises(ValueError, match="no longer open") as failure:
            browser_tools.click(handle, session_id="defect-frame-gone")
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"spent {elapsed:.1f}s polling for a document that cannot return"
        assert isinstance(failure.value, browser_tools.LocatorGone)
    finally:
        browser_tools.close_session("defect-frame-gone")


def test_a_ref_does_not_survive_the_frame_that_held_it(local_site):
    """`isConnected` stays true inside a removed frame's orphaned document."""
    driver = _open_or_skip(_fixture(local_site, "checkout.html"), "defect-detached-frame")
    try:
        page_perception.outline(driver, format="json")  # boots the registry
        # Registered by hand into the top document's registry, which is what the
        # outline itself used to do for every node it found inside a frame.
        handle = driver.execute_script(
            "const registry = window.__wsnRefs;"
            "const node = document.getElementById('pay').contentDocument"
            ".getElementById('leaf-button');"
            "const id = registry.next;"
            "registry.next = id + 1;"
            "registry.nodes.set(id, node);"
            "registry.byNode.set(node, id);"
            "return 'ref:' + registry.epoch + ':' + id;"
        )
        expression = page_perception.ref_expression(handle)
        assert driver.execute_script(f"return {expression} !== null;") is True

        driver.execute_script("document.getElementById('pay').remove();")
        assert driver.execute_script(
            "return window.__wsnRefs.nodes.get("
            + handle.rsplit(":", 1)[1]
            + ").isConnected;"
        ) is True, "the premise of this test changed: the node reports itself detached now"
        # Asked as a boolean: resolving it would hand back a node the driver
        # itself refuses, which is a failure with a much less obvious cause.
        assert driver.execute_script(f"return {expression} === null;") is True
    finally:
        browser_tools.close_session("defect-detached-frame")


# ---------------------------------------------------------------------------
# A frame path must open that frame, or say that it cannot
# ---------------------------------------------------------------------------


def test_the_frame_path_the_outline_reports_opens_that_frame(local_site):
    driver = _open_or_skip(_fixture(local_site, "frame_collision.html"), "defect-frame-path")
    try:
        _settle(
            driver,
            "document.getElementById('widget').shadowRoot.querySelector('iframe')"
            ".contentDocument.getElementById('leaf-button')",
        )
        outline = page_perception.outline(driver, format="json", limit=400)
        payment = _named(outline, "Confirm payment")
        advert = _named(outline, "Close ad")
        assert payment["frame"] != advert["frame"], "two frames, one path"

        landed = browser_tools.get_page_text(
            session_id="defect-frame-path", frame_selector=payment["frame"], mode="full"
        )
        assert "LEAF-TEXT" in landed["text"]
        assert "ADS-TEXT" not in landed["text"]
    finally:
        browser_tools.close_session("defect-frame-path")


def test_an_ambiguous_frame_selector_is_refused_instead_of_guessed(local_site):
    _open_or_skip(_fixture(local_site, "two_frames.html"), "defect-frame-ambiguous")
    try:
        with pytest.raises(ValueError, match="matches 2"):
            browser_tools.get_page_text(
                session_id="defect-frame-ambiguous", frame_selector="iframe"
            )
    finally:
        browser_tools.close_session("defect-frame-ambiguous")


def test_a_nested_frame_path_is_readable(local_site):
    driver = _open_or_skip(_fixture(local_site, "nested_host.html"), "defect-frame-nested")
    try:
        _settle(
            driver,
            "document.getElementById('outer').contentDocument"
            ".getElementById('inner').contentDocument.getElementById('leaf-button')",
        )
        outline = page_perception.outline(driver, format="json", limit=400)
        button = _named(outline, "Confirm payment")
        assert button["frame"] == "#outer >>> #inner"

        landed = browser_tools.get_page_text(
            session_id="defect-frame-nested", frame_selector=button["frame"], mode="full"
        )
        assert "LEAF-TEXT" in landed["text"]
        assert browser_tools.click(button["ref"], session_id="defect-frame-nested")["success"]
    finally:
        browser_tools.close_session("defect-frame-nested")


# ---------------------------------------------------------------------------
# page_text must not return a sub-tree as if it were the page
# ---------------------------------------------------------------------------


def test_full_mode_returns_the_whole_body_not_the_app_shell(local_site):
    driver = _open_or_skip(_fixture(local_site, "spa_shell.html"), "defect-text-full")
    try:
        result = page_perception.page_text(driver, mode="full")
        assert "REAL-BODY" in result["text"]
        assert "Invoice 4471" in result["text"]
        assert result["root_reason"] == "body"
        assert result["excluded_chars"] == 0

        # mode='full' never asks wsnPickRoot anything, so the guard that stops a
        # spinner shell from answering for the page is only exercised here.
        narrowed = page_perception.page_text(driver, mode="main")
        assert narrowed["text"] != "Loading..."
        assert "REAL-BODY" in narrowed["text"], (
            "the <main> holding a spinner was taken for the main content"
        )
        assert narrowed["root_reason"] != "main"
    finally:
        browser_tools.close_session("defect-text-full")


def test_main_mode_says_how_much_of_the_page_it_left_out(local_site):
    driver = _open_or_skip(_fixture(local_site, "chrome_page.html"), "defect-text-main")
    try:
        main = page_perception.page_text(driver, mode="main")
        assert "MAIN-BODY" in main["text"]
        assert "NAV-LINKS" not in main["text"]
        assert main["excluded_chars"] > 0
        assert any("mode='full'" in reason for reason in main["excluded"])
        assert main["body_chars"] > main["excluded_chars"]

        whole = page_perception.page_text(driver, mode="full")
        assert "NAV-LINKS" in whole["text"] and "FOOTER-NOTICE" in whole["text"]
    finally:
        browser_tools.close_session("defect-text-main")


def test_frame_text_is_counted_in_the_same_universe_as_the_body(local_site):
    """A frame's words are not in body.innerText, so the loss clamped to zero."""
    driver = _open_or_skip(
        _fixture(local_site, "chrome_with_frame.html"), "defect-text-universe"
    )
    try:
        main = page_perception.page_text(driver, mode="main")
        assert "MAIN-BODY" in main["text"]
        assert "FRAME-BODY" in main["text"], "the frame inside <main> is part of it"
        assert "NAV-LINKS" not in main["text"] and "FOOTER-NOTICE" not in main["text"]
        # The frame contributes more text than the chrome that was dropped, so a
        # count taken against the top body alone goes negative and clamps to zero.
        assert main["frames"]["chars"] > main["body_chars"]
        assert main["excluded_chars"] > 0, (
            "the navigation and the footer were dropped and reported as nothing"
        )
        assert main["excluded"], "a silent loss is the whole defect"
        assert main["frames"]["same_origin"] == 1
    finally:
        browser_tools.close_session("defect-text-universe")


def test_a_dialog_inside_a_dialog_is_read_once(local_site):
    driver = _open_or_skip(_fixture(local_site, "nested_dialogs.html"), "defect-text-nested")
    try:
        result = page_perception.page_text(driver, mode="main")
        assert result["text"].count("INNER-ALERT") == 1
        assert result["text"].count("OUTER-WALL") == 1
        assert result["dialogs_appended"] == 1, "one overlay, not two"
    finally:
        browser_tools.close_session("defect-text-nested")


def test_an_index_of_articles_is_not_reduced_to_its_first_post(local_site):
    driver = _open_or_skip(_fixture(local_site, "articles.html"), "defect-text-articles")
    try:
        for mode in ("main", "full"):
            text = page_perception.page_text(driver, mode=mode)["text"]
            for marker in ("ARTICLE-ONE", "ARTICLE-TWO", "ARTICLE-THREE"):
                assert marker in text, f"mode={mode} dropped {marker}"
    finally:
        browser_tools.close_session("defect-text-articles")


def test_page_text_reads_the_frame_that_is_the_whole_page(local_site):
    driver = _open_or_skip(_fixture(local_site, "iframe_only.html"), "defect-text-frame")
    try:
        result = page_perception.page_text(driver, mode="full")
        assert "LEAF-TEXT" in result["text"]
        assert result["frames"]["same_origin"] == 1
        assert result["frames"]["cross_origin"] == 0
        assert result["chars"] > 0
    finally:
        browser_tools.close_session("defect-text-frame")


def test_an_open_dialog_is_not_dropped_by_main_mode(local_site):
    driver = _open_or_skip(_fixture(local_site, "dialog_page.html"), "defect-text-dialog")
    try:
        result = page_perception.page_text(driver, mode="main")
        assert result["root_tag"] == "main", "the landmark must still be the pick here"
        assert "MAIN-BODY" in result["text"]
        assert "COOKIE-WALL" in result["text"], "the modal standing in front of the page"
        assert result["dialogs_appended"] == 1
    finally:
        browser_tools.close_session("defect-text-dialog")


# ---------------------------------------------------------------------------
# find and outline must not report different pages
# ---------------------------------------------------------------------------


def test_find_skips_aria_hidden_subtrees_exactly_as_the_outline_does(local_site):
    driver = _open_or_skip(_fixture(local_site, "aria_hidden.html"), "defect-find-hidden")
    try:
        outline = page_perception.outline(driver, format="json")
        offered = {node["name"] for node in _nodes(outline)}
        assert "Buy now" not in offered and "Buy now (real)" in offered

        found = page_perception.find(driver, "Buy now", limit=5)
        best = found["matches"][0]
        assert driver.execute_script(
            f"const target = {page_perception.ref_expression(best['ref'])};"
            "return target ? target.id : null;"
        ) == "real"
        for match in found["matches"]:
            resolved = driver.execute_script(
                f"const target = {page_perception.ref_expression(match['ref'])};"
                "return target ? target.id : null;"
            )
            assert resolved not in {"ghost", "stashed"}
        assert found["aria_hidden_skipped"] >= 2
    finally:
        browser_tools.close_session("defect-find-hidden")


def test_find_says_it_is_guessing_when_nothing_answers_the_query(local_site):
    """The flag used to measure whether the element was clickable, not relevant."""
    driver = _open_or_skip(_fixture(local_site, "single_button.html"), "defect-find-guess")
    try:
        guess = page_perception.find(driver, "Submit order")
        assert guess["low_confidence"] is True
        assert guess["matches"], "the closest thing on the page is still worth showing"

        # Nothing cleared the bar, so `matched` is 0 while the guesses still come
        # back: the one case where `returned` is allowed to exceed `matched`.
        assert guess["matched"] == 0
        assert guess["returned"] == len(guess["matches"]) > 0
        assert guess["truncated"] is False

        best = guess["matches"][0]
        assert best["name"] == "Cancel"
        assert best["match_score"] < guess["match_threshold"], (
            "nothing on this page matched the query"
        )
        # The ranking score cleared the old bar of 25 on context alone: being in
        # the viewport, looking actionable and being enabled are worth 36 points
        # between them, and that is what used to be called confidence.
        assert best["score"] > guess["match_threshold"]

        answered = page_perception.find(driver, "Cancel")
        assert answered["low_confidence"] is False
        assert answered["matches"][0]["match_score"] == 100
        assert answered["matches"][0]["matched_field"] == "name"
    finally:
        browser_tools.close_session("defect-find-guess")


def test_a_role_filter_is_a_filter_and_not_a_preference(local_site):
    driver = _open_or_skip(_fixture(local_site, "single_button.html"), "defect-find-role")
    try:
        wrong_role = page_perception.find(driver, "Cancel", role="link")
        assert wrong_role["low_confidence"] is True, (
            "a button answered a question about a link, and said it was sure"
        )
        right_role = page_perception.find(driver, "Cancel", role="button")
        assert right_role["low_confidence"] is False
        assert right_role["matches"][0]["role"] == "button"
    finally:
        browser_tools.close_session("defect-find-role")


def test_find_flags_a_coin_flip_between_two_equally_good_matches(local_site):
    driver = _open_or_skip(
        _fixture(local_site, "duplicate_buttons.html"), "defect-find-ambiguous"
    )
    try:
        result = page_perception.find(driver, "Download report")
        assert result.get("ambiguous") is True, (
            "document order picked the winner and nothing said so"
        )
        assert result["low_confidence"] is False, "both are good matches; that is the point"
        first, second = result["matches"][0], result["matches"][1]
        assert first["match_score"] == second["match_score"]
        assert first["score"] == second["score"]

        # The caller who asked for exactly one answer is the one who most needs
        # to know the second one was just as good.
        alone = page_perception.find(driver, "Download report", limit=1)
        assert alone.get("ambiguous") is True
        assert alone["returned"] == 1 and alone["matched"] == 2

        driver.get(_fixture(local_site, "single_button.html"))
        settled = page_perception.find(driver, "Cancel")
        assert settled["ambiguous"] is False
    finally:
        browser_tools.close_session("defect-find-ambiguous")


def test_find_scores_the_words_a_person_can_actually_see(local_site):
    """An aria-label took the accessible name and hid the button's own text."""
    driver = _open_or_skip(_fixture(local_site, "labelled_button.html"), "defect-find-label")
    try:
        result = page_perception.find(driver, "Send message")
        assert result["low_confidence"] is False, (
            "the one right answer on the page was returned and called a guess"
        )
        assert result["matched"] >= 1
        best = result["matches"][0]
        assert best["matched_field"] == "text"
        assert best["match_score"] >= result["match_threshold"]
        assert driver.execute_script(
            f"const target = {page_perception.ref_expression(best['ref'])};"
            "return target ? target.id : null;"
        ) == "send"

        # The accessible name still answers for itself.
        by_label = page_perception.find(driver, "Compose a new support request")
        assert by_label["low_confidence"] is False
        assert by_label["matches"][0]["matched_field"] == "name"
    finally:
        browser_tools.close_session("defect-find-label")


def test_find_says_how_many_matched_when_the_limit_cut_the_list(local_site):
    driver = _open_or_skip(_fixture(local_site, "many_buttons.html"), "defect-find-counts")
    try:
        five = page_perception.find(driver, "Delete item", limit=5)
        assert five.get("matched") == 30, "the page scored thirty and reported five"
        assert five["returned"] == 5
        assert len(five["matches"]) == 5
        assert five["truncated"] is True
        assert five["scored"] >= five["matched"]
        assert five["candidates"] >= five["scored"]

        # The cap on `limit` is part of what the caller has to know about.
        capped = page_perception.find(driver, "Delete item", limit=100)
        assert capped["returned"] == 25
        assert capped["truncated"] is True
        assert capped["matched"] == 30
    finally:
        browser_tools.close_session("defect-find-counts")


# ---------------------------------------------------------------------------
# page_elements is the first call the recipes make
# ---------------------------------------------------------------------------


def test_page_elements_reaches_shadow_dom_and_its_selectors_work(local_site):
    driver = _open_or_skip(_fixture(local_site, "web_component.html"), "defect-elements-shadow")
    try:
        elements = browser_tools.get_page_elements(session_id="defect-elements-shadow")
        buttons = {button["text"]: button for button in elements["buttons"]}
        assert "Pay now" in buttons, "a page of controls was reported as having none"
        assert elements["links"] and elements["fields"] and elements["forms"]

        selector = buttons["Pay now"]["selector"]
        assert " >>> " in selector
        assert browser_tools.click(selector, session_id="defect-elements-shadow")["success"]
        assert driver.execute_script(
            "return document.querySelector('my-app').shadowRoot"
            ".getElementById('shadow-state').textContent;"
        ) == "clicked"

        filled = browser_tools.fill_fields(
            {elements["fields"][0]["selector"]: "Grace"}, session_id="defect-elements-shadow"
        )
        assert filled["errors"] == {}
    finally:
        browser_tools.close_session("defect-elements-shadow")


def test_a_selector_names_the_document_it_belongs_to(local_site):
    """Two documents both call their button #save; one of them deletes everything."""
    driver = _open_or_skip(_fixture(local_site, "frame_twins.html"), "defect-elements-frame")
    try:
        elements = browser_tools.get_page_elements(session_id="defect-elements-frame")
        by_text = {button["text"]: button for button in elements["buttons"]}
        assert set(by_text) == {"Delete everything", "Save draft"}
        assert by_text["Delete everything"]["selector"] == "#save"
        assert by_text["Save draft"]["selector"] == "#editor >>> #save", (
            "a bare #save from inside the frame addresses the top document's button"
        )

        clicked = browser_tools.click(
            by_text["Save draft"]["selector"], session_id="defect-elements-frame"
        )
        assert clicked["success"] is True
        assert driver.execute_script(
            "return document.getElementById('editor').contentDocument"
            ".getElementById('frame-log').textContent;"
        ) == "saved"
        assert driver.execute_script(
            "return document.getElementById('top-log').textContent;"
        ) == "intact", "clicking Save draft fired the top document's Delete everything"
    finally:
        browser_tools.close_session("defect-elements-frame")


def test_an_id_less_child_of_a_shadow_root_still_gets_a_selector(local_site):
    driver = _open_or_skip(_fixture(local_site, "shadow_twins.html"), "defect-elements-twins")
    try:
        elements = browser_tools.get_page_elements(session_id="defect-elements-twins")
        by_text = {button["text"]: button for button in elements["buttons"]}
        assert set(by_text) == {"Accept", "Decline"}
        for button in by_text.values():
            assert button["selector"], "reported as usable with nothing to use"

        assert browser_tools.click(
            by_text["Decline"]["selector"], session_id="defect-elements-twins"
        )["success"]
        assert driver.execute_script(
            "return document.getElementById('host').shadowRoot"
            ".getElementById('choice').textContent;"
        ) == "declined"
    finally:
        browser_tools.close_session("defect-elements-twins")


def test_a_frame_selector_naming_a_non_frame_gives_the_driver_back(local_site):
    driver = _open_or_skip(_fixture(local_site, "checkout.html"), "defect-frame-release")
    try:
        outline = page_perception.outline(driver, format="json", limit=400)
        inner = _named(outline, "Confirm payment")["ref"]

        with pytest.raises(ValueError, match="not a frame"):
            browser_tools.get_page_text(
                session_id="defect-frame-release", frame_selector=inner
            )

        # The failure must not leave the session inside the frame, or the next
        # read answers for that frame and presents it as the page.
        after = browser_tools.get_page_elements(session_id="defect-frame-release")
        assert after["title"] == "Checkout"
        assert driver.execute_script("return document.title;") == "Checkout"

        # page_elements takes no frame_selector, so it answers for the whole page
        # whatever anyone else left selected.
        driver.switch_to.frame(driver.find_element("css selector", "#pay"))
        whole = browser_tools.get_page_elements(session_id="defect-frame-release")
        assert whole["title"] == "Checkout"
        assert any(button["text"] == "Confirm payment" for button in whole["buttons"])
    finally:
        browser_tools.close_session("defect-frame-release")


def test_a_deep_ref_is_never_called_gone_while_its_document_is_open(local_site):
    """The resolver searches deeper than any topic walks, so a minted ref resolves."""
    driver = _open_or_skip(_fixture(local_site, "deep_frames.html"), "defect-frame-deepref")
    try:
        _settle(
            driver,
            "document.querySelector('#root iframe').contentDocument"
            ".querySelector('#host iframe')",
        )
        outline = page_perception.outline(driver, format="json", limit=600)
        deep = [
            node for node in _nodes(outline)
            if node.get("frame") and node.get("role") == "iframe"
        ]
        assert deep, "the outline reported no nested frames at all"
        # Whatever the outline was willing to mint, the action tools must reach.
        handle = deep[-1]["ref"]
        assert browser_tools._resolve_element(driver, handle) is not None
    finally:
        browser_tools._leave_element_frame(driver)
        browser_tools.close_session("defect-frame-deepref")


def test_every_topic_stops_at_the_same_frame_depth(local_site):
    """One topic reaching deeper than the rest hands out refs the others deny."""
    driver = _open_or_skip(_fixture(local_site, "deep_frames.html"), "defect-frame-depth")
    try:
        _settle(
            driver,
            "document.querySelector('#root iframe').contentDocument"
            ".querySelector('#host iframe')",
        )
        outline = page_perception.outline(driver, format="json", limit=600)
        found = page_perception.find(driver, "Deepest button", limit=5)
        elements = browser_tools.get_page_elements(session_id="defect-frame-depth")
        text = page_perception.page_text(driver, mode="full")

        seen_by_outline = any(
            node.get("name") == "Deepest button" for node in _nodes(outline)
        )
        seen_by_elements = any(
            button["text"] == "Deepest button" for button in elements["buttons"]
        )
        seen_by_find = found["matched"] > 0
        assert seen_by_outline == seen_by_elements == seen_by_find, (
            f"outline={seen_by_outline} page_elements={seen_by_elements} "
            f"find={seen_by_find} disagree about the same button"
        )

        # Nine frames, a shared bound of eight: everyone reads eight and says so.
        assert text["frames"]["same_origin"] == page_perception.MAX_FRAME_DEPTH
        assert text["frames"]["too_deep"] == 1
        assert elements["frames_too_deep"] >= 1
        assert found["frames_too_deep"] >= 1
        assert outline["frames"]["too_deep"] == 1
        assert any(str(page_perception.MAX_FRAME_DEPTH) in reason for reason in text["excluded"])
    finally:
        browser_tools.close_session("defect-frame-depth")


def test_page_elements_marks_controls_no_click_can_reach(local_site):
    _open_or_skip(_fixture(local_site, "web_component.html"), "defect-elements-hidden")
    try:
        elements = browser_tools.get_page_elements(session_id="defect-elements-hidden")
        by_id = {button["id"]: button for button in elements["buttons"]}
        # This one was always listed; it was listed as if it were clickable.
        assert by_id["light-stale"]["visible"] is False
        assert by_id["light-stale"]["hidden_reason"] == "visibility-hidden"
        assert by_id["pay"]["visible"] is True
        assert by_id["pay"]["hidden_reason"] == ""
        assert by_id["offscreen"]["visible"] is False
        assert by_id["offscreen"]["hidden_reason"] == "display-none"
        assert by_id["decorative"]["visible"] is False
        assert by_id["decorative"]["hidden_reason"] == "aria-hidden"
        # Whatever the limit drops, it must not be the control that works.
        assert elements["buttons"][0]["id"] == "pay"
        assert elements["found"]["buttons"] == 4
        assert elements["truncated"] is False
    finally:
        browser_tools.close_session("defect-elements-hidden")


def test_page_elements_paginates_the_whole_dom_and_scroll_materialises_lazy_controls(
    local_site,
):
    _open_or_skip(
        _fixture(local_site, "scroll_and_many.html"), "defect-elements-pagination"
    )
    try:
        first = browser_tools.get_page_elements(
            session_id="defect-elements-pagination",
            include_links=False,
            include_forms=False,
            limit=1000,
            offset=0,
            max_chars=browser_tools.MAX_RESPONSE_CHAR_BUDGET,
        )
        assert first["found"]["buttons"] == 1206
        assert first["returned"]["buttons"] == 1000
        assert first["range"]["buttons"] == {
            "start": 0,
            "end": 1000,
            "next_offset": 1000,
            "has_more": True,
        }
        assert first["collector_truncated"]["buttons"] is False
        below = next(button for button in first["buttons"] if button["id"] == "below-fold")
        assert below["visible"] is True  # rendered below the viewport is still in the DOM
        assert not any(button["id"] == "lazy-button" for button in first["buttons"])

        second = browser_tools.get_page_elements(
            session_id="defect-elements-pagination",
            include_links=False,
            include_forms=False,
            limit=1000,
            offset=1000,
            max_chars=browser_tools.MAX_RESPONSE_CHAR_BUDGET,
        )
        assert second["returned"]["buttons"] == 206
        assert second["range"]["buttons"]["next_offset"] is None
        ids = {button["id"] for button in first["buttons"] + second["buttons"]}
        assert len(ids) == 1206

        scrolled = browser_tools.scroll_page(
            700,
            session_id="defect-elements-pagination",
            wait_seconds=0.2,
            include_summary=False,
        )
        assert scrolled["after"]["scroll_y"] > scrolled["before"]["scroll_y"]
        after = browser_tools.get_page_elements(
            session_id="defect-elements-pagination",
            include_links=False,
            include_forms=False,
            limit=1000,
            offset=0,
            max_chars=browser_tools.MAX_RESPONSE_CHAR_BUDGET,
        )
        assert after["found"]["buttons"] == 1207
        last = browser_tools.get_page_elements(
            session_id="defect-elements-pagination",
            include_links=False,
            include_forms=False,
            limit=1000,
            offset=1000,
            max_chars=browser_tools.MAX_RESPONSE_CHAR_BUDGET,
        )
        assert any(button["id"] == "lazy-button" for button in last["buttons"])
    finally:
        browser_tools.close_session("defect-elements-pagination")

def test_a_thousand_controls_do_not_come_back_as_a_hundred_thousand_characters(
    local_site,
):
    """The defect this budget exists for: a real page answered `page_elements`
    with 83,616 characters, which the model that asked could not receive."""
    _open_or_skip(
        _fixture(local_site, "scroll_and_many.html"), "defect-elements-budget"
    )
    try:
        result = browser_tools.get_page_elements(
            session_id="defect-elements-budget",
            include_links=False,
            include_forms=False,
            limit=1000,
        )
        assert result["budget_truncated"] is True
        assert result["chars_returned"] <= result["char_budget"]
        assert result["chars_before_budget"] > result["char_budget"]
        # And the paging still works from where the budget stopped.
        kept = len(result["buttons"])
        assert result["range"]["buttons"]["next_offset"] == kept
        rest = browser_tools.get_page_elements(
            session_id="defect-elements-budget",
            include_links=False,
            include_forms=False,
            limit=1000,
            offset=kept,
        )
        assert rest["buttons"][0]["id"] != result["buttons"][0]["id"]
    finally:
        browser_tools.close_session("defect-elements-budget")



# ---------------------------------------------------------------------------
# The console topic's own note about itself
# ---------------------------------------------------------------------------


def test_the_console_note_matches_what_this_backend_actually_captured(local_site):
    """The note used to ask for a reload that captures nothing new."""
    _open_or_skip(f"{local_site.base_url}/boot-log?marker=note", "defect-console-note")
    try:
        payload = browser_tools.get_console("defect-console-note", limit=200)
        hooked = {item["text"] for item in payload["entries"] if item["kind"] == "console"}
        assert {"boot-log-note", "boot-error-note"} <= hooked, (
            "load-time output is missing, so a note promising it would be the lie"
        )
        assert "no reload is needed" in payload["note"]
        assert "reload the page" not in payload["note"].lower()
    finally:
        browser_tools.close_session("defect-console-note")


# ---------------------------------------------------------------------------
# A reported centre has to be a place the click actually goes
# ---------------------------------------------------------------------------

# Each entry is one frame in transformed_frames.html and the shape between its
# own coordinates and the page. The perception layer used to translate a frame's
# boxes by its content-box origin alone, so everything here but a plain offset
# came out somewhere the button is not - and the ones that still landed on it did
# so only because the button was big enough to absorb the error.
_FRAME_SHAPES = [
    ("#plain", "no transform"),
    ("#xrot", "transform: rotate(12deg)"),
    ("#xscl", "transform: scale(0.6)"),
    ("#xtra", "transform: translate(30px, 12px)"),
    ("#prot", "the rotate property"),
    ("#pscl", "the scale property"),
    ("#ptra", "the translate property"),
    ("#zoomed", "zoom: 0.7"),
    ("#anc", "a transformed ancestor"),
    ("#innerxf", "a transformed target inside a plain frame"),
    ("#persp", "a perspective chain"),
    ("#outer >>> #deep", "a frame nested inside a transformed frame"),
    ("#xsclrot", "transform: scale(0.6) rotate(8deg)"),
]

# The aim target's own untransformed centre, in its own document's coordinates.
_AIM_CENTRE = (47, 45)

_AIM_READY = """(() => {
  const ready = win => {
    for (const frame of win.document.querySelectorAll('iframe')) {
      if (!frame.contentWindow || !frame.contentWindow.__aim) return false;
      if (!ready(frame.contentWindow)) return false;
    }
    return true;
  };
  return ready(window);
})()"""

_AIM_RESET = """
const blank = () => ({verdict: 'none', hits: 0, x: null, y: null});
const walk = win => {
  try { win.__aim = blank(); } catch (error) { return; }
  for (const frame of win.document.querySelectorAll('iframe')) {
    try { if (frame.contentWindow) walk(frame.contentWindow); } catch (error) {}
  }
};
walk(window);
"""


def _aim_verdict(driver, frame_path: str) -> str:
    """What the page itself says about the click: which element received it.

    Read from the document that owns the button, because that is the only party
    that knows. Every other answer here - the tool's own centre, the tool's own
    idea of where the frame is - is the thing under test.
    """
    hops = [hop.strip() for hop in frame_path.split(">>>")]
    driver.switch_to.default_content()
    verdict = driver.execute_script("return window.__aim.verdict;")
    try:
        for hop in hops:
            driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, hop))
            record = driver.execute_script("return window.__aim;")
            if record["verdict"] != "none":
                verdict = record["verdict"]
    finally:
        driver.switch_to.default_content()
    return verdict


def _aim_report(driver, session_id: str, centres: dict[str, dict]) -> list[str]:
    """Click every reported centre and collect the frames that did not receive it."""
    failures: list[str] = []
    for frame_path, shape in _FRAME_SHAPES:
        centre = centres.get(frame_path)
        if centre is None:
            failures.append(f"{shape} ({frame_path}): no button was reported at all")
            continue
        driver.switch_to.default_content()
        driver.execute_script(_AIM_RESET)
        try:
            browser_tools.pointer_action(
                "click",
                centre["x"],
                centre["y"],
                session_id,
                wait_seconds=0,
                include_summary=False,
            )
        except ValueError as exc:
            failures.append(f"{shape} ({frame_path}): the centre was refused - {exc}")
            continue
        verdict = _aim_verdict(driver, frame_path)
        if verdict != "HIT":
            failures.append(
                f"{shape} ({frame_path}): clicking the reported centre "
                f"({centre['x']}, {centre['y']}) hit {verdict}"
            )
    return failures


def test_a_reported_centre_in_the_outline_is_where_the_click_lands(local_site):
    """Every shape a frame can take, judged by the document that gets the event.

    One session and one loop rather than a case per browser: every failure is
    collected, because the interesting answer is *which* transforms are wrong,
    and a test that stops at the first one hides the rest.
    """
    driver = _open_or_skip(
        _fixture(local_site, "transformed_frames.html"), "defect-frame-centre"
    )
    try:
        assert _settle(driver, _AIM_READY), "the fixture's frames never finished loading"
        outline = page_perception.outline(driver, format="json", limit=600)
        centres = {
            node["frame"]: node["center"]
            for node in _nodes(outline)
            if node.get("name") == "Aim here" and node.get("frame")
        }
        failures = _aim_report(driver, "defect-frame-centre", centres)
        assert not failures, "the outline's centre missed:\n  " + "\n  ".join(failures)
    finally:
        browser_tools.close_session("defect-frame-centre")


def test_a_reported_centre_in_find_is_where_the_click_lands(local_site):
    """find reports the same boxes as the outline, so it has the same duty."""
    driver = _open_or_skip(
        _fixture(local_site, "transformed_frames.html"), "defect-find-centre"
    )
    try:
        assert _settle(driver, _AIM_READY), "the fixture's frames never finished loading"
        found = page_perception.find(driver, "Aim here", role="button", limit=25)
        centres = {
            match["frame"]: match["center"]
            for match in found["matches"]
            if match.get("frame")
        }
        failures = _aim_report(driver, "defect-find-centre", centres)
        assert not failures, "find's centre missed:\n  " + "\n  ".join(failures)
    finally:
        browser_tools.close_session("defect-find-centre")


def test_a_box_that_only_approximates_its_projection_says_so(local_site):
    """A perspective chain has no affine map onto the page.

    The input layer already reports its mapping there as an approximation. The
    outline used to hand over the same guess as an ordinary number, which is the
    one case a caller cannot check for themselves - so it is labelled per node,
    and only there: a flag on every frame would say nothing.
    """
    driver = _open_or_skip(
        _fixture(local_site, "transformed_frames.html"), "defect-frame-approx"
    )
    try:
        assert _settle(driver, _AIM_READY), "the fixture's frames never finished loading"
        outline = page_perception.outline(driver, format="json", limit=600)
        aims = [node for node in _nodes(outline) if node.get("name") == "Aim here"]
        approximate = {
            node.get("frame") for node in aims if node.get("page_rect_approximate")
        }
        assert approximate == {"#persp"}, (
            f"only the perspective frame projects non-affinely, but {approximate} "
            "were marked"
        )

        text = page_perception.outline(driver, format="text", limit=600)["outline"]
        marked = [line for line in text.splitlines() if "approximate-box" in line]
        assert len(marked) == 1 and "Aim here" in marked[0]

        found = page_perception.find(driver, "Aim here", role="button", limit=25)
        flagged = {
            match.get("frame") for match in found["matches"]
            if match.get("page_rect_approximate")
        }
        assert flagged == {"#persp"}, "find and the outline disagree about the same box"
    finally:
        browser_tools.close_session("defect-frame-approx")


def test_a_rotated_frame_reports_a_bounding_box_and_an_exact_centre(local_site):
    """What ``page_rect`` means once a frame is turned, said out loud.

    A rotated rectangle has no axis-aligned rectangle of its own, so ``page_rect``
    is the bounding box of the mapped corners - larger than the element, and not
    a thing to aim at. ``center`` is the mapped centre, and the click test above
    is what proves it exact.
    """
    driver = _open_or_skip(
        _fixture(local_site, "transformed_frames.html"), "defect-frame-bbox"
    )
    try:
        assert _settle(driver, _AIM_READY), "the fixture's frames never finished loading"
        outline = page_perception.outline(driver, format="json", limit=600)
        aims = {
            node["frame"]: node
            for node in _nodes(outline)
            if node.get("name") == "Aim here" and node.get("frame")
        }
        rotated = aims["#xrot"]
        # The button is 46x22 in its own document and the frame is not scaled, so
        # any growth here is the rotation's, and a translation-only path had none.
        assert (rotated["rect"]["w"], rotated["rect"]["h"]) == (46, 22)
        assert rotated["page_rect"]["w"] > 46 and rotated["page_rect"]["h"] > 22
        centre = rotated["center"]
        box = rotated["page_rect"]
        assert box["x"] < centre["x"] < box["x"] + box["w"]
        assert box["y"] < centre["y"] < box["y"] + box["h"]

        # A frame that is only offset has nothing to bound: the box is the box.
        plain = aims["#plain"]
        assert (plain["page_rect"]["w"], plain["page_rect"]["h"]) == (46, 22)
    finally:
        browser_tools.close_session("defect-frame-bbox")


def test_perception_and_input_aim_through_the_same_frame_map(local_site):
    """A drift guard, not the proof - the proof is the click test above.

    Both layers answer "where is this frame-local point on the page", and they
    used to answer differently: input composed the frame's transform chain while
    perception added the frame's origin. Two implementations of one question
    drift, so this fails the moment they stop being the same function.
    """
    driver = _open_or_skip(
        _fixture(local_site, "transformed_frames.html"), "defect-frame-agree"
    )
    try:
        assert _settle(driver, _AIM_READY), "the fixture's frames never finished loading"
        outline = page_perception.outline(driver, format="json", limit=600)
        aims = {
            node["frame"]: node
            for node in _nodes(outline)
            if node.get("name") == "Aim here" and node.get("frame")
        }
        disagreements = []
        for frame_path, shape in _FRAME_SHAPES:
            # Input is aimed by a CSS selector resolved in the top document, so a
            # nested frame is not something it can be asked about at all.
            if ">>>" in frame_path or frame_path not in aims:
                continue
            node = aims[frame_path]
            if frame_path == "#innerxf":
                # This target carries its own transform, so its centre is not the
                # untransformed one; the frame map is not what moved it.
                local_x = node["rect"]["x"] + node["rect"]["w"] / 2
                local_y = node["rect"]["y"] + node["rect"]["h"] / 2
            else:
                local_x, local_y = _AIM_CENTRE
            driver.switch_to.default_content()
            page_x, page_y = browser_tools._frame_map(driver, frame_path).to_page(
                local_x, local_y
            )
            centre = node["center"]
            if abs(page_x - centre["x"]) > 1 or abs(page_y - centre["y"]) > 1:
                disagreements.append(
                    f"{shape} ({frame_path}): input aims at ({page_x:.1f}, "
                    f"{page_y:.1f}), perception reports ({centre['x']}, {centre['y']})"
                )
        assert not disagreements, "the two layers disagree:\n  " + "\n  ".join(
            disagreements
        )
    finally:
        browser_tools.close_session("defect-frame-agree")
