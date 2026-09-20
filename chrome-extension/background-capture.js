// One fresh compositor video frame; no activation, window mutation or emulation.
// Unlike surface snapshots this path does not wait for an on-screen presentation.
export function createBackgroundCapture(debuggerApi, timeoutMs = 8000, cleanupTimeoutMs = 1000) {
  const pending = new Map();
  return async function capture(tabId) {
    if (pending.has(tabId)) throw new Error('A background capture is already pending for this tab');
    const target = {tabId};
    let timer, resolveFrame, rejectFrame, earlyFrame;
    let startSucceeded = false, finished = false;
    const frame = new Promise((resolve, reject) => { resolveFrame = resolve; rejectFrame = reject; });
    const onEvent = (source, method, params) => {
      if (source.tabId !== tabId || source.sessionId || method !== 'Page.screencastFrame') return;
      if (!params?.data || !Number.isInteger(params.sessionId)) return;
      if (!startSucceeded) { earlyFrame = params; return; }
      if (finished) return;
      debuggerApi.sendCommand(target, 'Page.screencastFrameAck', {sessionId: params.sessionId})
        .then(() => resolveFrame({data: params.data}), rejectFrame);
    };
    const onDetach = source => { if (source.tabId === tabId) rejectFrame(new Error('Tab debugger detached during background capture')); };
    pending.set(tabId, true);
    debuggerApi.onEvent.addListener(onEvent);
    debuggerApi.onDetach.addListener(onDetach);
    timer = setTimeout(() => rejectFrame(new Error('Background capture produced no frame. Use page_elements/page_text; do not call show or activate a window without the user asking.')), timeoutMs);
    // A rejected start (including an existing recording) does not belong to us
    // and must never be followed by stopScreencast.
    const started = Promise.resolve().then(() => debuggerApi.sendCommand(target, 'Page.startScreencast', {format: 'png', everyNthFrame: 1}))
      .then(() => { startSucceeded = true; if (earlyFrame && !finished) onEvent(target, 'Page.screencastFrame', earlyFrame); });
    try {
      const [, result] = await Promise.all([started, frame]);
      return result;
    } finally {
      clearTimeout(timer);
      finished = true;
      debuggerApi.onEvent.removeListener(onEvent);
      debuggerApi.onDetach.removeListener(onDetach);
      // Also cleans up a start that completes after timeout. Keep the tab locked
      // until that cleanup ends so a late stop cannot terminate a newer capture.
      const cleanup = started.then(() => debuggerApi.sendCommand(target, 'Page.stopScreencast'))
        .catch(() => {}).finally(() => pending.delete(tabId));
      if (startSucceeded) {
        let cleanupTimer;
        try { await Promise.race([cleanup, new Promise(resolve => { cleanupTimer = setTimeout(resolve, cleanupTimeoutMs); })]); }
        finally { clearTimeout(cleanupTimer); }
      }
    }
  };
}
