"""find, asked the way an agent asks: filler words, role words and facts.

Every case here was a live miss on an encyclopedia page: "link to Unreal Engine"
found nothing because "link" and "to" were text to match, "initial release date"
ranked the login link first because "in" counted as the start of "initial", and
an infobox fact was out of reach because no table header was a candidate.
"""
from __future__ import annotations

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools, page_perception

SESSION = "find-agent-queries"


@pytest.fixture
def driver(local_site):
    try:
        browser_tools.open_page(f"{local_site.base_url}/fixtures/perception/agent_queries.html",
                                session_id=SESSION, width=1024, height=768, headless=True,
                                profile_mode="temporary")
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    yield browser_tools._get_session(SESSION).driver
    browser_tools.close_session(SESSION)


def _id(driver, match):
    return driver.execute_script(f"const target = {page_perception.ref_expression(match['ref'])};"
                                 "return target ? target.id : null;")


def test_a_role_word_and_filler_are_not_text_to_match(driver):
    found = page_perception.find(driver, "link to Unreal Engine")
    assert found["low_confidence"] is False
    assert _id(driver, found["matches"][0]) == "unreal"
    assert found["terms"] == {"words": ["unreal", "engine"], "role_hint": ["link"]}


def test_the_role_word_picks_between_two_elements_with_the_same_words(driver):
    assert _id(driver, page_perception.find(driver, "search field")["matches"][0]) == "q"
    assert _id(driver, page_perception.find(driver, "click the search button")["matches"][0]) == "go"


def test_a_fact_comes_back_with_the_value_it_labels(driver):
    for query, answer in (("initial release date", "1.0 / June 8, 2005"), ("release date", "1.0 / June 8, 2005"),
                          ("what license", "Proprietary"), ("Written in", "C++, C#"),
                          ("engine family", "Real-time 3D")):
        found = page_perception.find(driver, query)
        assert found["low_confidence"] is False, query
        assert found["matches"][0]["answer"] == answer, query


def test_a_header_and_the_link_inside_it_are_one_answer_not_a_coin_flip(driver):
    found = page_perception.find(driver, "Stable release version")
    assert found["low_confidence"] is False
    assert found["ambiguous"] is False
    assert found["matches"][0]["answer"] == "6000.6.2f1"


def test_short_words_do_not_start_long_ones(driver):
    found = page_perception.find(driver, "initial release date")
    assert all(_id(driver, match) != "login" for match in found["matches"])


def test_an_exact_label_still_wins_and_shows_its_visible_text(driver):
    found = page_perception.find(driver, "Log in")
    best = found["matches"][0]
    assert _id(driver, best) == "login"
    assert best["matched_field"] == "text" and best["text"] == "Log in"
