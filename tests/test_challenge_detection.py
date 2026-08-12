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
