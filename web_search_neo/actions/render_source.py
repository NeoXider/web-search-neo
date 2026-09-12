"""Browser-side implementation shared by the legacy facade."""


_RENDER_BOOTSTRAP_SCRIPT = r"""
(() => {
const stateKey = '__webSearchNeoRenderControl';
if (window[stateKey]) return;

// Capture every timing primitive before anything is replaced, so the gate can
// restore real timing and can schedule its own work without gating itself.
const nativeRequest = window.requestAnimationFrame.bind(window);
const nativeCancel = window.cancelAnimationFrame.bind(window);
const nativeSetTimeout = window.setTimeout.bind(window);
const nativeClearTimeout = window.clearTimeout.bind(window);
const nativeSetInterval = window.setInterval.bind(window);
const nativeClearInterval = window.clearInterval.bind(window);
const nativeIdle = window.requestIdleCallback ? window.requestIdleCallback.bind(window) : null;
const nativeCancelIdle = window.cancelIdleCallback ? window.cancelIdleCallback.bind(window) : null;
const nativePerformanceNow = performance.now.bind(performance);
const nativeDateNow = Date.now.bind(Date);
const epochOffset = nativeDateNow() - nativePerformanceNow();

// Yielding between frames must not be a timer. A tab nobody is looking at - and
// agent tabs open in the background so the user can keep working - clamps
// setTimeout to about a second, and to a minute once intensive throttling kicks
// in, so stepping sixty frames through nativeSetTimeout took 53 seconds where a
// visible tab took 241 ms. A MessageChannel message is still a macrotask, so page
// work, network callbacks and microtasks run between frames exactly as before,
// but nothing throttles it.
const yieldChannel = typeof MessageChannel === 'function' ? new MessageChannel() : null;
const yieldQueue = [];
if (yieldChannel) {
  yieldChannel.port1.onmessage = () => {
    const task = yieldQueue.shift();
    if (task) task();
  };
}
const yieldTask = task => {
  if (!yieldChannel) { nativeSetTimeout(task, 0); return; }
  yieldQueue.push(task);
  yieldChannel.port2.postMessage(0);
};

const state = {
    mode: 'normal',
    targetFps: null,
    interval: 1000 / 60,
    frameDelta: 1000 / 60,
    freezeTime: true,
    gateTimers: true,
    clockInstalled: false,
    clockPatched: false,
    timersInstalled: false,
    // How far ahead of the native clock the page-visible one has been carried by
    // stepping. It never shrinks, because a clock that goes backwards is worse
    // than one that is wrong: see restoreClock.
    skew: 0,
    // The last frame timestamp the page was given, which no later one may
    // undercut: see state.stamp.
    lastStamp: 0,
    virtualNow: nativePerformanceNow(),
    lastFrame: nativePerformanceNow(),
    lastRealFlush: nativePerformanceNow(),
    frameCount: 0,
    nextId: -1,
    nextTimerId: -1,
    pending: new Map(),
    native: new Map(),
    nativeIds: new Map(),
    timers: new Map(),
    liveTimers: new Map(),
    timer: null,
    // How deep inside a chain of timer callbacks the gate currently is, which is
    // what decides whether the next setTimeout gets the spec's nesting clamp.
    timerDepth: 0,
    nativeRequest: nativeRequest,
    nativeCancel: nativeCancel
};

// The page-visible clock. While the gate is engaged it only moves when a frame
// is released, so a game sees a constant delta no matter how long the agent
// spent thinking between calls. Off the gate it is the native clock plus the
// skew stepping has earned, so the two never disagree about which way time runs.
state.now = () => (
    state.gated() && state.freezeTime
      ? state.virtualNow
      : nativePerformanceNow() + state.skew
);
state.gated = () => state.mode !== 'normal';

// Sixty frames of 100ms cost the wall clock a fraction of that, so the frozen
// clock ends up seconds ahead of the native one. Handing the raw native clock
// back would drop the page's time by exactly that much: performance.now() falls,
// the next frame's delta is negative, physics integrates backwards, tweens snap
// and any timestamp the page stored now sits in the future. Carrying the
// difference forward as a fixed offset keeps the page's own clock monotonic
// across every mode change, at the price of it running ahead of wall time -
// which is what the page was told all along.
state.carryClock = previousNow => {
    state.skew = Math.max(state.skew, previousNow - nativePerformanceNow());
};

// Every frame timestamp the page is given passes through here, and none of them
// may undercut the last one. Measuring the skew is not enough on its own: a
// browser dates a frame from when it *began*, so the first native frame after a
// mode change can carry a stamp from before the moment the skew was read - up to
// a frame period earlier - and hand the page a small negative delta, which a
// game reads no differently from a large one. The shortfall is exactly what the
// skew was missing, so it is added there rather than papered over per frame:
// the whole page-visible clock moves up with the stamp instead of trailing it.
state.stamp = value => {
    if (value < state.lastStamp) {
      state.skew += state.lastStamp - value;
      // now() is the same clock as these stamps, so it has to be the moved one.
      state.patchClock();
      return state.lastStamp;
    }
    state.lastStamp = value;
    return value;
};

state.patchClock = () => {
    if (state.clockPatched) return;
    performance.now = () => state.now();
    Date.now = () => Math.round(epochOffset + state.now());
    state.clockPatched = true;
};
state.installClock = () => {
    state.clockInstalled = true;
    state.patchClock();
};
state.restoreClock = () => {
    state.clockInstalled = false;
    // Only a page that never gained a skew gets its untouched native clock back;
    // with one, the wrapper stays in place to keep applying it.
    if (state.skew) {
        state.patchClock();
        return;
    }
    if (!state.clockPatched) return;
    performance.now = nativePerformanceNow;
    Date.now = nativeDateNow;
    state.clockPatched = false;
};

// Timer wrappers are installed once, at bootstrap, and stay pass-through while
// the gate is off. Installing them only when step mode starts would leave every
// timer a real game registered during load running on the wall clock, which is
// exactly the case that matters.
state.wrapTimer = (callback, delay, args, interval) => {
    if (typeof callback !== 'function') return null;
    const id = state.nextTimerId--;
    const depth = state.timerDepth + 1;
    // HTML clamps a timeout scheduled from inside a timer to 4ms once the chain
    // is five deep, and that clamp is all that stands between a `setTimeout(loop,
    // 0)` game loop and an unbounded run of ticks inside a single frame, every
    // one of them reading the same instant off the frozen clock.
    const floor = interval === null ? (depth > 5 ? 4 : 0) : 1;
    const wait = Math.max(floor, Number(delay) || 0);
    if (state.gated() && state.gateTimers) {
      state.timers.set(id, {
        callback: callback, args: args, interval: interval,
        due: state.now() + wait, depth: depth
      });
      // A queued timer is work that only a released frame can run, so it has to
      // be able to start the pump on its own; in step mode this does nothing and
      // the agent stays the only source of frames.
      state.schedule();
      return id;
    }
    const fire = interval === null
      ? (...inner) => { state.liveTimers.delete(id); callback(...inner); }
      : callback;
    const nativeId = interval === null
      ? nativeSetTimeout(fire, wait, ...args)
      : nativeSetInterval(fire, wait, ...args);
    state.liveTimers.set(id, {
      nativeId: nativeId, callback: callback, args: args,
      interval: interval, realDue: nativePerformanceNow() + wait
    });
    return id;
};

state.dropTimer = id => {
    if (state.timers.delete(id)) return true;
    const live = state.liveTimers.get(id);
    if (!live) return false;
    if (live.interval === null) nativeClearTimeout(live.nativeId);
    else nativeClearInterval(live.nativeId);
    state.liveTimers.delete(id);
    return true;
};

state.installTimers = () => {
    if (state.timersInstalled) return;
    window.setTimeout = (callback, delay, ...args) =>
      state.wrapTimer(callback, delay, args, null) ?? nativeSetTimeout(callback, delay, ...args);
    window.clearTimeout = id => { if (!state.dropTimer(id)) nativeClearTimeout(id); };
    window.setInterval = (callback, delay, ...args) =>
      state.wrapTimer(callback, delay, args, Math.max(1, Number(delay) || 0))
        ?? nativeSetInterval(callback, delay, ...args);
    window.clearInterval = id => { if (!state.dropTimer(id)) nativeClearInterval(id); };
    if (nativeIdle) {
      window.requestIdleCallback = (callback, options) => {
        if (typeof callback !== 'function') return nativeIdle(callback, options);
        return state.wrapTimer(
          () => callback({didTimeout: false, timeRemaining: () => 8}), 0, [], null
        );
      };
      window.cancelIdleCallback = id => { if (!state.dropTimer(id)) nativeCancelIdle(id); };
    }
    state.timersInstalled = true;
};

// Pull timers the real scheduler is already holding into the virtual queue, so
// that gating catches everything the page set up before the gate existed.
state.captureTimers = () => {
    const now = state.now();
    const real = nativePerformanceNow();
    for (const [id, entry] of state.liveTimers) {
      if (entry.interval === null) nativeClearTimeout(entry.nativeId);
      else nativeClearInterval(entry.nativeId);
      const remaining = entry.interval === null
        ? Math.max(0, entry.realDue - real)
        : entry.interval;
      state.timers.set(id, {
        callback: entry.callback, args: entry.args,
        interval: entry.interval, due: now + remaining
      });
    }
    state.liveTimers.clear();
};

// Rebase virtual deadlines onto another clock. Turning freeze_time off swaps the
// clock underneath the queue, and a deadline read against the wrong one is
// already in the past, so the whole queue would detonate on the next frame.
state.rebaseTimers = (fromNow, toNow) => {
    for (const entry of state.timers.values()) {
      entry.due = toNow + Math.max(0, entry.due - fromNow);
    }
};

// Give the queue back to the real scheduler, keeping ids valid for clearTimeout.
// `referenceNow` is the clock the deadlines were written against; the caller has
// to pass it whenever the mode is about to change.
state.releaseTimers = referenceNow => {
    const queued = Array.from(state.timers.entries());
    state.timers.clear();
    const real = nativePerformanceNow();
    const now = referenceNow === undefined ? state.now() : referenceNow;
    for (const [id, entry] of queued) {
      const fire = entry.interval === null
        ? (...inner) => { state.liveTimers.delete(id); entry.callback(...inner); }
        : entry.callback;
      const wait = entry.interval === null ? Math.max(0, entry.due - now) : entry.interval;
      const nativeId = entry.interval === null
        ? nativeSetTimeout(fire, wait, ...entry.args)
        : nativeSetInterval(fire, wait, ...entry.args);
      state.liveTimers.set(id, {
        nativeId: nativeId, callback: entry.callback, args: entry.args,
        interval: entry.interval, realDue: real + wait
      });
    }
};
// A released frame covers `frameDelta` of virtual time, and every timer whose
// deadline falls inside that span really did come due inside it. So they run in
// deadline order with the page-visible clock parked at each one's own deadline,
// and an interval keeps its phase instead of being pushed to the end of the
// frame - rescheduling from `now` stretched every period out to a whole frame,
// which made a 5ms interval tick once per frame instead of three times, and made
// a 16ms one indistinguishable from a 100ms one under a 100ms frame delta.
state.runDueTimers = (now, spanStart) => {
    if (!state.gateTimers || !state.timers.size) return 0;
    const steer = state.gated() && state.freezeTime;
    let cursor = spanStart === undefined ? now : Math.min(spanStart, now);
    let count = 0;
    while (count < 512) {
      let dueId = null;
      let due = null;
      for (const [id, entry] of state.timers) {
        if (entry.due <= now && (due === null || entry.due < due.due)) {
          dueId = id;
          due = entry;
        }
      }
      if (due === null) break;
      cursor = Math.max(cursor, Math.min(now, due.due));
      if (steer) state.virtualNow = cursor;
      if (due.interval) due.due += due.interval;
      else state.timers.delete(dueId);
      const outerDepth = state.timerDepth;
      state.timerDepth = due.depth || 1;
      try { due.callback(...due.args); }
      catch (error) { nativeSetTimeout(() => { throw error; }, 0); }
      finally { state.timerDepth = outerDepth; }
      count += 1;
    }
    if (count >= 512) {
      // The page wants more timer work than one frame can hold. Carrying the
      // backlog forward would make every later frame slower still, so the
      // stragglers give up their missed ticks the way a real browser does.
      for (const entry of state.timers.values()) {
        if (entry.interval && entry.due <= now) entry.due = now + entry.interval;
      }
    }
    if (steer) state.virtualNow = now;
    return count;
};

state.flush = () => {
    const previous = state.lastFrame;
    if (state.gated() && state.freezeTime) state.virtualNow += state.frameDelta;
    else state.virtualNow = nativePerformanceNow() + state.skew;
    const timestamp = state.stamp(state.now());
    state.lastFrame = timestamp;
    state.frameCount += 1;
    state.runDueTimers(timestamp, previous);
    const batch = Array.from(state.pending.entries());
    state.pending.clear();
    for (const [, callback] of batch) {
      try { callback(timestamp); } catch (error) { nativeSetTimeout(() => { throw error; }, 0); }
    }
    state.schedule();
    return batch.length;
};

// Keep the throttled pump running for as long as anything is waiting on a
// frame - a queued frame callback or a gated timer. Waiting only on frame
// callbacks deadlocks a page whose loop boots from a timer: that timer runs on
// the frame it was itself going to ask for. With nothing queued no timer is
// armed at all, so an idle page costs nothing and the pump restarts from
// `request` and `wrapTimer` the moment work appears.
state.pumpWanted = () =>
    state.pending.size > 0 || (state.gateTimers && state.timers.size > 0);

state.schedule = () => {
    if (state.mode !== 'throttled' || state.timer !== null || !state.pumpWanted()) return;
    const elapsed = nativePerformanceNow() - state.lastRealFlush;
    const delay = Math.max(0, state.interval - elapsed);
    const pump = () => {
      state.timer = null;
      if (state.mode !== 'throttled') return;
      state.lastRealFlush = nativePerformanceNow();
      state.flush();
    };
    // A pump that is already late is not waiting for anything, so it yields
    // rather than arming a timer a hidden tab would clamp to a second. Zero is
    // not a timer id any browser hands out, so it marks the pending yield
    // without confusing the clearTimeout in setMode.
    if (delay > 0) {
      state.timer = nativeSetTimeout(pump, delay);
    } else {
      state.timer = 0;
      yieldTask(pump);
    }
};

// While the gate is off the callback goes to the real scheduler, but it is also
// remembered: a callback already queued there when the gate engages would fire
// on the next compositor frame - one the agent never asked for, landing in the
// middle of an unrelated call - so setMode has to be able to reclaim it.
state.request = callback => {
    if (state.mode === 'normal') {
      const id = state.nativeRequest(timestamp => {
        state.native.delete(id);
        state.nativeIds.delete(id);
        // A frame timestamp is the same clock performance.now() reads, and a
        // game measures its delta from the last one it was given - which may
        // well be a stepped one. Handing over the raw native stamp after a
        // skew has been earned is the backwards jump all over again.
        callback(state.stamp(timestamp + state.skew));
      });
      state.native.set(id, callback);
      return id;
    }
    const id = state.nextId--;
    state.pending.set(id, callback);
    state.schedule();
    return id;
};

// Hand a callback back to the real scheduler under the id the page already holds:
// re-registering it under a fresh id would make the page's cancelAnimationFrame
// silently miss, and the frame it thought it cancelled still runs.
state.adopt = (id, callback) => {
    const nativeId = state.nativeRequest(timestamp => {
      state.native.delete(id);
      state.nativeIds.delete(id);
      // The stamp this callback was waiting for was going to come off the
      // stepped clock; see state.request for why it still has to.
      callback(state.stamp(timestamp + state.skew));
    });
    state.native.set(id, callback);
    state.nativeIds.set(id, nativeId);
};

state.cancel = id => {
    if (state.pending.delete(id)) return;
    const adopted = state.nativeIds.get(id);
    state.nativeIds.delete(id);
    state.native.delete(id);
    state.nativeCancel(adopted === undefined ? id : adopted);
};

// Release `count` frames, yielding to the real task queue between them so that
// network callbacks and page microtasks can run like they would in a real frame.
// The yield is a message rather than a timer because a hidden tab clamps timers:
// see yieldTask.
state.step = (count, done) => {
    let remaining = count;
    let callbacks = 0;
    const run = () => {
      callbacks += state.flush();
      remaining -= 1;
      if (remaining > 0) yieldTask(run);
      else done({
        success: true, frames: count, callbacks,
        pending_callbacks: state.pending.size,
        pending_timers: state.timers.size,
        frame_count: state.frameCount,
        virtual_now: Math.round(state.now())
      });
    };
    run();
};

state.setMode = (mode, targetFps, options) => {
    const settings = options || {};
    if (state.timer !== null) nativeClearTimeout(state.timer);
    state.timer = null;
    // Everything queued so far carries a deadline written against the clock that
    // is live right now. It has to be read before that clock is swapped.
    const previousNow = state.now();
    const nextFreeze = settings.freeze_time !== undefined
      ? !!settings.freeze_time : state.freezeTime;
    const nextGate = settings.gate_timers !== undefined
      ? !!settings.gate_timers : state.gateTimers;
    if (mode === 'normal' || !nextGate) state.releaseTimers(previousNow);
    state.mode = mode;
    state.targetFps = mode === 'throttled' ? targetFps : null;
    state.interval = 1000 / targetFps;
    if (settings.frame_delta_ms) state.frameDelta = settings.frame_delta_ms;
    state.freezeTime = nextFreeze;
    state.gateTimers = nextGate;
    if (mode === 'normal') {
      state.carryClock(previousNow);
      state.restoreClock();
      const callbacks = Array.from(state.pending.entries());
      state.pending.clear();
      for (const [id, callback] of callbacks) state.adopt(id, callback);
    } else {
      // Reclaim frames the real scheduler still owes, keeping their ids valid
      // so a later cancelAnimationFrame still finds them.
      for (const [id, callback] of state.native) {
        const adopted = state.nativeIds.get(id);
        state.nativeCancel(adopted === undefined ? id : adopted);
        state.pending.set(id, callback);
      }
      state.native.clear();
      state.nativeIds.clear();
      // Carry on from where the page's clock already is. Reading the native one
      // here threw away every skew an earlier round of stepping had earned, so
      // re-entering step mode dropped the clock just as leaving it did.
      state.virtualNow = previousNow;
      if (state.freezeTime) state.installClock();
      else { state.carryClock(previousNow); state.restoreClock(); }
      if (state.gateTimers) {
        state.rebaseTimers(previousNow, state.now());
        state.captureTimers();
      }
      state.schedule();
    }
};

window[stateKey] = state;
window.requestAnimationFrame = state.request;
window.cancelAnimationFrame = state.cancel;
// Wrap timers immediately so the ones a game registers while loading can be
// reclaimed later; while the gate is off they pass straight through.
state.installTimers();
})();
"""


_RENDER_CONTROL_SCRIPT = r"""
const mode = arguments[0];
const targetFps = arguments[1];
const options = arguments[2];
const state = window.__webSearchNeoRenderControl;
if (!state) {
  return {error: 'Render bootstrap is unavailable in this document'};
}
state.setMode(mode, targetFps, options);
return {
  mode: state.mode,
  target_fps: state.targetFps,
  pending_callbacks: state.pending.size,
  frame_delta_ms: Math.round(state.frameDelta * 1000) / 1000,
  time_frozen: state.clockInstalled,
  timers_gated: state.gated() && state.gateTimers
};
"""
