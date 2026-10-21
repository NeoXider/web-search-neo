// The popup's Computers tab: other machines running Web Search Neo on this LAN.
//
// The popup can only talk to this worker, and this worker can only talk to the
// local bridge daemon, over the socket it already authenticated. So each popup
// request becomes one {"type": "nodes", id, op, ...} frame and the daemon
// (web_search_neo/node_link.py) answers {"type": "nodes_result", id, result|error}.
// Nothing here reaches the network on its own.

const REQUEST_TIMEOUT_MS = 20000;
// Starting a node server waits for it to come up, and a LAN scan probes every answer.
const SLOW_OPS = new Set(["share", "rotate_key", "send_tab"]);
const SLOW_TIMEOUT_MS = 45000;

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
      }, SLOW_OPS.has(op) ? SLOW_TIMEOUT_MS : REQUEST_TIMEOUT_MS);
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
      default:
        return null;
    }
  }

  return {message, deliver, request};
}
