"""Tab groups take the colour of their liveliest driven tab, and get their own colour back.

Runs chrome-extension/tab-status.js under Node against a fake chrome.tabs /
chrome.tabGroups / chrome.storage.session, with no browser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXTENSION_DIR = Path(__file__).resolve().parents[1] / "chrome-extension"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is required to exercise the companion modules")
def test_groups_follow_the_agent_and_get_their_colour_back() -> None:
    module = f"""
const groups = {{10: {{id: 10, color: "purple"}}, 20: {{id: 20, color: "blue"}}}};
const tabs = {{1: {{id: 1, groupId: 10}}, 2: {{id: 2, groupId: 10}}, 3: {{id: 3, groupId: 20}},
               4: {{id: 4, groupId: -1}}}};
const store = {{}};
const updates = [];
globalThis.chrome = {{
  tabs: {{get: async id => tabs[id] || Promise.reject(new Error("gone"))}},
  tabGroups: {{
    get: async id => ({{...groups[id]}}),
    update: async (id, change) => {{ updates.push([id, change.color]); Object.assign(groups[id], change); }},
  }},
  storage: {{session: {{
    get: async key => ({{[key]: store[key]}}),
    set: async value => Object.assign(store, value),
  }}}},
  runtime: {{getManifest: () => ({{}})}},
}};
const status = await import({json.dumps((EXTENSION_DIR / "tab-status.js").as_uri())});
const phaseOf = entry => entry.phase;
const settle = () => new Promise(resolve => setTimeout(resolve, 400));

status.syncGroups({{"1": {{tabId: 1, phase: "recent"}}, "2": {{tabId: 2, phase: "active"}},
                   "3": {{tabId: 3, phase: "recent"}}, "4": {{tabId: 4, phase: "active"}}}},
                  Date.now(), phaseOf);
await settle();
const working = {{10: groups[10].color, 20: groups[20].color}};
status.setPausedLook(true);
await settle();
const paused = {{10: groups[10].color, 20: groups[20].color}};
status.setPausedLook(false);
status.syncGroups({{"3": {{tabId: 3, phase: "recent"}}}}, Date.now(), phaseOf);
await settle();
const afterTen = {{10: groups[10].color, 20: groups[20].color}};
status.syncGroups({{}}, Date.now(), phaseOf);
await settle();
return {{working, paused, afterTen, done: {{10: groups[10].color, 20: groups[20].color}},
        writes: updates.length, stored: store.agent_group_colors}};
"""
    program = (
        "const result = await (async () => {\n" + module + "\n})();\n"
        "process.stdout.write(JSON.stringify(result));\n"
    )
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", program],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout)
    # The liveliest tab wins the group; an ungrouped tab touches nothing.
    assert out["working"] == {"10": "green", "20": "yellow"}
    assert out["paused"] == {"10": "orange", "20": "orange"}
    # A group with no driven tab left gets its own colour back, the other keeps going.
    assert out["afterTen"] == {"10": "purple", "20": "yellow"}
    assert out["done"] == {"10": "purple", "20": "blue"}
    assert out["stored"] == {"original": {}, "applied": {}}
    assert out["writes"] == 7, "colours are written only when they change"
