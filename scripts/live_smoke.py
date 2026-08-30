"""Harmless public-network smoke checks; not part of deterministic pytest runs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_search_neo import browser_tools  # noqa: E402
from web_search_neo import main  # noqa: E402
from web_search_neo import msp_search  # noqa: E402


def main_smoke() -> int:
    report: dict = {}
    try:
        report["search"] = msp_search.search_web(
            "Unity C# developer official documentation",
            num=3,
            timeout_seconds=8,
            fresh=True,
        )

        parallel = asyncio.run(
            main.browser_open_pages(
                ["https://example.com", "https://www.python.org"],
                session_ids=["live-example", "live-python"],
                width=900,
                height=600,
                profile_mode="temporary",
            )
        )
        report["parallel_pages"] = parallel
        browser_tools.close_all_sessions()

        session_id = "selenium-live"
        browser_tools.open_page(
            "https://www.selenium.dev/selenium/web/web-form.html",
            session_id,
            1100,
            700,
            20,
            profile_mode="temporary",
        )
        report["form_fill"] = browser_tools.fill_fields(
            {
                "#my-text-id": "Web Search Neo live test",
                "textarea[name='my-textarea']": "Rendered form interaction verified",
                "select[name='my-select']": "2",
                "#my-check-2": True,
                "#my-radio-2": True,
            },
            session_id=session_id,
        )
        report["file_upload"] = browser_tools.upload_file(
            "input[name='my-file']", [str(ROOT / "requirements.txt")], session_id
        )
        png = browser_tools.screenshot(session_id, 1100, 700, False)
        report["screenshot"] = {
            "png": png.startswith(b"\x89PNG\r\n\x1a\n"),
            "dimensions": struct.unpack(">II", png[16:24]),
            "bytes": len(png),
        }
        report["form_submit"] = browser_tools.submit_form(
            "form", session_id, "button[type='submit']", 1
        )
    finally:
        browser_tools.close_all_sessions()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    success = (
        report.get("search", {}).get("success")
        and report.get("parallel_pages", {}).get("success_count") == 2
        and report.get("form_fill", {}).get("success")
        and report.get("file_upload", {}).get("success")
        and report.get("screenshot", {}).get("dimensions") == (1100, 700)
        and report.get("form_submit", {}).get("success")
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main_smoke())
