import subprocess
import json
import urllib.request
from pathlib import Path


def test_background_frame_lifecycle_and_isolation():
    module = (Path(__file__).resolve().parents[1] / 'chrome-extension/background-capture.js').as_uri()
    script = r'''
import assert from 'node:assert/strict';
import {createBackgroundCapture} from '__MODULE__';
function event() { const callbacks = new Set(); return {addListener: fn => callbacks.add(fn), removeListener: fn => callbacks.delete(fn), emit: (...args) => [...callbacks].forEach(fn=>fn(...args)), get count(){return callbacks.size}}; }
function fixture(mode='success') {
 const calls=[], api={onEvent:event(),onDetach:event(),sendCommand:async(target,method,params)=>{
  calls.push(method);
  if(method==='Page.startScreencast') {
   if(mode==='busy') { api.onEvent.emit(target,'Page.screencastFrame',{sessionId:7,data:'other-recording'}); throw Error('Screencast is already active'); }
   if(mode==='success') {api.onEvent.emit({tabId:99},'Page.screencastFrame',{sessionId:2,data:'wrong-tab'});api.onEvent.emit(target,'Page.screencastFrame',{sessionId:1,data:'fresh-png'});}
  }
 }};
 return {api,calls,capture:createBackgroundCapture(api,20)};
}
const ok=fixture();
assert.deepEqual(await ok.capture(1),{data:'fresh-png'});
assert.deepEqual(ok.calls,['Page.startScreencast','Page.screencastFrameAck','Page.stopScreencast']);
assert.equal(ok.api.onEvent.count,0);assert.equal(ok.api.onDetach.count,0);
assert.deepEqual(await ok.capture(1),{data:'fresh-png'});
const busy=fixture('busy');await assert.rejects(busy.capture(1),/already active/);
assert.deepEqual(busy.calls,['Page.startScreencast']);assert.equal(busy.api.onEvent.count,0);
const slow=fixture('slow'), first=slow.capture(1);
await assert.rejects(slow.capture(1),/pending/);
await assert.rejects(first,/do not call show/);
assert.deepEqual(slow.calls,['Page.startScreencast','Page.stopScreencast']);
assert.equal(slow.api.onEvent.count,0);
const detached=fixture('slow'), p=detached.capture(1);await new Promise(r=>setImmediate(r));
detached.api.onDetach.emit({tabId:1});await assert.rejects(p,/detached/);
assert.deepEqual(detached.calls,['Page.startScreencast','Page.stopScreencast']);
const late=fixture('slow');let finishStart,finishStop;
late.api.sendCommand=async(target,method)=>{late.calls.push(method);if(method==='Page.startScreencast')await new Promise(r=>finishStart=r);if(method==='Page.stopScreencast')await new Promise(r=>finishStop=r);};
const lateCapture=createBackgroundCapture(late.api,10,10);
await assert.rejects(lateCapture(1),/no frame/);
await assert.rejects(lateCapture(1),/pending/);
finishStart();await new Promise(r=>setImmediate(r));
assert.equal(late.calls.at(-1),'Page.stopScreencast');
await assert.rejects(lateCapture(1),/pending/);
finishStop();await new Promise(r=>setImmediate(r));
const stuck=fixture();const normalSend=stuck.api.sendCommand;
stuck.api.sendCommand=(target,method,params)=>method==='Page.stopScreencast'?new Promise(()=>{}):normalSend(target,method,params);
const bounded=createBackgroundCapture(stuck.api,20,10);
assert.deepEqual(await bounded(1),{data:'fresh-png'});
await assert.rejects(bounded(1),/pending/);
const ack=fixture();const ackSend=ack.api.sendCommand;
ack.api.sendCommand=(target,method,params)=>method==='Page.screencastFrameAck'?Promise.reject(Error('ack failed')):ackSend(target,method,params);
await assert.rejects(ack.capture(1),/ack failed/);
assert.equal(ack.calls.at(-1),'Page.stopScreencast');
'''.replace('__MODULE__', module)
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_real_chrome_returns_two_fresh_background_frames(local_site):
    from web_search_neo import browser_tools
    sid = 'background-frame-test'
    browser_tools.open_page(local_site.base_url, session_id=sid, headless=True, profile_mode='temporary')
    try:
        driver = browser_tools._get_session(sid).driver
        address = driver.capabilities['goog:chromeOptions']['debuggerAddress']
        with urllib.request.urlopen(f'http://{address}/json/list', timeout=5) as response:
            targets = json.load(response)
        target = next(row for row in targets if row.get('type') == 'page' and row.get('url', '').startswith(local_site.base_url))
        module = (Path(__file__).resolve().parents[1] / 'chrome-extension/background-capture.js').as_uri()
        script = r'''
import assert from 'node:assert/strict';
import {createBackgroundCapture} from '__MODULE__';
const ws = new WebSocket(process.argv[1]), requests = new Map(); let seq=0;
const listeners=new Set();
ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=requests.get(m.id);requests.delete(m.id);if(m.error)p.reject(Error(m.error.message));else p.resolve(m.result);}else for(const fn of listeners)fn({tabId:1},m.method,m.params)});
await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true})});
const send=(target,method,params)=>new Promise((resolve,reject)=>{const id=++seq;requests.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
const api={sendCommand:send,onEvent:{addListener:fn=>listeners.add(fn),removeListener:fn=>listeners.delete(fn)},onDetach:{addListener(){},removeListener(){}}};
try {
 await send({},'Page.enable',{});
 const capture=createBackgroundCapture(api);
 let previous='';
 for(const color of ['red','green']) {
  await send({},'Runtime.evaluate',{expression:`document.body.innerHTML='<h1>FRESH ${color}</h1>';document.body.style.background='${color}'`});
  const frame=await capture(1), bytes=Buffer.from(frame.data,'base64');
  assert.equal(bytes.subarray(0,8).toString('hex'),'89504e470d0a1a0a');
  assert.ok(bytes.length>1000);assert.notEqual(frame.data,previous);previous=frame.data;
 }
 assert.equal(listeners.size,0);
} finally { ws.close(); }
'''.replace('__MODULE__', module)
        result = subprocess.run(['node', '--input-type=module', '-e', script, target['webSocketDebuggerUrl']], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    finally:
        browser_tools.close_session(sid)
