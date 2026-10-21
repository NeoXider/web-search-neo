// The popup's Computers tab: other machines running Web Search Neo on this LAN.
//
// The popup can only talk to this worker, and this worker can only talk to the
// local bridge daemon, over the socket it already authenticated. So each popup
// request becomes one {"type": "nodes", id, op, ...} frame and the daemon
// (web_search_neo/node_link.py) answers {"type": "nodes_result", id, result|error}.
// Nothing here reaches the network on its own.
//
// It also owns the pairing notification: when another computer asks to pair,
// this computer's node sends "companion.pairRequest" through the bridge and a
// Chrome notification shows the asking computer and its code, with Allow and
// Deny. Either button answers through the same socket.

const REQUEST_TIMEOUT_MS = 20000;
// Starting a node server waits for it to come up, and a LAN scan probes every answer.
const SLOW_OPS = new Set(["share", "rotate_key", "send_tab", "pair"]);
const SLOW_TIMEOUT_MS = 45000;
// Windows' UAC prompt waits for the person; the daemon gives up after 150 s. Switching
// sharing on may ask the same question.
const OP_TIMEOUT_MS = {firewall: 160000, share: 175000};
const PAIR_NOTICE = "wsn-pair-";
const SOCKET_WAIT_MS = 6000;

export function createNodeRelay(liveSocket) {
  const pending = new Map();
  let nextId = 1;

  function request(op, params = {}) {
    const socket = liveSocket();
    if (!socket) {
      return Promise.reject(new Error(
        "The bridge is not connected, so other computers cannot be reached. " +
        "Start an agent on this computer (or `wsn --node`), then try again.",
      ));
    }
    const id = `nodes-${Date.now()}-${nextId++}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("The bridge did not answer in time; an older bridge may not know computers yet."));
      }, OP_TIMEOUT_MS[op] || (SLOW_OPS.has(op) ? SLOW_TIMEOUT_MS : REQUEST_TIMEOUT_MS));
      pending.set(id, {resolve, reject, timer});
      try {
        socket.send(JSON.stringify({type: "nodes", id, op, ...params}));
      } catch (error) {
        clearTimeout(timer);
        pending.delete(id);
        reject(error);
      }
    });
  }

  // Returns true when the frame was a nodes answer (consumed here).
  function deliver(message) {
    if (message?.type !== "nodes_result") return false;
    const entry = pending.get(message.id);
    if (!entry) return true;
    pending.delete(message.id);
    clearTimeout(entry.timer);
    if (message.error) entry.reject(new Error(String(message.error)));
    else entry.resolve(message.result);
    return true;
  }

  async function activeTabUrl() {
    const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
    const url = tab?.url || tab?.pendingUrl || "";
    if (!/^https?:\/\//i.test(url)) throw new Error("Open an http(s) page first; only web pages can be sent.");
    return url;
  }

  // Popup messages; returns a promise, or null for a type this module does not own.
  function message(msg) {
    switch (msg?.type) {
      case "companion.nodes":
        return request("overview", {discover: Boolean(msg.discover), refresh: Boolean(msg.refresh)});
      case "companion.nodeAdd":
        return request("add", {name: msg.name || "", address: msg.address || "", key: msg.key || ""});
      case "companion.nodeRemove":
        return request("remove", {name: msg.name || ""});
      case "companion.nodeShare":
        return request("share", {enabled: Boolean(msg.enabled)});
      case "companion.nodeRotateKey":
        return request("rotate_key");
      case "companion.nodeSendTab":
        return activeTabUrl().then(url => request("send_tab", {node: msg.node || "", url}));
      case "companion.nodePair":
        return request("pair", {address: msg.address || "", node: msg.node || ""});
      case "companion.nodePairPoll":
        return request("pair_poll", {attempt: msg.attempt || ""});
      case "companion.nodePairCancel":
        return request("pair_cancel", {attempt: msg.attempt || ""});
      case "companion.nodePairDecide":
        return decide(String(msg.request || ""), msg.allow === true);
      case "companion.nodeFirewall":
        return request("firewall");
      default:
        return null;
    }
  }

  // A click on a notification can wake this worker before its socket is back.
  async function whenConnected() {
    const until = Date.now() + SOCKET_WAIT_MS;
    while (!liveSocket() && Date.now() < until) await new Promise(done => setTimeout(done, 300));
  }

  async function decide(requestId, allow) {
    await whenConnected();
    const answer = await request("pair_decide", {request: requestId, allow});
    chrome.notifications?.clear?.(PAIR_NOTICE + requestId);
    return answer;
  }

  // The bridge command a pairing request arrives as; strings are shown, never parsed.
  async function showPairRequest(params = {}) {
    const requestId = String(params.request || "");
    if (!/^[0-9a-f]{8,32}$/.test(requestId)) throw new Error("pairing request id is malformed");
    const name = String(params.name || "Another computer").slice(0, 32);
    const address = String(params.address || "").slice(0, 64);
    const code = String(params.code || "").slice(0, 8);
    if (!chrome.notifications?.create) return {shown: false};
    await chrome.notifications.create(PAIR_NOTICE + requestId, {
      type: "basic",
      iconUrl: "icons/slime-128.png",
      title: `${name} wants to drive this Chrome`,
      message: `Code ${code}. Allow only if ${name} shows the same code.`,
      contextMessage: `${address} · Web Search Neo pairing`,
      buttons: [{title: "Allow"}, {title: "Deny"}],
      requireInteraction: true,
      priority: 2,
    });
    return {shown: true};
  }

  chrome.notifications?.onButtonClicked?.addListener((noticeId, index) => {
    if (!String(noticeId).startsWith(PAIR_NOTICE)) return;
    decide(String(noticeId).slice(PAIR_NOTICE.length), index === 0).catch(error => {
      console.warn("companion: pairing answer failed", error);
      chrome.action?.openPopup?.().catch(() => {});
    });
  });
  chrome.notifications?.onClicked?.addListener(noticeId => {
    if (String(noticeId).startsWith(PAIR_NOTICE)) chrome.action?.openPopup?.().catch(() => {});
  });

  return {message, deliver, request, commands: {"companion.pairRequest": showPairRequest}};
}
