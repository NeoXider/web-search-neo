"""Render the production companion widget into PNG screenshots, offline.

Loads the real ``chrome-extension/popup.html`` (its own CSS and JS) in a real
headless Chrome page and injects one thing before any page script runs: a
mocked ``chrome.runtime`` that answers ``companion.status`` with fixed,
harmless state, plus a stubbed ``fetch`` so the GitHub release check never
touches the network. The harness opens no tabs, attaches to nothing, reads no
bridge port, and needs no secret - the widget is photographed exactly as users
see it, fed only canned numbers.

Usage::

    python scripts/widget_screenshots.py                 # connected + waiting
    python scripts/widget_screenshots.py --all           # every state
    python scripts/widget_screenshots.py --out-dir docs/assets

Outputs by default:
    docs/assets/companion-widget-connected.png
    docs/assets/companion-widget-waiting.png
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "chrome-extension"
DEFAULT_OUT_DIR = ROOT / "docs" / "assets"

# The one shape the popup consumes: the service worker's connectionStatus()
# payload. Nothing here is read from a live browser or daemon.
STATES = {
    "connected": {
        "enabled": True,
        "connected": True,
        "connecting": False,
        "state": "connected",
        "failure_kind": None,
        "controlled_tabs": 2,
        "max_sessions": 8,
        "default_max_sessions": 8,
        "max_sessions_ceiling": 64,
        "bridge_url": "ws://127.0.0.1:8765",
        "bridge_port": 8765,
        "default_bridge_port": 8765,
        "next_attempt_at": 0,
    },
    "waiting": {
        "enabled": True,
        "connected": False,
        "connecting": False,
        "state": "waiting",
        "failure_kind": "transport",
        "controlled_tabs": 3,
        "max_sessions": 8,
        "default_max_sessions": 8,
        "max_sessions_ceiling": 64,
        "bridge_url": "ws://127.0.0.1:8765",
        "bridge_port": 8765,
        "default_bridge_port": 8765,
        # Relative to injection time, so the countdown renders a stable value.
        "next_attempt_at": "NOW_PLUS_300000",
    },
    "connecting": {
        "enabled": True,
        "connected": False,
        "connecting": True,
        "state": "connecting",
        "failure_kind": None,
        "controlled_tabs": 0,
        "max_sessions": 8,
        "default_max_sessions": 8,
        "max_sessions_ceiling": 64,
        "bridge_url": "ws://127.0.0.1:8765",
        "bridge_port": 8765,
        "default_bridge_port": 8765,
        "next_attempt_at": 0,
    },
    "disabled": {
        "enabled": False,
        "connected": False,
        "connecting": False,
        "state": "disabled",
        "failure_kind": None,
        "controlled_tabs": 0,
        "max_sessions": 8,
        "default_max_sessions": 8,
        "max_sessions_ceiling": 64,
        "bridge_url": "ws://127.0.0.1:8765",
        "bridge_port": 8765,
        "default_bridge_port": 8765,
        "next_attempt_at": 0,
    },
    "error": {
        "enabled": True,
        "connected": False,
        "connecting": False,
        "state": "error",
        "failure_kind": "auth",
        "controlled_tabs": 0,
        "max_sessions": 8,
        "default_max_sessions": 8,
        "max_sessions_ceiling": 64,
        "bridge_url": "ws://127.0.0.1:8765",
        "bridge_port": 8765,
        "default_bridge_port": 8765,
        "next_attempt_at": 0,
    },
}

MOCK_SOURCE = """
window.chrome = {
  runtime: {sendMessage: async () => (%(status)s)},
  tabs: {create: () => undefined},
};
// The release check must stay offline: answer "same version", always.
window.fetch = async () => ({
  ok: true,
  status: 200,
  json: async () => ([{tag_name: %(tag)s, html_url: ""}]),
});
"""


def _manifest_version() -> str:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest["version"])


def _mock_source(state: dict, version: str) -> str:
    return MOCK_SOURCE % {
        "status": json.dumps({**state, "version": version}),
        "tag": json.dumps(f"v{version}"),
    }


def _capture(driver, name: str, out_dir: Path) -> Path:
    target = out_dir / f"companion-widget-{name}.png"
    expected = name if name != "error" else "error"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        rendered = driver.execute_script(
            "const panel = document.querySelector('#panel');"
            "const release = document.querySelector('#release-status');"
            "return [panel && panel.dataset.state, release && release.textContent];"
        )
        if rendered == [expected, "latest"]:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError(f"{name}: widget never reached the mocked state: {rendered}")
    element = driver.find_element("css selector", "#panel")
    element.screenshot(str(target))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--all", action="store_true",
        help="capture every state instead of just connected and waiting",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    from selenium import webdriver

    version = _manifest_version()
    names = list(STATES) if args.all else ["connected", "waiting"]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    options = webdriver.ChromeOptions()
    for flag in (
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=2",
        "--window-size=400,760",
        "--no-first-run",
        "--no-default-browser-check",
    ):
        options.add_argument(flag)

    driver = webdriver.Chrome(options=options)
    written: list[Path] = []
    try:
        for name in names:
            state = dict(STATES[name])
            if state.get("next_attempt_at") == "NOW_PLUS_300000":
                state["next_attempt_at"] = int(time.time() * 1000) + 300_000
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _mock_source(state, version)},
            )
            driver.get((EXTENSION / "popup.html").as_uri())
            written.append(_capture(driver, name, args.out_dir))
    finally:
        driver.quit()

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(f"widget screenshots failed: {error}", file=sys.stderr)
        sys.exit(1)
