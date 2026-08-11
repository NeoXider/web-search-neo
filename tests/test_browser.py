from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import struct

import pytest
from selenium.common.exceptions import WebDriverException

import browser_tools
import main


def _open_or_skip(url: str, session_id: str, **kwargs):
    try:
        return browser_tools.open_page(url, session_id=session_id, **kwargs)
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def test_browser_full_form_upload_click_submit_and_screenshot(local_site, tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("verified resume payload", encoding="utf-8")
    opened = _open_or_skip(
        f"{local_site.base_url}/form?session=full-flow",
        "full-flow",
        width=800,
        height=600,
    )
    assert opened["session_id"] == "full-flow"
    assert opened["title"] == "Form full-flow"
    assert opened["viewport_width"] == 800
    assert opened["viewport_height"] == 600

    elements = browser_tools.get_page_elements("full-flow")
    assert {link["selector"] for link in elements["links"]} >= {"#fixture-link"}
    assert {button["selector"] for button in elements["buttons"]} >= {
        "#action-button",
        "#submit-button",
    }
    form = next(item for item in elements["forms"] if item["selector"] == "#application")
    by_selector = {field["selector"]: field for field in form["fields"]}
    assert by_selector["#candidate-name"]["label"] == "Candidate name"
    assert by_selector["#resume"]["type"] == "file"
    assert {item["value"] for item in by_selector["#role"]["options"]} == {
        "python",
        "unity",
    }

    filled = browser_tools.fill_fields(
        {
            "#candidate-name": "Neo Candidate",
            "#cover-letter": "Unity and C# experience",
            "#role": "unity",
            "#remote": True,
        },
        files={"#resume": str(resume)},
        session_id="full-flow",
    )
    assert filled["success"] is True
    assert set(filled["filled"]) == {
        "#candidate-name",
        "#cover-letter",
        "#role",
        "#remote",
    }
    assert filled["files_uploaded"] == ["#resume"]

    clicked = browser_tools.click("#action-button", "full-flow", wait_seconds=0)
    assert clicked["success"] is True
    session = browser_tools._get_session("full-flow")
    assert session.driver.find_element("css selector", "#click-state").text == "clicked"

    png = browser_tools.screenshot("full-flow", width=640, height=480, full_page=False)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1_000
    assert struct.unpack(">II", png[16:24]) == (640, 480)

    submitted = browser_tools.submit_form(
        "#application",
        "full-flow",
        submit_selector="#submit-button",
        wait_seconds=0.1,
    )
    assert submitted["success"] is True
    assert submitted["validation_passed"] is True
    assert submitted["submit_triggered"] is True
    assert submitted["title"] == "Submitted"
    assert submitted["url"].endswith("/submit")

    assert local_site.requests
    request = local_site.requests[-1]
    body = request.body.decode("utf-8", errors="replace")
    assert request.path == "/submit"
    assert request.headers["Content-Type"].startswith("multipart/form-data;")
    assert 'name="candidate_name"' in body and "Neo Candidate" in body
    assert 'name="role"' in body and "unity" in body
    assert 'name="remote"' in body and "on" in body
    assert 'filename="resume.txt"' in body and "verified resume payload" in body

    status = browser_tools.get_status("full-flow")
    assert status["available"] is True
    assert status["session_open"] is True
    closed = browser_tools.close_session("full-flow")
    assert closed == {"session_id": "full-flow", "closed": True, "active_sessions": []}
    assert browser_tools.get_status("full-flow")["session_open"] is False


def test_fill_fields_returns_partial_errors_without_losing_successful_fields(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "partial")

    result = browser_tools.fill_fields(
        {"#candidate-name": "Still filled", "#missing": "not found"},
        session_id="partial",
    )

    assert result["success"] is False
    assert result["filled"] == ["#candidate-name"]
    assert "#missing" in result["errors"]


def test_two_named_sessions_remain_independent_during_parallel_actions(local_site):
    _open_or_skip(f"{local_site.base_url}/form?session=alpha", "alpha")
    _open_or_skip(f"{local_site.base_url}/form?session=beta", "beta")

    def fill_and_read(session_id: str, value: str) -> tuple[str, str, str]:
        result = browser_tools.fill_fields(
            {"#candidate-name": value}, session_id=session_id
        )
        session = browser_tools._get_session(session_id)
        marker = session.driver.find_element("css selector", "#session-marker").text
        field_value = session.driver.find_element(
            "css selector", "#candidate-name"
        ).get_attribute("value")
        return result["session_id"], marker, field_value

    with ThreadPoolExecutor(max_workers=2) as executor:
        alpha_future = executor.submit(fill_and_read, "alpha", "Alice")
        beta_future = executor.submit(fill_and_read, "beta", "Bob")
        alpha = alpha_future.result(timeout=15)
        beta = beta_future.result(timeout=15)

    assert alpha == ("alpha", "alpha", "Alice")
    assert beta == ("beta", "beta", "Bob")
    assert browser_tools.get_status("alpha")["active_sessions"] == ["alpha", "beta"]
    assert browser_tools.get_status("beta")["active_sessions"] == ["alpha", "beta"]


def test_async_bulk_open_creates_two_independent_named_sessions(local_site):
    result = asyncio.run(
        main.browser_open_pages(
            [
                f"{local_site.base_url}/form?session=bulk-one",
                f"{local_site.base_url}/form?session=bulk-two",
            ],
            session_ids=["bulk-one", "bulk-two"],
            width=700,
            height=500,
        )
    )

    assert result["success_count"] == 2, result
    assert result["failure_count"] == 0
    assert [page["session_id"] for page in result["pages"]] == [
        "bulk-one",
        "bulk-two",
    ]
    assert [page["title"] for page in result["pages"]] == [
        "Form bulk-one",
        "Form bulk-two",
    ]
    assert browser_tools.get_status("bulk-one")["active_sessions"] == [
        "bulk-one",
        "bulk-two",
    ]


@pytest.mark.parametrize("session_id", ["", "contains space", "../escape", "x" * 65])
def test_session_id_is_validated(session_id):
    with pytest.raises(ValueError, match="session_id"):
        browser_tools.get_status(session_id)


def test_browser_requires_open_session_before_actions():
    with pytest.raises(ValueError, match="call browser_open_page first"):
        browser_tools.get_page_elements("missing")


def test_submit_reports_native_validation_failure(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "invalid-submit")
    result = browser_tools.submit_form("#application", "invalid-submit", wait_seconds=0)
    assert result["success"] is False
    assert result["validation_passed"] is False
    assert result["submit_triggered"] is False
    assert result["validation_errors"][0]["id"] == "candidate-name"


def test_selected_radio_cannot_be_falsely_reported_unchecked(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "radio")
    session = browser_tools._get_session("radio")
    session.driver.execute_script(
        "document.body.insertAdjacentHTML('beforeend', '<input id=radio-one type=radio name=choice checked>');"
    )
    result = browser_tools.fill_fields({"#radio-one": False}, session_id="radio")
    assert result["success"] is False
    assert "cannot be unchecked" in result["errors"]["#radio-one"]
    assert session.driver.find_element("css selector", "#radio-one").is_selected()


def test_separate_upload_tool_supports_file_input(local_site, tmp_path):
    upload = tmp_path / "cv.pdf"
    upload.write_bytes(b"%PDF-test")
    _open_or_skip(f"{local_site.base_url}/form", "upload")
    result = browser_tools.upload_file("#resume", [str(upload)], "upload")
    assert result["success"] is True
    assert result["files_uploaded"] == 1
    assert result["file_names"] == ["cv.pdf"]


def test_wait_for_dynamic_element(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "dynamic-wait")
    session = browser_tools._get_session("dynamic-wait")
    session.driver.execute_script(
        "setTimeout(() => document.body.insertAdjacentHTML('beforeend', "
        "'<button id=dynamic-button>Ready</button>'), 100);"
    )

    result = browser_tools.wait_for_element(
        "#dynamic-button", "dynamic-wait", state="clickable", timeout_seconds=2
    )

    assert result["success"] is True
    assert result["selector"] == "#dynamic-button"
    assert result["state"] == "clickable"
    assert result["tag"] == "button"
