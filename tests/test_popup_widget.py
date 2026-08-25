"""Offline checks for the 1.9.0 companion widget release.

Everything here is static or driven through local stubs: no network, no Chrome,
no bridge, no secrets. The dynamic rendering path runs popup.js under Node
against a fake ``document`` and a fake ``chrome.runtime`` - the same production
file users load.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = PROJECT_ROOT / "chrome-extension"
NODE = shutil.which("node")

POPUP_HTML = (EXTENSION_DIR / "popup.html").read_text(encoding="utf-8")
POPUP_JS = (EXTENSION_DIR / "popup.js").read_text(encoding="utf-8")
POPUP_CSS = (EXTENSION_DIR / "popup.css").read_text(encoding="utf-8")
MANIFEST = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))

requires_node = pytest.mark.skipif(
    NODE is None, reason="node is required to exercise the popup script"
)


# --- selector integrity, preview wiring, declaration order -------------------


def test_every_selector_popup_js_uses_exists_in_popup_html() -> None:
    targets = set(re.findall(r'querySelector\("#([^"]+)"\)', POPUP_JS))
    assert targets, "popup.js must address the widget through ids"
    missing = sorted(targets - _popup_ids())
    assert not missing, f"popup.js queries ids popup.html does not render: {missing}"


def test_no_dangling_status_card_reference() -> None:
    # The old name pointed at nothing and threw on every render; the state card
    # is #state-card now, reached only through a defined binding.
    assert "statusCardNode" not in POPUP_JS
    assert 'querySelector("#state-card")' not in POPUP_JS or 'id="state-card"' in POPUP_HTML
    for name in re.findall(r"const (\w+) = document\.querySelector", POPUP_JS):
        assert POPUP_JS.count(name) >= 1  # every binding is at least declared once


def test_preview_state_is_declared_before_the_preview_boots() -> None:
    boot = POPUP_JS.index("startPreview(String(PREVIEW))")
    for declaration in (
        "let previewTabs", "let previewCap", "let previewCeiling", "let previewPort",
        "let previewVersion", "let previewUpdate", "let previewEnabled",
        "function previewSend", "function startPreview",
    ):
        found = POPUP_JS.index(declaration)
        assert found < boot, f"{declaration} must precede the preview boot (TDZ)"


def test_preview_harness_drives_the_production_popup_offline() -> None:
    harness = PROJECT_ROOT / "scripts" / "companion-widget-preview.html"
    text = harness.read_text(encoding="utf-8")
    assert "../chrome-extension/popup.html" in text, "the iframe must load the real popup"
    assert "wsn-preview" in text
    for state in ("connected", "connecting", "waiting", "disabled", "error"):
        assert state in text, f"the harness must offer the {state} state"
    assert "chrome." not in re.sub(r"<!--.*?-->", "", text, flags=re.S)


def test_preview_mode_touches_no_chrome_api() -> None:
    send_body = POPUP_JS[POPUP_JS.index("async function send"):POPUP_JS.index("function countdown")]
    assert "PREVIEW" in send_body and "chrome.runtime" in send_body, (
        "preview mode must answer from the local fake, never chrome.runtime"
    )
    guard = POPUP_JS.index("openGitHubButton.addEventListener")
    guard_body = POPUP_JS[guard:guard + 200]
    assert "if (PREVIEW) return;" in guard_body
    assert 'location.protocol === "file:"' in POPUP_JS
    assert 'location.protocol === "http:"' in POPUP_JS
    assert 'location.hostname' in POPUP_JS
    assert 'location.protocol === "chrome-extension:"' not in POPUP_JS


def _wrap_async(body: str) -> str:
    """Run top-level ``return``-bearing module code as one async IIFE."""
    indented = "\n".join("  " + line for line in body.splitlines())
    return (
        "globalThis.window = globalThis;\n"
        "(async () => {\n"
        f"{indented}\n"
        "})().then(result => { console.log(JSON.stringify(result)); })"
        ".catch(error => { console.error(error); process.exit(1); });\n"
    )


def _popup_ids() -> set[str]:
    return set(re.findall(r'id="([^"]+)"', POPUP_HTML))


# --- language ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        EXTENSION_DIR / "popup.html",
        EXTENSION_DIR / "popup.css",
        EXTENSION_DIR / "popup.js",
        EXTENSION_DIR / "manifest.json",
    ],
)
def test_the_widget_speaks_english_only(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    cyrillic = re.findall(r"[А-Яа-яЁё]+", text)
    assert not cyrillic, f"{path.name} carries non-English UI copy: {cyrillic[:5]}"


def test_the_changelog_is_english_and_general() -> None:
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert not re.findall(r"[А-Яа-яЁё]", changelog), "changelog still has Russian sections"
    for vendor in ("Gmail", "Telegram", "Teamtailor", "Workable", "hh-reply", "proton-send"):
        assert vendor.lower() not in changelog.lower(), (
            f"domain-specific example '{vendor}' should be generalized out of the changelog"
        )


# --- accessibility -----------------------------------------------------------


def test_every_control_is_reachable_without_sight() -> None:
    open_tags = list(re.finditer(r"<button([^>]*)>", POPUP_HTML))
    bodies = re.findall(r"<button[^>]*>(.*?)</button>", POPUP_HTML, re.S)
    assert len(open_tags) == len(bodies) >= 6
    for opening, body in zip(open_tags, bodies):
        label = re.search(r'aria-label="([^"]+)"', opening.group(1))
        visible = re.sub(r"<[^>]+>", "", body).strip()
        assert label or visible, f"a button has neither aria-label nor visible text: {body[:40]}"

    svgs = re.findall(r"<svg[^>]*>", POPUP_HTML)
    assert len(svgs) >= 5, "the icon-first widget must ship its own inline icons"
    for svg in svgs:
        assert 'aria-hidden="true"' in svg or 'role="img"' in svg, f"unlabelled icon: {svg}"


def test_status_is_a_live_region_and_states_have_words() -> None:
    assert 'role="status"' in POPUP_HTML
    assert 'aria-live="polite"' in POPUP_HTML
    # One word per connection state comes from the worker; the markup starts
    # honest (loading) instead of pretending to be connected.
    assert 'data-state="loading"' in POPUP_HTML


def test_reduced_motion_disables_the_animation() -> None:
    assert "@keyframes pulse-ring" in POPUP_CSS, "waiting/connecting must animate"
    match = re.search(r"@media \(prefers-reduced-motion:\s*reduce\)\s*\{(.*)\}", POPUP_CSS, re.S)
    assert match, "popup.css must honour prefers-reduced-motion"
    block = match.group(1)
    assert "animation-duration" in block and "transition-duration" in block


def test_focus_and_contrast_tokens_exist() -> None:
    assert ":focus-visible" in POPUP_CSS, "keyboard focus must be visible"
    assert "--muted:" in POPUP_CSS and "--text:" in POPUP_CSS


# --- shortcut ----------------------------------------------------------------


def test_the_shortcut_opens_the_panel_and_nothing_else() -> None:
    command = MANIFEST["commands"]["_execute_action"]
    assert command["suggested_key"]["default"] == "Alt+Shift+N"
    assert command["suggested_key"]["mac"] == "Alt+Shift+N"
    assert MANIFEST["action"]["default_popup"] == "popup.html"
    description = command["description"]
    assert description and description.isascii(), "shortcut description must be English"

    # Opening the popup must not navigate or control anything: the only tab
    # creation lives behind the explicit GitHub button click.
    github_button = POPUP_JS.index("openGitHubButton.addEventListener")
    create_calls = [match.start() for match in re.finditer(r"chrome\.tabs\.create", POPUP_JS)]
    assert create_calls and all(position > github_button for position in create_calls), (
        "a tab may only be opened by the GitHub button click handler"
    )
    assert "chrome.tabs.update" not in POPUP_JS
    assert not re.search(r"^location\.(href|assign|replace)", POPUP_JS, re.M)


# --- versions ----------------------------------------------------------------


def test_every_user_facing_version_reads_1_9_0() -> None:
    expected = main.__version__
    assert MANIFEST["version"] == expected
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^\s*version\s*=\s*"([^"]+)"', pyproject, re.M)
    assert declared and declared.group(1) == expected
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.splitlines()[2].startswith(f"## {expected}")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    install = (PROJECT_ROOT / "INSTALL.md").read_text(encoding="utf-8")
    for name, text in (("README", readme), ("INSTALL", install)):
        assert f"is version {expected}" in text, f"{name} does not announce {expected}"
    # The preview driver's fallback and the manifest cannot drift apart.
    assert f'"{expected}"' in POPUP_JS


def test_readme_carries_the_cover_and_both_widget_shots() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/assets/web-search-neo-cover.png" in readme
    assert "docs/assets/companion-widget-connected.png" in readme
    assert "docs/assets/companion-widget-waiting.png" in readme


# --- domain neutrality -------------------------------------------------------


def test_macros_stay_project_local_and_domain_neutral() -> None:
    architecture = (PROJECT_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    skill = (PROJECT_ROOT / "skills/web-search-neo/SKILL.md").read_text(encoding="utf-8")
    assert "domain-neutral" in architecture and "domain-neutral" in skill
    for host in ("hh.ru", "workable", "teamtailor", "linkedin.com/jobs"):
        assert host.lower() not in skill.lower(), f"bundled skill names a domain host: {host}"
    # No concrete macro files ship with the server: they belong to the project.
    bundled: list[Path] = []
    for directory in ("skills", "docs", "scripts", "chrome-extension"):
        bundled.extend((PROJECT_ROOT / directory).rglob("*.json"))
        bundled.extend((PROJECT_ROOT / directory).glob("*.json"))
    macro_like = [
        path for path in bundled
        if '"steps"' in path.read_text(encoding="utf-8") or "placeholders" in path.read_text(
            encoding="utf-8"
        )
    ]
    assert not macro_like, f"macro files must not be bundled: {macro_like}"


def test_msp_server_logs_stay_ignored() -> None:
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^msp_server\.log\*$", gitignore, re.M)


def test_screenshot_harness_exists_and_is_readonly() -> None:
    harness = PROJECT_ROOT / "scripts" / "widget_screenshots.py"
    text = harness.read_text(encoding="utf-8")
    assert "popup.html" in text, "the harness must reuse the production popup"
    assert "sendMessage" in text, "state arrives only through a mocked chrome.runtime"
    for banned in ("import socket", "websocket", "BridgeDaemon", "ChromeBridge", "BRIDGE_TOKEN"):
        assert banned not in text
    assert "companion-widget-connected.png" in text
    assert "companion-widget-waiting.png" in text


# --- status safety -----------------------------------------------------------


def test_status_payload_carries_no_secret() -> None:
    source = (EXTENSION_DIR / "service-worker.js").read_text(encoding="utf-8")
    start = source.index("function connectionStatus()")
    body = source[start : start + 900]
    assert "token" not in body.lower(), "connectionStatus must not expose the shared secret"


def test_popup_never_touches_the_shared_secret() -> None:
    assert "BRIDGE_TOKEN" not in POPUP_JS
    assert "token" not in POPUP_JS.lower()


# --- dynamic rendering (Node, fully stubbed) ---------------------------------


def _wrap_async(body: str) -> str:
    return (
        "const result = await (async () => {\n" + body + "\n})();\n"
        "process.stdout.write(JSON.stringify(result ?? null));\n"
        "process.exit(0);\n"
    )


def _node_render_probe():
    """Import popup.js once under stubs, drive render(), and read the widget."""
    ids = sorted(_popup_ids())
    module = f"""
const callbacks = {{}};
const ids = {json.dumps(ids)};
const nodes = new Map(ids.map(id => [id, {{
  textContent: "", title: "", value: "", dataset: {{}}, checked: false, disabled: false,
  open: false, style: {{width: ""}},
  addEventListener(type, callback) {{ callbacks[`${{id}}:${{type}}`] = callback; }},
}}]));
globalThis.document = {{
  querySelector: selector => nodes.get(selector.slice(1)),
  activeElement: null,
}};
const payloads = [];
globalThis.chrome = {{
  runtime: {{
    sendMessage: async message => {{
      payloads.push(message);
      if (payloads.length > 1) throw new Error("status socket closed");
      return STATUS;
    }},
  }},
  tabs: {{create: () => {{}}}},
}};
globalThis.fetch = async () => ({{
  ok: true, json: async () => ([{{tag_name: "v{main.__version__}", html_url: ""}}]),
}});
globalThis.setInterval = () => 0;
await import({json.dumps((EXTENSION_DIR / "popup.js").as_uri())});
await new Promise(resolve => setTimeout(resolve, 20));

const snap = () => Object.fromEntries(["panel", "status", "next-attempt", "tabs",
  "max-sessions-value", "max-sessions-ceiling", "meter-fill", "message", "version"]
  .map(id => {{
    const node = nodes.get(id);
    return [id.replace(/-/g, "_"), node && {{
      text: node.textContent,
      state: node.dataset ? node.dataset.state : undefined,
      width: node.style ? node.style.width : undefined,
    }}];
  }}));

const results = {{initial: snap()}};
for (const [name, payload] of Object.entries(SCENARIOS)) {{
  window.__wsn.render(payload);
  results[name] = snap();
}}
return results;
"""
    scenarios = {
        "disabled": {
            "enabled": False, "connected": False, "connecting": False,
            "state": "disabled", "failure_kind": None, "controlled_tabs": 0,
            "max_sessions": 8, "max_sessions_ceiling": 64, "next_attempt_at": 0,
        },
        "waiting": {
            "enabled": True, "connected": False, "connecting": False,
            "state": "waiting", "failure_kind": "transport", "controlled_tabs": 3,
            "max_sessions": 8, "max_sessions_ceiling": 64,
            "next_attempt_at": "JS_NOW_PLUS_230000",
        },
        "connecting": {
            "enabled": True, "connected": False, "connecting": True,
            "state": "connecting", "failure_kind": None, "controlled_tabs": 1,
            "max_sessions": 8, "max_sessions_ceiling": 64, "next_attempt_at": 0,
        },
        "error": {
            "enabled": True, "connected": False, "connecting": False,
            "state": "error", "failure_kind": "auth", "controlled_tabs": 0,
            "max_sessions": 8, "max_sessions_ceiling": 64, "next_attempt_at": 0,
        },
        "capacity": {
            "enabled": True, "connected": True, "connecting": False,
            "state": "connected", "failure_kind": None, "controlled_tabs": 7,
            "max_sessions": 32, "max_sessions_ceiling": 64, "next_attempt_at": 0,
        },
    }
    prelude = (
        "globalThis.window = globalThis;\n"
        f"const STATUS = {json.dumps({
            'enabled': True, 'connected': True, 'connecting': False, 'state': 'connected',
            'failure_kind': None, 'controlled_tabs': 2, 'max_sessions': 8,
            'max_sessions_ceiling': 64, 'bridge_url': 'ws://127.0.0.1:8765',
            'bridge_port': 8765, 'default_bridge_port': 8765, 'next_attempt_at': 0,
            'version': main.__version__,
        })};\n"
        "const SCENARIOS = "
        + json.dumps(scenarios).replace('"JS_NOW_PLUS_230000"', "Date.now() + 230000")
        + ";\n"
    )
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", _wrap_async(prelude + module)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@requires_node
def test_the_widget_renders_each_real_state_from_the_worker() -> None:
    result = _node_render_probe()

    initial = result["initial"]
    assert initial["panel"]["state"] == "connected"
    assert initial["status"]["text"] == "Connected"
    assert initial["tabs"]["text"] == "2"
    assert initial["max_sessions_value"]["text"] == "8"
    assert initial["max_sessions_ceiling"]["text"] == "64"
    assert initial["meter_fill"]["width"] == "12.5%"
    assert initial["version"]["text"] == main.__version__
    # Connected means nothing to count down to: the subtext says where it is.
    assert initial["next_attempt"]["text"] == "127.0.0.1:8765"

    assert result["disabled"]["panel"]["state"] == "disabled"
    assert result["disabled"]["status"]["text"] == "Disabled"

    assert result["waiting"]["panel"]["state"] == "waiting"
    assert result["waiting"]["status"]["text"] == "Waiting"
    assert re.fullmatch(r"retry in \d+m", result["waiting"]["next_attempt"]["text"]), (
        "a deliberate backoff must show its countdown, not look broken"
    )

    assert result["connecting"]["panel"]["state"] == "connecting"
    assert result["connecting"]["status"]["text"].startswith("Connecting")

    assert result["error"]["panel"]["state"] == "error"
    assert "setup_current_chrome" in result["error"]["next_attempt"]["text"]

    capacity = result["capacity"]
    assert capacity["tabs"]["text"] == "7"
    assert capacity["max_sessions_value"]["text"] == "32"
    assert capacity["meter_fill"]["width"] == "50.0%"


@requires_node
def test_a_failed_status_poll_reports_the_real_error_instead_of_a_state() -> None:
    module = f"""
const ids = {json.dumps(sorted(_popup_ids()))};
const nodes = new Map(ids.map(id => [id, {{
  textContent: "", title: "", value: "", dataset: {{}}, checked: false, disabled: false,
  open: false, style: {{width: ""}}, addEventListener() {{}},
}}]));
globalThis.document = {{
  querySelector: selector => nodes.get(selector.slice(1)),
  activeElement: null,
}};
globalThis.chrome = {{
  runtime: {{sendMessage: async () => {{ throw new Error("worker is gone"); }}}},
  tabs: {{create: () => {{}}}},
}};
globalThis.fetch = async () => ({{
  ok: true, json: async () => ([{{tag_name: "v{main.__version__}", html_url: ""}}]),
}});
globalThis.setInterval = () => 0;
await import({json.dumps((EXTENSION_DIR / "popup.js").as_uri())});
await new Promise(resolve => setTimeout(resolve, 20));
return {{
  panel: nodes.get("panel").dataset.state,
  status: nodes.get("status").textContent,
  message: nodes.get("message").textContent,
}};
"""
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", _wrap_async(module)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["panel"] == "error"
    assert outcome["message"] == "worker is gone", "the widget must show the real failure"
