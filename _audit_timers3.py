import json, time
import browser_tools as bt

driver = bt.create_driver(headless=True)
boot = bt._RENDER_BOOTSTRAP_SCRIPT
ctrl = bt._RENDER_CONTROL_SCRIPT
def mode(m, fps=60.0, **opts):
    o = {"frame_delta_ms": 1000/60, "freeze_time": True, "gate_timers": True}; o.update(opts)
    return driver.execute_script(ctrl, m, fps, o)
def q(s, *a): return driver.execute_script(s, *a)
try:
    # what is the mystery 15,000,000 ms timer?
    driver.get("data:text/html;charset=utf-8,<html><body>hi</body></html>")
    driver.execute_script(boot)
    mode("step")
    print("mystery timers:", q("""
      const s=window.__webSearchNeoRenderControl;
      return Array.from(s.timers.entries()).map(([id,e])=>({id, interval:e.interval,
        remaining: Math.round(e.due-s.now()), src: String(e.callback).slice(0,120)}));"""))
    mode("normal")

    print()
    print("### J. throttled: rAF loop rescheduled through setTimeout (very common) ###")
    driver.get("data:text/html;charset=utf-8,"
        "<html><body><script>window.frames=0;window.ticks=0;"
        "function loop(){window.frames++;setTimeout(()=>requestAnimationFrame(loop),0);}"
        "requestAnimationFrame(loop);"
        "setInterval(()=>window.ticks++,50);</script></body></html>")
    driver.execute_script(boot)
    print("  ctrl:", mode("throttled", 10.0))
    time.sleep(1.5)
    print("  frames after 1.5s (target 10fps -> expect ~15):", q("return window.frames;"))
    print("  setInterval ticks (expect ~15 gated):", q("return window.ticks;"))
    print("  pending rAF:", q("return window.__webSearchNeoRenderControl.pending.size;"),
          "timers:", q("return window.__webSearchNeoRenderControl.timers.size;"),
          "schedule timer:", q("return window.__webSearchNeoRenderControl.timer;"))
    print("  -> recover with normal:")
    mode("normal"); time.sleep(0.5)
    print("  frames after normal+0.5s:", q("return window.frames;"), "ticks:", q("return window.ticks;"))

    print()
    print("### K. STEP mode, same loop: does step() keep working? ###")
    driver.get("data:text/html;charset=utf-8,"
        "<html><body><script>window.frames=0;"
        "function loop(){window.frames++;setTimeout(()=>requestAnimationFrame(loop),0);}"
        "requestAnimationFrame(loop);</script></body></html>")
    driver.execute_script(boot)
    mode("step")
    outs=[]
    for i in range(6):
        outs.append(driver.execute_async_script(
            "const s=window.__webSearchNeoRenderControl;const d=arguments[arguments.length-1];s.step(1,d);"))
    print("  frames after 6 steps:", q("return window.frames;"))
    print("  last step result:", json.dumps(outs[-1]))

    print()
    print("### L. throttled with plain rAF loop but game pauses on a setTimeout ###")
    driver.get("data:text/html;charset=utf-8,"
        "<html><body><script>window.frames=0;window.paused=false;"
        "function loop(){window.frames++;if(!window.paused)requestAnimationFrame(loop);}"
        "requestAnimationFrame(loop);"
        "window.pause=()=>{window.paused=true;setTimeout(()=>{window.paused=false;requestAnimationFrame(loop);},100);};"
        "</script></body></html>")
    driver.execute_script(boot)
    mode("throttled", 10.0)
    time.sleep(0.4)
    f1 = q("return window.frames;")
    q("window.pause();")
    time.sleep(1.2)
    f2 = q("return window.frames;")
    print(f"  frames before pause={f1}, after pause+1.2s={f2}  (resume timer gated => stuck)")
    print("  paused flag:", q("return window.paused;"), "timers:", q("return window.__webSearchNeoRenderControl.timers.size;"))
finally:
    driver.quit()
