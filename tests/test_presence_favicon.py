"""The tab-strip mark must sit in the corner of the page's own icon, never replace it.

The installer runs in Node against a small fake DOM, so these cases need no Chrome.
They cover the two ways the whole icon used to be redrawn: the older activity badge's
full-size picture being taken for the page's icon, and a single-page app whose icon
link arrives after the mark was first painted.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from web_search_neo import agent_presence
from web_search_neo.sessions import activity

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run the page script")

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const scenario = process.argv[3];

const log = {srcs: [], draws: [], observers: []};
let canvases = 0;

class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.parentNode = null; this.children = []; this.isConnected = false; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  get href() { return this.attrs.href || ""; }
  set href(v) { this.attrs.href = String(v); }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this; child.isConnected = true; this.children.push(child);
    return child;
  }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null; child.isConnected = false;
    return child;
  }
  getContext() {
    return new Proxy({}, {
      get(target, key) {
        if (key in target) return target[key];
        return (...args) => { if (key === "drawImage") log.draws.push(args[0] && args[0]._src); };
      },
      set(target, key, value) { target[key] = value; return true; },
    });
  }
  toDataURL() { canvases += 1; return "data:image/png;mark" + canvases; }
}

const head = new El("head");
head.isConnected = true;
const body = new El("body");

function links(selector) {
  const all = head.children.filter((node) => node.tag === "link");
  if (selector === "link") return all;
  const attribute = /^link\[([a-z-]+)\]$/.exec(selector);
  return attribute ? all.filter((node) => node.hasAttribute(attribute[1])) : [];
}

class FakeImage {
  set src(value) {
    this._src = value;
    log.srcs.push(value);
    const loads = !/favicon\.ico$/.test(value);
    setTimeout(() => { const fn = loads ? this.onload : this.onerror; if (fn) fn(); }, 5);
  }
  get src() { return this._src; }
}

const sandbox = {
  document: {
    head, body, documentElement: body, readyState: "complete",
    querySelectorAll: (selector) => links(selector),
    createElement: (tag) => new El(tag),
    addEventListener() {}, removeEventListener() {},
  },
  location: {href: "https://chat.example/c/1"},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  Image: FakeImage,
  MutationObserver: class { constructor(fn) { log.observers.push(fn); } observe() {} disconnect() {} },
  matchMedia: () => ({matches: false}),
  requestAnimationFrame() {},
  URL, Date, setTimeout, clearTimeout, console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const own = () => head.children.find((node) => node.hasAttribute("data-wsn-presence"));

(async () => {
  if (scenario === "older-badge-link") {
    const robot = new El("link");
    robot.setAttribute("rel", "icon");
    robot.setAttribute("data-wsn-activity", "1");
    robot.href = "data:image/svg+xml,ROBOT";
    head.appendChild(robot);
    vm.runInContext(source, sandbox);
    sandbox.window.__wsnPresence.note({action: "click", flash: false});
    await wait(150);
    console.log(JSON.stringify({
      olderLinks: links("link[data-wsn-activity]").length,
      robotRequested: log.srcs.some((src) => src.indexOf("ROBOT") !== -1),
      ownHref: own() ? own().href : null,
    }));
  } else if (scenario === "left-parked-by-an-earlier-build") {
    const parked = new El("link");
    parked.setAttribute("rel", "wsn-parked-icon");
    parked.setAttribute("data-wsn-rel", "icon");
    parked.href = "https://chat.example/real-icon.png";
    head.appendChild(parked);
    vm.runInContext(source, sandbox);
    sandbox.window.__wsnPresence.note({action: "click", flash: false});
    await wait(150);
    const marked = {ownHref: own() ? own().href : null, drawn: log.draws.slice()};
    sandbox.window.__wsnPresence.restore();
    console.log(JSON.stringify({marked, restoredRel: parked.getAttribute("rel"), ownLeft: !!own()}));
  } else if (scenario === "page-icon") {
    const real = new El("link");
    real.setAttribute("rel", "icon");
    real.href = "https://chat.example/real-icon.png";
    head.appendChild(real);
    vm.runInContext(source, sandbox);
    sandbox.window.__wsnPresence.note({action: "click", flash: false});
    await wait(150);
    const marked = {ownHref: own() ? own().href : null, pageRel: real.getAttribute("rel"), drawn: log.draws.slice()};
    sandbox.window.__wsnPresence.restore();
    console.log(JSON.stringify({
      marked,
      restoredRel: real.getAttribute("rel"),
      ownLeft: !!own(),
      parkedAttr: real.hasAttribute("data-wsn-rel"),
    }));
  } else if (scenario === "icon-arrives-late") {
    vm.runInContext(source, sandbox);
    sandbox.window.__wsnPresence.note({action: "click", flash: false});
    await wait(150);
    const before = own() ? own().href : null;
    const drawnBefore = log.draws.slice();
    const real = new El("link");
    real.setAttribute("rel", "icon");
    real.href = "https://chat.example/real-icon.png";
    head.appendChild(real);
    log.observers.forEach((fn) => fn());
    await wait(300);
    console.log(JSON.stringify({
      before, drawnBefore, drawnAfter: log.draws,
      after: own() ? own().href : null,
      pageLinkParked: real.getAttribute("rel"),
    }));
  }
  process.exit(0);
})();
"""


def _run(tmp_path, scenario: str) -> dict:
    source = tmp_path / "presence.js"
    source.write_text(agent_presence.install_source(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run(
        ["node", str(harness), str(source), scenario],
        capture_output=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])


def test_the_older_badges_picture_is_never_taken_for_the_pages_icon(tmp_path):
    result = _run(tmp_path, "older-badge-link")
    assert result["robotRequested"] is False
    assert result["olderLinks"] == 0, "the older badge's link must be gone once this script owns the icon"
    assert str(result["ownHref"]).startswith("data:image/png")


def test_the_mark_goes_over_the_pages_icon_and_the_icon_comes_back_on_restore(tmp_path):
    # The overlay once overwrote the list of parked icon links with a boolean at install,
    # so parking the first real icon threw: the mark never appeared and the icon never came back.
    result = _run(tmp_path, "page-icon")
    marked = result["marked"]
    assert str(marked["ownHref"]).startswith("data:image/png")
    assert marked["pageRel"] == "wsn-parked-icon"
    assert "https://chat.example/real-icon.png" in marked["drawn"], "the page's own icon is the base"
    assert result["restoredRel"] == "icon"
    assert result["parkedAttr"] is False
    assert result["ownLeft"] is False


def test_an_icon_an_earlier_build_parked_is_read_and_handed_back(tmp_path):
    # A tab that outlived the upgrade still has the link the broken build parked and could not
    # return. Without adopting it the new build saw no icon and kept a bare corner mark.
    result = _run(tmp_path, "left-parked-by-an-earlier-build")
    assert "https://chat.example/real-icon.png" in result["marked"]["drawn"]
    assert str(result["marked"]["ownHref"]).startswith("data:image/png")
    assert result["restoredRel"] == "icon"
    assert result["ownLeft"] is False


def test_an_icon_that_arrives_after_the_first_paint_gets_the_mark_over_it(tmp_path):
    result = _run(tmp_path, "icon-arrives-late")
    assert result["before"], "the bare corner mark is painted while the page has no icon"
    assert "https://chat.example/real-icon.png" not in result["drawnBefore"]
    assert "https://chat.example/real-icon.png" in result["drawnAfter"], "the page's icon is the base once it exists"
    assert result["after"] != result["before"]
    assert result["pageLinkParked"] == "wsn-parked-icon"


def test_the_older_badge_never_paints_a_full_size_picture():
    source = activity._TAB_ACTIVITY_SOURCE
    assert "robotIcon" not in source and "image/svg+xml" not in source
    assert "cornerIcon" in source
    # A dot over a missing icon is retried once, so a late-arriving icon is composited.
    assert "state.retried" in source


def test_the_older_badge_ignores_links_it_does_not_own():
    source = activity._TAB_ACTIVITY_SOURCE
    assert ':not([data-wsn-activity]):not([data-wsn-presence])' in source


def test_the_installed_script_is_replaced_by_the_fixed_one():
    assert agent_presence.PRESENCE_VERSION >= 16
