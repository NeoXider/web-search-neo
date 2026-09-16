"""Response stubs, independent of session ownership and page summaries.

Callers serialize access to each driver. Companion rules use CDP Fetch; the
Selenium fallback covers page fetch/XHR, not workers or subresource loading.
"""
from __future__ import annotations

import json
from typing import Any

_REQUEST_MOCKS: dict[str, list[dict[str, Any]]] = {}
_MOCK_STUB_SCRIPT_IDS: dict[str, str] = {}
_MOCK_BODY_LIMIT = 1_000_000

_MOCK_STUB_SOURCE = r"""
(() => {
  if (window.__wsnMockState) return;
  const originalFetch = window.fetch;
  const OrigXHR = window.XMLHttpRequest;
  const state = window.__wsnMockState = {mocks: []};
  const absolute = url => { try { return new URL(String(url), location.href).href; }
    catch (_) { return String(url); } };
  const matches = (url, pattern) => {
    let regex = '';
    const p = String(pattern);
    for (let i = 0; i < p.length; i++) {
      const c = p[i];
      if (c === '\\' && i + 1 < p.length) {
        regex += p[++i].replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      } else if (c === '*') regex += '.*';
      else if (c === '?') regex += '.';
      else regex += c.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }
    return new RegExp('^' + regex + '$').test(absolute(url));
  };
  const find = url => state.mocks.find(m => matches(url, m.pattern));
  const noBody = (code, method) => [204, 205, 304].includes(code) || method === 'HEAD';
  const headersFor = entry => {
    const headers = new Headers(entry.headers || {});
    if (!headers.has('content-type')) headers.set('content-type', 'application/json');
    return headers;
  };
  window.__wsnMockSet = mocks => { state.mocks = Array.isArray(mocks) ? mocks : []; };
  if (originalFetch) window.fetch = function(input, init) {
    const entry = find(input && input.url !== undefined ? input.url : input);
    if (!entry) return originalFetch.apply(this, arguments);
    const method = String(init?.method || input?.method || 'GET').toUpperCase();
    const signal = init?.signal || input?.signal;
    if (signal?.aborted) return Promise.reject(signal.reason || new DOMException('Aborted', 'AbortError'));
    try {
      const response = new Response(noBody(entry.status, method) ? null : entry.body, {
        status: entry.status, headers: headersFor(entry),
      });
      Object.defineProperty(response, 'url', {value: absolute(input?.url ?? input)});
      return Promise.resolve(response);
    } catch (error) { return Promise.reject(error); }
  };
  function MockXHR() {
    const xhr = new OrigXHR();
    const orig = {open: xhr.open, send: xhr.send, abort: xhr.abort,
      getResponseHeader: xhr.getResponseHeader, getAllResponseHeaders: xhr.getAllResponseHeaders};
    let url = '', method = 'GET', async = true, active = false, timer = 0, generation = 0;
    let responseHeaders = null;
    const properties = ['status', 'statusText', 'responseText', 'response', 'responseURL', 'readyState'];
    const set = (name, value) => Object.defineProperty(xhr, name, {value, configurable: true});
    const fire = name => xhr.dispatchEvent(new ProgressEvent(name));
    xhr.open = function(m, u, a = true) {
      generation++; clearTimeout(timer); active = false; responseHeaders = null;
      for (const name of properties) delete xhr[name];
      method = String(m).toUpperCase(); url = absolute(u); async = a !== false;
      return orig.open.apply(xhr, arguments);
    };
    xhr.getResponseHeader = function(name) {
      return responseHeaders ? responseHeaders.get(name) : orig.getResponseHeader.apply(xhr, arguments);
    };
    xhr.getAllResponseHeaders = function() {
      return responseHeaders ? [...responseHeaders].map(([k,v]) => k + ': ' + v + '\r\n').join('')
        : orig.getAllResponseHeaders.apply(xhr, arguments);
    };
    xhr.abort = function() {
      if (!active) return orig.abort.apply(xhr, arguments);
      generation++; clearTimeout(timer); active = false;
      set('status', 0); set('response', null); set('responseText', ''); set('readyState', 4);
      fire('readystatechange'); fire('abort'); fire('loadend'); set('readyState', 0);
    };
    xhr.send = function() {
      const entry = find(url);
      if (!entry) return orig.send.apply(xhr, arguments);
      if (xhr.readyState !== 1 || active) throw new DOMException('Invalid state', 'InvalidStateError');
      active = true;
      const current = ++generation;
      const finish = () => {
        if (current !== generation || !active) return;
        const text = noBody(entry.status, method) ? '' : String(entry.body || '');
        responseHeaders = headersFor(entry);
        set('status', entry.status); set('statusText', ''); set('responseURL', url);
        set('readyState', 2); fire('readystatechange');
        if (current !== generation || !active) return;
        set('readyState', 3); fire('readystatechange');
        if (current !== generation || !active) return;
        let response = text;
        if (xhr.responseType === 'json') { try { response = JSON.parse(text); } catch (_) { response = null; } }
        else if (xhr.responseType === 'arraybuffer') response = new TextEncoder().encode(text).buffer;
        else if (xhr.responseType === 'blob') response = new Blob([text], {type: responseHeaders.get('content-type') || ''});
        else if (xhr.responseType === 'document') response = new DOMParser().parseFromString(text, 'text/html');
        set('response', response);
        if (!xhr.responseType || xhr.responseType === 'text') set('responseText', text);
        set('readyState', 4); active = false; fire('readystatechange');
        if (current !== generation) return;
        fire('load');
        if (current === generation) fire('loadend');
      };
      fire('loadstart');
      if (async) timer = setTimeout(finish, 0); else finish();
    };
    return xhr;
  }
  MockXHR.prototype = OrigXHR.prototype;
  for (const key of ['UNSENT', 'OPENED', 'HEADERS_RECEIVED', 'LOADING', 'DONE']) {
    Object.defineProperty(MockXHR, key, {value: OrigXHR[key]});
  }
  window.XMLHttpRequest = MockXHR;
  const wrappedFetch = window.fetch;
  state.stop = () => {
    state.mocks = [];
    if (window.fetch === wrappedFetch) window.fetch = originalFetch;
    if (window.XMLHttpRequest === MockXHR) window.XMLHttpRequest = OrigXHR;
    delete window.__wsnMockSet; delete window.__wsnMockState;
  };
})();
"""


def _validate_mock(url_pattern: str, status: int, headers: dict[str, str] | None,
                   body: str) -> dict[str, Any]:
    pattern = str(url_pattern or '').strip()
    if not pattern:
        raise ValueError('mock needs a url_pattern (CDP wildcard)')
    try:
        code = int(status)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('mock status must be an integer 200-599') from exc
    if isinstance(status, bool) or str(status) != str(code) or not 200 <= code <= 599:
        raise ValueError('mock status must be an integer 200-599')
    clean_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    if any('\r' in k + v or '\n' in k + v for k, v in clean_headers.items()):
        raise ValueError('mock headers must not contain newlines')
    text = '' if body is None else str(body)
    if len(text.encode('utf-8')) > _MOCK_BODY_LIMIT:
        raise ValueError(f'mock body exceeds the {_MOCK_BODY_LIMIT}-byte limit')
    return {'pattern': pattern, 'status': code, 'headers': clean_headers, 'body': text}


def _source(mocks: list[dict[str, Any]]) -> str:
    return _MOCK_STUB_SOURCE + '\nwindow.__wsnMockSet(' + json.dumps(mocks, ensure_ascii=True) + ');'


def _push(driver: Any, session_id: str, mocks: list[dict[str, Any]]) -> None:
    """Replace navigation registration only after new registration succeeds."""
    previous = _MOCK_STUB_SCRIPT_IDS.get(session_id)
    result = driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': _source(mocks)})
    identifier = str((result or {}).get('identifier') or '')
    if not identifier:
        raise RuntimeError('The browser did not register the mock navigation script')
    try:
        driver.execute_script(_source(mocks))
        if previous:
            driver.execute_cdp_cmd('Page.removeScriptToEvaluateOnNewDocument', {'identifier': previous})
    except Exception:
        driver.execute_cdp_cmd('Page.removeScriptToEvaluateOnNewDocument', {'identifier': identifier})
        raise
    _MOCK_STUB_SCRIPT_IDS[session_id] = identifier


def add(driver: Any, session_id: str, url_pattern: str, status: int = 200,
        headers: dict[str, str] | None = None, body: str = '') -> dict[str, Any]:
    entry = _validate_mock(url_pattern, status, headers, body)
    registry = [m for m in _REQUEST_MOCKS.get(session_id, []) if m['pattern'] != entry['pattern']] + [entry]
    if getattr(driver, 'is_extension_bridge', False):
        answer = driver.bridge.request('mock.add', {'tabId': driver.tab_id, **entry}, timeout=15.0)
        count = int(answer['mocks'])
    else:
        _push(driver, session_id, registry)
        count = len(registry)
    _REQUEST_MOCKS[session_id] = registry
    return {'mocked': True, 'pattern': entry['pattern'], 'status': entry['status'], 'mocks': count}


def list_requests(driver: Any, session_id: str) -> dict[str, Any]:
    if getattr(driver, 'is_extension_bridge', False):
        answer = driver.bridge.request('mock.list', {'tabId': driver.tab_id}, timeout=15.0)
        rows = answer.get('mocks') or []
    else:
        rows = [{k: m[k] for k in ('pattern', 'status', 'headers')} | {'body_chars': len(m['body'])}
                for m in _REQUEST_MOCKS.get(session_id, [])]
    return {'mocks': rows, 'count': len(rows)}


def clear(driver: Any, session_id: str, url_pattern: str | None = None) -> dict[str, Any]:
    previous = _REQUEST_MOCKS.get(session_id, [])
    kept = [m for m in previous if m['pattern'] != url_pattern] if url_pattern else []
    if getattr(driver, 'is_extension_bridge', False):
        answer = driver.bridge.request('mock.clear', {'tabId': driver.tab_id,
                                      **({'pattern': url_pattern} if url_pattern else {})}, timeout=15.0)
        details = {'cleared': int(answer['cleared']), 'mocks': int(answer['mocks'])}
    else:
        if kept:
            _push(driver, session_id, kept)
        else:
            script_id = _MOCK_STUB_SCRIPT_IDS.get(session_id)
            if script_id:
                driver.execute_cdp_cmd('Page.removeScriptToEvaluateOnNewDocument', {'identifier': script_id})
                _MOCK_STUB_SCRIPT_IDS.pop(session_id, None)
            driver.execute_script('if (window.__wsnMockState) window.__wsnMockState.stop();')
        details = {'cleared': len(previous) - len(kept), 'mocks': len(kept)}
    if kept:
        _REQUEST_MOCKS[session_id] = kept
    else:
        _REQUEST_MOCKS.pop(session_id, None)
    return details


def teardown(driver: Any, session_id: str) -> None:
    """Restore a surviving claimed tab, then discard this session's registry."""
    try:
        if session_id in _REQUEST_MOCKS or session_id in _MOCK_STUB_SCRIPT_IDS:
            clear(driver, session_id)
    finally:
        _REQUEST_MOCKS.pop(session_id, None)
        _MOCK_STUB_SCRIPT_IDS.pop(session_id, None)


def forget(session_id: str) -> None:
    """Drop the registry for a session whose tab or browser is already gone."""
    _REQUEST_MOCKS.pop(session_id, None)
    _MOCK_STUB_SCRIPT_IDS.pop(session_id, None)
