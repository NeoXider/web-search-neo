import json, time
import browser_tools as bt

PAGE = ("data:text/html;charset=utf-8,"
        "<html><body><script>"
        "window.log=[];"
        "window.setup=()=>{window.log=[];"
        " setTimeout(()=>log.push(['t100',Math.round(performance.now())]),100);"
        " setInterval(()=>log.push(['i50',Math.round(performance.now())]),50);};"
        "window.rafRan=[];"
        "</script></body></html>")

driver = bt.create_driver(headless=True)
boot = bt._RENDER_BOOTSTRAP_SCRIPT
ctrl = bt._RENDER_CONTROL_SCRIPT

def fresh():
    driver.get(PAGE)
    driver.execute_script(boot)

def mode(m, fps=60.0, **opts):
    o = {"frame_delta_ms": 1000/60, "freeze_time": True, "gate_timers": True}; o.update(opts)
    return driver.execute_script(ctrl, m, fps, o)

def step(n=1):
    return driver.execute_async_script(
        "const s=window.__webSearchNeoRenderControl;const d=arguments[arguments.length-1];s.step(arguments[0],d);", n)

def q(s, *a): return driver.execute_script(s, *a)

try:
    print("### A. cancelAnimationFrame after step->normal (adopt), all in ONE script ###")
    fresh()
    res = driver.execute_script("""
      const s = window.__webSearchNeoRenderControl;
      window.rafRan = [];
      s.setMode('step', 60, {frame_delta_ms:16.667, freeze_time:true, gate_timers:true});
      const id = requestAnimationFrame(() => window.rafRan.push('X'));
      const out = {id_in_step: id};
      s.setMode('normal', 60, {frame_delta_ms:16.667, freeze_time:true, gate_timers:true});
      out.native_keys = Array.from(s.native.keys());
      out.nativeIds = Array.from(s.nativeIds.entries());
      cancelAnimationFrame(id);
      out.after_cancel_native = Array.from(s.native.keys());
      out.after_cancel_nativeIds = Array.from(s.nativeIds.entries());
      return out;
    """)
    print(json.dumps(res))
    time.sleep(0.4)
    print("  rafRan after 0.4s (MUST be [] -> cancel honoured):", q("return window.rafRan;"))

    print()
    print("### B. cancelAnimationFrame after normal->step, ONE script ###")
    fresh()
    res = driver.execute_script("""
      const s = window.__webSearchNeoRenderControl;
      window.rafRan = [];
      s.setMode('normal', 60, {frame_delta_ms:16.667, freeze_time:true, gate_timers:true});
      const id = requestAnimationFrame(() => window.rafRan.push('Y'));
      const out = {id_in_normal: id, native_before: Array.from(s.native.keys())};
      s.setMode('step', 60, {frame_delta_ms:16.667, freeze_time:true, gate_timers:true});
      out.pending_keys = Array.from(s.pending.keys());
      cancelAnimationFrame(id);
      out.pending_after_cancel = Array.from(s.pending.keys());
      return out;
    """)
    print(json.dumps(res))
    step(3)
    print("  rafRan after 3 frames (MUST be []):", q("return window.rafRan;"))

    print()
    print("### C. step->normal->step->normal toggling with an outstanding rAF ###")
    fresh()
    res = driver.execute_script("""
      const s = window.__webSearchNeoRenderControl;
      window.rafRan = [];
      const o = {frame_delta_ms:16.667, freeze_time:true, gate_timers:true};
      s.setMode('step',60,o);
      const id = requestAnimationFrame(() => window.rafRan.push('Z'));
      s.setMode('normal',60,o);
      const afterFirstAdopt = {native: Array.from(s.native.keys()), nativeIds: Array.from(s.nativeIds.entries())};
      s.setMode('step',60,o);
      const afterBack = {pending: Array.from(s.pending.keys()), native: Array.from(s.native.keys()), nativeIds: Array.from(s.nativeIds.entries())};
      s.setMode('normal',60,o);
      const afterSecondAdopt = {native: Array.from(s.native.keys()), nativeIds: Array.from(s.nativeIds.entries())};
      cancelAnimationFrame(id);
      return {id, afterFirstAdopt, afterBack, afterSecondAdopt,
              after_cancel: {native: Array.from(s.native.keys()), nativeIds: Array.from(s.nativeIds.entries())}};
    """)
    print(json.dumps(res))
    time.sleep(0.4)
    print("  rafRan (MUST be []):", q("return window.rafRan;"))

    print()
    print("### D. gated step: freeze_time TRUE -> FALSE, do timers detonate? ###")
    fresh(); q("window.setup();"); mode("step"); step(1)
    before = q("const s=window.__webSearchNeoRenderControl;return {n:s.timers.size, due: Array.from(s.timers.values()).map(e=>Math.round(e.due-s.now()))};")
    print("  before switch, remaining ms:", before)
    mode("step", freeze_time=False)
    after = q("const s=window.__webSearchNeoRenderControl;return {n:s.timers.size, due: Array.from(s.timers.values()).map(e=>Math.round(e.due-s.now()))};")
    print("  after freeze->off, remaining ms:", after)
    print("  log:", q("return window.log;"))

    print()
    print("### E. gated step: freeze_time FALSE -> TRUE ###")
    fresh(); q("window.setup();"); mode("step", freeze_time=False); step(1)
    print("  before:", q("const s=window.__webSearchNeoRenderControl;return Array.from(s.timers.values()).map(e=>Math.round(e.due-s.now()));"))
    mode("step", freeze_time=True)
    print("  after :", q("const s=window.__webSearchNeoRenderControl;return Array.from(s.timers.values()).map(e=>Math.round(e.due-s.now()));"))

    print()
    print("### F. gated step -> gate_timers OFF: are deadlines preserved? ###")
    fresh(); q("window.setup();"); mode("step"); step(1)
    print("  before, remaining ms:", q("const s=window.__webSearchNeoRenderControl;return Array.from(s.timers.values()).map(e=>Math.round(e.due-s.now()));"))
    mode("step", gate_timers=False)
    print("  liveTimers realDue-now:", q("const s=window.__webSearchNeoRenderControl;const r=performance.now();return {n:s.liveTimers.size, d:Array.from(s.liveTimers.values()).map(e=>Math.round(e.realDue-r))};"))
    t0=time.time(); time.sleep(0.30)
    print("  log after 0.3s real:", q("return window.log;"))

    print()
    print("### G. step -> normal: deadlines preserved? ###")
    fresh(); q("window.setup();"); mode("step"); step(1)
    print("  before, remaining ms:", q("const s=window.__webSearchNeoRenderControl;return Array.from(s.timers.values()).map(e=>Math.round(e.due-s.now()));"))
    mode("normal")
    time.sleep(0.30)
    print("  log after 0.3s real:", q("return window.log;"))

    print()
    print("### H. throttled mode with NO rAF loop: do gated timers ever run? ###")
    fresh(); q("window.setup();")
    print("  ctrl:", mode("throttled", 10.0))
    time.sleep(1.0)
    print("  log after 1s wall clock:", q("return window.log;"))
    print("  timers.size:", q("return window.__webSearchNeoRenderControl.timers.size;"))
    print("  pending rAF:", q("return window.__webSearchNeoRenderControl.pending.size;"))

    print()
    print("### I. throttled mode WITH a rAF loop ###")
    fresh(); q("window.setup(); window.frames=0; (function loop(){window.frames++;requestAnimationFrame(loop);})();")
    mode("throttled", 10.0)
    time.sleep(1.0)
    print("  frames in 1s at target 10fps:", q("return window.frames;"))
    print("  log:", q("return window.log;"))
finally:
    driver.quit()
