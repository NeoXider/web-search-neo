"""Behavioral regressions for response stubs; all traffic stays on local fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from web_search_neo.cdp import request_mocks as mocks


def test_mock_validation_rejects_fractional_and_header_injection():
    for code in (200.5, True, 0, float('inf')):
        with pytest.raises(ValueError, match='integer'):
            mocks._validate_mock('*', code, None, '')
    with pytest.raises(ValueError, match='newlines'):
        mocks._validate_mock('*', 200, {'X-Test': 'ok\r\ninjected'}, '')


def test_mock_failure_does_not_publish_registry():
    class Driver:
        def execute_cdp_cmd(self, method, params, timeout=None):
            raise RuntimeError('installation refused')

    with pytest.raises(RuntimeError, match='refused'):
        mocks.add(Driver(), 'failed-install', '*')
    assert 'failed-install' not in mocks._REQUEST_MOCKS


def test_companion_mock_lifecycle_and_child_request_routing():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for companion JavaScript test')
    source = (Path(__file__).parents[1] / 'chrome-extension/service-worker.js').read_text(encoding='utf-8')
    block = source[source.index('const requestMocks = new Map();'):source.index('async function cdpSend(')]
    script = r"""
const assert = require('node:assert/strict');
let calls = [], saved = null, fail = false;
const chrome = {debugger: {onEvent: {addListener() {}}}};
async function restoreState() {}
async function persistState() { saved = JSON.stringify([...requestMocks]); }
async function ensureDebugger() {}
async function cdpSend(arg) { calls.push(arg); if (fail) throw Error('refused'); return {}; }
""" + block + r"""
(async () => {
  assert(mockWildcardMatch('https://a/x1', 'https://a/x?'));
  assert(mockWildcardMatch('https://a/x?', 'https://a/x\\?'));
  const entry = {pattern:'https://a/*', status:200, headers:{}, body:'ok'};
  await commitMocks(7, [entry]);
  assert.equal(JSON.parse(saved)[0][1][0].body, 'ok');
  fail = true;
  await assert.rejects(commitMocks(7, []), /refused/);
  assert.equal(mocksForTab(7).length, 1);
  fail = false; calls = [];
  await handleFetchPaused({tabId:7, sessionId:'child'}, {requestId:'r', request:{url:'https://a/x'}});
  assert.equal(calls.at(-1).sessionId, 'child');
  assert.equal(calls.at(-1).method, 'Fetch.fulfillRequest');
  requestMocks.set(7, [{...entry,status:204}]); calls = [];
  await handleFetchPaused({tabId:7}, {requestId:'r', request:{url:'https://a/x'}});
  assert(!('body' in calls.at(-1).params));
  await commitMocks(7, []);
  assert.equal(requestMocks.has(7), false);
  const order = [];
  await Promise.all([mutateMocks(7, async()=>{await new Promise(r=>setTimeout(r,10));order.push(1)}),
    mutateMocks(7, async()=>order.push(2))]);
  assert.deepEqual(order, [1,2]);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    answer = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=30)
    assert answer.returncode == 0, answer.stderr


def test_live_mock_navigation_fetch_xhr_and_restore(local_site):
    from web_search_neo import browser_tools

    session_id = 'mock-audit-live'
    browser_tools.open_page(local_site.base_url, session_id=session_id,
                            headless=True, profile_mode='temporary')
    driver = browser_tools._get_session(session_id).driver
    original = 'window.__originalMockFetch=window.fetch;window.__originalMockXHR=window.XMLHttpRequest;'
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': original})
    driver.execute_script(original)
    try:
        mocks.add(driver, session_id, local_site.base_url + '/api/mock*', body='{"value":42}',
                  headers={'Content-Type': 'application/json', 'X-Mock': 'yes'})
        for navigate in (False, True):
            if navigate:
                driver.get(local_site.base_url)
            result = driver.execute_async_script("""
const done = arguments[arguments.length-1];
fetch('/api/mock').then(r=>r.json()).then(done,e=>done({error:String(e)}));
""")
            assert result == {'value': 42}
            xhr = driver.execute_async_script("""
const done = arguments[arguments.length-1], xhr = new XMLHttpRequest(), events=[];
xhr.open('GET','/api/mock'); xhr.responseType='json';
xhr.addEventListener('load',()=>events.push('load'));
xhr.onloadend=()=>done({events,status:xhr.status,response:xhr.response,header:xhr.getResponseHeader('X-Mock')});
xhr.send();
""")
            assert xhr == {'events': ['load'], 'status': 200, 'response': {'value': 42}, 'header': 'yes'}
        mocks.add(driver, session_id, local_site.base_url + '/empty', status=204)
        assert driver.execute_async_script("""
const done=arguments[arguments.length-1];
fetch('/empty').then(async r=>done([r.status,await r.text()]),e=>done(String(e)));
""") == [204, '']
        mocks.clear(driver, session_id, local_site.base_url + '/empty')
        driver.get(local_site.base_url)
        assert mocks.list_requests(driver, session_id)['count'] == 1
        assert driver.execute_async_script("""
const done=arguments[arguments.length-1];fetch('/api/mock').then(r=>r.json()).then(done);
""") == {'value': 42}
        mocks.clear(driver, session_id)
        assert driver.execute_script('return window.fetch === window.__originalMockFetch && window.XMLHttpRequest === window.__originalMockXHR;')
        driver.get(local_site.base_url)
        assert driver.execute_script('return !window.__wsnMockState;')
    finally:
        mocks.teardown(driver, session_id)
        browser_tools.close_session(session_id=session_id)
