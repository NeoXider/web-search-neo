// One debugger attachment per tab, however many commands ask for it at once.
//
// Attaching is the slowest thing the companion does. On a machine whose CPU and
// GPU are busy with other work Chrome can take ten seconds to attach to a fresh
// tab and answer the setup commands, where an idle one takes a tenth of that.
// Two things used to make it slower still. Every command that arrived while the
// first attach was running began its own attach and its own setup, so a new tab
// was set up two or three times over, each pass queued behind the last; and the
// setup commands went out one after another although none of them depends on
// another. Together that pushed an ordinary first command past its timeout, and
// the agent was told the bridge had timed out when Chrome was merely busy.
//
// Here the first caller attaches, everyone else waits on the same promise, and
// what follows Runtime.enable goes out together. The answer carries how long
// each part took, so a slow machine shows up as a number rather than a failure.

const AUTO_ATTACH = {autoAttach: true, waitForDebuggerOnStart: false, flatten: true};

export function createDebuggerAttacher({
  chromeApi, version, attachedTabs, restoreState, bufferFor, forgetTab, persistState, sendSafe,
  now = () => Date.now(),
}) {
  const inFlight = new Map(); // tabId -> the attach that is running for it
  const timings = new Map();  // tabId -> how long its last attach took

  async function attach(tabId) {
    const started = now();
    const chrome = chromeApi();
    await restoreState();
    bufferFor(tabId);
    if (attachedTabs.has(tabId)) {
      try {
        // chrome.storage.session outlives an MV3 worker. Usually Chrome keeps the
        // debugger attachment too, but it can drop just that attachment (a tab
        // crash, an extension reload, or the old MCP session letting go) while
        // the stored Set still says it is present. Trusting the shelf then makes
        // every later command fail with "Debugger is not attached" and, because
        // this early return never repairs it, the tab stays unusable forever.
        await chrome.debugger.sendCommand({tabId}, "Runtime.enable");
        return {tab_id: tabId, attached: true, reused: true, total_ms: now() - started};
      } catch (error) {
        console.warn(`bridge: forgot a stale debugger attachment for tab ${tabId}`, error);
        // Preserve already-recorded events, but make capture and child sessions
        // earn their state again after the fresh debugger attachment below.
        forgetTab(tabId, false);
      }
    }
    try {
      await chrome.debugger.attach({tabId}, version);
    } catch (error) {
      // Chrome refuses a second attach both when the debugger already on the tab
      // is this worker's own - the ordinary case after an eviction, since Chrome
      // keeps the attachment that the worker forgot - and when it belongs to
      // somebody else: a DevTools window, another extension. The message does not
      // reliably tell those apart, so this does not try to. The setup below is
      // what decides: a session that is ours answers it, and one that is not
      // throws, leaving the tab unattached rather than recorded as ready.
      if (!/already attached/i.test(String(error?.message || error))) throw error;
    }
    const attachedAt = now();
    try {
      await chrome.debugger.sendCommand({tabId}, "Runtime.enable");
      // Focus emulation is what makes a tab that was never in front controllable,
      // which measurement rather than theory established: without it
      // Input.dispatchKeyEvent and dispatchMouseEvent return success and are then
      // dropped on the floor, requestAnimationFrame never fires, and setTimeout is
      // clamped to one second, then to sixty after a minute or so hidden. With it
      // the same hidden tab measured 49 fps and 4.5 ms timers - foreground
      // numbers - and input arrives. The cost is that the page is told it is
      // focused and visible when it is not, so a script watching visibilitychange
      // sees a tab that never hides. For a page an agent is driving that is closer
      // to the truth than the alternative. The setting belongs to this debugger
      // session and dies with it.
      await Promise.all([
        sendSafe(tabId, "Page.enable"),
        sendSafe(tabId, "Log.enable"),
        sendSafe(tabId, "Target.setAutoAttach", AUTO_ATTACH),
        sendSafe(tabId, "Emulation.setFocusEmulationEnabled", {enabled: true}),
      ]);
    } catch (error) {
      // Nothing here is optional: a tab that skipped this is the one that accepts
      // keystrokes and drops them. So it is not recorded, the half-open session is
      // let go, and the next call attaches again - which is what recovers a tab
      // whose DevTools window the user has meanwhile closed.
      await chrome.debugger.detach({tabId}).catch(() => {});
      throw new Error(
        `Chrome would not let the companion drive tab ${tabId}: ` +
          `${error?.message || error}. Something else is debugging that tab - close its ` +
          "DevTools window (or the other extension using it) and try again.",
      );
    }
    attachedTabs.add(tabId);
    persistState();
    const finished = now();
    return {
      tab_id: tabId, attached: true, reused: false,
      attach_ms: attachedAt - started, setup_ms: finished - attachedAt, total_ms: finished - started,
    };
  }

  // Everyone who asks while an attach is running gets that attach, not a second one.
  function ensure(rawTabId) {
    const tabId = Number(rawTabId);
    let running = inFlight.get(tabId);
    if (!running) {
      running = attach(tabId)
        .then(timing => {
          timings.set(tabId, timing);
          return timing;
        })
        .finally(() => {
          if (inFlight.get(tabId) === running) inFlight.delete(tabId);
        });
      inFlight.set(tabId, running);
    }
    return running;
  }

  return {
    ensure,
    lastTiming: tabId => timings.get(Number(tabId)) || null,
    forget: tabId => timings.delete(Number(tabId)),
  };
}
