// The companion's side of the bridge handshake (protocol 2). The raw token never
// leaves the extension, and nothing derived from it is sent until the daemon has
// proved it holds the same secret:
//
//   companion -> daemon  {type: "hello", protocol: 2, role: "extension", nonce: Nc, browser}
//   daemon -> companion  {type: "challenge", protocol: 2, nonce: Ns,
//                         proof: HMAC(token, "wsn-bridge-server|Nc|Ns")}
//   companion -> daemon  {type: "auth", proof: HMAC(token, "wsn-bridge-client|Ns|Nc")}
//   daemon -> companion  {type: "hello_ack", protocol: 2}
//
// The Python side lives in web_search_neo/bridge_handshake.py.

export const PROTOCOL_VERSION = 2;
const PROOF_PATTERN = /^[0-9a-f]{64}$/;

export function parseBridgeToken(source) {
  const match = /BRIDGE_TOKEN\s*=\s*["']([0-9a-f]{64})["']/.exec(String(source ?? ""));
  if (!match) throw new Error("bridge-token.js holds no usable token");
  return match[1];
}

// bridge-token.js is written by the Python side and is absent in a fresh clone.
// It is read rather than imported on purpose: the worker's module map caches a
// failed import for the life of the worker, so a token file that appears after
// the first attempt would never be seen without a manual reload.
export async function loadBridgeToken() {
  const response = await fetch(chrome.runtime.getURL("bridge-token.js"), {cache: "no-store"});
  if (!response.ok) throw new Error(`bridge-token.js is unreadable (HTTP ${response.status})`);
  return parseBridgeToken(await response.text());
}

export function bytesToHex(bytes) {
  return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

export function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length >> 1);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

export function timingSafeEqual(left, right) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left[index] ^ right[index];
  return difference === 0;
}

export function newNonce() {
  return bytesToHex(crypto.getRandomValues(new Uint8Array(16)));
}

export async function hmacHex(token, message) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(token),
    {name: "HMAC", hash: "SHA-256"},
    false,
    ["sign"],
  );
  return bytesToHex(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
}

export const serverProofMessage = (clientNonce, serverNonce) =>
  `wsn-bridge-server|${clientNonce}|${serverNonce}`;
export const clientProofMessage = (serverNonce, clientNonce) =>
  `wsn-bridge-client|${serverNonce}|${clientNonce}`;

// The auth frame answering a daemon's challenge, or null when the daemon did not
// prove it knows the token (in which case nothing may be sent back).
export async function answerChallenge(token, clientNonce, challenge) {
  const nonce = typeof challenge?.nonce === "string" ? challenge.nonce : "";
  const proof = typeof challenge?.proof === "string" ? challenge.proof : "";
  if (challenge?.protocol !== PROTOCOL_VERSION) return null;
  if (nonce.length < 16 || nonce.length > 256 || !PROOF_PATTERN.test(proof)) return null;
  const expected = await hmacHex(token, serverProofMessage(clientNonce, nonce));
  if (!timingSafeEqual(hexToBytes(proof), hexToBytes(expected))) return null;
  return {type: "auth", proof: await hmacHex(token, clientProofMessage(nonce, clientNonce))};
}

// One unverified frame. `state.stage` walks hello -> challenged -> answered;
// `isCurrent()` says whether the socket is still the live one, and `onVerified`
// runs once the daemon acknowledged our proof. Anything out of order closes.
export function advanceHandshake(connection, message, state, {token, nonce, isCurrent, onVerified}) {
  if (message.type === "challenge" && state.stage === "hello") {
    state.stage = "challenged";
    answerChallenge(token, nonce, message)
      .then(auth => {
        if (!auth) {
          console.warn("bridge: the server did not prove it knows the companion token");
          connection.close();
          return;
        }
        if (!isCurrent()) return;
        connection.send(JSON.stringify(auth));
        state.stage = "answered";
      })
      .catch(error => {
        console.warn("bridge: could not check the server proof", error);
        connection.close();
      });
    return;
  }
  if (message.type === "hello_ack" && state.stage === "answered" &&
      message.protocol === PROTOCOL_VERSION) {
    state.stage = "verified";
    onVerified();
    return;
  }
  // Anything else - a command above all - means the peer is not the local
  // server we share a secret with.
  console.warn(`bridge: closing an unverified peer that sent ${message.type}`);
  connection.close();
}
