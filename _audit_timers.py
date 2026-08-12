import json, time, sys
import browser_tools as bt

PAGE = """
<html><body><script>
window.log = [];
window.mark = (name) => window.log.push([name, Math.round(performance.now()), Math.round(Date.now())]);
window.setupTimers = () => {
  window.log = [];
  window.tid = setTimeout(() => mark('timeout100'), 100);
  window.iid = setInterval(() => mark('interval50'), 50);
};
window.rafRan = [];
window.reqRaf = (tag) => {
  const id = requestAnimationFrame(() => window.rafRan.push(tag));
  return id;
};
</script></body></html>
"""

driver = bt.create_driver(headless=True)
try:
    driver.get("data:text/html;charset=utf-8," + PAGE.replace("#", "%23"))
    boot = bt._RENDER_BOOTSTRAP_SCRIPT
    ctrl = bt._RENDER_CONTROL_SCRIPT
    def mode(m, fps=60.0, **opts):
        o = {"frame_delta_ms": 1000/60, "freeze_time": True, "gate_timers": True}
        o.update(opts)
        driver.execute_script(boot)
        return driver.execute_script(ctrl, m, fps, o)
    def step(n):
        return driver.execute_async_script(
            "const state=window.__webSearchNeoRenderControl;const done=arguments[arguments.length-1];"
            "if(!state) return done({error:'missing'});state.step(arguments[0], done);", n)
    def log():
        return driver.execute_script("return window.log;")
    def q(script, *a):
        return driver.execute_script(script, *a)

    print("=== T1 normal->step(freeze,gate): virtual clock drives timers ===")
    driver.execute_script(boot)
    q("window.setupTimers();")
    print("mode:", mode("step"))
    for i in range(1, 9):
        step(1)
    print("after 8 frames (~133ms virtual), log =", log())
    print("state.timers.size =", q("return window.__webSearchNeoRenderControl.timers.size;"))
    print("real sleep 1s, then log (should NOT grow):")
    time.sleep(1.0)
    print("  log =", log())

    print()
    print("=== T2 step(gate=T,freeze=T) -> step(gate=T,freeze=F): no detonation ===")
    q("window.setupTimers();")
    mode("step")
    step(1)
    before = log()
    r = mode("step", freeze_time=False)
    print("ctrl:", r)
    print("immediately after switch log =", log(), " (was", before, ")")
    step(1)
    print("after 1 frame log =", log())
    print("timers.size:", q("return window.__webSearchNeoRenderControl.timers.size;"))
    print("due-now delta ms:", q(
        "const s=window.__webSearchNeoRenderControl;"
        "return Array.from(s.timers.values()).map(e=>Math.round(e.due - s.now()));"))

    print()
    print("=== T3 step(gate=T) -> step(gate=F): timers handed back to real scheduler ===")
    q("window.setupTimers();")
    mode("step")
    step(1)
    mode("step", gate_timers=False)
    print("timers.size:", q("return window.__webSearchNeoRenderControl.timers.size;"),
          "liveTimers.size:", q("return window.__webSearchNeoRenderControl.liveTimers.size;"))
    time.sleep(0.5)
    print("after 0.5s real sleep log =", log())

    print()
    print("=== T4 step -> normal: queue resumes ===")
    q("window.setupTimers();")
    mode("step")
    step(1)
    mode("normal")
    time.sleep(0.5)
    print("after normal + 0.5s log =", log())
    print("clockInstalled:", q("return window.__webSearchNeoRenderControl.clockInstalled;"))

    print()
    print("=== T5 rAF id survives step->normal (adopt) and cancel works ===")
    q("window.rafRan = [];")
    mode("step")
    rid = q("return window.reqRaf('inStep');")
    print("id issued in step mode:", rid)
    mode("normal")
    print("native map keys:", q("return Array.from(window.__webSearchNeoRenderControl.native.keys());"))
    print("nativeIds map:", q("return Array.from(window.__webSearchNeoRenderControl.nativeIds.entries());"))
    q("cancelAnimationFrame(arguments[0]);", rid)
    time.sleep(0.3)
    print("rafRan after cancel (expect []):", q("return window.rafRan;"))

    print()
    print("=== T6 rAF id survives normal->step and cancel works ===")
    q("window.rafRan = [];")
    mode("normal")
    rid2 = q("return window.reqRaf('inNormal');")
    print("id issued in normal:", rid2)
    mode("step")
    print("pending keys:", q("return Array.from(window.__webSearchNeoRenderControl.pending.keys());"))
    q("cancelAnimationFrame(arguments[0]);", rid2)
    step(2)
    print("rafRan after cancel+2 frames (expect []):", q("return window.rafRan;"))

    print()
    print("=== T7 adopt then re-enter step then normal again (repeat toggling) ===")
    q("window.rafRan = [];")
    mode("step")
    rid3 = q("return window.reqRaf('A');")
    mode("normal")
    mode("step")
    mode("normal")
    print("after step->normal->step->normal, native keys:",
          q("return Array.from(window.__webSearchNeoRenderControl.native.keys());"),
          "nativeIds:", q("return Array.from(window.__webSearchNeoRenderControl.nativeIds.entries());"))
    time.sleep(0.3)
    print("rafRan (expect ['A'] once):", q("return window.rafRan;"))
    print("native size after fire:", q("return window.__webSearchNeoRenderControl.native.size;"),
          "nativeIds size:", q("return window.__webSearchNeoRenderControl.nativeIds.size;"))

    print()
    print("=== T8 throttled <-> step with gate, interval survival ===")
    q("window.setupTimers();")
    mode("throttled", 10.0)
    step_res = None
    time.sleep(0.3)
    print("throttled log after 0.3s:", log())
    mode("step")
    print("timers.size after throttled->step:", q("return window.__webSearchNeoRenderControl.timers.size;"))
    for i in range(6): step(1)
    print("log after 6 step frames:", log())

    print()
    print("=== T9 normal->normal repeated / gate flags ===")
    print(mode("normal"))
    print(mode("normal", gate_timers=False, freeze_time=False))
    print("timers/live:", q("return [window.__webSearchNeoRenderControl.timers.size, window.__webSearchNeoRenderControl.liveTimers.size];"))
finally:
    driver.quit()
