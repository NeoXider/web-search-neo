"""Regressions for form actions and challenge verdicts that were confidently wrong.

Each test here reproduces a report the caller had no way to doubt: a field that
came back filled with a value it had refused, a form reported as submitted when
nothing was sent (and as failed when it had been), an upload that described the
call instead of the input, and a challenge detector that stopped the agent on
pages that were never blocking it while walking straight past the ones that were.
"""

from __future__ import annotations

import time

import pytest
from selenium.common.exceptions import WebDriverException

import browser_tools
import page_perception


def _open_or_skip(url: str, session_id: str):
    try:
        browser_tools.open_page(
            url, session_id=session_id, headless=True, profile_mode="temporary"
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    return browser_tools._get_session(session_id).driver


def _form(local_site, name: str) -> str:
    return f"{local_site.base_url}/fixtures/forms/{name}"


def _challenge(local_site, name: str) -> str:
    return f"{local_site.base_url}/fixtures/challenges/{name}"


def _in_frame(driver, frame: str, expression: str):
    driver.switch_to.default_content()
    driver.switch_to.frame(driver.find_element("css selector", frame))
    try:
        return driver.execute_script(f"return {expression};")
    finally:
        driver.switch_to.default_content()


def _settle(driver, expression: str, timeout: float = 5.0) -> bool:
    """Wait for content a fixture builds after its own document is parsed."""
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
# fill must report the value the control kept, not the value it was handed
# ---------------------------------------------------------------------------


def test_a_number_input_that_dropped_the_text_is_not_reported_as_filled(local_site):
    _open_or_skip(_form(local_site, "picky.html"), "defect-fill-number")
    result = browser_tools.fill_fields({"#age": "not a number"}, session_id="defect-fill-number")
    assert result["success"] is False
    assert result["filled"] == []
    assert result["field_values"]["#age"] == ""
    assert "not a number" in result["errors"]["#age"]


def test_a_maxlength_truncation_is_reported_with_the_value_kept(local_site):
    _open_or_skip(_form(local_site, "picky.html"), "defect-fill-maxlength")
    result = browser_tools.fill_fields(
        {"#code": "ABCDEFGH"}, session_id="defect-fill-maxlength"
    )
    assert result["success"] is False
    assert result["field_values"]["#code"] == "ABCD"
    assert "'ABCD'" in result["errors"]["#code"]


def test_a_value_an_input_handler_rewrote_is_reported(local_site):
    _open_or_skip(_form(local_site, "picky.html"), "defect-fill-handler")
    result = browser_tools.fill_fields({"#digits": "12ab34"}, session_id="defect-fill-handler")
    assert result["success"] is False
    assert result["field_values"]["#digits"] == "1234"


def test_a_field_that_takes_its_value_is_still_reported_as_filled(local_site):
    _open_or_skip(_form(local_site, "picky.html"), "defect-fill-ok")
    result = browser_tools.fill_fields({"#free": "ok", "#role": "python"}, session_id="defect-fill-ok")
    assert result["success"] is True
    assert set(result["filled"]) == {"#free", "#role"}
    assert result["field_values"] == {"#free": "ok", "#role": "python"}
    assert result["errors"] == {}


def test_readonly_and_disabled_controls_say_why_without_a_driver_stacktrace(local_site):
    _open_or_skip(_form(local_site, "picky.html"), "defect-fill-locked")
    result = browser_tools.fill_fields(
        {"#locked": "changed", "#off": "changed"}, session_id="defect-fill-locked"
    )
    assert result["success"] is False
    assert "readonly" in result["errors"]["#locked"]
    assert "disabled" in result["errors"]["#off"]
    for message in result["errors"].values():
        assert "Stacktrace" not in message
        assert len(message) < 200


def test_a_disabled_option_is_refused_instead_of_silently_ignored(local_site):
    driver = _open_or_skip(_form(local_site, "picky.html"), "defect-fill-option")
    result = browser_tools.fill_fields({"#role": "unity"}, session_id="defect-fill-option")
    assert result["success"] is False
    assert "disabled" in result["errors"]["#role"]
    assert driver.execute_script("return document.getElementById('role').value;") == "python"


def test_an_option_that_does_not_exist_lists_the_ones_that_do(local_site):
    _open_or_skip(_form(local_site, "picky.html"), "defect-fill-missing-option")
    result = browser_tools.fill_fields({"#role": "Rust"}, session_id="defect-fill-missing-option")
    assert result["success"] is False
    assert "'python'" in result["errors"]["#role"]


# ---------------------------------------------------------------------------
# checkbox values, and the change event the last field never got
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, 1, "1", "yes", "on", "check", "checked", "TRUE"])
def test_values_that_mean_tick_the_box(local_site, value):
    driver = _open_or_skip(_form(local_site, "change_events.html"), "defect-check-true")
    driver.execute_script(
        "document.body.insertAdjacentHTML('beforeend', '<input id=\"box\" type=\"checkbox\">');"
    )
    result = browser_tools.fill_fields({"#box": value}, session_id="defect-check-true")
    assert result["success"] is True
    assert driver.execute_script("return document.getElementById('box').checked;") is True


@pytest.mark.parametrize("value", [False, 0, "0", "no", "off", "uncheck", "unchecked", ""])
def test_values_that_mean_clear_the_box(local_site, value):
    driver = _open_or_skip(_form(local_site, "change_events.html"), "defect-check-false")
    driver.execute_script(
        "document.body.insertAdjacentHTML('beforeend', "
        "'<input id=\"box\" type=\"checkbox\" checked>');"
    )
    result = browser_tools.fill_fields({"#box": value}, session_id="defect-check-false")
    assert result["success"] is True
    assert driver.execute_script("return document.getElementById('box').checked;") is False


def test_a_checkbox_value_that_means_neither_is_refused_instead_of_clearing_it(local_site):
    driver = _open_or_skip(_form(local_site, "change_events.html"), "defect-check-junk")
    driver.execute_script(
        "document.body.insertAdjacentHTML('beforeend', "
        "'<input id=\"box\" type=\"checkbox\" checked>');"
    )
    result = browser_tools.fill_fields({"#box": "maybe"}, session_id="defect-check-junk")
    assert result["success"] is False
    assert "check" in result["errors"]["#box"]
    assert driver.execute_script("return document.getElementById('box').checked;") is True


def test_the_last_field_of_a_fill_fires_its_change_event(local_site):
    driver = _open_or_skip(_form(local_site, "change_events.html"), "defect-change")
    browser_tools.fill_fields(
        {"#first": "a", "#second": "b", "#third": "c"}, session_id="defect-change"
    )
    assert driver.execute_script("return window.changed;") == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# submit has to be told by the document, not by the title
# ---------------------------------------------------------------------------


def test_a_form_that_reloads_the_same_url_is_reported_as_submitted(local_site):
    _open_or_skip(_form(local_site, "submit_same.html?q=1"), "defect-submit-same")
    result = browser_tools.submit_form("#repeat", session_id="defect-submit-same")
    assert result["success"] is True
    assert result["submit_triggered"] is True
    assert result["navigation_observed"] is True


def test_a_button_that_submits_nothing_is_not_success_just_because_the_title_ticks(local_site):
    driver = _open_or_skip(_form(local_site, "submit_never.html"), "defect-submit-never")
    before = driver.title
    result = browser_tools.submit_form(
        "#feedback", session_id="defect-submit-never", submit_selector="#fake-send"
    )
    assert result["title"] != before  # the page really does keep changing its title
    assert result["success"] is False
    assert result["submit_triggered"] is False
    assert result["navigation_observed"] is False


def test_a_form_whose_handler_cancels_the_navigation_says_so(local_site):
    driver = _open_or_skip(_form(local_site, "change_events.html"), "defect-submit-spa")
    driver.execute_script(
        "document.getElementById('profile')"
        ".addEventListener('submit', event => event.preventDefault());"
    )
    result = browser_tools.submit_form("#profile", session_id="defect-submit-spa")
    assert result["submit_event_fired"] is True
    assert result["navigation_observed"] is False
    assert result["submit_default_prevented"] is True


# ---------------------------------------------------------------------------
# upload describes the input, not the request
# ---------------------------------------------------------------------------


def test_a_second_upload_replaces_the_first_instead_of_stacking_on_it(local_site, tmp_path):
    driver = _open_or_skip(_form(local_site, "upload_many.html"), "defect-upload-replace")
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one")
    second.write_text("two")

    browser_tools.upload_file("#many", [str(first)], session_id="defect-upload-replace")
    result = browser_tools.upload_file("#many", [str(second)], session_id="defect-upload-replace")

    assert driver.execute_script("return window.attachedNames();") == ["second.txt"]
    assert result["file_names"] == ["second.txt"]
    assert result["files_uploaded"] == {"#many": ["second.txt"]}


def test_upload_and_fill_report_files_the_same_way(local_site, tmp_path):
    _open_or_skip(_form(local_site, "upload_many.html"), "defect-upload-shape")
    sample = tmp_path / "cv.txt"
    sample.write_text("cv")

    uploaded = browser_tools.upload_file("#one", [str(sample)], session_id="defect-upload-shape")
    filled = browser_tools.fill_fields(
        {}, files={"#one": str(sample)}, session_id="defect-upload-shape"
    )

    assert uploaded["files_uploaded"] == {"#one": ["cv.txt"]}
    assert filled["files_uploaded"] == {"#one": ["cv.txt"]}


# ---------------------------------------------------------------------------
# a form inside a frame is reachable by name
# ---------------------------------------------------------------------------


def test_the_action_tools_act_inside_the_frame_they_are_given(local_site, tmp_path):
    driver = _open_or_skip(_form(local_site, "frame_host.html"), "defect-frame-actions")
    sample = tmp_path / "cv.txt"
    sample.write_text("cv")

    filled = browser_tools.fill_fields(
        {"#candidate": "Ada"}, session_id="defect-frame-actions", frame_selector="#apply"
    )
    assert filled["success"] is True
    assert _in_frame(driver, "#apply", "document.getElementById('candidate').value") == "Ada"
    # The decoy frame carries the same ids, so acting in the wrong one shows up.
    assert _in_frame(driver, "#decoy", "document.getElementById('candidate').value") == "decoy"

    waited = browser_tools.wait_for_element(
        "#late", session_id="defect-frame-actions", state="present", frame_selector="#apply"
    )
    assert waited["success"] is True

    clicked = browser_tools.click(
        "#ping", session_id="defect-frame-actions", frame_selector="#apply"
    )
    assert clicked["success"] is True
    assert _in_frame(driver, "#apply", "document.getElementById('clicked').textContent") == "clicked"
    assert (
        _in_frame(driver, "#decoy", "document.getElementById('clicked').textContent")
        == "not clicked"
    )

    uploaded = browser_tools.upload_file(
        "#resume", [str(sample)], session_id="defect-frame-actions", frame_selector="#apply"
    )
    assert uploaded["files_uploaded"] == {"#resume": ["cv.txt"]}

    submitted = browser_tools.submit_form(
        "#application", session_id="defect-frame-actions", frame_selector="#apply"
    )
    assert submitted["success"] is True
    assert _in_frame(driver, "#apply", "document.title") == "Application received"
    # The action is over: the driver belongs to the top document again.
    assert driver.execute_script("return document.title;") == "Application host"


def test_wait_reports_the_wait_it_really_made_not_the_one_it_was_asked_for(local_site):
    """A requested timeout is honoured as passed, and the result says the number
    that was actually waited, not the one that was guessed."""
    _open_or_skip(_form(local_site, "frame_host.html"), "defect-wait-clamp")
    waited = browser_tools.wait_for_element(
        "#host-state", session_id="defect-wait-clamp", state="visible", timeout_seconds=120.0
    )
    # The wait succeeds immediately for a visible element, but the reported
    # timeout must be the value the caller asked for, never a forced cap.
    assert waited["timeout_seconds"] == 120.0

    with pytest.raises(WebDriverException) as failure:
        browser_tools.wait_for_element(
            "#nothing-here",
            session_id="defect-wait-clamp",
            state="present",
            timeout_seconds=1.0,
        )
    assert "1s" in str(failure.value)  # the wait it made, in the failure itself


def test_an_ambiguous_frame_selector_is_refused_by_the_action_tools(local_site):
    _open_or_skip(_form(local_site, "frame_host.html"), "defect-frame-ambiguous")
    with pytest.raises(ValueError, match="matches 2 elements"):
        browser_tools.click("#ping", session_id="defect-frame-ambiguous", frame_selector="iframe")


def test_a_handle_that_carries_its_own_frame_refuses_a_second_one(local_site):
    driver = _open_or_skip(_form(local_site, "frame_host.html"), "defect-frame-conflict")
    outline = page_perception.outline(driver, format="json", limit=400)
    ref = next(
        node["ref"]
        for node in outline["nodes"]
        if node.get("kind") == "node" and node.get("name") == "Ping"
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        browser_tools.click(ref, session_id="defect-frame-conflict", frame_selector="#apply")
    assert browser_tools.click(ref, session_id="defect-frame-conflict")["success"] is True


# ---------------------------------------------------------------------------
# a challenge is what blocks the caller, and it is not always in the top document
# ---------------------------------------------------------------------------


def test_a_captcha_on_a_readable_page_is_reported_but_does_not_block(local_site):
    driver = _open_or_skip(_challenge(local_site, "article_with_widget.html"), "defect-challenge-article")
    status = browser_tools._challenge_status(driver)
    assert status["challenge_detected"] is False
    assert status["manual_action_required"] is False
    assert status["captcha_widgets"] == ["div.h-captcha"]


def test_a_sitekey_that_is_not_a_captcha_is_not_a_challenge(local_site):
    driver = _open_or_skip(_challenge(local_site, "sitekey_not_a_captcha.html"), "defect-challenge-sitekey")
    status = browser_tools._challenge_status(driver)
    assert status["challenge_detected"] is False
    assert status["captcha_widgets"] == []


def test_a_captcha_lying_over_the_page_blocks_even_when_the_page_is_long(local_site):
    driver = _open_or_skip(_challenge(local_site, "overlay.html"), "defect-challenge-overlay")
    # The verdict has to come from the widget lying over the page, not from the
    # page being short or saying "verify you are human": the probe's own flag is
    # asserted so that this cannot pass on one of the other two rules.
    assert driver.execute_script(browser_tools._CHALLENGE_WIDGET_SCRIPT)["blocking"] is True
    status = browser_tools._challenge_status(driver)
    assert status["challenge_detected"] is True
    assert status["manual_action_required"] is True


@pytest.mark.parametrize(
    ("fixture", "evidence"),
    [
        ("datadome.html", 'iframe[src*="captcha-delivery.com"]'),
        ("awswaf.html", 'script[src*="captcha-sdk.awswaf.com"]'),
        ("nested_frame.html", 'iframe[src*="recaptcha/api2"] (in a frame)'),
        ("shadow_dom.html", "div.cf-turnstile (in shadow DOM)"),
    ],
)
def test_challenges_the_top_level_query_walked_past(local_site, fixture, evidence):
    driver = _open_or_skip(_challenge(local_site, fixture), f"defect-challenge-{fixture}")
    _settle(driver, "document.readyState === 'complete'")
    status = browser_tools._challenge_status(driver)
    assert status["challenge_detected"] is True
    assert status["challenge_type"] == "captcha"
    assert evidence in status["challenge_evidence"]


# ---------------------------------------------------------------------------
# blocking is a property of the whole stack over the page, not of one hit test
# ---------------------------------------------------------------------------


def test_a_captcha_in_a_shadow_root_over_the_page_blocks(local_site):
    """elementFromPoint retargets shadow content to its host, so the widget in a
    web component was never the node at the centre and never counted as blocking."""
    driver = _open_or_skip(_challenge(local_site, "shadow_overlay.html"), "defect-block-shadow")
    _settle(driver, "document.getElementById('gate').shadowRoot")
    assert driver.execute_script(browser_tools._CHALLENGE_WIDGET_SCRIPT)["blocking"] is True
    status = browser_tools._challenge_status(driver)
    assert status["challenge_detected"] is True
    assert status["manual_action_required"] is True
    assert status["captcha_widgets"] == ["div.cf-turnstile (in shadow DOM)"]


def test_a_veil_over_the_captcha_does_not_make_the_page_reachable(local_site):
    """A transparent layer above the widget wins the hit test; it blocks the page
    just as thoroughly, so the verdict must not depend on what is topmost."""
    driver = _open_or_skip(_challenge(local_site, "veil.html"), "defect-block-veil")
    assert driver.execute_script(browser_tools._CHALLENGE_WIDGET_SCRIPT)["blocking"] is True
    status = browser_tools._challenge_status(driver)
    assert status["challenge_detected"] is True
    assert status["manual_action_required"] is True


def test_a_dismissible_modal_over_a_readable_page_does_not_block(local_site):
    """Covering the centre of the viewport is not the same as blocking the page:
    an optional sign-in box leaves the whole thread readable and clickable."""
    driver = _open_or_skip(_challenge(local_site, "optional_modal.html"), "defect-block-modal")
    probe = driver.execute_script(browser_tools._CHALLENGE_WIDGET_SCRIPT)
    assert probe["body_length"] > 1500  # nothing about this page is an interstitial
    assert probe["blocking"] is False
    status = browser_tools._challenge_status(driver)
    assert status["challenge_detected"] is False
    assert status["manual_action_required"] is False
    assert status["captcha_widgets"] == ["div.g-recaptcha"]
    waited = browser_tools.wait_for_challenge_resolution(
        session_id="defect-block-modal", timeout_seconds=10.0
    )
    assert waited["resolved"] is True
    assert waited["timed_out"] is False
    assert waited["waited_seconds"] < 5.0


# ---------------------------------------------------------------------------
# a scan that stopped early must not read as a page with no captcha on it
# ---------------------------------------------------------------------------


def test_a_widget_past_the_old_node_budget_is_still_found(local_site):
    driver = _open_or_skip(
        _challenge(local_site, "huge_page.html?nodes=12000"), "defect-scan-budget"
    )
    _settle(driver, "window.fillerCount > 12000")
    status = browser_tools._challenge_status(driver)
    assert status["captcha_widgets"] == ["div.cf-turnstile (in shadow DOM)"]
    assert status["captcha_scan_incomplete"] is False


def test_a_scan_that_ran_out_of_budget_says_it_stopped_looking(local_site):
    driver = _open_or_skip(
        _challenge(local_site, "huge_page.html?nodes=60000"), "defect-scan-truncated"
    )
    _settle(driver, "window.fillerCount > 60000", timeout=15.0)
    status = browser_tools._challenge_status(driver)
    assert status["captcha_widgets"] == []
    assert status["captcha_scan_incomplete"] is True


def test_a_challenge_five_documents_down_is_found(local_site):
    driver = _open_or_skip(
        _challenge(local_site, "deep_frames.html?depth=5"), "defect-scan-depth"
    )
    _settle(driver, "window.gateReady()")
    status = browser_tools._challenge_status(driver)
    assert status["captcha_widgets"] == ["div.cf-turnstile (in a frame)"]
    assert status["captcha_scan_incomplete"] is False


def test_a_scan_that_ran_out_of_depth_says_it_stopped_looking(local_site):
    driver = _open_or_skip(
        _challenge(local_site, "deep_frames.html?depth=14"), "defect-scan-deep"
    )
    _settle(driver, "window.gateReady()", timeout=15.0)
    status = browser_tools._challenge_status(driver)
    assert status["captcha_widgets"] == []
    assert status["captcha_scan_incomplete"] is True


# ---------------------------------------------------------------------------
# the value a field is left holding is read off the field the page kept
# ---------------------------------------------------------------------------


def test_a_control_the_page_replaced_while_committing_is_not_reported_as_filled(local_site):
    """The change the blur fires can re-render the control; the value has to be
    read off whatever the page kept, not off the orphan left in the script."""
    driver = _open_or_skip(_form(local_site, "rerender.html"), "defect-fill-rerender")
    result = browser_tools.fill_fields({"#coupon": "SUMMER"}, session_id="defect-fill-rerender")
    assert driver.execute_script("return document.getElementById('coupon').value;") == ""
    assert driver.execute_script("return document.getElementById('notice').textContent;") == (
        "Coupon rejected"
    )
    assert result["success"] is False
    assert result["filled"] == []
    assert result["field_values"]["#coupon"] == ""
    assert "SUMMER" in result["errors"]["#coupon"]


def test_a_control_the_page_removed_while_committing_is_reported_gone(local_site):
    driver = _open_or_skip(_form(local_site, "rerender.html"), "defect-fill-removed")
    result = browser_tools.fill_fields({"#promo": "SPRING"}, session_id="defect-fill-removed")
    assert driver.execute_script("return !document.getElementById('promo');") is True
    assert result["success"] is False
    assert result["field_values"]["#promo"] is None
    assert "#promo" in result["errors"]


def test_a_multiple_select_holds_exactly_the_option_that_was_asked_for(local_site):
    """select_by_value adds to the selection, so two fills left both options set -
    the form would have submitted both - and the read-back named only the first."""
    driver = _open_or_skip(_form(local_site, "multiselect.html"), "defect-select-multi")
    first = browser_tools.fill_fields({"#langs": "b"}, session_id="defect-select-multi")
    assert first["success"] is True
    assert driver.execute_script("return window.selectedLangs();") == ["b"]

    second = browser_tools.fill_fields({"#langs": "c"}, session_id="defect-select-multi")
    assert driver.execute_script("return window.selectedLangs();") == ["c"]
    assert second["success"] is True
    assert second["field_values"]["#langs"] == ["c"]


def test_a_multiple_select_takes_every_value_it_is_given(local_site):
    driver = _open_or_skip(_form(local_site, "multiselect.html"), "defect-select-list")
    result = browser_tools.fill_fields({"#langs": ["a", "c"]}, session_id="defect-select-list")
    assert result["success"] is True
    assert driver.execute_script("return window.selectedLangs();") == ["a", "c"]
    assert result["field_values"]["#langs"] == ["a", "c"]
    assert driver.execute_script("return window.changed;") == ["langs"]


def test_a_value_the_field_sanitised_is_not_reported_as_a_refusal(local_site):
    """type=email and type=url strip the whitespace around a value by the spec's
    own algorithm, and a change handler may trim or fold case: the field took it."""
    _open_or_skip(_form(local_site, "picky.html"), "defect-fill-sanitised")
    result = browser_tools.fill_fields(
        {
            "#email": "  ada@example.com  ",
            "#site": "  https://example.test/  ",
            "#city": "  Berlin  ",
        },
        session_id="defect-fill-sanitised",
    )
    assert result["errors"] == {}
    assert result["success"] is True
    assert set(result["filled"]) == {"#email", "#site", "#city"}
    assert result["field_values"]["#email"] == "ada@example.com"
    assert result["field_values"]["#city"] == "berlin"


# ---------------------------------------------------------------------------
# controls that cannot be typed into
# ---------------------------------------------------------------------------


def test_date_and_dial_controls_take_their_values(local_site):
    """clear() + send_keys turned '2024-01-15' into '40115-02-20' and left the
    field holding a date that looks perfectly plausible."""
    driver = _open_or_skip(_form(local_site, "datetime.html"), "defect-fill-dates")
    result = browser_tools.fill_fields(
        {
            "#day": "2024-01-15",
            "#at": "09:30",
            "#when": "2024-01-15T09:30",
            "#cycle": "2024-01",
            "#sprint": "2024-W03",
            "#volume": "35",
            "#shade": "#ff8800",
        },
        session_id="defect-fill-dates",
    )
    assert result["errors"] == {}
    assert result["success"] is True
    assert driver.execute_script(
        "return Array.from(document.querySelectorAll('#booking input'))"
        ".map(node => node.id + '=' + node.value);"
    ) == [
        "day=2024-01-15",
        "at=09:30",
        "when=2024-01-15T09:30",
        "cycle=2024-01",
        "sprint=2024-W03",
        "volume=35",
        "shade=#ff8800",
    ]
    # One change event each, and the input event the page's own script expects.
    assert driver.execute_script("return window.changed;") == [
        "day", "at", "when", "cycle", "sprint", "volume", "shade",
    ]
    assert driver.execute_script("return window.inputEvents;") == [
        "day", "at", "when", "cycle", "sprint", "volume", "shade",
    ]


def test_a_date_in_the_wrong_format_names_the_format_that_works(local_site):
    driver = _open_or_skip(_form(local_site, "datetime.html"), "defect-fill-date-format")
    result = browser_tools.fill_fields({"#day": "01/15/2024"}, session_id="defect-fill-date-format")
    assert result["success"] is False
    assert "YYYY-MM-DD" in result["errors"]["#day"]
    # Not left holding a wrong date that reads like a right one.
    assert driver.execute_script("return document.getElementById('day').value;") == ""


def test_a_refused_range_value_does_not_move_the_slider(local_site):
    driver = _open_or_skip(_form(local_site, "datetime.html"), "defect-fill-range")
    result = browser_tools.fill_fields({"#volume": "loud"}, session_id="defect-fill-range")
    assert result["success"] is False
    assert driver.execute_script("return document.getElementById('volume').value;") == "80"


def test_a_control_that_cannot_be_typed_into_still_reports_what_it_holds(local_site):
    result_session = "defect-fill-custom"
    _open_or_skip(_form(local_site, "datetime.html"), result_session)
    result = browser_tools.fill_fields({"#custom": "text"}, session_id=result_session)
    assert result["success"] is False
    assert "#custom" in result["errors"]
    assert result["field_values"]["#custom"] == "preset"


# ---------------------------------------------------------------------------
# submit: what the page did, and where the result went
# ---------------------------------------------------------------------------


def test_a_framework_that_swallows_the_submit_event_still_counts_as_submitted(local_site):
    """stopImmediatePropagation on the form hid the submit from a listener added
    after it, so a working submit reported failure and the caller posted twice."""
    driver = _open_or_skip(_form(local_site, "spa_submit.html"), "defect-submit-spa-swallow")
    result = browser_tools.submit_form("#signup", session_id="defect-submit-spa-swallow")
    assert driver.execute_script("return window.spaHandled;") is True
    assert result["submit_event_fired"] is True
    assert result["submit_default_prevented"] is True
    assert result["success"] is True


def test_a_submit_that_opens_a_new_tab_says_the_result_is_not_on_this_page(local_site):
    driver = _open_or_skip(_form(local_site, "submit_blank.html"), "defect-submit-blank")
    before = len(driver.window_handles)
    result = browser_tools.submit_form("#preview", session_id="defect-submit-blank")
    assert len(driver.window_handles) == before + 1
    assert result["success"] is True
    assert result["new_tab_opened"] is True
    assert result["title"] == "Print preview"  # this page, not the result


def test_a_cross_origin_submit_explains_why_the_event_could_not_be_read(local_site):
    _open_or_skip(_form(local_site, "submit_cross_origin.html"), "defect-submit-cross")
    result = browser_tools.submit_form("#handoff", session_id="defect-submit-cross")
    assert result["success"] is True
    assert result["submit_triggered"] is True
    assert result["submit_event_fired"] is False
    assert "cross-origin" in result["submit_evidence"]


def test_the_submit_probe_leaves_no_trace_of_itself_in_the_page(local_site):
    """The token was written to sessionStorage under a branded key and never
    removed: a one-key fingerprint left on exactly the sites that look for one."""
    driver = _open_or_skip(_form(local_site, "spa_submit.html"), "defect-submit-trace")
    # WebDriver leaves a global of its own the first time a script is handed an
    # element; the snapshot is taken after that, so what it compares is ours.
    driver.execute_script(
        "return arguments[0].id;", driver.find_element("css selector", "#signup")
    )
    before = driver.execute_script("return Object.keys(window);")
    result = browser_tools.submit_form("#signup", session_id="defect-submit-trace")
    assert result["submit_event_fired"] is True  # the watcher really did run
    assert driver.execute_script("return Object.keys(sessionStorage);") == []
    assert driver.execute_script("return Object.keys(window);") == before
