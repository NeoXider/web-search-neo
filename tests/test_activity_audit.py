"""Execute the badge JavaScript across late-image and teardown races."""

import json
import shutil
import subprocess

import pytest

from web_search_neo.sessions import activity


@pytest.mark.parametrize("stop", [True, False])
def test_late_image_cannot_restore_a_stopped_or_expired_badge(stop):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute companion JavaScript")
    harness = r"""
const fs = require('fs');
const {source, stop} = JSON.parse(fs.readFileSync(0, 'utf8'));
let badge = null, timeout, image;
global.window = {};
global.setTimeout = fn => {timeout = fn; return 1;};
global.clearTimeout = () => {};
global.Image = class {constructor() {image = this;}};
const head = {
  appendChild(link) {badge = link; link.parentNode = head;},
  removeChild(link) {if (badge === link) badge = null;}
};
global.document = {
  head, documentElement: head, readyState: 'complete',
  querySelectorAll() {return [{href: 'https://example.test/icon.png'}];},
  querySelector() {return badge;},
  createElement(tag) {
    if (tag === 'canvas') return {getContext() {return null;}};
    return {setAttribute() {}};
  }
};
eval(source);
if (stop) window.__wsnActivity.stop(); else timeout();
image.onload();
image.onerror();
if (badge !== null) throw new Error('late image revived the badge');
if (!stop) {
  window.__wsnActivity.ping();
  image.onerror();
  if (!badge) throw new Error('new activity should re-arm the badge');
}
"""
    result = subprocess.run(
        [node, "-e", harness],
        input=json.dumps({"source": activity._TAB_ACTIVITY_SOURCE, "stop": stop}),
        text=True, capture_output=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr


def test_opt_out_removes_previously_installed_badge():
    from types import SimpleNamespace

    calls = []
    driver = SimpleNamespace(
        execute_script=lambda *args: calls.append(args),
        execute_cdp_cmd=lambda *args: calls.append(args),
    )
    session = SimpleNamespace(driver=driver, headless=False, tab_activity_script_id="old")
    activity._apply_tab_activity(session, "s", label_tab=False)
    assert session.tab_activity_script_id is None
    assert any(call[0] == "Page.removeScriptToEvaluateOnNewDocument" for call in calls)
