"""Run the real performance script against deterministic browser timers in Node."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from web_search_neo.audit.perf import PERF_SCRIPT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node evaluates the page-side script")

_HARNESS = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const {script, scenario} = JSON.parse(fs.readFileSync(0, 'utf8'));
let now = 0, nextId = 0;
const timers = new Map(), listeners = new Map(), observers = [];
const setTimer = (callback, delay) => {
  const id = ++nextId;
  timers.set(id, {at: now + Math.max(0, Number(delay) || 0), callback});
  return id;
};
const document = {
  readyState: scenario.loading ? 'loading' : 'complete',
  querySelectorAll: () => [],
  getElementsByTagName: () => [{}],
};
if (scenario.load_at !== undefined) setTimer(() => {
  document.readyState = 'complete';
  for (const [callback, options] of [...listeners]) {
    if (options.once) listeners.delete(callback);
    callback();
  }
}, scenario.load_at);
const context = vm.createContext({
  document,
  location: {href: 'https://fixture.invalid/perf'},
  performance: {
    now: () => now,
    getEntriesByType: type => type === 'paint' && scenario.paint_at != null && now >= scenario.paint_at
      ? ['first-paint', 'first-contentful-paint'].map(name => ({name, startTime: scenario.paint_at})) : [],
  },
  addEventListener: (name, callback, options = {}) => {
    if (name !== 'load') throw new Error('unexpected event ' + name);
    listeners.set(callback, options);
  },
  removeEventListener: (name, callback) => listeners.delete(callback),
  setTimeout: setTimer,
  clearTimeout: id => timers.delete(id),
  PerformanceObserver: class {
    observe({type}) { observers.push({type, at: now}); }
    takeRecords() { return []; }
    disconnect() {}
  },
});
(async () => {
  let settled = false, result, error;
  vm.runInContext('(async function () {\n' + script + '\n}).call(null, ' + scenario.wait_ms + ')', context)
    .then(value => { result = value; settled = true; }, failure => { error = failure; settled = true; });
  for (let turns = 0; !settled && turns < 10000; turns++) {
    // Drain all Promise reactions before advancing the synthetic clock.
    await new Promise(setImmediate);
    if (settled) break;
    const next = [...timers].sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
    if (!next) throw new Error('script stalled without a timer');
    timers.delete(next[0]);
    now = next[1].at;
    next[1].callback();
  }
  if (error) throw error;
  if (!settled) throw new Error('script did not finish');
  process.stdout.write(JSON.stringify({result, elapsed_ms: now, observers,
    load_listeners: listeners.size, pending_timers: timers.size}));
})().catch(error => { process.stderr.write(String(error.stack)); process.exitCode = 1; });
"""


def _run(**scenario):
    result = subprocess.run(
        [NODE, "-e", _HARNESS],
        input=json.dumps({"script": PERF_SCRIPT, "scenario": scenario}),
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _assert_finished_at(result, readiness_ms):
    assert result["observers"] == [
        {"type": "largest-contentful-paint", "at": readiness_ms},
        {"type": "layout-shift", "at": readiness_ms + 60},
    ]
    assert result["elapsed_ms"] == readiness_ms + 120
    assert result["load_listeners"] == result["pending_timers"] == 0


def test_complete_page_waits_for_delayed_real_paint():
    result = _run(wait_ms=1000, paint_at=276)
    assert result["result"]["fcp"] == result["result"]["fp"] == 276
    _assert_finished_at(result, 280)


def test_load_and_paint_share_one_readiness_budget():
    result = _run(wait_ms=100, loading=True, load_at=70, paint_at=250)
    assert result["result"]["ready_state"] == "complete"
    assert result["result"]["fcp"] is None
    _assert_finished_at(result, 100)


def test_paint_arriving_during_observer_windows_is_reported():
    result = _run(wait_ms=100, paint_at=150)
    assert result["result"]["fcp"] == result["result"]["fp"] == 150
    _assert_finished_at(result, 100)


def test_early_load_and_existing_paint_remove_the_unused_deadline_timer():
    result = _run(wait_ms=1000, loading=True, load_at=10, paint_at=5)
    assert result["result"]["fcp"] == 5
    _assert_finished_at(result, 10)


@pytest.mark.parametrize("loading", [False, True])
def test_zero_wait_skips_readiness_but_keeps_observer_windows(loading):
    result = _run(wait_ms=0, loading=loading, paint_at=276)
    assert result["result"]["fcp"] is None
    _assert_finished_at(result, 0)


@pytest.mark.parametrize("wait_ms,budget_ms", [(95, 95), (40000, 30000), (-10, 0)])
@pytest.mark.parametrize("loading", [False, True])
def test_missing_paint_stays_null_after_bounded_readiness(wait_ms, budget_ms, loading):
    result = _run(wait_ms=wait_ms, loading=loading)
    assert result["result"]["fcp"] is result["result"]["fp"] is None
    _assert_finished_at(result, budget_ms)
