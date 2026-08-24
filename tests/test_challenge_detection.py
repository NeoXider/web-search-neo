"""The challenge detector must fire on a real widget, not on the word "captcha".

The previous version matched substrings anywhere in the page text, so an article
about CAPTCHAs - or this project's own README - reported a challenge, and the
agent then waited up to three minutes for a human who was not needed.
"""

from __future__ import annotations

import pytest
from selenium.common.exceptions import WebDriverException

import browser_tools


def _open_or_skip(url: str, session_id: str):
    try:
        return browser_tools.open_page(
            url, session_id=session_id, headless=True, profile_mode="temporary"
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def _status(session_id: str, body_html: str, title: str | None = None) -> dict:
    session = browser_tools._get_session(session_id)
    session.driver.execute_script(
        "document.body.innerHTML = arguments[0];"
        "if (arguments[1]) document.title = arguments[1];",
        body_html,
        title,
    )
    return browser_tools._challenge_status(session.driver)


ARTICLE = """
<h1>How CAPTCHA systems work</h1>
<p>A CAPTCHA is a challenge used to tell humans and bots apart. reCAPTCHA and
hCaptcha are the best known implementations. This article explains how a captcha
scores a session, why some sites ask you to verify you are human, and what to do
when unusual traffic is detected. It is long enough to look like real editorial
content rather than an interstitial, because that is exactly the distinction the
detector has to make. Nothing on this page is an actual challenge widget: there
is no iframe, no sitekey, and no form to complete. The word captcha appears many
times, and that alone must not be treated as a challenge.</p>
"""


def test_prose_about_captcha_is_not_a_challenge(local_site):
    _open_or_skip(f"{local_site.base_url}/page", "challenge-article")
    status = _status("challenge-article", ARTICLE, "How CAPTCHA systems work")
    assert status["challenge_detected"] is False
    assert status["challenge_type"] is None


@pytest.mark.parametrize(
    "widget",
    [
        '<div class="g-recaptcha" data-sitekey="abc" style="width:300px;height:80px"></div>',
        '<div class="h-captcha" data-sitekey="abc" style="width:300px;height:80px"></div>',
        '<div class="cf-turnstile" style="width:300px;height:70px"></div>',
        '<iframe src="https://challenges.cloudflare.com/x" style="width:300px;height:65px"></iframe>',
        '<iframe src="https://www.google.com/recaptcha/api2/anchor" '
        'style="width:300px;height:80px"></iframe>',
    ],
)
def test_provider_widgets_are_detected(local_site, widget):
    _open_or_skip(f"{local_site.base_url}/page", "challenge-widget")
    status = _status("challenge-widget", widget)
    assert status["challenge_detected"] is True
    assert status["challenge_type"] == "captcha"
    assert status["challenge_evidence"]


def test_hidden_or_tiny_widget_is_ignored(local_site):
    _open_or_skip(f"{local_site.base_url}/page", "challenge-tiny")
    status = _status(
        "challenge-tiny",
        '<div class="g-recaptcha" style="width:1px;height:1px"></div>',
    )
    assert status["challenge_detected"] is False


def test_short_interstitial_text_is_detected(local_site):
    _open_or_skip(f"{local_site.base_url}/page", "challenge-interstitial")
    status = _status(
        "challenge-interstitial",
        "<h1>Just a moment...</h1><p>Checking your browser before you continue.</p>",
    )
    assert status["challenge_detected"] is True
    assert status["challenge_type"] == "human_verification"


def test_interstitial_phrase_inside_a_long_page_is_ignored(local_site):
    """The same phrase buried in an article is editorial content, not a gate."""
    _open_or_skip(f"{local_site.base_url}/page", "challenge-long")
    filler = "Ordinary body copy that makes this page look like an article. " * 40
    status = _status(
        "challenge-long",
        f"<h1>Bot mitigation</h1><p>{filler}</p>"
        "<p>Sites often say checking your browser while they score the request.</p>",
    )
    assert status["challenge_detected"] is False


def test_title_alone_is_enough_for_a_challenge(local_site):
    _open_or_skip(f"{local_site.base_url}/page", "challenge-title")
    status = _status("challenge-title", "<p>x</p>", "Access denied")
    assert status["challenge_detected"] is True
    assert status["challenge_type"] == "access_challenge"
    assert status["challenge_evidence"] == ["page heading"]


# ---------------------------------------------------------------------------
# the challenge with no box: it blocks the form, not the page
# ---------------------------------------------------------------------------


def _invisible(local_site, session_id: str) -> dict:
    _open_or_skip(
        f"{local_site.base_url}/fixtures/challenges/invisible_turnstile.html", session_id
    )
    return browser_tools._challenge_status(browser_tools._get_session(session_id).driver)


def test_an_invisible_turnstile_is_reported_instead_of_being_walked_past(local_site):
    """Twelve applications were lost to this page saying nothing was wrong."""
    status = _invisible(local_site, "challenge-invisible")

    # It is not in the way of reading the page, so it must not park the agent.
    assert status["challenge_detected"] is False
    # But it is in the way of the form, and that is now said out loud.
    assert status["invisible_challenge_pending"] is True
    invisible = status["invisible_challenge"]
    assert invisible["vendor"] == "turnstile"
    assert invisible["state"] == "token_empty"
    assert any("cf-turnstile-response" in item for item in invisible["evidence"])
    assert "captcha action" in invisible["hint"]


def test_a_minted_token_ends_the_pending_verdict(local_site):
    session_id = "challenge-invisible-solved"
    assert _invisible(local_site, session_id)["invisible_challenge_pending"] is True
    driver = browser_tools._get_session(session_id).driver
    driver.execute_script("window.solveChallenge();")

    status = browser_tools._challenge_status(driver)
    assert status["invisible_challenge_pending"] is False
    assert "invisible_challenge" not in status


def test_an_ordinary_page_carries_the_key_without_a_challenge(local_site):
    _open_or_skip(f"{local_site.base_url}/page", "challenge-clean")
    status = browser_tools._challenge_status(
        browser_tools._get_session("challenge-clean").driver
    )
    assert status["invisible_challenge_pending"] is False


def test_captcha_detect_no_longer_answers_captcha_present_false(local_site):
    _invisible(local_site, "challenge-invisible-detect")
    found = browser_tools.solve_captcha(mode="detect", session_id="challenge-invisible-detect")
    assert found["captcha_present"] is True
    assert found["invisible_challenge"]["vendor"] == "turnstile"


def test_waiting_does_not_call_an_unsolved_invisible_widget_resolved(local_site):
    _invisible(local_site, "challenge-invisible-wait")
    waited = browser_tools.wait_for_challenge_resolution(
        "challenge-invisible-wait", timeout_seconds=0.3, poll_interval_seconds=0.1
    )
    assert waited["resolved"] is False
    assert waited["timed_out"] is True


def test_a_click_that_sends_nothing_says_the_challenge_is_why(local_site):
    """The Workable failure: the button goes busy and no POST is ever made."""
    _invisible(local_site, "challenge-invisible-click")
    clicked = browser_tools.click("#send", session_id="challenge-invisible-click")

    assert clicked["success"] is True
    assert clicked["submit_blocked_by_challenge"] is True
    assert "no network request" in clicked["submit_block_reason"]


def test_a_click_that_does_send_something_is_left_alone(local_site):
    """The same page, with the token in place and a request actually made."""
    session_id = "challenge-invisible-click-ok"
    _invisible(local_site, session_id)
    driver = browser_tools._get_session(session_id).driver
    driver.execute_script(
        "window.solveChallenge();"
        "document.getElementById('send').addEventListener('click', () =>"
        " fetch('/uploads', {method: 'POST', body: 'sent'}));"
    )
    clicked = browser_tools.click("#send", session_id=session_id)
    assert "submit_blocked_by_challenge" not in clicked
