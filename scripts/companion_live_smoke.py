"""Live smoke for the unpacked companion in a disposable Chromium profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chrome_bridge import (  # noqa: E402
    DEFAULT_TAB_GROUP,
    ChromeBridgeDriver,
    get_chrome_bridge,
    list_current_chrome_tabs,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium-binary", required=True, type=Path)
    parser.add_argument("--url", default="https://example.com")
    args = parser.parse_args()
    chromium = args.chromium_binary.expanduser().resolve(strict=True)
    extension = (ROOT / "chrome-extension").resolve(strict=True)
    profile = Path(tempfile.mkdtemp(prefix="web-search-neo-companion-"))
    process: subprocess.Popen | None = None
    driver: ChromeBridgeDriver | None = None
    try:
        bridge = get_chrome_bridge()
        bridge.start()
        process = subprocess.Popen(
            [
                str(chromium),
                f"--user-data-dir={profile}",
                f"--disable-extensions-except={extension}",
                f"--load-extension={extension}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not bridge.wait_connected(15.0):
            raise RuntimeError(f"Companion did not connect: {bridge.status(0.0)}")
        driver = ChromeBridgeDriver(bridge=bridge, tab_group=DEFAULT_TAB_GROUP)
        driver.get(args.url)
        png = driver.get_screenshot_as_png()
        tabs = list_current_chrome_tabs(1.0)["tabs"]
        controlled = next(tab for tab in tabs if int(tab["id"]) == driver.tab_id)
        report = {
            "connected": True,
            "extension_version": bridge.browser_info.get("extension_version"),
            "tab_id": driver.tab_id,
            "tab_group": controlled.get("group"),
            "url": driver.current_url,
            "title": driver.title,
            "screenshot_png": png.startswith(b"\x89PNG\r\n\x1a\n"),
            "screenshot_bytes": len(png),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["tab_group"] == DEFAULT_TAB_GROUP and report["screenshot_png"] else 1
    finally:
        if driver is not None:
            driver.quit()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
